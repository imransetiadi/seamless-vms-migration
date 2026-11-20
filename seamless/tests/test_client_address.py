"""The caller's address behind a reverse proxy (SDD §13.1, Security.md §12; pentest 2026-11-19 P2):
the audit trail and the lockout count the address a trusted proxy forwards, never one any other
peer claims."""

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from seamless_migrate.api.app import create_app
from seamless_migrate.store import Store
from tests.api_support import api_settings

#: the ingress controller's pods: what FORWARDED_ALLOW_IPS names (k3s's default pod network)
PROXIES = "10.42.0.0/16"
PROXY = ("10.42.0.12", 41000)
STRANGER = ("198.51.100.9", 41000)
BAD = {"Authorization": "Bearer smg_not-a-token"}


@pytest.fixture
def served(tmp_path):
    """The app as uvicorn serves it with --proxy-headers and FORWARDED_ALLOW_IPS (cli.run_server):
    wrapped in the same middleware, reached once through the proxy and once directly."""
    settings, _ = api_settings(tmp_path, auth_lockout_per_minute=3)
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    store.create_schema()
    app = ProxyHeadersMiddleware(create_app(settings, store), trusted_hosts=PROXIES)
    with TestClient(app, client=PROXY) as through_proxy:
        yield through_proxy, TestClient(app, client=STRANGER), store
    store.dispose()


def denied_clients(store):
    events = store.events(since_seq=0, limit=100000)
    return [e.data["client"] for e in events if e.kind == "auth.denied"]


def test_a_trusted_proxys_forwarded_address_is_the_audited_client(served):
    """A request from a trusted proxy with X-Forwarded-For: 203.0.113.7 is audited as
    203.0.113.7 - the last address the proxy itself added, whatever the caller wrote before it;
    the same header from any other peer is ignored, and the peer's own address audited."""
    through_proxy, direct, store = served
    forwarded = {**BAD, "X-Forwarded-For": "203.0.113.7"}
    assert through_proxy.get("/api/v1/me", headers=forwarded).status_code == 401
    spoofed = {**BAD, "X-Forwarded-For": "192.0.2.66, 203.0.113.8"}
    assert through_proxy.get("/api/v1/me", headers=spoofed).status_code == 401
    assert direct.get("/api/v1/me", headers=forwarded).status_code == 401
    assert through_proxy.get("/api/v1/me", headers=BAD).status_code == 401
    assert denied_clients(store) == ["203.0.113.7", "203.0.113.8", "198.51.100.9", "10.42.0.12"]


def test_failed_authentications_lock_out_their_own_address_alone(served):
    """The lockout's key is the caller's address as the proxy forwards it: the failures of one
    address never answer another's with 429 - behind a proxy whose address every caller shared,
    one caller's failures would lock all of them out, or the limit never count anyone."""
    through_proxy, _, _ = served

    def get(address):
        headers = {**BAD, "X-Forwarded-For": address}
        return through_proxy.get("/api/v1/me", headers=headers).status_code

    assert [get("203.0.113.7") for _ in range(3)] == [401, 401, 401]
    assert get("203.0.113.7") == 429
    assert get("203.0.113.99") == 401
    assert get("203.0.113.7") == 429


def test_a_server_behind_no_trusted_proxy_says_so_at_start(tmp_path):
    """A non-loopback bind without FORWARDED_ALLOW_IPS is warned about at start, as "*" is; a
    loopback bind and a named proxy network are not."""
    from seamless_migrate.cli import untrusted_proxy_warning

    exposed, _ = api_settings(tmp_path, host="0.0.0.0")
    local, _ = api_settings(tmp_path, host="127.0.0.1")
    assert "FORWARDED_ALLOW_IPS is not set" in untrusted_proxy_warning(exposed, {})
    assert "'*'" in untrusted_proxy_warning(exposed, {"FORWARDED_ALLOW_IPS": "10.42.0.0/16, *"})
    assert untrusted_proxy_warning(exposed, {"FORWARDED_ALLOW_IPS": PROXIES}) is None
    assert untrusted_proxy_warning(local, {}) is None
