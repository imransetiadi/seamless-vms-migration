import asyncio
import inspect
import json
import time

import fastapi.routing
import httpx
import pytest
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.domain.enums import Phase, PlanStatus, Role, Strategy, SyncPassKind
from seamless_migrate.domain.models import Estimate, Migration, Plan, Provider, SyncPass, utcnow
from seamless_migrate.store import AsyncStore, Store
from tests.api_support import DESTINATION, SOURCE, Api, api_settings, write_tokens
from tests.factories import make_plan, make_vm
from tests.orch_support import build_harness, make_settings


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


def test_plan_rejects_duplicate_vm_ids(api):
    """SDD §12: one VM, one migration — a repeated VM id would make two migrations cut it over."""
    vm_ids = first_clean_vms(api, n=2)
    body = {
        "name": "twice",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": [vm_ids[0], vm_ids[1], vm_ids[0]],
    }
    twice = api.post("/api/v1/plans", Role.operator, json=body)
    assert twice.status_code == 422, twice.text
    assert vm_ids[0] in twice.json()["error"]["message"]
    created = api.post("/api/v1/plans", Role.operator, json={**body, "vm_ids": vm_ids})
    assert created.status_code == 201, created.text
    patched = api.client.patch(
        f"/api/v1/plans/{created.json()['id']}",
        headers=api.h(Role.operator),
        json={"vm_ids": [vm_ids[1], vm_ids[1]]},
    )
    assert patched.status_code == 422, patched.text
    assert vm_ids[1] in patched.json()["error"]["message"]


def test_plan_takes_an_empty_vm_ids_and_refuses_a_missing_one(api):
    """SDD §12: vm_ids is required and must not repeat a VM; an empty list is a draft plan to fill
    later. The dashboard's mock follows the same contract."""
    body = {
        "name": "later",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
    }
    empty = api.post("/api/v1/plans", Role.operator, json={**body, "vm_ids": []})
    assert empty.status_code == 201, empty.text
    assert empty.json()["vm_ids"] == [] and empty.json()["status"] == "draft"
    missing = api.post("/api/v1/plans", Role.operator, json=body)
    assert missing.status_code == 422
    error = missing.json()["error"]
    assert error == {"code": "validation_error", "message": "vm_ids: Field required"}


def test_plan_rejects_out_of_range_tcp_port(api):
    """SDD §12: verification settings that would roll back a good cutover or never open the gate
    are a 422 on create and on edit."""
    vm_ids = first_clean_vms(api, n=1)
    base = {
        "name": "bad settings",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": vm_ids,
    }
    bad = [
        ({"verification": {"tcp_ports": [22, 70000]}}, "70000"),
        ({"verification": {"windows_tcp_ports": [0]}}, "windows_tcp_ports"),
        ({"verification": {"timeout_s": -1}}, "timeout_s"),
    ]
    for extra, needle in bad:
        refused = api.post("/api/v1/plans", Role.operator, json={**base, **extra})
        assert refused.status_code == 422, (extra, refused.text)
        assert needle in refused.json()["error"]["message"]
    window = {"start": "2026-10-10T22:00:00Z", "end": "2026-10-10T20:00:00Z"}
    backwards = api.post("/api/v1/plans", Role.approver, json={**base, "cutover_window": window})
    assert backwards.status_code == 422 and "cutover_window" in backwards.json()["error"]["message"]
    created = api.post("/api/v1/plans", Role.operator, json=base)
    assert created.status_code == 201, created.text
    patched = api.client.patch(
        f"/api/v1/plans/{created.json()['id']}",
        headers=api.h(Role.operator),
        json={"verification": {"tcp_ports": [65536]}},
    )
    assert patched.status_code == 422 and "65536" in patched.json()["error"]["message"]


def test_plan_takes_application_probes_and_refuses_bad_ones(api):
    """SDD §7.5, §12 (ADV-18): application probes for every VM and per VM, checked like ports."""
    vm_ids = first_clean_vms(api, n=1)
    base = {
        "name": "probes",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": vm_ids,
    }
    bad = [
        ({"verification": {"app_probes": [{"kind": "tcp", "port": 70000}]}}, "70000"),
        ({"verification": {"app_probes": [{"kind": "http", "port": 80, "path": "x"}]}}, "path"),
        (
            {
                "probe_overrides": {
                    vm_ids[0]: [{"kind": "https", "port": 443, "expect_status": 700}]
                }
            },
            "expect_status",
        ),
        ({"verification": {"app_probes": [{"kind": "ftp", "port": 21}]}}, "kind"),
    ]
    for extra, needle in bad:
        refused = api.post("/api/v1/plans", Role.operator, json={**base, **extra})
        assert refused.status_code == 422, (extra, refused.text)
        assert needle in refused.json()["error"]["message"]
    probes = {
        "verification": {"app_probes": [{"kind": "http", "port": 80}]},
        "probe_overrides": {vm_ids[0]: [{"kind": "tcp", "port": 5432}]},
    }
    created = api.post("/api/v1/plans", Role.operator, json={**base, **probes})
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["verification"]["app_probes"] == [
        {"kind": "http", "port": 80, "path": "/", "expect_status": None}
    ]
    assert plan["probe_overrides"] == {
        vm_ids[0]: [{"kind": "tcp", "port": 5432, "path": "/", "expect_status": None}]
    }
    patched = api.client.patch(
        f"/api/v1/plans/{plan['id']}",
        headers=api.h(Role.operator),
        json={"probe_overrides": {vm_ids[0]: []}},
    )
    assert patched.status_code == 200 and patched.json()["probe_overrides"] == {vm_ids[0]: []}


def test_a_migration_reports_its_verification_and_suggested_probes(api):
    """SDD §4.2 (ADV-18): Migration.verification (null before one) and suggested_probes."""
    vm_ids = first_clean_vms(api, n=1)
    created = api.post(
        "/api/v1/plans",
        Role.operator,
        json={
            "name": "suggest",
            "source_provider_id": "src-osp",
            "destination_provider_id": "dst-rhoso",
            "vm_ids": vm_ids,
        },
    )
    assert created.status_code == 201, created.text
    assert (
        api.post(f"/api/v1/plans/{created.json()['id']}/validate", Role.operator).status_code == 200
    )
    [m] = api.get(f"/api/v1/migrations?plan_id={created.json()['id']}", Role.viewer).json()
    assert m["verification"] is None
    assert isinstance(m["suggested_probes"], list)


def test_plans_carry_their_derived_status_and_finalize_reminder(api):
    """SDD §4.2, §8.1 (ADV-19): every plan route returns derived_status; the reminder is ≥ 1 h."""
    vm_ids = first_clean_vms(api, n=1)
    base = {
        "name": "status",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": vm_ids,
    }
    refused = api.post("/api/v1/plans", Role.operator, json={**base, "finalize_warning_h": 0})
    assert refused.status_code == 422 and "finalize_warning_h" in refused.json()["error"]["message"]
    created = api.post("/api/v1/plans", Role.operator, json=base)
    assert created.status_code == 201, created.text
    plan = created.json()
    assert (plan["derived_status"], plan["finalize_warning_h"]) == ("draft", 72)
    assert api.get(f"/api/v1/plans/{plan['id']}", Role.viewer).json()["derived_status"] == "draft"
    listed = {p["id"]: p for p in api.get("/api/v1/plans", Role.viewer).json()}
    assert listed[plan["id"]]["derived_status"] == "draft"
    assert api.post(f"/api/v1/plans/{plan['id']}/validate", Role.operator).status_code == 200
    assert (
        api.get(f"/api/v1/plans/{plan['id']}", Role.viewer).json()["derived_status"] == "validated"
    )
    # the stored lifecycle says validated; its migration completed: the plan reads completed
    [m] = [x for x in api.store.list("migration", Migration) if x.plan_id == plan["id"]]
    api.store.put("migration", m.model_copy(update={"phase": Phase.completed}))
    got = api.get(f"/api/v1/plans/{plan['id']}", Role.viewer).json()
    assert (got["status"], got["derived_status"]) == ("validated", "completed")
    # derived_status is the API's: a client cannot set it
    patched = api.client.patch(
        f"/api/v1/plans/{plan['id']}",
        headers=api.h(Role.operator),
        json={"derived_status": "finalized"},
    )
    assert patched.status_code == 422


def test_the_migration_list_is_validated_once_and_sent_as_before(api, monkeypatch):
    """SDD §12: GET /migrations sends what the store validated, without FastAPI dumping it,
    validating it again and serializing it (185 ms CPU for 1,000 migrations)."""
    vm_ids = first_clean_vms(api, n=2)
    created = api.post(
        "/api/v1/plans",
        Role.operator,
        json={
            "name": "list",
            "source_provider_id": "src-osp",
            "destination_provider_id": "dst-rhoso",
            "vm_ids": vm_ids,
        },
    )
    plan_id = created.json()["id"]
    assert api.post(f"/api/v1/plans/{plan_id}/validate", Role.operator).status_code == 200
    expected = [m.model_dump(mode="json") for m in api.store.list("migration", Migration)]
    expected_plan = [m for m in expected if m["plan_id"] == plan_id]

    validated_again = []
    real_serialize = fastapi.routing.serialize_response

    async def counting_serialize(*args, **kwargs):
        validated_again.append(kwargs.get("field"))
        return await real_serialize(*args, **kwargs)

    monkeypatch.setattr(fastapi.routing, "serialize_response", counting_serialize)
    listed = api.get(f"/api/v1/migrations?plan_id={plan_id}", Role.viewer)
    everything = api.get("/api/v1/migrations", Role.viewer)
    assert listed.headers["content-type"] == "application/json"
    assert (listed.json(), everything.json()) == (expected_plan, expected)
    assert validated_again == []


def test_plan_routes_derive_the_status_without_loading_migrations(api, monkeypatch):
    """SDD §8.1, §11: GET /plans and /plans/{id} take the phases from Store.plan_phases."""
    vm_ids = first_clean_vms(api, n=1)
    created = api.post(
        "/api/v1/plans",
        Role.operator,
        json={
            "name": "phases",
            "source_provider_id": "src-osp",
            "destination_provider_id": "dst-rhoso",
            "vm_ids": vm_ids,
        },
    )
    plan_id = created.json()["id"]
    assert api.post(f"/api/v1/plans/{plan_id}/validate", Role.operator).status_code == 200
    [m] = [x for x in api.store.list("migration", Migration) if x.plan_id == plan_id]
    api.store.put("migration", m.model_copy(update={"phase": Phase.completed}))

    # what the plan routes list (the orchestrator's tick, in the same app, lists migrations too)
    listed_kinds: list[str] = []
    real_list = AsyncStore.list

    async def counting_list(self, kind, *args, **kwargs):
        if any(f.filename.endswith("routes_plans.py") for f in inspect.stack(0)):
            listed_kinds.append(kind)
        return await real_list(self, kind, *args, **kwargs)

    monkeypatch.setattr(AsyncStore, "list", counting_list)
    one = api.get(f"/api/v1/plans/{plan_id}", Role.viewer).json()
    many = {p["id"]: p for p in api.get("/api/v1/plans", Role.viewer).json()}
    assert one["derived_status"] == many[plan_id]["derived_status"] == "completed"
    assert "migration" not in listed_kinds


def test_plan_finalize_route_needs_an_approver_and_the_scope_named(api):
    """SDD §8.1, §12: POST /plans/{id}/finalize — 403 below approver, 400 for a wrong name,
    404 for an unknown wave, 409 when nothing in scope is completed."""
    vm_ids = first_clean_vms(api, n=1)
    created = api.post(
        "/api/v1/plans",
        Role.operator,
        json={
            "name": "to finalize",
            "source_provider_id": "src-osp",
            "destination_provider_id": "dst-rhoso",
            "vm_ids": vm_ids,
        },
    )
    plan_id = created.json()["id"]
    url = f"/api/v1/plans/{plan_id}/finalize"
    assert api.post(url, Role.operator, json={"confirm": "to finalize"}).status_code == 403
    assert api.post(url, Role.approver, json={"confirm": "wrong"}).status_code == 400
    assert (
        api.post(url, Role.approver, json={"confirm": "Pilot", "wave_id": "wave-9"}).status_code
        == 404
    )
    nothing = api.post(url, Role.approver, json={"confirm": "to finalize"})
    assert (
        nothing.status_code == 409
        and "no completed migration" in nothing.json()["error"]["message"]
    )


def test_the_demo_power_route_sets_a_fake_source_vms_power_and_exists_only_in_demo_mode(
    api, tmp_path
):
    """SDD §8.1, §12: POST /demo/vms/{source_id}/power — operator, demo mode only."""
    [vm] = api.get("/api/v1/providers/src-osp/inventory", Role.viewer).json()[:1]
    url = f"/api/v1/demo/vms/{vm['source_id']}/power"
    assert api.post(url, Role.viewer, json={"state": "on"}).status_code == 403
    off = api.post(url, Role.operator, json={"state": "off"})
    assert off.status_code == 200 and off.json()["power_state"] == "stopped"
    inventory = {
        v["source_id"]: v
        for v in api.get("/api/v1/providers/src-osp/inventory", Role.viewer).json()
    }
    assert inventory[vm["source_id"]]["power_state"] == "stopped"
    assert api.post(url, Role.operator, json={"state": "on"}).json()["power_state"] == "running"
    assert api.post(url, Role.operator, json={"state": "sideways"}).status_code == 422
    assert (
        api.post("/api/v1/demo/vms/os-nope/power", Role.operator, json={"state": "on"}).status_code
        == 404
    )

    settings, tokens = api_settings(tmp_path / "real", demo=False)
    store = Store(f"sqlite:///{tmp_path / 'real.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        real = Api(client, store, settings, tokens)
        refused = real.post(url, Role.operator, json={"state": "on"})
        assert refused.status_code == 404
        assert refused.json()["error"]["message"] == "no such route outside demo mode"
    store.dispose()


def test_patch_plan_conflicts_while_a_migration_is_in_flight(api):
    """SDD §12: a pre-copied migration waiting for its cutover (here after pause and re-validation)
    would cut over to a changed destination or mappings — PATCH and auto-waves answer 409, naming
    the VM; once nothing is in flight the plan can be edited again."""
    vm_ids = first_clean_vms(api, n=2)
    body = {
        "name": "in flight",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": vm_ids,
    }
    plan = api.post("/api/v1/plans", Role.operator, json=body).json()
    assert api.post(f"/api/v1/plans/{plan['id']}/validate", Role.operator).status_code == 200
    migrations = api.store.list("migration", Migration, plan_id=plan["id"])
    waiting = migrations[0]
    waiting.phase = Phase.awaiting_cutover
    api.store.put("migration", waiting)
    patch = {"description": "move to another cloud"}
    refused = api.client.patch(
        f"/api/v1/plans/{plan['id']}", headers=api.h(Role.operator), json=patch
    )
    assert refused.status_code == 409, refused.text
    assert waiting.vm.name in refused.json()["error"]["message"]
    waves = api.post(
        f"/api/v1/plans/{plan['id']}/waves/auto", Role.operator, json={"max_wave_size": 5}
    )
    assert waves.status_code == 409 and waiting.vm.name in waves.json()["error"]["message"]
    # a failed migration whose source runs stays editable: fix the cause, then retry
    waiting.phase = Phase.failed
    api.store.put("migration", waiting)
    allowed = api.client.patch(
        f"/api/v1/plans/{plan['id']}", headers=api.h(Role.operator), json=patch
    )
    assert allowed.status_code == 200, allowed.text


def test_a_plans_providers_cannot_change_while_one_of_its_migrations_is_failed(api):
    """SDD §12: a failed migration whose source runs keeps its plan editable — to fix the cause
    before a retry — but not its providers: its rollback acts on the clouds its attempt used (the
    cold one matches the destination server by name), so a changed provider is refused until it
    is rolled back or cancelled."""
    vm_ids = first_clean_vms(api, n=2)
    body = {
        "name": "switch clouds",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": vm_ids,
    }
    plan = api.post("/api/v1/plans", Role.operator, json=body).json()
    assert api.post(f"/api/v1/plans/{plan['id']}/validate", Role.operator).status_code == 200
    for kept, copy in (("dst-rhoso", "dst-rhoso-2"), ("src-osp", "src-osp-2")):
        other = api.store.get("provider", kept, Provider).model_copy(update={"id": copy})
        api.store.put("provider", other, expected_version=0)
    failed = api.store.list("migration", Migration, plan_id=plan["id"])[0]
    failed.phase = Phase.failed  # its pre-copy failed; the source VM kept running
    api.store.put("migration", failed)
    url = f"/api/v1/plans/{plan['id']}"

    def edit(change: dict):
        return api.client.patch(url, headers=api.h(Role.operator), json=change)

    refused = edit({"destination_provider_id": "dst-rhoso-2"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["message"] == (
        f"roll back or cancel the failed migration ({failed.vm.name}) before changing the plan's "
        "providers: its rollback acts on the clouds its attempt used"
    )
    assert edit({"source_provider_id": "src-osp-2"}).status_code == 409  # either side
    assert edit({"description": "fix the network mapping"}).status_code == 200  # for the retry
    assert edit({"destination_provider_id": "dst-rhoso"}).status_code == 200  # not a change
    failed = api.store.get("migration", failed.id, Migration)
    failed.phase = Phase.rolled_back
    api.store.put("migration", failed)
    moved = edit({"destination_provider_id": "dst-rhoso-2"})
    assert moved.status_code == 200, moved.text


def test_the_in_flight_refusal_says_what_releases_each_vm(api):
    """SDD §12: a completed migration is finished and cannot be cancelled, nor can a failed one
    whose cutover stopped the source — the 409 of an edit or re-wave names each VM with what
    releases it."""
    vm_ids = first_clean_vms(api, n=2)
    body = {
        "name": "finished",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": vm_ids,
    }
    plan = api.post("/api/v1/plans", Role.operator, json=body).json()
    assert api.post(f"/api/v1/plans/{plan['id']}/validate", Role.operator).status_code == 200
    done, stopped = api.store.list("migration", Migration, plan_id=plan["id"])
    done.phase = Phase.completed
    stopped.phase = Phase.failed
    stopped.downtime_started_at = utcnow()
    for m in (done, stopped):
        api.store.put("migration", m)
    clauses = (
        f"roll back or retry the failed migration whose source VM is stopped ({stopped.vm.name}); "
        f"finalize or roll back the completed migration ({done.vm.name})"
    )
    refused = api.client.patch(
        f"/api/v1/plans/{plan['id']}", headers=api.h(Role.operator), json={"description": "x"}
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["message"] == f"{clauses} before editing the plan"
    waves = api.post(
        f"/api/v1/plans/{plan['id']}/waves/auto", Role.operator, json={"max_wave_size": 5}
    )
    assert waves.status_code == 409
    assert waves.json()["error"]["message"] == f"{clauses} before re-planning the waves"


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
    # an operator may re-send a policy field at its current value (as the defaults on POST),
    # but not change it
    current = api.get(f"/api/v1/plans/{plan['id']}").json()["auto_cutover"]
    policy = api.client.patch(
        f"/api/v1/plans/{plan['id']}", headers=api.h(Role.operator), json={"auto_cutover": current}
    )
    assert policy.status_code == 200, policy.text
    policy = api.client.patch(
        f"/api/v1/plans/{plan['id']}",
        headers=api.h(Role.operator),
        json={"auto_cutover": not current},
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


def test_plan_refuses_non_finite_estimator_inputs(api):
    """SDD §9.1: the JSON parser accepts the NaN and Infinity literals; plans must not."""
    vm_ids = first_clean_vms(api)

    def create(extra: str):
        body = (
            '{"name": "non-finite", "source_provider_id": "src-osp", '
            f'"destination_provider_id": "dst-rhoso", "vm_ids": {json.dumps(vm_ids)}, {extra}}}'
        )
        headers = {**api.h(Role.operator), "Content-Type": "application/json"}
        return api.client.post("/api/v1/plans", headers=headers, content=body)

    for literal in ("NaN", "Infinity"):
        res = create(f'"estimator_overrides": {{"scan_bps": {literal}}}')
        assert res.status_code == 400, res.text
        assert "scan_bps" in res.json()["error"]["message"]
        res = create(f'"link_bps": {literal}')
        assert res.status_code == 422, res.text
        assert "link_bps" in res.json()["error"]["message"]
    created = create('"link_bps": 1e9')
    assert created.status_code == 201, created.text
    # PATCH merges into the stored plan: the same check applies
    patched = api.client.patch(
        f"/api/v1/plans/{created.json()['id']}",
        headers={**api.h(Role.operator), "Content-Type": "application/json"},
        content='{"link_bps": Infinity}',
    )
    assert patched.status_code == 422 and "link_bps" in patched.json()["error"]["message"]


@pytest.mark.parametrize(
    ("change", "status", "message"),
    [
        ({"name": "x" * 201}, 422, "name: at most 200 characters (got 201)"),
        ({"vm_ids": ["vm-1", "vm-1"]}, 422, "vm_ids lists a VM more than once: vm-1"),
        (
            {"cutover_window": {"start": "2026-10-11T02:00:00Z", "end": "2026-10-11T01:00:00Z"}},
            422,
            "cutover_window: end must be after start",
        ),
        (
            {"estimator_overrides": {"nope": 1.0}},
            400,
            "invalid estimator_overrides: nope: unknown field",
        ),
    ],
    ids=["name", "vm_ids", "settings", "estimator_overrides"],
)
def test_plan_checks_answer_like_plan_apply(api, change, status, message):
    """SDD §12/§15: POST /plans and `seamless plan apply` run one set of checks with one wording
    (test_plan_apply_refuses_what_the_api_refuses holds the CLI to the same messages)."""
    body = {
        "name": "checked",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": ["vm-1"],
        "require_approval": True,
    }
    refused = api.post("/api/v1/plans", Role.approver, json={**body, **change})
    assert refused.status_code == status, refused.text
    assert refused.json()["error"]["message"] == message


def test_plan_texts_are_bounded_at_the_api_boundary(api):
    """SDD §12: a plan name holds 200 characters, a description 2000; stored plans still load."""
    vm_ids = first_clean_vms(api)
    body = {
        "name": "x" * 200,
        "description": "d" * 2000,
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": vm_ids,
    }
    created = api.post("/api/v1/plans", Role.operator, json=body)
    assert created.status_code == 201, created.text
    for field, value in (("name", "x" * 201), ("description", "d" * 2001)):
        res = api.post("/api/v1/plans", Role.operator, json={**body, field: value})
        assert res.status_code == 422, res.text
        assert field in res.json()["error"]["message"]
        url = f"/api/v1/plans/{created.json()['id']}"
        patched = api.client.patch(url, headers=api.h(Role.operator), json={field: value})
        assert patched.status_code == 422, patched.text
    # a plan stored before the check keeps loading
    stored = api.store.get("plan", created.json()["id"], Plan)
    stored.name = "n" * 5000
    api.store.put("plan", stored)
    assert api.get(f"/api/v1/plans/{stored.id}").json()["name"] == "n" * 5000


def test_action_texts_hold_at_most_2000_characters(api):
    """SDD §12: comment, reason and confirm are bounded; 2000 characters still pass."""
    migs = seed_migrations(api.store)
    too_long = "x" * 2001
    for action, role, body in (
        ("approve", Role.approver, {"comment": too_long}),
        ("cutover", Role.approver, {"comment": too_long}),
        ("rollback", Role.operator, {"reason": too_long}),
        ("cancel", Role.operator, {"reason": too_long}),
        ("finalize", Role.approver, {"confirm": too_long}),
    ):
        res = api.post(f"/api/v1/migrations/{migs['awaiting'].id}/{action}", role, json=body)
        assert res.status_code == 422, (action, res.text)
        assert res.json()["error"]["code"] == "validation_error"
    approve = f"/api/v1/migrations/{migs['awaiting'].id}/approve"
    ok = api.post(approve, Role.approver, json={"comment": "x" * 2000})
    assert ok.status_code == 200, ok.text


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


def test_events_tail_returns_the_newest_limit_events_ascending(api):
    """SDD §12: ``GET /events?tail=true`` is the latest history in one request."""
    seed_migrations(api.store)
    api.post("/api/v1/migrations/mig-0000000000/approve", Role.approver, json={})
    every = api.get("/api/v1/events?limit=1000").json()
    assert len(every) > 2

    tail = api.get("/api/v1/events?tail=true&limit=2").json()
    assert [e["seq"] for e in tail] == [e["seq"] for e in every[-2:]]
    assert tail[-1]["kind"] == "migration.approved"
    mine = api.get("/api/v1/events?tail=true&limit=1&migration_id=mig-0000000000").json()
    assert [e["kind"] for e in mine] == ["migration.approved"]
    assert api.get(f"/api/v1/events?tail=true&since={every[-1]['seq']}").json() == []
    # without tail the first page is unchanged: the oldest events after since
    assert api.get("/api/v1/events?limit=2").json() == every[:2]


def test_events_before_reads_the_page_before_the_oldest_shown(api):
    """SDD §12, §16: Load older events asks for the newest page below the oldest event shown."""
    seed_migrations(api.store)
    api.post("/api/v1/migrations/mig-0000000000/approve", Role.approver, json={})
    every = api.get("/api/v1/events?limit=1000").json()
    assert len(every) >= 3
    newest = api.get("/api/v1/events?tail=true&limit=1").json()
    older = api.get(f"/api/v1/events?tail=true&limit=1&before={newest[0]['seq']}").json()
    assert [e["seq"] for e in newest + older] == [every[-1]["seq"], every[-2]["seq"]]
    assert api.get("/api/v1/events?tail=true&before=0").status_code == 422


def test_auto_waves_takes_a_wave_size_from_1_to_1000(api):
    """SDD §12: max_wave_size is 1-1000; outside it the API answers 422 validation_error, the
    shape the dashboard mock copies."""
    body = {
        "name": "Wave sizes",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": first_clean_vms(api),
    }
    plan = api.post("/api/v1/plans", Role.operator, json=body).json()
    url = f"/api/v1/plans/{plan['id']}/waves/auto"
    for size, bound in ((0, "greater than or equal to 1"), (1001, "less than or equal to 1000")):
        refused = api.post(url, Role.operator, json={"max_wave_size": size})
        assert refused.status_code == 422, refused.text
        error = refused.json()["error"]
        assert error["code"] == "validation_error"
        assert error["message"] == f"max_wave_size: Input should be {bound}"
    planned = api.post(url, Role.operator, json={"max_wave_size": 1000})
    assert planned.status_code == 200, planned.text


def test_provider_failures_while_planning_answer_502_redacted(api, monkeypatch):
    """SDD §12: a provider failure answers 502 provider_error with the redacted message — also
    while validating (inventory) or planning waves (get_vm), never a 400 carrying the SDK's raw
    text, which may hold endpoint URLs or credentials."""
    from seamless_migrate.providers.base import ProviderError

    body = {
        "name": "Provider down",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": first_clean_vms(api),
    }
    plan = api.post("/api/v1/plans", Role.operator, json=body).json()
    secret = "auth failed OS_PASSWORD=hunter2 at https://k:5000"

    class Broken:
        async def check(self):
            return {}

        async def inventory(self):
            raise ProviderError(f"src-osp: {secret}")

        async def get_vm(self, vm_id):
            raise ProviderError(f"src-osp: {secret}")

    monkeypatch.setattr(api.client.app.state.services.providers, "get", lambda p: Broken())
    calls = (
        (f"/api/v1/plans/{plan['id']}/validate", None),
        (f"/api/v1/plans/{plan['id']}/waves/auto", {"max_wave_size": 5}),
    )
    for path, payload in calls:
        res = api.post(path, Role.operator, json=payload)
        assert res.status_code == 502, (path, res.text)
        assert res.json()["error"]["code"] == "provider_error"
        assert "hunter2" not in res.text and "auth failed" in res.text


def test_a_failed_plan_keeps_its_providers_from_deletion(api):
    """SDD §12/§8: a failed plan (pre-staging failed) can be started again, so its providers
    cannot be deleted under it; a completed plan releases them."""
    from seamless_migrate.domain.enums import PlanStatus
    from seamless_migrate.domain.models import Plan

    body = {
        "name": "Pre-staging failed",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": [],
    }
    plan = api.post("/api/v1/plans", Role.operator, json=body).json()
    stored = api.store.get("plan", plan["id"], Plan)
    stored.status = PlanStatus.failed
    api.store.put("plan", stored)
    delete = lambda: api.client.delete(  # noqa: E731
        "/api/v1/providers/src-osp", headers=api.h(Role.admin)
    )
    refused = delete()
    assert refused.status_code == 409, refused.text
    assert plan["id"] in refused.json()["error"]["message"]
    stored = api.store.get("plan", plan["id"], Plan)
    stored.status = PlanStatus.completed
    api.store.put("plan", stored)
    assert delete().status_code == 204


async def test_a_retry_in_a_completed_plan_whose_provider_was_deleted_answers_409(tmp_path):
    """SDD §5.1/§12: DELETE /providers/{id} is allowed once a plan has completed; a retry would
    reopen the plan without that provider, so it answers 409 and changes nothing."""
    store = Store(f"sqlite:///{tmp_path / 'retry.db'}")
    store.create_schema()
    tokens = write_tokens(tmp_path / "tokens.yaml")
    settings = make_settings(tmp_path, tokens_file=tmp_path / "tokens.yaml", dashboard_dir=None)
    vms = [make_vm(source_id="vm-1", name="web-01")]
    h = build_harness(tmp_path, store, vms, settings=settings)
    plan = make_plan(vm_ids=["vm-1"], default_strategy="cold")
    store.put("plan", plan, expected_version=0)
    await h.orch.validate_plan(plan.id, "alice")
    m = (await h.migrations(plan.id))[0]
    m.phase = Phase.rolled_back  # its cutover failed and was rolled back, so the plan completed
    store.put("migration", m)
    done = store.get("plan", plan.id, Plan)
    done.status = PlanStatus.completed
    store.put("plan", done)
    app = create_app(settings, orchestrator=h.orch)
    admin = {"Authorization": f"Bearer {tokens[Role.admin]}"}
    operator = {"Authorization": f"Bearer {tokens[Role.operator]}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        deleted = await c.delete("/api/v1/providers/dst-rhoso", headers=admin)
        assert deleted.status_code == 204, deleted.text
        refused = await c.post(f"/api/v1/migrations/{m.id}/retry", headers=operator)
        assert refused.status_code == 409, refused.text
        assert "provider 'dst-rhoso' no longer exists" in refused.json()["error"]["message"]
        migration = (await c.get(f"/api/v1/migrations/{m.id}", headers=operator)).json()
        assert migration["phase"] == "rolled_back"
        stored = (await c.get(f"/api/v1/plans/{plan.id}", headers=operator)).json()
        assert stored["status"] == "completed"
    await h.orch.stop()
    store.dispose()


def test_a_completed_plan_keeps_its_providers_while_a_migration_holds_its_vm(api):
    """SDD §12/§8.1: a completed migration can be rolled back until it is finalized, and its
    rollback needs both providers - so can a rollback in progress or one that failed; a rolled-back
    migration lets its VM go (a retry refuses when a provider is gone)."""
    vm_ids = first_clean_vms(api, n=2)
    body = {
        "name": "awaiting finalize",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": vm_ids,
    }
    plan = api.post("/api/v1/plans", Role.operator, json=body).json()
    assert api.post(f"/api/v1/plans/{plan['id']}/validate", Role.operator).status_code == 200
    first, second = api.store.list("migration", Migration, plan_id=plan["id"])
    done = api.store.get("plan", plan["id"], Plan)
    done.status = PlanStatus.completed
    api.store.put("plan", done)
    delete = lambda: api.client.delete(  # noqa: E731
        "/api/v1/providers/dst-rhoso", headers=api.h(Role.admin)
    )
    second.phase = Phase.finalized
    api.store.put("migration", second)
    for phase in (Phase.completed, Phase.rolling_back, Phase.failed):
        first.phase = phase
        api.store.put("migration", first)
        refused = delete()
        assert refused.status_code == 409, (phase, refused.text)
        assert refused.json()["error"]["message"] == (
            f'provider is used by plan(s) that still need it: {plan["id"]} ("awaiting finalize", '
            "1 VM not finalized); a plan needs its providers until it completes and its VMs are "
            "finalized"
        )
    second.phase = Phase.completed
    api.store.put("migration", second)
    assert '("awaiting finalize", 2 VMs not finalized)' in delete().json()["error"]["message"]
    first.phase, second.phase = Phase.rolled_back, Phase.cancelled
    for m in (first, second):
        api.store.put("migration", m)
    assert delete().status_code == 204


def test_a_plan_that_has_not_completed_names_its_status_in_the_refusal(api):
    body = {
        "name": "still a draft",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": [],
    }
    plan = api.post("/api/v1/plans", Role.operator, json=body).json()
    refused = api.client.delete("/api/v1/providers/src-osp", headers=api.h(Role.admin))
    assert refused.status_code == 409
    assert refused.json()["error"]["message"] == (
        f'provider is used by plan(s) that still need it: {plan["id"]} ("still a draft", draft); '
        "a plan needs its providers until it completes and its VMs are finalized"
    )
    ids = [plan["id"]] + [
        api.post("/api/v1/plans", Role.operator, json={**body, "name": f"draft {i}"}).json()["id"]
        for i in range(10)
    ]
    message = api.client.delete("/api/v1/providers/src-osp", headers=api.h(Role.admin)).json()[
        "error"
    ]["message"]
    # at most ten plans, then how many more
    assert ids[9] in message and ids[10] not in message and '("draft 9"' not in message
    assert message.endswith(
        "draft) and 1 more; a plan needs its providers until it completes and its VMs are finalized"
    )


async def test_a_rollback_whose_provider_no_longer_exists_answers_409(tmp_path):
    """SDD §5.1: without its providers a rollback would fail, and a migration whose cutover stopped
    its source could then be neither rolled back, retried nor cancelled - so it is refused and the
    migration stays as it is (a provider deleted before the rule of SDD §12)."""
    store = Store(f"sqlite:///{tmp_path / 'rollback.db'}")
    store.create_schema()
    tokens = write_tokens(tmp_path / "tokens.yaml")
    settings = make_settings(tmp_path, tokens_file=tmp_path / "tokens.yaml", dashboard_dir=None)
    h = build_harness(
        tmp_path, store, [make_vm(source_id="vm-1", name="web-01")], settings=settings
    )
    plan = make_plan(vm_ids=["vm-1"], default_strategy="cold")
    store.put("plan", plan, expected_version=0)
    await h.orch.validate_plan(plan.id, "alice")
    m = (await h.migrations(plan.id))[0]
    m.phase = Phase.completed
    m.downtime_started_at = utcnow()
    m.downtime_ended_at = utcnow()
    store.put("migration", m)
    store.delete("provider", "dst-rhoso")
    app = create_app(settings, orchestrator=h.orch)
    operator = {"Authorization": f"Bearer {tokens[Role.operator]}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        refused = await c.post(
            f"/api/v1/migrations/{m.id}/rollback", headers=operator, json={"reason": "app broken"}
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["message"] == (
            "web-01 cannot be rolled back: the plan's destination provider 'dst-rhoso' no longer "
            "exists"
        )
        migration = (await c.get(f"/api/v1/migrations/{m.id}", headers=operator)).json()
        assert migration["phase"] == "completed"
    await h.orch.stop()
    store.dispose()


async def _race_with_a_provider_deletion(tmp_path, act):
    """Run ``act`` (a request that checks the plan's providers) and DELETE /providers/src-osp while
    ``act`` holds between its source and its destination check (SDD §12: one lock)."""
    store = Store(f"sqlite:///{tmp_path / 'race.db'}")
    store.create_schema()
    tokens = write_tokens(tmp_path / "tokens.yaml")
    settings = make_settings(tmp_path, tokens_file=tmp_path / "tokens.yaml", dashboard_dir=None)
    h = build_harness(
        tmp_path, store, [make_vm(source_id="vm-1", name="web-01")], settings=settings
    )
    app = create_app(settings, orchestrator=h.orch)
    headers = {r: {"Authorization": f"Bearer {tokens[r]}"} for r in (Role.operator, Role.admin)}
    entered, release = asyncio.Event(), asyncio.Event()
    get = h.orch.db.get

    async def slow_get(kind, key, model):
        if kind == "provider" and key == "dst-rhoso" and not entered.is_set():
            entered.set()  # the source was checked; the destination check waits here
            await release.wait()
        return await get(kind, key, model)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        setup = await act(c, headers, store, h, arm=lambda: setattr(h.orch.db, "get", slow_get))
        acting = asyncio.create_task(setup())
        await asyncio.wait_for(entered.wait(), 5)
        deleting = asyncio.create_task(
            c.delete("/api/v1/providers/src-osp", headers=headers[Role.admin])
        )
        await asyncio.sleep(0.05)  # as far as the deletion gets meanwhile
        release.set()
        acted, deleted = await acting, await deleting
        providers = {
            p["id"] for p in (await c.get("/api/v1/providers", headers=headers[Role.admin])).json()
        }
    await h.orch.stop()
    store.dispose()
    return acted, deleted, providers


async def test_a_plan_created_while_its_provider_is_deleted_keeps_the_provider(tmp_path):
    async def act(c, headers, store, h, arm):
        body = {
            "name": "racing",
            "source_provider_id": "src-osp",
            "destination_provider_id": "dst-rhoso",
            "vm_ids": ["vm-1"],
        }
        arm()
        return lambda: c.post("/api/v1/plans", headers=headers[Role.operator], json=body)

    created, deleted, providers = await _race_with_a_provider_deletion(tmp_path, act)
    assert created.status_code == 201, created.text
    assert deleted.status_code == 409, deleted.text
    assert "src-osp" in providers


async def test_a_plan_edited_onto_a_provider_being_deleted_keeps_the_provider(tmp_path):
    async def act(c, headers, store, h, arm):
        plan = make_plan(
            vm_ids=["vm-1"], source_provider_id="src-other", destination_provider_id="dst-rhoso"
        )
        other = store.get("provider", "src-osp", Provider).model_copy(update={"id": "src-other"})
        store.put("provider", other, expected_version=0)
        store.put("plan", plan, expected_version=0)
        arm()
        return lambda: c.patch(
            f"/api/v1/plans/{plan.id}",
            headers=headers[Role.operator],
            json={"source_provider_id": "src-osp"},
        )

    edited, deleted, providers = await _race_with_a_provider_deletion(tmp_path, act)
    assert edited.status_code == 200, edited.text
    assert deleted.status_code == 409, deleted.text
    assert "src-osp" in providers


async def test_a_retry_that_reopens_a_plan_while_its_provider_is_deleted_keeps_the_provider(
    tmp_path,
):
    async def act(c, headers, store, h, arm):
        plan = make_plan(vm_ids=["vm-1"], default_strategy="cold")
        store.put("plan", plan, expected_version=0)
        await h.orch.validate_plan(plan.id, "alice")
        m = (await h.migrations(plan.id))[0]
        m.phase = Phase.rolled_back  # a rolled-back migration lets its VM go: deletable
        store.put("migration", m)
        done = store.get("plan", plan.id, Plan)
        done.status = PlanStatus.completed
        store.put("plan", done)
        arm()
        return lambda: c.post(f"/api/v1/migrations/{m.id}/retry", headers=headers[Role.operator])

    retried, deleted, providers = await _race_with_a_provider_deletion(tmp_path, act)
    assert retried.status_code == 200, retried.text
    assert deleted.status_code == 409, deleted.text
    assert "src-osp" in providers


async def test_an_edit_during_a_validation_leaves_the_plan_draft(tmp_path):
    """SDD §8: PATCH /plans/{id} runs under the plan's lock like the other status writers, so an
    edit that arrives while a validation runs waits for it and the plan ends draft - never
    'validated' with vm_ids no validation saw - and Start refuses it until it is validated again."""
    store = Store(f"sqlite:///{tmp_path / 'race.db'}")
    store.create_schema()
    tokens = write_tokens(tmp_path / "tokens.yaml")
    settings = make_settings(tmp_path, tokens_file=tmp_path / "tokens.yaml", dashboard_dir=None)
    vms = [make_vm(source_id="vm-1", name="web-01"), make_vm(source_id="vm-2", name="db-01")]
    h = build_harness(tmp_path, store, vms, settings=settings)
    entered, release = asyncio.Event(), asyncio.Event()
    inventory = h.source.inventory

    async def slow_inventory():
        # the validation holds here, inside the plan's lock
        entered.set()
        await release.wait()
        return await inventory()

    h.source.inventory = slow_inventory
    plan = make_plan(vm_ids=["vm-1", "vm-2"], default_strategy="warm")
    store.put("plan", plan, expected_version=0)
    app = create_app(settings, orchestrator=h.orch)
    headers = {"Authorization": f"Bearer {tokens[Role.operator]}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        validate = asyncio.create_task(c.post(f"/api/v1/plans/{plan.id}/validate", headers=headers))
        await asyncio.wait_for(entered.wait(), 5)
        edit = asyncio.create_task(
            c.patch(f"/api/v1/plans/{plan.id}", headers=headers, json={"vm_ids": ["vm-1"]})
        )
        await asyncio.sleep(0.05)  # as far as the edit gets while the validation runs
        release.set()
        assert (await validate).status_code == 200
        edited = await edit
        assert edited.status_code == 200
        assert (edited.json()["status"], edited.json()["vm_ids"]) == ("draft", ["vm-1"])
        stored = (await c.get(f"/api/v1/plans/{plan.id}", headers=headers)).json()
        assert (stored["status"], stored["vm_ids"]) == ("draft", ["vm-1"])
        assert (await c.post(f"/api/v1/plans/{plan.id}/start", headers=headers)).status_code == 409
    await h.orch.stop()
    store.dispose()


def test_the_cutover_requester_cannot_approve_it_over_the_api(api):
    """ADV-17 (SDD §5.4): the request is not an approval; the requester's approval is refused."""
    migs = seed_migrations(api.store)
    base = f"/api/v1/migrations/{migs['ready'].id}"
    requested = api.post(f"{base}/cutover", Role.approver, json={"comment": "Window agreed"})
    assert requested.status_code == 200, requested.text
    body = requested.json()
    assert (body["cutover_requested_by"], body["approvals"]) == ("approver-user", [])
    assert body["cutover_requested_at"] is not None
    refused = api.post(f"{base}/approve", Role.approver, json={"comment": "my own"})
    assert refused.status_code == 409
    assert refused.json()["error"]["message"] == (
        "You requested this cutover. Another approver must approve it."
    )
    approved = api.post(f"{base}/approve", Role.admin, json={"comment": "Checked the findings"})
    assert approved.status_code == 200 and approved.json()["approvals"][-1]["actor"] == "admin-user"


def test_only_an_approver_changes_the_distinct_approver_setting(api):
    """ADV-17: Distinct approver belongs to the approval policy, approver-only like it (SDD §12)."""
    body = {
        "name": "Four eyes off",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": first_clean_vms(api),
        "require_distinct_approver": False,
    }
    refused = api.post("/api/v1/plans", Role.operator, json=body)
    assert refused.status_code == 403, refused.text
    assert "require_distinct_approver" in refused.json()["error"]["message"]
    created = api.post("/api/v1/plans", Role.approver, json=body)
    assert created.status_code == 201 and created.json()["require_distinct_approver"] is False
    plain = api.post(
        "/api/v1/plans", Role.operator, json={**body, "require_distinct_approver": True}
    )
    assert plain.status_code == 201, "the default value needs no approver"


def test_a_force_cutover_needs_an_approver_a_reason_and_a_warm_pass_in_progress(api):
    """ADV-16 (SDD §5.4, §12): only precopy/syncing of a warm migration; 10-2000 characters."""
    migs = seed_migrations(api.store)
    ready = f"/api/v1/migrations/{migs['ready'].id}/force-cutover"
    body = {"reason": "The change window closes at 22:00"}
    assert api.post(ready, Role.operator, json=body).status_code == 403
    assert api.post(ready, Role.approver, json={"reason": "short"}).status_code == 422
    assert api.post(ready, Role.approver, json=body).status_code == 409  # not in a pass
