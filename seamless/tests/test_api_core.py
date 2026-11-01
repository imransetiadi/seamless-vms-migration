import hashlib
import inspect
import re
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from seamless_migrate.ai.memory import MemoryClient
from seamless_migrate.api.app import create_app
from seamless_migrate.domain.enums import Phase, Role
from seamless_migrate.domain.models import Migration, Plan, Provider
from seamless_migrate.store import AsyncStore, Store
from tests.api_support import DESTINATION, ROLES, SOURCE, Api, api_settings
from tests.factories import make_migration


@pytest.fixture
def api(tmp_path):
    settings, tokens = api_settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    store.create_schema()
    app = create_app(settings, store)
    with TestClient(app) as client:
        yield Api(client, store, settings, tokens)
    store.dispose()


def test_health_public_reports_db(api, monkeypatch):
    res = api.client.get("/api/v1/health")
    assert res.status_code == 200
    body = res.json()
    orchestrator = body.pop("orchestrator")
    # a store without a lease (a test's): the server leads alone (SDD §12, plan task HA4)
    assert body.pop("leader") == {"leading": True, "holder": None, "epoch": None, "since": None}
    # demo_speed: the dashboard runs a demo's downtime clock in model time (SDD §12, ADV-16)
    assert body == {
        "status": "ok",
        "version": "0.5.0-rc.1",
        "demo": True,
        "demo_speed": 1e7,
        "auth_disabled": False,
        "db": "ok",
    }
    assert orchestrator["running"] is True and orchestrator["healthy"] is True
    assert orchestrator["ticks"] >= 0
    assert orchestrator["last_tick_age_s"] is None or orchestrator["last_tick_age_s"] >= 0
    monkeypatch.setattr(api.store, "ping", lambda: False)
    assert api.client.get("/api/v1/health").json()["db"] == "error"
    assert api.client.get("/api/v1/health").json()["status"] == "degraded"
    monkeypatch.setattr(api.store, "ping", lambda: True)
    # a dead or stale tick loop degrades the service even with a healthy database
    monkeypatch.setattr(
        api.client.app.state.services.orchestrator,
        "health",
        lambda: {"running": False, "last_tick_age_s": 99.0, "ticks": 5, "healthy": False},
    )
    degraded = api.client.get("/api/v1/health").json()
    assert degraded["status"] == "degraded" and degraded["orchestrator"]["running"] is False
    # readiness answers 503 with the same body while degraded, 200 otherwise
    ready = api.client.get("/api/v1/ready")
    assert ready.status_code == 503 and ready.json()["status"] == "degraded"
    monkeypatch.undo()
    assert api.client.get("/api/v1/ready").status_code == 200
    assert api.client.get("/api/v1/ready").json()["status"] == "ok"


def test_health_names_the_leader_and_whether_this_replica_leads(api, tmp_path, monkeypatch):
    """SDD §12, §25.6: /health's leader says whether the replica that answers leads, and names the
    lease's holder, its epoch and when it was taken while the lease is valid - the same on every
    replica -, each null while no replica leads. A server whose store holds no lease (a test's)
    leads alone."""
    from datetime import datetime

    from seamless_migrate import lease as lease_module
    from seamless_migrate.api import app as app_module
    from seamless_migrate.lease import Lease

    nobody = {"leading": False, "holder": None, "epoch": None, "since": None}
    assert api.client.get("/api/v1/health").json()["leader"] == {**nobody, "leading": True}

    monkeypatch.setattr(lease_module, "RETRY_S", 0.1)
    monkeypatch.setattr(app_module, "replica_address", lambda port, host=None: f"192.0.2.10:{port}")
    url = f"sqlite:///{tmp_path / 'replicas.db'}"
    observer = Store(url)

    def replica(name: str, port: int) -> TestClient:
        settings, _ = api_settings(tmp_path / name, db_url=url, port=port)
        return TestClient(create_app(settings.model_copy(update={"leader_lease_s": 30.0})))

    try:
        with replica("seamless-a", 8081) as a:
            holder = a.app.state.services.leadership.holder
            row = Lease(observer.engine, "observer", "192.0.2.99:8080", 30.0).read()
            assert row is not None and row.holder == holder
            leads = a.get("/api/v1/health").json()["leader"]
            assert {**leads, "since": None} == {
                "leading": True,
                "holder": holder,
                "epoch": 1,
                "since": None,
            }
            assert datetime.fromisoformat(leads["since"].replace("Z", "+00:00")) == row.acquired_at
            with replica("seamless-b", 8082) as b:
                assert b.get("/api/v1/health").json()["leader"] == {**leads, "leading": False}
                # the row cannot be read: the leader names itself, a follower knows of no leader
                for client in (a, b):
                    services = client.app.state.services
                    monkeypatch.setattr(services.leadership, "row", lambda: 1 / 0)
                assert a.get("/api/v1/health").json()["leader"] == {**leads, "since": None}
                unread = b.get("/api/v1/health").json()
                assert unread["leader"] == nobody and unread["orchestrator"]["running"] is False
        # the leader stopped and released its lease, and no replica here campaigns: nobody leads
        monkeypatch.setattr(Lease, "campaign", lambda self, record=None: None)
        with replica("seamless-c", 8083) as c:
            assert c.get("/api/v1/health").json()["leader"] == nobody
    finally:
        observer.dispose()


def test_unhandled_errors_keep_the_envelope_and_headers(tmp_path):
    settings, _ = api_settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'err.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store), raise_server_exceptions=False) as client:

        def boom() -> dict:
            raise RuntimeError("db exploded password=hunter2")

        client.app.state.services.orchestrator.health = boom
        res = client.get("/api/v1/health")
        assert res.status_code == 500
        assert res.json() == {"error": {"code": "internal_error", "message": "internal error"}}
        assert res.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
    store.dispose()


def test_out_of_range_query_integers_are_422(api):
    for path in (
        "/api/v1/events?since=99999999999999999999",
        "/api/v1/migrations?offset=99999999999999999999",
    ):
        res = api.get(path)
        assert res.status_code == 422, path
        assert res.json()["error"]["code"] == "validation_error"
    # events.seq is PostgreSQL's integer: one past its width is a 422, never a 500 there (SDD §12,
    # review ST-01)
    assert api.get("/api/v1/events?since=2147483647").status_code == 200
    for path in (
        "/api/v1/events?since=2147483648",
        "/api/v1/events?tail=true&before=2147483648",
        "/api/v1/events/stream?since=2147483648",
    ):
        assert api.get(path).status_code == 422, path


def test_provider_errors_are_redacted_in_the_api(api, monkeypatch):
    from seamless_migrate.providers.base import ProviderError

    class Broken:
        async def list_vms(self):
            raise ProviderError("src-osp: auth failed OS_PASSWORD=hunter2 at https://k:5000")

    assert api.post("/api/v1/providers", json=SOURCE).status_code == 201
    monkeypatch.setattr(api.client.app.state.services.providers, "get", lambda p: Broken())
    res = api.get("/api/v1/providers/src-osp/inventory")
    assert res.status_code == 502 and res.json()["error"]["code"] == "provider_error"
    assert "hunter2" not in res.text and "auth failed" in res.text


def test_large_responses_are_gzip_compressed_when_the_client_accepts_gzip(api, monkeypatch):
    """SDD §12: responses of 1 KiB and more are gzip-compressed when Accept-Encoding allows it."""
    # the stats' throughput buckets start at the orchestrator's minute: one clock for both requests,
    # or two requests either side of a minute boundary answer different series (a CI flake)
    pinned = datetime(2026, 10, 30, 12, 0, 59, 900000, tzinfo=UTC)
    monkeypatch.setattr(api.client.app.state.services.orchestrator, "_now", lambda: pinned)
    path = "/api/v1/stats"  # 60 throughput buckets: well over 1 KiB
    accepted = api.client.get(path, headers={**api.h(Role.viewer), "Accept-Encoding": "gzip"})
    assert accepted.status_code == 200
    assert len(accepted.content) >= 1024, "the stats should be a large response"
    assert accepted.headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in accepted.headers.get("vary", "").lower()
    # the security headers (Security.md R-05) wrap the compressor
    assert accepted.headers.get("x-content-type-options") == "nosniff"
    assert "content-security-policy" in accepted.headers
    plain = api.client.get(path, headers={**api.h(Role.viewer), "Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.json() == accepted.json()
    # small responses are sent as they are
    health = api.client.get("/api/v1/health", headers={"Accept-Encoding": "gzip"})
    assert len(health.content) < 1024 and "content-encoding" not in health.headers


def test_unauthenticated_401_and_audit_event(api):
    res = api.client.get("/api/v1/plans")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthorized"
    bad = api.client.get("/api/v1/plans", headers={"Authorization": "Bearer smg_not-a-real-token"})
    assert bad.status_code == 401
    denied = [e for e in api.store.events(since_seq=0) if e.kind == "auth.denied"]
    assert len(denied) == 2
    assert denied[0].data["path"] == "/api/v1/plans"
    assert "smg_not-a-real-token" not in str([e.model_dump() for e in denied])
    me = api.get("/api/v1/me", Role.approver)
    assert me.json() == {"name": "approver-user", "role": "approver", "host_key_checking": True}


def test_me_says_whether_host_keys_are_checked_and_health_does_not(tmp_path):
    """SDD §12, §15.1, §16 (plan task ST7): GET /me tells a signed-in viewer whether the data path
    checks the conversion hosts' keys - the dashboard's provider page warns while it does not -;
    the public /health never says it."""
    settings, tokens = api_settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'off.db'}")
    store.create_schema()
    off = settings.model_copy(update={"ansible_host_key_checking": False})
    with TestClient(create_app(off, store)) as client:
        api = Api(client, store, off, tokens)
        assert api.get("/api/v1/me", Role.viewer).json()["host_key_checking"] is False
        assert "host_key_checking" not in client.get("/api/v1/health").json()
    store.dispose()


def test_me_and_forbidden_audit(api):
    res = api.post("/api/v1/providers", Role.operator, json=SOURCE)
    assert res.status_code == 403 and res.json()["error"]["code"] == "forbidden"
    denied = [e for e in api.store.events(since_seq=0) if e.kind == "auth.denied"]
    assert denied[-1].actor == "operator-user" and denied[-1].data["required_role"] == "admin"


def test_a_denied_request_records_its_path_as_sent(api):
    """SDD §13.1 (review ST-03): auth.denied records the path as the client sent it,
    percent-encoded - the decoded one put a NUL into the event, which PostgreSQL refuses, so the
    401 became a 500 and the probe left no audit record."""
    res = api.client.get("/api/v1/providers/x%00y")
    assert res.status_code == 401
    [denied] = [e for e in api.store.events(since_seq=0) if e.kind == "auth.denied"]
    assert denied.data["path"] == "/api/v1/providers/x%00y"
    assert denied.message == "GET /api/v1/providers/x%00y: missing or invalid bearer token"


def test_a_128_character_admins_policy_change_is_audited(api):
    """SDD §11, §14.5 (review ST-02): names are 1-128 characters and the advisor's audit events
    name user:<name> - 133 characters for a 128-character admin against events.actor's 128. The
    change and its audit event are both written, the actor cut to the column."""
    name = "a" * 128
    created = api.post("/api/v1/users", Role.admin, json={"name": name, "role": "admin"})
    assert created.status_code == 201, created.text
    headers = {"Authorization": f"Bearer {created.json()['token']}"}
    spec = {
        "name": "long-named admin",
        "scope": {"provider_ids": [], "plan_ids": []},
        "decision_types": ["migration_strategy"],
        "mode": "always_require_approval",
        "threshold": 0.6,
    }
    res = api.client.post("/api/v1/advisor/policies", headers=headers, json=spec)
    assert res.status_code == 201, res.text
    [audited] = [e for e in api.store.events(since_seq=0) if e.kind == "policy.created"]
    actor = f"user:{name}"  # its start, then 12 hex of its SHA-256 (review RV48-10)
    assert audited.actor == actor[:115] + "~" + hashlib.sha256(actor.encode()).hexdigest()[:12]


@pytest.mark.parametrize(
    ("fragment", "message"),
    [
        ('"name": "fin\\u0000ance"', "name: holds a NUL character"),
        ('"name": "fin\\ud800ance"', "name: holds a lone surrogate"),
        (
            '"name": "Mapped", "mappings": {"networks": {"app\\u0000-net": "prod-net"}}',
            "mappings.networks: a key holds a NUL character",
        ),
    ],
    ids=["nul", "lone-surrogate", "nul-key"],
)
def test_a_request_body_holding_a_nul_or_a_lone_surrogate_is_a_422_before_the_route_runs(
    api, fragment, message
):
    """SDD §11, §12 (reviews RV48-07, RV48-08): a JSON string with a NUL or a lone surrogate
    passed the request model - the plan was written, then its plan.created message could not be
    encoded (a 500, a plan without its audit event), PostgreSQL refused the document, and the
    store's strip merged a mapping's two keys. Such a body is refused 422 naming where, once the
    token and the role are checked, before the route writes anything."""
    for provider in (SOURCE, DESTINATION):
        assert api.post("/api/v1/providers", json=provider).status_code == 201
    body = (
        "{" + fragment + ', "source_provider_id": "src-osp",'
        ' "destination_provider_id": "dst-rhoso", "vm_ids": []}'
    )

    def post(role):
        headers = {**api.h(role), "content-type": "application/json"}
        return api.client.post("/api/v1/plans", headers=headers, content=body)

    assert post(None).status_code == 401  # the token first
    assert post(Role.viewer).status_code == 403  # then the role
    res = post(Role.operator)
    assert (res.status_code, res.json()["error"]) == (
        422,
        {"code": "validation_error", "message": message},
    )
    assert api.store.list("plan", Plan) == [] and "plan.created" not in api.kinds()


@pytest.mark.parametrize(
    ("content_type", "depth"),
    [("application/json", 1500), ("text/plain", 1500), ("text/plain", 10000)],
)
def test_a_body_nested_however_deep_is_answered_as_its_route_answers_it(api, content_type, depth):
    """SDD §12 (review RV49-05): the request-body check walked the parsed JSON in Python frames,
    one per level - a body nested 1,500 deep (3 KB) was a RecursionError, a 500, for any caller
    with the route's role, before the route's own validation ran. It walks without recursion,
    and a body its parser cannot read is left to the route: a 4xx with the envelope."""
    body = '{"name": "x", "description": ' + "[" * depth + "]" * depth + "}"
    headers = {**api.h(Role.operator), "content-type": content_type}
    res = api.client.post("/api/v1/plans", content=body.encode(), headers=headers)
    assert res.status_code in (400, 422) and "error" in res.json(), res.text


def test_a_value_the_store_refuses_is_a_422_naming_its_field(api, monkeypatch):
    """SDD §11, §12: a source cloud's name holding a NUL - Neutron's names are free strings - is
    refused by the store, never kept stripped as a name the source does not have; the route
    answers 422 validation_error with the store's message, naming the field, never a 500."""
    from seamless_migrate.domain.models import Nic
    from seamless_migrate.providers.fake import FakeSourceProvider

    for provider in (SOURCE, DESTINATION):
        assert api.post("/api/v1/providers", json=provider).status_code == 201
    [first, *_] = [vm["source_id"] for vm in api.get("/api/v1/providers/src-osp/inventory").json()]
    original = FakeSourceProvider.get_vm

    async def get_vm(self, source_id):
        vm = await original(self, source_id)
        return vm.model_copy(update={"nics": [Nic(network="app\x00-net")]})

    monkeypatch.setattr(FakeSourceProvider, "get_vm", get_vm)
    plan = {"name": "NUL network", "source_provider_id": "src-osp"}
    plan |= {"destination_provider_id": "dst-rhoso", "vm_ids": [first]}
    made = api.post("/api/v1/plans", Role.operator, json=plan)
    assert made.status_code == 201, made.text
    res = api.post(f"/api/v1/plans/{made.json()['id']}/validate", Role.operator)
    assert res.status_code == 422 and res.json()["error"]["code"] == "validation_error", res.text
    assert re.fullmatch(
        r"[a-z_]+ [\w-]+: [\w.]+: holds a NUL character, which the store cannot keep",
        res.json()["error"]["message"],
    ), res.text
    assert api.store.list("migration", Migration) == []


ROUTES = [
    ("GET", "/api/v1/me", Role.viewer, None),
    ("GET", "/api/v1/users", Role.admin, None),
    ("POST", "/api/v1/users", Role.admin, {"name": "matrix-user"}),
    ("GET", "/api/v1/providers", Role.viewer, None),
    ("POST", "/api/v1/providers", Role.admin, {**SOURCE, "id": "matrix-src"}),
    ("GET", "/api/v1/providers/src-osp", Role.viewer, None),
    ("PATCH", "/api/v1/providers/src-osp", Role.admin, {"name": "renamed"}),
    (
        "PUT",
        "/api/v1/providers/src-osp/credentials",
        Role.admin,
        {"username": "u", "password": "p", "project_name": "x"},
    ),
    (
        "PUT",
        "/api/v1/providers/src-osp/ceph-key",
        Role.admin,
        {"key": "AQFakeCephKeyForTestsOnlyNotRealAtAll0=="},  # gitleaks:allow (no key material)
    ),
    (
        "PUT",
        "/api/v1/providers/src-osp/conversion-key",
        Role.admin,
        {
            "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n"  # gitleaks:allow (fake)
            + "A" * 64
            + "\n-----END OPENSSH PRIVATE KEY-----"
        },
    ),
    # a provider's credential per Keystone project (SDD §12, §27.4, plan task TC2), project ids of
    # 32 hex digits made up here: a body without the secret, which writes nothing for an admin
    # either (422) - a failing matrix prints its body, and no secret may reach a failure message
    (
        "PUT",
        "/api/v1/providers/src-osp/projects/5e1f0c2b9a7d4e3f8b6a1c0d2e4f6a8b/credentials",
        Role.admin,
        {"application_credential_id": "matrix-project-credential"},
    ),
    (
        "DELETE",
        "/api/v1/providers/src-osp/projects/0f1e2d3c4b5a69788796a5b4c3d2e1f0/credentials",
        Role.admin,
        None,
    ),
    ("DELETE", "/api/v1/providers/nope-1", Role.admin, None),
    ("POST", "/api/v1/providers/src-osp/check", Role.operator, None),
    ("GET", "/api/v1/providers/src-osp/inventory", Role.viewer, None),
    # the hook catalog (SDD §12, plan task N3)
    ("GET", "/api/v1/hooks", Role.viewer, None),
    (
        "POST",
        "/api/v1/hooks",
        Role.admin,
        {"name": "drain-lb", "kind": "playbook", "playbook": "/opt/hooks/drain-lb.yml"},
    ),
    ("GET", "/api/v1/hooks/nope-1", Role.viewer, None),
    ("PATCH", "/api/v1/hooks/nope-1", Role.admin, {"timeout_s": 60}),
    ("DELETE", "/api/v1/hooks/nope-1", Role.admin, None),
    ("GET", "/api/v1/notifications/channels", Role.viewer, None),
    (
        "POST",
        "/api/v1/notifications/channels",
        Role.admin,
        {"name": "matrix-slack", "kind": "slack"},
    ),
    ("GET", "/api/v1/notifications/channels/nope-1", Role.viewer, None),
    ("PATCH", "/api/v1/notifications/channels/nope-1", Role.admin, {"enabled": False}),
    ("DELETE", "/api/v1/notifications/channels/nope-1", Role.admin, None),
    (
        "PUT",
        "/api/v1/notifications/channels/nope-1/secret",
        Role.admin,
        {"url": "https://hooks.slack.com/services/T000/B000/matrix"},
    ),
    ("POST", "/api/v1/notifications/channels/nope-1/test", Role.admin, None),
    ("GET", "/api/v1/notifications/subscriptions", Role.viewer, None),
    (
        "POST",
        "/api/v1/notifications/subscriptions",
        Role.operator,
        {"channel": "matrix-slack", "plan_id": "plan-00000000"},
    ),
    ("DELETE", "/api/v1/notifications/subscriptions/sub-nope0001", Role.operator, None),
    ("GET", "/api/v1/plans", Role.viewer, None),
    (
        "POST",
        "/api/v1/plans",
        Role.operator,
        {
            "name": "m",
            "source_provider_id": "src-osp",
            "destination_provider_id": "dst-rhoso",
            "vm_ids": [],
        },
    ),
    ("GET", "/api/v1/plans/plan-00000000", Role.viewer, None),
    ("PATCH", "/api/v1/plans/plan-00000000", Role.operator, {"name": "x"}),
    ("POST", "/api/v1/plans/plan-00000000/waves/auto", Role.operator, {"max_wave_size": 5}),
    ("POST", "/api/v1/plans/plan-00000000/validate", Role.operator, None),
    ("POST", "/api/v1/plans/plan-00000000/start", Role.operator, None),
    ("POST", "/api/v1/plans/plan-00000000/pause", Role.operator, None),
    ("POST", "/api/v1/plans/plan-00000000/cancel", Role.operator, {}),
    ("GET", "/api/v1/migrations", Role.viewer, None),
    ("GET", "/api/v1/migrations/mig-0000000000", Role.viewer, None),
    ("POST", "/api/v1/migrations/mig-0000000000/approve", Role.approver, {"comment": "ok"}),
    ("POST", "/api/v1/migrations/mig-0000000000/cutover", Role.approver, {}),
    ("POST", "/api/v1/migrations/mig-0000000000/sync", Role.operator, None),
    ("POST", "/api/v1/migrations/mig-0000000000/rollback", Role.operator, {"reason": "r"}),
    ("POST", "/api/v1/migrations/mig-0000000000/retry", Role.operator, None),
    ("POST", "/api/v1/migrations/mig-0000000000/cancel", Role.operator, {}),
    ("POST", "/api/v1/migrations/mig-0000000000/finalize", Role.approver, {"confirm": "x"}),
    ("PUT", "/api/v1/migrations/mig-0000000000/strategy", Role.operator, {"strategy": "cold"}),
    ("GET", "/api/v1/events", Role.viewer, None),
    ("GET", "/api/v1/stats", Role.viewer, None),
    ("GET", "/api/v1/advisor/status", Role.viewer, None),
    ("GET", "/api/v1/advisor/notes", Role.viewer, None),
    ("POST", "/api/v1/advisor/similar-incidents", Role.operator, {"query": "timeout"}),
    ("GET", "/api/v1/advisor/data-sent", Role.viewer, None),
    ("GET", "/api/v1/advisor/recommendations", Role.viewer, None),
    ("GET", "/api/v1/advisor/recommendations/rec-nope", Role.viewer, None),
    ("POST", "/api/v1/advisor/recommendations/rec-nope/approve", Role.admin, {"comment": "ok"}),
    (
        "POST",
        "/api/v1/advisor/recommendations/rec-nope/override",
        Role.admin,
        {"strategy": "cold", "reason": "r"},
    ),
    ("GET", "/api/v1/advisor/snapshots/snap-nope", Role.viewer, None),
    # a body that creates or changes nothing, even for a role the route allows
    ("GET", "/api/v1/advisor/policies", Role.viewer, None),
    ("POST", "/api/v1/advisor/policies", Role.admin, {}),
    ("PUT", "/api/v1/advisor/policies/pol-nope", Role.admin, {}),
    ("DELETE", "/api/v1/advisor/policies/pol-nope", Role.admin, None),
    ("POST", "/api/v1/migrations/mig-0000000000/force-cutover", Role.approver, {"reason": "r"}),
    ("POST", "/api/v1/plans/plan-00000000/finalize", Role.approver, {"confirm": "x"}),
    ("GET", "/api/v1/metrics", Role.viewer, None),
]


@pytest.fixture(scope="module")
def matrix_api(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("matrix")
    # the matrix fires >100 unauthenticated requests from one client: no lockout here
    settings, tokens = api_settings(
        tmp_path, auth_lockout_per_minute=0, secrets_dir=tmp_path / "secrets"
    )
    store = Store(f"sqlite:///{tmp_path / 'matrix.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        api = Api(client, store, settings, tokens)
        api.client.post("/api/v1/providers", headers=api.h(Role.admin), json=SOURCE)
        api.client.post("/api/v1/providers", headers=api.h(Role.admin), json=DESTINATION)
        yield api
    store.dispose()


@pytest.mark.parametrize(
    "method,path,min_role,body", ROUTES, ids=[f"{m} {p}" for m, p, _, _ in ROUTES]
)
@pytest.mark.parametrize("role", ROLES)
def test_role_matrix(matrix_api, method, path, min_role, body, role):
    api = matrix_api
    res = api.client.request(method, path, headers=api.h(role), json=body)
    if role.at_least(min_role):
        assert res.status_code not in (401, 403), (res.status_code, res.text)
    else:
        assert res.status_code == 403, (res.status_code, res.text)
    assert api.client.request(method, path, json=body).status_code == 401


def test_sse_route_requires_viewer(api):
    assert api.client.get("/api/v1/events/stream").status_code == 401


def test_error_envelope_shape(api):
    def envelope(res, status, code):
        assert res.status_code == status, res.text
        body = res.json()
        assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}
        assert body["error"]["code"] == code and body["error"]["message"]

    envelope(api.get("/api/v1/plans/plan-missing0"), 404, "not_found")
    envelope(
        api.post("/api/v1/plans", Role.operator, json={"name": "no providers"}),
        422,
        "validation_error",
    )
    envelope(
        api.post(
            "/api/v1/plans",
            Role.operator,
            json={
                "name": "x",
                "source_provider_id": "nope",
                "destination_provider_id": "nope2",
                "vm_ids": [],
            },
        ),
        400,
        "bad_request",
    )
    api.post("/api/v1/providers", json=SOURCE)
    envelope(api.post("/api/v1/providers", json=SOURCE), 409, "conflict")
    envelope(api.client.get("/api/v1/no-such-route"), 404, "not_found")
    envelope(api.client.delete("/api/v1/health"), 405, "method_not_allowed")


def test_metrics_exposes_every_family_sdd_18_names(api):
    """SDD §18 (QASuite FR-23, review RV37B-14): /metrics declares every metric family the SDD
    names - a summary's _sum/_count as its family, a label set as nothing more - so a renamed
    metric fails here, not only on a dashboard that stopped drawing."""
    from seamless_migrate.config import find_repo_root

    sdd = (find_repo_root() / "docs" / "SDD.md").read_text(encoding="utf-8")
    section = sdd[sdd.index("## 18. Observability") : sdd.index("\n## 19.")]
    families = set()
    for name in re.findall(r"`(seamless_[a-z_]+)(?:/_count)?(?:\{[^}`]*\})?`", section):
        families.add(name.removesuffix("_sum") if name.endswith("_seconds_sum") else name)
    assert "seamless_tick_seconds_max" in families and "seamless_tick_seconds" in families
    text = api.get("/api/v1/metrics").text
    declared = set(re.findall(r"^# TYPE (\S+) ", text, re.MULTILINE))
    assert families - declared == set()


def test_metrics_format(api, tmp_path):
    api.post("/api/v1/providers", json=SOURCE)
    res = api.get("/api/v1/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    text = res.text
    for line in (
        "# TYPE seamless_migrations gauge",
        'seamless_migrations{phase="completed"} 0',
        "# TYPE seamless_bytes_transferred_total counter",
        "seamless_bytes_transferred_total 0",
        "seamless_downtime_seconds_sum 0",
        "seamless_downtime_seconds_count 0",
        "seamless_downtime_seconds_max 0",
        "# TYPE seamless_tick_seconds summary",
        "seamless_tick_seconds_sum",
        "seamless_tick_seconds_count",
        "# TYPE seamless_tick_seconds_max gauge",
        "seamless_tick_seconds_max",
        "# TYPE seamless_tick_slow_total counter",
        "# TYPE seamless_step_duration_seconds summary",
        "# TYPE seamless_advisor_calls_total counter",
        "# TYPE seamless_cutover_refusals gauge",
        "seamless_cutover_refusals 0",
    ):
        assert line in text, line
    assert all(f'seamless_migrations{{phase="{p.value}"}}' in text for p in Phase)
    assert api.client.get("/api/v1/metrics").status_code == 401

    settings, _ = api_settings(tmp_path / "public", metrics_public=True)
    public_store = Store(f"sqlite:///{tmp_path / 'public.db'}")
    public_store.create_schema()
    with TestClient(create_app(settings, public_store)) as client:
        assert client.get("/api/v1/metrics").status_code == 200
    public_store.dispose()


def test_metrics_say_whether_this_replica_leads_and_the_epoch_it_read(
    api, any_store, tmp_path, monkeypatch
):
    """SDD §18, §25.8 (plan task HA5): two servers on one database - SQLite, and PostgreSQL with
    SEAMLESS_TEST_PG_URL -, each through the API's lifespan. The one that took the lease says
    seamless_leader 1 and the follower 0, and both say the epoch in the lease's row as they read
    it at the scrape - the follower holds none of its own. Once the leader is cut off from its
    lease - its renewals fail, its renew deadline deposes it, its lease lapses -, the follower
    takes it over and says 1 and the next epoch, and the deposed replica, still serving, says 0
    and reads that epoch. Before any lease - the API on a store a test hands it, which takes
    none - both gauges read 0. Neither carries a label: no replica's holder or address, each a
    host name, leaves through /metrics."""
    import contextlib
    import time

    import sqlalchemy as sa

    from seamless_migrate import lease as lease_module

    texts: list[str] = []

    def gauges(client: TestClient, headers: dict[str, str]) -> tuple[int, int]:
        text = client.get("/api/v1/metrics", headers=headers).text
        texts.append(text)
        assert "# TYPE seamless_leader gauge\n" in text
        assert "# TYPE seamless_leader_epoch gauge\n" in text
        samples = re.findall(r"^(seamless_leader\S*) (\S+)$", text, re.MULTILINE)
        assert [name for name, _ in samples] == ["seamless_leader", "seamless_leader_epoch"]
        return int(samples[0][1]), int(samples[1][1])

    def unreachable(*args: object, **kwargs: object) -> None:
        raise sa.exc.OperationalError("leases", {}, ConnectionError("the lease is out of reach"))

    assert gauges(api.client, api.h(Role.viewer)) == (0, 0)

    monkeypatch.setattr(lease_module, "RETRY_S", 0.1)  # a try or a renewal every tenth of a second
    apps, headers = [], []
    for name in ("a", "b"):
        settings, tokens = api_settings(tmp_path / name, db_url=any_store.url)
        # a lease of two seconds, below the variable's bound, which a test may set past (§15.1)
        apps.append(create_app(settings.model_copy(update={"leader_lease_s": 2.0})))
        headers.append({"Authorization": f"Bearer {tokens[Role.viewer]}"})
    try:
        with contextlib.ExitStack() as servers:
            a = servers.enter_context(TestClient(apps[0]))
            b = servers.enter_context(TestClient(apps[1]))
            assert gauges(a, headers[0]) == (1, 1), "it took the lease before it served"
            assert gauges(b, headers[1]) == (0, 1), "it follows and reads the leader's epoch"
            assert apps[1].state.services.store.epoch is None, "a follower holds no epoch"
            cut_off = apps[0].state.services.leadership.elector.lease
            monkeypatch.setattr(cut_off, "renew", unreachable)
            monkeypatch.setattr(cut_off, "campaign", unreachable)
            deadline = time.monotonic() + 30
            while gauges(b, headers[1])[0] == 0:
                assert time.monotonic() < deadline, "the follower never took the lease over"
                time.sleep(0.05)
            assert gauges(b, headers[1]) == (1, 2)
            assert gauges(a, headers[0]) == (0, 2), "deposed, it serves and reads the new epoch"
            assert apps[0].state.services.store.epoch is None
    finally:
        for app in apps:
            app.state.services.store.dispose()
    for app in apps:
        lease = app.state.services.leadership.elector.lease
        assert [t for t in texts if lease.holder in t or lease.address in t] == []


def test_stats_and_metrics_reuse_the_document_cache_until_a_change(api):
    svc = api.client.app.state.services
    assert api.get("/api/v1/stats").status_code == 200
    migrations_list = svc._doc_cache["migration"].rows
    plans_list = svc._doc_cache["plan"].rows
    assert api.get("/api/v1/metrics").status_code == 200
    assert api.get("/api/v1/stats?plan_id=plan-x").status_code == 200
    # nothing changed: the same list objects served every call (no reload)
    assert svc._doc_cache["migration"].rows is migrations_list
    assert svc._doc_cache["plan"].rows is plans_list
    # a new plan document moves the stamp of "plan" only
    assert api.post("/api/v1/providers", json=SOURCE).status_code == 201
    assert api.post("/api/v1/providers", json=DESTINATION).status_code == 201
    created = api.post(
        "/api/v1/plans",
        Role.approver,
        json={
            "name": "cache",
            "source_provider_id": SOURCE["id"],
            "destination_provider_id": DESTINATION["id"],
            "vm_ids": ["vm-1"],
        },
    )
    assert created.status_code == 201, created.text
    assert api.get("/api/v1/stats").status_code == 200
    assert svc._doc_cache["plan"].rows is not plans_list
    assert [p.id for p in svc._doc_cache["plan"].rows] == [created.json()["id"]]
    assert svc._doc_cache["migration"].rows is migrations_list


def test_the_document_cache_reloads_only_the_documents_that_changed(api, monkeypatch):
    """SDD §11: during a run a write lands every second; /stats reloads that document, not all."""
    svc = api.client.app.state.services
    kept, changed, gone = (make_migration() for _ in range(3))
    for m in (kept, changed, gone):
        api.store.put("migration", m)
    assert api.get("/api/v1/stats").json()["total"] == 3
    before = {m.id: m for m in svc._doc_cache["migration"].rows}

    api.store.put(
        "migration", changed.model_copy(update={"phase": Phase.syncing}), expected_version=1
    )
    api.store.delete("migration", gone.id)
    added = make_migration()
    api.store.put("migration", added)
    # what the API's cache loads (the orchestrator's tick, in the same app, has a cache of its own)
    loaded: list[str] = []
    real_get_many = AsyncStore.get_many

    async def counting_get_many(self, kind, ids, model_cls):
        if any(f.filename.endswith("deps.py") for f in inspect.stack(0)):
            loaded.extend(ids)
        return await real_get_many(self, kind, ids, model_cls)

    monkeypatch.setattr(AsyncStore, "get_many", counting_get_many)
    stats = api.get("/api/v1/stats").json()

    after = svc._doc_cache["migration"].rows
    assert [m.id for m in after] == [kept.id, changed.id, added.id]  # creation order
    assert after[0] is before[kept.id]  # not reloaded
    assert after[1].phase == Phase.syncing
    assert sorted(loaded) == sorted([changed.id, added.id])
    assert stats["total"] == 3 and stats["by_phase"].get("syncing") == 1


def test_stats_shape(api):
    res = api.get("/api/v1/stats")
    assert res.status_code == 200
    stats = res.json()
    assert set(stats) == {
        "total",
        "by_phase",
        "completed",
        "failed",
        "in_progress",
        "bytes_transferred",
        "avg_downtime_s",
        "p95_downtime_s",
        "max_downtime_s",
        "slo_compliance_pct",
        "data_inconsistent",
        "awaiting_finalize",
        "finalized",
        "downtime_by_strategy",
        "throughput_series",
    }
    assert stats["total"] == 0 and set(stats["by_phase"]) == {p.value for p in Phase}
    assert stats["avg_downtime_s"] is None and stats["slo_compliance_pct"] is None
    assert len(stats["throughput_series"]) == 60
    assert set(stats["throughput_series"][0]) == {"ts", "bps"}
    assert api.get("/api/v1/stats?plan_id=plan-unknown0").json()["total"] == 0


def test_advisor_status_and_similar_incidents_without_memory(api):
    status = api.get("/api/v1/advisor/status").json()
    assert status == {
        "jev": {
            "mode": "off",
            "available": False,
            "last_error": None,
            # ADV-06 (SDD §13.4): where the model runs, and whether the console may go there
            "deployment": "remote",
            "endpoint_host": None,
            "console_sent": False,
        },
        "memory": {"enabled": False, "available": False, "last_error": None},
    }
    sent = api.get("/api/v1/advisor/data-sent").json()["items"]
    assert [(i["decision_type"], i["tool"]) for i in sent] == [
        ("migration_strategy", "jev_decide"),
        ("workload_tier", "jev_classify"),
        ("verification_review", "jev_verify"),
        ("console_screening", "jev_screen"),
    ]
    assert all(i["sent"] is False for i in sent), "Jev is off: nothing is sent"
    keys = [f["key"] for f in sent[0]["fields"]]
    assert "estimates" in keys and all(f["label"] for f in sent[0]["fields"])
    res = api.post("/api/v1/advisor/similar-incidents", Role.operator, json={"query": "x"})
    assert res.status_code == 200 and res.json() == {"hits": []}


LEAKED = (
    "cutover of db-01 failed; ran --os-password Hunter2-Prod-Secret; "
    "OS_PASSWORD=Hunter2-Prod-Secret"  # gitleaks:allow
)


def memory_answering(content):
    """agentmemory answering every search with one hit whose title and content carry a password."""

    def memory(request):
        title = "db-01 OS_PASSWORD=Hunter2-Prod-Secret"  # gitleaks:allow
        hit = {"title": title, "content": content}
        return httpx.Response(200, json={"results": [hit]})

    return MemoryClient(
        "http://memory.example:3111",
        None,
        "seamless-migrate",
        transport=httpx.MockTransport(memory),
    )


def test_similar_incidents_reach_operators_redacted(api):
    """SDD §13.4 (audit AP-05): POST /advisor/similar-incidents answers agentmemory's hits - text of
    an outside service, which agents write too - redacted."""
    api.client.app.state.services.memory = memory_answering(LEAKED)
    res = api.post("/api/v1/advisor/similar-incidents", Role.operator, json={"query": "db-01"})
    assert res.status_code == 200 and res.json()["hits"], res.text
    assert "Hunter2-Prod-Secret" not in res.text, res.text


def test_similar_incidents_reach_viewers_redacted_in_recommendations(api):
    """SDD §13.4, §14.4 (audit AP-05): the hits a validation stores in each recommendation, which
    every viewer reads from GET /advisor/recommendations, are redacted before they are cut."""
    for provider in (SOURCE, DESTINATION):
        assert api.post("/api/v1/providers", json=provider).status_code == 201
    api.client.app.state.services.orchestrator.knowledge.memory = memory_answering(LEAKED)
    vm_ids = [vm["source_id"] for vm in api.get("/api/v1/providers/src-osp/inventory").json()]
    plan = {"name": "Redacted hits", "source_provider_id": "src-osp"}
    plan |= {"destination_provider_id": "dst-rhoso", "vm_ids": vm_ids}
    made = api.post("/api/v1/plans", Role.operator, json=plan)
    assert made.status_code == 201, made.text
    validated = api.post(f"/api/v1/plans/{made.json()['id']}/validate", Role.operator)
    assert validated.status_code == 200, validated.text
    page = api.get("/api/v1/advisor/recommendations?limit=200", Role.viewer)
    assert page.status_code == 200 and page.json()["total"] > 0, page.text
    hits = [hit for rec in page.json()["items"] for hit in rec["similar_incidents"]]
    assert hits and "Hunter2-Prod-Secret" not in page.text, hits[:1]


def test_cors_preflight_answers_keep_the_security_headers_and_the_envelope(tmp_path):
    """SDD §12 (audit AP-09): every response carries the R-05 security headers and every error is
    the envelope - a CORS preflight's answer too, allowed or refused, though the CORS layer answers
    it before any route."""
    settings, _ = api_settings(tmp_path, cors_origins=["https://dashboard.example"])
    store = Store(f"sqlite:///{tmp_path / 'cors.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:

        def preflight(origin):
            headers = {"Origin": origin, "Access-Control-Request-Method": "GET"}
            return client.options("/api/v1/plans", headers=headers)

        refused, allowed = preflight("https://evil.example"), preflight("https://dashboard.example")
    store.dispose()
    for res in (refused, allowed):
        assert {"x-frame-options", "content-security-policy"} <= set(res.headers), res.headers
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://dashboard.example"
    assert refused.status_code == 400, refused.text
    assert refused.json() == {"error": {"code": "bad_request", "message": "Disallowed CORS origin"}}
    assert "access-control-allow-origin" not in refused.headers
    # a cache must not answer the allowed origin with this refusal
    assert "Origin" in refused.headers["vary"]


def test_spa_fallback_serves_index(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Seamless</title>")
    (dist / "assets" / "app.js").write_text("console.log('x')")
    (tmp_path / "secret.txt").write_text("nope")
    settings, _ = api_settings(tmp_path, dashboard_dir=dist)
    store = Store(f"sqlite:///{tmp_path / 'spa.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        for path in ("/", "/plans", "/migrations/mig-123", "/inventory/src-osp"):
            res = client.get(path)
            assert res.status_code == 200 and "Seamless" in res.text, path
        js = client.get("/assets/app.js")
        assert js.status_code == 200 and "console.log" in js.text
        api_404 = client.get("/api/v1/does-not-exist")
        assert api_404.status_code == 404 and api_404.json()["error"]["code"] == "not_found"
        assert "nope" not in client.get("/..%2Fsecret.txt").text
    store.dispose()


def test_the_dashboard_fallback_looks_at_no_path_outside_its_directory(tmp_path, monkeypatch):
    """SDD §12 (CodeQL py/path-injection): a path that leaves the dashboard's directory - ".."
    segments - is answered with the index without the file system being asked about it, and a
    symlink inside the directory that names a file outside it is not served."""
    import os

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Seamless</title>")
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    (dist / "leak.txt").symlink_to(secret)
    settings, _ = api_settings(tmp_path, dashboard_dir=dist)
    store = Store(f"sqlite:///{tmp_path / 'spa-paths.db'}")
    store.create_schema()
    looked: list[str] = []
    stat, lstat = os.stat, os.lstat

    def spying(real):
        def call(path, *args, **kwargs):
            looked.append(os.fspath(path) if not isinstance(path, int) else "")
            return real(path, *args, **kwargs)

        return call

    with TestClient(create_app(settings, store)) as client:
        monkeypatch.setattr(os, "stat", spying(stat))
        monkeypatch.setattr(os, "lstat", spying(lstat))
        outside = client.get("/..%2Fsecret.txt")
        monkeypatch.setattr(os, "stat", stat)
        monkeypatch.setattr(os, "lstat", lstat)
        assert outside.status_code == 200 and "Seamless" in outside.text
        assert str(secret) not in looked, looked
        linked = client.get("/leak.txt")
        assert linked.status_code == 200 and "nope" not in linked.text
    store.dispose()


def test_the_dashboard_fallback_answers_a_nul_path_with_the_index(tmp_path):
    """SDD §12 (review RV33-05): a path holding a NUL byte - GET /plans%00, unauthenticated - names
    no file: the fallback answers the index, never a 500 with a traceback in the log."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Seamless</title>")
    settings, _ = api_settings(tmp_path, dashboard_dir=dist)
    store = Store(f"sqlite:///{tmp_path / 'spa-nul.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        for path in ("/plans%00", "/%00", "/assets%00/x.js"):
            answer = client.get(path)
            assert answer.status_code == 200 and "Seamless" in answer.text, (path, answer.text)
    store.dispose()


def test_a_wrong_method_on_an_api_route_is_405_where_the_dashboard_is_served(tmp_path):
    """SDD §12: 405 `method_not_allowed` for a method an API route does not take - the
    dashboard's GET fallback must not turn a GET on a POST-only route into a 404."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Seamless</title>")
    settings, _ = api_settings(tmp_path, dashboard_dir=dist)
    store = Store(f"sqlite:///{tmp_path / 'spa405.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        for path in ("/api/v1/plans/plan-00000001/start", "/api/v1/migrations/mig-1/cutover"):
            res = client.get(path)
            assert (res.status_code, res.json()["error"]["code"]) == (405, "method_not_allowed")
        # an unknown API path stays 404, the dashboard's own routes stay the dashboard
        missing = client.get("/api/v1/does-not-exist")
        assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not_found")
        assert client.get("/plans/plan-00000001").status_code == 200
    store.dispose()


def test_auth_disabled_maps_to_anonymous_admin(tmp_path):
    settings, _ = api_settings(tmp_path, auth_disabled=True, tokens_file=None)
    store = Store(f"sqlite:///{tmp_path / 'anon.db'}")
    store.create_schema()
    # an auth-off server is reached by a loopback name only (SDD §13.1, audit AP-03)
    with TestClient(create_app(settings, store), base_url="http://127.0.0.1:8080") as client:
        assert client.get("/api/v1/me").json() == {
            "name": "anonymous",
            "role": "admin",
            "host_key_checking": True,
        }
        assert client.post("/api/v1/providers", json=SOURCE).status_code == 201
        created = [e for e in store.events(since_seq=0) if e.kind == "provider.created"]
        assert created[0].actor == "anonymous"
    store.dispose()


def test_with_authentication_off_only_this_machines_pages_reach_the_api(tmp_path):
    """SDD §13.1, Security.md C1-02/C1-12 (audit AP-03): with authentication off the anonymous
    admin answers only this machine. A DNS-rebinding page reaches the loopback port under its own
    name - a Host naming no loopback address - and another site's page sends simple cross-site
    requests: both are refused with 403, before any side effect. Loopback names (with any port,
    IPv6 too) and clients that send no Origin (the CLI, curl) keep working."""
    settings, _ = api_settings(tmp_path, auth_disabled=True, tokens_file=None)
    store = Store(f"sqlite:///{tmp_path / 'off.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store), base_url="http://127.0.0.1:8080") as client:
        assert client.post("/api/v1/providers", json=SOURCE).status_code == 201
        rebound = {"Host": "rebind.attacker.example:8080"}
        listed = client.get("/api/v1/providers", headers=rebound)
        assert listed.status_code == 403, listed.text
        assert listed.json()["error"]["code"] == "forbidden"
        assert "x-frame-options" in listed.headers  # R-05 headers on the refusal too
        repointed = client.patch(
            "/api/v1/providers/src-osp",
            headers={**rebound, "Origin": "http://rebind.attacker.example:8080"},
            json={"endpoint": "https://rebind.attacker.example/v3"},
        )
        assert repointed.status_code == 403, repointed.text
        # a simple cross-site request reaches the loopback Host itself: its Origin gives it away
        for cross_site in (
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            refused = client.post("/api/v1/providers/src-osp/check", headers=cross_site)
            assert refused.status_code == 403, (cross_site, refused.text)
        assert store.get("provider", "src-osp", Provider).endpoint == SOURCE["endpoint"]
        denied = [e for e in store.events(since_seq=0) if e.kind == "auth.denied"]
        assert len(denied) == 5 and all(
            e.data["reason"].startswith("with authentication off") for e in denied
        ), [e.message for e in denied]
        for host in ("127.0.0.1:8080", "localhost:5173", "[::1]:8080", "127.0.0.2", "LOCALHOST"):
            assert client.get("/api/v1/providers", headers={"Host": host}).status_code == 200, host
        same_site = {"Origin": "http://localhost:5173", "Sec-Fetch-Site": "same-site"}
        assert (
            client.patch(
                "/api/v1/providers/src-osp", headers=same_site, json={"name": "Source DC1"}
            ).status_code
            == 200
        )
    store.dispose()


def test_with_authentication_on_any_host_is_answered(api):
    """SDD §13.1 (audit AP-03): the loopback rule is the auth-off mode's alone - with tokens the
    server answers the names it is reached by (a route, an ingress host)."""
    headers = {**api.h("viewer"), "Host": "seamless.apps.example"}
    assert api.client.get("/api/v1/providers", headers=headers).status_code == 200


def test_health_says_whether_authentication_is_off(api, tmp_path):
    # Sign in offers to continue without a token only when it works, and asks a public route:
    # GET /me without a token would be refused, an auth.denied event and a lockout tick per
    # visit (SDD §12, §16)
    for path in ("/api/v1/health", "/api/v1/ready"):
        assert api.client.get(path).json()["auth_disabled"] is False
    assert [e for e in api.store.events(since_seq=0) if e.kind == "auth.denied"] == []
    (tmp_path / "off").mkdir()
    settings, _ = api_settings(tmp_path / "off", auth_disabled=True, tokens_file=None)
    store = Store(f"sqlite:///{tmp_path / 'off.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        for path in ("/api/v1/health", "/api/v1/ready"):
            assert client.get(path).json()["auth_disabled"] is True
    store.dispose()


def test_render_metrics_escapes_labels_and_formats_numbers():
    from seamless_migrate.metrics import _escape, _num, advisor_calls, render_metrics

    assert _escape('a"b\\c\nd') == 'a\\"b\\\\c\\nd'
    assert _num(2.0) == "2" and _num(2.5) == "2.5" and _num(3) == "3"
    assert advisor_calls(None) == {} and advisor_calls(object()) == {}
    text = render_metrics(
        [],
        {"cut over": [12.5, 2]},
        {("jev_decide", "ok"): 3, ('tool"x', "error"): 1},
        {"count": 4, "sum": 0.5, "max": 0.25, "slow": 1},
    )
    assert 'seamless_step_duration_seconds_sum{step="cut over"} 12.5' in text
    assert 'seamless_advisor_calls_total{tool="tool\\"x",outcome="error"} 1' in text
    assert "seamless_tick_seconds_count 4" in text and "seamless_tick_slow_total 1" in text
    assert text.endswith("\n")


def test_the_cutover_refusals_gauge_counts_the_migrations_dashboard_lists_so():
    """SDD §18: the migrations in precopy, syncing or awaiting_cutover whose last pass found what
    their cutover will refuse - not one in another phase, not one whose pass found nothing."""
    from seamless_migrate.metrics import render_metrics

    refusal = ["the NIC 00:50:56:40:d5:b3 of portal-02 on 'dc2-prod': port p-1 holds 10.2.0.32"]
    migrations = [
        make_migration(id=f"mig-{i:010x}", phase=phase, cutover_refusals=refusals)
        for i, (phase, refusals) in enumerate(
            [
                (Phase.precopy, refusal),
                (Phase.syncing, refusal * 2),  # one migration, however many problems
                (Phase.awaiting_cutover, refusal),
                (Phase.awaiting_cutover, []),  # its last pass found nothing
                (Phase.failed, refusal),  # refused already: Needs attention lists it as failed
                (Phase.ready, refusal),  # a retry: its pre-copy looks again
            ]
        )
    ]
    text = render_metrics(migrations, {}, {})
    assert "# TYPE seamless_cutover_refusals gauge" in text
    assert "\nseamless_cutover_refusals 3\n" in text


def test_metrics_counts_the_migrations_waiting_for_gpu_capacity(api):
    """SDD §18 (§5.4 rule 5, §22.7): /metrics' seamless_gpu_waits gauge counts the migrations whose
    cutover waits for GPU capacity - one in ready and one at its warm gate - and none of the
    others, whatever their phase."""
    from seamless_migrate.domain.models import GpuWait, utcnow

    assert "\nseamless_gpu_waits 0\n" in api.get("/api/v1/metrics").text
    wait = GpuWait(
        since=utcnow(),
        checked_at=utcnow(),
        pool="VGPU CUSTOM_NVIDIA_A100_4C",
        needed=2,
        free=1,
        reason="VGPU CUSTOM_NVIDIA_A100_4C of RHOSO 18.0 has 1 unit free; it needs 2",
    )
    for i, (phase, gpu_wait) in enumerate(
        [
            (Phase.ready, wait),
            (Phase.awaiting_cutover, wait),
            (Phase.ready, None),
            (Phase.awaiting_cutover, None),
            (Phase.completed, None),
        ]
    ):
        api.store.put(
            "migration", make_migration(id=f"mig-{i:010x}", phase=phase, gpu_wait=gpu_wait)
        )
    text = api.get("/api/v1/metrics").text
    assert "# TYPE seamless_gpu_waits gauge" in text
    assert "\nseamless_gpu_waits 2\n" in text


# routes test_role_matrix cannot drive, and the test that checks their role instead
MATRIX_EXCEPTIONS = {
    ("GET", "/events/stream"): "test_sse_route_requires_viewer (a stream never ends)",
    ("POST", "/demo/vms/{id}/power"): (
        "test_the_demo_power_route_sets_a_fake_source_vms_power_and_exists_only_in_demo_mode"
    ),
}


def _route_key(app, method, path):
    """(method, path template with every placeholder written {id}) of the route serving path: of
    the templates that match, the one with the fewest placeholders, as a literal route wins."""
    matching = [
        template
        for template, operations in app.openapi()["paths"].items()
        if method.lower() in operations
        and re.match("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$", path)
    ]
    assert matching, f"no route serves {method} {path}"
    template = min(matching, key=lambda t: t.count("{"))
    return method, re.sub(r"\{[^}]+\}", "{id}", template[len("/api/v1") :])


def test_the_role_matrix_covers_every_route_sdd_12_lists(api):
    """SDD §12: test_role_matrix drives every route with the role the table gives it - a route
    added without its matrix row, or a row whose role drifted from the table, fails here - and
    every public route answers without a token."""
    from seamless_migrate.config import find_repo_root

    sdd = (find_repo_root() / "docs" / "SDD.md").read_text(encoding="utf-8")
    documented = {
        (m, re.sub(r"\{[^}]+\}", "{id}", p)): role
        for m, p, role in re.findall(
            r"^\| (GET|POST|PUT|PATCH|DELETE) \| `([^`]+)` \| (\w+)", sdd, re.M
        )
    }
    matrix = {_route_key(api.client.app, m, p): r.value for m, p, r, _ in ROUTES}
    expected = {
        key: role
        for key, role in documented.items()
        if role != "public" and key not in MATRIX_EXCEPTIONS
    }
    assert matrix == expected
    for method, path in [key for key, role in documented.items() if role == "public"]:
        assert api.client.request(method, "/api/v1" + path).status_code not in (401, 403), path


def test_every_route_is_documented_in_sdd_12(api):
    """The routes the app serves and the SDD §12 table agree (method + path, both ways)."""
    import re

    from seamless_migrate.config import find_repo_root

    paths = api.client.app.openapi()["paths"]
    served = set()
    for path, operations in paths.items():
        if not path.startswith("/api/v1/"):
            continue
        norm = re.sub(r"\{[^}]+\}", "{id}", path[len("/api/v1") :])
        for method in operations:
            if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                served.add((method.upper(), norm))
    sdd = (find_repo_root() / "docs" / "SDD.md").read_text(encoding="utf-8")
    documented = {
        (m, re.sub(r"\{[^}]+\}", "{id}", p))
        for m, p in re.findall(r"^\| (GET|POST|PUT|PATCH|DELETE) \| `([^`]+)` \|", sdd, re.M)
    }
    assert served - documented == set(), "served but missing from SDD §12"
    assert documented - served == set(), "in SDD §12 but not served"
    assert len(served) >= 30


def test_auth_lockout_after_repeated_failures(tmp_path):
    settings, tokens = api_settings(tmp_path, auth_lockout_per_minute=3)
    store = Store(f"sqlite:///{tmp_path / 'lock.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        bad = {"Authorization": "Bearer smg_not_a_real_token"}
        for _ in range(3):
            assert client.get("/api/v1/me", headers=bad).status_code == 401
        locked = client.get("/api/v1/me", headers=bad)
        assert locked.status_code == 429 and locked.json()["error"]["code"] == "too_many_requests"
        # valid tokens from the same address keep working (an ingress or NAT shares one
        # address between everyone): only failed authentications are answered with 429
        good = {"Authorization": f"Bearer {tokens[Role.admin]}"}
        assert client.get("/api/v1/me", headers=good).status_code == 200
        assert client.get("/api/v1/me", headers=bad).status_code == 429
        assert client.get("/api/v1/health").status_code == 200, "public routes stay open"
        client.app.state.services._auth_failures.clear()
        assert client.get("/api/v1/me", headers=bad).status_code == 401
        assert "testclient" in client.app.state.services._auth_failures
        kinds = [e.kind for e in store.events(since_seq=0, limit=100)]
        assert kinds.count("auth.denied") >= 4
    store.dispose()

    off, _ = api_settings(tmp_path / "off", auth_lockout_per_minute=0)
    store2 = Store(f"sqlite:///{tmp_path / 'off.db'}")
    store2.create_schema()
    with TestClient(create_app(off, store2)) as client:
        for _ in range(5):
            assert client.get("/api/v1/me", headers=bad).status_code == 401
    store2.dispose()


def test_a_locked_out_client_costs_no_database_read(tmp_path, monkeypatch):
    """Audit B-01 (SDD §13.1): once an address is locked out, a failed authentication is answered
    429 before the database users are looked up - a flood costs no document read - while a valid
    bootstrap token from the same address still passes; the users' digests are kept in memory and
    read again only when the user kind's change stamp moved."""
    settings, tokens = api_settings(tmp_path, auth_lockout_per_minute=2)
    store = Store(f"sqlite:///{tmp_path / 'lock.db'}")
    store.create_schema()
    reads: list[str] = []
    original_list, original_stamp = Store.list, Store.change_stamp

    def counting_list(self, kind, *args, **kwargs):
        if kind == "user":
            reads.append("list")
        return original_list(self, kind, *args, **kwargs)

    def counting_stamp(self, kind):
        if kind == "user":
            reads.append("stamp")
        return original_stamp(self, kind)

    monkeypatch.setattr(Store, "list", counting_list)
    monkeypatch.setattr(Store, "change_stamp", counting_stamp)
    with TestClient(create_app(settings, store)) as client:
        bad = {"Authorization": "Bearer smg_not_a_real_token"}
        for _ in range(2):
            assert client.get("/api/v1/me", headers=bad).status_code == 401
        assert reads == ["stamp", "list", "stamp"], "the digests are kept while the stamp holds"
        for _ in range(20):
            assert client.get("/api/v1/me", headers=bad).status_code == 429
        assert reads == ["stamp", "list", "stamp"], "a locked-out address never reaches the store"
        good = {"Authorization": f"Bearer {tokens[Role.admin]}"}
        assert client.get("/api/v1/me", headers=good).status_code == 200
        assert reads == ["stamp", "list", "stamp"], "the bootstrap file answers without the store"
        client.app.state.services._auth_failures.clear()
        assert client.get("/api/v1/me", headers=bad).status_code == 401
        assert reads == ["stamp", "list", "stamp", "stamp"]
    store.dispose()


def test_database_user_digests_are_read_again_after_their_max_age(tmp_path, monkeypatch):
    """Audit B-01 (SDD §13.1): a deletion and a re-creation at the same version leave the change
    stamp unchanged, so the kept digests are read again at the latest after their max age - a
    rotated token then works and the replaced one does not."""
    from seamless_migrate.api import deps
    from seamless_migrate.security.auth import DashboardUser, generate_token, hash_token

    settings, _ = api_settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'users.db'}")
    store.create_schema()
    first, second = generate_token(), generate_token()

    def alice(token: str) -> DashboardUser:
        return DashboardUser(id="alice", name="alice", role=Role.viewer, sha256=hash_token(token))

    def me(token: str) -> int:
        return client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code

    with TestClient(create_app(settings, store)) as client:
        store.create_user(alice(first), "admin-user")
        assert me(first) == 200
        store.delete("user", "alice")
        store.create_user(alice(second), "admin-user")
        monkeypatch.setattr(deps, "USER_DIGESTS_MAX_AGE_S", 0.0)
        assert (me(second), me(first)) == (200, 401)
    store.dispose()


def test_security_headers_and_api_docs_exposure(tmp_path):
    # demo: docs are public; headers on API and SPA responses alike
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Seamless</title>")
    settings, tokens = api_settings(tmp_path, dashboard_dir=dist)
    store = Store(f"sqlite:///{tmp_path / 'hdr.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        for path in ("/", "/api/v1/health", "/api/v1/does-not-exist"):
            res = client.get(path)
            assert res.headers["x-content-type-options"] == "nosniff", path
            assert res.headers["x-frame-options"] == "DENY"
            assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
            # the dashboard bundles its fonts (SDD §16): fonts and styles come from this server only
            csp = res.headers["content-security-policy"]
            directives = dict(d.strip().split(" ", 1) for d in csp.split(";"))
            assert directives["font-src"] == "'self'", path
            assert directives["style-src"] == "'self' 'unsafe-inline'", path
        docs = client.get("/api/docs")
        assert docs.status_code == 200 and "swagger" in docs.text.lower()
        csp = docs.headers["content-security-policy"]
        assert "cdn.jsdelivr.net" in csp and "'unsafe-inline'" not in csp.split(";")[1]
        # the inline bootstrap script carries the per-response nonce the CSP allows
        [nonce] = re.findall(r"'nonce-([A-Za-z0-9_-]+)'", csp)
        assert "<script>" not in docs.text and f'<script nonce="{nonce}">' in docs.text
        assert (
            nonce
            != re.findall(
                r"'nonce-([A-Za-z0-9_-]+)'",
                client.get("/api/docs").headers["content-security-policy"],
            )[0]
        )
        spec = client.get("/api/openapi.json").json()
        assert "/api/v1/plans" in spec["paths"]
        assert "/api/openapi.json" not in spec["paths"], "docs routes stay out of the schema"
    store.dispose()

    # outside demo mode the docs need a viewer token (Security.md R-04, QASuite S-07)
    prod, tokens = api_settings(tmp_path / "prod", demo=False)
    store2 = Store(f"sqlite:///{tmp_path / 'prod.db'}")
    store2.create_schema()
    with TestClient(create_app(prod, store2)) as client:
        assert client.get("/api/docs").status_code == 401
        assert client.get("/api/openapi.json").status_code == 401
        viewer = {"Authorization": f"Bearer {tokens[Role.viewer]}"}
        assert client.get("/api/openapi.json", headers=viewer).status_code == 200
        assert client.get("/api/docs", headers=viewer).status_code == 200
        assert client.get("/docs").status_code == 404 and client.get("/redoc").status_code == 404
    store2.dispose()


def test_disabled_api_docs_paths_answer_404_where_the_dashboard_is_served(tmp_path):
    """FastAPI's default /docs, /redoc and /openapi.json are disabled (SDD §12, Security.md R-04,
    QASuite S-07): the SPA fallback must not answer them with the dashboard, or a scanner pointed
    at /openapi.json (S-09, S-23) reads HTML."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Seamless</title>")
    prod, _ = api_settings(tmp_path, demo=False, dashboard_dir=dist)
    store = Store(f"sqlite:///{tmp_path / 'docs.db'}")
    store.create_schema()
    with TestClient(create_app(prod, store)) as client:
        for path in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
            res = client.get(path)
            assert res.status_code == 404, path
            assert res.json()["error"]["code"] == "not_found", path
        # the dashboard's own routes still get the SPA
        for path in ("/", "/plans", "/migrations/mig-1", "/docs-and-more"):
            res = client.get(path)
            assert res.status_code == 200 and "<title>Seamless</title>" in res.text, path
    store.dispose()


def test_dashboard_mock_error_codes_are_api_codes():
    """The dashboard's mock API (mock mode, unit tests) answers only with (status, code) pairs the
    real API sends (SDD §12), so the UI is never developed against an error the backend never
    returns."""
    import re

    from seamless_migrate.api import app as app_module
    from seamless_migrate.config import find_repo_root

    root = find_repo_root()
    api: set[tuple[int, str]] = {(s, c) for s, c in app_module._STATUS_CODES.items()}
    for path in (root / "seamless" / "src" / "seamless_migrate" / "api").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        api |= {
            (int(s), c)
            for s, c in re.findall(r'(?:ApiError|_error)\(\s*(\d{3}),\s*"([a-z_]+)"', text)
        }
    mock = (root / "dashboard" / "src" / "api" / "mock.ts").read_text(encoding="utf-8")
    used = {(int(s), c) for s, c in re.findall(r"HttpError\((\d{3}), '([a-z_]+)'", mock)}
    used |= {(int(s), c) for c, s in re.findall(r"code: '([a-z_]+)'[^\n]*?status: (\d{3})", mock)}
    used |= {
        (int(s), c)
        for s, c in re.findall(r"status: (\d{3}),\s*body: \{ error: \{ code: '([a-z_]+)'", mock)
    }
    assert used, "no error responses found in mock.ts"
    assert used <= api, f"mock-only error responses: {sorted(used - api)}"


_APPROVE = "/api/v1/migrations/mig-0000000000/approve"
_MIB = 1024 * 1024


def test_request_body_over_1_mib_is_refused_with_413_before_auth(api):
    """SDD §12, Security.md R-17: FastAPI parses a body before the token is checked, so an
    oversized body is refused first; exactly 1 MiB still reaches the route (here: 401)."""
    head, tail = b'{"comment": "', b'"}'
    exact = head + b"x" * (_MIB - len(head) - len(tail)) + tail
    assert len(exact) == _MIB
    json_headers = {"Content-Type": "application/json"}
    assert api.client.post(_APPROVE, headers=json_headers, content=exact).status_code == 401
    res = api.client.post(_APPROVE, headers=json_headers, content=exact + b" ")
    assert res.status_code == 413, res.text
    assert res.json()["error"]["code"] == "payload_too_large"
    assert res.headers["X-Content-Type-Options"] == "nosniff"  # R-05 holds for the 413 too


def test_chunked_request_body_over_1_mib_is_refused_with_413(api):
    """A chunked body has no Content-Length: the bytes are counted as they arrive (SDD §12)."""

    def chunks():
        for _ in range(17):  # 17 x 64 KiB = 1,114,112 bytes
            yield b"x" * 65536

    res = api.client.post(_APPROVE, headers={"Content-Type": "application/json"}, content=chunks())
    assert res.status_code == 413, res.text
    assert res.json()["error"]["code"] == "payload_too_large"


async def test_body_limit_counts_streamed_chunks():
    """The inner app never receives more than the limit across several body messages."""
    from starlette.exceptions import HTTPException

    from seamless_migrate.api.app import BodyLimitMiddleware

    seen: list[int] = []

    async def inner(scope, receive, send):
        while True:
            message = await receive()
            seen.append(len(message.get("body", b"")))
            if not message.get("more_body"):
                return

    messages = iter(
        [{"type": "http.request", "body": b"abcd", "more_body": True}] * 3
        + [{"type": "http.request", "body": b"", "more_body": False}]
    )

    async def receive():
        return next(messages)

    async def send(message):
        raise AssertionError("the middleware must not answer a body without Content-Length itself")

    scope = {"type": "http", "method": "POST", "path": "/api/v1/plans", "headers": []}
    with pytest.raises(HTTPException) as refused:
        await BodyLimitMiddleware(inner, max_bytes=10)(scope, receive, send)
    assert refused.value.status_code == 413
    assert seen == [4, 4]  # the third chunk (12 > 10 bytes) never reached the app


@pytest.mark.parametrize(
    ("where", "console"),
    [
        ({}, False),  # remote by default: the console excerpt stays local (SDD §13.4)
        ({"jev_deployment": "on_prem", "jev_endpoint_host": "jev.dc1.example.internal"}, True),
        ({"console_screening_send_to_remote": True}, True),
    ],
)
def test_the_advisor_says_where_its_model_runs_and_what_it_receives(tmp_path, where, console):
    """ADV-06: with Jev on, the status names the deployment and the console's fate; the data-sent
    listing agrees."""
    settings, tokens = api_settings(
        tmp_path, jev_mode="http", jev_url="http://jev.invalid:8080/mcp", **where
    )
    store = Store(f"sqlite:///{tmp_path / 'where.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        api = Api(client, store, settings, tokens)
        jev = api.get("/api/v1/advisor/status").json()["jev"]
        assert (jev["deployment"], jev["endpoint_host"], jev["console_sent"]) == (
            where.get("jev_deployment", "remote"),
            where.get("jev_endpoint_host"),
            console,
        )
        items = {
            i["decision_type"]: i for i in api.get("/api/v1/advisor/data-sent").json()["items"]
        }
        assert items["migration_strategy"]["sent"] is True
        assert items["console_screening"]["sent"] is console
        verify_keys = [f["key"] for f in items["verification_review"]["fields"]]
        assert ("console" in verify_keys) is console
    store.dispose()


def test_plans_refuse_a_resource_they_cannot_pre_stage(api):
    """SDD §4.2, §12: POST and PATCH /plans refuse a pre-stage name without an export/import
    playbook pair (422); "flavors" is accepted."""
    assert api.post("/api/v1/providers", json=SOURCE).status_code == 201
    assert api.post("/api/v1/providers", json=DESTINATION).status_code == 201
    body = {
        "name": "prestage",
        "source_provider_id": SOURCE["id"],
        "destination_provider_id": DESTINATION["id"],
        "vm_ids": ["vm-1"],
    }
    refused = api.post(
        "/api/v1/plans", Role.operator, json={**body, "prestage_resources": ["flavours"]}
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"] == {
        "code": "validation_error",
        "message": "prestage_resources: cannot pre-stage 'flavours'",
    }
    created = api.post(
        "/api/v1/plans", Role.operator, json={**body, "prestage_resources": ["networks", "flavors"]}
    )
    assert created.status_code == 201, created.text
    patched = api.client.patch(
        f"/api/v1/plans/{created.json()['id']}",
        headers=api.h(Role.operator),
        json={"prestage_resources": ["workloads"]},
    )
    assert patched.status_code == 422 and "'workloads'" in patched.json()["error"]["message"]


#: well-formed ISO-8601 times whose UTC instant lies outside datetime's range (audit AA-01)
OUT_OF_RANGE = ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00")


@pytest.mark.parametrize("stamp", OUT_OF_RANGE)
def test_a_time_outside_datetimes_range_is_refused_naming_its_field(tmp_path, stamp):
    """SDD §4, §12 (audit AA-01): converting such a time to UTC raised OverflowError, which pydantic
    does not turn into a validation error - POST /plans, PATCH /plans/{id} and POST /providers
    answered 500 with a traceback in the log. It is a 422 naming the field."""
    settings, tokens = api_settings(tmp_path, secrets_dir=tmp_path / "secrets")
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store), raise_server_exceptions=False) as client:
        api = Api(client, store, settings, tokens)
        assert api.post("/api/v1/providers", json=SOURCE).status_code == 201
        assert api.post("/api/v1/providers", json=DESTINATION).status_code == 201
        plan = {
            "name": "Range plan",
            "source_provider_id": SOURCE["id"],
            "destination_provider_id": DESTINATION["id"],
            "vm_ids": [],
        }
        window = {"start": stamp, "end": "2030-01-02T00:00:00Z"}
        created = api.post("/api/v1/plans", Role.approver, json={**plan, "cutover_window": window})
        assert created.status_code == 422, created.text
        assert "cutover_window.start" in created.json()["error"]["message"]
        draft = api.post("/api/v1/plans", Role.approver, json=plan).json()
        patched = client.patch(
            f"/api/v1/plans/{draft['id']}",
            headers=api.h(Role.approver),
            json={"cutover_window": window},
        )
        assert patched.status_code == 422, patched.text
        provider = api.post(
            "/api/v1/providers", json={**SOURCE, "id": "src-two", "last_checked_at": stamp}
        )
        assert provider.status_code == 422, provider.text
        assert "last_checked_at" in provider.json()["error"]["message"]
    store.dispose()


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/advisor/recommendations/rec-none/approve",
        "/api/v1/plans/plan-none/validate",
        "/api/v1/plans/plan-none/start",
        "/api/v1/plans/plan-none/pause",
        "/api/v1/migrations/mig-none/sync",
        "/api/v1/migrations/mig-none/retry",
        "/api/v1/providers/prov-none/check",
    ],
)
def test_a_route_whose_request_is_empty_refuses_a_field(api, path):
    """SDD §12 (audit AA-04): the routes that take no body dropped every field of one - an approve
    sent the option meant for an override applied the advisor's, recorded as the person's. An
    empty object or no body passes; a field is a 422 naming it."""
    refused = api.client.post(path, headers=api.h(Role.admin), json={"option": "cold"})
    assert refused.status_code == 422, refused.text
    assert "option: Extra inputs are not permitted" in refused.json()["error"]["message"]
    for body in ({}, None):
        taken = api.client.post(path, headers=api.h(Role.admin), json=body)
        assert taken.status_code == 404, (body, taken.text)  # its unknown id, as before
