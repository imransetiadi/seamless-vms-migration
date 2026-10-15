import asyncio
from types import SimpleNamespace

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.models import VerificationConfig
from seamless_migrate.verification import Verifier
from tests.factories import make_migration, make_plan


class Dest:
    def __init__(
        self, status="ACTIVE", port_status="ACTIVE", console="web-01 login: ", address="127.0.0.1"
    ):
        self.status = status
        self.port_status = port_status
        self.console = console
        self.address = address
        self.polls = 0

    async def get_server(self, server_id):
        self.polls += 1
        return {
            "status": self.status,
            "ports": [
                {
                    "status": self.port_status,
                    "fixed_ips": [self.address],
                    "floating_ips": ["127.0.0.1"],
                }
            ],
        }

    async def console_log(self, server_id, lines=200):
        return self.console


def ctx(tcp_ports=(), timeout_s=1, **kw):
    plan = make_plan(
        verification=VerificationConfig(tcp_ports=list(tcp_ports), timeout_s=timeout_s, **kw)
    )
    return SimpleNamespace(plan=plan, migration=make_migration(destination_server_id="srv-9"))


@pytest.fixture
async def listener():
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield port
    server.close()
    await server.wait_closed()


def unused_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_verification_checks(listener):
    verifier = Verifier(Dest(), Settings(), poll_s=0.01)
    result = await verifier.verify(ctx(tcp_ports=[listener]))
    assert result.passed is True
    names = [c["name"] for c in result.checks]
    assert names == ["server_active", "ports_up", f"tcp:{listener}", "console"]
    assert all(c["ok"] for c in result.checks)
    assert "login:" in result.evidence["console"]

    # every check failing on its own makes the result fail
    closed = unused_port()
    failing = [
        (Dest(status="ERROR"), [], "server_active"),
        (Dest(port_status="DOWN"), [], "ports_up"),
        (Dest(), [closed], f"tcp:{closed}"),
        (Dest(console="Kernel panic - not syncing: VFS"), [], "console"),
    ]
    for dest, ports, bad in failing:
        res = await Verifier(dest, Settings(), poll_s=0.01).verify(
            ctx(tcp_ports=ports, timeout_s=0)
        )
        assert res.passed is False, bad
        assert [c["name"] for c in res.checks if not c["ok"]] == [bad]


async def test_verification_fails_fast_on_a_terminal_server_state():
    """ERROR never becomes ACTIVE: no polling until the timeout, inside the downtime window."""
    dest = Dest(status="ERROR")
    result = await Verifier(dest, Settings(), poll_s=0.01).verify(ctx(timeout_s=600))
    assert result.passed is False and result.evidence["attempts"] == 1
    assert result.evidence["terminal"] is True
    assert [c["name"] for c in result.checks if not c["ok"]] == ["server_active"]


async def test_verification_polls_until_timeout_then_passes():
    dest = Dest(status="BUILD")
    verifier = Verifier(dest, Settings(), poll_s=0.01)

    async def boot_later():
        await asyncio.sleep(0.05)
        dest.status = "ACTIVE"

    asyncio.ensure_future(boot_later())
    result = await verifier.verify(ctx(timeout_s=5))
    assert result.passed and dest.polls > 1


async def test_verification_console_patterns_and_floating_probe(listener):
    dest = Dest(console="Cloud-init v. 23.4 finished at Tue", address="10.255.255.1")
    res = await Verifier(dest, Settings(), poll_s=0.01).verify(
        ctx(tcp_ports=[listener], probe_address="floating")
    )
    assert res.passed, res.checks


async def test_verification_console_unavailable_skips_with_warning():
    res = await Verifier(Dest(console=None), Settings(), poll_s=0.01).verify(ctx())
    assert res.passed is True
    console = [c for c in res.checks if c["name"] == "console"][0]
    assert console["skipped"] is True and "unavailable" in console["detail"]
    assert res.evidence["warnings"] == ["console log unavailable; console check skipped"]
    assert "console" not in res.evidence


async def test_verification_without_destination_server_fails_fast():
    c = ctx()
    c.migration.destination_server_id = None
    res = await Verifier(Dest(), Settings(), poll_s=0.01).verify(c)
    assert res.passed is False and res.checks[0]["name"] == "server_active"


async def test_verification_edge_paths(listener):
    from seamless_migrate.providers.base import ProviderError
    from seamless_migrate.verification import _pattern_match, _tcp_probe

    # an invalid regex falls back to a plain substring match
    assert _pattern_match(["login: ", "(unclosed"], "x (unclosed y") == "(unclosed"
    assert _pattern_match(["(unclosed"], "nothing") is None
    assert (
        _pattern_match([r"Reached target .*Multi-User"], "Reached target Multi-User System")
        is not None
    )
    # a closed port is reported unreachable, an open one with its connect time
    ok, detail = await _tcp_probe("127.0.0.1", unused_port())
    assert ok is False and "unreachable" in detail
    ok, detail = await _tcp_probe("127.0.0.1", listener)
    assert ok is True and "connected" in detail

    # the destination provider failing to describe the server fails the verification
    class Broken(Dest):
        async def get_server(self, server_id):
            raise ProviderError("HTTP 503 from nova")

    settings = Settings(data_dir="/tmp/x")
    result = await Verifier(Broken(), settings, poll_s=0.01).verify(ctx())
    assert result.passed is False and result.failed_checks() == ["server_active"]
    assert "HTTP 503" in result.checks[0]["detail"]

    # no address of the requested kind: every TCP check fails with a clear reason
    class NoAddress(Dest):
        async def get_server(self, server_id):
            return {
                "status": "ACTIVE",
                "ports": [{"status": "ACTIVE", "fixed_ips": [], "floating_ips": []}],
            }

    result = await Verifier(NoAddress(), settings, poll_s=0.01).verify(ctx(tcp_ports=[22]))
    assert result.passed is False
    tcp = next(c for c in result.checks if c["name"] == "tcp:22")
    assert tcp["ok"] is False and "no fixed address" in tcp["detail"]
    summary = result.summary()
    assert summary["passed"] is False and [c["name"] for c in summary["checks"] if not c["ok"]] == [
        "tcp:22"
    ]


def windows_ctx(tcp_ports=(), windows_tcp_ports=(), timeout_s=0):
    from tests.factories import make_vm

    plan = make_plan(
        verification=VerificationConfig(
            tcp_ports=list(tcp_ports),
            windows_tcp_ports=list(windows_tcp_ports),
            timeout_s=timeout_s,
        )
    )
    vm = make_vm(os_type="windows2019srvNext_64Guest")
    return SimpleNamespace(
        plan=plan, migration=make_migration(destination_server_id="srv-9", vm=vm)
    )


async def test_tcp_probe_counts_an_unusable_port_as_closed():
    """A port asyncio refuses (above 65535, negative) is a closed port, never an exception that
    would fail the verification step and roll back a good cutover (SDD §7.5)."""
    from seamless_migrate.verification import _tcp_probe

    for port in (70000, -1):
        ok, detail = await _tcp_probe("127.0.0.1", port)
        assert ok is False and str(port) in detail


async def test_verification_windows_guest_skips_console_and_probes_windows_ports(listener):
    """SDD §7.5: Windows writes nothing to the serial console; RDP/WinRM ports replace SSH."""
    closed = unused_port()
    dest = Dest(console="")  # an empty serial console, as Windows leaves it
    result = await Verifier(dest, Settings(), poll_s=0).verify(
        windows_ctx(tcp_ports=[closed], windows_tcp_ports=[listener])
    )
    assert result.passed, result.checks
    names = {c["name"]: c for c in result.checks}
    assert f"tcp:{listener}" in names and f"tcp:{closed}" not in names
    assert names["console"]["skipped"] is True and "Windows" in names["console"]["detail"]


async def test_verification_windows_guest_without_ports_passes_with_a_warning():
    result = await Verifier(Dest(console=""), Settings(), poll_s=0).verify(windows_ctx())
    assert result.passed
    assert any("no TCP port" in w for w in result.evidence["warnings"])


# -- application probes (SDD §7.5, ADV-18) ----------------------------------------------------
from seamless_migrate.domain.models import AppProbe  # noqa: E402


def app_ctx(probes, *, overrides=None, vm=None, timeout_s=0):
    plan = make_plan(
        verification=VerificationConfig(timeout_s=timeout_s, app_probes=list(probes)),
        probe_overrides=overrides or {},
    )
    kw = {"vm": vm} if vm is not None else {}
    return SimpleNamespace(plan=plan, migration=make_migration(destination_server_id="srv-9", **kw))


@pytest.fixture
async def http_server():
    """A local HTTP server answering with the status the test sets (default 200)."""
    state = {"status": 200, "paths": []}

    async def handle(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        state["paths"].append(request.split(b" ")[1].decode())
        writer.write(
            f"HTTP/1.1 {state['status']} X\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    state["port"] = server.sockets[0].getsockname()[1]
    yield state
    server.close()
    await server.wait_closed()


async def test_an_application_probe_that_answers_passes_with_its_target_latency_and_time(listener):
    result = await Verifier(Dest(), Settings(), poll_s=0.01).verify(
        app_ctx([AppProbe(kind="tcp", port=listener)])
    )
    assert result.passed, result.checks
    check = next(c for c in result.checks if c["name"] == f"app:tcp:{listener}")
    assert check["ok"] is True
    (probe,) = result.probes
    assert (probe.target, probe.kind, probe.result) == (f"127.0.0.1:{listener}", "tcp", "passed")
    assert probe.latency_ms is not None and probe.latency_ms >= 0
    assert probe.at.tzinfo is not None
    record = result.record()
    assert record.passed and [p.target for p in record.probes] == [f"127.0.0.1:{listener}"]
    assert result.summary()["probes"][0]["result"] == "passed"


async def test_a_refused_application_probe_fails_the_verification():
    closed = unused_port()
    result = await Verifier(Dest(), Settings(), poll_s=0.01).verify(
        app_ctx([AppProbe(kind="tcp", port=closed)])
    )
    assert result.passed is False
    assert result.failed_checks() == [f"app:tcp:{closed}"]
    (probe,) = result.probes
    assert probe.result == "failed" and "refused" in probe.detail and probe.latency_ms is None


async def test_http_probes_read_the_status_of_the_path(http_server):
    port = http_server["port"]
    ok = await Verifier(Dest(), Settings(), poll_s=0.01).verify(
        app_ctx([AppProbe(kind="http", port=port, path="/healthz")])
    )
    assert ok.passed, ok.checks
    assert http_server["paths"] == ["/healthz"]
    (probe,) = ok.probes
    assert probe.target == f"http://127.0.0.1:{port}/healthz" and probe.detail == "HTTP 200"
    http_server["status"] = 503
    bad = await Verifier(Dest(), Settings(), poll_s=0.01).verify(
        app_ctx([AppProbe(kind="http", port=port)])
    )
    assert (
        bad.passed is False
        and bad.probes[0].result == "failed"
        and bad.probes[0].detail == "HTTP 503"
    )
    # an expected status is matched exactly
    http_server["status"] = 204
    exact = await Verifier(Dest(), Settings(), poll_s=0.01).verify(
        app_ctx([AppProbe(kind="http", port=port, expect_status=200)])
    )
    assert exact.passed is False and exact.probes[0].detail == "HTTP 204, expected 200"


async def test_an_application_probe_goes_to_the_guest_never_through_a_proxy(
    http_server, monkeypatch
):
    """SDD §7.5: a probe runs from the control plane against the probe address - an HTTP(S) proxy
    in the environment (a proxied deployment sets one) never answers for the guest."""
    proxied: list[bytes] = []

    async def proxy(reader, writer):
        proxied.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    try:
        ok = await Verifier(Dest(), Settings(), poll_s=0.01).verify(
            app_ctx([AppProbe(kind="http", port=http_server["port"], path="/healthz")])
        )
    finally:
        server.close()
        await server.wait_closed()
    assert proxied == [] and http_server["paths"] == ["/healthz"]
    assert ok.passed and ok.probes[0].detail == "HTTP 200"


async def test_https_probes_do_not_verify_the_certificate(monkeypatch):
    """The probe connects to an address the certificate does not name: it reads only the status."""
    import seamless_migrate.verification as verification

    seen = {}

    class Response:
        status_code = 200

    class Client:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            seen["url"] = url
            return Response()

    monkeypatch.setattr(verification.httpx, "AsyncClient", Client)
    answer = await verification._app_probe(
        "10.0.0.5", AppProbe(kind="https", port=8443, path="/health")
    )
    assert answer.result == "passed" and seen["verify"] is False
    assert seen["trust_env"] is False, "https goes to the guest too, never through a proxy"
    assert seen["url"] == "https://10.0.0.5:8443/health"
    # an IPv6 address is bracketed in the URL
    await verification._app_probe("fd00::5", AppProbe(kind="http", port=80))
    assert seen["url"] == "http://[fd00::5]:80/"


async def test_an_unreachable_probe_is_retried_until_the_deadline_then_passes_for_review():
    """Owner's open question 15: unreachable from the control plane is not a failure (SDD §7.5)."""
    from seamless_migrate.verification import ProbeAnswer

    calls = []

    async def unreachable(host, probe):
        calls.append(probe.port)
        return ProbeAnswer("unreachable", f"{host}:{probe.port} unreachable (TimeoutError)", False)

    clock = {"t": 0.0}

    async def sleep(seconds):
        clock["t"] += max(seconds, 0.5)

    verifier = Verifier(
        Dest(), Settings(), poll_s=1, sleep=sleep, clock=lambda: clock["t"], app_probe=unreachable
    )
    result = await verifier.verify(app_ctx([AppProbe(kind="tcp", port=5432)], timeout_s=5))
    assert result.passed is True
    assert len(calls) > 1  # polled while it was unreachable
    assert result.unreachable_targets() == ["127.0.0.1:5432"]
    check = next(c for c in result.checks if c["name"] == "app:tcp:5432")
    assert check["skipped"] is True and "unreachable" in check["detail"]
    assert result.probes[0].result == "unreachable" and result.probes[0].latency_ms is None


async def test_a_vm_runs_its_probe_override(listener):
    vm_probes = {"vm-1": [AppProbe(kind="tcp", port=listener)]}
    closed = unused_port()
    result = await Verifier(Dest(), Settings(), poll_s=0.01).verify(
        app_ctx([AppProbe(kind="tcp", port=closed)], overrides=vm_probes)
    )
    assert result.passed, result.checks
    assert [p.target for p in result.probes] == [f"127.0.0.1:{listener}"]


async def test_a_destination_that_simulates_its_guests_answers_the_probes():
    """Demo mode (SDD §7.4): the fake destination says what the guest runs; nothing leaves."""
    from seamless_migrate.verification import ProbeAnswer

    asked = []

    class Simulated(Dest):
        async def probe_service(self, server_id, vm, probe, address):
            asked.append((server_id, vm.name, probe.port, address))
            return ProbeAnswer("failed", f"{address}:{probe.port} refused the connection", False)

    result = await Verifier(Simulated(address="10.30.0.7"), Settings(), poll_s=0.01).verify(
        app_ctx([AppProbe(kind="tcp", port=1521)])
    )
    assert asked == [("srv-9", "web-01", 1521, "10.30.0.7")]
    assert result.passed is False and result.probes[0].target == "10.30.0.7:1521"


async def test_a_destination_that_simulates_its_guests_answers_the_tcp_checks_too():
    """Demo mode (SDD §7.4): the plan's TCP checks go to the simulated guest as its application
    probes do - SSH answers, another port is refused - and no real connection is tried."""
    from seamless_migrate.verification import ProbeAnswer

    asked = []

    class Simulated(Dest):
        async def probe_service(self, server_id, vm, probe, address):
            asked.append((probe.kind, probe.port, vm.guest_os.family))
            if probe.port in (22, 3389):
                return ProbeAnswer("passed", f"connected to {address}:{probe.port}", True)
            if probe.port == 9999:
                return ProbeAnswer("unreachable", f"{address}:{probe.port} unreachable", False)
            return ProbeAnswer("failed", f"{address}:{probe.port} refused the connection", False)

    async def no_network(host, port):
        raise AssertionError(f"a real connection to {host}:{port}")

    verifier = Verifier(Simulated(address="10.30.0.81"), Settings(), poll_s=0.01, probe=no_network)
    linux = ctx(tcp_ports=[22, 8080, 9999], timeout_s=0)
    result = await verifier.verify(linux)
    checks = {c["name"]: (c["ok"], c["detail"]) for c in result.checks}
    assert checks["tcp:22"] == (True, "connected to 10.30.0.81:22")
    assert checks["tcp:8080"] == (False, "10.30.0.81:8080 refused the connection")
    # unreached is not answered: a TCP check fails on it, as on a real timeout
    assert checks["tcp:9999"] == (False, "10.30.0.81:9999 unreachable")
    assert asked == [("tcp", 22, "linux"), ("tcp", 8080, "linux"), ("tcp", 9999, "linux")]
    asked.clear()
    windows = await verifier.verify(windows_ctx(tcp_ports=[22], windows_tcp_ports=[3389]))
    assert {c["name"]: c["ok"] for c in windows.checks}["tcp:3389"] is True
    assert asked == [("tcp", 3389, "windows")]


async def test_without_its_vm_a_tcp_check_goes_to_the_address_itself(listener):
    """The simulation answers for the VM it knows (SDD §7.4): a context without one probes the
    address itself."""

    class Simulated(Dest):
        async def probe_service(self, server_id, vm, probe, address):
            raise AssertionError("the simulation needs the VM")

    plan = make_plan(verification=VerificationConfig(tcp_ports=[listener], timeout_s=0))
    context = SimpleNamespace(plan=plan, migration=SimpleNamespace(destination_server_id="srv-9"))
    result = await Verifier(Simulated(), Settings(), poll_s=0.01).verify(context)
    assert {c["name"]: c["ok"] for c in result.checks}[f"tcp:{listener}"] is True


async def test_demo_verification_runs_in_model_time():
    """SDD §7.5: in demo mode the timeout and the polling run divided by demo_speed."""
    clock = {"t": 0.0}
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    dest = Dest(status="BUILD")
    settings = Settings(demo=True, demo_speed=60)
    result = await Verifier(
        dest, settings, poll_s=10, sleep=sleep, clock=lambda: clock["t"]
    ).verify(ctx(timeout_s=120))
    assert result.passed is False
    assert clock["t"] == pytest.approx(2.0)  # 120 s of model time
    assert max(slept) == pytest.approx(10 / 60)


async def test_a_probe_failure_says_whether_the_guest_answered(monkeypatch):
    """Refused or a TLS error: the guest answered and its service did not (failed). A read timeout
    after connecting fails too; only an address the control plane cannot reach is unreachable."""
    import ssl

    import httpx

    import seamless_migrate.verification as verification

    def chained(cause):
        try:
            raise cause
        except Exception as inner:
            try:
                raise httpx.ConnectError("connect failed") from inner
            except httpx.ConnectError as outer:
                return outer

    tls = verification._connect_failure("10.0.0.5:443", chained(ssl.SSLError("wrong version")))
    assert (tls.result, tls.detail) == ("failed", "10.0.0.5:443: TLS handshake failed")
    refused = verification._connect_failure("10.0.0.5:80", chained(ConnectionRefusedError()))
    assert refused.result == "failed" and "refused" in refused.detail
    no_route = verification._connect_failure("10.0.0.5:80", chained(OSError(113, "No route")))
    assert no_route.result == "unreachable"

    class Slow:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(verification.httpx, "AsyncClient", Slow)
    slow = await verification._app_probe("10.0.0.5", AppProbe(kind="http", port=80))
    assert (slow.result, slow.answered) == ("failed", False)
    assert slow.detail == "http://10.0.0.5:80/: no answer within 5 s"


async def test_an_application_probe_without_an_address_fails():
    class NoAddress(Dest):
        async def get_server(self, server_id):
            return {"status": "ACTIVE", "ports": [{"status": "ACTIVE", "fixed_ips": []}]}

    result = await Verifier(NoAddress(), Settings(), poll_s=0.01).verify(
        app_ctx([AppProbe(kind="tcp", port=5432)])
    )
    assert result.passed is False and result.failed_checks() == ["app:tcp:5432"]
    assert [(p.target, p.result) for p in result.probes] == [("no fixed address", "failed")]
