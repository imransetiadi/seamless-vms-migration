"""The API over both store backends (SDD §11/§12): the request-path queries that are pushed into
SQL — JSON-path filters, paging, the change stamp, event replay — behave the same on SQLite and
PostgreSQL (the latter runs when ``SEAMLESS_TEST_PG_URL`` is set, as in CI)."""

from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.domain.enums import Role
from tests.api_support import DESTINATION, SOURCE, Api, api_settings


def test_api_journey_on_every_store_backend(any_store, tmp_path):
    settings, tokens = api_settings(tmp_path)
    with TestClient(create_app(settings, any_store)) as client:
        api = Api(client, any_store, settings, tokens)
        assert api.post("/api/v1/providers", json=SOURCE).status_code == 201
        assert api.post("/api/v1/providers", json=DESTINATION).status_code == 201
        inventory = api.get(f"/api/v1/providers/{SOURCE['id']}/inventory").json()
        vm_ids = [vm["source_id"] for vm in inventory[:6]]
        created = api.post(
            "/api/v1/plans",
            Role.approver,
            json={
                "name": "pg journey",
                "source_provider_id": SOURCE["id"],
                "destination_provider_id": DESTINATION["id"],
                "vm_ids": vm_ids,
            },
        )
        assert created.status_code == 201, created.text
        plan_id = created.json()["id"]
        report = api.post(f"/api/v1/plans/{plan_id}/validate", Role.operator)
        assert report.status_code == 200 and len(report.json()["migrations"]) == len(vm_ids)

        # JSON-path filters and paging run in SQL on both dialects
        page = api.get(f"/api/v1/migrations?plan_id={plan_id}&limit=2&offset=1").json()
        everything = api.get(f"/api/v1/migrations?plan_id={plan_id}").json()
        assert [m["id"] for m in page] == [m["id"] for m in everything[1:3]]
        phases = {m["phase"] for m in everything}
        for phase in phases:
            subset = api.get(f"/api/v1/migrations?plan_id={plan_id}&phase={phase}").json()
            assert subset and all(m["phase"] == phase for m in subset)
        assert api.get("/api/v1/migrations?plan_id=plan-nope").json() == []

        # the plan list filters by status and pages in SQL too
        assert [p["id"] for p in api.get("/api/v1/plans?status=validated").json()] == [plan_id]
        assert api.get("/api/v1/plans?status=running").json() == []
        assert api.get("/api/v1/plans?limit=1&offset=1").json() == []
        assert api.get("/api/v1/plans?status=nope").status_code == 422

        # change-stamp cache, stats and metrics
        stats = api.get(f"/api/v1/stats?plan_id={plan_id}").json()
        assert stats["total"] == len(vm_ids)
        assert api.get("/api/v1/metrics").status_code == 200
        assert any_store.change_stamp("migration")[0] == len(vm_ids)

        # event replay by sequence, bounded
        events = api.get("/api/v1/events?since=0&limit=1000").json()
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        later = api.get(f"/api/v1/events?since={seqs[len(seqs) // 2]}").json()
        assert all(e["seq"] > seqs[len(seqs) // 2] for e in later)
        assert api.get("/api/v1/events?since=99999999999999999999").status_code == 422
