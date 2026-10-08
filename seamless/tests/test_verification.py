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
