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


# -- GPU checks (SDD §7.5, §22.8, plan task G8) -------------------------------------------------
from seamless_migrate.domain.enums import ProviderKind, ProviderRole  # noqa: E402
from seamless_migrate.domain.models import GpuDevice, Mappings  # noqa: E402
from seamless_migrate.providers.base import ProviderError  # noqa: E402
from seamless_migrate.verification import (  # noqa: E402
    GPU_FLAVOR_UNREAD,
    GPU_HIDDEN,
    GPU_NOT_CHECKED,
    GPU_UNREAD,
)
from tests.factories import make_provider, make_vm  # noqa: E402

A100 = "CUSTOM_NVIDIA_A100_4C"
#: the GPU flavor as compute 2.47 embeds it in a server it created (SDD §22.3)
GPU_FLAVOR = {
    "name": "g1.a100-4c",
    "extra_specs": {"resources:VGPU": "1", f"trait:{A100}": "required"},
}
NO_FLAVOR = object()


class GpuDest(Dest):
    """A destination whose server shows ``flavor`` and holds ``allocations`` - or raises them -
    per resource class (SDD §22.3)."""

    def __init__(self, flavor=GPU_FLAVOR, allocations=None, **kw):
        super().__init__(**kw)
        self.flavor = flavor
        self.allocations = allocations
        self.allocation_reads = 0

    async def get_server(self, server_id):
        server = await super().get_server(server_id)
        if self.flavor is not NO_FLAVOR:
            server["flavor"] = self.flavor
        return server

    async def server_allocations(self, server_id):
        self.allocation_reads += 1
        if isinstance(self.allocations, Exception):
            raise self.allocations
        return self.allocations


def openstack_gpu_vm(**kw):
    """An OpenStack VM whose flavor g1.vgpu asks for one vGPU (SDD §22.2)."""
    data = {
        "flavor": "g1.vgpu",
        "flavor_extra_specs": {"resources:VGPU": "1"},
        "gpus": [GpuDevice(kind="vgpu", resource_class="VGPU")],
    }
    data.update(kw)
    return make_vm(**data)


def gpu_ctx(vm=None, *, kind=ProviderKind.openstack, mappings=None, profiles=None, timeout_s=0):
    """A verification of ``vm`` - by default an OpenStack GPU VM whose plan maps its flavor to the
    destination's GPU flavor g1.a100-4c (SDD §22.4) - with its source and destination."""
    plan = make_plan(
        verification=VerificationConfig(timeout_s=timeout_s),
        mappings=mappings or Mappings(flavors={"g1.vgpu": "g1.a100-4c"}),
    )
    source = make_provider(kind=kind)
    destination = make_provider(
        id="dst-rhoso",
        name="RHOSO 18.0",
        kind=ProviderKind.rhoso,
        role=ProviderRole.destination,
        gpu_profiles=profiles or {},
    )
    migration = make_migration(destination_server_id="srv-9", vm=vm or openstack_gpu_vm())
    return SimpleNamespace(plan=plan, migration=migration, source=source, destination=destination)


def by_name(result):
    return {c["name"]: c for c in result.checks}


async def test_gpu_flavor_check_passes_on_the_mapped_gpu_flavor():
    """SDD §7.5, §22.8 (plan task G8): the server's flavor is the GPU flavor the plan maps the VM
    to - an OpenStack VM's flavor mapping, a VMware VM's GPU mapping - and its extra specs still ask
    for the GPU, a CUSTOM_ class with the profile the destination declares for the flavor. A server
    that hides its extra specs passes by the name, and the allocations go unchecked, with a
    warning: nothing tells its requests."""
    dest = GpuDest(allocations={"VGPU": 1, "VCPU": 8, "MEMORY_MB": 32768})
    result = await Verifier(dest, Settings(), poll_s=0).verify(gpu_ctx())
    assert result.passed, result.checks
    assert [c["name"] for c in result.checks] == [
        "server_active",
        "ports_up",
        "gpu_flavor",
        "gpu_allocation",
        "console",
    ]
    check = by_name(result)["gpu_flavor"]
    assert (check["ok"], check.get("skipped")) == (True, None)
    assert check["detail"] == f"flavor g1.a100-4c, the GPU flavor: VGPU {A100} ×1"

    vmware = gpu_ctx(
        make_vm(flavor=None, gpus=[GpuDevice(kind="vgpu", profile="grid_a100-4c")]),
        kind=ProviderKind.vmware,
        mappings=Mappings(gpus={"grid_a100-4c": "g1.a100-4c"}),
    )
    result = await Verifier(dest, Settings(), poll_s=0).verify(vmware)
    assert result.passed and by_name(result)["gpu_flavor"]["ok"] is True, result.checks

    custom = GpuDest(
        flavor={"name": "g1.a100-4c", "extra_specs": {"resources:CUSTOM_NVIDIA_471": "1"}},
        allocations={"CUSTOM_NVIDIA_471": 1},
    )
    result = await Verifier(custom, Settings(), poll_s=0).verify(
        gpu_ctx(profiles={"g1.a100-4c": "A100-4C"})
    )
    assert result.passed, result.checks
    assert by_name(result)["gpu_flavor"]["detail"] == (
        "flavor g1.a100-4c, the GPU flavor: CUSTOM_NVIDIA_471 ×1"
    )

    hidden = GpuDest(flavor={"name": "g1.a100-4c", "extra_specs": {}}, allocations={"VGPU": 1})
    result = await Verifier(hidden, Settings(), poll_s=0).verify(gpu_ctx())
    assert result.passed, result.checks
    checks = by_name(result)
    assert checks["gpu_flavor"]["ok"] is True and "skipped" not in checks["gpu_flavor"]
    assert "hides its extra specs" in checks["gpu_flavor"]["detail"]
    assert checks["gpu_allocation"] == {
        "name": "gpu_allocation",
        "ok": True,
        "detail": GPU_HIDDEN,
        "skipped": True,
    }
    assert result.evidence["warnings"] == [GPU_HIDDEN] and hidden.allocation_reads == 0


async def test_gpu_flavor_check_fails_a_server_created_with_another_flavor():
    """SDD §7.5, §22.8 (plan task G8): a server created another way - with a kit's own flavor, or
    with a flavor of the GPU flavor's name whose extra specs ask for no GPU - fails gpu_flavor, and
    at once: no polling changes a server's flavor, while the source stays stopped until the
    rollback. Its allocations are not checked then. A server that reports no flavor is not judged
    (test_a_gpu_server_whose_flavor_cannot_be_read_is_not_judged, G8 review)."""
    cases = [
        (
            {"name": "m1.xlarge", "extra_specs": {"hw:cpu_policy": "dedicated"}},
            "the server's flavor is m1.xlarge, not the GPU flavor g1.a100-4c",
        ),
        (
            {"name": "m1.xlarge", "extra_specs": {}},
            "the server's flavor is m1.xlarge, not the GPU flavor g1.a100-4c",
        ),
        (
            {"name": "g1.a100-4c", "extra_specs": {"hw:cpu_policy": "dedicated"}},
            "the server's flavor g1.a100-4c asks for no GPU",
        ),
    ]
    for flavor, detail in cases:
        clock = {"t": 0.0}

        async def sleep(seconds, clock=clock):
            clock["t"] += max(seconds, 1.0)

        dest = GpuDest(flavor=flavor, allocations={"VGPU": 1})
        verifier = Verifier(
            dest, Settings(), poll_s=10, sleep=sleep, clock=lambda clock=clock: clock["t"]
        )
        result = await verifier.verify(gpu_ctx(timeout_s=600))
        assert result.passed is False, flavor
        assert result.failed_checks() == ["gpu_flavor"], result.checks
        checks = by_name(result)
        assert checks["gpu_flavor"]["detail"] == detail
        assert result.evidence["attempts"] == 1 and result.evidence["terminal"] is True
        assert checks["gpu_allocation"] == {
            "name": "gpu_allocation",
            "ok": True,
            "detail": GPU_NOT_CHECKED,
            "skipped": True,
        }
        assert dest.allocation_reads == 0 and "warnings" not in result.evidence


async def test_gpu_allocation_check_needs_the_units_of_each_vgpu_request():
    """SDD §7.5, §22.8 (plan task G8): the server's allocations, summed per resource class, hold
    the units of each vGPU request of its flavor - two numbered groups of one VGPU need two, a
    CUSTOM_ class of a declared profile its own; more than asked for passes, and another class's
    units count for nothing."""
    groups = {
        "name": "g1.a100-4c",
        "extra_specs": {
            "resources1:VGPU": "1",
            f"trait1:{A100}": "required",
            "resources2:VGPU": "1",
            f"trait2:{A100}": "required",
        },
    }
    cases = [
        ({"VGPU": 2, "VCPU": 16}, True, "VGPU: 2 held, 2 asked for"),
        ({"VGPU": 3}, True, "VGPU: 3 held, 2 asked for"),
        ({"VGPU": 1, "VCPU": 16}, False, "VGPU: 1 held, 2 asked for"),
        (
            {"VCPU": 16, "MEMORY_MB": 65536, "CUSTOM_NVIDIA_471": 2},
            False,
            "VGPU: 0 held, 2 asked for",
        ),
    ]
    for held, ok, detail in cases:
        dest = GpuDest(flavor=groups, allocations=held)
        result = await Verifier(dest, Settings(), poll_s=0).verify(gpu_ctx())
        check = by_name(result)["gpu_allocation"]
        assert (result.passed, check["ok"], check["detail"]) == (ok, ok, detail), held
        assert "skipped" not in check and dest.allocation_reads == 1
        assert result.failed_checks() == ([] if ok else ["gpu_allocation"])

    mixed = {
        "name": "g1.a100-4c",
        "extra_specs": {"resources:VGPU": "1", "resources1:CUSTOM_NVIDIA_471": "2"},
    }
    profiles = {"g1.a100-4c": "A100-4C"}
    dest = GpuDest(flavor=mixed, allocations={"VGPU": 4, "CUSTOM_NVIDIA_471": 1})
    result = await Verifier(dest, Settings(), poll_s=0).verify(gpu_ctx(profiles=profiles))
    assert result.failed_checks() == ["gpu_allocation"]
    assert by_name(result)["gpu_allocation"]["detail"] == (
        "VGPU: 4 held, 1 asked for; CUSTOM_NVIDIA_471: 1 held, 2 asked for"
    )


async def test_a_gpu_server_whose_flavor_cannot_be_read_is_not_judged():
    """SDD §7.5, §22.3 (G8 review): a server that reports no flavor name - below compute 2.47, one
    whose flavor Nova no longer has - is not judged: both GPU checks are skipped with a warning, no
    allocation is read, and the verification goes on as any VM's does; nothing is guessed."""
    for flavor in (NO_FLAVOR, {"name": None, "extra_specs": {}}):
        dest = GpuDest(flavor=flavor, allocations={"VGPU": 1})
        result = await Verifier(dest, Settings(), poll_s=0).verify(gpu_ctx())
        assert result.passed is True, (flavor, result.checks)
        checks = by_name(result)
        for name in ("gpu_flavor", "gpu_allocation"):
            assert checks[name] == {
                "name": name,
                "ok": True,
                "detail": GPU_FLAVOR_UNREAD,
                "skipped": True,
            }, flavor
        assert result.evidence["warnings"] == [GPU_FLAVOR_UNREAD]
        assert not result.evidence.get("terminal") and dest.allocation_reads == 0


class SlowBoot(GpuDest):
    """A GPU server whose guest prints its login prompt on the third poll."""

    async def console_log(self, server_id, lines=200):
        return "web-01 login: " if self.polls >= 3 else "booting"


class LateAllocations(GpuDest):
    """A GPU server still building, with no allocation yet, then ACTIVE with its vGPU."""

    async def get_server(self, server_id):
        self.status = "BUILD" if self.polls < 1 else "ACTIVE"
        return await super().get_server(server_id)

    async def server_allocations(self, server_id):
        self.allocation_reads += 1
        return None if self.status == "BUILD" else {"VGPU": 1}


def clocked(dest):
    """A verifier of ``dest`` whose sleeps advance a clock of its own: polls without waiting."""
    clock = {"t": 0.0}

    async def sleep(seconds):
        clock["t"] += max(seconds, 1.0)

    return Verifier(dest, Settings(), poll_s=10, sleep=sleep, clock=lambda: clock["t"])


async def test_only_a_wrong_gpu_flavor_ends_the_verification_at_once():
    """SDD §7.5 (G8 review): a GPU VM's guest is polled as any guest is - a passed gpu_flavor, its
    extra specs shown or hidden, never ends the verification, so a guest that boots slowly passes
    on a later poll, and a failed gpu_allocation leaves it polling to its deadline; a server that
    builds before its vGPU is allocated passes once it is, with no warning left of the build."""
    for flavor in (GPU_FLAVOR, {"name": "g1.a100-4c", "extra_specs": {}}):
        dest = SlowBoot(flavor=flavor, allocations={"VGPU": 1})
        result = await clocked(dest).verify(gpu_ctx(timeout_s=600))
        assert result.passed is True, (flavor, result.checks)
        assert result.evidence["attempts"] == 3 and not result.evidence.get("terminal")
    short = await clocked(GpuDest(allocations={"VGPU": 0})).verify(gpu_ctx(timeout_s=600))
    assert short.failed_checks() == ["gpu_allocation"], short.checks
    assert short.evidence["attempts"] > 1 and not short.evidence.get("terminal")
    late = await clocked(LateAllocations()).verify(gpu_ctx(timeout_s=600))
    assert late.passed is True and late.evidence["attempts"] == 2, late.checks
    assert "warnings" not in late.evidence and late.record().warnings == []


async def test_gpu_allocation_check_is_skipped_with_a_warning_when_placement_cannot_be_read():
    """SDD §7.5, §22.8 (plan task G8): allocations Placement does not give (None) - or a read that
    fails - skip gpu_allocation with a warning, never a guess; the verification passes on its
    other checks, and the record keeps the warning."""
    for unread in (None, {}, ProviderError("dst-rhoso: call timed out after 60 s")):
        dest = GpuDest(allocations=unread)
        result = await Verifier(dest, Settings(), poll_s=0).verify(gpu_ctx())
        assert result.passed is True, result.checks
        assert by_name(result)["gpu_allocation"] == {
            "name": "gpu_allocation",
            "ok": True,
            "detail": GPU_UNREAD,
            "skipped": True,
        }
        assert by_name(result)["gpu_flavor"]["ok"] is True and dest.allocation_reads == 1
        assert result.evidence["warnings"] == [GPU_UNREAD]
        assert result.record().warnings == [GPU_UNREAD]


async def test_gpu_allocation_check_is_skipped_for_a_pci_alias():
    """SDD §7.5, §22.8 (plan task G8): the allocations do not name a PCI alias request's resource
    class for Seamless - nova.conf sets it -, so a flavor whose GPU is an alias skips
    gpu_allocation with a warning and reads no allocation; a flavor that asks for a vGPU too has
    its vGPU checked, with the warning."""
    alias_vm = make_vm(
        flavor="p1.src",
        flavor_extra_specs={"pci_passthrough:alias": "a100:1"},
        gpus=[GpuDevice(kind="passthrough", alias="a100")],
    )
    mapped = Mappings(flavors={"p1.src": "p1.a100"})
    flavor = {"name": "p1.a100", "extra_specs": {"pci_passthrough:alias": "a100:1"}}
    dest = GpuDest(flavor=flavor, allocations={"VCPU": 16})
    result = await Verifier(dest, Settings(), poll_s=0).verify(gpu_ctx(alias_vm, mappings=mapped))
    assert result.passed is True, result.checks
    checks = by_name(result)
    assert checks["gpu_flavor"]["detail"] == "flavor p1.a100, the GPU flavor: PCI alias a100 ×1"
    warning = result.evidence["warnings"]
    assert len(warning) == 1 and warning[0].startswith("PCI alias a100 not checked")
    assert checks["gpu_allocation"]["skipped"] is True
    assert checks["gpu_allocation"]["detail"] == warning[0]
    assert dest.allocation_reads == 0

    both = {
        "name": "p1.a100",
        "extra_specs": {"resources:VGPU": "1", "pci_passthrough:alias": "a100:1"},
    }
    for held, ok in (({"VGPU": 1}, True), ({"VCPU": 16}, False)):
        dest = GpuDest(flavor=both, allocations=held)
        result = await Verifier(dest, Settings(), poll_s=0).verify(
            gpu_ctx(alias_vm, mappings=mapped)
        )
        check = by_name(result)["gpu_allocation"]
        assert (result.passed, check["ok"], "skipped" in check) == (ok, ok, False), held
        assert [w.split(" not checked")[0] for w in result.evidence["warnings"]] == [
            "PCI alias a100"
        ]


async def test_a_vm_without_a_gpu_flavor_gets_no_gpu_check():
    """SDD §7.5 (plan task G8): a VM without a GPU flavor gets neither GPU check and no allocation
    read - a VM without a GPU too, even one its flavor mapping sends to a GPU flavor."""
    plain = make_vm()
    mapped = Mappings(flavors={"m1.small": "g1.a100-4c"})
    for vm, mappings in ((plain, None), (plain, mapped)):
        dest = GpuDest(allocations={"VGPU": 1})
        result = await Verifier(dest, Settings(), poll_s=0).verify(gpu_ctx(vm, mappings=mappings))
        assert result.passed, result.checks
        assert [c["name"] for c in result.checks] == ["server_active", "ports_up", "console"]
        assert dest.allocation_reads == 0 and "warnings" not in result.evidence


async def test_gpu_check_details_name_no_provider(caplog):
    """SDD §7.5, §13.4, §22.12 (plan task G8): the GPU checks name classes and units, never a
    Placement provider - the destination's compute hosts, named after them and known by uuids -,
    whatever Placement answers the allocations read: units held on host-named providers, an error
    whose text names one, an answer that is no JSON, a read that raises with such a text. The
    advisor's review_verification sends the check results to Jev."""
    pytest.importorskip("openstack")
    import json
    import logging

    from tests.test_providers import FakePlacement, gpu_destination

    hosts = ("gpu-host-a", "gpu-host-b")
    gpus = ("gpu-host-a_pci_0000_3b_00_0", "gpu-host-b_pci_0000_af_00_0")
    uuids = {
        gpus[0]: "5b0c9a7e-1f2d-4e3c-9a8b-7c6d5e4f3a21",
        hosts[0]: "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b",
        gpus[1]: "2a3b4c5d-6e7f-4a8b-9c0d-1e2f3a4b5c6d",
        hosts[1]: "f0e1d2c3-b4a5-4968-8776-a5b4c3d2e1f0",
    }
    told = f"resource provider {gpus[0]} ({uuids[gpus[0]]}) of {hosts[0]}: generation conflict"

    def held(vgpus):
        return {
            "allocations": {
                uuids[gpus[0]]: {"generation": 3, "resources": {"VGPU": vgpus}},
                uuids[hosts[0]]: {"generation": 9, "resources": {"VCPU": 8, "MEMORY_MB": 32768}},
            },
            "project_id": "proj-1",
        }

    answers = [
        (200, held(1)),
        (200, held(0)),
        (409, {"errors": [{"status": 409, "code": "placement.concurrent_update", "detail": told}]}),
        (500, {"errors": [{"status": 500, "code": gpus[0], "title": told, "detail": told}]}),
        (503, told),
    ]
    caplog.set_level(logging.DEBUG, logger="seamless_migrate")
    results = []
    for status, body in answers:
        placement = FakePlacement(lambda url, params, status=status, body=body: (status, body))
        conn, dst = gpu_destination([], placement)
        [server] = conn.compute.servers(details=True)
        server.flavor = {"original_name": "g1.a100-4c", "extra_specs": GPU_FLAVOR["extra_specs"]}
        ctx = gpu_ctx()
        ctx.migration.destination_server_id = "srv-1"
        results.append(await Verifier(dst, Settings(), poll_s=0).verify(ctx))
    raising = GpuDest(allocations=ProviderError(f"dst-rhoso: {told}"))
    results.append(await Verifier(raising, Settings(), poll_s=0).verify(gpu_ctx()))

    details = [by_name(r)["gpu_allocation"]["detail"] for r in results]
    assert details == [
        "VGPU: 1 held, 1 asked for",
        "VGPU: 0 held, 1 asked for",
        GPU_UNREAD,
        GPU_UNREAD,
        GPU_UNREAD,
        GPU_UNREAD,
    ]
    kept = [
        json.dumps(
            [r.summary(), r.record().model_dump(mode="json"), r.evidence], ensure_ascii=False
        )
        for r in results
    ]
    for name in (*hosts, *gpus, *uuids.values()):
        assert not [k for k in kept if name in k], name
        assert name not in caplog.text, name
