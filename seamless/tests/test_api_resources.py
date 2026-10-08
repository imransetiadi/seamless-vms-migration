import time

import pytest
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.domain.enums import Phase, Role, Strategy, SyncPassKind
from seamless_migrate.domain.models import Estimate, Migration, Plan, SyncPass, utcnow
from seamless_migrate.store import Store
from tests.api_support import DESTINATION, SOURCE, Api, api_settings
from tests.factories import make_vm


@pytest.fixture
def api(tmp_path):
    settings, tokens = api_settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        api = Api(client, store, settings, tokens)
        assert api.post("/api/v1/providers", json=SOURCE).status_code == 201
        assert api.post("/api/v1/providers", json=DESTINATION).status_code == 201
        yield api
    store.dispose()


def wait_for(fn, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)


def test_provider_crud(api):
    providers = api.get("/api/v1/providers").json()
    assert [p["id"] for p in providers] == ["src-osp", "dst-rhoso"]
    # status fields sent by the client are ignored
    sneaky = {
        **SOURCE,
        "id": "src-two",
        "status": "ok",
        "status_message": "fine",
        "capabilities": {"admin": True},
    }
    created = api.post("/api/v1/providers", json=sneaky)
    assert created.status_code == 201
    assert created.json()["status"] == "unknown" and created.json()["capabilities"] == {}
    assert api.get("/api/v1/providers/src-two").json()["name"] == SOURCE["name"]
    bad_id = api.post("/api/v1/providers", json={**SOURCE, "id": "Bad_ID"})
    assert bad_id.status_code == 422

    checked = api.post("/api/v1/providers/src-osp/check", Role.operator)
    assert checked.status_code == 200
    assert checked.json()["status"] == "ok" and checked.json()["capabilities"]["admin"] is True
    assert checked.json()["last_checked_at"].endswith("Z")
    inventory = api.get("/api/v1/providers/src-osp/inventory").json()
    assert len(inventory) == 24 and {"source_id", "disk_bytes", "used_bytes"} <= set(inventory[0])
    dst_inventory = api.get("/api/v1/providers/dst-rhoso/inventory").json()
    assert set(dst_inventory) == {"networks", "flavors", "volume_types", "quotas", "projects"}

    # a provider used by a non-terminal plan cannot be deleted
    plan = api.post(
        "/api/v1/plans",
        Role.operator,
        json={
            "name": "P",
            "source_provider_id": "src-osp",
            "destination_provider_id": "dst-rhoso",
            "vm_ids": [],
        },
    ).json()
    assert (
        api.client.delete("/api/v1/providers/src-osp", headers=api.h(Role.admin)).status_code == 409
    )
    assert (
        api.client.delete("/api/v1/providers/src-two", headers=api.h(Role.admin)).status_code == 204
    )
    assert api.get("/api/v1/providers/src-two").status_code == 404
    kinds = api.kinds()
    assert kinds.count("provider.created") == 3 and "provider.deleted" in kinds
    assert "provider.checked" in kinds and "plan.created" in kinds
    assert plan["status"] == "draft"


def first_clean_vms(api, n=3):
    inventory = api.get("/api/v1/providers/src-osp/inventory").json()
    names = ["web-01", "web-02", "api-gw-01", "portal-01", "dns-01", "bastion-01"]
    return [vm["source_id"] for vm in inventory if vm["name"] in names][:n]


def test_plan_create_validate_start_flow(api):
    vm_ids = first_clean_vms(api)
    body = {
        "name": "Finance web tier",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": vm_ids,
        "default_strategy": "warm",
        "require_approval": False,
        "auto_cutover": True,
        "verification": {"timeout_s": 5},
    }
    # require_approval / auto_cutover / cutover_window are approver-only (SDD §12)
    refused = api.post("/api/v1/plans", Role.operator, json=body)
    assert refused.status_code == 403, refused.text
    assert "require_approval" in refused.json()["error"]["message"]
    assert api.kinds().count("auth.denied") >= 1
    created = api.post("/api/v1/plans", Role.approver, json=body)
    assert created.status_code == 201
    plan = created.json()
    assert plan["id"].startswith("plan-") and plan["status"] == "draft"
    # an operator may create a plan that keeps the policy defaults
    plain = api.post(
        "/api/v1/plans",
        Role.operator,
        json={k: v for k, v in body.items() if k not in ("require_approval", "auto_cutover")},
    )
    assert plain.status_code == 201 and plain.json()["require_approval"] is True
    # unknown estimator_overrides fields are a 400 (SDD §9.1)
    bogus = api.post(
        "/api/v1/plans",
        Role.operator,
        json={
            "name": "bad overrides",
            "source_provider_id": "src-osp",
            "destination_provider_id": "dst-rhoso",
            "vm_ids": vm_ids,
            "estimator_overrides": {"scan_bps": 1e9, "nope": 1},
        },
    )
    assert bogus.status_code == 400 and "nope" in bogus.json()["error"]["message"]
    zero = api.post(
        "/api/v1/plans",
        Role.operator,
        json={
            "name": "zero scan",
            "source_provider_id": "src-osp",
            "destination_provider_id": "dst-rhoso",
            "vm_ids": vm_ids,
            "estimator_overrides": {"scan_bps": 0},
        },
    )
    assert zero.status_code == 400 and "scan_bps" in zero.json()["error"]["message"]
    # an operator may spell out the policy defaults: only non-default values need an approver
    defaults = api.post(
        "/api/v1/plans",
        Role.operator,
        json={
            "name": "defaults spelled out",
            "source_provider_id": "src-osp",
            "destination_provider_id": "dst-rhoso",
            "vm_ids": vm_ids,
            "require_approval": True,
            "auto_cutover": False,
            "cutover_window": None,
        },
    )
    assert defaults.status_code == 201, defaults.text

    patched = api.client.patch(
        f"/api/v1/plans/{plan['id']}",
        headers=api.h(Role.operator),
        json={"description": "pilot", "downtime_slo_s": 600},
    )
    assert patched.status_code == 200 and patched.json()["downtime_slo_s"] == 600
    policy = api.client.patch(
        f"/api/v1/plans/{plan['id']}", headers=api.h(Role.operator), json={"auto_cutover": False}
    )
    assert policy.status_code == 403
    policy = api.client.patch(
        f"/api/v1/plans/{plan['id']}",
        headers=api.h(Role.approver),
        json={"cutover_window": None, "estimator_overrides": {"parallel_disks": 2}},
    )
    assert policy.status_code == 200 and policy.json()["estimator_overrides"] == {
        "parallel_disks": 2
    }
    overrides = api.client.patch(
        f"/api/v1/plans/{plan['id']}",
        headers=api.h(Role.operator),
        json={"estimator_overrides": {"Boot_S": 1}},
    )
    assert overrides.status_code == 400 and "Boot_S" in overrides.json()["error"]["message"]
    unknown_field = api.client.patch(
        f"/api/v1/plans/{plan['id']}", headers=api.h(Role.operator), json={"status": "running"}
    )
    assert unknown_field.status_code == 422

    waves = api.post(
        f"/api/v1/plans/{plan['id']}/waves/auto", Role.operator, json={"max_wave_size": 2}
    )
    assert waves.status_code == 200 and waves.json()["waves"][0]["name"] == "Pilot"

    report = api.post(f"/api/v1/plans/{plan['id']}/validate", Role.operator)
    assert report.status_code == 200
    assert report.json()["ok"] is True and len(report.json()["migrations"]) == 3
    assert api.get(f"/api/v1/plans/{plan['id']}").json()["status"] == "validated"
    # PATCH is refused once the plan runs
    started = api.post(f"/api/v1/plans/{plan['id']}/start", Role.operator)
    assert started.status_code == 200 and started.json()["status"] == "running"
    refused = api.client.patch(
        f"/api/v1/plans/{plan['id']}", headers=api.h(Role.operator), json={"description": "late"}
    )
    assert refused.status_code == 409

    def all_completed():
        migs = api.get(f"/api/v1/migrations?plan_id={plan['id']}").json()
        return migs if migs and all(m["phase"] == "completed" for m in migs) else None

    migs = wait_for(all_completed, timeout=20)
    assert all(m["actual_downtime_s"] is not None for m in migs)
    wait_for(lambda: api.get(f"/api/v1/plans/{plan['id']}").json()["status"] == "completed")
    phase_filter = api.get(f"/api/v1/migrations?plan_id={plan['id']}&phase=completed").json()
    assert len(phase_filter) == 3
    page = api.get(f"/api/v1/migrations?plan_id={plan['id']}&limit=2&offset=1").json()
    assert [m["id"] for m in page] == [m["id"] for m in migs[1:3]]
    assert api.get(f"/api/v1/migrations?plan_id={plan['id']}&offset=3").json() == []
    assert api.get("/api/v1/migrations?limit=0").status_code == 422
    assert api.get("/api/v1/migrations?phase=failed").json() == []
    stats = api.get(f"/api/v1/stats?plan_id={plan['id']}").json()
    assert stats["total"] == 3 and stats["completed"] == 3 and stats["avg_downtime_s"] is not None
    assert stats["slo_compliance_pct"] == 100.0


def seed_migrations(store: Store) -> dict[str, Migration]:
    plan = Plan(
        id="plan-0000feed",
        name="Seeded",
        source_provider_id="src-osp",
        destination_provider_id="dst-rhoso",
        vm_ids=["vm-a"],
        status="validated",
    )
    store.put("plan", plan)
    estimates = [
        Estimate(
            strategy=s,
            eligible=s != Strategy.storage_handover,
            precopy_s=0,
            passes=0,
            downtime_s=100,
            total_s=100,
            final_delta_bytes=0,
            meets_slo=True,
        )
        for s in (Strategy.cold, Strategy.warm, Strategy.storage_handover)
    ]
    now = utcnow()
    one_pass = [
        SyncPass(
            number=1,
            kind=SyncPassKind.full,
            started_at=now,
            ended_at=now,
            bytes_scanned=10,
            bytes_changed=10,
            bytes_transferred=10,
            duration_s=100.0,
        )
    ]
    out = {}
    specs = {
        "ready": dict(phase=Phase.ready, strategy=Strategy.cold),
        "awaiting": dict(
            phase=Phase.awaiting_cutover, strategy=Strategy.warm, sync_passes=one_pass
        ),
        "completed": dict(
            phase=Phase.completed,
            strategy=Strategy.cold,
            downtime_started_at=now,
            downtime_ended_at=now,
            actual_downtime_s=0.0,
            destination_server_id="sim-x",
        ),
        "failed": dict(phase=Phase.failed, strategy=Strategy.cold, error="boom"),
    }
    for i, (key, spec) in enumerate(specs.items()):
        m = Migration(
            id=f"mig-00000000{i:02d}",
            plan_id=plan.id,
            estimates=estimates,
            vm=make_vm(source_id=f"vm-{key}", name=f"vm-{key}"),
            **spec,
        )
        store.put("migration", m)
        out[key] = m
    return out


def test_migration_actions_transitions(api):
    migs = seed_migrations(api.store)
    base = "/api/v1/migrations"
    ready, awaiting = migs["ready"].id, migs["awaiting"].id

    res = api.client.put(
        f"{base}/{ready}/strategy", headers=api.h(Role.operator), json={"strategy": "warm"}
    )
    assert res.status_code == 200 and res.json()["strategy"] == "warm"
    res = api.client.put(
        f"{base}/{ready}/strategy",
        headers=api.h(Role.operator),
        json={"strategy": "storage_handover"},
    )
    assert res.status_code == 400
    res = api.post(f"{base}/{ready}/approve", Role.approver, json={"comment": "CHG-1"})
    assert res.status_code == 200 and res.json()["approvals"][-1]["actor"] == "approver-user"
    res = api.post(f"{base}/{ready}/cutover", Role.approver, json={"force_window": True})
    assert res.status_code == 200 and res.json()["cutover_requested"] is True
    res = api.post(f"{base}/{ready}/sync", Role.operator)
    assert res.status_code == 409  # only awaiting_cutover
    res = api.post(f"{base}/{ready}/cancel", Role.operator, json={"reason": "descoped"})
    assert res.status_code == 200 and res.json()["phase"] == "cancelled"

    res = api.post(f"{base}/{awaiting}/sync", Role.operator)
    assert res.status_code == 200 and res.json()["phase"] == "syncing"
    wait_for(lambda: api.get(f"{base}/{awaiting}").json()["phase"] == "awaiting_cutover")
    assert len(api.get(f"{base}/{awaiting}").json()["sync_passes"]) == 2

    failed = migs["failed"].id
    res = api.post(f"{base}/{failed}/retry", Role.operator)
    assert res.status_code == 200 and res.json()["phase"] == "ready"
    assert res.json()["attempts"] == 1 and res.json()["error"] is None

    completed = migs["completed"].id
    res = api.post(f"{base}/{completed}/rollback", Role.operator, json={"reason": "app broken"})
    assert res.status_code == 200 and res.json()["phase"] == "rolling_back"
    wait_for(lambda: api.get(f"{base}/{completed}").json()["phase"] == "rolled_back")
    res = api.post(f"{base}/{completed}/rollback", Role.operator, json={"reason": "again"})
    assert res.status_code == 409 and res.json()["error"]["code"] == "conflict"
    res = api.post(f"{base}/{completed}/rollback", Role.operator, json={})
    assert res.status_code == 422  # reason is required

    kinds = api.kinds()
    for kind in ("migration.action", "migration.approved", "migration.phase"):
        assert kind in kinds
    actions = [e for e in api.store.events(since_seq=0) if e.kind == "migration.action"]
    assert {e.data["action"] for e in actions} >= {
        "strategy",
        "cutover",
        "cancel",
        "sync",
        "retry",
        "rollback",
    }
    assert all(e.actor.endswith("-user") for e in actions)


def test_finalize_confirm_mismatch_400(api):
    migs = seed_migrations(api.store)
    mid = migs["completed"].id
    res = api.post(
        f"/api/v1/migrations/{mid}/finalize", Role.approver, json={"confirm": "VM-COMPLETED"}
    )
    assert res.status_code == 400 and res.json()["error"]["code"] == "bad_request"
    res = api.post(
        f"/api/v1/migrations/{mid}/finalize", Role.operator, json={"confirm": "vm-completed"}
    )
    assert res.status_code == 403
    res = api.post(
        f"/api/v1/migrations/{mid}/finalize",
        Role.approver,
        json={"confirm": "vm-completed", "delete_source": True},
    )
    assert res.status_code == 200 and res.json()["phase"] == "finalized"


def test_events_since(api):
    seed_migrations(api.store)
    events = api.get("/api/v1/events").json()
    assert events and all(
        {"seq", "ts", "kind", "actor", "message", "data"} <= set(e) for e in events
    )
    last = events[-1]["seq"]
    assert api.get(f"/api/v1/events?since={last}").json() == []
    api.post("/api/v1/migrations/mig-0000000000/approve", Role.approver, json={})
    newer = api.get(f"/api/v1/events?since={last}").json()
    assert [e["kind"] for e in newer] == ["migration.approved"]
    filtered = api.get("/api/v1/events?migration_id=mig-0000000000").json()
    assert all(e["migration_id"] == "mig-0000000000" for e in filtered)
    assert len(api.get("/api/v1/events?limit=1").json()) == 1
    assert api.get("/api/v1/events?limit=1001").status_code == 422
