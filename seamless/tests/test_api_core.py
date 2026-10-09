import re

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
    body = res.json()
    orchestrator = body.pop("orchestrator")
    assert body == {"status": "ok", "version": "0.1.0", "demo": True, "db": "ok"}
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
    assert api.get("/api/v1/events?since=9223372036854775807").status_code == 200


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
    ("PATCH", "/api/v1/providers/src-osp", Role.admin, {"name": "renamed"}),
    (
        "PUT",
        "/api/v1/providers/src-osp/credentials",
        Role.admin,
        {"username": "u", "password": "p", "project_name": "x"},
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
        "seamless_tick_seconds_count",
        "# TYPE seamless_tick_slow_total counter",
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


def test_stats_and_metrics_reuse_the_document_cache_until_a_change(api):
    svc = api.client.app.state.services
    assert api.get("/api/v1/stats").status_code == 200
    migrations_list = svc._doc_cache["migration"][1]
    plans_list = svc._doc_cache["plan"][1]
    assert api.get("/api/v1/metrics").status_code == 200
    assert api.get("/api/v1/stats?plan_id=plan-x").status_code == 200
    # nothing changed: the same list objects served every call (no reload)
    assert svc._doc_cache["migration"][1] is migrations_list
    assert svc._doc_cache["plan"][1] is plans_list
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
    assert svc._doc_cache["plan"][1] is not plans_list
    assert [p.id for p in svc._doc_cache["plan"][1]] == [created.json()["id"]]
    assert svc._doc_cache["migration"][1] is migrations_list


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
            assert "fonts.gstatic.com" in res.headers["content-security-policy"]
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
