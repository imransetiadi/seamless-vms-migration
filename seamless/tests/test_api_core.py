import pytest
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.domain.enums import Phase, Role
from seamless_migrate.store import Store
from tests.api_support import DESTINATION, ROLES, SOURCE, Api, api_settings


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
    assert res.json() == {"status": "ok", "version": "0.1.0", "demo": True, "db": "ok"}
    monkeypatch.setattr(api.store, "ping", lambda: False)
    assert api.client.get("/api/v1/health").json()["db"] == "error"
    assert api.client.get("/api/v1/health").json()["status"] == "degraded"


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
    assert me.json() == {"name": "approver-user", "role": "approver"}


def test_me_and_forbidden_audit(api):
    res = api.post("/api/v1/providers", Role.operator, json=SOURCE)
    assert res.status_code == 403 and res.json()["error"]["code"] == "forbidden"
    denied = [e for e in api.store.events(since_seq=0) if e.kind == "auth.denied"]
    assert denied[-1].actor == "operator-user" and denied[-1].data["required_role"] == "admin"


ROUTES = [
    ("GET", "/api/v1/me", Role.viewer, None),
    ("GET", "/api/v1/providers", Role.viewer, None),
    ("POST", "/api/v1/providers", Role.admin, {**SOURCE, "id": "matrix-src"}),
    ("GET", "/api/v1/providers/src-osp", Role.viewer, None),
    ("DELETE", "/api/v1/providers/nope-1", Role.admin, None),
    ("POST", "/api/v1/providers/src-osp/check", Role.operator, None),
    ("GET", "/api/v1/providers/src-osp/inventory", Role.viewer, None),
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
    ("POST", "/api/v1/advisor/similar-incidents", Role.operator, {"query": "timeout"}),
    ("GET", "/api/v1/metrics", Role.viewer, None),
]


@pytest.fixture(scope="module")
def matrix_api(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("matrix")
    settings, tokens = api_settings(tmp_path)
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
        "# TYPE seamless_step_duration_seconds summary",
        "# TYPE seamless_advisor_calls_total counter",
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
        "jev": {"mode": "off", "available": False, "last_error": None},
        "memory": {"enabled": False, "available": False, "last_error": None},
    }
    res = api.post("/api/v1/advisor/similar-incidents", Role.operator, json={"query": "x"})
    assert res.status_code == 200 and res.json() == {"hits": []}


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


def test_auth_disabled_maps_to_anonymous_admin(tmp_path):
    settings, _ = api_settings(tmp_path, auth_disabled=True, tokens_file=None)
    store = Store(f"sqlite:///{tmp_path / 'anon.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        assert client.get("/api/v1/me").json() == {"name": "anonymous", "role": "admin"}
        assert client.post("/api/v1/providers", json=SOURCE).status_code == 201
        created = [e for e in store.events(since_seq=0) if e.kind == "provider.created"]
        assert created[0].actor == "anonymous"
    store.dispose()
