"""The API's hardening after the pentest of 2026-11-19 (P4): what a caller without a token learns,
the headers every answer carries, and where a provider's CA bundle may lie."""

import pytest
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.domain.enums import Role
from seamless_migrate.store import Store
from tests.api_support import SOURCE, Api, api_settings

FULL = {"status", "version", "demo", "demo_speed", "auth_disabled", "db", "orchestrator", "leader"}


def make(tmp_path, **kw):
    settings, tokens = api_settings(tmp_path, **kw)
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    store.create_schema()
    return settings, tokens, store


@pytest.fixture
def api(tmp_path):
    settings, tokens, store = make(tmp_path, demo=False)
    with TestClient(create_app(settings, store)) as client:
        yield Api(client, store, settings, tokens)
    store.dispose()


def test_health_tells_a_caller_without_a_token_its_status_alone(api):
    """SDD §12 (pentest P4.1): /health and /ready without a token - or with one that is none -
    answer {"status"} alone: no version, no auth_disabled, no leader's host name, epoch or loop
    statistics. A viewer reads the whole report there and under /health/detail, which needs the
    role. A probe is no sign-in: nothing is audited or counted."""
    for path in ("/api/v1/health", "/api/v1/ready"):
        for headers in ({}, {"Authorization": "Bearer smg_not-a-token"}):
            answer = api.client.get(path, headers=headers)
            assert answer.status_code == 200 and answer.json() == {"status": "ok"}, path
        assert set(api.get(path).json()) == FULL
    assert api.client.get("/api/v1/health/detail").status_code == 401
    detail = api.get("/api/v1/health/detail")
    assert detail.status_code == 200 and set(detail.json()) == FULL
    assert detail.json()["leader"]["leading"] is True
    assert api.kinds().count("auth.denied") == 1  # /health/detail without a token, alone


def test_health_stays_whole_in_demo_mode_and_with_authentication_off(tmp_path):
    """SDD §12: demo mode documents its API to anyone and holds nothing real, and with
    authentication off every caller is the anonymous admin: the report stays whole there."""
    settings, _, store = make(tmp_path, demo=True)
    with TestClient(create_app(settings, store)) as client:
        assert set(client.get("/api/v1/health").json()) == FULL
    store.dispose()


def test_ready_answers_503_with_the_status_alone_while_degraded(api, monkeypatch):
    monkeypatch.setattr(api.store, "ping", lambda: False)
    answer = api.client.get("/api/v1/ready")
    assert answer.status_code == 503 and answer.json() == {"status": "degraded"}
    assert api.client.get("/api/v1/health").json() == {"status": "degraded"}
    assert api.get("/api/v1/ready").json()["db"] == "error"


def test_every_answer_carries_hsts(api):
    """Security.md R-05 (pentest P4.2): Strict-Transport-Security for a year, subdomains with
    it, beside the headers every answer already carries - an error's too."""
    for answer in (
        api.client.get("/api/v1/health"),
        api.client.get("/api/v1/me"),
        api.get("/api/v1/nope"),
    ):
        headers = answer.headers
        assert headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-content-type-options"] == "nosniff"
        assert "script-src 'self'" in headers["content-security-policy"]


@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/api/v1/providers"),
        ("POST", "/api/v1/hooks"),
        ("POST", "/api/v1/plans"),
        ("PATCH", "/api/v1/providers/src-osp"),
        ("PUT", "/api/v1/hooks/x1/secret"),
        ("POST", "/api/v1/users"),
    ],
)
def test_the_token_is_checked_before_the_body_is_read(api, method, path):
    """SDD §12, §13.1 (pentest P4.3): a request without a token is a 401 whatever its body - one
    that is no JSON too, which FastAPI would refuse first with a 422 that tells a stranger the
    route exists and reads a body; a role too low is a 403 before the body as well. With the
    role, the body is judged."""
    broken = {"content": b"x", "headers": {"Content-Type": "application/json"}}
    answer = api.client.request(method, path, **broken)
    assert answer.status_code == 401, answer.text
    low = {**broken, "headers": {**broken["headers"], **api.h(Role.viewer)}}
    assert api.client.request(method, path, **low).status_code == 403
    admin = {**broken, "headers": {**broken["headers"], **api.h(Role.admin)}}
    assert api.client.request(method, path, **admin).status_code == 422
    assert api.kinds().count("auth.denied") == 2  # once each: the check is not run twice


def test_head_is_answered_as_get_without_a_body(api):
    """RFC 9110 §9.3.2 (pentest P4.6): HEAD on a GET route is that route's answer without its
    body - its status, its role check and its headers -, not a 405 naming a body that never
    comes; a route that takes no GET stays a 405, and the event stream, which never ends, too."""
    head = api.client.head("/api/v1/me", headers=api.h(Role.viewer))
    get = api.get("/api/v1/me")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == get.headers["content-length"]
    assert api.client.head("/api/v1/me").status_code == 401
    assert api.client.head("/api/v1/health").status_code == 200
    assert api.client.head("/api/v1/events/stream", headers=api.h(Role.viewer)).status_code == 405


def test_a_ca_bundle_lies_in_a_ca_directory(tmp_path):
    """SDD §12 (pentest P4.4): ca_cert_path was any path, and a check answered differently for a
    file that exists and one that does not. It lies under one of SEAMLESS_CA_DIRS once symlinks
    and ".." are resolved; one refusal whatever is at the path, which names no file's state."""
    ca_dir = tmp_path / "ca"
    ca_dir.mkdir()
    (ca_dir / "cloud.pem").write_text("-")
    (tmp_path / "outside.pem").write_text("-")
    (ca_dir / "link.pem").symlink_to(tmp_path / "outside.pem")
    settings, tokens, store = make(tmp_path, demo=False, ca_dirs=[str(ca_dir)])
    with TestClient(create_app(settings, store)) as client:
        api = Api(client, store, settings, tokens)
        refusals = set()
        for path in (
            "/etc/passwd",
            "/etc/does-not-exist",
            str(tmp_path / "outside.pem"),
            str(ca_dir / ".." / "outside.pem"),
            str(ca_dir / "link.pem"),
            "relative/ca.pem",
            str(ca_dir),
        ):
            refused = api.post("/api/v1/providers", json={**SOURCE, "ca_cert_path": path})
            assert refused.status_code == 422, (path, refused.text)
            refusals.add(refused.json()["error"]["message"])
        assert len(refusals) == 1 and "SEAMLESS_CA_DIRS" in refusals.pop()
        for path in (str(ca_dir / "cloud.pem"), str(ca_dir / "not-yet-mounted.pem")):
            created = api.post(
                "/api/v1/providers", json={**SOURCE, "id": f"p{len(path)}", "ca_cert_path": path}
            )
            assert created.status_code == 201, created.text
        patched = api.client.patch(
            f"/api/v1/providers/p{len(path)}",
            headers=api.h(Role.admin),
            json={"ca_cert_path": "/etc/shadow"},
        )
        assert patched.status_code == 422
    store.dispose()
