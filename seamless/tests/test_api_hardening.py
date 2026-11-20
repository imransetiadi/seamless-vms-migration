"""The API's hardening after the pentest of 2026-11-19 (P4): what a caller without a token learns,
the headers every answer carries, and where a provider's CA bundle may lie."""

import asyncio

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


def test_auth_and_user_events_are_an_admins_alone(api):
    """SDD §12, §13.1 (pentest 2026-11-19 P5): auth.denied names a refused request's path, method,
    needed role and caller's address, and user.created who was given which role: an admin reads
    them, no other role does - from GET /events, whose pages still hold `limit` events its reader
    may see, as from the stream."""
    assert api.client.get("/api/v1/me").status_code == 401  # auth.denied
    created = api.post("/api/v1/users", json={"name": "ext-auditor", "role": "viewer"})
    assert created.status_code == 201  # user.created
    hook = {"name": "h1", "kind": "playbook", "playbook": "/opt/hooks/h1.yml"}
    assert api.post("/api/v1/hooks", json=hook).status_code == 201  # hook.created
    seen = {
        role: [e["kind"] for e in api.get("/api/v1/events", role=role).json()]
        for role in (Role.viewer, Role.operator, Role.approver, Role.admin)
    }
    assert {"auth.denied", "user.created", "hook.created"} <= set(seen[Role.admin])
    for role in (Role.viewer, Role.operator, Role.approver):
        assert "hook.created" in seen[role]
        assert not [k for k in seen[role] if k.startswith(("auth.", "user."))], role
    # a page is filled with what the reader may see: the newest one is the hook's, not hidden
    newest = api.get("/api/v1/events?tail=true&limit=1", role=Role.viewer).json()
    assert [e["kind"] for e in newest] == ["hook.created"]


async def test_the_event_stream_sends_auth_and_user_events_to_an_admin_alone():
    from seamless_migrate.api.routes_events import sse_frames
    from seamless_migrate.events import ADMIN_ONLY_KINDS, EventBus, new_event

    assert {"auth.denied", "user.created"} <= ADMIN_ONLY_KINDS
    assert all(kind.startswith(("auth.", "user.")) for kind in ADMIN_ONLY_KINDS)

    async def frames(hidden):
        bus = EventBus(None)
        stream = sse_frames(bus.subscribe(), 0.05, hidden=hidden)
        first = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0.01)
        for seq, kind in enumerate(("auth.denied", "user.created", "plan.created"), start=1):
            event = new_event(kind, kind)
            event.seq = seq
            bus.publish(event, persist=True)
        got = [await first]
        while not got[-1].startswith(": heartbeat"):
            got.append(await stream.__anext__())
        await stream.aclose()
        return [line.split("event: ")[1].split("\n")[0] for line in got if "event: " in line]

    assert await frames(frozenset()) == ["auth.denied", "user.created", "plan.created"]
    assert await frames(ADMIN_ONLY_KINDS) == ["plan.created"]


def test_the_api_document_is_an_admins_outside_demo_mode(api):
    """SDD §12, Security.md R-04 (pentest P5): /api/openapi.json and /api/docs map every route and
    field of the installation; outside demo mode they are an admin's."""
    for path in ("/api/openapi.json", "/api/docs"):
        assert api.client.get(path).status_code == 401
        for role in (Role.viewer, Role.operator, Role.approver):
            assert api.get(path, role=role).status_code == 403, (path, role)
        assert api.get(path, role=Role.admin).status_code == 200


def test_the_provider_input_schema_offers_no_server_owned_field(api):
    """SDD §12 (pentest P5): ProviderCreate in the API's document names what a request sets - no
    status, check report, pin or project credential, which POST ignores and PATCH refuses."""
    from seamless_migrate.domain.models import PROVIDER_SERVER_OWNED

    spec = api.get("/api/openapi.json", role=Role.admin).json()
    offered = set(spec["components"]["schemas"]["ProviderCreate"]["properties"])
    assert offered & set(PROVIDER_SERVER_OWNED) == set()
    assert {
        "id",
        "name",
        "kind",
        "role",
        "endpoint",
        "conversion_host",
        "allow_plain_http",
    } <= offered
    stored = set(spec["components"]["schemas"]["Provider"]["properties"])
    assert set(PROVIDER_SERVER_OWNED) <= stored  # the answers still carry them
    # the behaviour is unchanged: POST ignores a server-owned field, never stores it
    body = {**SOURCE, "status": "ok", "capabilities": {"admin": True}}
    created = api.post("/api/v1/providers", json=body)
    assert created.status_code == 201, created.text
    assert (created.json()["status"], created.json()["capabilities"]) == ("unknown", {})


def test_an_admin_removes_a_dashboard_user_and_its_token_stops_at_once(api):
    """SDD §12, §13.1 (the retest of 2026-11-20 found no way to revoke one): DELETE /users/{name}
    is an admin's; the user's token is refused on its next request, user.deleted names who
    removed whom with which role - never a token or a digest - and an admin's alone reads it. A
    bootstrap token of the tokens file and the admin's own user are refused (409), an unknown
    name is a 404, and the name is matched as POST /users compares it, ignoring case."""
    created = api.post("/api/v1/users", json={"name": "Ext-Auditor", "role": "viewer"})
    assert created.status_code == 201
    token = created.json()["token"]
    as_user = {"Authorization": f"Bearer {token}"}
    assert api.client.get("/api/v1/me", headers=as_user).status_code == 200
    for role in (Role.viewer, Role.operator, Role.approver):
        refused = api.client.delete("/api/v1/users/Ext-Auditor", headers=api.h(role))
        assert refused.status_code == 403, role
    assert api.client.get("/api/v1/me", headers=as_user).status_code == 200
    removed = api.client.delete("/api/v1/users/ext-auditor", headers=api.h(Role.admin))
    assert removed.status_code == 204, removed.text
    assert api.client.get("/api/v1/me", headers=as_user).status_code == 401
    names = [u["name"] for u in api.get("/api/v1/users", role=Role.admin).json()]
    assert "Ext-Auditor" not in names
    gone = api.client.delete("/api/v1/users/ext-auditor", headers=api.h(Role.admin))
    assert gone.status_code == 404
    # a bootstrap token is the tokens file's to revoke
    bootstrap = api.client.delete("/api/v1/users/viewer-user", headers=api.h(Role.admin))
    assert bootstrap.status_code == 409 and "tokens file" in bootstrap.text
    events = api.get("/api/v1/events?limit=1000", role=Role.admin).json()
    [deleted] = [e for e in events if e["kind"] == "user.deleted"]
    assert deleted["data"] == {"name": "Ext-Auditor", "role": "viewer"}
    assert deleted["actor"] == "admin-user"
    assert token not in str(events)
    seen = [e["kind"] for e in api.get("/api/v1/events?limit=1000", role=Role.viewer).json()]
    assert "user.deleted" not in seen


def test_an_admin_cannot_remove_the_user_it_signs_in_as(api):
    """SDD §12: the last way in is never cut from the dashboard."""
    created = api.post("/api/v1/users", json={"name": "second-admin", "role": "admin"})
    own = {"Authorization": f"Bearer {created.json()['token']}"}
    refused = api.client.delete("/api/v1/users/Second-Admin", headers=own)
    assert refused.status_code == 409 and "signed in as" in refused.text
    assert api.client.get("/api/v1/me", headers=own).status_code == 200
