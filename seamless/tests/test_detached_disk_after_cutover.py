"""A re-validation after a cutover attempt never drops a disk the VM had (SDD §5.4, §7.2, §9.3,
review RV7-01): a cold export detaches the data volumes, and one that failed may have left them
detached; the volume stays among the migration's disks as the blocker SRC_VM_VOLUME_DETACHED."""

from __future__ import annotations

import asyncio

import yaml

from seamless_migrate.domain.enums import Phase as P
from seamless_migrate.domain.enums import Strategy
from seamless_migrate.domain.models import Nic
from seamless_migrate.executors.ansible import SERVER_TYPE, AnsibleExecutor
from seamless_migrate.executors.base import PermanentStepError, StepName
from seamless_migrate.planning.preflight import keep_detached_volumes
from tests.factories import make_disk, make_vm
from tests.orch_support import ScriptedExecutor, build_harness, make_settings
from tests.test_orchestrator import plan_for

DISKS = [make_disk(id="vol-1"), make_disk(id="vol-data", bootable=False, device="/dev/vdb")]


def _export_of(vm) -> dict:
    """export_workloads.yml's workloads.yml for ``vm`` as Nova lists it now: one server whose
    params.volumes[] are its attached Cinder volumes, by id (os-migrate's server_volumes)."""
    return {
        "os_migrate_version": "1.1.0",
        "resources": [
            {
                "type": SERVER_TYPE,
                "_info": {"id": vm.source_id},
                "_migration_params": {},
                "params": {
                    "name": vm.name,
                    "volumes": [
                        {"_info": {"id": d.id}, "params": {"name": d.id}}
                        for d in vm.disks
                        if d.kind == "volume"
                    ],
                },
            }
        ],
    }


def _web_01():
    return make_vm(
        source_id="vm-1",
        name="web-01",
        disks=list(DISKS),
        nics=[Nic(network="app-net", mtu=1500, fixed_ips=["10.0.0.1"])],
        change_rate_bps=2 * 2**20,
    )


async def test_a_disk_an_earlier_attempt_left_detached_never_leaves_the_vm_by_a_new_validation(
    tmp_path, store
):
    """Attempt 1 of a cold cutover stops the VM; its export detaches the data volume and fails,
    and the rescue that would attach it again failed too. The retry's export lacks the volume: the
    executor refuses it and asks to attach it again - no validation offered. A validation once the
    migration is ready again keeps the volume and blocks (SRC_VM_VOLUME_DETACHED), and so does the
    next one; attached again, the validation clears and the cutover imports both disks."""
    settings = make_settings(tmp_path)
    vm = _web_01()
    attempts: list[list[str]] = []  # the volumes each cutover attempt's import would get
    harness: list = []
    calls = {"n": 0}

    async def cutover(ctx):
        source = harness[0].source
        now = source.vms["vm-1"]  # the source VM as Nova lists it at this attempt
        calls["n"] += 1
        osm = tmp_path / f"osm-{calls['n']}"
        osm.mkdir()
        (osm / "workloads.yml").write_text(yaml.safe_dump(_export_of(now), sort_keys=False))
        validated = [d.id for d in ctx.migration.vm.disks if d.kind == "volume"]
        await asyncio.to_thread(
            AnsibleExecutor._rewrite_workloads,
            osm, None, ctx.migration.vm.source_id, None, None, None, None, validated,
        )  # fmt: skip
        exported = yaml.safe_load((osm / "workloads.yml").read_text())["resources"][0]
        attempts.append(sorted(v["_info"]["id"] for v in exported["params"]["volumes"]))
        if len(attempts) == 1:
            await ctx.mark_downtime_start()
            source.vms["vm-1"] = now.model_copy(
                update={"disks": [d for d in now.disks if d.id != "vol-data"]}
            )
            raise PermanentStepError(
                "import_workloads.yml failed (exit 2): Got unexpected disk list after attaching "
                "volume to source conversion host instance"
            )
        return await executor.sim.run(StepName.CUTOVER, ctx)

    executor = ScriptedExecutor(settings, hooks={StepName.CUTOVER: cutover})
    h = build_harness(tmp_path, store, [vm], executor=executor, settings=settings)
    harness.append(h)
    plan = plan_for([vm], default_strategy=Strategy.cold)
    store.put("plan", plan)
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    await h.orch.start()
    try:
        mid = (await h.by_vm(plan.id, "vm-1")).id
        await h.wait_phase(mid, P.rolled_back)

        # the retry: the export lacks vol-data, and the refusal offers no way past the check
        await h.orch.retry(mid, "bayu")
        refused = await h.wait_phase(mid, P.failed)
        assert "vol-data" in (refused.error or "")
        assert "attach them to the source VM again" in (refused.error or "")
        assert "validate the plan" not in (refused.error or "")

        # retried while paused, then validated: the VM is read without the volume, which stays
        await h.orch.pause_plan(plan.id, "bayu")
        await h.orch.retry(mid, "bayu")
        for _ in range(2):  # and a validation after that keeps it too
            await h.orch.validate_plan(plan.id, "bayu")
            blocked = await h.migration(mid)
            assert blocked.phase == P.blocked, blocked.phase
            assert [f.code for f in blocked.findings if f.severity == "blocker"] == [
                "SRC_VM_VOLUME_DETACHED"
            ]
            assert [d.id for d in blocked.vm.disks] == ["vol-1", "vol-data"]

        # attached again: the validation clears, and the cutover imports both disks
        h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"disks": list(DISKS)})
        await h.orch.validate_plan(plan.id, "bayu")
        assert (await h.migration(mid)).phase == P.ready
        await h.orch.start_plan(plan.id, "bayu")
        done = await h.wait_phase(mid, P.completed, P.failed, timeout=20)
    finally:
        await h.orch.stop()

    assert done.phase == P.completed, done.error
    assert attempts == [["vol-1", "vol-data"], ["vol-1", "vol-data"]]


async def test_a_validation_before_any_cutover_still_drops_a_volume_detached_on_purpose(
    tmp_path, store
):
    """Only a migration whose history reached cutover keeps a vanished volume: before that, an
    operator who detached a data volume validates again, and the migration goes without it."""
    vm = _web_01()
    h = build_harness(tmp_path, store, [vm])
    plan = plan_for([vm], default_strategy=Strategy.cold)
    store.put("plan", plan)
    await h.orch.validate_plan(plan.id, "alice")
    mid = (await h.by_vm(plan.id, "vm-1")).id

    h.source.vms["vm-1"] = vm.model_copy(update={"disks": [DISKS[0]]})
    await h.orch.validate_plan(plan.id, "alice")

    m = await h.migration(mid)
    assert m.phase == P.ready
    assert [d.id for d in m.vm.disks] == ["vol-1"]
    assert not [f for f in m.findings if f.code == "SRC_VM_VOLUME_DETACHED"]


def test_a_volume_the_vm_no_longer_lists_stays_with_a_blocker():
    previous = _web_01()
    current = previous.model_copy(update={"disks": [DISKS[0]]})

    vm, findings = keep_detached_volumes(previous, current)

    assert [d.id for d in vm.disks] == ["vol-1", "vol-data"]
    assert [(f.code, f.severity) for f in findings] == [("SRC_VM_VOLUME_DETACHED", "blocker")]
    assert "vol-data" in findings[0].message and "web-01" in findings[0].message
    assert keep_detached_volumes(previous, previous) == (previous, [])
    # a root or ephemeral disk the provider no longer reports is no Cinder volume to keep
    root = make_disk(id="vm-1-root", kind="image_root", volume_type=None)
    image_booted = previous.model_copy(update={"disks": [root, DISKS[1]]})
    data_only = current.model_copy(update={"disks": [DISKS[1]]})
    assert keep_detached_volumes(image_booted, data_only)[1] == []
