"""Retiring a plan by cancelling it (SDD §12, §8.1; the owner's choice of 2026-10-13).

``POST /plans/{id}/cancel`` cancels a plan none of whose migrations started: each migration is
cancelled as ``POST /migrations/{id}/cancel`` does, the plan becomes ``cancelled`` and nothing is
deleted (§11) - its VMs are free for another plan and its providers for deletion.
"""

import pytest
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.domain.enums import Phase, Role, SyncPassKind
from seamless_migrate.domain.models import Migration, SyncPass, utcnow
from seamless_migrate.store import Store
from tests.api_support import DESTINATION, SOURCE, Api, api_settings


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


def clean_vms(api, n):
    inventory = api.get("/api/v1/providers/src-osp/inventory").json()
    names = ["web-01", "web-02", "api-gw-01", "portal-01", "dns-01", "bastion-01"]
    return [vm["source_id"] for vm in inventory if vm["name"] in names][:n]


def new_plan(api, name, vm_ids):
    body = {
        "name": name,
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": vm_ids,
    }
    created = api.post("/api/v1/plans", Role.operator, json=body)
    assert created.status_code == 201, created.text
    return created.json()["id"]


def validated_plan(api, name, vm_ids):
    plan_id = new_plan(api, name, vm_ids)
    validated = api.post(f"/api/v1/plans/{plan_id}/validate", Role.operator)
    assert validated.status_code == 200, validated.text
    return plan_id


def migrations(api, plan_id):
    return [m for m in api.store.list("migration", Migration) if m.plan_id == plan_id]


def cancel(api, plan_id, role=Role.operator, **body):
    return api.post(f"/api/v1/plans/{plan_id}/cancel", role, json=body)


def test_cancelling_a_plan_cancels_its_unstarted_migrations_and_frees_its_vms(api):
    vm_ids = clean_vms(api, 2)
    plan_id = validated_plan(api, "made by mistake", vm_ids)
    assert {m.phase for m in migrations(api, plan_id)} <= {Phase.ready, Phase.blocked}
    # the plan holds its VMs: another plan cannot take one (SDD §5.4)
    other = new_plan(api, "the real one", vm_ids[:1])
    held = api.post(f"/api/v1/plans/{other}/validate", Role.operator)
    assert held.status_code == 409 and "made by mistake" in held.json()["error"]["message"]

    res = cancel(api, plan_id, reason="made by mistake")
    assert res.status_code == 200, res.text
    assert (res.json()["status"], res.json()["derived_status"]) == ("cancelled", "cancelled")
    # nothing is deleted (SDD §11): the plan and its migrations stay, cancelled
    assert api.get(f"/api/v1/plans/{plan_id}").json()["status"] == "cancelled"
    assert [m.phase for m in migrations(api, plan_id)] == [Phase.cancelled, Phase.cancelled]
    events = api.store.events(since_seq=0, limit=100000)
    [done] = [e for e in events if e.kind == "plan.cancelled"]
    assert (done.plan_id, done.actor) == (plan_id, "operator-user")
    assert done.data == {"reason": "made by mistake", "migrations": 2}
    # each migration was cancelled as POST /migrations/{id}/cancel does, with its own event
    cancels = [e for e in events if e.kind == "migration.action" and e.plan_id == plan_id]
    assert sorted(e.data["action"] for e in cancels) == ["cancel", "cancel"]
    # the VM is free for another plan
    freed = api.post(f"/api/v1/plans/{other}/validate", Role.operator)
    assert freed.status_code == 200, freed.text


def test_cancelling_a_plan_cleans_up_what_an_earlier_attempt_copied(api):
    """SDD §12: each migration is cancelled as POST /migrations/{id}/cancel does (§5.1) - a
    ready one that kept the passes of an earlier attempt records its cleanup, which removes it."""
    plan_id = validated_plan(api, "tried before", clean_vms(api, 1))
    [m] = migrations(api, plan_id)
    earlier = SyncPass(number=1, kind=SyncPassKind.full, started_at=utcnow(), ended_at=utcnow())
    kept = m.model_copy(update={"phase": Phase.ready, "sync_passes": [earlier]})
    api.store.put("migration", kept)
    res = cancel(api, plan_id)
    assert res.status_code == 200, res.text
    [after] = migrations(api, plan_id)
    assert after.phase == Phase.cancelled
    # the cancel's cleanup ran, or still runs: either way the migration recorded it
    assert after.checkpoint in ("cleanup", "cleaned")
    events = api.store.events(since_seq=0, limit=100000)
    assert any(e.kind == "migration.action" and e.migration_id == m.id for e in events)


def test_a_cancelled_plan_frees_its_providers_for_deletion(api):
    plan_id = validated_plan(api, "abandoned", clean_vms(api, 1))
    admin = api.h(Role.admin)
    needed = api.client.delete("/api/v1/providers/dst-rhoso", headers=admin)
    assert needed.status_code == 409
    cancelled = cancel(api, plan_id)
    assert cancelled.status_code == 200
    freed = api.client.delete("/api/v1/providers/dst-rhoso", headers=admin)
    assert freed.status_code == 204, freed.text


def test_a_running_plan_is_paused_before_it_is_cancelled(api):
    plan_id = validated_plan(api, "running", clean_vms(api, 1))
    started = api.post(f"/api/v1/plans/{plan_id}/start", Role.operator)
    assert started.status_code == 200
    res = cancel(api, plan_id)
    assert res.status_code == 409
    assert res.json()["error"]["message"] == "pause the plan before cancelling it"
    assert api.get(f"/api/v1/plans/{plan_id}").json()["status"] == "running"


def test_a_plan_whose_migration_started_is_not_cancelled_and_names_it(api):
    plan_id = validated_plan(api, "half done", clean_vms(api, 2))
    started, waiting = migrations(api, plan_id)
    api.store.put("migration", started.model_copy(update={"phase": Phase.failed}))
    res = cancel(api, plan_id)
    assert res.status_code == 409
    message = res.json()["error"]["message"]
    assert f"{started.vm.name} ({Phase.failed})" in message
    assert waiting.vm.name not in message
    # what cancelling it on its own does, a VMware one's included (SDD §12, §5.1, docs-drift DD3-02)
    assert message.endswith(
        "cancel a failed or rolled-back migration on its own first, which cleans up what it "
        "copied - on VMware it names the kit's copies it keeps"
    ), message
    # nothing changed: the plan and its waiting migration are as they were
    assert api.get(f"/api/v1/plans/{plan_id}").json()["status"] == "validated"
    phases = {m.id: m.phase for m in migrations(api, plan_id)}
    assert phases == {started.id: Phase.failed, waiting.id: waiting.phase}


def test_a_plan_whose_unstarted_migration_has_its_source_stopped_is_not_cancelled(api):
    """SDD §12 (RR-01): a ready migration can still have its source stopped - a retry after a
    failed cutover keeps the open clock (§5.2) - and a cancel cannot restart it (§5.1). The plan
    cancel names it and refuses before cancelling any migration, though another sorts before it."""
    plan_id = validated_plan(api, "stopped source", clean_vms(api, 2))
    first, last = sorted(migrations(api, plan_id), key=lambda m: m.id)
    update = {"phase": Phase.ready, "downtime_started_at": utcnow(), "downtime_ended_at": None}
    api.store.put("migration", last.model_copy(update=update))
    res = cancel(api, plan_id)
    assert res.status_code == 409
    message = res.json()["error"]["message"]
    assert last.vm.name in message and "stopped" in message and "cut it over" in message
    assert first.vm.name not in message
    # nothing changed: the plan and the migration sorted first are as they were
    assert api.get(f"/api/v1/plans/{plan_id}").json()["status"] == "validated"
    phases = {m.id: m.phase for m in migrations(api, plan_id)}
    assert phases == {first.id: first.phase, last.id: Phase.ready}


def test_a_cancelled_plan_is_terminal(api):
    plan_id = validated_plan(api, "done with", clean_vms(api, 1))
    first = cancel(api, plan_id)
    assert first.status_code == 200
    for action in ("validate", "start", "pause"):
        refused = api.post(f"/api/v1/plans/{plan_id}/{action}", Role.operator)
        assert refused.status_code == 409, action
    edit = api.client.patch(
        f"/api/v1/plans/{plan_id}", headers=api.h(Role.operator), json={"name": "revived"}
    )
    assert edit.status_code == 409
    waves = api.post(f"/api/v1/plans/{plan_id}/waves/auto", Role.operator, json={})
    assert waves.status_code == 409
    # cancelling it again answers the plan as it is, without another event
    again = cancel(api, plan_id)
    assert again.status_code == 200 and again.json()["status"] == "cancelled"
    kinds = [e.kind for e in api.store.events(since_seq=0, limit=100000)]
    assert kinds.count("plan.cancelled") == 1


def test_cancelling_a_plan_needs_the_operator_role_and_a_known_plan(api):
    plan_id = new_plan(api, "draft", clean_vms(api, 1))
    viewer = cancel(api, plan_id, role=Role.viewer)
    assert viewer.status_code == 403
    unknown = cancel(api, "plan-00000000")
    assert unknown.status_code == 404
    # a draft without migrations is cancelled too
    draft = cancel(api, plan_id)
    assert draft.json()["status"] == "cancelled"
