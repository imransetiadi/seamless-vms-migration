import asyncio
import dataclasses
import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from seamless_migrate.ai.advisor import Advisor
from seamless_migrate.ai.jev import JevClient
from seamless_migrate.ai.knowledge import KnowledgeService
from seamless_migrate.ai.memory import MemoryClient
from seamless_migrate.ai.redaction import redact
from seamless_migrate.ai.snapshots import canonical_json
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import (
    DecisionStatus,
    Phase,
    PlanStatus,
    ProviderKind,
    ReasonCode,
    Strategy,
)
from seamless_migrate.domain.models import (
    AdvisorNote,
    AdvisorPolicy,
    AdvisorRecommendation,
    AdvisorSnapshot,
    AppProbe,
    Approval,
    CutoverWindow,
    Event,
    Finding,
    ForceCutover,
    Migration,
    Nic,
    PhaseChange,
    Plan,
    PolicyScope,
    Provider,
    SyncPass,
    VerificationConfig,
    Wave,
    next_pass_number,
    utcnow,
)
from seamless_migrate.events import AUDIT_KINDS, EventBus, emit, put_all_and_emit
from seamless_migrate.executors.base import (
    PermanentStepError,
    StepName,
    StepResult,
    TransientStepError,
)
from seamless_migrate.executors.simulated import SimulatedExecutor
from seamless_migrate.orchestrator import (
    VM_CLAIMS,
    BadRequest,
    NotAllowed,
    Unprocessable,
    counted_approvals,
)
from seamless_migrate.planning.estimator import (
    estimate_final_downtime,
    first_pass_seconds,
    params_for_plan,
)
from seamless_migrate.providers.base import ProviderError
from seamless_migrate.store import ConflictError, NotFound, Store, UnstorableValue
from tests.executor_support import make_ctx
from tests.factories import make_disk, make_migration, make_vm
from tests.jev_fakes import fixture_responder, load_fixture, session_factory, tool_result
from tests.orch_support import (
    DST_PROVIDER,
    SRC_PROVIDER,
    Clock,
    ScriptedExecutor,
    StaticDestination,
    build_harness,
    make_settings,
)
from tests.test_knowledge import memory_with

P = Phase


def vm(i: int, name: str | None = None, size_gb: int = 20, used_gb: float = 10.0, **kw):
    data = {
        "source_id": f"vm-{i}",
        "name": name or f"web-{i:02d}",
        "disks": [make_disk(id=f"vol-{i}", size_gb=size_gb, used_gb=used_gb)],
        "nics": [Nic(network="app-net", mtu=1500, fixed_ips=[f"10.0.0.{i}"])],
        "change_rate_bps": 2 * 2**20,
    }
    data.update(kw)
    return make_vm(**data)


def plan_for(vms, **kw) -> Plan:
    data = {
        "name": "Finance",
        "source_provider_id": SRC_PROVIDER.id,
        "destination_provider_id": DST_PROVIDER.id,
        "vm_ids": [v.source_id for v in vms],
        "require_approval": False,
        "auto_cutover": True,
        "verification": VerificationConfig(timeout_s=2),
    }
    data.update(kw)
    return Plan(**data)


async def setup(tmp_path, store, vms, plan_kw=None, **harness_kw):
    h = build_harness(tmp_path, store, vms, **harness_kw)
    plan = plan_for(vms, **(plan_kw or {}))
    store.put("plan", plan)
    return h, plan


async def run_plan(h, plan):
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    await h.orch.start()


async def _a_paused_plan_whose_finished_vm_left_the_source(tmp_path, store):
    """SDD §5.4 (review PL-09): web-01 is cut over, completed and finalized with delete_source - its
    VM gone from the source, as a storage handover's step 4 leaves it too -, and its plan paused
    with web-02 still ready. Returns the harness, the plan and the source's reads from then on."""
    vms = [vm(1), vm(2)]
    h, plan = await setup(
        tmp_path, store, vms, plan_kw={"default_strategy": Strategy.cold, "auto_cutover": False}
    )
    await run_plan(h, plan)
    first = await h.by_vm(plan.id, "vm-1")
    await h.orch.request_cutover(first.id, "sari")
    await h.wait_phase(first.id, P.completed)
    await h.orch.pause_plan(plan.id, "alice")
    await h.orch.finalize(first.id, "sari", delete_source=True, confirm="web-01")
    del h.source.vms["vm-1"]  # the finalize deleted the source server
    reads: list[str] = []
    get_vm = h.source.get_vm

    async def counted(source_id):
        reads.append(source_id)
        return await get_vm(source_id)

    h.source.get_vm = counted
    return h, plan, reads


async def test_a_plan_whose_finished_vm_left_the_source_validates_again(tmp_path, store):
    """SDD §5.4 (review PL-09): a validation reads from the source only the VMs it re-assesses; a
    finished migration counts as its recorded VM, so its plan validates again once that VM left
    the source - never 502 "VM ... not found" - and the finished migration stays as it is."""
    h, plan, reads = await _a_paused_plan_whose_finished_vm_left_the_source(tmp_path, store)
    try:
        report = await h.orch.validate_plan(plan.id, "alice")
    finally:
        await h.orch.stop()
    assert sorted((i.vm_name, i.phase) for i in report.migrations) == [
        ("web-01", P.finalized),
        ("web-02", P.ready),
    ]
    assert reads == ["vm-2"]


async def test_the_waves_of_a_plan_whose_finished_vm_left_the_source_are_planned_again(
    tmp_path, store
):
    """SDD §5.4, §9.4 (review PL-09): the wave planner reads the plan's VMs as a validation does -
    a finished migration's VM as recorded -, so the waves of a plan whose finished VM left the
    source are planned again, every VM in one."""
    h, plan, reads = await _a_paused_plan_whose_finished_vm_left_the_source(tmp_path, store)
    try:
        planned = await h.orch.auto_waves(plan.id, 10, "alice")
    finally:
        await h.orch.stop()
    assert sorted(v for w in planned.waves for v in w.vm_ids) == ["vm-1", "vm-2"]
    assert reads == ["vm-2"]


class QuotaDestination(StaticDestination):
    """A destination whose free quota is what Nova and Cinder report for the project: the limit
    less what its servers and volumes already use (providers/openstack.py), and whose m1.small is
    the test VMs' size - the flavor the quota prices them with (SDD §9.3, review PL-01)."""

    def __init__(self, free: dict[str, int]) -> None:
        super().__init__()
        self.free = dict(free)

    async def inventory(self):
        inv = await super().inventory()
        inv.flavors = [
            {"name": "m1.small", "vcpus": 2, "ram_mb": 4096, "disk_gb": 20, "extra_specs": {}}
        ]
        inv.quotas = {"finance": dict(self.free)}
        return inv


async def test_a_revalidation_never_counts_a_completed_migration_again(tmp_path, store):
    """SDD §9.3, §5.4 (review PL-02): two VMs fit the project exactly. Once web-01 is cut over and
    completed, its server and volume are in use - the destination reports that much less free -,
    so the re-validation of the paused plan counts web-02 alone: it stays ready."""
    full = {"cores": 4, "ram_mb": 8192, "instances": 2, "volumes": 2, "gigabytes": 40}
    dst = QuotaDestination(full)  # two VMs of 2 vCPUs, 4 GiB, one 20 GiB volume each
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1), vm(2)],
        {"default_strategy": Strategy.cold, "auto_cutover": False},
        destination=dst,
    )
    await run_plan(h, plan)
    try:
        first = await h.by_vm(plan.id, "vm-1")
        await h.orch.request_cutover(first.id, "sari")
        await h.wait_phase(first.id, P.completed)
        await h.orch.pause_plan(plan.id, "alice")
        dst.free = {"cores": 2, "ram_mb": 4096, "instances": 1, "volumes": 1, "gigabytes": 20}
        report = await h.orch.validate_plan(plan.id, "alice")
    finally:
        await h.orch.stop()
    second = next(i for i in report.migrations if i.vm_name == "web-02")
    assert (second.phase, [f.code for f in second.findings]) == (P.ready, [])


async def _a_plan_in_two_waves(tmp_path, store):
    """web-01 in the Pilot (wave-1), web-02 in wave-3, validated; web-02's migration then failed
    before its source stopped, as a cutover refused by the destination leaves it."""
    h, plan = await setup(tmp_path, store, [vm(1), vm(2)], {"default_strategy": Strategy.cold})
    plan.waves = [
        Wave(id="wave-1", name="Pilot", order=1, vm_ids=["vm-1"]),
        Wave(id="wave-3", name="Wave 3", order=3, vm_ids=["vm-2"], depends_on=["wave-1"]),
    ]
    store.put("plan", plan)
    await h.orch.validate_plan(plan.id, "alice")
    failed = await h.by_vm(plan.id, "vm-2")
    assert failed.wave_id == "wave-3"
    failed.phase = P.failed
    store.put("migration", failed)
    return h, plan, failed


async def test_a_validation_writes_the_plans_wave_into_a_migration_it_leaves_alone(tmp_path, store):
    """SDD §9.4 (review PL-11): the waves planned anew put web-02 in the Pilot; the validation
    that every wave change needs writes that wave into its failed migration too, which it does not
    re-assess - so the plan starts, never refused as one whose migration "belongs to no wave"."""
    h, plan, failed = await _a_plan_in_two_waves(tmp_path, store)
    try:
        replanned = await h.orch.auto_waves(plan.id, 10, "alice")
        [pilot] = replanned.waves
        assert pilot.vm_ids == ["vm-1", "vm-2"]
        await h.orch.validate_plan(plan.id, "alice")
        left = await h.migration(failed.id)
        assert (left.phase, left.wave_id) == (P.failed, pilot.id)
        assert (await h.orch.start_plan(plan.id, "alice")).status == PlanStatus.running
    finally:
        await h.orch.stop()


async def test_a_validation_clears_the_wave_of_a_migration_whose_vm_left_every_wave(
    tmp_path, store
):
    """SDD §9.4 (review PL-11): waves edited so that web-02 is in none: the validation writes no
    wave into its failed migration either - never one the plan no longer has - and start_plan
    names it as belonging to no wave."""
    h, plan, failed = await _a_plan_in_two_waves(tmp_path, store)
    edited = await h.plan(plan.id)
    edited.waves = [w for w in edited.waves if w.id == "wave-1"]
    edited.status = PlanStatus.draft
    store.put("plan", edited)
    try:
        await h.orch.validate_plan(plan.id, "alice")
        assert (await h.migration(failed.id)).wave_id is None
        with pytest.raises(NotAllowed, match="belong to no wave: web-02"):
            await h.orch.start_plan(plan.id, "alice")
    finally:
        await h.orch.stop()


async def test_a_retry_takes_the_wave_the_plan_gives_its_vm(tmp_path, store):
    """SDD §9.4, §12 (review PL-11): a retried migration runs in its VM's wave in the plan, never
    in a wave it was assessed under that the plan no longer has - the tick would read that as no
    wave and cut it over at once, before the Pilot."""
    h, plan, failed = await _a_plan_in_two_waves(tmp_path, store)
    stale = await h.migration(failed.id)
    stale.wave_id = "wave-gone"  # the waves it was assessed under, planned anew since
    store.put("migration", stale)
    retried = await h.orch.retry(failed.id, "bayu")
    assert (retried.phase, retried.wave_id) == (P.ready, "wave-3")


async def test_a_retry_refuses_a_vm_in_no_wave_of_the_plan(tmp_path, store):
    """SDD §9.4, §12 (review PL-11): a retry refuses (409, before any change) a migration of a plan
    with waves whose VM is in none of them, as start_plan does: it would run at once, outside every
    wave's order and max_parallel."""
    h, plan, failed = await _a_plan_in_two_waves(tmp_path, store)
    edited = await h.plan(plan.id)
    edited.waves = [w for w in edited.waves if w.id == "wave-1"]
    store.put("plan", edited)
    with pytest.raises(NotAllowed, match="belongs to no wave"):
        await h.orch.retry(failed.id, "bayu")
    left = await h.migration(failed.id)
    assert (left.phase, left.wave_id) == (P.failed, "wave-3")


#: two VMs of 2 vCPUs, 4 GiB and one 20 GiB volume each fit the project exactly; once web-01's
#: attempt holds its 20 GiB destination volume, Cinder reports one volume and 20 GiB less free
QUOTA_FOR_TWO = {"cores": 4, "ram_mb": 8192, "instances": 2, "volumes": 2, "gigabytes": 40}
QUOTA_ONE_VOLUME_HELD = {**QUOTA_FOR_TWO, "volumes": 1, "gigabytes": 20}


async def _a_cutover_failing_after_the_stop(ctx):
    await ctx.mark_downtime_start()
    raise PermanentStepError("create the server in the destination: HTTP 500")


async def _a_first_pass_failing(ctx):
    raise PermanentStepError("the first pass failed after creating its destination volumes")


@pytest.mark.parametrize(
    "how",
    [
        "rolled back after its passes",
        "failed with its volumes recorded",
        "retried back to ready",
        "failed before creating anything",
        "failed with a record of no volume",
    ],
)
async def test_a_revalidation_never_counts_the_volumes_an_earlier_attempt_holds(
    tmp_path, store, how
):
    """SDD §9.3 (review RV38A-01): web-01's warm attempt keeps the destination volume it created
    for the next attempt - a rollback deletes the server only, a first pass that failed after
    creating it leaves it (its workload record says so), and a Retry puts the migration back in
    ready with it -, so Cinder counts it in use and the re-validation counts web-01's server alone:
    web-02 fits. An attempt that created nothing counts all of it - a record holding no destination
    volume, a pending source snapshot alone, too -, and web-02 does not fit."""
    from seamless_migrate.executors.ansible import run_dir

    dst = QuotaDestination(QUOTA_FOR_TWO)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1), vm(2)],
        {
            "default_strategy": Strategy.warm,
            "strategy_overrides": {"vm-2": Strategy.cold},  # web-02 waits in ready
            "auto_cutover": False,
        },
        destination=dst,
    )
    fails_first_pass = how.startswith("failed")
    if fails_first_pass:
        h.executor.hooks[StepName.PRECOPY] = _a_first_pass_failing
    else:
        h.executor.hooks[StepName.CUTOVER] = _a_cutover_failing_after_the_stop
    await run_plan(h, plan)
    try:
        first = await h.by_vm(plan.id, "vm-1")
        if fails_first_pass:
            await h.wait_phase(first.id, P.failed, timeout=20)
        else:
            await h.wait_phase(first.id, P.awaiting_cutover, timeout=20)
            await h.orch.request_cutover(first.id, "sari")
            await h.wait_phase(first.id, P.rolled_back, timeout=20)
        await h.orch.pause_plan(plan.id, "alice")
        if how == "retried back to ready":
            assert (await h.orch.retry(first.id, "bayu")).phase == P.ready
        written = {
            "failed with its volumes recorded": {"dest_volumes": {"vol-1": "dst-vol-1"}},
            "failed with a record of no volume": {"dest_volumes": {}, "pending_snapshot": "snap-1"},
        }
        if how in written:
            record = run_dir(h.settings, plan.id, first.id) / "osm" / "workload_warm" / "vm-1.json"
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps(written[how]))
        held = how not in ("failed before creating anything", "failed with a record of no volume")
        # one volume in use: web-01's when it holds one - and when it holds nothing, the control,
        # counting all of web-01 leaves web-02 no room
        dst.free = dict(QUOTA_ONE_VOLUME_HELD)
        report = await h.orch.validate_plan(plan.id, "alice")
    finally:
        await h.orch.stop()
    second = next(i for i in report.migrations if i.vm_name == "web-02")
    blocked = [f.code for f in second.findings if f.code == "DST_QUOTA_INSUFFICIENT"]
    assert (second.phase, blocked) == (
        (P.ready, []) if held else (P.blocked, ["DST_QUOTA_INSUFFICIENT"])
    ), how


#: an attempt rolled back before any cutover ran, and one whose cutover ran
UNCUT = (P.precopy, P.syncing, P.awaiting_cutover, P.rolling_back, P.rolled_back)
CUT = (P.precopy, P.syncing, P.awaiting_cutover, P.cutover, P.rolling_back, P.rolled_back)


def _history(*phases: Phase) -> list[PhaseChange]:
    """Phase changes through ``phases``, a minute apart, from ready."""
    start, before, changes = utcnow() - timedelta(hours=3), P.ready, []
    for n, phase in enumerate(phases):
        at = start + timedelta(minutes=n)
        changes.append(
            PhaseChange(from_phase=before, to_phase=phase, at=at, reason="test", actor="test")
        )
        before = phase
    return changes


def _passes(count: int, history: list[PhaseChange] | None = None) -> list[SyncPass]:
    """``count`` passes, started in the last attempt of ``history`` - after it last entered
    precopy - or three hours ago without one."""
    precopy = [c.at for c in history or [] if c.to_phase == P.precopy]
    start = precopy[-1] + timedelta(seconds=1) if precopy else utcnow() - timedelta(hours=3)
    return [
        SyncPass(
            number=n,
            kind="full" if n == 1 else "delta",
            started_at=start + timedelta(seconds=n),
            duration_s=60.0,
        )
        for n in range(1, count + 1)
    ]


#: what a rollback kept, where no workload record tells (SDD §5.1, §9.3, review RV39-02): strategy,
#: the phases of the migration's history, its recorded passes, and whether its copies remain
NO_RECORD_CASES = pytest.mark.parametrize(
    ("strategy", "phases", "passes", "held"),
    [
        (Strategy.warm, CUT, 2, True),
        (Strategy.warm, UNCUT, 0, False),
        (Strategy.vmware_warm, UNCUT, 2, True),
        (Strategy.vmware_warm, UNCUT, 0, False),
        (Strategy.vmware_warm, CUT, 2, False),
        (Strategy.vmware_warm, (*CUT, P.ready, *UNCUT), 3, True),
        (
            Strategy.vmware_warm,
            (*CUT, P.ready, P.precopy, P.awaiting_cutover, P.rolling_back, P.rolled_back),
            3,
            True,
        ),
        (Strategy.vmware_warm, (*UNCUT, P.ready, *CUT[:4], P.failed, P.ready), 3, False),
        (Strategy.cold, (P.cutover, P.rolling_back, P.rolled_back), 1, False),
        (Strategy.vmware_cold, (P.cutover, P.rolling_back, P.rolled_back), 1, False),
        (Strategy.storage_handover, (P.cutover, P.rolling_back, P.rolled_back), 0, False),
    ],
    ids=[
        "warm passes: the volumes stay for the next attempt",
        "warm with no pass copied nothing",
        "vmware_warm rolled back before its cutover: the kit's copies stay",
        "vmware_warm whose first copy was cut off: its rollback deleted it",
        "vmware_warm whose cutover ran: its rollback deleted them",
        "vmware_warm whose last attempt ran no cutover",
        "vmware_warm whose last attempt converged from its first pass",
        "vmware_warm retried to ready after a cutover",
        "cold: its rollback deletes its copy",
        "vmware_cold: its rollback deletes its copy",
        "storage_handover: no copy",
    ],
)


@NO_RECORD_CASES
async def test_where_no_record_tells_warm_passes_and_an_uncut_vmware_warm_attempt_hold_volumes(
    tmp_path, store, strategy, phases, passes, held
):
    """SDD §9.3, §7.2 (reviews RV38A-01, RV39-02, RV39-04): without a workload record - the demo,
    the VMware kit - a warm migration's passes hold its destination volumes, as do a vmware_warm
    one's when its last attempt ran no cutover (no cutover since it last entered precopy): the
    kit's rollback deletes its copies after one, or marks them cutover. A cold, vmware_cold or
    storage_handover rollback keeps no copy, the demo's recorded full pass notwithstanding."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    history = _history(*phases)
    m = make_migration(
        plan_id=plan.id,
        vm=vm(1),
        strategy=strategy,
        phase=phases[-1],
        phase_history=history,
        sync_passes=_passes(passes, history),
    )
    try:
        assert await h.orch.holds_destination_volumes(m) is held
    finally:
        await h.orch.stop()


@NO_RECORD_CASES
async def test_where_no_record_tells_copied_data_is_what_a_rollback_kept(
    tmp_path, store, strategy, phases, passes, held
):
    """SDD §5.1, §12 (review RV39-02's rule for copied data): without a workload record a
    rolled-back migration keeps copied data - its cancel cleans it up, its providers stay - as
    §9.3 counts it held: a warm migration's passes, a vmware_warm one's while its last attempt ran
    no cutover. The kit's rollback deleted its copies after one, and a vmware_cold rollback keeps
    none."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    history = _history(*phases)
    m = make_migration(
        plan_id=plan.id,
        vm=vm(1),
        strategy=strategy,
        phase=phases[-1],
        phase_history=history,
        sync_passes=_passes(passes, history),
    )
    try:
        assert await h.orch.copied_data(m) is held
    finally:
        await h.orch.stop()


@pytest.mark.parametrize(
    ("strategy", "held"), [(Strategy.vmware_warm, False), (Strategy.warm, True)]
)
async def test_an_earlier_attempts_passes_hold_no_kit_copy_of_a_later_attempt(
    tmp_path, store, strategy, held
):
    """SDD §9.3, §5.1 (reviews RV40-05, RV41B-02): attempt 1 recorded its passes and ran its
    cutover - the kit's rollback deleted its copies, the cutover's own final pass with them -,
    attempt 2's first copy was cut off with no pass recorded (the kit deletes it too): no pass
    since the kit released its copies, so it holds nothing. A warm migration's rollback keeps its
    volumes for every later attempt: its passes still hold them."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    history = _history(*CUT, P.ready, P.precopy, P.failed, P.ready)
    first_attempt = history[: len(CUT)]
    # the cutover's own final pass, recorded while it ran (review RV41B-02): released all the same
    cut_at = next(c.at for c in history if c.to_phase == P.cutover)
    final = SyncPass(
        number=3, kind="final", started_at=cut_at + timedelta(seconds=5), duration_s=30.0
    )
    m = make_migration(
        plan_id=plan.id,
        vm=vm(1),
        strategy=strategy,
        phase=P.ready,
        phase_history=history,
        sync_passes=[*_passes(2, first_attempt), final],
    )
    try:
        assert await h.orch.holds_destination_volumes(m) is held
        assert await h.orch.copied_data(m) is held
    finally:
        await h.orch.stop()


@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        ((), (P.ready, P.precopy, P.rolling_back, P.rolled_back, P.ready)),
        ((), (P.ready, P.precopy, P.failed, P.ready)),
        ((*CUT, P.ready), (P.ready, P.precopy, P.failed, P.ready)),
    ],
    ids=[
        "attempt 2 rolled back during its first run",
        "attempt 2's first run failed",
        "after a cutover's attempt, the next one's copies through a third cut off",
    ],
)
async def test_an_uncut_attempts_kit_copies_stay_through_a_later_attempt_cut_off(
    tmp_path, store, earlier, later
):
    """SDD §9.3, §7.2, §5.1 (review RV41B-02): attempt 1 completed its passes and ran no cutover -
    the kit marked its copies seamless_copied true, and its rollback kept them -, attempt 2's
    first run, a CBT sync of those copies, was cut off before it recorded a pass: the copies stay,
    so the kit still holds them - only a cutover releases them (RV40-05). The rule took attempt 2's
    passes alone and read them released: a false quota shortfall for another VM, a provider
    deleted under a rolled-back VM's copies."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    history = _history(*earlier, *UNCUT, *later)
    m = make_migration(
        plan_id=plan.id,
        vm=vm(1),
        strategy=Strategy.vmware_warm,
        phase=later[-1],
        phase_history=history,
        sync_passes=_passes(2, history[: len(earlier) + len(UNCUT)]),
    )
    try:
        assert await h.orch.holds_destination_volumes(m) is True
        assert await h.orch.copied_data(m) is True
    finally:
        await h.orch.stop()


#: a retried attempt, the strategy chosen since, the phases through now: are its copies held
RETRIED_CASES = pytest.mark.parametrize(
    ("attempted", "chosen", "phases", "held"),
    [
        (Strategy.vmware_warm, Strategy.vmware_cold, (*UNCUT, P.ready), True),
        (Strategy.warm, Strategy.cold, (*UNCUT, P.ready), True),
        (Strategy.warm, Strategy.cold, (*UNCUT, P.ready, P.validating, P.blocked), True),
        (Strategy.cold, Strategy.warm, (P.cutover, P.rolling_back, P.rolled_back, P.ready), False),
        (
            Strategy.vmware_warm,
            Strategy.vmware_cold,
            (*UNCUT, P.ready, P.cutover, P.rolling_back, P.rolled_back),
            False,
        ),
        (
            Strategy.cold,
            Strategy.warm,
            (P.cutover, P.rolling_back, P.rolled_back, P.ready, *UNCUT),
            True,
        ),
    ],
    ids=[
        "vmware_warm switched to vmware_cold: the kit's copies stay",
        "warm switched to cold, ready",
        "warm switched to cold, blocked by a validation",
        "cold switched to warm: its rollback deleted its copy",
        "the vmware_cold attempt ran and was rolled back: its rollback deleted them",
        "the warm attempt chosen after a cold one ran its passes: they hold",
    ],
)


@RETRIED_CASES
async def test_where_no_record_tells_the_retried_attempts_strategy_holds_its_copies(
    tmp_path, store, attempted, chosen, phases, held
):
    """SDD §9.3, §5.1 (review RV40-06): a Retry puts a migration back with its attempt's copies
    kept, and a validation or PUT .../strategy may choose another strategy for the next attempt;
    while it waits for that attempt the copies are the retried attempt's - the VMware kit keeps
    no record, so a vmware_warm migration switched to vmware_cold read as holding nothing. Once
    the next attempt ran, its own strategy tells."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    history = _history(*phases)
    # the passes of the warm attempt: the retried one's, or the later one's after a cold attempt
    warm = attempted in (Strategy.warm, Strategy.vmware_warm)
    m = make_migration(
        plan_id=plan.id,
        vm=vm(1),
        strategy=chosen,
        phase=phases[-1],
        phase_history=history,
        attempt_strategies=[attempted],
        sync_passes=_passes(2, history[: phases.index(P.ready)] if warm else history),
    )
    try:
        assert await h.orch.holds_destination_volumes(m) is held
        assert await h.orch.copied_data(m) is held
    finally:
        await h.orch.stop()


async def test_a_pass_that_started_as_the_attempt_after_a_cutover_began_holds_kit_copies(
    tmp_path, store
):
    """SDD §9.3 (review RV41B-04): the passes since the kit last released its copies are those
    that started once the attempt after the last cutover entered pre-copy - one that started at
    that very instant among them."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    history = _history(*CUT, P.ready, *UNCUT)
    entered = history[len(CUT) + 1].at
    assert history[len(CUT) + 1].to_phase == P.precopy
    m = make_migration(
        plan_id=plan.id,
        vm=vm(1),
        strategy=Strategy.vmware_warm,
        phase=P.rolled_back,
        phase_history=history,
        sync_passes=[SyncPass(number=1, kind="full", started_at=entered, duration_s=60.0)],
    )
    try:
        assert await h.orch.holds_destination_volumes(m) is True
    finally:
        await h.orch.stop()


async def test_a_workload_record_without_volumes_wins_over_warm_passes(tmp_path, store):
    """SDD §9.3, §5.1 (review RV40-08): the record tells, and the fallback applies only where none
    can: a record whose destination volumes are gone - a cold cutover's cleanup or rollback
    emptied it (COLD-02) - holds nothing and keeps no copied data, whatever warm passes the
    migration lists."""
    from seamless_migrate.executors.ansible import run_dir

    h, plan = await setup(tmp_path, store, [vm(1)])
    m = make_migration(
        plan_id=plan.id, vm=vm(1), strategy=Strategy.warm, phase=P.ready, sync_passes=_passes(2)
    )
    record = run_dir(h.settings, plan.id, m.id) / "osm" / "workload_warm" / "vm-1.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"dest_volumes": {}}))
    try:
        assert await h.orch.holds_destination_volumes(m) is False
        assert await h.orch.copied_data(m) is False
    finally:
        await h.orch.stop()


@pytest.mark.parametrize(
    "other_reason", [None, "admin rights are required on the source cloud"], ids=["only", "other"]
)
async def test_a_retry_takes_an_earlier_retrys_held_mark_off_once_nothing_is_held(
    tmp_path, store, other_reason
):
    """SDD §12 (review RV43B-02): a Retry marks the stored storage_handover estimate ineligible
    while an earlier attempt's destination volumes are held - and takes that mark off once none
    are: a warm attempt rolled back with its pass (held: marked), then a cold attempt - whose
    cutover removes the warm copies first and whose rollback keeps none - rolled back, and a
    second Retry. The estimate is eligible again when §9.2's held reason was its only one, and
    keeps its other reasons otherwise; PUT …/strategy judges the same."""
    from seamless_migrate.planning.selector import HELD_VOLUMES_REASON

    handover = {"enabled": True, "backend_map": {"ceph-ssd": "rhoso@ceph#rbd"}}
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"handover": handover, "auto_cutover": False, "default_strategy": Strategy.warm},
    )
    try:
        await h.orch.validate_plan(plan.id, "alice")
        m = await h.by_vm(plan.id, "vm-1")
        if other_reason is not None:
            m.estimates = [
                e.model_copy(update={"eligible": False, "reasons": [other_reason]})
                if e.strategy == Strategy.storage_handover
                else e
                for e in m.estimates
            ]
        m.phase = P.rolled_back
        m.phase_history = [*m.phase_history, *_history(*UNCUT)]
        m.sync_passes = _passes(1, m.phase_history)
        store.put("migration", m)
        paused = await h.orch._plan(plan.id)
        paused.status = PlanStatus.paused
        store.put("plan", paused)
        await h.orch.retry(m.id, "alice")
        m = await h.migration(m.id)
        if other_reason is None:  # an eligible one is marked; an ineligible one stays as it is
            marked = m.estimate_for(Strategy.storage_handover)
            assert (marked.eligible, marked.reasons) == (False, [HELD_VOLUMES_REASON])
        else:
            m.estimates = [
                e.model_copy(update={"reasons": [other_reason, HELD_VOLUMES_REASON]})
                if e.strategy == Strategy.storage_handover
                else e
                for e in m.estimates
            ]  # as a validation that saw them held stores it
            store.put("migration", m)
        await h.orch.set_strategy(m.id, Strategy.cold, "alice")
        m = await h.migration(m.id)
        m.phase = P.rolled_back
        m.phase_history = [*m.phase_history, *_history(P.cutover, P.rolling_back, P.rolled_back)]
        store.put("migration", m)
        await h.orch.retry(m.id, "alice")
        m = await h.migration(m.id)
        assert not await h.orch.holds_destination_volumes(m)
        est = m.estimate_for(Strategy.storage_handover)
        if other_reason is None:
            assert (est.eligible, est.reasons) == (True, [])
            await h.orch.set_strategy(m.id, Strategy.storage_handover, "alice")
            assert (await h.migration(m.id)).strategy == Strategy.storage_handover
        else:
            assert (est.eligible, est.reasons) == (False, [other_reason])
            with pytest.raises(BadRequest, match=other_reason):
                await h.orch.set_strategy(m.id, Strategy.storage_handover, "alice")
    finally:
        await h.orch.stop()


@pytest.mark.parametrize(
    ("record", "passes", "held"),
    [
        ({"dest_volumes": {"vol-1": "dst-vol-1"}}, 2, True),
        ({"dest_volumes": {"vol-1": "dst-vol-1"}}, 0, True),
        ({"dest_volumes": {}, "pending_snapshot": "snap-1"}, 0, False),
        (None, 0, False),
    ],
    ids=[
        "volumes held",
        "volumes held by the record alone: its first pass failed after creating them",
        "a pending source snapshot alone holds no destination volume",
        "nothing held",
    ],
)
async def test_a_strategy_change_to_a_handover_is_refused_while_an_earlier_attempt_holds_volumes(
    tmp_path, store, record, passes, held
):
    """SDD §12, §9.2, §9.3 (reviews RV40-01, RV41B-03): a Retry puts a rolled-back warm attempt
    back in ready without re-assessing the estimates the first validation stored -
    storage_handover eligible then, nothing held -, and a paused plan starts without a new
    validation: the strategy change judges the held volumes itself - its record first, which holds
    them also when the first pass failed after creating them (RV3-05) - and refuses the handover,
    which would leave them behind; cold, which removes them first, and warm, which uses them, stay
    open. A pending source snapshot alone holds no destination volume, and with nothing held the
    handover is accepted."""
    from seamless_migrate.executors.ansible import run_dir
    from seamless_migrate.planning.selector import HELD_VOLUMES_REASON

    handover = {"enabled": True, "backend_map": {"ceph-ssd": "rhoso@ceph#rbd"}}
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"handover": handover, "auto_cutover": False, "default_strategy": Strategy.warm},
    )
    try:
        await h.orch.validate_plan(plan.id, "alice")
        first = await h.by_vm(plan.id, "vm-1")
        assert first.estimate_for(Strategy.storage_handover).eligible
        # the warm attempt ran its pre-copy and was rolled back; its record tells what it keeps
        first.phase = P.rolled_back
        first.phase_history = [*first.phase_history, *_history(*UNCUT)]
        first.sync_passes = _passes(passes, first.phase_history)
        if record is not None:
            path = run_dir(h.settings, plan.id, first.id) / "osm" / "workload_warm" / "vm-1.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record))
        store.put("migration", first)
        paused = await h.orch._plan(plan.id)
        paused.status = PlanStatus.paused
        store.put("plan", paused)
        await h.orch.retry(first.id, "alice")
        if held:
            with pytest.raises(BadRequest, match=HELD_VOLUMES_REASON):
                await h.orch.set_strategy(first.id, Strategy.storage_handover, "alice")
            left = await h.migration(first.id)
            assert left.strategy == Strategy.warm
            assert (await h.plan(plan.id)).strategy_overrides == {}
            # the other strategies are not held out: cold removes the volumes first (COLD-02),
            # warm uses them
            for other in (Strategy.cold, Strategy.warm):
                assert (await h.orch.set_strategy(first.id, other, "alice")).strategy == other
        else:
            changed = await h.orch.set_strategy(first.id, Strategy.storage_handover, "alice")
            assert changed.strategy == Strategy.storage_handover
    finally:
        await h.orch.stop()


@pytest.mark.parametrize(
    ("record", "passes", "held"),
    [
        ({"dest_volumes": {"vol-1": "dst-vol-1"}}, 2, True),
        ({"dest_volumes": {"vol-1": "dst-vol-1"}}, 0, True),
        ({"dest_volumes": {}, "pending_snapshot": "snap-1"}, 0, False),
        (None, 0, False),
    ],
    ids=[
        "volumes held",
        "volumes held by the record alone",
        "a pending source snapshot alone",
        "nothing held",
    ],
)
async def test_a_retry_marks_the_handover_ineligible_while_volumes_are_held(
    tmp_path, store, record, passes, held
):
    """SDD §12, §9.2 (review 41b's lead): a Retry assesses nothing anew, but it marks the stored
    storage_handover estimate ineligible with §9.2's reason while the migration holds an earlier
    attempt's destination volumes - the dashboard offered "Use Storage handover", and only a
    refusal told why. The other estimates stay as the last validation stored them."""
    from seamless_migrate.executors.ansible import run_dir
    from seamless_migrate.planning.selector import HELD_VOLUMES_REASON

    handover = {"enabled": True, "backend_map": {"ceph-ssd": "rhoso@ceph#rbd"}}
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"handover": handover, "auto_cutover": False, "default_strategy": Strategy.warm},
    )
    try:
        await h.orch.validate_plan(plan.id, "alice")
        first = await h.by_vm(plan.id, "vm-1")
        before = {e.strategy: (e.eligible, list(e.reasons)) for e in first.estimates}
        assert before[Strategy.storage_handover] == (True, [])
        first.phase = P.rolled_back
        first.phase_history = [*first.phase_history, *_history(*UNCUT)]
        first.sync_passes = _passes(passes, first.phase_history)
        if record is not None:
            path = run_dir(h.settings, plan.id, first.id) / "osm" / "workload_warm" / "vm-1.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record))
        store.put("migration", first)
        paused = await h.orch._plan(plan.id)
        paused.status = PlanStatus.paused
        store.put("plan", paused)
        await h.orch.retry(first.id, "alice")
        after = {
            e.strategy: (e.eligible, list(e.reasons))
            for e in (await h.migration(first.id)).estimates
        }
        expected = (False, [HELD_VOLUMES_REASON]) if held else (True, [])
        assert after == {**before, Strategy.storage_handover: expected}
    finally:
        await h.orch.stop()


async def test_an_unreadable_workload_record_counts_as_volumes_held(tmp_path, store):
    """SDD §9.3 (review RV39-04): a record that cannot be read may list volumes the next attempt
    uses: it counts as holding them, whatever the strategy's fallback would say."""
    from seamless_migrate.executors.ansible import run_dir

    h, plan = await setup(tmp_path, store, [vm(1)])
    m = make_migration(plan_id=plan.id, vm=vm(1), strategy=Strategy.cold, phase=P.rolled_back)
    record = run_dir(h.settings, plan.id, m.id) / "osm" / "workload_warm" / "vm-1.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{")
    try:
        assert await h.orch.holds_destination_volumes(m) is True
    finally:
        await h.orch.stop()


@pytest.mark.parametrize(
    "held",
    ["its record holds volumes", "warm passes without a record", "nothing held"],
)
async def test_a_handover_is_ineligible_while_an_earlier_attempt_holds_its_volumes(
    tmp_path, store, held
):
    """SDD §9.2, §9.3 (review 39, lead L1): a warm attempt Retry put back in ready keeps its
    destination volumes - its workload record says so, or, with no record (the demo), its passes.
    A storage handover manages the source's volumes and never removes those, so it is ineligible
    then, saying what to run instead; with nothing held it stays eligible."""
    from seamless_migrate.executors.ansible import run_dir
    from seamless_migrate.planning.selector import HELD_VOLUMES_REASON

    handover = {"enabled": True, "backend_map": {"ceph-ssd": "rhoso@ceph#rbd"}}
    h, plan = await setup(tmp_path, store, [vm(1)], {"handover": handover, "auto_cutover": False})
    try:
        await h.orch.validate_plan(plan.id, "alice")
        first = await h.by_vm(plan.id, "vm-1")
        assert first.estimate_for(Strategy.storage_handover).eligible
        # what a rolled-back warm attempt leaves once a Retry put it back in ready
        first.strategy = Strategy.warm
        first.phase = P.ready
        if held == "its record holds volumes":
            record = run_dir(h.settings, plan.id, first.id) / "osm" / "workload_warm" / "vm-1.json"
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps({"dest_volumes": {"vol-1": "dst-vol-1"}}))
        elif held == "warm passes without a record":
            first.sync_passes = _passes(2)
        store.put("migration", first)
        await h.orch.validate_plan(plan.id, "alice")
        again = await h.by_vm(plan.id, "vm-1")
    finally:
        await h.orch.stop()
    estimate = again.estimate_for(Strategy.storage_handover)
    if held == "nothing held":
        assert estimate.eligible
    else:
        assert (estimate.eligible, estimate.reasons) == (False, [HELD_VOLUMES_REASON])
        assert again.strategy != Strategy.storage_handover


@pytest.mark.parametrize("cutover_ran", [True, False], ids=["its cutover ran", "before it"])
async def test_a_revalidation_counts_all_of_a_vmware_warm_attempt_whose_cutover_ran(
    tmp_path, store, cutover_ran
):
    """SDD §9.3, §7.2 (review RV39-02): the kit writes no workload record. A vmware_warm attempt
    whose cutover ran and then rolled back lost its kit volumes - the rollback deletes them, or
    marks them cutover - and its next attempt copies in full: it counts all of it, and web-02,
    for which one volume and 20 GiB are free besides, no longer fits. Rolled back before its
    cutover, its completed copies stay for the next pass: its server alone counts, web-02 fits."""
    dst = QuotaDestination(QUOTA_FOR_TWO)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1), vm(2)],
        {"default_strategy": Strategy.cold, "auto_cutover": False},
        destination=dst,
    )
    try:
        await h.orch.validate_plan(plan.id, "alice")
        first = await h.by_vm(plan.id, "vm-1")
        first.strategy = Strategy.vmware_warm
        first.phase = P.rolled_back
        first.phase_history = [*first.phase_history, *_history(*(CUT if cutover_ran else UNCUT))]
        first.sync_passes = _passes(2, first.phase_history)
        store.put("migration", first)
        dst.free = dict(QUOTA_ONE_VOLUME_HELD)
        report = await h.orch.validate_plan(plan.id, "alice")
    finally:
        await h.orch.stop()
    second = next(i for i in report.migrations if i.vm_name == "web-02")
    blocked = [f.code for f in second.findings if f.code == "DST_QUOTA_INSUFFICIENT"]
    assert blocked == (["DST_QUOTA_INSUFFICIENT"] if cutover_ran else [])


@pytest.mark.parametrize("calibrated_mib", [None, 200], ids=["planned S", "S calibrated low"])
async def test_a_link_bound_delta_pass_leaves_the_slo_rule_converging(
    tmp_path, store, calibrated_mib
):
    """SDD §5.3, §9.1 (review RV38A-02): after a delta pass the link bound - the guest wrote 40 GiB
    in the 1000 s between the snapshots, sent in 327.68 s at 125 MiB/s, its 100 GiB scan 204.8 s -
    the calibration keeps the planned scan rate, so the final downtime stays within the 500 s SLO
    and the rule converges by slo: no extra pass for a pass that ran as predicted. Judged at the
    migration's own S: one an earlier pass calibrated low (200 MiB/s, a scan of 512 s the pass
    could not have lasted) is raised to what the pass reads, 312.5 MiB/s."""
    from seamless_migrate.domain.models import SyncPass
    from seamless_migrate.planning.estimator import EstimatorParams, scan_seconds

    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm, "downtime_slo_s": 500}
    )
    the_vm = make_vm(disks=[make_disk(id="d0", size_gb=100, used_gb=60.0)], change_rate_bps=None)
    first = SyncPass(
        number=1,
        kind="full",
        started_at=utcnow(),
        duration_s=521.5,
        bytes_scanned=the_vm.disk_bytes,
        bytes_changed=the_vm.used_bytes,
        bytes_transferred=the_vm.used_bytes,
    )
    params = EstimatorParams()
    sent = 40 * 2**30
    second = SyncPass(
        number=2,
        kind="delta",
        started_at=first.started_at + timedelta(seconds=1000),
        duration_s=max(scan_seconds(the_vm, params), sent / params.link_bps),
        bytes_scanned=the_vm.disk_bytes,
        bytes_changed=sent,
        bytes_transferred=sent,
    )
    m = make_migration(plan_id=plan.id, phase=P.syncing, vm=the_vm, sync_passes=[first, second])
    m.observed_scan_bps = calibrated_mib * 2**20 if calibrated_mib else None
    h.orch._calibrate(m, plan, second)
    if calibrated_mib is None:
        assert m.observed_scan_bps is None
        assert h.orch._converged(m, plan)[0] == "slo"
    else:
        assert m.observed_scan_bps == pytest.approx(312.5 * 2**20)


async def test_a_scan_bound_delta_pass_lowers_an_optimistic_scan_rate_before_the_slo_rule(
    tmp_path, store
):
    """SDD §5.3, §9.1 (review RV39-01): the storage scans at 250 MiB/s where the plan says 500. A
    delta pass that changed 40 GiB lasts its 409.6 s scan, longer than its 327.68 s over the link:
    the scan bound it, so the calibration takes 250 MiB/s - although the model's scan at the
    planned S (204.8 s) is shorter than the transfer - and the final downtime, about 680 s, keeps
    the SLO rule from converging on a 500 s SLO."""
    from seamless_migrate.domain.models import SyncPass
    from seamless_migrate.planning.estimator import EstimatorParams

    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm, "downtime_slo_s": 500}
    )
    the_vm = make_vm(disks=[make_disk(id="d0", size_gb=100, used_gb=60.0)], change_rate_bps=None)
    first = SyncPass(
        number=1,
        kind="full",
        started_at=utcnow(),
        duration_s=521.5,
        bytes_scanned=the_vm.disk_bytes,
        bytes_changed=the_vm.used_bytes,
        bytes_transferred=the_vm.used_bytes,
    )
    params = EstimatorParams()
    sent = 40 * 2**30
    second = SyncPass(
        number=2,
        kind="delta",
        started_at=first.started_at + timedelta(seconds=1000),
        duration_s=max(the_vm.disk_bytes / (250 * 2**20), sent / params.link_bps),
        bytes_scanned=the_vm.disk_bytes,
        bytes_changed=sent,
        bytes_transferred=sent,
    )
    m = make_migration(plan_id=plan.id, phase=P.syncing, vm=the_vm, sync_passes=[first, second])
    try:
        h.orch._calibrate(m, plan, second)
        assert m.observed_scan_bps == pytest.approx(250 * 2**20)
        assert h.orch._converged(m, plan)[0] != "slo"
    finally:
        await h.orch.stop()


async def test_validate_creates_migrations_with_findings_and_estimates(tmp_path, store):
    gpu = vm(3, "gpu-01", flavor_extra_specs={"resources:VGPU": "1"})
    vms = [vm(1), vm(2, "db-02", size_gb=600, used_gb=300.0), gpu]
    h, plan = await setup(tmp_path, store, vms)
    report = await h.orch.validate_plan(plan.id, "alice")
    assert report.plan_id == plan.id and report.ok is False
    assert len(report.migrations) == 3
    by_name = {item.vm_name: item for item in report.migrations}
    assert by_name["gpu-01"].phase == P.blocked
    # blocked until its plan maps its vGPU, and told of its guest's driver and license (SDD §22.5)
    assert [f.code for f in by_name["gpu-01"].findings] == [
        "VM_VGPU",
        "GUEST_GPU_DRIVER",
        "GUEST_GPU_LICENSE",
    ]
    assert all(not e.eligible for e in by_name["gpu-01"].estimates)
    web = await h.by_vm(plan.id, "vm-1")
    assert web.phase == P.ready and web.wave_id is None
    assert {e.strategy for e in web.estimates} == {
        Strategy.cold,
        Strategy.warm,
        Strategy.storage_handover,
    }
    assert web.estimate is not None and web.estimate.strategy == web.strategy
    assert not web.estimate_for(Strategy.storage_handover).eligible
    assert h.history(web) == [P.pending, P.validating, P.ready]
    assert (await h.plan(plan.id)).status == PlanStatus.validated
    kinds = h.kinds()
    assert kinds.count("migration.created") == 3 and "plan.validated" in kinds

    # re-validation reuses the migrations and refreshes them
    again = await h.orch.validate_plan(plan.id, "alice")
    assert {i.migration_id for i in again.migrations} == {i.migration_id for i in report.migrations}
    assert len(await h.migrations(plan.id)) == 3
    with pytest.raises(NotFound):
        await h.orch.validate_plan("plan-unknown0", "alice")


async def test_validation_judges_a_network_with_the_roles_its_check_just_read(tmp_path, store):
    """SDD §9.3, §10: NET_PORT_NOT_PERMITTED judges the roles the destination's check reports at
    this validation, not the capabilities an earlier check stored."""

    class MemberDestination(StaticDestination):
        """A member's token; its app-net belongs to another project, which shares it."""

        async def check(self):
            return {**await super().check(), "roles": ["member", "reader"]}

        async def inventory(self):
            shared = {
                "id": "net-app",
                "name": "app-net",
                "mtu": 1500,
                "project_id": "project-admin",
                "own": False,
                "shared": True,
                "external": False,
            }
            subnet = {
                "name": "app-v4",
                "network": "app-net",
                "network_id": "net-app",
                "cidr": "10.0.0.0/24",
            }
            return dataclasses.replace(
                await super().inventory(), network_details=[shared], subnets=[subnet]
            )

    h, plan = await setup(tmp_path, store, [vm(1)], destination=MemberDestination())
    # an earlier check stored an admin token's roles
    store.put(
        "provider",
        DST_PROVIDER.model_copy(update={"capabilities": {"admin": True, "roles": ["admin"]}}),
    )
    await h.orch.validate_plan(plan.id, "alice")
    web = await h.by_vm(plan.id, "vm-1")
    assert [f.code for f in web.findings] == ["NET_PORT_NOT_PERMITTED"]
    assert "'app-net' (project project-admin)" in web.findings[0].message
    assert web.phase == P.blocked


async def test_validation_records_resolved_flavor_mapping(tmp_path, store):
    # StaticDestination offers one flavor, m1.small (64 vCPUs): an unmapped flavor resolves to it
    custom = vm(1, flavor="custom.4x8", vcpus=4, ram_mb=8192)
    h, plan = await setup(tmp_path, store, [custom, vm(2)])
    await h.orch.validate_plan(plan.id, "alice")
    resolved = await h.by_vm(plan.id, "vm-1")
    assert resolved.resolved_mappings.flavors == {"custom.4x8": "m1.small"}
    assert [f.code for f in resolved.findings] == ["MAP_FLAVOR_AUTO"]
    assert resolved.phase == P.ready  # info findings never block
    same_name = await h.by_vm(plan.id, "vm-2")
    assert same_name.resolved_mappings.flavors == {} and same_name.findings == []
    # an explicit mapping added later replaces the automatic one on re-validation
    plan = await h.plan(plan.id)
    plan.mappings.flavors["custom.4x8"] = "m1.small"
    store.put("plan", plan)
    await h.orch.validate_plan(plan.id, "alice")
    assert (await h.by_vm(plan.id, "vm-1")).resolved_mappings.flavors == {}


async def test_retry_after_rollback_counts_an_attempt(tmp_path, store):
    """A retry after the automatic rollback is a new attempt: the simulated executor seeds
    its failure injection with ``attempts``, so the same cutover must not fail forever."""
    from seamless_migrate.domain.models import Migration

    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    m.phase = P.rolled_back
    m.downtime_started_at = h.orch.now() - timedelta(seconds=600)
    m.downtime_ended_at = h.orch.now()
    m.actual_downtime_s = 600.0
    store.put("migration", m)
    again = await h.orch.retry(m.id, "alice")
    assert again.phase == P.ready and again.attempts == 1
    stored = store.get("migration", m.id, Migration)
    assert stored.attempts == 1
    # the previous attempt's downtime clock is not inherited by the next cutover
    assert stored.downtime_started_at is None and stored.downtime_ended_at is None
    assert stored.actual_downtime_s is None


async def test_a_retry_clears_the_earlier_attempts_data_issues_and_convergence_rule(
    tmp_path, store
):
    """SDD §5.1 (ADV-16..20 audit, F7): what an attempt found about itself - the invariants it
    broke, the rule that ended its passes - goes with it; the new attempt records its own."""
    from seamless_migrate.domain.models import Migration

    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    m.phase = P.rolled_back
    m.data_issues = ["DOWNTIME_BELOW_FINAL_SYNC: 0 s of downtime around a 100 s final sync"]
    m.converged_by = "slo"
    store.put("migration", m)
    again = await h.orch.retry(m.id, "alice")
    stored = store.get("migration", m.id, Migration)
    for retried in (again, stored):
        assert (retried.phase, retried.data_issues, retried.converged_by) == (P.ready, [], None)


async def test_retry_after_failed_cutover_keeps_clock_and_refuses_cancel(tmp_path, store):
    """SDD §5.1/§5.2: a cutover failed with the source stopped (no automatic rollback). A retry
    keeps the open downtime clock — the next attempt's downtime counts from the first stop — and
    the migration cannot be cancelled, before or after the retry, while the source is stopped."""
    from seamless_migrate.domain.models import Migration

    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    stopped = h.orch.now() - timedelta(seconds=900)
    m.phase = P.failed
    m.downtime_started_at = stopped
    store.put("migration", m)
    with pytest.raises(NotAllowed, match="source VM is stopped"):
        await h.orch.cancel(m.id, "bayu", "abandon")
    again = await h.orch.retry(m.id, "bayu")
    assert again.phase == P.ready and again.downtime_started_at == stopped
    stored = store.get("migration", m.id, Migration)
    assert stored.downtime_started_at == stopped and stored.downtime_ended_at is None
    with pytest.raises(NotAllowed, match="source VM is stopped"):
        await h.orch.cancel(m.id, "bayu", "abandon")
    assert store.get("migration", m.id, Migration).phase == P.ready


async def test_retry_clears_the_cutover_request_and_its_window_bypass(tmp_path, store):
    """SDD §5.1/§5.4: a retry clears the cutover request and the window bypass granted with it
    (force_window), so a bypass never carries over to the next attempt; the approvals stay, as
    the assessment did not change."""
    from seamless_migrate.domain.models import Approval, Migration

    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    m.approvals = [Approval(actor="ana", at=h.orch.now(), comment="after the backup")]
    m.cutover_requested = True
    m.force_window = True
    m.phase = P.failed
    store.put("migration", m)
    again = await h.orch.retry(m.id, "bayu")
    assert again.phase == P.ready
    stored = store.get("migration", m.id, Migration)
    assert stored.cutover_requested is False
    assert stored.force_window is False
    assert [a.actor for a in stored.approvals] == ["ana"]


async def test_a_retry_never_writes_a_ready_migration_that_carries_the_old_request(tmp_path, store):
    """SDD §5.1: everything a retry clears is saved in the same write as the move to ready. A
    write that left a ready migration with the earlier attempt's request, its requester and the
    window bypass — the last write before a crash or a store error — would let the next tick cut
    it over outside its window without a new request."""
    from seamless_migrate.domain.models import ForceCutover

    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    m.cutover_requested = True
    m.cutover_requested_by = "sari"
    m.force_window = True
    m.force_cutover = ForceCutover(
        actor="rina",
        at=h.orch.now(),
        reason="the window closes",
        delta_bytes=2**30,
        estimated_downtime_s=320.0,
    )
    m.error = "cutover: conversion host unreachable"
    m.phase = P.failed
    store.put("migration", m)
    written: list[Migration] = []
    save, write_all = h.orch._save, h.orch.store.put_all_with_events

    async def recording_save(doc, version):
        if doc.id == m.id:
            written.append(doc.model_copy(deep=True))
        return await save(doc, version)

    def recording_write(writes, appended):  # a transition: the migration with its events
        written.extend(d.model_copy(deep=True) for k, d, _ in writes if k == "migration")
        return write_all(writes, appended)

    h.orch._save = recording_save
    h.orch.store.put_all_with_events = recording_write
    again = await h.orch.retry(m.id, "bayu")
    assert again.phase == P.ready
    assert [w.phase for w in written] == [P.ready]  # one write
    [ready] = written
    assert (ready.cutover_requested, ready.cutover_requested_by, ready.force_window) == (
        False,
        None,
        False,
    )
    assert (ready.force_cutover, ready.error) == (None, None)


def _park_vms(h, monkeypatch):
    """Make the orchestrator's next source inventory reads wait until released."""
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.orch._vms

    async def parked(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(h.orch, "_vms", parked)
    return entered, release


async def test_start_waits_for_auto_waves_instead_of_being_overwritten(
    tmp_path, store, monkeypatch
):
    """SDD §8: status changes run under the plan's lock and re-check the fresh copy. A start
    issued while auto-waves reads the inventory waits for it and then sees the plan back in
    draft; it never runs a plan that auto-waves then turns into a draft."""
    from seamless_migrate.domain.models import Plan

    h, plan = await setup(tmp_path, store, [vm(1), vm(2)])
    await h.orch.validate_plan(plan.id, "alice")
    entered, release = _park_vms(h, monkeypatch)
    waves = asyncio.create_task(h.orch.auto_waves(plan.id, 5, "alice"))
    await entered.wait()
    start = asyncio.create_task(h.orch.start_plan(plan.id, "bayu"))
    for _ in range(10):
        await asyncio.sleep(0)  # the start runs as far as it can meanwhile
    release.set()
    await waves
    with pytest.raises(NotAllowed, match="validate it first"):
        await start
    assert store.get("plan", plan.id, Plan).status == PlanStatus.draft


async def test_start_waits_for_a_running_validation(tmp_path, store, monkeypatch):
    """SDD §8: a start issued while the plan is validated again waits for the validation, so the
    validation never marks the started plan validated."""
    from seamless_migrate.domain.models import Plan

    h, plan = await setup(tmp_path, store, [vm(1), vm(2)])
    await h.orch.validate_plan(plan.id, "alice")
    entered, release = _park_vms(h, monkeypatch)
    validating = asyncio.create_task(h.orch.validate_plan(plan.id, "alice"))
    await entered.wait()
    start = asyncio.create_task(h.orch.start_plan(plan.id, "bayu"))
    for _ in range(10):
        await asyncio.sleep(0)
    release.set()
    await validating
    assert (await start).status == PlanStatus.running
    assert store.get("plan", plan.id, Plan).status == PlanStatus.running


async def test_validate_cancels_a_removed_vms_failed_migration(tmp_path, store):
    """SDD §5.4: validation cancels the migrations of VMs removed from vm_ids, a failed one too
    (its source never stopped): left failed it would hold the VM and keep the plan from
    completing."""
    from seamless_migrate.domain.models import Migration, Plan

    h, plan = await setup(tmp_path, store, [vm(1), vm(2)])
    await h.orch.validate_plan(plan.id, "alice")
    dropped = await h.by_vm(plan.id, "vm-2")
    dropped.phase = P.failed  # a pre-copy failed; the source VM kept running
    store.put("migration", dropped)
    current = store.get("plan", plan.id, Plan)
    current.vm_ids = ["vm-1"]
    store.put("plan", current)
    await h.orch.validate_plan(plan.id, "alice")
    assert store.get("migration", dropped.id, Migration).phase == P.cancelled


async def test_a_validation_leaves_a_removed_vms_migration_that_moved_meanwhile_and_finishes(
    tmp_path, store
):
    """SDD §5.4: an operator rolls a removed VM's failed migration back while the validation
    re-assesses the plan's VMs; it can no longer be cancelled — the validation leaves it as it
    is, cancels the other removed VM's migration, still finishes, and names it in kept."""
    from seamless_migrate.domain.models import Migration, Plan

    h, plan = await setup(tmp_path, store, [vm(1), vm(2), vm(3)])
    await h.orch.validate_plan(plan.id, "alice")
    moved, dropped = [await h.by_vm(plan.id, v) for v in ("vm-2", "vm-3")]
    for m in (moved, dropped):
        m.phase = P.failed  # a pre-copy failed; the source VM kept running
        store.put("migration", m)
    current = store.get("plan", plan.id, Plan)
    current.vm_ids = ["vm-1"]
    store.put("plan", current)
    gate, entered = asyncio.Event(), asyncio.Event()
    apply_validation = h.orch._apply_validation

    async def slow_apply_validation(*args, **kwargs):
        # the removed VMs are known; the plan's VMs are being re-assessed (minutes of provider,
        # Jev and memory calls in a real plan)
        entered.set()
        await gate.wait()
        return await apply_validation(*args, **kwargs)

    h.orch._apply_validation = slow_apply_validation
    validation = asyncio.create_task(h.orch.validate_plan(plan.id, "alice"))
    await asyncio.wait_for(entered.wait(), 5)
    await h.orch.rollback(moved.id, "bayu", "keep the source running")  # meanwhile
    gate.set()
    report = await validation
    await h.orch.stop()
    assert [item.vm_name for item in report.migrations] == ["web-01"]
    assert store.get("migration", dropped.id, Migration).phase == P.cancelled
    kept_phase = store.get("migration", moved.id, Migration).phase
    assert kept_phase in (P.rolling_back, P.rolled_back)
    assert store.get("plan", plan.id, Plan).status == PlanStatus.validated
    validated = [e for e in store.events(since_seq=0, limit=100000) if e.kind == "plan.validated"]
    [kept] = validated[-1].data["kept"]
    assert kept["vm"] == "web-02" and kept["phase"] in ("rolling_back", "rolled_back")


async def test_a_validation_leaves_a_removed_vms_migration_rolled_back_meanwhile(tmp_path, store):
    """SDD §5.4: an operator may cancel a rolled-back migration (§5.1), but the validation does not:
    a removed VM's migration whose rollback finished while the validation re-assessed the plan stays
    rolled back - the operator's move stands - and is named in kept."""
    from seamless_migrate.domain.models import Migration, Plan

    h, plan = await setup(tmp_path, store, [vm(1), vm(2)])
    await h.orch.validate_plan(plan.id, "alice")
    moved = await h.by_vm(plan.id, "vm-2")
    moved.phase = P.failed  # a pre-copy failed; the source VM kept running
    store.put("migration", moved)
    current = store.get("plan", plan.id, Plan)
    current.vm_ids = ["vm-1"]
    store.put("plan", current)
    gate, entered = asyncio.Event(), asyncio.Event()
    apply_validation = h.orch._apply_validation

    async def slow_apply_validation(*args, **kwargs):
        entered.set()
        await gate.wait()
        return await apply_validation(*args, **kwargs)

    h.orch._apply_validation = slow_apply_validation
    validation = asyncio.create_task(h.orch.validate_plan(plan.id, "alice"))
    await asyncio.wait_for(entered.wait(), 5)
    await h.orch.rollback(moved.id, "bayu", "keep the source running")
    await h.wait_phase(moved.id, P.rolled_back)  # the rollback finished before the cancel
    gate.set()
    await validation
    await h.orch.stop()
    assert store.get("migration", moved.id, Migration).phase == P.rolled_back
    validated = [e for e in store.events(since_seq=0, limit=100000) if e.kind == "plan.validated"]
    assert validated[-1].data["kept"] == [{"vm": "web-02", "phase": "rolled_back"}]


async def _left_validating(tmp_path, store):
    """A plan of two VMs that an earlier version's validation left as it was when it failed
    between vm-2's two writes (a database error, a restart): vm-2 in validating, the plan not
    validated (SDD §5.4). Validation now writes each migration once, so the state is seeded."""
    h, plan = await setup(tmp_path, store, [vm(1), vm(2)])
    await h.orch.validate_plan(plan.id, "alice")
    stuck = await h.by_vm(plan.id, "vm-2")
    stuck.phase = P.validating
    stuck.phase_history = stuck.phase_history[:2]
    store.put("migration", stuck)
    left = await h.plan(plan.id)
    left.status = PlanStatus.draft
    store.put("plan", left)
    assert h.history(await h.migration(stuck.id)) == [P.pending, P.validating]
    return h, plan, stuck


async def test_a_validation_takes_over_a_migration_an_interrupted_one_left_validating(
    tmp_path, store
):
    """SDD §5.4: an earlier version's validation whose second write failed (a database error, a
    restart) left a migration in validating; the next validation of the plan re-assesses it like a
    ready one, so the plan can start and the VM is not held for good."""
    h, plan, stuck = await _left_validating(tmp_path, store)
    report = await h.orch.validate_plan(plan.id, "alice")
    assert {item.migration_id: item.phase for item in report.migrations}[stuck.id] == P.ready


async def test_a_validation_reads_the_source_vm_of_a_migration_left_validating(tmp_path, store):
    """SDD §5.4 (review PL-09): a migration an interrupted validation left in validating is
    re-assessed, so its VM is read from the source again - a disk added since counts -, never taken
    as recorded as a finished migration's is."""
    h, plan, stuck = await _left_validating(tmp_path, store)
    grown = h.source.vms["vm-2"]
    extra = make_disk(id="vol-2b", size_gb=50, used_gb=5.0)
    h.source.vms["vm-2"] = grown.model_copy(update={"disks": [*grown.disks, extra]})
    await h.orch.validate_plan(plan.id, "alice")
    taken = await h.migration(stuck.id)
    assert (taken.phase, [d.id for d in taken.vm.disks]) == (P.ready, ["vol-2", "vol-2b"])
    taken = await h.migration(stuck.id)
    assert taken.phase == P.ready
    # taken over as it was: one transition, written once
    assert h.history(taken) == [P.pending, P.validating, P.ready]
    assert (await h.plan(plan.id)).status == PlanStatus.validated


async def test_a_validation_writes_each_migration_once_with_the_same_events(tmp_path, store):
    """SDD §5.4 (control-plane perf audit CP-P6): a new migration is created already assessed - its
    history pending -> validating -> ready or blocked, in one write - and an existing one takes
    both transitions in one write; the events stay migration.created for a new one and one
    migration.phase per transition, in that order."""
    gpu = vm(3, "gpu-01", flavor_extra_specs={"resources:VGPU": "1"})
    h, plan = await setup(tmp_path, store, [vm(1), gpu])

    def phase_events(m):
        return [
            (e.kind, e.data.get("from"), e.data.get("to"))
            for e in store.events(migration_id=m.id, limit=1000)
        ]

    await h.orch.validate_plan(plan.id, "alice")
    expected = {"vm-1": P.ready, "vm-3": P.blocked}
    for vm_id, last in expected.items():
        m = await h.by_vm(plan.id, vm_id)
        assert store.get_versioned("migration", m.id, Migration)[1] == 1, vm_id
        assert h.history(m) == [P.pending, P.validating, last]
        assert phase_events(m) == [
            ("migration.created", None, None),
            ("migration.phase", "pending", "validating"),
            ("migration.phase", "validating", str(last)),
        ]
        created, validating, assessed = store.events(migration_id=m.id, limit=1000)
        assert created.data == {"vm": m.vm.name, "source_id": vm_id, "strategy": str(m.strategy)}
        assert created.message == f"migration of {m.vm.name} created"
        assert validating.data == {
            "from": "pending",
            "to": "validating",
            "reason": "pre-flight validation",
        }
        assert validating.message == f"{m.vm.name}: pending -> validating (pre-flight validation)"
        reason = m.phase_history[-1].reason
        extra = {"findings": ["VM_VGPU"]} if last == P.blocked else {"strategy": str(m.strategy)}
        assert assessed.data == {"from": "validating", "to": str(last), "reason": reason, **extra}
        assert assessed.message == f"{m.vm.name}: validating -> {last} ({reason})"
        assert {e.actor for e in (created, validating, assessed)} == {"alice"}
    await h.orch.validate_plan(plan.id, "alice")
    for vm_id, last in expected.items():
        m = await h.by_vm(plan.id, vm_id)
        assert store.get_versioned("migration", m.id, Migration)[1] == 2, vm_id
        assert h.history(m) == [P.pending, P.validating, last, P.validating, last]
        assert phase_events(m)[3:] == [
            ("migration.phase", str(last), "validating"),
            ("migration.phase", "validating", str(last)),
        ]


async def test_a_validation_runs_four_statements_per_vm(tmp_path):
    """SDD §5.4, §11 (CP-P6): per VM, a first validation inserts the migration and its three events,
    and a validation again reads it, updates it and inserts its two events - four statements each,
    where it was ten and seven. Each size gets its own store, so no other plan is read."""
    import sqlalchemy as sa

    seen: list[str] = []

    def count(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    async def statements(n):
        own = Store(f"sqlite:///{tmp_path / f'n{n}.db'}")
        own.create_schema()
        h, plan = await setup(tmp_path / f"n{n}", own, [vm(i) for i in range(1, n + 1)])
        seen.clear()
        sa.event.listen(own.engine, "before_cursor_execute", count)
        try:
            await h.orch.validate_plan(plan.id, "alice")
            first = len(seen)
            seen.clear()
            await h.orch.validate_plan(plan.id, "alice")
            again = len(seen)
        finally:
            sa.event.remove(own.engine, "before_cursor_execute", count)
            own.dispose()
        return first, again

    (first4, again4), (first8, again8) = await statements(4), await statements(8)
    assert ((first8 - first4) / 4, (again8 - again4) / 4) == (4, 4)


async def test_a_failed_validation_write_leaves_no_migration_in_validating(tmp_path, store):
    """SDD §5.4: the one write lands or not - a database error leaves a new VM without a migration
    and an existing migration as it was, never in validating."""
    h, plan = await setup(tmp_path, store, [vm(1), vm(2)])
    write_all = h.orch.store.put_all_with_events

    def failing_write(writes, appended):
        # the write that lands the assessment (an earlier version wrote validating before it)
        if any(
            kind == "migration" and model.vm.source_id == "vm-2" and model.phase != P.validating
            for kind, model, _ in writes
        ):
            raise ConnectionError("database connection lost")
        return write_all(writes, appended)

    h.orch.store.put_all_with_events = failing_write
    with pytest.raises(ConnectionError):
        await h.orch.validate_plan(plan.id, "alice")
    assert [m.vm.source_id for m in await h.migrations(plan.id)] == ["vm-1"]
    h.orch.store.put_all_with_events = write_all
    await h.orch.validate_plan(plan.id, "alice")
    ready = await h.by_vm(plan.id, "vm-2")
    _, version = store.get_versioned("migration", ready.id, Migration)
    h.orch.store.put_all_with_events = failing_write
    with pytest.raises(ConnectionError):
        await h.orch.validate_plan(plan.id, "alice")
    kept, same = store.get_versioned("migration", ready.id, Migration)
    assert (kept.phase, same, h.history(kept)) == (P.ready, version, h.history(ready))


async def test_validation_cancels_a_removed_vms_leftover_in_validating(tmp_path, store):
    """SDD §5.1/§5.4: once its VM left vm_ids, a migration an interrupted validation left in
    validating is cancelled by the next validation - otherwise it held the VM for good, with no
    action to release it - and another plan can take the VM."""
    h, plan, stuck = await _left_validating(tmp_path, store)
    edited = await h.plan(plan.id)
    edited.vm_ids = ["vm-1"]
    store.put("plan", edited)
    await h.orch.validate_plan(plan.id, "alice")
    left = await h.migration(stuck.id)
    assert left.phase == P.cancelled
    assert left.phase_history[-1].reason == "cancelled: removed from the plan"
    other = plan_for([vm(2)], name="Second wave")
    store.put("plan", other)
    report = await h.orch.validate_plan(other.id, "alice")
    assert [item.phase for item in report.migrations] == [P.ready], "the VM is free again"


async def test_the_cancel_action_refuses_a_migration_in_validating(tmp_path, store):
    """SDD §5.1: a validation may be moving a migration in validating, so the cancel action
    refuses it and says to validate the plan again."""
    h, plan, stuck = await _left_validating(tmp_path, store)
    with pytest.raises(NotAllowed, match="validate the plan again"):
        await h.orch.cancel(stuck.id, "alice")
    assert (await h.migration(stuck.id)).phase == P.validating


async def test_validate_cleans_up_a_removed_vms_warm_data_path(tmp_path, store):
    """SDD §5.1/§5.4: validation cancels a removed VM's failed migration through cancel(), so a
    warm one that recorded passes gets the same cleanup as any cancel with a data path."""
    settings = make_settings(tmp_path)
    rollbacks: list[dict] = []

    async def rollback(ctx):
        rollbacks.append(dict(ctx.options))
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(settings, hooks={StepName.ROLLBACK: rollback})
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1), vm(2)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    await h.orch.validate_plan(plan.id, "alice")
    dropped = await h.by_vm(plan.id, "vm-2")
    now = datetime.now(UTC)
    dropped.phase = P.failed  # a delta pass failed after a full one; the source VM kept running
    dropped.sync_passes = [SyncPass(number=1, kind="full", started_at=now, ended_at=now)]
    store.put("migration", dropped)
    current = store.get("plan", plan.id, Plan)
    current.vm_ids = ["vm-1"]
    store.put("plan", current)

    await h.orch.validate_plan(plan.id, "alice")
    assert store.get("migration", dropped.id, Migration).phase == P.cancelled
    assert await _settled(h, dropped.id) == ["cancel", "cleanup"]
    assert rollbacks == [{"delete_dest_volumes": True}]


@pytest.mark.parametrize("ended", [False, True])
async def test_validate_refuses_to_drop_a_failed_migration_that_stopped_its_source(
    tmp_path, store, ended
):
    """SDD §5.1/§5.4: a failed migration whose cutover stopped the source cannot be cancelled,
    so validation refuses to drop its VM before it changes anything: roll it back first."""
    from seamless_migrate.domain.models import Migration, Plan

    h, plan = await setup(tmp_path, store, [vm(1), vm(2)])
    await h.orch.validate_plan(plan.id, "alice")
    kept, dropped = await h.by_vm(plan.id, "vm-1"), await h.by_vm(plan.id, "vm-2")
    dropped.phase = P.failed
    dropped.downtime_started_at = h.orch.now() - timedelta(seconds=300)
    if ended:
        dropped.downtime_ended_at = h.orch.now() - timedelta(seconds=60)
    store.put("migration", dropped)
    current = store.get("plan", plan.id, Plan)
    current.vm_ids = ["vm-1"]
    store.put("plan", current)
    with pytest.raises(NotAllowed, match="was stopped" if ended else "source VM is stopped"):
        await h.orch.validate_plan(plan.id, "alice")
    assert store.get("migration", dropped.id, Migration).phase == P.failed
    # refused before anything changed: the kept VM was not validated again
    untouched = store.get("migration", kept.id, Migration)
    assert len(untouched.phase_history) == len(kept.phase_history)


async def test_validate_refuses_to_drop_a_vm_whose_source_is_stopped(tmp_path, store):
    """Validation cancels the VMs removed from ``vm_ids``; one whose source is stopped (a retried
    cutover) cannot be cancelled (SDD §5.1), so validation refuses before it changes anything."""
    from seamless_migrate.domain.models import Migration, Plan

    h, plan = await setup(tmp_path, store, [vm(1), vm(2)])
    await h.orch.validate_plan(plan.id, "alice")
    kept, dropped = await h.by_vm(plan.id, "vm-1"), await h.by_vm(plan.id, "vm-2")
    dropped.downtime_started_at = h.orch.now() - timedelta(seconds=300)
    store.put("migration", dropped)
    current = store.get("plan", plan.id, Plan)
    current.vm_ids = ["vm-1"]
    store.put("plan", current)
    with pytest.raises(NotAllowed, match="source VM is stopped"):
        await h.orch.validate_plan(plan.id, "alice")
    assert store.get("migration", dropped.id, Migration).phase == P.ready
    untouched = store.get("migration", kept.id, Migration)
    assert len(untouched.phase_history) == len(kept.phase_history)


async def test_validate_refuses_repeated_vm_ids(tmp_path, store):
    """A plan written before the API check (or by `plan apply`) that repeats a VM: validation
    refuses and no migration is created (SDD §5.4)."""
    from seamless_migrate.domain.models import Plan

    h, plan = await setup(tmp_path, store, [vm(1)])
    current = store.get("plan", plan.id, Plan)
    current.vm_ids = ["vm-1", "vm-1"]
    store.put("plan", current)
    with pytest.raises(BadRequest, match="more than once"):
        await h.orch.validate_plan(plan.id, "alice")
    assert await h.migrations(plan.id) == []


async def test_validate_refuses_invalid_verification_settings(tmp_path, store):
    """A stored plan with a port outside 1-65535 (written before the API check, or by `plan
    apply`) is refused at validation rather than failing every verification (SDD §12)."""
    from seamless_migrate.domain.models import Plan

    h, plan = await setup(tmp_path, store, [vm(1)])
    current = store.get("plan", plan.id, Plan)
    current.verification.tcp_ports = [22, 70000]
    store.put("plan", current)
    with pytest.raises(BadRequest, match="70000"):
        await h.orch.validate_plan(plan.id, "alice")
    assert await h.migrations(plan.id) == []


async def test_validate_refuses_an_infinite_link_bps(tmp_path, store):
    """A stored plan with link_bps = inf (written on SQLite before the API check, which the store
    now refuses too, SDD §11) still loads, and validation refuses it instead of estimating every
    transfer as instant (SDD §9.1)."""
    import sqlalchemy as sa

    from seamless_migrate.domain.models import Plan
    from seamless_migrate.store import documents

    h, plan = await setup(tmp_path, store, [vm(1)])
    data = {**store.get("plan", plan.id, Plan).model_dump(mode="json"), "link_bps": float("inf")}
    with store.engine.begin() as conn:  # as an earlier version wrote it: Infinity in the JSON
        conn.execute(
            sa.update(documents)
            .where(documents.c.kind == "plan", documents.c.id == plan.id)
            .values(data=data)
        )
    assert store.get("plan", plan.id, Plan).link_bps == float("inf")
    with pytest.raises(BadRequest, match="link_bps must be a finite positive number"):
        await h.orch.validate_plan(plan.id, "alice")
    assert await h.migrations(plan.id) == []


async def test_concurrent_validations_do_not_duplicate_migrations(tmp_path, store):
    h, plan = await setup(tmp_path, store, [vm(1), vm(2), vm(3)])
    reports = await asyncio.gather(
        h.orch.validate_plan(plan.id, "alice"), h.orch.validate_plan(plan.id, "bob")
    )
    assert all(len(r.migrations) == 3 for r in reports)
    assert len(await h.migrations(plan.id)) == 3


async def test_validate_refuses_a_vm_another_plan_holds(tmp_path, store):
    """SDD §5.4: one VM, one migration across plans. A second plan with a VM that a migration of
    the first plan holds is refused, naming that plan and the phase, and nothing is created."""
    h, first = await setup(tmp_path, store, [vm(1), vm(2), vm(3)], {"vm_ids": ["vm-1", "vm-2"]})
    await h.orch.validate_plan(first.id, "alice")
    second = plan_for([vm(2), vm(3)], name="Second wave")
    store.put("plan", second)

    with pytest.raises(NotAllowed, match=r"web-02.*Finance.*ready"):
        await h.orch.validate_plan(second.id, "bob")
    assert await h.migrations(second.id) == []


async def test_two_processes_cannot_claim_one_vm_at_once(tmp_path, any_store):
    """SDD §5.4: the claim lock holds across processes - a `seamless plan validate` beside the
    server (two Orchestrators on one database, each with its own in-process locks)."""
    server = build_harness(tmp_path, any_store, [vm(1)])
    cli = build_harness(tmp_path, any_store, [vm(1)])
    a, b = plan_for([vm(1)], name="A"), plan_for([vm(1)], name="B")
    any_store.put("plan", a)
    any_store.put("plan", b)
    inside = asyncio.Event()
    for src in (server.source, cli.source):
        original = src.get_vm

        async def get_vm(source_id, original=original):
            inside.set()
            await asyncio.sleep(0.3)  # the other validation runs meanwhile
            return await original(source_id)

        src.get_vm = get_vm
    first = asyncio.ensure_future(server.orch.validate_plan(a.id, "alice"))
    await asyncio.wait_for(inside.wait(), 5)
    results = await asyncio.gather(
        first, cli.orch.validate_plan(b.id, "cli"), return_exceptions=True
    )
    assert sum(isinstance(r, NotAllowed) for r in results) == 1, results
    holders = [m for m in any_store.list("migration", Migration) if m.vm.source_id == "vm-1"]
    assert [m.plan_id for m in holders] == [a.id]


@pytest.mark.parametrize("phase", [P.cancelled, P.finalized, P.rolled_back])
async def test_a_vm_is_free_for_another_plan_once_cancelled_finalized_or_rolled_back(
    tmp_path, store, phase
):
    h, first = await setup(tmp_path, store, [vm(1), vm(2)])
    await h.orch.validate_plan(first.id, "alice")
    held = await h.by_vm(first.id, "vm-2")
    held.phase = phase
    store.put("migration", held)
    second = plan_for([vm(2)], name="Second wave")
    store.put("plan", second)

    report = await h.orch.validate_plan(second.id, "bob")
    assert [item.vm_name for item in report.migrations] == ["web-02"]


async def test_a_plan_of_another_source_does_not_hold_the_vm(tmp_path, store):
    """VM ids belong to a source cloud: a migration in a plan of another source provider with the
    same VM id is another VM and does not hold this one (SDD §5.4)."""
    h, first = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(first.id, "alice")
    elsewhere = plan_for([vm(1)], name="Other cloud", source_provider_id="other-source")
    store.put("plan", elsewhere)
    mine = await h.by_vm(first.id, "vm-1")
    store.put(
        "migration",
        mine.model_copy(update={"id": "mig-other", "plan_id": elsewhere.id, "phase": P.precopy}),
    )

    report = await h.orch.validate_plan(first.id, "alice")
    assert [item.vm_name for item in report.migrations] == ["web-01"]


async def test_cross_plan_refusal_counts_vms_and_names_each_holder_once(tmp_path, store):
    """Plans validated before the rule (SDD §5.4) may hold one VM several times: the refusal
    counts VMs, not migrations, and names each holding plan once - by name and id, as two plans
    may share a name (both are "Finance" here)."""
    h, first = await setup(tmp_path, store, [vm(1), vm(2)])
    await h.orch.validate_plan(first.id, "alice")
    legacy = plan_for([vm(1), vm(2)])  # another plan called "Finance", from before the rule
    store.put("plan", legacy)
    for vm_id in ("vm-1", "vm-2"):
        held = await h.by_vm(first.id, vm_id)
        store.put(
            "migration", held.model_copy(update={"id": f"legacy-{vm_id}", "plan_id": legacy.id})
        )
    third = plan_for([vm(1), vm(2)], name="Third wave")
    store.put("plan", third)

    with pytest.raises(NotAllowed) as refused:
        await h.orch.validate_plan(third.id, "bob")
    a, b = sorted([first.id, legacy.id])
    assert str(refused.value).startswith(
        "2 VM(s) already have a migration in another plan: "
        f'web-01 (plan "Finance", {a}, ready), web-01 (plan "Finance", {b}, ready), '
        f'web-02 (plan "Finance", {a}, ready), web-02 (plan "Finance", {b}, ready); '
    )


async def test_cross_plan_refusal_says_how_many_holders_it_leaves_out(tmp_path, store):
    vms = [vm(i) for i in range(1, 13)]
    h, first = await setup(tmp_path, store, vms)
    await h.orch.validate_plan(first.id, "alice")
    second = plan_for(vms, name="Second wave")
    store.put("plan", second)

    with pytest.raises(
        NotAllowed,
        match=r"^12 VM\(s\) .*web-10 \(plan \"Finance\", plan-[0-9a-f]{8}, ready\) and 2 more; ",
    ):
        await h.orch.validate_plan(second.id, "bob")


def _first_cutover_fails(tmp_path, store):
    """A one-VM cold plan whose first cutover fails after stopping the source (rolled back
    automatically, which completes the plan); later attempts succeed."""
    settings = make_settings(tmp_path)
    tries: list[int] = []

    async def cutover(ctx):
        tries.append(1)
        if len(tries) == 1:
            await ctx.mark_downtime_start()
            raise PermanentStepError("dst volume attach failed")
        return await executor.sim.run(StepName.CUTOVER, ctx)

    executor = ScriptedExecutor(settings, hooks={StepName.CUTOVER: cutover})
    h = build_harness(tmp_path, store, [vm(1)], executor=executor, settings=settings)
    plan = plan_for([vm(1)], default_strategy=Strategy.cold)
    store.put("plan", plan)
    return h, plan, tries


async def test_a_retry_reopens_a_plan_its_rollback_completed(tmp_path, store):
    """SDD §5.1: a rolled-back migration is wave-complete, so its plan completes; a retry of it
    reopens the plan, or the new attempt would wait in ready for good."""
    h, plan, tries = _first_cutover_fails(tmp_path, store)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await h.wait_phase(mid, P.rolled_back)
    await h.wait_plan(plan.id, PlanStatus.completed)
    retried = await h.orch.retry(mid, "alice")
    assert retried.phase == P.ready
    assert (await h.plan(plan.id)).status == PlanStatus.running, "the retry reopens the plan"
    m = await h.wait_phase(mid, P.completed)
    await h.wait_plan(plan.id, PlanStatus.completed)
    await h.wait_kind("plan.completed", 2)
    await h.orch.stop()
    assert len(tries) == 2 and m.attempts == 1
    reopened = [
        e
        for e in store.events(since_seq=0, limit=1000)
        if e.kind == "plan.updated" and e.data.get("status") == "running"
    ]
    assert len(reopened) == 1 and reopened[0].actor == "alice" and "web-01" in reopened[0].message
    assert h.kinds().count("plan.completed") == 2


async def test_a_tick_during_a_retrys_write_leaves_the_reopen_to_the_retry(tmp_path, store):
    """SDD §5.1, §8 (PR #58 CI): the tick reopens a completed plan holding a ready migration - the
    fallback for a retry whose own reopen a store error kept from it. A tick that ran between the
    retry's write of ready and its reopen, the migration's lock still held, reopened the plan as
    "system", and the audit named no retry of web-01 and not alice. It leaves the plan to the
    retry, as any plan or migration whose lock is busy."""
    h, plan, _ = _first_cutover_fails(tmp_path, store)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await h.wait_phase(mid, P.rolled_back)
    await h.wait_plan(plan.id, PlanStatus.completed)
    real_transitions = h.orch._transitions

    async def transitions(m, v, steps, *args, **kwargs):
        out = await real_transitions(m, v, steps, *args, **kwargs)
        if steps and steps[0][0] == P.ready:
            await h.orch.tick()  # the retry wrote ready and still holds the migration's lock
        return out

    h.orch._transitions = transitions
    await h.orch.retry(mid, "alice")
    await h.orch.stop()
    reopened = [
        e
        for e in store.events(since_seq=0, limit=1000)
        if e.kind == "plan.updated" and e.data.get("status") == "running"
    ]
    assert [(e.actor, "web-01" in e.message) for e in reopened] == [("alice", True)], reopened


async def test_a_retry_in_a_completed_plan_whose_provider_is_gone_is_refused(tmp_path, store):
    """SDD §5.1/§12: a completed plan's providers may be deleted; reopening it without them would
    leave a running plan that cannot run, so the retry is refused and nothing changes."""
    h, plan, _ = _first_cutover_fails(tmp_path, store)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await h.wait_phase(mid, P.rolled_back)
    await h.wait_plan(plan.id, PlanStatus.completed)
    store.delete("provider", DST_PROVIDER.id)
    with pytest.raises(NotAllowed, match=f"provider {DST_PROVIDER.id!r} no longer exists"):
        await h.orch.retry(mid, "alice")
    await h.orch.stop()
    assert (await h.migration(mid)).phase == P.rolled_back
    assert (await h.plan(plan.id)).status == PlanStatus.completed
    assert "retry" not in [e.data.get("action") for e in store.events(since_seq=0, limit=1000)]


async def test_a_retry_after_the_ticks_snapshot_keeps_the_plan_running(tmp_path, store):
    """SDD §8: the tick completes a plan on migrations re-read under the plan's lock — a retry
    that returned after the tick's snapshot must not leave a completed plan with a ready VM."""
    h, plan, tries = _first_cutover_fails(tmp_path, store)
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    mid = (await h.by_vm(plan.id, "vm-1")).id
    tick_plan = h.orch._tick_plan
    retried: list[Migration] = []

    async def retry_after_the_snapshot(plan_, migs, counts, now):
        if not retried and any(m.id == mid and m.phase == P.rolled_back for m in migs):
            retried.append(await h.orch.retry(mid, "alice"))  # the snapshot still says rolled_back
        return await tick_plan(plan_, migs, counts, now)

    h.orch._tick_plan = retry_after_the_snapshot
    await h.orch.start()
    m = await h.wait_phase(mid, P.completed)
    await h.wait_plan(plan.id, PlanStatus.completed)
    await h.wait_kind("plan.completed")
    await h.orch.stop()
    assert retried and retried[0].phase == P.ready
    assert len(tries) == 2 and m.attempts == 1
    assert h.kinds().count("plan.completed") == 1, "completed once, after the retried attempt"


@pytest.mark.parametrize(
    "status",
    [
        PlanStatus.draft,
        PlanStatus.validated,
        PlanStatus.running,
        PlanStatus.paused,
        PlanStatus.failed,
    ],
)
async def test_a_retry_reopens_only_a_completed_plan(tmp_path, store, status):
    """SDD §5.1: a plan in any other status keeps it at a retry (a paused one runs the new
    attempt once it is started again) and no plan.updated says it reopened."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    m.phase = P.rolled_back
    store.put("migration", m)
    fresh = await h.plan(plan.id)
    fresh.status = status
    store.put("plan", fresh)
    assert (await h.orch.retry(m.id, "alice")).phase == P.ready
    assert (await h.plan(plan.id)).status == status
    assert not [e for e in store.events(since_seq=0, limit=1000) if e.kind == "plan.updated"]


async def test_a_retry_waiting_for_the_vm_claims_leaves_its_plans_lock_free(tmp_path, store):
    """SDD §8: a validation of another plan holds the claim lock through its provider calls; a
    retry waiting for it must not hold its own plan's lock meanwhile, or the tick, which starts
    that plan's steps under it, would wait too - for every plan."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    m.phase = P.failed
    store.put("migration", m)
    async with h.orch._lock(VM_CLAIMS):  # another plan's validation
        retry = asyncio.create_task(h.orch.retry(m.id, "alice"))
        await asyncio.sleep(0.05)
        assert not retry.done()
        assert not h.orch.plan_lock(plan.id).locked(), "the tick's _begin would wait on it"
    assert (await retry).phase == P.ready


async def test_a_validation_of_a_running_plan_is_refused_before_it_waits_for_the_vm_claims(
    tmp_path, store
):
    """SDD §8: a validation of another plan holds the claim lock through its provider calls; a
    validation of a running plan is refused (409) under its own plan's lock before it waits for
    the claims, so it never holds the lock the tick needs to start that plan's steps."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    async with h.orch._lock(VM_CLAIMS):  # another plan's validation
        validation = asyncio.create_task(h.orch.validate_plan(plan.id, "bayu"))
        await asyncio.sleep(0.05)
        assert validation.done(), "it waits for the claims, holding the running plan's lock"
        assert not h.orch.plan_lock(plan.id).locked(), "the tick's _begin would wait on it"
        with pytest.raises(NotAllowed, match="pause the plan before validating it again"):
            await validation


async def test_the_lock_table_does_not_grow_with_the_ids_requests_name(tmp_path, store):
    """SDD §8: a lock lives while a coroutine holds it or waits for it — requests naming unknown
    ids (404) leave no lock behind, and a held lock is the one every caller gets for its key."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    before = len(h.orch._locks)
    for n in range(500):
        with pytest.raises(NotFound):
            await h.orch.cancel(f"mig-unknown-{n}", "bayu")
        with pytest.raises(NotFound):
            await h.orch.validate_plan(f"plan-unknown-{n}", "bayu")
    assert len(h.orch._locks) <= before
    async with h.orch.plan_lock(plan.id):
        assert h.orch.plan_lock(plan.id).locked()  # the same lock while it is held
        waiter = asyncio.create_task(h.orch.validate_plan(plan.id, "alice"))
        await asyncio.sleep(0.05)
        assert not waiter.done(), "a validation waits for the plan's lock"
    assert (await waiter).plan_id == plan.id


async def test_a_retry_during_the_plans_completion_reopens_it(tmp_path, store):
    """SDD §8: the completion holds the plan's lock from its re-read to its write; a retry that
    arrives meanwhile waits for it, then finds the plan completed and reopens it."""
    h, plan, tries = _first_cutover_fails(tmp_path, store)
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    mid = (await h.by_vm(plan.id, "vm-1")).id
    update_plan = h.orch._update_plan
    retries: list[asyncio.Task] = []

    async def retry_while_completing(plan_id, mutate):
        if mutate.__name__ == "finish" and not retries:
            retries.append(asyncio.create_task(h.orch.retry(mid, "alice")))
            await asyncio.sleep(0.05)  # the retry runs as far as the plan's lock lets it
        return await update_plan(plan_id, mutate)

    h.orch._update_plan = retry_while_completing
    await h.orch.start()
    m = await h.wait_phase(mid, P.completed)
    await h.wait_plan(plan.id, PlanStatus.completed)
    await h.wait_kind("plan.completed", 2)
    await h.orch.stop()
    assert retries and (await retries[0]).phase == P.ready
    assert len(tries) == 2 and m.attempts == 1
    assert h.kinds().count("plan.completed") == 2


async def test_retry_refused_while_another_plan_holds_the_vm(tmp_path, store):
    """A rolled-back migration lets its VM go; a retry would take it back (rolled_back -> ready),
    so it is refused while another plan holds the VM (SDD §5.4)."""
    h, first = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(first.id, "alice")
    old = await h.by_vm(first.id, "vm-1")
    old.phase = P.rolled_back
    store.put("migration", old)
    second = plan_for([vm(1)], name="Second wave")
    store.put("plan", second)
    await h.orch.validate_plan(second.id, "bob")

    with pytest.raises(NotAllowed, match=r"web-01.*Second wave.*ready"):
        await h.orch.retry(old.id, "alice")
    assert (await h.by_vm(first.id, "vm-1")).phase == P.rolled_back

    await h.orch.cancel((await h.by_vm(second.id, "vm-1")).id, "bob", "back to the first plan")
    assert (await h.orch.retry(old.id, "alice")).phase == P.ready


async def test_validate_and_retry_wait_for_another_process_holding_the_claims(tmp_path, any_store):
    """SDD §5.4: validate_plan and retry take the database-wide claim lock - while another
    process holds it they wait, and they go on once it lets go."""
    h, first = await setup(tmp_path, any_store, [vm(1)])
    other = Store(any_store.url)  # another process's connection to the database
    try:
        held = other.cross_process_lock("vm-claims")
        assert held.try_acquire()
        validation = asyncio.ensure_future(h.orch.validate_plan(first.id, "alice"))
        await asyncio.sleep(0.4)
        assert not validation.done()
        held.release()
        await asyncio.wait_for(validation, 10)

        old = await h.by_vm(first.id, "vm-1")
        old.phase = P.rolled_back
        any_store.put("migration", old)
        assert held.try_acquire()
        retry = asyncio.ensure_future(h.orch.retry(old.id, "alice"))
        await asyncio.sleep(0.4)
        assert not retry.done()
        held.release()
        assert (await asyncio.wait_for(retry, 10)).phase == P.ready
    finally:
        other.dispose()


async def test_a_validation_never_marks_validated_a_plan_another_process_edited(tmp_path, store):
    """SDD §8: an edit from another process (its own plan lock) during the validation leaves the
    plan as the edit left it, and the validation fails with a conflict - never `validated` with
    settings no validation saw."""
    server = build_harness(tmp_path, store, [vm(1)])
    cli = build_harness(tmp_path, store, [vm(1)])
    plan = plan_for([vm(1)], downtime_slo_s=600)
    store.put("plan", plan)
    original = cli.source.inventory

    async def inventory():
        # meanwhile, PATCH /plans/{id} on the server, under the server's own plan lock
        async with server.orch.plan_lock(plan.id):
            p, v = store.get_versioned("plan", plan.id, Plan)
            p.downtime_slo_s, p.status = 60, PlanStatus.draft
            store.put("plan", p, expected_version=v)
        return await original()

    cli.source.inventory = inventory
    with pytest.raises(ConflictError, match="changed while it was being validated"):
        await cli.orch.validate_plan(plan.id, "cli")
    after = store.get("plan", plan.id, Plan)
    assert (after.status, after.downtime_slo_s) == (PlanStatus.draft, 60)
    assert not [e for e in store.events(since_seq=0) if e.kind == "plan.validated"]

    # validated again, with the settings it now has
    report = await cli.orch.validate_plan(plan.id, "cli")
    assert report.ok and store.get("plan", plan.id, Plan).status == PlanStatus.validated


async def test_a_validation_never_marks_validated_a_plan_another_process_started(tmp_path, store):
    """SDD §8: a start by the server during a CLI validation stands; the validation fails (409)."""
    server = build_harness(tmp_path, store, [vm(1)])
    cli = build_harness(tmp_path, store, [vm(1)])
    plan = plan_for([vm(1)])
    store.put("plan", plan)
    await cli.orch.validate_plan(plan.id, "cli")
    original = cli.source.inventory

    async def inventory():
        await server.orch.start_plan(plan.id, "alice")  # POST /plans/{id}/start on the server
        return await original()

    cli.source.inventory = inventory
    with pytest.raises(ConflictError, match="changed while it was being validated"):
        await cli.orch.validate_plan(plan.id, "cli")
    assert store.get("plan", plan.id, Plan).status == PlanStatus.running


async def test_a_validation_never_marks_validated_a_plan_another_process_rewaved(tmp_path, store):
    """SDD §8: auto-waves by the server during a CLI validation (new waves, draft) stands."""
    server = build_harness(tmp_path, store, [vm(1), vm(2)])
    cli = build_harness(tmp_path, store, [vm(1), vm(2)])
    plan = plan_for([vm(1), vm(2)])
    store.put("plan", plan)
    await cli.orch.validate_plan(plan.id, "cli")
    original = cli.source.inventory

    async def inventory():
        await server.orch.auto_waves(
            plan.id, 1, "alice"
        )  # POST /plans/{id}/waves/auto on the server
        return await original()

    cli.source.inventory = inventory
    with pytest.raises(ConflictError, match="changed while it was being validated"):
        await cli.orch.validate_plan(plan.id, "cli")
    after = store.get("plan", plan.id, Plan)
    assert after.waves and after.status == PlanStatus.draft


async def test_a_validation_of_a_draft_never_hides_waves_another_process_planned(tmp_path, store):
    """SDD §8: a draft re-waved by the server during a CLI validation stays a draft with its new
    waves; only the waves changed (the status was draft already), and the validation fails (409)."""
    vms = [vm(i) for i in range(1, 6)]
    server = build_harness(tmp_path, store, vms)
    cli = build_harness(tmp_path, store, vms)
    plan = plan_for(vms)
    store.put("plan", plan)
    await cli.orch.validate_plan(plan.id, "cli")
    await server.orch.auto_waves(plan.id, 2, "alice")  # a draft again: a pilot of 3, a wave of 2
    before = store.get("plan", plan.id, Plan)
    assert before.status == PlanStatus.draft
    original = cli.source.inventory

    async def inventory():
        await server.orch.auto_waves(plan.id, 1, "alice")  # re-waved meanwhile: still a draft
        return await original()

    cli.source.inventory = inventory
    with pytest.raises(ConflictError, match="changed while it was being validated"):
        await cli.orch.validate_plan(plan.id, "cli")
    after = store.get("plan", plan.id, Plan)
    assert after.status == PlanStatus.draft and len(after.waves) > len(before.waves)


async def test_concurrent_validations_of_two_plans_claim_a_vm_once(tmp_path, store):
    h, first = await setup(tmp_path, store, [vm(1), vm(2)])
    second = plan_for([vm(2)], name="Second wave")
    store.put("plan", second)

    results = await asyncio.gather(
        h.orch.validate_plan(first.id, "alice"),
        h.orch.validate_plan(second.id, "bob"),
        return_exceptions=True,
    )
    assert sorted(type(r).__name__ for r in results) == ["NotAllowed", "ValidationReport"]
    holders = [
        m for p in (first, second) for m in await h.migrations(p.id) if m.vm.source_id == "vm-2"
    ]
    assert len(holders) == 1


async def test_a_validation_waits_while_another_plan_claims_its_vms(tmp_path, store, monkeypatch):
    """The first plan has passed its check and is still creating migrations: the second plan's
    validation waits for it, then sees the claim (SDD §5.4), instead of claiming the VM too."""
    h, first = await setup(tmp_path, store, [vm(1), vm(2)])
    second = plan_for([vm(2)], name="Second wave")
    store.put("plan", second)
    creating, release = asyncio.Event(), asyncio.Event()
    create = h.orch._create_migration

    async def slow_create(plan, vm_ref, strategy, actor, **kw):
        if plan.id == first.id:
            creating.set()
            await release.wait()
        return await create(plan, vm_ref, strategy, actor, **kw)

    monkeypatch.setattr(h.orch, "_create_migration", slow_create)
    claiming = asyncio.create_task(h.orch.validate_plan(first.id, "alice"))
    await creating.wait()
    waiting = asyncio.create_task(h.orch.validate_plan(second.id, "bob"))
    await asyncio.wait({waiting}, timeout=1)
    assert not waiting.done(), "the second plan validated while the first was claiming the VM"

    release.set()
    await claiming
    with pytest.raises(NotAllowed, match=r"web-02.*Finance"):
        await waiting
    assert await h.migrations(second.id) == []


async def test_start_rejects_blocked_plan(tmp_path, store):
    gpu = vm(3, "gpu-01", flavor_extra_specs={"resources:VGPU": "1"})
    h, plan = await setup(tmp_path, store, [vm(1), gpu])
    with pytest.raises(NotAllowed):  # not validated yet
        await h.orch.start_plan(plan.id, "alice")
    await h.orch.validate_plan(plan.id, "alice")
    with pytest.raises(NotAllowed, match="blocked"):
        await h.orch.start_plan(plan.id, "alice")
    blocked = await h.by_vm(plan.id, "vm-3")
    await h.orch.cancel(blocked.id, "alice", "GPU workload stays on the old cloud")
    started = await h.orch.start_plan(plan.id, "alice")
    assert started.status == PlanStatus.running


def _refusing(settings, answers):
    """A warm migration whose pre-copy and sync passes return these cutover_refusals in turn."""
    executor = ScriptedExecutor(settings)
    queue = list(answers)

    def hook(step):
        async def run(ctx):
            result = await executor.sim.run(step, ctx)
            result.cutover_refusals = queue.pop(0) if queue else None
            return result

        return run

    executor.hooks.update(
        {StepName.PRECOPY: hook(StepName.PRECOPY), StepName.SYNC: hook(StepName.SYNC)}
    )
    return executor


def _refusals_at_each_pass(store, monkeypatch) -> list[list[str]]:
    """The stored migration's cutover_refusals as each pass's event is written."""
    seen: list[list[str]] = []
    append = store.append_event

    def recording(event):
        if event.kind == "migration.sync_pass":
            seen.append(store.get("migration", event.migration_id, Migration).cutover_refusals)
        return append(event)

    monkeypatch.setattr(store, "append_event", recording)
    return seen


async def test_a_warm_migration_keeps_what_the_cutover_will_refuse_until_its_cutover(
    tmp_path, store, monkeypatch
):
    """SDD §7.2: a warm pass records on the migration, before its event, what it met that the
    cutover will refuse; a pass that learned nothing leaves it, and a successful cutover empties
    it."""
    settings = make_settings(tmp_path)
    problem = "the NIC 00:50:56:00:00:01 of vm-1 on 'app-net': port port-old holds its MAC"
    seen = _refusals_at_each_pass(store, monkeypatch)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm, "downtime_slo_s": 300},
        executor=_refusing(settings, [[problem], None]),
        settings=settings,
    )
    await run_plan(h, plan)
    done = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert [p.kind for p in done.sync_passes] == ["full", "delta", "final"]
    assert seen == [[problem], [problem], []]
    assert done.cutover_refusals == []


async def test_a_warm_pass_that_meets_none_empties_the_cutover_refusals(
    tmp_path, store, monkeypatch
):
    """SDD §7.2: fixed while the passes run - the next pass's empty list replaces the problems."""
    settings = make_settings(tmp_path)
    seen = _refusals_at_each_pass(store, monkeypatch)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm, "downtime_slo_s": 300},
        executor=_refusing(settings, [["port p2 on network 'gone' does not resolve"], []]),
        settings=settings,
    )
    await run_plan(h, plan)
    await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert seen == [["port p2 on network 'gone' does not resolve"], [], []]


async def test_the_demo_fault_refuses_the_cutover_before_the_stop_until_a_pass_finds_it_fixed(
    tmp_path, store, monkeypatch
):
    """SDD §7.4: through the orchestrator, the simulator's `demo-fault: cutover-refusal` - its
    first two passes record what the cutover will refuse (§7.2), the cutover after them fails
    before the source stops (no downtime, no rollback), and the retry's pass finds it fixed: the
    list empties and the migration completes."""
    seen = _refusals_at_each_pass(store, monkeypatch)
    nic = Nic(network="app-net", mac="00:50:56:00:00:01", mtu=1500, fixed_ips=["10.0.0.1"])
    tagged = vm(1, "dc2-portal-02", nics=[nic], tags={"demo-fault": "cutover-refusal"})
    h, plan = await setup(
        tmp_path, store, [tagged], {"default_strategy": Strategy.warm, "downtime_slo_s": 300}
    )
    refusal = (
        "the NIC 00:50:56:00:00:01 of dc2-portal-02 on 'app-net': port demo-port-1 holds 10.0.0.1"
    )
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    refused = await h.wait_phase(mid, P.failed)
    assert refused.downtime_started_at is None and refused.error == f"cutover: {refusal}"
    assert refused.cutover_refusals == [refusal], "a refused cutover keeps the list"
    await h.orch.retry(mid, "alice")
    done = await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    assert seen[:3] == [[refusal], [refusal], []] and all(s == [] for s in seen[3:])
    assert done.cutover_refusals == [] and done.downtime_started_at is not None


async def test_warm_flow_reaches_completed_with_downtime(tmp_path, store):
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm, "downtime_slo_s": 300}
    )
    await run_plan(h, plan)
    m = await h.by_vm(plan.id, "vm-1")
    done = await h.wait_phase(m.id, P.completed)
    await h.wait_plan(plan.id, PlanStatus.completed)
    # plan.completed is written after the plan's status: stopping now would cancel it
    await h.wait_kind("plan.completed")
    await h.orch.stop()
    assert h.history(done) == [
        P.pending,
        P.validating,
        P.ready,
        P.precopy,
        P.syncing,
        P.awaiting_cutover,
        P.cutover,
        P.verifying,
        P.completed,
    ]
    assert [p.kind for p in done.sync_passes] == ["full", "delta", "final"]
    assert [p.number for p in done.sync_passes] == [1, 2, 3]
    assert done.downtime_started_at and done.downtime_ended_at
    # a simulated cutover counts model seconds plus the plan's boot time (SDD §5.2, ADV-16)
    assert done.actual_downtime_s == pytest.approx(
        done.downtime_model_s + params_for_plan(plan).boot_s
    )
    assert done.destination_server_id and done.progress_pct == 100
    assert done.bytes_transferred == sum(p.bytes_transferred for p in done.sync_passes)
    kinds = h.kinds()
    for kind in (
        "plan.started",
        "migration.sync_pass",
        "migration.downtime_started",
        "migration.downtime_ended",
        "plan.completed",
    ):
        assert kind in kinds, kind
    assert (await h.plan(plan.id)).status == PlanStatus.completed
    steps = [s for _, s in h.executor.calls]
    assert steps == [StepName.PRECOPY, StepName.SYNC, StepName.CUTOVER]


async def test_cold_flow(tmp_path, store):
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert h.history(m)[2:] == [P.ready, P.cutover, P.verifying, P.completed]
    assert [s for _, s in h.executor.calls] == [StepName.CUTOVER]
    assert h.executor.prestaged == [plan.id]


async def test_a_tick_loads_only_the_migrations_that_changed(tmp_path, store, monkeypatch):
    """Performance.md, SDD §11: the tick reads (id, version) and loads only the migrations that
    changed since the last tick - not every migration of the live plans once a second."""
    vms = [vm(i) for i in range(1, 4)]
    plan_kw = {"default_strategy": Strategy.warm, "require_approval": True}
    h, plan = await setup(tmp_path, store, vms, {**plan_kw, "keep_warm_interval_s": 86400})
    await run_plan(h, plan)
    for i in range(1, 4):
        await h.wait_phase((await h.by_vm(plan.id, f"vm-{i}")).id, P.awaiting_cutover)
    await h.orch.stop()
    await h.orch.tick()  # the cache holds every migration now
    m2 = await h.by_vm(plan.id, "vm-2")
    _, version = store.get_versioned("migration", m2.id, Migration)
    store.put("migration", m2.model_copy(update={"progress_pct": 12.0}), expected_version=version)

    listed: list[str] = []
    loaded: list[str] = []
    real_list, real_get_many = store.list, store.get_many

    def counting_list(kind, *args, **kwargs):
        listed.append(kind)
        return real_list(kind, *args, **kwargs)

    def counting_get_many(kind, ids, model_cls):
        loaded.extend(ids)
        return real_get_many(kind, ids, model_cls)

    monkeypatch.setattr(store, "list", counting_list)
    monkeypatch.setattr(store, "get_many", counting_get_many)
    await h.orch.tick()
    assert "migration" not in listed
    assert loaded == [m2.id]
    # still waiting for the approval: the tick saw the migrations as they are
    phases = [(await h.by_vm(plan.id, f"vm-{i}")).phase for i in (1, 2, 3)]
    assert phases == [P.awaiting_cutover] * 3


async def test_requires_approval_waits(tmp_path, store):
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm, "require_approval": True}
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.awaiting_cutover)
    await asyncio.sleep(0.1)  # many ticks
    assert (await h.migration(m.id)).phase == P.awaiting_cutover
    approved = await h.orch.approve(m.id, "sari", "change CHG-42")
    assert approved.approvals[-1].actor == "sari" and approved.approvals[-1].comment
    await h.wait_phase(m.id, P.completed)
    await h.orch.stop()
    assert "migration.approved" in h.kinds()


async def test_cutover_window_respected_and_force_window(tmp_path, store):
    window = CutoverWindow(
        start=Clock()() + timedelta(days=1), end=Clock()() + timedelta(days=1, hours=4)
    )
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {
            "default_strategy": Strategy.warm,
            "require_approval": True,
            # a test of the window: the request approves, as without maker-checker (SDD §5.4)
            "require_distinct_approver": False,
            "auto_cutover": False,
            "cutover_window": window,
        },
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.awaiting_cutover)
    await h.orch.request_cutover(m.id, "sari", force_window=False)
    await asyncio.sleep(0.1)
    assert (await h.migration(m.id)).phase == P.awaiting_cutover, "outside the window"
    h.clock.advance(timedelta(days=1, hours=1).total_seconds())  # inside the window now
    await h.wait_phase(m.id, P.completed)

    # a second migration cut over outside the window with force_window
    h2, plan2 = await setup(
        tmp_path / "b",
        store,
        [vm(5)],
        {
            "default_strategy": Strategy.warm,
            "require_approval": True,
            "require_distinct_approver": False,
            "auto_cutover": False,
            "cutover_window": window,
        },
    )
    await h.orch.stop()
    await run_plan(h2, plan2)
    m2 = await h2.wait_phase((await h2.by_vm(plan2.id, "vm-5")).id, P.awaiting_cutover)
    out = await h2.orch.request_cutover(m2.id, "sari", force_window=True, comment="emergency")
    assert out.cutover_requested and out.force_window and out.approvals[-1].comment == "emergency"
    # persisted on the migration document (a restarted orchestrator reads it back)
    assert (await h2.migration(m2.id)).force_window is True
    await h2.wait_phase(m2.id, P.completed)
    await h2.orch.stop()


async def test_keep_warm_pass_runs_after_interval(tmp_path, store):
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm, "require_approval": True, "keep_warm_interval_s": 900},
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.awaiting_cutover)
    passes = len(m.sync_passes)
    await asyncio.sleep(0.1)
    assert len((await h.migration(m.id)).sync_passes) == passes, "no pass before the interval"
    h.clock.advance(901)
    deadline = asyncio.get_running_loop().time() + 5
    while len((await h.migration(m.id)).sync_passes) == passes:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)
    h.clock.advance(-901)  # back to "now": no further keep-warm passes are due
    await asyncio.sleep(0.05)
    m = await h.wait_phase(m.id, P.awaiting_cutover)
    await h.orch.stop()
    history = h.history(m)
    first_wait = history.index(P.awaiting_cutover)
    assert P.syncing in history[first_wait:] and history[-1] == P.awaiting_cutover
    assert m.sync_passes[-1].kind == "delta" and len(m.sync_passes) > passes

    # an explicit sync request runs a pass immediately
    h2, plan2 = await setup(
        tmp_path / "b",
        store,
        [vm(2)],
        {"default_strategy": Strategy.warm, "require_approval": True},
    )
    await run_plan(h2, plan2)
    m2 = await h2.wait_phase((await h2.by_vm(plan2.id, "vm-2")).id, P.awaiting_cutover)
    before = len(m2.sync_passes)
    synced = await h2.orch.request_sync(m2.id, "bayu")
    assert synced.phase == P.syncing
    m2 = await h2.wait_phase(m2.id, P.awaiting_cutover)
    assert len(m2.sync_passes) == before + 1
    await h2.orch.cancel(m2.id, "bayu", "descoped")
    with pytest.raises(NotAllowed):
        await h2.orch.request_sync(m2.id, "bayu")
    await h2.orch.stop()


def blocking_cutover(gates: dict[str, asyncio.Event], started: list[str]):
    async def hook(ctx):
        await ctx.mark_downtime_start()
        started.append(ctx.migration.vm.source_id)
        await gates.setdefault(ctx.migration.vm.source_id, asyncio.Event()).wait()
        return StepResult(destination_server_id=f"dst-{ctx.migration.vm.source_id}")

    return hook


async def test_revalidation_and_strategy_change_clear_approvals(tmp_path, store):
    """An approval is given for one assessment (SDD §5.4): a new validation or a strategy change
    needs a new one, and a pending cutover request does not survive either."""
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    await h.orch.approve(m.id, "sari", "ok")
    await h.orch.request_cutover(m.id, "sari", force_window=True)
    m = await h.migration(m.id)
    # ADV-17: the request is not an approval (the plan requires a distinct approver)
    assert len(m.approvals) == 1 and m.cutover_requested is True
    assert m.force_window is True
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.migration(m.id)
    assert m.approvals == [] and m.cutover_requested is False
    assert m.force_window is False
    await h.orch.approve(m.id, "sari", "again")
    await h.orch.set_strategy(m.id, Strategy.warm, "rina")
    assert (await h.migration(m.id)).approvals == []


async def test_validation_refuses_a_cancelled_vm_still_listed(tmp_path, store):
    h, plan = await setup(tmp_path, store, [vm(1), vm(2)], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.cancel((await h.by_vm(plan.id, "vm-2")).id, "alice", "descoped")
    with pytest.raises(BadRequest, match="cancelled migration: web-02"):
        await h.orch.validate_plan(plan.id, "alice")


async def test_start_plan_refuses_a_vm_outside_every_wave(tmp_path, store):
    waves = [Wave(id="wave-1", name="Pilot", order=1, vm_ids=["vm-1"])]
    h, plan = await setup(
        tmp_path, store, [vm(1), vm(2)], {"default_strategy": Strategy.cold, "waves": waves}
    )
    await h.orch.validate_plan(plan.id, "alice")
    with pytest.raises(NotAllowed, match="belong to no wave: web-02"):
        await h.orch.start_plan(plan.id, "alice")


async def test_keep_warm_runs_while_waiting_for_a_cutover_slot(tmp_path, store):
    """A warm migration whose gate is open but which waits for max_concurrent_cutovers keeps
    its delta small meanwhile (SDD §5.4 rule 4)."""
    settings = make_settings(tmp_path, max_concurrent_cutovers=1)
    gates: dict[str, asyncio.Event] = {}
    started: list[str] = []
    executor = ScriptedExecutor(
        settings, hooks={StepName.CUTOVER: blocking_cutover(gates, started)}
    )
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1), vm(2)],
        {
            "default_strategy": Strategy.warm,
            "require_approval": False,
            "auto_cutover": True,
            "keep_warm_interval_s": 900,
        },
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    deadline = asyncio.get_running_loop().time() + 5
    while len(started) < 1:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)
    waiting = next(m for m in await h.migrations(plan.id) if m.phase != P.cutover)
    waiting = await h.wait_phase(waiting.id, P.awaiting_cutover)
    passes = len(waiting.sync_passes)
    h.clock.advance(901)
    deadline = asyncio.get_running_loop().time() + 5
    while len((await h.migration(waiting.id)).sync_passes) == passes:
        assert asyncio.get_running_loop().time() < deadline, "no keep-warm pass while queued"
        await asyncio.sleep(0.01)
    assert len(started) == 1, "the slot limit still holds"
    for gate in gates.values():
        gate.set()
    gates.setdefault(waiting.vm.source_id, asyncio.Event()).set()
    await h.wait_plan(plan.id, PlanStatus.completed, timeout=20)
    await h.orch.stop()


async def test_vmware_warm_convergence_ignores_missing_byte_counts(tmp_path, store):
    from seamless_migrate.domain.models import utcnow

    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    cbt = m.model_copy(
        update={
            "strategy": Strategy.vmware_warm,
            "sync_passes": [SyncPass(number=1, kind="full", started_at=utcnow(), duration_s=300.0)],
        }
    )
    converged, reason = h.orch._converged(cbt, plan)
    assert (converged, reason) == ("slo", "estimated final downtime 365 s is within the SLO")
    tight = plan.model_copy(update={"downtime_slo_s": 100})
    converged, reason = h.orch._converged(cbt, tight)
    assert converged is None and reason.startswith("another CBT pass: estimated downtime")
    warm = cbt.model_copy(update={"strategy": Strategy.warm})
    assert h.orch._converged(warm, plan) == ("threshold", "converged: last pass changed 0 B")


async def test_keep_warm_history_is_bounded_and_bytes_stay_counted(tmp_path, store):
    """SDD §5.4: a long wait keeps the first max_sync_passes passes and the latest 20; the bytes of
    the dropped passes stay in bytes_transferred, and pass numbers keep counting."""
    from seamless_migrate.domain.models import utcnow

    plan_kw = {"default_strategy": Strategy.warm, "max_sync_passes": 3}
    h, plan = await setup(tmp_path, store, [vm(1)], plan_kw)
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    cur, v = await h.orch._load(m.id)
    for _ in range(30):
        done = SyncPass(
            number=1,  # the orchestrator numbers the pass
            kind="delta",
            started_at=utcnow(),
            ended_at=utcnow(),
            duration_s=1.0,
            bytes_transferred=1000,
        )
        cur, v = await h.orch._record_pass(cur, v, done, plan)
    stored, _ = await h.orch._load(m.id)
    assert [p.number for p in stored.sync_passes] == [1, 2, 3, *range(11, 31)]
    assert stored.sync_bytes_dropped == 7 * 1000
    assert stored.bytes_transferred == 30 * 1000


# -- the cutover gate's fifth rule: GPU capacity (SDD §5.4 rule 5, §22.7, plan task G6) --------

#: the destination's vGPU flavor the gate's tests map their GPU VMs to: one vGPU of a numbered
#: request group that asks for the A100-4C trait (SDD §22.2, §22.3)
GATE_FLAVOR = "g1.a100-4c"
GATE_SPECS = {"resources1:VGPU": "1", "trait1:CUSTOM_NVIDIA_A100_4C": "required"}
#: its pool, and its class's pool, by pool_name (SDD §10)
GATE_POOL = "VGPU CUSTOM_NVIDIA_A100_4C"
GATE_ROOM = {GATE_POOL: 1, "VGPU": 1}
GATE_FULL = {GATE_POOL: 0, "VGPU": 0}


def gate_vm(i: int, **kw):
    """An OpenStack VM whose flavor g1.vgpu asks for one vGPU, as its source reports it (§22.2)."""
    from seamless_migrate.planning.gpu import gpu_requests

    specs = kw.pop("flavor_extra_specs", {"resources:VGPU": "1"})
    data = {"flavor": "g1.vgpu", "flavor_extra_specs": specs, "gpus": gpu_requests(specs)}
    return vm(i, f"gpu-{i:02d}", **{**data, **kw})


class GateDestination(StaticDestination):
    """A destination whose one GPU flavor, g1.a100-4c, asks for ``specs`` (SDD §22.3), its pools
    holding ``free`` units by pool name - for the validation's inventory and the gate's reads
    alike. ``answer`` replaces what a capacity read returns (a GpuCapacity, or an exception it
    raises), ``flavors_fail`` is raised by the flavor list, ``hold`` keeps each capacity read
    waiting until it is set; each read is counted."""

    def __init__(self, free=None, specs=None) -> None:
        super().__init__()
        self.free = dict(GATE_ROOM if free is None else free)
        self.specs = dict(GATE_SPECS if specs is None else specs)
        self.answer = None
        self.flavors_fail: Exception | None = None
        self.hold: asyncio.Event | None = None
        self.flavor_reads = 0
        self.reads: list[list] = []

    def gpu_flavor_list(self) -> list[dict]:
        from seamless_migrate.planning.gpu import gpu_requests

        size = {"name": GATE_FLAVOR, "vcpus": 8, "ram_mb": 32768, "disk_gb": 80}
        return [{**size, "gpus": gpu_requests(self.specs)}]

    def counted(self, requests):
        from seamless_migrate.planning.gpu import gpu_pools, pool_name
        from seamless_migrate.planning.preflight import GpuCapacity, GpuPool

        pools = []
        for pool in gpu_pools(requests):
            free = self.free.get(pool_name(pool.resource_class, pool.traits, pool.forbidden_traits))
            pools.append(
                GpuPool(
                    resource_class=pool.resource_class,
                    traits=list(pool.traits),
                    forbidden_traits=list(pool.forbidden_traits),
                    same_provider=pool.same_provider,
                    free=free or 0,
                    providers=1 if free else 0,
                    largest_free=free or 0,
                )
            )
        return GpuCapacity(status="ok", read_at=utcnow(), pools=pools)

    async def inventory(self):
        inv = await super().inventory()
        size = {"name": GATE_FLAVOR, "vcpus": 8, "ram_mb": 32768, "disk_gb": 80}
        inv.flavors.append({**size, "extra_specs": dict(self.specs)})
        inv.gpu_flavors = self.gpu_flavor_list()
        inv.gpu_capacity = self.counted([g for f in inv.gpu_flavors for g in f["gpus"]])
        return inv

    async def gpu_flavors(self) -> list[dict]:
        self.flavor_reads += 1
        if self.flavors_fail is not None:
            raise self.flavors_fail
        return self.gpu_flavor_list()

    async def gpu_capacity(self, requests):
        self.reads.append(list(requests))
        if self.hold is not None:
            await self.hold.wait()
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer if self.answer is not None else self.counted(requests)


async def _eventually(check, timeout: float = 10.0):
    """What ``check()`` returns once it is truthy, polled while the orchestrator runs."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not (found := await check()):
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)
    return found


async def _gate_plan(tmp_path, store, vms, plan_kw, dst=None, **harness_kw):
    """A plan of ``vms`` that maps g1.vgpu to the GPU flavor, validated with room in its pools and
    started with them full: another tenant took the units after the validation (SDD §22.7). The
    orchestrator is not started yet."""
    from seamless_migrate.domain.models import Mappings

    dst = dst if dst is not None else GateDestination()
    plan_kw = {"mappings": Mappings(flavors={"g1.vgpu": GATE_FLAVOR}), **plan_kw}
    h, plan = await setup(tmp_path, store, vms, plan_kw, destination=dst, **harness_kw)
    await h.orch.validate_plan(plan.id, "alice")
    for m in await h.migrations(plan.id):
        assert m.phase == P.ready, [f.code for f in m.findings]
    dst.free = dict(GATE_FULL)
    await h.orch.start_plan(plan.id, "alice")
    return h, plan, dst


async def _gpu_wait(h, mid: str, after=None) -> Migration:
    """The migration once a read recorded its GPU wait - a read later than ``after``'s, given a
    wait it already had."""

    async def recorded():
        m = await h.migration(mid)
        wait = m.gpu_wait
        return m if wait is not None and (after is None or wait.checked_at > after) else None

    return await _eventually(recorded)


def _gpu_events(h, mid: str) -> list[Event]:
    return [
        e
        for e in h.store.events(since_seq=0, limit=100000)
        if e.kind == "migration.gpu_wait" and e.migration_id == mid
    ]


async def test_the_source_of_a_vm_waiting_for_gpu_capacity_is_never_stopped(tmp_path, store):
    """SDD §5.4 rule 5, §22.7: the pool empties after the validation, and the warm migration that
    converged waits at its gate - however many ticks pass, its source is never stopped, its
    downtime clock never opens and no cutover step runs."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.warm}
    )
    stopped: list[str] = []

    async def power_off(source_id: str) -> None:
        stopped.append(source_id)

    # the simulated cutover stops the source through its provider (SDD §7.4)
    h.source.power_off = power_off
    h.executor.sim.providers = h.orch.providers
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await _gpu_wait(h, mid)
    await asyncio.sleep(0.2)  # many ticks
    m = await h.migration(mid)
    await h.orch.stop()
    assert m.phase == P.awaiting_cutover and m.gpu_wait is not None
    assert (stopped, m.downtime_started_at) == ([], None)
    assert StepName.CUTOVER not in [step for i, step in h.executor.calls if i == mid]
    assert len(dst.reads) == 1, "a read is good for a minute"


async def test_a_warm_migration_waiting_for_gpu_capacity_keeps_its_keep_warm_passes(
    tmp_path, store
):
    """SDD §5.4, §22.7: while its cutover waits for GPU capacity, a warm migration runs its
    keep-warm passes - awaiting_cutover, syncing, awaiting_cutover - and its wait goes on through
    them: the same beginning, a later check."""
    plan_kw = {"default_strategy": Strategy.warm, "keep_warm_interval_s": 900}
    h, plan, dst = await _gate_plan(tmp_path, store, [gate_vm(1)], plan_kw)
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    began = (await _gpu_wait(h, mid)).gpu_wait
    passes = len((await h.migration(mid)).sync_passes)
    h.clock.advance(901)  # a keep-warm pass is due, and the read is a minute old

    async def passed():
        m = await h.migration(mid)
        return m if len(m.sync_passes) > passes and m.phase == P.awaiting_cutover else None

    await _eventually(passed)
    m = await _gpu_wait(h, mid, after=began.checked_at)
    await h.orch.stop()
    history = h.history(m)
    waiting = history.index(P.awaiting_cutover)
    assert P.syncing in history[waiting:] and P.cutover not in history
    assert m.gpu_wait.since == began.since and m.phase in (P.awaiting_cutover, P.syncing)
    assert m.sync_passes[-1].kind == "delta"


async def test_a_single_shot_migration_waiting_for_gpu_capacity_stays_ready(tmp_path, store):
    """SDD §5.4 rule 5, §22.7: a cold GPU VM whose read finds its pool full stays in ready, its wait
    naming the pool, the units needed and free, and why - the destination by its label; nothing
    of its cutover runs."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}
    )
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await _gpu_wait(h, mid)
    await asyncio.sleep(0.2)  # many ticks
    m = await h.migration(mid)
    await h.orch.stop()
    wait = m.gpu_wait
    assert m.phase == P.ready and h.history(m)[-1] == P.ready
    assert (wait.pool, wait.needed, wait.free) == (GATE_POOL, 1, 0)
    assert wait.since == wait.checked_at
    assert "RHOSO 18.0" in wait.reason and GATE_FLAVOR in wait.reason
    assert [i for i, _ in h.executor.calls if i == mid] == []
    assert dst.reads == [[g for g in dst.gpu_flavor_list()[0]["gpus"]]]


async def test_the_gpu_wait_clears_and_the_cutover_starts_once_a_read_finds_room(tmp_path, store):
    """SDD §22.7: the unit is free again, and the read a minute later finds it: the wait clears and
    the cutover starts - the VM is migrated as any other."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}
    )
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await _gpu_wait(h, mid)
    dst.free = dict(GATE_ROOM)  # another tenant released a vGPU
    await asyncio.sleep(0.1)
    assert (await h.migration(mid)).phase == P.ready, "the cutover waits for a read"
    h.clock.advance(61)
    m = await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    assert m.gpu_wait is None and P.cutover in h.history(m)
    assert len(dst.reads) == 2


async def test_the_gate_counts_the_gpus_of_cutovers_without_a_server_yet(tmp_path, store):
    """SDD §22.7: one unit free and two GPU VMs at their gates - reads made at once never let both
    cut over: the first one's cutover takes the unit, and while it has recorded no server the
    second waits, needing 2 of the 1 free. Once the first records its server, Placement counts its
    unit and the gate no more: a read lets the second cut over."""
    settings = make_settings(tmp_path)
    gates: dict[str, asyncio.Event] = {}
    started: list[str] = []
    executor = ScriptedExecutor(
        settings, hooks={StepName.CUTOVER: blocking_cutover(gates, started)}
    )
    h, plan, dst = await _gate_plan(
        tmp_path,
        store,
        [gate_vm(1), gate_vm(2)],
        {"default_strategy": Strategy.cold},
        dst=GateDestination(free={GATE_POOL: 2, "VGPU": 2}),  # the validation counts both
        executor=executor,
        settings=settings,
    )
    dst.free = dict(GATE_ROOM)
    dst.hold = asyncio.Event()  # both reads wait at Placement, each having counted no cutover
    await h.orch.start()

    async def both_read():
        return len(dst.reads) == 2

    await _eventually(both_read)
    dst.hold.set()

    async def one_started():
        return started if started else None

    await _eventually(one_started)
    first = await h.by_vm(plan.id, started[0])
    second = next(m for m in await h.migrations(plan.id) if m.id != first.id)
    m = await _gpu_wait(h, second.id)
    await asyncio.sleep(0.1)  # many ticks
    assert (m.gpu_wait.pool, m.gpu_wait.needed, m.gpu_wait.free) == (GATE_POOL, 2, 1)
    assert len(started) == 1 and (await h.migration(second.id)).phase == P.ready
    async with h.orch._lock(first.id):  # its server exists: Placement holds its unit
        cur, v = await h.orch._load(first.id)
        cur.destination_server_id = f"dst-{first.vm.source_id}"
        await h.orch._save(cur, v)
    h.clock.advance(61)

    async def both_started():
        return len(started) == 2

    await _eventually(both_started)
    for vm_id in started:
        gates.setdefault(vm_id, asyncio.Event()).set()
    await h.wait_plan(plan.id, PlanStatus.completed)
    await h.orch.stop()
    assert (await h.migration(second.id)).gpu_wait is None


@pytest.mark.parametrize("unknown", ["forbidden", "no_placement", "alias"])
async def test_unknown_gpu_capacity_does_not_hold_the_cutover(tmp_path, store, unknown):
    """SDD §22.7: capacity Placement does not show - its credential refused, no Placement in the
    catalog - and a PCI alias request, whose devices nova.conf configures, let the cutover start:
    nothing is read that could hold it, as the pre-flight warned (DST_GPU_CAPACITY_UNKNOWN); an
    alias flavor needs its flavor list alone, and no Placement read."""
    from seamless_migrate.planning.preflight import GpuCapacity

    alias = {"pci_passthrough:alias": "a100:1"}
    dst = GateDestination(specs=alias if unknown == "alias" else None)
    vms = [gate_vm(1, flavor_extra_specs=alias) if unknown == "alias" else gate_vm(1)]
    h, plan, dst = await _gate_plan(
        tmp_path, store, vms, {"default_strategy": Strategy.cold}, dst=dst
    )
    if unknown != "alias":
        detail = "HTTP 403 policy.not_authorized" if unknown == "forbidden" else None
        dst.answer = GpuCapacity(status="unknown", reason=unknown, detail=detail, read_at=utcnow())
    await h.orch.start()
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert m.gpu_wait is None and _gpu_events(h, m.id) == []
    assert (dst.flavor_reads, len(dst.reads)) == ((1, 0) if unknown == "alias" else (1, 1))


class AliasDestination(StaticDestination):
    """A destination double that lists a PCI alias GPU flavor in its inventory and leaves its GPU
    reads to StaticDestination, which reads them from that inventory - as G8's test of a
    PCI-alias VM does (SDD §22.3); its capacity reads are counted."""

    def __init__(self) -> None:
        super().__init__()
        self.capacity_reads = 0

    async def inventory(self):
        from seamless_migrate.planning.gpu import gpu_requests

        inv = await super().inventory()
        alias = {"pci_passthrough:alias": "a100:1"}
        size = {"name": "p1.a100", "vcpus": 8, "ram_mb": 32768, "disk_gb": 80}
        inv.flavors.append({**size, "extra_specs": alias})
        inv.gpu_flavors = [{**size, "gpus": gpu_requests(alias)}]
        return inv

    async def gpu_capacity(self, requests):
        self.capacity_reads += 1
        return await super().gpu_capacity(requests)


async def test_a_pci_alias_vm_cuts_over_on_one_flavor_list_and_no_capacity_read(tmp_path, store):
    """SDD §22.7 (G6 review, G8's test): a PCI-alias VM is read for as any GPU VM - the flavor list
    tells that its GPU flavor asks for no vGPU, so Placement is not asked and nothing holds it -
    on a destination double whose GPU reads are StaticDestination's own."""
    from seamless_migrate.domain.models import Mappings

    alias = gate_vm(1, flavor="p1.src", flavor_extra_specs={"pci_passthrough:alias": "a100:1"})
    plan_kw = {
        "default_strategy": Strategy.cold,
        "mappings": Mappings(flavors={"p1.src": "p1.a100"}),
    }
    dst = AliasDestination()
    h, plan = await setup(tmp_path, store, [alias], plan_kw, destination=dst)
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert (m.gpu_wait, dst.capacity_reads) == (None, 0)
    assert "migration.gpu_wait" not in h.kinds()


async def test_a_failed_capacity_read_holds_the_cutover_until_a_read_answers(tmp_path, store):
    """SDD §22.7, §22.12: a read that fails - the flavor list or the capacity read raising, or
    Placement answering 503 - holds the cutover, the wait naming the failure by its status or kind,
    never by its text, and free unknown; once a read answers with room, the cutover starts."""
    from seamless_migrate.planning.preflight import GpuCapacity

    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}
    )
    dst.flavors_fail = ProviderError("dst-rhoso: gpu-host-a answered nothing")
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    first = (await _gpu_wait(h, mid)).gpu_wait
    # nothing names its pool yet: no read of the wait listed the flavor's requests
    assert (first.pool, first.needed, first.free) == ("", 0, None)
    assert "RHOSO 18.0" in first.reason and "ProviderError" in first.reason
    assert "gpu-host-a" not in first.reason
    dst.flavors_fail = None
    dst.answer = GpuCapacity(status="unknown", reason="error", detail="HTTP 503", read_at=utcnow())
    h.clock.advance(61)
    wait = (await _gpu_wait(h, mid, after=first.checked_at)).gpu_wait
    assert (wait.since, wait.pool, wait.needed, wait.free) == (first.since, GATE_POOL, 1, None)
    assert "HTTP 503" in wait.reason
    dst.answer = ProviderError("dst-rhoso: gpu-host-a timed out")
    h.clock.advance(61)
    raised = (await _gpu_wait(h, mid, after=wait.checked_at)).gpu_wait
    assert (raised.pool, raised.needed, raised.free) == (GATE_POOL, 1, None)
    assert "ProviderError" in raised.reason and "gpu-host-a" not in raised.reason
    # a failed flavor list now keeps the pool and units its wait names
    dst.flavors_fail = ProviderError("dst-rhoso: gpu-host-a answered nothing")
    h.clock.advance(61)
    kept = (await _gpu_wait(h, mid, after=raised.checked_at)).gpu_wait
    assert (kept.pool, kept.needed, kept.free) == (GATE_POOL, 1, None)
    assert "flavors" in kept.reason and kept.since == first.since
    assert (await h.migration(mid)).phase == P.ready
    dst.flavors_fail = None
    dst.answer = None
    dst.free = dict(GATE_ROOM)
    h.clock.advance(61)
    m = await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    assert m.gpu_wait is None
    assert [e.data["waiting"] for e in _gpu_events(h, mid)] == [True, False]


async def test_a_capacity_read_older_than_a_minute_is_read_again(tmp_path, store):
    """SDD §5.4 rule 5, §22.7: a read is good for 60 s from its answer - the ticks in between read
    nothing - and is made again once it is older, while rules 1–4 hold; in demo mode the minute is
    model time (÷ demo_speed)."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}
    )
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    first = (await _gpu_wait(h, mid)).gpu_wait
    h.clock.advance(59)
    await asyncio.sleep(0.1)  # many ticks
    assert (len(dst.reads), dst.flavor_reads) == (1, 1)
    h.clock.advance(2)
    again = (await _gpu_wait(h, mid, after=first.checked_at)).gpu_wait
    await h.orch.pause_plan(plan.id, "alice")  # the demo's orchestrator below ticks its own plan
    await h.orch.stop()
    assert (len(dst.reads), dst.flavor_reads) == (2, 2)
    assert again.since == first.since and again.checked_at > first.checked_at

    settings = make_settings(tmp_path / "demo", demo=True, demo_speed=60.0)
    h, plan, dst = await _gate_plan(
        tmp_path / "demo",
        store,
        [gate_vm(2)],
        {"default_strategy": Strategy.cold},
        settings=settings,
    )
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-2")).id
    first = (await _gpu_wait(h, mid)).gpu_wait
    h.clock.advance(0.5)  # 30 model seconds
    await asyncio.sleep(0.1)
    assert len(dst.reads) == 1
    h.clock.advance(0.6)  # past the model minute
    await _gpu_wait(h, mid, after=first.checked_at)
    await h.orch.stop()
    assert len(dst.reads) == 2


async def test_a_slow_capacity_read_never_delays_the_tick(tmp_path, store):
    """SDD §5.4 rule 5, §22.7: the read is a task of its own, one per migration however long it
    takes: while the destination does not answer it, that cutover waits, and the tick goes on -
    another VM of the plan cuts over meanwhile."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1), vm(2)], {"default_strategy": Strategy.cold}
    )
    dst.free = dict(GATE_ROOM)
    dst.hold = asyncio.Event()  # Placement does not answer
    await h.orch.start()
    gpu, other = await h.by_vm(plan.id, "vm-1"), await h.by_vm(plan.id, "vm-2")
    await h.wait_phase(other.id, P.completed)
    ticks = h.orch.tick_stats["count"]
    await asyncio.sleep(0.2)
    m = await h.migration(gpu.id)
    assert h.orch.tick_stats["count"] > ticks and h.orch.health()["healthy"]
    assert (m.phase, m.gpu_wait) == (P.ready, None)
    assert len(dst.reads) == 1, "one read at a time, never one per tick"
    dst.hold.set()
    m = await h.wait_phase(gpu.id, P.completed)
    await h.orch.stop()
    assert len(dst.reads) == 1 and m.gpu_wait is None


async def test_a_force_cutover_still_waits_for_gpu_capacity(tmp_path, store):
    """SDD §5.4 rule 5, §22.7: a Force cutover, requested with the window bypassed, still waits for
    GPU capacity - it converges, waits with its wait recorded, and cuts over once a read finds
    room."""
    window = CutoverWindow(
        start=Clock()() + timedelta(days=1), end=Clock()() + timedelta(days=1, hours=4)
    )
    plan_kw = {
        "default_strategy": Strategy.warm,
        "auto_cutover": False,
        "cutover_window": window,
        "downtime_slo_s": 1,
        "convergence_threshold_bytes": 0,
        "max_sync_passes": 50,
    }
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1, size_gb=200, used_gb=150.0)], plan_kw
    )
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await h.wait_phase(mid, P.syncing)
    await h.orch.request_cutover(mid, "sari", force_window=True)
    await h.orch.force_cutover(mid, "sari", "The change window closes at 22:00")
    m = await _gpu_wait(h, mid)
    await asyncio.sleep(0.2)  # many ticks
    m = await h.migration(mid)
    assert (m.converged_by, m.force_window) == ("force", True)
    assert m.phase == P.awaiting_cutover and P.cutover not in h.history(m)
    dst.free = dict(GATE_ROOM)
    h.clock.advance(61)
    m = await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    assert m.gpu_wait is None


async def test_gpu_wait_events_are_emitted_once_when_the_wait_begins_and_ends(tmp_path, store):
    """SDD §4.3, §22.7: migration.gpu_wait is written once when the wait begins - however many reads
    find no room - and once when a read finds room, before the cutover's phase change; each holds
    the pool, the units needed and free, and its message names the destination by its label."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}
    )
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    wait = (await _gpu_wait(h, mid)).gpu_wait
    for _ in range(2):  # two more reads without room
        h.clock.advance(61)
        wait = (await _gpu_wait(h, mid, after=wait.checked_at)).gpu_wait
    dst.free = dict(GATE_ROOM)
    h.clock.advance(61)
    await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    waits = _gpu_events(h, mid)
    assert [e.data for e in waits] == [
        {"waiting": True, "pool": GATE_POOL, "needed": 1, "free": 0},
        {"waiting": False, "pool": GATE_POOL, "needed": 1, "free": 1},
    ]
    assert all("RHOSO 18.0" in e.message and e.plan_id == plan.id for e in waits)
    cutover = next(
        e
        for e in h.store.events(since_seq=0, limit=100000)
        if e.migration_id == mid and e.kind == "migration.phase" and e.data["to"] == "cutover"
    )
    assert waits[1].seq < cutover.seq and len(dst.reads) == 4


async def test_a_retry_or_a_cancel_drops_the_gpu_wait(tmp_path, store):
    """SDD §5.4, §22.7: leaving the waiting phases drops gpu_wait in the same write - a cancel in
    ready, a keep-warm pass that fails -, which migration.phase announces; the retry starts with
    none, and its own first read begins a wait anew."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}
    )
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await _gpu_wait(h, mid)
    cancelled = await h.orch.cancel(mid, "alice", "descoped")
    await h.orch.stop()
    assert (cancelled.phase, cancelled.gpu_wait) == (P.cancelled, None)
    assert (await h.migration(mid)).gpu_wait is None
    assert [e.data["waiting"] for e in _gpu_events(h, mid)] == [True]

    settings = make_settings(tmp_path / "b")
    failing = asyncio.Event()

    async def keep_warm(ctx):
        if failing.is_set():
            raise PermanentStepError("the keep-warm pass lost its snapshot")
        return await executor.sim.run(StepName.SYNC, ctx)

    executor = ScriptedExecutor(settings, hooks={StepName.SYNC: keep_warm})
    h, plan, dst = await _gate_plan(
        tmp_path / "b",
        store,
        [gate_vm(2)],
        {"default_strategy": Strategy.warm, "keep_warm_interval_s": 900},
        executor=executor,
        settings=settings,
    )
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-2")).id
    old = (await _gpu_wait(h, mid)).gpu_wait
    failing.set()
    h.clock.advance(901)
    failed = await h.wait_phase(mid, P.failed)
    assert failed.gpu_wait is None
    failing.clear()
    retried = await h.orch.retry(mid, "alice")
    assert (retried.phase, retried.gpu_wait) == (P.ready, None)
    again = (await _gpu_wait(h, mid, after=old.checked_at)).gpu_wait
    await h.orch.stop()
    assert again.since > old.since
    assert [e.data["waiting"] for e in _gpu_events(h, mid)] == [True, True]


async def test_a_validation_drops_the_gpu_wait_and_the_read_made_before_it(tmp_path, store):
    """SDD §5.4, §22.7: a validation of the paused plan takes a waiting migration through
    validating in one write, which drops its wait; the read made before no longer counts - its
    minute is not over, but its stay at the gate is -, so the restarted plan reads anew."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}
    )
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    old = (await _gpu_wait(h, mid)).gpu_wait
    await h.orch.pause_plan(plan.id, "alice")
    dst.free = dict(GATE_ROOM)  # the validation sees room again
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.migration(mid)
    assert (m.phase, m.gpu_wait) == (P.ready, None)
    dst.free = dict(GATE_FULL)
    await h.orch.start_plan(plan.id, "alice")
    again = (await _gpu_wait(h, mid, after=old.checked_at)).gpu_wait
    await h.orch.stop()
    assert again.since > old.since and len(dst.reads) == 2
    assert [e.data["waiting"] for e in _gpu_events(h, mid)] == [True, True]


async def _gpu_outcome_with_room(h, mid: str):
    """The migration's GPU read once one is recorded with room."""

    async def recorded():
        outcome = h.orch._gpu_outcomes.get(mid)
        return outcome if outcome is not None and outcome.read.room else None

    return await _eventually(recorded)


async def test_a_read_made_before_another_gpu_cutover_began_never_lets_a_second_cut_over(
    tmp_path, store
):
    """SDD §5.4 rule 5, §22.7 (G6 review): two cutover slots, one unit free, both reads out at once;
    B's read cannot be recorded - a person's action holds B's migration - until A has cut over on
    its own read and ended its step, its server holding the unit. B's read, made before A's cutover
    began, no longer counts whatever phase A is in now: B reads again and waits."""
    settings = make_settings(tmp_path, max_concurrent_cutovers=2)
    gates: dict[str, asyncio.Event] = {}
    started: list[str] = []
    executor = ScriptedExecutor(
        settings, hooks={StepName.CUTOVER: blocking_cutover(gates, started)}
    )
    h, plan, dst = await _gate_plan(
        tmp_path,
        store,
        [gate_vm(1), gate_vm(2)],
        {"default_strategy": Strategy.cold},
        dst=GateDestination(free={GATE_POOL: 2, "VGPU": 2}),  # the validation counts both
        executor=executor,
        settings=settings,
    )
    dst.free = dict(GATE_ROOM)
    dst.hold = asyncio.Event()
    await h.orch.start()
    a, b = await h.by_vm(plan.id, "vm-1"), await h.by_vm(plan.id, "vm-2")

    async def both_read():
        return len(dst.reads) == 2

    await _eventually(both_read)
    held = h.orch._lock(b.id)
    await held.acquire()
    try:
        dst.hold.set()

        async def a_started():
            return started == ["vm-1"]

        await _eventually(a_started)
        dst.free = dict(GATE_FULL)  # A's server holds the unit
        gates.setdefault("vm-1", asyncio.Event()).set()
        await h.wait_phase(a.id, P.completed)
    finally:
        held.release()
    m = await _gpu_wait(h, b.id)
    await asyncio.sleep(0.1)  # many ticks
    await h.orch.stop()
    assert started == ["vm-1"] and (await h.migration(b.id)).phase == P.ready
    assert (m.gpu_wait.pool, m.gpu_wait.needed, m.gpu_wait.free) == (GATE_POOL, 1, 0)


async def test_a_keep_warm_pass_never_hides_a_gpu_cutover_begun_after_the_read(tmp_path, store):
    """SDD §22.7 (G6 review): B's read finds room while B runs a keep-warm pass; A cuts over and
    ends its step meanwhile, its server holding the unit. Back at its gate, B does not cut over on
    that read - A's cutover began after it -: it reads again and waits."""
    settings = make_settings(tmp_path)
    gates: dict[str, asyncio.Event] = {}
    started: list[str] = []
    passing = asyncio.Event()
    keep_b_in_its_pass: list[str] = []

    async def keep_warm(ctx):
        if keep_b_in_its_pass and ctx.migration.vm.source_id == "vm-2":
            await passing.wait()
        return await executor.sim.run(StepName.SYNC, ctx)

    executor = ScriptedExecutor(
        settings,
        hooks={StepName.CUTOVER: blocking_cutover(gates, started), StepName.SYNC: keep_warm},
    )
    h, plan, dst = await _gate_plan(
        tmp_path,
        store,
        [gate_vm(1), gate_vm(2)],
        {"default_strategy": Strategy.warm, "auto_cutover": False},
        dst=GateDestination(free={GATE_POOL: 2, "VGPU": 2}),
        executor=executor,
        settings=settings,
    )
    dst.free = dict(GATE_ROOM)
    await h.orch.start()
    a, b = await h.by_vm(plan.id, "vm-1"), await h.by_vm(plan.id, "vm-2")
    await h.wait_phase(a.id, P.awaiting_cutover)
    await h.wait_phase(b.id, P.awaiting_cutover)
    dst.hold = asyncio.Event()
    await h.orch.request_cutover(b.id, "sari", force_window=True)

    async def b_reading():
        return len(dst.reads) == 1

    await _eventually(b_reading)
    keep_b_in_its_pass.append("on")
    await h.orch.request_sync(b.id, "sari")  # a keep-warm pass while its read is out
    dst.hold.set()
    await _gpu_outcome_with_room(h, b.id)
    assert (await h.migration(b.id)).phase == P.syncing
    dst.hold = None
    await h.orch.request_cutover(a.id, "sari", force_window=True)

    async def a_started():
        return "vm-1" in started

    await _eventually(a_started)
    dst.free = dict(GATE_FULL)  # A's server holds the unit
    gates.setdefault("vm-1", asyncio.Event()).set()
    await h.wait_phase(a.id, P.completed)
    passing.set()  # B's pass ends: B is back at its gate
    m = await _gpu_wait(h, b.id)
    await asyncio.sleep(0.1)  # many ticks
    await h.orch.stop()
    assert "vm-2" not in started and (m.gpu_wait.needed, m.gpu_wait.free) == (1, 0)


async def test_a_cutover_of_a_vm_without_a_gpu_leaves_a_gpu_read_as_it_is(tmp_path, store):
    """SDD §22.7 (G6 review): a cutover begun after a GPU read began drops the read only when it
    asks of the pools the read counted: a VM without a GPU that cuts over meanwhile leaves it as it
    is, and the GPU VM cuts over on that one read - no GPU VM starves behind other cutovers."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1), vm(2)], {"default_strategy": Strategy.cold}
    )
    dst.free = dict(GATE_ROOM)
    dst.hold = asyncio.Event()  # the GPU VM's read is out while the other VM cuts over
    await h.orch.start()
    gpu, other = await h.by_vm(plan.id, "vm-1"), await h.by_vm(plan.id, "vm-2")
    await h.wait_phase(other.id, P.cutover, P.verifying, P.completed)
    dst.hold.set()
    await h.wait_plan(plan.id, PlanStatus.completed)
    await h.orch.stop()
    assert len(dst.reads) == 1
    assert (await h.migration(gpu.id)).gpu_wait is None


class TwoClassDestination(GateDestination):
    """GateDestination with a second vGPU flavor, g1.mdev-x, of another resource class: a
    CUSTOM_MDEV_X request, a vGPU by its declared profile (SDD §22.2)."""

    MDEV = {"resources:CUSTOM_MDEV_X": "1"}
    SIZE = {"name": "g1.mdev-x", "vcpus": 8, "ram_mb": 32768, "disk_gb": 80}

    def gpu_flavor_list(self) -> list[dict]:
        from seamless_migrate.planning.gpu import gpu_requests

        return [
            *super().gpu_flavor_list(),
            {**self.SIZE, "gpus": gpu_requests(self.MDEV, "A100-4C")},
        ]

    async def inventory(self):
        inv = await super().inventory()
        inv.flavors.append({**self.SIZE, "extra_specs": dict(self.MDEV)})
        return inv


async def test_a_gpu_cutover_in_other_pools_leaves_a_gpu_read_as_it_is(tmp_path, store):
    """SDD §22.7 (G6 review): a read is dropped for a cutover begun after it only when that cutover
    asks of the pools it counted: two GPU VMs of two resource classes read at once, and the second
    cuts over on its read though the first's cutover began after it."""
    from seamless_migrate.domain.models import Mappings

    room = {GATE_POOL: 1, "VGPU": 1, "CUSTOM_MDEV_X": 1}
    mappings = Mappings(flavors={"g1.vgpu": GATE_FLAVOR, "g1.src2": "g1.mdev-x"})
    h, plan, dst = await _gate_plan(
        tmp_path,
        store,
        [gate_vm(1), gate_vm(2, flavor="g1.src2")],
        {"default_strategy": Strategy.cold, "mappings": mappings},
        dst=TwoClassDestination(free=room),
    )
    dst.free = dict(room)
    dst.hold = asyncio.Event()
    await h.orch.start()

    async def both_read():
        return len(dst.reads) == 2

    await _eventually(both_read)
    dst.hold.set()
    await h.wait_plan(plan.id, PlanStatus.completed)
    await h.orch.stop()
    assert len(dst.reads) == 2, "neither read was made again"


async def test_gpu_reads_start_only_while_a_cutover_slot_is_left(tmp_path, store):
    """SDD §5.4 rule 5, §22.7 (G6 review): six GPU VMs at their gates and one cutover slot - one
    read is out at a time, never one per VM -, and all six cut over in turn."""
    settings = make_settings(tmp_path, max_concurrent_cutovers=1)
    room = {GATE_POOL: 6, "VGPU": 6}
    h, plan, dst = await _gate_plan(
        tmp_path,
        store,
        [gate_vm(i) for i in range(1, 7)],
        {"default_strategy": Strategy.cold},
        dst=GateDestination(free=room),
        settings=settings,
    )
    dst.free = dict(room)
    dst.hold = asyncio.Event()
    await h.orch.start()

    async def reading():
        return dst.reads

    await _eventually(reading)
    await asyncio.sleep(0.2)  # many ticks
    assert (len(dst.reads), dst.flavor_reads) == (1, 1)
    dst.hold.set()
    await h.wait_plan(plan.id, PlanStatus.completed, timeout=20)
    await h.orch.stop()
    assert len(dst.reads) >= 6


async def test_a_gpu_read_wakes_the_tick_and_counts_some_ticks_in_demo_mode(tmp_path, store):
    """SDD §22.7 (G6 review): a read's answer wakes the tick, which takes it at once; in demo mode a
    read counts its model minute but never under a few ticks, so at demo_speed 6000 the cutover
    starts on its first reads instead of reading for ever."""
    settings = make_settings(tmp_path, tick_s=2.0)  # no tick of its own soon after the read
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}, settings=settings
    )
    dst.free = dict(GATE_ROOM)
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await h.wait_phase(mid, P.cutover, P.verifying, P.completed, timeout=1.5)
    await h.orch.stop()

    settings = make_settings(tmp_path / "demo", demo=True, demo_speed=6000.0, tick_s=0.1)
    h, plan, dst = await _gate_plan(
        tmp_path / "demo",
        store,
        [gate_vm(2)],
        {"default_strategy": Strategy.cold},
        settings=settings,
    )
    dst.free = dict(GATE_ROOM)
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-2")).id
    await h.wait_phase(mid, P.cutover, P.verifying, P.completed, timeout=6)
    await h.orch.stop()
    assert len(dst.reads) <= 3


async def test_a_passthrough_vm_mapped_to_a_vgpu_flavor_waits_for_gpu_capacity(tmp_path, store):
    """SDD §22.4, §22.7 (G6 review): a 17.1 VM with a PCI passthrough GPU that its plan maps to an
    18.0 vGPU flavor - a change of kind the pre-flight accepts - is read for like any GPU VM: the
    pool empties after the validation, and it waits; once the unit is free, it cuts over."""
    from seamless_migrate.domain.models import Mappings

    passthrough = gate_vm(
        1, flavor="p1.src", flavor_extra_specs={"pci_passthrough:alias": "a100:1"}
    )
    plan_kw = {
        "default_strategy": Strategy.cold,
        "mappings": Mappings(flavors={"p1.src": GATE_FLAVOR}),
    }
    h, plan, dst = await _gate_plan(tmp_path, store, [passthrough], plan_kw)
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    m = await _gpu_wait(h, mid)
    assert (m.phase, m.gpu_wait.pool, m.gpu_wait.needed, m.gpu_wait.free) == (
        P.ready,
        GATE_POOL,
        1,
        0,
    )
    dst.free = dict(GATE_ROOM)
    h.clock.advance(61)
    m = await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    assert m.gpu_wait is None


async def test_a_crashed_gpu_read_holds_the_cutover_and_says_so(tmp_path, store, caplog):
    """SDD §22.7 (G6 review): a read that fails outside the destination's answers - here the store
    it counts the other cutovers from - holds the cutover, as a read without room does, and the
    log says so; the next read, a minute later, lets it cut over."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}
    )
    dst.free = dict(GATE_ROOM)  # room: only the crash could hold it
    crashed: list[bool] = []
    count = h.orch._cutovers_without_server

    async def crashing(*args):
        if not crashed:
            crashed.append(True)
            raise RuntimeError("the store went away")
        return await count(*args)

    h.orch._cutovers_without_server = crashing
    caplog.set_level(logging.WARNING, logger="seamless_migrate.orchestrator")
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id

    async def crash_seen():
        return crashed

    await _eventually(crash_seen)
    await asyncio.sleep(0.2)  # many ticks
    assert (await h.migration(mid)).phase == P.ready
    assert "its cutover waits for the next read" in caplog.text
    h.clock.advance(61)
    await h.wait_phase(mid, P.completed)
    await h.orch.stop()


async def test_a_restarted_orchestrator_goes_on_with_a_stored_gpu_wait(tmp_path, store):
    """SDD §4.3, §22.7 (G6 review): after a restart the stored wait goes on - the new process's
    first read finds no room and updates it, emitting no second migration.gpu_wait (waiting true)
    -, and a read with room ends it once."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}
    )
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    first = (await _gpu_wait(h, mid)).gpu_wait
    await h.orch.stop()
    restarted = build_harness(tmp_path, store, [gate_vm(1)], destination=dst, clock=h.clock)
    await restarted.orch.start()
    again = (await _gpu_wait(restarted, mid, after=first.checked_at)).gpu_wait
    assert again.since == first.since
    dst.free = dict(GATE_ROOM)
    restarted.clock.advance(61)
    await restarted.wait_phase(mid, P.completed)
    await restarted.orch.stop()
    assert [e.data["waiting"] for e in _gpu_events(restarted, mid)] == [True, False]


async def test_two_gpu_cutovers_with_room_for_both_both_cut_over(tmp_path, store):
    """SDD §22.7 (G6 review): two GPU VMs whose reads find room for both cut over, though each read
    was made before the other's cutover began - the second one reads again and counts the first."""
    room = {GATE_POOL: 2, "VGPU": 2}
    h, plan, dst = await _gate_plan(
        tmp_path,
        store,
        [gate_vm(1), gate_vm(2)],
        {"default_strategy": Strategy.cold},
        dst=GateDestination(free=room),
    )
    dst.free = dict(room)
    await h.orch.start()
    await h.wait_plan(plan.id, PlanStatus.completed)
    await h.orch.stop()
    assert all(m.phase == P.completed for m in await h.migrations(plan.id))
    assert "migration.gpu_wait" not in h.kinds()


async def test_a_read_in_flight_across_a_validation_is_not_recorded_on_the_new_stay(
    tmp_path, store
):
    """SDD §22.7 (G6 review): a read still out when a validation takes the migration through
    validating answers after it - no room -, and is recorded on no stay: the migration it was made
    for left its gate. Started again, the new stay reads for itself."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}
    )
    dst.hold = asyncio.Event()
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id

    async def reading():
        return dst.reads

    await _eventually(reading)
    await h.orch.pause_plan(plan.id, "alice")
    dst.free = dict(GATE_ROOM)  # the validation sees room
    await h.orch.validate_plan(plan.id, "alice")
    dst.free = dict(GATE_FULL)
    dst.hold.set()  # the read made before the validation answers: no room
    await asyncio.sleep(0.2)
    m = await h.migration(mid)
    assert (m.phase, m.gpu_wait, _gpu_events(h, mid)) == (P.ready, None, [])
    assert mid not in h.orch._gpu_outcomes
    dst.hold = None
    await h.orch.start_plan(plan.id, "alice")
    await _gpu_wait(h, mid)
    await h.orch.stop()
    assert len(dst.reads) == 2


async def test_a_migration_held_by_its_advisor_decision_is_not_read_for(tmp_path, store):
    """SDD §5.4, §14.6 (G6 review): a migration whose advisor decision waits for approval starts
    no cutover, so no GPU capacity is read for it; once the hold is gone, it is."""
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}
    )
    mid = (await h.by_vm(plan.id, "vm-1")).id
    cur, v = await h.orch._load(mid)
    cur.advisor_hold = "rec-000000000001"
    await h.orch._save(cur, v)
    await h.orch.start()
    await asyncio.sleep(0.2)  # many ticks
    assert (dst.flavor_reads, len(dst.reads)) == (0, 0)
    async with h.orch._lock(mid):
        cur, v = await h.orch._load(mid)
        cur.advisor_hold = None
        await h.orch._save(cur, v)
    await _gpu_wait(h, mid)
    await h.orch.stop()
    assert len(dst.reads) == 1


class GateDestinations:
    """A registry that hands each destination provider its own double, by id (SDD §10)."""

    def __init__(self, source, by_id) -> None:
        self.source, self.by_id = source, by_id

    def get(self, provider):
        return self.by_id[provider.id] if provider.role == "destination" else self.source


async def _set_advisor_hold(h, mid: str, hold: str | None) -> None:
    async with h.orch._lock(mid):
        cur, v = await h.orch._load(mid)
        cur.advisor_hold = hold
        await h.orch._save(cur, v)


@pytest.mark.parametrize("slots", [1, 3, 5])
async def test_a_destination_whose_gpu_reads_hang_never_holds_another_destinations(
    tmp_path, store, slots
):
    """SDD §5.4 rule 5, §22.7 (G6 review): plan H's destination never answers its GPU reads; plan
    G's has room. Reads and the places they hold count per destination, so H's hung reads - at
    most one per cutover slot and one fewer than GPU_READS_MAX, the worker threads they take -
    hold none of G's: G's GPU VMs read and cut over while H's wait."""
    from seamless_migrate.domain.models import Mappings
    from seamless_migrate.orchestrator import GPU_READS_MAX

    settings = make_settings(tmp_path, max_concurrent_cutovers=slots)
    room = {GATE_POOL: 4, "VGPU": 4}
    hung, healthy = GateDestination(free=room), GateDestination(free=room)
    vms = [gate_vm(i) for i in range(1, 8)]
    h = build_harness(tmp_path, store, vms, destination=hung, settings=settings)
    h.orch.providers = GateDestinations(h.source, {DST_PROVIDER.id: hung, "dst-two": healthy})
    store.put("provider", DST_PROVIDER.model_copy(update={"id": "dst-two", "name": "RHOSO two"}))
    kw = {"default_strategy": Strategy.cold, "mappings": Mappings(flavors={"g1.vgpu": GATE_FLAVOR})}
    plan_h = plan_for(vms[:4], name="H", **kw)
    plan_g = plan_for(vms[4:], name="G", destination_provider_id="dst-two", **kw)
    for plan in (plan_h, plan_g):  # H is ticked first
        store.put("plan", plan)
        await h.orch.validate_plan(plan.id, "alice")
        await h.orch.start_plan(plan.id, "alice")
    hung.hold = asyncio.Event()  # H's reads hang, as a provider call may for minutes
    await h.orch.start()
    try:
        await h.wait_plan(plan_g.id, PlanStatus.completed, timeout=20)
        assert len(hung.reads) == min(slots, GPU_READS_MAX - 1) and len(healthy.reads) >= 3
        assert {m.phase for m in await h.migrations(plan_h.id)} == {P.ready}
    finally:
        hung.hold.set()
        await h.orch.pause_plan(plan_h.id, "alice")
        await h.orch.stop()


async def test_a_gpu_vm_keeps_its_cutover_slot_while_its_read_is_out(tmp_path, store):
    """SDD §5.4 rule 5, §22.7 (G6 review): one cutover slot, the GPU VM first in its wave order and
    four VMs without a GPU behind it. While its read is out, the tick gives its slot to none of
    them: the GPU VM cuts over first, after one read, and the others after it."""
    settings = make_settings(tmp_path, max_concurrent_cutovers=1)
    started: list[str] = []

    async def recording(ctx):
        started.append(ctx.migration.vm.source_id)
        return await executor.sim.run(StepName.CUTOVER, ctx)

    executor = ScriptedExecutor(settings, hooks={StepName.CUTOVER: recording})
    h, plan, dst = await _gate_plan(
        tmp_path,
        store,
        [gate_vm(1)] + [vm(i) for i in range(2, 6)],
        {"default_strategy": Strategy.cold},
        executor=executor,
        settings=settings,
    )
    dst.free = dict(GATE_ROOM)
    dst.hold = asyncio.Event()  # its read is out a while
    await h.orch.start()

    async def reading():
        return dst.reads

    await _eventually(reading)
    await asyncio.sleep(0.2)  # many ticks
    assert started == []
    dst.hold.set()
    await h.wait_plan(plan.id, PlanStatus.completed, timeout=20)
    await h.orch.stop()
    assert started[0] == "vm-1" and len(started) == 5 and len(dst.reads) == 1


async def test_a_gpu_vm_whose_cutover_cannot_start_yet_keeps_its_place(tmp_path, store):
    """SDD §22.7 (G6 review): one cutover slot; B's GPU VM read and found room, but its cutover
    cannot start - a person's action holds its plan -, and A's VM of the same destination comes to
    its gate behind it in the tick's order. While B's read counts, A's VM does not take the slot:
    once B's plan is free, B cuts over first."""
    from seamless_migrate.domain.models import Mappings

    settings = make_settings(tmp_path, max_concurrent_cutovers=1)
    started: list[str] = []

    async def recording(ctx):
        started.append(ctx.migration.vm.source_id)
        return await executor.sim.run(StepName.CUTOVER, ctx)

    executor = ScriptedExecutor(settings, hooks={StepName.CUTOVER: recording})
    dst = GateDestination()
    h = build_harness(
        tmp_path, store, [gate_vm(1), vm(2)], executor=executor, settings=settings, destination=dst
    )
    kw = {"default_strategy": Strategy.cold, "mappings": Mappings(flavors={"g1.vgpu": GATE_FLAVOR})}
    plan_b = plan_for([gate_vm(1)], name="B", **kw)
    plan_a = plan_for([vm(2)], name="A", **kw)
    for plan in (plan_b, plan_a):  # B's plan is ticked first
        store.put("plan", plan)
        await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan_b.id, "alice")
    b = await h.by_vm(plan_b.id, "vm-1")
    busy = h.orch.plan_lock(plan_b.id)
    await busy.acquire()
    try:
        await h.orch.start()
        await _gpu_outcome_with_room(h, b.id)
        # A comes to its gate once B holds its place (its pre-staging could otherwise end first)
        await h.orch.start_plan(plan_a.id, "alice")
        await asyncio.sleep(0.2)  # many ticks: B keeps its place
        assert started == []
    finally:
        busy.release()
    await h.wait_plan(plan_a.id, PlanStatus.completed)
    await h.orch.stop()
    assert started == ["vm-1", "vm-2"]


async def test_a_gpu_vm_waiting_for_capacity_leaves_its_cutover_slot_to_the_vms_behind_it(
    tmp_path, store
):
    """SDD §22.7 (G6 review): a read that finds no room releases the GPU VM's place at once: with
    one cutover slot, the VMs behind it cut over while it waits for GPU capacity."""
    settings = make_settings(tmp_path, max_concurrent_cutovers=1)
    h, plan, dst = await _gate_plan(
        tmp_path,
        store,
        [gate_vm(1), vm(2), vm(3)],
        {"default_strategy": Strategy.cold},
        settings=settings,
    )
    await h.orch.start()
    gpu = await h.by_vm(plan.id, "vm-1")
    await _gpu_wait(h, gpu.id)
    for vm_id in ("vm-2", "vm-3"):
        await h.wait_phase((await h.by_vm(plan.id, vm_id)).id, P.completed)
    m = await h.migration(gpu.id)
    await h.orch.pause_plan(plan.id, "alice")
    await h.orch.stop()
    assert m.phase == P.ready and m.gpu_wait is not None


async def test_a_destinations_gpu_reads_never_pass_its_cutover_slots(tmp_path, store):
    """SDD §22.7 (G6 review): a destination has at most max_concurrent_cutovers reads out - the
    worker threads its reads take -, also when GPU VMs ahead of the ones reading come to their
    gates later: with two slots and the destination silent, a third read waits for one to end."""
    settings = make_settings(tmp_path, max_concurrent_cutovers=2)
    room = {GATE_POOL: 3, "VGPU": 3}
    h, plan, dst = await _gate_plan(
        tmp_path,
        store,
        [gate_vm(1), gate_vm(2), gate_vm(3)],
        {"default_strategy": Strategy.cold},
        dst=GateDestination(free=room),
        settings=settings,
    )
    first, second, third = [await h.by_vm(plan.id, f"vm-{i}") for i in (1, 2, 3)]
    for m in (first, second):  # they come to their gates later
        await _set_advisor_hold(h, m.id, "rec-000000000001")
    dst.hold = asyncio.Event()  # the destination answers no read
    await h.orch.start()

    async def reads(n: int):
        async def counted():
            return len(dst.reads) == n

        return await _eventually(counted)

    await reads(1)  # the third one reads
    await _set_advisor_hold(h, second.id, None)
    await reads(2)  # the second one, ahead of it, reads too
    await _set_advisor_hold(h, first.id, None)
    await asyncio.sleep(0.2)  # many ticks: the first one, ahead of both, waits for a read to end
    assert len(dst.reads) == 2
    dst.hold.set()
    await h.orch.pause_plan(plan.id, "alice")
    await h.orch.stop()


async def test_a_demo_gpu_read_counts_at_least_three_ticks(tmp_path, store):
    """SDD §22.7 (G6 review): in demo mode a read counts its model minute but never under three
    ticks - at demo_speed 6000 and a 0.2 s tick, 0.6 s rather than 10 ms -, so a read without room
    is not made again on the next tick, and is once its three ticks are over."""
    settings = make_settings(tmp_path, demo=True, demo_speed=6000.0, tick_s=0.2)
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1)], {"default_strategy": Strategy.cold}, settings=settings
    )
    assert h.orch._gpu_read_s() == pytest.approx(0.6)
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    first = (await _gpu_wait(h, mid)).gpu_wait
    await asyncio.sleep(0.25)  # past a tick, within three
    assert len(dst.reads) == 1
    await _gpu_wait(h, mid, after=first.checked_at)
    await h.orch.pause_plan(plan.id, "alice")
    await h.orch.stop()


@pytest.mark.parametrize("limit", ["wave", "wave-precopy", "steps", "precopy"])
async def test_a_gpu_vm_keeps_its_wave_room_and_its_step_while_its_read_is_out(
    tmp_path, store, limit
):
    """SDD §5.4 rule 5, §22.7 (G6 final review): a held place keeps its wave's room and a step too,
    not only its cutover slot. The GPU VM first, four VMs behind it, and a wave of max_parallel 1
    or max_concurrent_migrations of 1 - the VMs behind cutting over, or starting their pre-copies
    -: nothing behind it starts while its read is out, and its cutover comes first, after one
    read."""
    waved = limit.startswith("wave")
    settings = make_settings(tmp_path, max_concurrent_migrations=10 if waved else 1)
    steps: list[tuple[str, str]] = []

    def recording(step: StepName):
        async def hook(ctx):
            steps.append((ctx.migration.vm.source_id, str(step)))
            return await executor.sim.run(step, ctx)

        return hook

    executor = ScriptedExecutor(
        settings,
        hooks={
            StepName.CUTOVER: recording(StepName.CUTOVER),
            StepName.PRECOPY: recording(StepName.PRECOPY),
        },
    )
    vms = [gate_vm(1)] + [vm(i) for i in range(2, 6)]
    plan_kw: dict = {"default_strategy": Strategy.cold}
    if waved:
        ids = [v.source_id for v in vms]
        plan_kw["waves"] = [Wave(id="wave-1", name="All", order=1, vm_ids=ids, max_parallel=1)]
    if limit.endswith("precopy"):
        plan_kw["strategy_overrides"] = {f"vm-{i}": Strategy.warm for i in range(2, 6)}
    h, plan, dst = await _gate_plan(
        tmp_path, store, vms, plan_kw, executor=executor, settings=settings
    )
    dst.free = dict(GATE_ROOM)
    dst.hold = asyncio.Event()  # its read is out a while
    await h.orch.start()

    async def reading():
        return dst.reads

    await _eventually(reading)
    await asyncio.sleep(0.2)  # many ticks
    assert steps == []
    dst.hold.set()
    await h.wait_plan(plan.id, PlanStatus.completed, timeout=30)
    await h.orch.stop()
    assert steps[0] == ("vm-1", "cutover") and len(dst.reads) == 1


async def test_a_gpu_vm_keeps_its_step_from_the_keep_warm_passes_behind_it(tmp_path, store):
    """SDD §22.7 (G6 final review): with max_concurrent_migrations of 1, a warm VM behind the GPU VM
    whose keep-warm pass falls due while the GPU VM's read is out runs no pass in its step; the GPU
    VM cuts over first, and the pass follows."""
    settings = make_settings(tmp_path, max_concurrent_migrations=1)
    plan_kw = {
        "default_strategy": Strategy.cold,
        "auto_cutover": False,
        "strategy_overrides": {"vm-2": Strategy.warm},
    }
    h, plan, dst = await _gate_plan(
        tmp_path, store, [gate_vm(1), vm(2)], plan_kw, settings=settings
    )
    dst.free = dict(GATE_ROOM)
    await h.orch.start()
    gpu, warm = await h.by_vm(plan.id, "vm-1"), await h.by_vm(plan.id, "vm-2")
    passes = len((await h.wait_phase(warm.id, P.awaiting_cutover)).sync_passes)
    dst.hold = asyncio.Event()  # the GPU VM's read is out a while
    await h.orch.request_cutover(gpu.id, "sari", force_window=True)

    async def reading():
        return dst.reads

    await _eventually(reading)
    h.clock.advance(901)  # the warm VM's keep-warm pass falls due
    await asyncio.sleep(0.2)  # many ticks
    assert len((await h.migration(warm.id)).sync_passes) == passes
    dst.hold.set()
    await h.wait_phase(gpu.id, P.completed)

    async def passed():
        return len((await h.migration(warm.id)).sync_passes) > passes

    await _eventually(passed)
    await h.orch.stop()


async def test_a_vm_ahead_of_a_held_place_keeps_its_turn(tmp_path, store):
    """SDD §22.7 (G6 final review): a held place holds back only the VMs behind it: with
    max_concurrent_migrations of 1, a warm VM ahead of the GPU VM in the wave order starts its
    pre-copy in that one step while the GPU VM's read is out."""
    settings = make_settings(tmp_path, max_concurrent_migrations=1)
    precopies: list[str] = []

    async def precopy(ctx):
        precopies.append(ctx.migration.vm.source_id)
        return await executor.sim.run(StepName.PRECOPY, ctx)

    executor = ScriptedExecutor(settings, hooks={StepName.PRECOPY: precopy})
    plan_kw = {"default_strategy": Strategy.cold, "strategy_overrides": {"vm-1": Strategy.warm}}
    h, plan, dst = await _gate_plan(
        tmp_path, store, [vm(1), gate_vm(2)], plan_kw, executor=executor, settings=settings
    )
    dst.free = dict(GATE_ROOM)
    dst.hold = asyncio.Event()  # the GPU VM's read is out
    await h.orch.start()

    async def ahead_copies_while_the_gpu_vm_reads():
        return precopies == ["vm-1"] and len(dst.reads) == 1

    try:
        await _eventually(ahead_copies_while_the_gpu_vm_reads)
    finally:
        dst.hold.set()
        await h.orch.pause_plan(plan.id, "alice")
        await h.orch.stop()


async def test_hung_gpu_reads_never_take_every_thread_the_store_needs(tmp_path, store):
    """SDD §22.7 (G6 final review): the gate's reads out at once are capped below asyncio's default
    thread pool, which the store's calls share. Three destinations whose reads hang, each read
    holding a worker thread as a provider call does, take GPU_READS_MAX of the 8 threads; the
    store keeps answering and the tick goes on."""
    import contextlib
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from seamless_migrate.domain.models import Mappings
    from seamless_migrate.orchestrator import GPU_READS_MAX

    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=8))
    stuck = threading.Event()

    class ThreadHungDestination(GateDestination):
        async def gpu_capacity(self, requests):
            self.reads.append(list(requests))
            await asyncio.to_thread(stuck.wait, 30)
            return self.counted(requests)

    ids = [DST_PROVIDER.id, "dst-b", "dst-c"]
    dsts = {i: ThreadHungDestination(free={GATE_POOL: 3, "VGPU": 3}) for i in ids}
    vms = [gate_vm(i) for i in range(1, 10)]
    h = build_harness(tmp_path, store, vms, destination=dsts[ids[0]])
    h.orch.providers = GateDestinations(h.source, dsts)
    for i in ids[1:]:
        store.put("provider", DST_PROVIDER.model_copy(update={"id": i, "name": f"RHOSO {i}"}))
    kw = {"default_strategy": Strategy.cold, "mappings": Mappings(flavors={"g1.vgpu": GATE_FLAVOR})}
    plans = [
        plan_for(vms[3 * n : 3 * n + 3], name=f"P{n}", destination_provider_id=ids[n], **kw)
        for n in range(3)
    ]
    for plan in plans:
        store.put("plan", plan)
        await h.orch.validate_plan(plan.id, "alice")
        await h.orch.start_plan(plan.id, "alice")
    last = (await h.by_vm(plans[2].id, "vm-9")).id
    await h.orch.start()
    try:

        async def capped():
            return sum(len(d.reads) for d in dsts.values()) == GPU_READS_MAX

        await _eventually(capped)
        ticks = h.orch.tick_stats["count"]
        await asyncio.sleep(0.3)  # many ticks
        assert sum(len(d.reads) for d in dsts.values()) == GPU_READS_MAX
        await asyncio.wait_for(h.migration(last), 2.0)  # the store answers
        assert h.orch.tick_stats["count"] > ticks
    finally:
        stuck.set()
        for plan in plans:
            with contextlib.suppress(NotAllowed):
                await h.orch.pause_plan(plan.id, "alice")
        await h.orch.stop()


async def test_step_timeout_fails_the_attempt_and_rolls_back_after_a_stop(tmp_path, store):
    """A hung cutover (after the source stopped) is cut short by SEAMLESS_STEP_TIMEOUT_S and
    handled like any permanent failure: automatic rollback (SDD §15.1, §7.2)."""
    settings = make_settings(tmp_path, step_timeout_s=0.2)
    cancelled = asyncio.Event()

    async def hung_cutover(ctx):
        await ctx.mark_downtime_start()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return StepResult()

    executor = ScriptedExecutor(settings, hooks={StepName.CUTOVER: hung_cutover})
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.cold},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.orch.stop()
    assert cancelled.is_set(), "the hung step task was cancelled"
    assert "exceeded the step timeout of 0.2 s" in m.error
    assert h.history(m)[-4:] == [P.cutover, P.failed, P.rolling_back, P.rolled_back]


async def test_concurrent_checks_of_one_provider_do_not_conflict(tmp_path, store):
    """SDD §8: checks of one provider run one after the other, instead of colliding on the
    provider's versioned write (ConflictError, a 409 for a double-clicked Check)."""
    h, _ = await setup(tmp_path, store, [vm(1)])
    results = await asyncio.gather(
        h.orch.check_provider(SRC_PROVIDER.id, "alice"),
        h.orch.check_provider(SRC_PROVIDER.id, "bob"),
        return_exceptions=True,
    )
    assert [type(r).__name__ for r in results] == ["Provider", "Provider"], results


async def test_a_validation_and_a_manual_check_of_its_provider_both_succeed(tmp_path, store):
    h, plan = await setup(tmp_path, store, [vm(1)])
    results = await asyncio.gather(
        h.orch.validate_plan(plan.id, "alice"),
        h.orch.check_provider(SRC_PROVIDER.id, "bob"),
        return_exceptions=True,
    )
    assert [type(r).__name__ for r in results] == ["ValidationReport", "Provider"], results


async def test_check_provider_does_not_resurrect_a_deleted_provider(tmp_path, store):
    """A provider deleted while its check ran is not re-inserted by the check's write."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    source_id = plan.source_provider_id

    class Racing:
        async def check(self):
            await h.orch.db.delete("provider", source_id)
            return {"admin": True}

    h.orch.providers = SimpleNamespace(get=lambda p: Racing())
    with pytest.raises((ConflictError, NotFound)):
        await h.orch.check_provider(source_id, "alice")
    with pytest.raises(NotFound):
        await h.orch.db.get("provider", source_id, Provider)


async def test_cancel_during_a_pass_rolls_the_data_path_back(tmp_path, store):
    """A cancel in precopy/syncing kills the pass and then runs the rollback step so the
    snapshots, temporary and destination volumes of the abandoned pass are removed."""
    settings = make_settings(tmp_path)
    started = asyncio.Event()
    seen_options: list[dict] = []

    async def blocking_pass(ctx):
        started.set()
        await asyncio.sleep(60)
        return StepResult()

    async def rollback(ctx):
        seen_options.append(dict(ctx.options))
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(
        settings, hooks={StepName.PRECOPY: blocking_pass, StepName.ROLLBACK: rollback}
    )
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    m = await h.by_vm(plan.id, "vm-1")
    await asyncio.wait_for(started.wait(), 5)
    cancelled = await h.orch.cancel(m.id, "rina", "window closed")
    assert cancelled.phase == P.cancelled
    for _ in range(300):
        if (m.id, StepName.ROLLBACK) in executor.calls:
            break
        await asyncio.sleep(0.01)
    assert (m.id, StepName.ROLLBACK) in executor.calls
    assert seen_options == [{"delete_dest_volumes": True}]
    for _ in range(300):
        if m.id not in h.orch._cleanups:
            break
        await asyncio.sleep(0.01)
    actions = [
        e.data.get("action")
        for e in h.store.events(since_seq=0, limit=1000)
        if e.kind == "migration.action" and e.migration_id == m.id
    ]
    assert actions == ["cancel", "cleanup"]
    assert (await h.migration(m.id)).phase == P.cancelled
    await h.orch.stop()


def _backoff_harness(tmp_path, store):
    """A warm migration whose pre-copy attempts all fail transiently; every attempt records the
    phase it started in, and the rollback step records its options."""
    settings = make_settings(tmp_path)
    started_in: list[Phase] = []
    rollbacks: list[dict] = []

    async def failing_pass(ctx):
        started_in.append(store.get("migration", ctx.migration.id, Migration).phase)
        raise TransientStepError("HTTP 503 from cinder")

    async def rollback(ctx):
        rollbacks.append(dict(ctx.options))
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(
        settings, hooks={StepName.PRECOPY: failing_pass, StepName.ROLLBACK: rollback}
    )
    return settings, executor, started_in, rollbacks


def _hold_after_failure(h, at: str) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the driver after a transient failure — while it reports the failure ("report") or
    during the backoff ("backoff") — until the test sets the returned release event."""
    held, release = asyncio.Event(), asyncio.Event()
    if at == "backoff":

        async def held_sleep(seconds: float) -> None:
            held.set()
            await release.wait()

        h.orch._sleep = held_sleep
    else:
        emit = h.orch._emit

        async def held_emit(kind, message, **kw):
            if kind == "migration.log" and "transient failure" in message and not release.is_set():
                held.set()
                await release.wait()
            return await emit(kind, message, **kw)

        h.orch._emit = held_emit
    return held, release


async def _settled(h, mid: str) -> list[str]:
    for _ in range(300):
        if mid not in h.orch._cleanups and mid not in h.orch._drivers:
            break
        await asyncio.sleep(0.01)
    return [
        e.data.get("action")
        for e in h.store.events(since_seq=0, limit=1000)
        if e.kind == "migration.action" and e.migration_id == mid
    ]


@pytest.mark.parametrize("held_at", ["report", "backoff"])
async def test_cancel_after_a_transient_failure_cleans_up_and_starts_no_further_attempt(
    tmp_path, store, held_at
):
    """SDD §5.1/§8: from a transient failure of a pre-copy attempt until the next attempt starts —
    while the failure is reported and during the backoff, when no step task runs — a cancel ends
    the retries and runs the rollback step once with delete_dest_volumes, like a cancel of a
    running pass: the failed attempt may have left snapshots and volumes behind."""
    settings, executor, started_in, rollbacks = _backoff_harness(tmp_path, store)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    held, release = _hold_after_failure(h, held_at)
    await run_plan(h, plan)
    m = await h.by_vm(plan.id, "vm-1")
    await asyncio.wait_for(held.wait(), 5)
    assert m.id not in h.orch._steps, "between attempts no step task runs"

    cancelled = await h.orch.cancel(m.id, "rina", "window closed")
    assert cancelled.phase == P.cancelled
    for _ in range(300):
        if rollbacks:
            break
        await asyncio.sleep(0.01)
    assert rollbacks == [{"delete_dest_volumes": True}], "the failed attempt is cleaned up"
    release.set()
    assert await _settled(h, m.id) == ["cancel", "cleanup"]
    assert started_in == [P.precopy], "no attempt after the cancel"
    assert (await h.migration(m.id)).phase == P.cancelled
    await h.orch.stop()


async def test_an_attempt_never_starts_for_a_migration_cancelled_after_its_backoff(tmp_path, store):
    """SDD §8: the next attempt starts under the migration's lock and only while the migration is
    still in its phase. A cancel that lands after the backoff, while the driver reads the phase,
    either finds the attempt running (and kills and cleans it up) or ends the retries: an attempt
    never starts for a cancelled migration, and the last attempt is always cleaned up."""
    settings, executor, started_in, rollbacks = _backoff_harness(tmp_path, store)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    in_backoff, release = _hold_after_failure(h, "backoff")
    load = h.orch._load
    armed, paused, resume = False, asyncio.Event(), asyncio.Event()

    async def pausing_load(mid):
        # the driver's first read after the backoff waits until the test let the cancel run
        nonlocal armed
        loaded = await load(mid)
        task = asyncio.current_task()
        if armed and task is not None and task.get_name() == f"migration:{mid}":
            armed = False
            paused.set()
            await resume.wait()
        return loaded

    h.orch._load = pausing_load
    await run_plan(h, plan)
    m = await h.by_vm(plan.id, "vm-1")
    await asyncio.wait_for(in_backoff.wait(), 5)
    armed = True
    release.set()
    await asyncio.wait_for(paused.wait(), 5)
    cancel = asyncio.create_task(h.orch.cancel(m.id, "rina", "window closed"))
    await asyncio.sleep(0.05)  # as far as the cancel gets while the driver reads the phase
    resume.set()
    assert (await cancel).phase == P.cancelled

    actions = await _settled(h, m.id)
    assert P.cancelled not in started_in, "an attempt started for a cancelled migration"
    assert actions == ["cancel", "cleanup"]
    assert rollbacks == [{"delete_dest_volumes": True}]
    assert (await h.migration(m.id)).phase == P.cancelled
    await h.orch.stop()


@pytest.mark.parametrize("where", ["awaiting_cutover", "failed"])
async def test_cancel_with_recorded_passes_cleans_up_the_warm_data_path(tmp_path, store, where):
    """SDD §5.1: a warm migration whose passes left a data path — converged and waiting for its
    cutover, or failed after a pass — keeps destination volumes and source snapshots that no later
    action could remove (cancelled is terminal): its cancel runs the rollback step once with
    delete_dest_volumes, though no step runs."""
    settings = make_settings(tmp_path)
    rollbacks: list[dict] = []

    async def rollback(ctx):
        rollbacks.append(dict(ctx.options))
        return StepResult(details={"source_running": True})

    async def quota(ctx):
        raise PermanentStepError("snapshot quota exceeded")

    hooks = {StepName.ROLLBACK: rollback}
    if where == "failed":
        # a first pass that does not converge, then a delta pass that fails for good
        hooks[StepName.PRECOPY] = scripted_pass(recent_base(), 0, 100.0, 10 * 2**30, "full")
        hooks[StepName.SYNC] = quota
    executor = ScriptedExecutor(settings, hooks=hooks)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm, "downtime_slo_s": 300, "require_approval": True},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P(where))
    assert m.sync_passes and m.downtime_started_at is None
    assert m.id not in h.orch._steps, "no step runs"

    cancelled = await h.orch.cancel(m.id, "rina", "descoped")
    assert cancelled.phase == P.cancelled
    assert await _settled(h, m.id) == ["cancel", "cleanup"]
    assert rollbacks == [{"delete_dest_volumes": True}]
    await h.orch.stop()


async def test_a_rolled_back_migration_is_cancelled_and_its_kept_data_path_cleaned_up(
    tmp_path, store
):
    """SDD §5.1 (RR-03): a rollback deletes the destination server but keeps the warm data path for
    a retry - it passes no delete_dest_volumes. Cancelling the rolled-back migration ends it for
    good, so the cancel runs the rollback step once more, with delete_dest_volumes."""
    settings = make_settings(tmp_path)
    rollbacks: list[dict] = []

    async def rollback(ctx):
        rollbacks.append(dict(ctx.options))
        return StepResult(details={"source_running": True})

    async def quota(ctx):
        raise PermanentStepError("snapshot quota exceeded")

    hooks = {
        StepName.ROLLBACK: rollback,
        # a first pass that does not converge, then a delta pass that fails for good
        StepName.PRECOPY: scripted_pass(recent_base(), 0, 100.0, 10 * 2**30, "full"),
        StepName.SYNC: quota,
    }
    executor = ScriptedExecutor(settings, hooks=hooks)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm, "downtime_slo_s": 300, "require_approval": True},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.failed)
    await h.orch.rollback(m.id, "rina", "the snapshot quota is not ours to raise")
    m = await h.wait_phase(m.id, P.rolled_back)
    assert m.sync_passes and rollbacks == [{}], "the rollback kept the data path for a retry"

    cancelled = await h.orch.cancel(m.id, "rina", "this VM is not migrated after all")
    assert cancelled.phase == P.cancelled
    assert (await _settled(h, m.id))[-2:] == ["cancel", "cleanup"]
    assert rollbacks == [{}, {"delete_dest_volumes": True}]
    await h.orch.stop()


async def test_a_rolled_back_migration_without_passes_is_cancelled_and_then_its_plan(
    tmp_path, store
):
    """SDD §5.1, §12 (RR-03): a rolled-back migration that recorded no pass - as a storage
    handover, which copies nothing - leaves no data path, so its cancel cleans nothing up; and
    with it cancelled, Cancel plan, which refuses while it is rolled back and says to cancel it
    on its own first, retires the plan."""
    settings = make_settings(tmp_path)
    rollbacks: list[dict] = []

    async def rollback(ctx):
        rollbacks.append(dict(ctx.options))
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(settings, hooks={StepName.ROLLBACK: rollback})
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold},
        executor=executor, settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.rollback(m.id, "rina", "the application owner wants it back")
    m = await h.wait_phase(m.id, P.rolled_back)
    await h.wait_plan(plan.id, PlanStatus.completed)
    assert rollbacks == [{}]
    m.sync_passes = []  # nothing copied
    store.put("migration", m)
    with pytest.raises(NotAllowed, match="on its own first"):
        await h.orch.cancel_plan(plan.id, "rina", "retired")

    assert (await h.orch.cancel(m.id, "rina", "not migrated after all")).phase == P.cancelled
    assert "cleanup" not in await _settled(h, m.id)
    assert rollbacks == [{}]
    assert (await h.orch.cancel_plan(plan.id, "rina", "retired")).status == PlanStatus.cancelled
    await h.orch.stop()


async def test_a_rolled_back_migrations_cancel_cleanup_touches_only_its_own_data_path(
    tmp_path, store
):
    """SDD §5.1 (review RV-01, RV-02): a warm rollback deleted the server, started the source and
    kept the copied data; the cleanup when the migration is then cancelled matches nothing by
    name, deletes no port and leaves the source as it is - the VM the rollback released may since
    be another plan's, with a same-named server, ports keyed to the VM and a source its cutover
    stopped."""
    from seamless_migrate.executors.ansible import build_vars

    settings = make_settings(tmp_path)
    seen: list[dict] = []

    async def rollback(ctx):
        seen.append({"options": dict(ctx.options), "vars": build_vars(StepName.ROLLBACK, ctx)})
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(settings, hooks={StepName.ROLLBACK: rollback})
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm},
        executor=executor, settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.rollback(m.id, "rina", "the application owner wants it back")
    m = await h.wait_phase(m.id, P.rolled_back)
    assert m.sync_passes and m.downtime_started_at is not None
    assert "os_migrate_rollback_start_source" not in seen[0]["vars"]  # it starts the source

    await h.orch.cancel(m.id, "rina", "this VM is not migrated after all")
    assert (await _settled(h, m.id))[-2:] == ["cancel", "cleanup"]
    await h.orch.stop()
    cleanup = seen[1]
    assert cleanup["options"] == {"delete_dest_volumes": True}
    assert cleanup["vars"]["os_migrate_rollback_delete_dest_volumes"] is True
    assert "os_migrate_rollback_match_by_name" not in cleanup["vars"]
    assert cleanup["vars"]["os_migrate_rollback_start_source"] is False
    assert cleanup["vars"]["os_migrate_rollback_delete_ports"] is False


@pytest.mark.parametrize("read", ["kept", "none", "unread", "unsure"])
async def test_a_cancels_cleanup_names_the_kit_volumes_it_keeps(tmp_path, store, read):
    """SDD §5.1 (review RV42B, lead NP-2): a VMware cancel's cleanup keeps the kit's copies of
    the VM - another plan's migration of it takes them as its own - and its migration.action names
    each, the ids in data.kept_volumes, for the operator to delete once no plan migrates the VM; a
    read of them that failed is named, and the cleanup stands. The volumes held by a server it
    could not tell - the conversion host's lookup failed - are named with the error, to be checked
    before any is deleted, beside the ones it keeps (review RV45B-02)."""
    details = {
        "kept": {"kept_volumes": [
            {"id": "vol-a", "name": "web-01-2000"}, {"id": "vol-b", "name": "web-01-2001"},
            {"id": "vol-c", "name": "web-01-2002", "on_conversion_host": True},
        ]},
        "none": {"kept_volumes": []},
        "unread": {"kept_volumes": None, "kept_volumes_unread": "volumes: HTTP 503 " + "x" * 400},
        "unsure": {
            "kept_volumes": [{"id": "vol-a", "name": "web-01-2000"}],
            "unsure_volumes": [{"id": "vol-c", "name": "web-01-2002"}],
            "unsure_why": "servers: HTTP 503 " + "y" * 400,
        },
    }[read]  # fmt: skip

    async def rollback(ctx):
        if ctx.options.get("delete_dest_volumes"):
            return StepResult(details={"deleted_server": None, "deleted_ports": [], **details})
        return StepResult(details={"source_running": True})

    settings = make_settings(tmp_path)
    executor = ScriptedExecutor(settings, hooks={StepName.ROLLBACK: rollback})
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm},
        executor=executor, settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.rollback(m.id, "rina", "the application owner wants it back")
    m = await h.wait_phase(m.id, P.rolled_back)
    await h.orch.cancel(m.id, "rina", "this VM is not migrated after all")
    assert (await _settled(h, m.id))[-2:] == ["cancel", "cleanup"]
    await h.orch.stop()
    [event] = [
        e
        for e in h.store.events(since_seq=0, limit=1000)
        if e.kind == "migration.action" and e.data.get("action") == "cleanup"
    ]
    assert store.get("migration", m.id, Migration).checkpoint == "cleaned"
    head = f"{m.vm.name}: temporary resources of the cancelled migration removed"
    if read == "none":
        assert event.message == head
        assert "kept_volumes" not in event.data
    elif read == "kept":
        assert event.message.startswith(head + "; ")
        assert (
            "vol-a (web-01-2000), vol-b (web-01-2001), "
            "vol-c (web-01-2002, attached to the conversion host)"
        ) in event.message  # the kit's copy a cut-off pass left there (RV44B-01)
        assert "delete them once no plan migrates the VM" in event.message
        assert event.data["kept_volumes"] == ["vol-a", "vol-b", "vol-c"]
    elif read == "unread":
        assert event.message.startswith(head + "; ")
        # cut as the cleanup's failure is: 300 characters of the provider's answer
        assert "(volumes: HTTP 503 " + "x" * 282 + ")" in event.message
        assert "delete them once no plan migrates the VM" in event.message
        assert "kept_volumes" not in event.data
    else:
        # the ones it keeps as ever, and the ones of a holder it could not tell after them: never
        # among the volumes to delete - each may be another server's disk (RV45B-02)
        assert event.message.startswith(head + "; ")
        assert "vol-a (web-01-2000)" in event.message
        assert event.data["kept_volumes"] == ["vol-a"]
        assert (
            "it could not tell which server holds vol-c (web-01-2002) (servers: HTTP 503 "
            + "y" * 282
            + "): each may be the copy a cut-off pass left on the conversion host or another "
            "server's disk - check which server holds it before deleting it"
        ) in event.message


def _write_record(settings, plan_id, m, **record):
    """The workload's record (SDD §6.3), as the collection keeps it in the migration's run dir."""
    from seamless_migrate.executors.ansible import run_dir

    path = run_dir(settings, plan_id, m.id) / "osm" / "workload_warm" / f"{m.vm.source_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "server_id": m.vm.source_id,
                "dest_volumes": {},
                "passes": [],
                "pending_snapshot": None,
                "destination_server_id": None,
                **record,
            }
        )
    )
    return path


async def test_a_cancel_after_a_failed_first_pass_removes_what_its_record_holds(tmp_path, store):
    """SDD §5.1 (review RV3-05): a first warm pass that created its destination volumes, recorded
    them and then failed for good (a volume quota) recorded no pass - its record still holds them,
    so the cancel runs the cleanup that removes them."""
    settings = make_settings(tmp_path)
    rollbacks: list[dict] = []

    async def rollback(ctx):
        rollbacks.append(dict(ctx.options))
        return StepResult(details={"source_running": True})

    async def first_pass(ctx):
        _write_record(
            settings,
            ctx.plan.id,
            ctx.migration,
            dest_volumes={"/dev/vda": {"dest_id": "dvol-1", "size": 20}},
        )
        raise PermanentStepError(
            "import_workloads_precopy.yml failed (exit 2): VolumeLimitExceeded: Maximum number of "
            "volumes allowed (10) exceeded"
        )

    executor = ScriptedExecutor(
        settings, hooks={StepName.PRECOPY: first_pass, StepName.ROLLBACK: rollback}
    )
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm},
        executor=executor, settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.failed)
    assert not m.sync_passes and m.downtime_started_at is None

    await h.orch.cancel(m.id, "rina", "the quota is not ours to raise")
    assert (await _settled(h, m.id))[-2:] == ["cancel", "cleanup"]
    await h.orch.stop()
    assert rollbacks == [{"delete_dest_volumes": True}]


@pytest.mark.parametrize("restarted", [False, True])
async def test_a_cancels_cleanup_runs_the_step_of_the_attempt_whose_data_remains(
    tmp_path, store, restarted
):
    """SDD §5.1 (review RV43B-04): a warm attempt failed with a pending source snapshot in its
    record - no destination volume, so after the Retry the storage handover stays open and
    PUT …/strategy takes it. The cancel's cleanup is still the warm attempt's rollback step, run
    as warm: the handover's step knows nothing of that snapshot and would report 'nothing to roll
    back'. A cleanup the orchestrator runs again at its start, the cancel cut short, does the
    same."""
    from seamless_migrate.executors.ansible import run_dir
    from seamless_migrate.executors.router import ExecutorRouter

    handover = {"enabled": True, "backend_map": {"ceph-ssd": "rhoso@ceph#rbd"}}
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"handover": handover, "auto_cutover": False, "default_strategy": Strategy.warm},
    )
    calls: list[tuple[str, str, dict]] = []

    class Recorder:
        def __init__(self, name):
            self.name = name

        def supports(self, strategy):
            return True

        async def run(self, step, ctx):
            calls.append((self.name, str(ctx.migration.strategy), dict(ctx.options)))
            return StepResult(details={"source_running": True})

    try:
        await h.orch.validate_plan(plan.id, "alice")
        m = await h.by_vm(plan.id, "vm-1")
        m.phase = P.failed
        m.phase_history = [*m.phase_history, *_history(P.precopy, P.failed)]
        store.put("migration", m)
        path = run_dir(h.settings, plan.id, m.id) / "osm" / "workload_warm" / "vm-1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "server_id": "vm-1",
                    "dest_volumes": {},
                    "passes": [],
                    "pending_snapshot": {"transfer_uuid": "t-1", "volume_map": {}},
                    "destination_server_id": None,
                }
            )
        )
        paused = await h.orch._plan(plan.id)
        paused.status = PlanStatus.paused
        store.put("plan", paused)
        await h.orch.retry(m.id, "alice")
        await h.orch.set_strategy(m.id, Strategy.storage_handover, "alice")
        h.orch.executors = ExecutorRouter(
            h.settings, ansible=Recorder("ansible"), handover=Recorder("storage_handover")
        )
        if restarted:
            # the cancel saved its mark, then the control plane stopped before the cleanup ran
            h.orch._stopping = True
            await h.orch.cancel(m.id, "rina", "not migrated after all")
            await h.orch.stop()
            assert calls == []
            await h.orch.start()
        else:
            await h.orch.cancel(m.id, "rina", "not migrated after all")
        assert (await _settled(h, m.id))[-1] == "cleanup"
    finally:
        await h.orch.stop()
    assert calls == [("ansible", "warm", {"delete_dest_volumes": True})]
    left = store.get("migration", m.id, Migration)
    assert left.strategy == Strategy.storage_handover  # the stored choice is untouched
    assert left.checkpoint == "cleaned"


async def test_a_rolled_back_cold_migrations_cancel_runs_no_cleanup(tmp_path, store):
    """SDD §5.1 (review RV3-07): a cold cutover lists a full pass, but the cold rollback deleted
    what the cold import recorded - its record holds nothing, so a cancel has no data path and
    runs no cleanup (with no record at all, a cold migration's passes do not count either)."""
    settings = make_settings(tmp_path)
    rollbacks: list[dict] = []

    async def rollback(ctx):
        rollbacks.append(dict(ctx.options))
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(settings, hooks={StepName.ROLLBACK: rollback})
    h, plan = await setup(
        tmp_path, store, [vm(1), vm(2)], {"default_strategy": Strategy.cold},
        executor=executor, settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    rolled = []
    for name in ("vm-1", "vm-2"):
        m = await h.wait_phase((await h.by_vm(plan.id, name)).id, P.completed)
        await h.orch.rollback(m.id, "rina", "the application owner wants it back")
        rolled.append(await h.wait_phase(m.id, P.rolled_back))
    assert all(m.sync_passes for m in rolled)
    _write_record(settings, plan.id, rolled[0])  # emptied by the cold rollback

    for m in rolled:
        await h.orch.cancel(m.id, "rina", "this VM is not migrated after all")
        assert (await _settled(h, m.id))[-1] == "cancel"
    await h.orch.stop()
    assert len(rollbacks) == 2  # the two rollbacks, no cleanup


async def test_a_cleanup_stays_tracked_until_its_outcome_is_recorded(tmp_path, store):
    """The cleanup after a cancel leaves _cleanups only once it has finished, its outcome event
    included, however slowly the event store answers: whoever waits for it (stop() cancels what it
    tracks) never sees it gone while the outcome is still unrecorded."""
    settings = make_settings(tmp_path)

    async def rollback(ctx):
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(settings, hooks={StepName.ROLLBACK: rollback})
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm, "require_approval": True},
        executor=executor,
        settings=settings,
    )
    emit = h.orch._emit

    async def slow_outcome(kind, message, **kw):
        if kind == "migration.action" and (kw.get("data") or {}).get("action") == "cleanup":
            await asyncio.sleep(0.05)  # several polls of _settled
        return await emit(kind, message, **kw)

    h.orch._emit = slow_outcome
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.awaiting_cutover)
    await h.orch.cancel(m.id, "rina", "descoped")
    assert await _settled(h, m.id) == ["cancel", "cleanup"]
    assert m.id not in h.orch._cleanups, "a finished cleanup is no longer tracked"
    await h.orch.stop()


async def test_a_shutdown_interrupts_a_cancels_cleanup_and_records_why(tmp_path, store):
    """SDD §5.1/§8: stop() cancels a cleanup that still waits for its killed step; the cleanup must
    not swallow that cancellation and run the rollback playbook during the shutdown. Its outcome is
    still recorded — a migration.error saying it runs again — and the cancel's checkpoint "cleanup"
    makes the next start run it."""
    settings = make_settings(tmp_path)
    started, dying = asyncio.Event(), asyncio.Event()
    rollbacks: list[dict] = []

    async def slow_dying_pass(ctx):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            dying.set()
            await asyncio.sleep(5)  # the executor kills the playbook and removes its files
            raise

    async def rollback(ctx):
        rollbacks.append(dict(ctx.options))
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(
        settings, hooks={StepName.PRECOPY: slow_dying_pass, StepName.ROLLBACK: rollback}
    )
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    m = await h.by_vm(plan.id, "vm-1")
    await asyncio.wait_for(started.wait(), 5)
    await h.orch.cancel(m.id, "rina", "window closed")
    await asyncio.wait_for(dying.wait(), 5)
    await asyncio.sleep(0.05)  # the cleanup task starts and waits for the dying step
    assert m.id in h.orch._cleanups, "the cleanup waits for the dying step"

    await asyncio.wait_for(h.orch.stop(), 2)
    assert rollbacks == [], "no rollback playbook runs during the shutdown"
    errors = [
        e
        for e in h.store.events(since_seq=0, limit=1000)
        if e.kind == "migration.error" and e.migration_id == m.id
    ]
    assert [e.data.get("step") for e in errors] == ["cleanup"]
    assert "interrupted" in errors[0].message and "runs again" in errors[0].message
    assert store.get("migration", m.id, Migration).checkpoint == "cleanup"

    again = build_harness(tmp_path, store, [vm(1)], executor=executor, settings=settings)
    await again.orch.start()
    for _ in range(300):
        if store.get("migration", m.id, Migration).checkpoint != "cleanup":
            break
        await asyncio.sleep(0.01)
    await again.orch.stop()
    assert rollbacks == [{"delete_dest_volumes": True}]
    assert store.get("migration", m.id, Migration).checkpoint == "cleaned"


async def test_a_cancels_cleanup_cut_short_by_a_crash_runs_when_the_orchestrator_starts(
    tmp_path, store
):
    """SDD §5.1: the cancel saves checkpoint "cleanup" with cancelled; a process that dies while
    the cleanup runs (no stop()) leaves the mark, and the next start runs the cleanup — the
    snapshots and volumes go, the outcome is recorded, and the checkpoint becomes "cleaned", so
    a later start does not run it again."""
    settings = make_settings(tmp_path)
    in_cleanup, forever = asyncio.Event(), asyncio.Event()

    async def long_pass(ctx):
        await forever.wait()

    async def hanging_cleanup(ctx):
        in_cleanup.set()
        await forever.wait()  # deleting volumes when the process dies

    executor = ScriptedExecutor(
        settings, hooks={StepName.PRECOPY: long_pass, StepName.ROLLBACK: hanging_cleanup}
    )
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await h.wait_phase(mid, P.precopy)
    await h.orch.cancel(mid, "rina", "wrong VM")
    await asyncio.wait_for(in_cleanup.wait(), 5)
    marked = store.get("migration", mid, Migration)
    assert (marked.phase, marked.checkpoint) == (P.cancelled, "cleanup")
    # the process dies here: its tasks never run again

    restarted = ScriptedExecutor(settings)
    again = build_harness(tmp_path, store, [vm(1)], executor=restarted, settings=settings)
    await again.orch.start()
    for _ in range(300):
        if store.get("migration", mid, Migration).checkpoint == "cleaned":
            break
        await asyncio.sleep(0.01)
    await again.orch.stop()
    assert [s for _, s in restarted.calls] == [StepName.ROLLBACK]
    assert store.get("migration", mid, Migration).checkpoint == "cleaned"
    outcome = [
        e
        for e in store.events(since_seq=0, limit=100000)
        if e.migration_id == mid and e.kind == "migration.action"
    ]
    assert [e.data.get("action") for e in outcome] == ["cancel", "cleanup"]

    once_more = ScriptedExecutor(settings)
    third = build_harness(tmp_path, store, [vm(1)], executor=once_more, settings=settings)
    await third.orch.start()
    await asyncio.sleep(0.2)
    await third.orch.stop()
    assert once_more.calls == []
    h.orch._stopping = True  # the dead process's tasks, after the assertions
    for task in [*h.orch._cleanups.values(), *h.orch._drivers.values(), *h.orch._steps.values()]:
        task.cancel()


async def test_a_cancels_cleanup_whose_outcome_was_not_recorded_runs_again(tmp_path, store):
    """SDD §5.1: the mark goes only once the cleanup's outcome is recorded — an outcome event that
    fails to be written (a database blip) leaves checkpoint "cleanup", so the next start runs the
    cleanup again and records it."""
    settings = make_settings(tmp_path)
    started = asyncio.Event()
    rollbacks: list[dict] = []

    async def long_pass(ctx):
        started.set()
        await asyncio.sleep(60)

    async def cleanup(ctx):
        rollbacks.append(dict(ctx.options))
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(
        settings, hooks={StepName.PRECOPY: long_pass, StepName.ROLLBACK: cleanup}
    )
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    real_emit = h.orch._emit

    async def blip_on_the_outcome(kind, message, **kw):
        if kind == "migration.action" and (kw.get("data") or {}).get("action") == "cleanup":
            raise OSError("server closed the connection unexpectedly")
        return await real_emit(kind, message, **kw)

    h.orch._emit = blip_on_the_outcome
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await asyncio.wait_for(started.wait(), 5)
    await h.orch.cancel(mid, "rina", "wrong VM")
    for _ in range(300):
        if rollbacks and mid not in h.orch._cleanups:
            break
        await asyncio.sleep(0.01)
    await h.orch.stop()
    assert store.get("migration", mid, Migration).checkpoint == "cleanup"

    again = build_harness(tmp_path, store, [vm(1)], executor=executor, settings=settings)
    await again.orch.start()
    for _ in range(300):
        if store.get("migration", mid, Migration).checkpoint == "cleaned":
            break
        await asyncio.sleep(0.01)
    await again.orch.stop()
    assert len(rollbacks) == 2 and store.get("migration", mid, Migration).checkpoint == "cleaned"


@pytest.mark.parametrize("outcome", ["cleanup_failed", "cleaned"])
@pytest.mark.parametrize("slow", ["before_the_write", "after_the_write"])
async def test_a_stop_while_the_cleanup_outcome_is_saved_waits_for_it(
    tmp_path, store, slow, outcome
):
    """SDD §5.1: a stop that comes after the cancel's cleanup ran waits for its outcome to be
    saved; the outcome stands and no migration.error says the cleanup was interrupted — also
    when the save had already written and only the task had not resumed yet."""
    settings = make_settings(tmp_path)
    started = asyncio.Event()

    async def long_pass(ctx):
        started.set()
        await asyncio.sleep(60)

    async def cleanup(ctx):
        if outcome == "cleanup_failed":
            raise PermanentStepError("cinder: volume is busy")
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(
        settings, hooks={StepName.PRECOPY: long_pass, StepName.ROLLBACK: cleanup}
    )
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    saving = asyncio.Event()
    real = h.orch._cleanup_recorded

    async def slow_save(mid: str, outcome: str) -> None:
        if slow == "before_the_write":
            saving.set()
            await asyncio.sleep(0.2)
        await real(mid, outcome)
        if slow == "after_the_write":
            # written, but the task has not resumed when the stop comes (a busy event loop)
            saving.set()
            await asyncio.sleep(0.2)

    h.orch._cleanup_recorded = slow_save
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await asyncio.wait_for(started.wait(), 5)
    await h.orch.cancel(mid, "rina", "wrong VM")
    await asyncio.wait_for(saving.wait(), 5)
    await h.orch.stop()
    assert store.get("migration", mid, Migration).checkpoint == outcome
    events = [e for e in store.events(since_seq=0, limit=100000) if e.migration_id == mid]
    errors = [e.message for e in events if e.kind == "migration.error"]
    if outcome == "cleanup_failed":
        assert len(errors) == 1
        assert "rollback_workloads.yml" in errors[0]
    else:
        assert errors == []
        assert [e.data.get("action") for e in events if e.kind == "migration.action"][
            -1
        ] == "cleanup"


async def test_a_cancels_cleanup_that_failed_is_not_run_again(tmp_path, store):
    """SDD §5.1: a cleanup that fails records a migration.error naming the manual
    rollback_workloads.yml run and sets checkpoint "cleanup_failed": the next start does not
    retry it."""
    settings = make_settings(tmp_path)
    started = asyncio.Event()

    async def long_pass(ctx):
        started.set()
        await asyncio.sleep(60)

    async def failing_cleanup(ctx):
        raise PermanentStepError("cinder: volume is busy")

    executor = ScriptedExecutor(
        settings, hooks={StepName.PRECOPY: long_pass, StepName.ROLLBACK: failing_cleanup}
    )
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await asyncio.wait_for(started.wait(), 5)
    await h.orch.cancel(mid, "rina", "wrong VM")
    for _ in range(300):
        if store.get("migration", mid, Migration).checkpoint != "cleanup":
            break
        await asyncio.sleep(0.01)
    await h.orch.stop()
    assert store.get("migration", mid, Migration).checkpoint == "cleanup_failed"
    [error] = [
        e
        for e in store.events(since_seq=0, limit=100000)
        if e.migration_id == mid and e.kind == "migration.error"
    ]
    assert "rollback_workloads.yml" in error.message

    again_executor = ScriptedExecutor(settings)
    again = build_harness(tmp_path, store, [vm(1)], executor=again_executor, settings=settings)
    await again.orch.start()
    await asyncio.sleep(0.2)
    await again.orch.stop()
    assert again_executor.calls == []


async def test_cancel_after_a_pass_finished_but_before_it_is_recorded_cleans_up(tmp_path, store):
    """SDD §5.1: a cancel from precopy cleans up even in the gap between a finished first pass and
    its recording - no step runs, no retry waits and no pass is recorded yet, but the pass's
    destination volumes and snapshots exist."""
    settings = make_settings(tmp_path)
    finished, cancelled = asyncio.Event(), asyncio.Event()
    rollbacks: list[dict] = []

    async def first_pass(ctx):
        result = await executor.sim.run(StepName.PRECOPY, ctx)
        finished.set()  # snapshots and destination volumes exist now
        return result

    async def rollback(ctx):
        rollbacks.append(dict(ctx.options))
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(
        settings, hooks={StepName.PRECOPY: first_pass, StepName.ROLLBACK: rollback}
    )
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    read_plan = h.orch._plan

    async def plan_then_cancel(plan_id):
        # the driver reads the plan before it records the finished pass: the operator cancels now
        if finished.is_set() and not cancelled.is_set():
            cancelled.set()
            m = await h.by_vm(plan.id, "vm-1")
            assert m.phase == P.precopy and not m.sync_passes and m.id not in h.orch._steps
            await h.orch.cancel(m.id, "rina", "window closed")
        return await read_plan(plan_id)

    h.orch._plan = plan_then_cancel
    await run_plan(h, plan)
    await asyncio.wait_for(cancelled.wait(), 5)
    m = await h.by_vm(plan.id, "vm-1")
    assert await _settled(h, m.id) == ["cancel", "cleanup"]
    assert rollbacks == [{"delete_dest_volumes": True}]
    assert (await h.migration(m.id)).phase == P.cancelled
    await h.orch.stop()


async def test_cancel_without_a_data_path_runs_no_cleanup(tmp_path, store):
    """SDD §5.1: a warm migration that never ran a pass (validated, its plan not started) left
    nothing behind: its cancel runs no rollback step."""
    settings = make_settings(tmp_path)
    executor = ScriptedExecutor(settings)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    assert m.phase == P.ready and not m.sync_passes

    await h.orch.cancel(m.id, "rina", "descoped")
    assert await _settled(h, m.id) == ["cancel"]
    assert executor.calls == []


def _strategy_changed_before(h, to: Phase, strategy: Strategy) -> asyncio.Event:
    """Run set_strategy (under the migration's lock, as the API does) right after the tick chose the
    migration for ``to`` on its snapshot and before it starts that step."""
    begin = h.orch._begin
    changed = asyncio.Event()

    async def racing_begin(mid, allowed, target, reason):
        if target == to and not changed.is_set():
            changed.set()
            await h.orch.set_strategy(mid, strategy, "rina")
        return await begin(mid, allowed, target, reason)

    h.orch._begin = racing_begin
    return changed


@pytest.mark.parametrize(
    ("strategy", "step"), [(Strategy.cold, P.cutover), (Strategy.warm, P.precopy)]
)
async def test_a_pause_stops_the_step_the_tick_already_chose(tmp_path, store, strategy, step):
    """SDD §8: the tick chose the step on its plan snapshot; a pause that returned before the step
    starts wins - starting a step re-checks, under the plan's lock, that the plan still runs - so
    the source is not stopped (cutover) or copied (pre-copy) after the operator paused the plan."""
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": strategy})
    begin = h.orch._begin
    paused = asyncio.Event()

    async def racing_begin(mid, allowed, target, reason):
        if target == step and not paused.is_set():
            await h.orch.pause_plan(plan.id, "rina")
            paused.set()
        return await begin(mid, allowed, target, reason)

    h.orch._begin = racing_begin
    await run_plan(h, plan)
    await asyncio.wait_for(paused.wait(), 5)
    await asyncio.sleep(0.1)  # many ticks of the paused plan
    m = await h.migration((await h.by_vm(plan.id, "vm-1")).id)
    await h.orch.stop()
    assert (await h.plan(plan.id)).status == PlanStatus.paused
    assert m.phase == P.ready and step not in h.history(m)
    assert h.executor.calls == []


async def test_a_pause_waits_for_a_step_that_is_starting(tmp_path, store):
    """SDD §8: a step starts under the plan's lock, so a pause that arrives after the start read the
    plan's status waits for it: when the pause returns, no step starts any more - the cutover's
    phase event comes before the plan.paused event, never after it."""
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    begin, read_plan = h.orch._begin, h.orch._plan
    starting: list[asyncio.Task] = []
    pause: list[asyncio.Task] = []

    async def tracked_begin(mid, allowed, target, reason):
        starting.append(asyncio.current_task())
        return await begin(mid, allowed, target, reason)

    async def racing_plan(plan_id):
        loaded = await read_plan(plan_id)
        if not pause and asyncio.current_task() in starting:
            # the start has read the plan's status: the operator pauses now
            pause.append(asyncio.create_task(h.orch.pause_plan(plan.id, "rina")))
            await asyncio.sleep(0.05)  # as far as the pause gets meanwhile
        return loaded

    h.orch._begin, h.orch._plan = tracked_begin, racing_plan
    await run_plan(h, plan)
    for _ in range(300):
        if pause and pause[0].done():
            break
        await asyncio.sleep(0.01)
    await pause[0]
    await asyncio.sleep(0.1)
    await h.orch.stop()
    events = h.store.events(since_seq=0, limit=10000)
    paused_at = next(e.seq for e in events if e.kind == "plan.paused")
    started_at = [
        e.seq for e in events if e.kind == "migration.phase" and e.data.get("to") == "cutover"
    ]
    assert started_at and all(seq < paused_at for seq in started_at)


async def test_a_migration_turned_cold_before_its_precopy_starts_does_not_precopy(tmp_path, store):
    """SDD §8: the tick chose a warm migration for pre-copy on its snapshot; a set_strategy to cold
    meanwhile wins: a pre-copy starts only for a warm strategy, so the cold migration cuts over
    without one instead of failing in a pre-copy its executor cannot run."""
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm})
    changed = _strategy_changed_before(h, P.precopy, Strategy.cold)
    await run_plan(h, plan)
    await asyncio.wait_for(changed.wait(), 5)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert m.strategy == Strategy.cold
    assert P.precopy not in h.history(m)
    assert [s for _, s in h.executor.calls] == [StepName.CUTOVER]


async def test_a_migration_turned_warm_before_its_cutover_starts_precopies_first(tmp_path, store):
    """SDD §8: the tick chose a single-shot migration for a cutover from ready on its snapshot; a
    set_strategy to warm meanwhile wins: a cutover from ready starts only for a single-shot
    strategy, so the warm migration pre-copies first instead of cutting over with no pass."""
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    changed = _strategy_changed_before(h, P.cutover, Strategy.warm)
    await run_plan(h, plan)
    await asyncio.wait_for(changed.wait(), 5)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert m.strategy == Strategy.warm
    history = h.history(m)
    assert P.precopy in history and history.index(P.precopy) < history.index(P.cutover)
    steps = [s for _, s in h.executor.calls]
    assert steps[0] == StepName.PRECOPY and StepName.CUTOVER in steps


async def test_wave_dependencies_respected(tmp_path, store):
    vms = [vm(1), vm(2)]
    waves = [
        Wave(id="wave-1", name="Pilot", order=1, vm_ids=["vm-1"]),
        Wave(id="wave-2", name="Wave 2", order=2, vm_ids=["vm-2"], depends_on=["wave-1"]),
    ]
    settings = make_settings(tmp_path)
    gates: dict[str, asyncio.Event] = {}
    started: list[str] = []
    executor = ScriptedExecutor(
        settings, hooks={StepName.CUTOVER: blocking_cutover(gates, started)}
    )
    h, plan = await setup(
        tmp_path,
        store,
        vms,
        {"default_strategy": Strategy.cold, "waves": waves},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    m1 = await h.by_vm(plan.id, "vm-1")
    m2 = await h.by_vm(plan.id, "vm-2")
    assert (m1.wave_id, m2.wave_id) == ("wave-1", "wave-2")
    await h.wait_phase(m1.id, P.cutover)
    await asyncio.sleep(0.1)
    assert (await h.migration(m2.id)).phase == P.ready, "wave 2 waits for wave 1"
    gates.setdefault("vm-1", asyncio.Event()).set()
    await h.wait_phase(m1.id, P.completed)
    await h.wait_phase(m2.id, P.cutover)
    gates.setdefault("vm-2", asyncio.Event()).set()
    await h.wait_phase(m2.id, P.completed)
    await h.wait_plan(plan.id, PlanStatus.completed)
    await h.orch.stop()
    assert started == ["vm-1", "vm-2"]
    kinds = h.kinds()
    assert kinds.count("wave.started") == 2 and kinds.count("wave.completed") == 2


async def test_max_concurrent_cutovers(tmp_path, store):
    vms = [vm(i) for i in range(1, 5)]
    settings = make_settings(tmp_path, max_concurrent_cutovers=2)
    gates: dict[str, asyncio.Event] = {}
    started: list[str] = []
    executor = ScriptedExecutor(
        settings, hooks={StepName.CUTOVER: blocking_cutover(gates, started)}
    )
    h, plan = await setup(
        tmp_path,
        store,
        vms,
        {"default_strategy": Strategy.cold},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)

    async def in_cutover():
        return [m for m in await h.migrations(plan.id) if m.phase == P.cutover]

    deadline = asyncio.get_running_loop().time() + 5
    while len(started) < 2:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)
    for _ in range(10):
        assert len(await in_cutover()) <= 2
        await asyncio.sleep(0.02)
    assert len(started) == 2
    for vid in list(started):
        gates[vid].set()
    while len(started) < 4:
        assert asyncio.get_running_loop().time() < deadline + 5
        assert len(await in_cutover()) <= 2
        await asyncio.sleep(0.01)
    for vid in started:
        gates.setdefault(vid, asyncio.Event()).set()
    for m in await h.migrations(plan.id):
        await h.wait_phase(m.id, P.completed)
    await h.orch.stop()


async def test_downtime_clock_falls_back_to_the_cutover_step_start(tmp_path, store):
    """SDD §7.2: an executor that never reports the stop (no stop task line) still gets a
    downtime window, bounded by the moment the cutover step began — exactly one event."""
    settings = make_settings(tmp_path)

    async def silent_cutover(ctx):
        return StepResult(destination_server_id=f"dst-{ctx.migration.vm.source_id}")

    executor = ScriptedExecutor(settings, hooks={StepName.CUTOVER: silent_cutover})
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.cold},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    from seamless_migrate.orchestrator import _phase_started

    assert m.downtime_started_at == _phase_started(m, P.cutover)
    assert m.downtime_ended_at is not None and m.actual_downtime_s is not None
    assert h.kinds().count("migration.downtime_started") == 1


def slow_clock_answer(store):
    """The store saves the migration's downtime clock, but its answer is slow to come back (a busy
    database): the returned events are set once the save landed, and the answer waits for the
    second."""
    import threading

    held, release = threading.Event(), threading.Event()

    def slowed(write):
        def slow_answer(kind, model, *args, **kw):
            answer = write(kind, model, *args, **kw)
            if kind == "migration" and model.downtime_started_at is not None and not held.is_set():
                held.set()
                release.wait(30)
            return answer

        return slow_answer

    store.put = slowed(store.put)
    store.put_with_event = slowed(store.put_with_event)  # the clock saved with its event
    return held, release


async def test_a_cancelled_clock_mark_ends_once_its_clock_and_its_event_are_stored(tmp_path, store):
    """SDD §4.3, §7.2: whichever executor opens the downtime clock, a cancel that lands while the
    clock's save runs - the step timeout, a rollback request, a shutdown - waits for that save and
    for its migration.downtime_started event: an open clock is never left unannounced (review
    RV26-06)."""
    h = build_harness(tmp_path, store, [vm(1)])
    m = make_migration(id="mig-00000000aa", plan_id="plan-00c0ffee", vm=vm(1), phase=P.cutover)
    store.put("migration", m)
    held, release = slow_clock_answer(store)
    mark = asyncio.create_task(h.orch._mark_downtime(m.id))
    try:
        assert await asyncio.to_thread(held.wait, 10), "the clock was never saved"
        mark.cancel()
        await asyncio.sleep(0.2)
        assert not mark.done(), "the cancel cut the mark off between the clock and its event"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await mark
    assert (await h.migration(m.id)).downtime_started_at is not None
    assert h.kinds().count("migration.downtime_started") == 1


async def test_a_downtime_event_the_store_refuses_still_leaves_the_clock_open(tmp_path, store):
    """SDD §5, §8: the clock is saved before its event - a store error writing
    migration.downtime_started fails the mark, but leaves the clock open, so the automatic
    rollback still restarts a source that may be stopped and a cancel is still refused (review
    RV26-06)."""
    h = build_harness(tmp_path, store, [vm(1)])
    m = make_migration(id="mig-00000000aa", plan_id="plan-00c0ffee", vm=vm(1), phase=P.cutover)
    store.put("migration", m)
    append, put_with_event = store.append_event, store.put_with_event

    def refused(event):
        if event.kind == "migration.downtime_started":
            raise RuntimeError("database is locked")

    def refusing(event):
        refused(event)
        return append(event)

    def refusing_with(kind, model, event, *args, **kw):
        refused(event)  # the event written with the clock in one transaction: neither lands
        return put_with_event(kind, model, event, *args, **kw)

    store.append_event, store.put_with_event = refusing, refusing_with
    with pytest.raises(RuntimeError, match="database is locked"):
        await h.orch._mark_downtime(m.id)
    assert (await h.migration(m.id)).downtime_started_at is not None
    assert "migration.downtime_started" not in h.kinds()


#: each half of the clock's close, refused in turn when it runs (reviews RV36B-03, RV37B-01): the
#: migration's UPDATE, or its migration.downtime_ended's INSERT
CLOSE_HALVES = {
    "clock": ("UPDATE documents", "migration"),
    "event": ("INSERT INTO events", "migration.downtime_ended"),
}


@pytest.mark.parametrize("half", list(CLOSE_HALVES))
async def test_a_store_error_closing_the_clock_inside_a_step_retries_the_attempt(
    tmp_path, store, refuse_write, half
):
    """SDD §5.2 (reviews RV36B-03, RV37B-01): a rollback closes the clock inside its step once the
    source runs; a store error on either half of that close - the clock's save, its event - fails
    the attempt as transient, never the rollback, and leaves neither: the retry closes the clock
    and announces it, once."""
    settings = make_settings(tmp_path)
    attempts: list[int] = []
    refused: list[list[str]] = []

    async def cutover(ctx):
        await ctx.mark_downtime_start()
        raise PermanentStepError("the destination refused the server")

    async def rollback(ctx):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            refused.append(refuse_write(store, *CLOSE_HALVES[half]))
        await ctx.mark_downtime_end()
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(
        settings, hooks={StepName.CUTOVER: cutover, StepName.ROLLBACK: rollback}
    )
    h, plan = await setup(tmp_path, store, [vm(1)], executor=executor, settings=settings)
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back, P.failed)
    await h.orch.stop()
    assert m.phase == P.rolled_back, (h.history(m), m.error)
    assert refused[0] == [" ".join(CLOSE_HALVES[half])] and attempts == [1, 2]
    assert m.downtime_ended_at is not None and m.actual_downtime_s is not None
    assert h.kinds().count("migration.downtime_started") == 1
    assert h.kinds().count("migration.downtime_ended") == 1, h.kinds()


@pytest.mark.parametrize("half", list(CLOSE_HALVES))
async def test_a_restop_whose_close_the_store_refused_announces_each_outage_end(
    tmp_path, store, refuse_write, half
):
    """SDD §5.2, §7.3 step 1 (reviews RV33-04, RV37B-01): a cutover attempt finds the source it
    stopped running again, closes that outage's clock and stops the source once more - a new
    outage. A store error on either half of the close retries the attempt, whose close then lands
    with its event: each outage reads its start and its end, never two starts in a row."""
    settings = make_settings(tmp_path)
    attempts: list[int] = []
    refused: list[list[str]] = []

    async def cutover(ctx):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            await ctx.mark_downtime_start()
            raise TransientStepError("Timeout waiting for the stop of the source server")
        if len(attempts) == 2:
            refused.append(refuse_write(store, *CLOSE_HALVES[half]))
        await ctx.mark_downtime_end()  # the source ran again: that outage ended
        await ctx.mark_downtime_start()  # and the re-stop starts a new one
        return StepResult(destination_server_id="dst-1")

    executor = ScriptedExecutor(settings, hooks={StepName.CUTOVER: cutover})
    h, plan = await setup(tmp_path, store, [vm(1)], executor=executor, settings=settings)
    await run_plan(h, plan)
    m = await h.wait_phase(
        (await h.by_vm(plan.id, "vm-1")).id, P.completed, P.failed, P.rolled_back
    )
    await h.orch.stop()
    assert m.phase == P.completed, (h.history(m), m.error)
    assert refused[0] == [" ".join(CLOSE_HALVES[half])] and attempts == [1, 2, 3]
    assert [k for k in h.kinds() if k.startswith("migration.downtime")] == [
        "migration.downtime_started",
        "migration.downtime_ended",
        "migration.downtime_started",
        "migration.downtime_ended",
    ]


@pytest.mark.parametrize("half", list(CLOSE_HALVES))
async def test_a_rollback_whose_closing_write_the_store_refused_resumes_and_announces_it(
    tmp_path, store, refuse_write, half
):
    """SDD §5.2, §8 (review RV37B-01): a rollback whose step leaves the clock to its end - the
    rollback's own close - and whose closing write the store refuses, on either half (the
    migration's save, its migration.downtime_ended), writes neither: its driver resumes it, and
    the close lands with its event - rolled_back, the outage announced once."""
    settings = make_settings(tmp_path)
    attempts: list[int] = []
    refused: list[list[str]] = []

    async def cutover(ctx):
        await ctx.mark_downtime_start()
        raise PermanentStepError("the destination refused the server")

    async def rollback(ctx):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:  # the next write of the migration is the rollback's close
            refused.append(refuse_write(store, *CLOSE_HALVES[half]))
        return StepResult(details={"source_running": True})

    executor = ScriptedExecutor(
        settings, hooks={StepName.CUTOVER: cutover, StepName.ROLLBACK: rollback}
    )
    h, plan = await setup(tmp_path, store, [vm(1)], executor=executor, settings=settings)
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back, P.failed)
    await h.orch.stop()
    assert m.phase == P.rolled_back, (h.history(m), m.error)
    assert refused[0] == [" ".join(CLOSE_HALVES[half])] and attempts == [1, 2]
    assert m.downtime_ended_at is not None and m.actual_downtime_s is not None
    assert h.kinds().count("migration.downtime_ended") == 1, h.kinds()


async def test_an_ansible_cutover_cut_off_while_its_clock_saves_still_announces_it_once(
    tmp_path, store, monkeypatch
):
    """SDD §4.3, §7.2, §15.1: the step timeout cuts a cold cutover's playbook off while the clock
    its stop task opened is being saved. The clock is announced once - migration.downtime_started
    in the trail - and the automatic rollback follows (review RV26-06)."""
    from seamless_migrate.config import find_repo_root
    from seamless_migrate.executors.ansible import AnsibleExecutor
    from tests.factories import make_plan
    from tests.fake_ansible import install
    from tests.test_executor_ansible import FakeRegistry

    script = install(tmp_path / "bin")
    monkeypatch.setenv("ANSIBLE_FAKE_LOG", str(tmp_path / "ansible.log"))
    monkeypatch.setenv("ANSIBLE_FAKE_SLEEP", "0.05")
    clouds = tmp_path / "clouds.yaml"
    clouds.write_text(
        "clouds:\n"
        "  src:\n    auth: {auth_url: 'https://src/v3', username: su, password: spw, "
        "project_name: finance}\n"
        "  dst:\n    auth: {auth_url: 'https://dst/v3', username: du, password: dpw, "
        "project_name: finance}\n"
    )
    settings = make_settings(
        tmp_path,
        ansible_playbook=str(script),
        clouds_yaml=clouds,
        collection_root=find_repo_root(),
        step_timeout_s=1.0,
        max_step_retries=0,
    )
    executor = AnsibleExecutor(settings, providers=FakeRegistry(), poll_s=0.02)
    source = make_vm(source_id="srv-1", name="web-01")
    h = build_harness(tmp_path, store, [source], executor=executor, settings=settings)
    plan = make_plan(
        id="plan-00c0ffee",
        vm_ids=[source.source_id],
        status=PlanStatus.running,
        require_approval=False,
        auto_cutover=True,
    )
    store.put("plan", plan)
    m = make_migration(
        id="mig-00000000aa", plan_id=plan.id, vm=source, strategy=Strategy.cold, phase=P.ready
    )
    store.put("migration", m)
    held, release = slow_clock_answer(store)
    await h.orch.start()
    try:
        assert await asyncio.to_thread(held.wait, 15), "the clock was never saved"
        await asyncio.sleep(1.5)  # the step timeout cuts the step off while the save answers
        release.set()
        final = await h.wait_phase(m.id, P.rolled_back, P.failed, timeout=30)
        assert final.downtime_started_at is not None and P.rolling_back in h.history(final)
        assert h.kinds().count("migration.downtime_started") == 1, h.kinds()
    finally:
        release.set()
        await h.orch.stop()


def _vmware_kit_journey(tmp_path, store, monkeypatch, registry_class=None):
    """A vmware_cold migration of web-01, ready, in a running plan, driven by the real
    AnsibleExecutor over the fake kit run (tests/fake_kit.py), whose first run fails at the kit's
    "Create network ports" after its migrate task converted the volume and powered the VM off."""
    from seamless_migrate.config import find_repo_root
    from seamless_migrate.executors.ansible import AnsibleExecutor
    from tests.factories import make_plan
    from tests.fake_kit import KitRegistry, install_kit
    from tests.test_executor_ansible import MAPPINGS, VCENTER, VMW_NICS

    state = tmp_path / "kit-state"
    state.mkdir()
    (state / "power").write_text("on")
    (state / "fail_ports_once").write_text("")
    script = install_kit(tmp_path / "bin")
    monkeypatch.setenv("ANSIBLE_FAKE_KIT_STATE", str(state))
    monkeypatch.setenv("ANSIBLE_FAKE_CREATED", str(tmp_path / "created.txt"))
    clouds = tmp_path / "clouds.yaml"
    clouds.write_text(
        "clouds:\n"
        "  dst:\n    auth: {auth_url: 'https://dst/v3', username: du, password: dpw, "
        "project_name: finance}\n"
    )
    secret = tmp_path / "secrets" / "vcenter-dc2"
    secret.mkdir(parents=True)
    for key, value in (
        ("username", "svc@vsphere.local"),
        ("password", "vpw"),
        ("datacenter", "DC2"),
    ):
        (secret / key).write_text(value)
    settings = make_settings(
        tmp_path,
        ansible_playbook=str(script),
        clouds_yaml=clouds,
        secrets_dir=tmp_path / "secrets",
        collection_root=find_repo_root(),
        max_step_retries=0,
    )
    registry = (registry_class or KitRegistry)(state, "web-01")
    executor = AnsibleExecutor(settings, providers=registry, poll_s=0.02)
    vm = make_vm(source_id="vm-1", name="web-01", nics=VMW_NICS)
    h = build_harness(tmp_path, store, [vm], executor=executor, settings=settings)
    store.put("provider", VCENTER)
    plan = make_plan(
        id="plan-00c0ffee",
        source_provider_id=VCENTER.id,
        vm_ids=["vm-1"],
        status=PlanStatus.running,
        require_approval=False,
        auto_cutover=True,
        mappings=MAPPINGS,
    )
    store.put("plan", plan)
    m = make_migration(
        id="mig-00000000aa", plan_id=plan.id, vm=vm, strategy=Strategy.vmware_cold, phase=P.ready
    )
    store.put("migration", m)
    return h, m, state


async def test_a_vmware_retry_after_the_automatic_rollback_copies_and_stops_the_source_again(
    tmp_path, store, monkeypatch
):
    """SDD §7.2, §8 (review RV28-01): a VMware cutover that fails after the kit's migrate task -
    its clock open - is rolled back automatically, and the rollback powers the VM on. The kit had
    converted the VM's volume; on the retry its migrate task would take that volume as migrated
    and boot the destination from it - no copy, no power-off, the source running beside it. The
    rollback deletes that volume, so the retry copies the VM again and powers it off."""
    h, m, state = _vmware_kit_journey(tmp_path, store, monkeypatch)
    await h.orch.start()
    try:
        first = await h.wait_phase(m.id, P.rolled_back, P.failed, timeout=30)
        assert first.phase == P.rolled_back and P.rolling_back in h.history(first), first.error
        assert (state / "power").read_text() == "on", "the rollback powers the VM on"
        assert not (state / "volume").exists(), "the rollback kept the kit's converted volume"
        await h.orch.retry(m.id, "rina")
        done = await h.wait_phase(m.id, P.completed, P.failed, P.rolled_back, timeout=30)
        assert done.phase == P.completed, (done.phase, done.error)
        assert (state / "power").read_text() == "off", "the cutover completed with the VM running"
    finally:
        await h.orch.stop()


async def test_a_vmware_rollback_cinder_will_not_clean_restarts_the_source_and_the_retry_waits(
    tmp_path, store, monkeypatch
):
    """SDD §7.2, §8 (review RV29-01): the automatic rollback powers the source on before it
    deletes the kit's converted volume - Cinder refusing that delete fails the rollback with the
    source running, never stopped, and its clock closed at the power-on (SDD §5.2, review
    RV30-03). A retry's cutover would boot the destination from that volume beside the source: it
    is refused before the kit runs and, the source running, ends failed; the rollback the
    operator then requests deletes the volume once Cinder does, and the next retry copies the VM
    again and stops it."""
    from seamless_migrate.providers.base import ProviderError
    from tests.fake_kit import KitRegistry

    refusing = [True]

    class Picky(KitRegistry):
        def get(self, provider):
            impl = super().get(provider)
            delete = impl.delete_volume

            async def delete_volume(volume_id):
                if refusing[0]:
                    impl.calls.append(("delete_volume", volume_id))
                    raise ProviderError("dst-rhoso: BadRequestException: 400: Invalid volume")
                await delete(volume_id)

            impl.delete_volume = delete_volume
            return impl

    h, m, state = _vmware_kit_journey(tmp_path, store, monkeypatch, Picky)
    runs = lambda: len((state / "runs").read_text().split())  # noqa: E731
    await h.orch.start()
    try:
        first = await h.wait_phase(m.id, P.failed, P.rolled_back, timeout=30)
        assert first.phase == P.failed and P.rolling_back in h.history(first), first.error
        assert (state / "power").read_text() == "on", "a refused clean-up kept the source stopped"
        assert (state / "volume").exists() and runs() == 1
        # the source runs again: its clock closed at the power-on, not at the rollback's end
        assert first.downtime_ended_at is not None and first.actual_downtime_s is not None
        kinds = [e.kind for e in h.store.events(since_seq=0, limit=1000)]
        assert kinds.count("migration.downtime_ended") == 1
        refusing[0] = False
        await h.orch.retry(m.id, "rina")
        refused = await h.wait_phase(m.id, P.rolled_back, P.failed, P.completed, timeout=30)
        assert refused.phase == P.failed, (refused.phase, refused.error)
        assert "roll the migration back again" in (refused.error or "")
        assert runs() == 1, "the kit ran on the converted volume while the source ran"
        await h.orch.rollback(m.id, "rina", "delete the kit's volume")
        second = await h.wait_phase(m.id, P.rolled_back, P.failed, P.completed, timeout=30)
        assert second.phase == P.rolled_back, (second.phase, second.error)
        assert not (state / "volume").exists() and (state / "power").read_text() == "on"
        await h.orch.retry(m.id, "rina")
        done = await h.wait_phase(m.id, P.completed, P.failed, P.rolled_back, timeout=30)
        assert done.phase == P.completed, (done.phase, done.error)
        assert runs() == 2 and (state / "power").read_text() == "off"
    finally:
        await h.orch.stop()


async def test_failed_cutover_auto_rolls_back(tmp_path, store):
    settings = make_settings(tmp_path)

    async def failing(ctx):
        await ctx.mark_downtime_start()
        raise PermanentStepError("dst volume attach failed: password=hunter2")

    executor = ScriptedExecutor(settings, hooks={StepName.CUTOVER: failing})
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.cold},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.wait_plan(plan.id, PlanStatus.completed)  # rolled_back is wave-complete
    await h.orch.stop()
    assert h.history(m)[-4:] == [P.cutover, P.failed, P.rolling_back, P.rolled_back]
    assert m.error and "attach failed" in m.error
    assert "hunter2" not in m.error, "Migration.error is redacted"
    for e in h.store.events(since_seq=0, limit=1000):
        assert "hunter2" not in e.message and "hunter2" not in str(e.data), e.kind
    assert m.downtime_started_at and m.downtime_ended_at and m.actual_downtime_s is not None
    assert [s for _, s in executor.calls] == [StepName.CUTOVER, StepName.ROLLBACK]
    assert "migration.error" in h.kinds()


async def test_step_output_lines_are_redacted_before_they_are_cut(tmp_path, store, monkeypatch):
    """SDD §4, §13.4 (Security.md C2-02): a step's output line reaches the migration.log
    subscribers through redact — its first 2000 + 16384 characters, then the cut to 2000 — so a
    password a task prints without no_log, a bearer token and a PEM block whose end lies beyond
    the cut never reach a viewer, while the rest of the line still does; a line of megabytes is
    redacted no further than the window."""
    settings = make_settings(tmp_path)
    pem = "-----BEGIN RSA PRIVATE KEY-----" + "K" * 2500 + "-----END RSA PRIVATE KEY-----"
    lines = [
        'fatal: [localhost]: FAILED! => {"msg": "login refused", "password": "Hunter2xyz"}',
        "GET /v3/auth/projects Authorization: Bearer abc.def.ghi",
        "deploy key: " + pem,
        "huge result: " + "x" * 2**22,
    ]
    redacted: list[int] = []

    def measured_redact(text, *args, **kwargs):
        if text.startswith("huge result: "):
            redacted.append(len(text))
        return redact(text, *args, **kwargs)

    monkeypatch.setattr("seamless_migrate.ai.redaction.redact", measured_redact)

    async def chatty_precopy(ctx):
        for line in lines:
            await ctx.log(line)
        return await executor.sim.run(StepName.PRECOPY, ctx)

    executor = ScriptedExecutor(settings, hooks={StepName.PRECOPY: chatty_precopy})
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    published: list[str] = []
    publish = h.bus.publish

    def recording_publish(event, persist):
        if event.kind == "migration.log":
            published.append(event.message)
        publish(event, persist)

    h.bus.publish = recording_publish
    await run_plan(h, plan)
    await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    shown = [m for m in published if m.startswith(("fatal:", "GET /v3", "deploy key:", "huge"))]
    assert len(shown) == 4, published
    assert all(len(m) <= 2000 for m in shown)
    assert not any(s in m for m in shown for s in ("Hunter2xyz", "abc.def.ghi", "KKKKKKKK"))
    assert shown[0].startswith('fatal: [localhost]: FAILED! => {"msg": "login refused"')
    assert shown[2] == "deploy key: [REDACTED PEM]"
    assert shown[3] == ("huge result: " + "x" * 2000)[:2000]
    assert redacted == [2000 + 16384]


class _HangingKnowledge:
    """agentmemory that does not answer the failure's lookup until released."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def on_failure(self, migration, step, error):
        self.entered.set()
        await self.release.wait()
        return None

    async def on_completed(self, migration):
        return None

    async def on_rolled_back(self, migration, reason):
        return None

    async def similar_for(self, option, vm):
        return []


def _stopping_cutover(settings):
    async def failing_cutover(ctx):
        await ctx.mark_downtime_start()  # the source VM is stopped
        raise PermanentStepError("conversion host lost")

    return ScriptedExecutor(settings, hooks={StepName.CUTOVER: failing_cutover})


async def test_a_restart_right_after_a_failure_still_rolls_the_stopped_vm_back(tmp_path, store):
    """SDD §8: the automatic rollback is saved in the same write as the failure, so a restart
    before it ends - the knowledge lookup still waiting on agentmemory, which holds the rollback
    back no more (audit OC-04) - resumes it: the VM never stays stopped in failed."""
    settings = make_settings(tmp_path)
    knowledge = _HangingKnowledge()
    rolling = asyncio.Event()

    async def held_rollback(ctx):
        rolling.set()
        await asyncio.Event().wait()  # until the restart cancels it

    executor = _stopping_cutover(settings)
    executor.hooks[StepName.ROLLBACK] = held_rollback
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.cold},
        executor=executor,
        knowledge=knowledge,
        settings=settings,
    )
    assert plan.verification.auto_rollback is True
    await run_plan(h, plan)
    await asyncio.wait_for(asyncio.gather(knowledge.entered.wait(), rolling.wait()), 10)
    m = await h.by_vm(plan.id, "vm-1")
    assert (m.phase, m.downtime_ended_at) == (P.rolling_back, None)
    assert [str(c.to_phase) for c in m.phase_history][-3:] == ["cutover", "failed", "rolling_back"]
    await h.orch.stop()  # a restart while the rollback and the lookup wait

    again = build_harness(
        tmp_path, store, [vm(1)], executor=ScriptedExecutor(settings), settings=settings
    )
    await again.orch.start()
    done = await again.wait_phase(m.id, P.rolled_back)
    await again.orch.stop()
    assert done.downtime_ended_at is not None
    assert [str(c.to_phase) for c in done.phase_history][-4:] == [
        "cutover",
        "failed",
        "rolling_back",
        "rolled_back",
    ]


async def test_a_slow_agentmemory_never_holds_the_automatic_rollback_back(tmp_path, store):
    """SDD §8 (audit OC-04): the failure's lookup of similar incidents ran inside the migration's
    own driver - _launch() was a no-op from there - so the automatic rollback waited for
    agentmemory (5 s per call with the real client) while the source stayed stopped. The lookup
    is a task of its own: the rollback runs at once, and a stop cancels the lookup."""
    settings = make_settings(tmp_path)
    knowledge = _HangingKnowledge()
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.cold},
        executor=_stopping_cutover(settings),
        knowledge=knowledge,
        settings=settings,
    )
    await run_plan(h, plan)
    await asyncio.wait_for(knowledge.entered.wait(), 10)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    done = await h.wait_phase(mid, P.rolled_back, timeout=5.0)  # the lookup still waits
    assert done.downtime_ended_at is not None and not knowledge.release.is_set()
    [lookup] = list(h.orch._lookups)
    await h.orch.stop()
    assert lookup.cancelled()


async def test_a_lookup_that_fails_is_logged_and_the_rollback_runs(tmp_path, store, caplog):
    """SDD §8, §14.3: agentmemory is best effort - a failure's lookup that raises attaches no
    note and is logged; the rollback it no longer holds back runs to its end."""

    class _RaisingKnowledge(_HangingKnowledge):
        async def on_failure(self, migration, step, error):
            raise RuntimeError("agentmemory answered garbage")

    settings = make_settings(tmp_path)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.cold},
        executor=_stopping_cutover(settings),
        knowledge=_RaisingKnowledge(),
        settings=settings,
    )
    caplog.set_level(logging.WARNING, logger="seamless_migrate.orchestrator")
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    done = await h.wait_phase(mid, P.rolled_back, timeout=5.0)
    deadline = asyncio.get_running_loop().time() + 5
    while h.orch._lookups:
        assert asyncio.get_running_loop().time() < deadline, "the lookup never ended"
        await asyncio.sleep(0.01)
    await h.orch.stop()
    assert not any(n.kind == "similar_incidents" for n in done.advisor_notes)
    assert f"the similar incidents of {mid} were not attached" in caplog.text
    assert mid not in h.orch._driver_crashes


async def test_a_driver_that_keeps_crashing_never_stops_the_other_plans(tmp_path, store):
    """SDD §8 (audit OC-01): the relaunch backoff computed 2**crashes * tick_s before its 300 s
    cap - at the 1,024th crash of one driver an int too large for a float, so from then on every
    tick raised in its first statement and no plan of the server moved. The exponent is capped
    first."""
    h = build_harness(tmp_path, store, [vm(1), vm(2)])
    crashing = plan_for([vm(1)], name="A", default_strategy=Strategy.cold)
    healthy = plan_for([vm(2)], name="B", default_strategy=Strategy.cold)
    store.put("plan", crashing)
    store.put("plan", healthy)
    real_cutover = h.orch._do_cutover

    async def crash_for_vm1(m):
        if m.vm.source_id == "vm-1":
            raise RuntimeError("a persistent error outside the step")
        await real_cutover(m)

    h.orch._do_cutover = crash_for_vm1
    await run_plan(h, crashing)
    mid = (await h.by_vm(crashing.id, "vm-1")).id
    deadline = asyncio.get_running_loop().time() + 10
    while h.orch._driver_crashes.get(mid, (0, 0.0))[0] < 1:
        assert asyncio.get_running_loop().time() < deadline, "the driver never crashed"
        await asyncio.sleep(0.01)
    # 1,024 crashes in a row, the last one longer ago than the 300 s cap: relaunched once more,
    # as after any other count
    h.orch._driver_crashes[mid] = (1024, time.monotonic() - 1000)
    while h.orch._driver_crashes[mid][0] < 1025:
        assert asyncio.get_running_loop().time() < deadline, "the driver was not relaunched"
        await asyncio.sleep(0.01)
    await h.orch.validate_plan(healthy.id, "alice")
    await h.orch.start_plan(healthy.id, "alice")
    other = await h.by_vm(healthy.id, "vm-2")
    try:
        done = await h.wait_phase(other.id, P.completed, timeout=5.0)
    finally:
        await h.orch.stop()
    assert done.phase == P.completed


async def test_a_relaunch_that_fails_for_one_migration_skips_only_it(tmp_path, store):
    """SDD §8 (audit OC-01): the relaunch of drivers that died is the first thing a tick does; an
    error there for one migration skips that migration, logged, and the tick goes on - the
    gates, passes and completions of every other plan with it."""
    h = build_harness(tmp_path, store, [vm(1), vm(2)])
    crashing = plan_for([vm(1)], name="A", default_strategy=Strategy.cold)
    healthy = plan_for([vm(2)], name="B", default_strategy=Strategy.cold)
    store.put("plan", crashing)
    store.put("plan", healthy)
    real_cutover, real_launch = h.orch._do_cutover, h.orch._launch
    held: set[str] = set()

    async def crash_for_vm1(m):
        if m.vm.source_id == "vm-1":
            held.add(m.id)
            raise RuntimeError("a persistent error outside the step")
        await real_cutover(m)

    def launch(mid):
        if mid in held:
            raise RuntimeError("the relaunch itself fails")
        real_launch(mid)

    h.orch._do_cutover, h.orch._launch = crash_for_vm1, launch
    await run_plan(h, crashing)
    mid = (await h.by_vm(crashing.id, "vm-1")).id
    deadline = asyncio.get_running_loop().time() + 10
    while mid not in held or (h.orch._drivers.get(mid) and not h.orch._drivers[mid].done()):
        assert asyncio.get_running_loop().time() < deadline, "the driver never crashed"
        await asyncio.sleep(0.01)
    h.orch._driver_crashes.pop(mid, None)  # no backoff: the next tick relaunches it, and fails
    await h.orch.validate_plan(healthy.id, "alice")
    await h.orch.start_plan(healthy.id, "alice")
    other = await h.by_vm(healthy.id, "vm-2")
    try:
        done = await h.wait_phase(other.id, P.completed, timeout=5.0)
    finally:
        await h.orch.stop()
    assert done.phase == P.completed


async def test_the_advisor_review_is_not_counted_as_downtime(tmp_path, store):
    """SDD §5.2 (audit OC-08): downtime_ended_at was taken after the advisor's review of a passed
    verification - Jev's screen and verify, up to 20 s each -, so the review's wait counted as
    outage in actual_downtime_s, the SLO compliance and the plan's figures. The outage ends when
    the verification step returns."""
    settings = make_settings(tmp_path)

    async def stopping_cutover(ctx):  # a real cutover: wall-clock downtime, no model seconds
        await ctx.mark_downtime_start()
        return StepResult(destination_server_id="dst-web-01")

    executor = ScriptedExecutor(settings, hooks={StepName.CUTOVER: stopping_cutover})
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.cold},
        executor=executor,
        settings=settings,
    )
    await h.orch.validate_plan(plan.id, "alice")
    reviewed: dict[str, datetime] = {}

    async def slow_review(vm_ref, result, review_reason=None):
        reviewed["at"] = h.clock()
        h.clock.advance(30)  # jev_screen and jev_verify take their time
        return None

    h.orch.advisor = SimpleNamespace(review_verification=slow_review)
    await h.orch.start_plan(plan.id, "alice")
    await h.orch.start()
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    # taken when the step returned: before the review began, not 30 s later
    for at in (m.downtime_ended_at, m.verification and m.verification.at):
        assert at is not None and timedelta(0) <= reviewed["at"] - at < timedelta(seconds=1), at
    assert m.actual_downtime_s is not None and m.actual_downtime_s < 30, m.actual_downtime_s


async def test_a_failed_event_write_after_a_failure_still_rolls_the_stopped_vm_back(
    tmp_path, store
):
    """SDD §5.1, §8: the failure, the rollback it starts and the failure's phase event are one
    transaction. Its event refused by the store - a database blip - leaves the stopped migration
    where it was and crashes the driver; the tick relaunches it, the step fails again, and the
    failure, its rollback and its event are written together: the stopped VM is rolled back."""
    import sqlalchemy as sa

    settings = make_settings(tmp_path)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.cold},
        executor=_stopping_cutover(settings),
        settings=settings,
    )
    blips = {"left": 1}

    def refuse_the_failure(conn, cursor, statement, parameters, context, executemany):
        if not blips["left"] or not statement.lstrip().upper().startswith("INSERT INTO EVENTS"):
            return
        params = context.compiled_parameters[0]
        if (
            params.get("kind") == "migration.phase"
            and (params.get("data") or {}).get("to") == "failed"
        ):
            blips["left"] -= 1
            raise OSError("server closed the connection unexpectedly")

    sa.event.listen(store.engine, "before_cursor_execute", refuse_the_failure)
    try:
        await run_plan(h, plan)
        done = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    finally:
        sa.event.remove(store.engine, "before_cursor_execute", refuse_the_failure)
        await h.orch.stop()
    assert blips["left"] == 0 and done.downtime_ended_at is not None
    assert [str(c.to_phase) for c in done.phase_history][-4:] == [
        "cutover",
        "failed",
        "rolling_back",
        "rolled_back",
    ]


async def test_a_verification_run_again_keeps_the_downtime_end_the_first_one_recorded(
    tmp_path, store
):
    """SDD §5.2: when the passed verification was saved but the migration had not reached
    completed — here its downtime_ended event fails to be written — the verification runs again
    and keeps the end of the downtime, and the downtime, the first one recorded: the outage ended
    then, however long the interruption took."""
    clock = Clock()
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold}, clock=clock
    )
    real_emit = h.orch._emit
    first: dict = {}

    async def blip_on_the_first_end(kind, message, **kw):
        if kind == "migration.downtime_ended" and not first:
            m = kw["migration"]
            first.update(end=m.downtime_ended_at, downtime=m.actual_downtime_s)
            clock.advance(600)  # the interruption takes ten minutes
            raise OSError("server closed the connection unexpectedly")
        return await real_emit(kind, message, **kw)

    h.orch._emit = blip_on_the_first_end
    await run_plan(h, plan)
    done = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert first, "the first verification recorded an end"
    assert (done.downtime_ended_at, done.actual_downtime_s) == (first["end"], first["downtime"])


async def test_failed_verification_rolls_back(tmp_path, store):
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {
            "default_strategy": Strategy.cold,
            "verification": VerificationConfig(timeout_s=0, tcp_ports=[]),
        },
    )
    h.destination.status = "ERROR"
    reviews: list[object] = []

    async def review(vm_ref, result, review_reason=None):
        reviews.append(result)
        return None

    # SDD §14.2: the advisor may flag a passed verification; a failed one (provider error
    # text, endpoints) is never sent to Jev
    h.orch.advisor = SimpleNamespace(review_verification=review)
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.orch.stop()
    assert h.history(m)[-5:] == [P.cutover, P.verifying, P.failed, P.rolling_back, P.rolled_back]
    assert "server_active" in m.error
    assert reviews == []


async def test_a_failed_gpu_check_rolls_the_vm_back(tmp_path, store):
    """SDD §7.5, §22.8 (plan task G8): the GPU checks are deterministic - a GPU VM's server created
    with another flavor than its GPU flavor fails the verification, and the plan's auto_rollback
    rolls the VM back, its source running again; the record keeps the failed check."""
    from seamless_migrate.domain.models import GpuDevice, Mappings
    from seamless_migrate.planning.gpu import gpu_requests

    alias = {"pci_passthrough:alias": "a100:1"}

    class GpuDestination(StaticDestination):
        """Lists the GPU flavor p1.a100 (SDD §22.3), but created the server with m1.small."""

        async def inventory(self):
            inventory = await super().inventory()
            size = {"name": "p1.a100", "vcpus": 16, "ram_mb": 131072, "disk_gb": 160}
            inventory.flavors.append({**size, "extra_specs": dict(alias)})
            inventory.gpu_flavors.append({**size, "gpus": gpu_requests(alias)})
            return inventory

        async def get_server(self, server_id):
            server = await super().get_server(server_id)
            return {**server, "flavor": {"name": "m1.small", "extra_specs": {}}}

    gpu_vm = vm(
        1,
        flavor="p1.src",
        flavor_extra_specs=dict(alias),
        gpus=[GpuDevice(kind="passthrough", alias="a100")],
    )
    h, plan = await setup(
        tmp_path,
        store,
        [gpu_vm],
        {
            "default_strategy": Strategy.cold,
            "mappings": Mappings(flavors={"p1.src": "p1.a100"}),
            "verification": VerificationConfig(timeout_s=0, tcp_ports=[]),
        },
        destination=GpuDestination(),
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.orch.stop()
    assert h.history(m)[-5:] == [P.cutover, P.verifying, P.failed, P.rolling_back, P.rolled_back]
    assert "gpu_flavor (the server's flavor is m1.small, not the GPU flavor p1.a100)" in m.error
    assert [c["name"] for c in m.verification.checks if not c["ok"]] == ["gpu_flavor"]
    assert m.downtime_ended_at is not None


async def test_a_failed_verification_keeps_provider_secrets_out_of_every_event(tmp_path, store):
    """SDD §12, §13.3: a provider error during verification is redacted wherever it goes - the
    check details of the verification summary in the migration.phase event included, which any
    viewer reads through /events and the stream; the failure itself stays readable."""

    class LeakyDestination(StaticDestination):
        async def get_server(self, server_id):
            raise ProviderError(
                "dst-rhoso: Unauthorized (HTTP 401): password=hunter2 token=gAAAAAsecret"
            )

    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {
            "default_strategy": Strategy.cold,
            "verification": VerificationConfig(timeout_s=0, tcp_ports=[]),
        },
        destination=LeakyDestination(),
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.orch.stop()

    events = h.store.events(since_seq=0, limit=10000)
    leaked = [
        (e.kind, e.seq)
        for e in events
        if any(secret in e.message + json.dumps(e.data) for secret in ("hunter2", "gAAAAAsecret"))
    ]
    assert leaked == []
    failed = [e for e in events if e.kind == "migration.phase" and e.data.get("to") == "failed"]
    assert [c["detail"] for c in failed[-1].data["verification"]["checks"]] == [
        "dst-rhoso: Unauthorized (HTTP 401): password=[REDACTED] token=[REDACTED]"
    ]
    assert "hunter2" not in (m.error or "")


async def test_rollback_request_during_step_is_serialized(tmp_path, store):
    settings = make_settings(tmp_path)
    gates: dict[str, asyncio.Event] = {}
    started: list[str] = []
    executor = ScriptedExecutor(
        settings, hooks={StepName.CUTOVER: blocking_cutover(gates, started)}
    )
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.cold},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    deadline = asyncio.get_running_loop().time() + 5
    while not started:
        assert asyncio.get_running_loop().time() < deadline, "cutover never started"
        await asyncio.sleep(0.01)
    out = await h.orch.rollback(mid, "bayu", "customer escalation")
    assert out.phase == P.rolling_back
    m = await h.wait_phase(mid, P.rolled_back)
    gates.setdefault(
        "vm-1", asyncio.Event()
    ).set()  # a late completion of the cancelled step changes nothing
    await asyncio.sleep(0.1)
    m = await h.migration(mid)
    await h.orch.stop()
    assert m.phase == P.rolled_back
    assert h.history(m)[-3:] == [P.cutover, P.rolling_back, P.rolled_back]
    assert h.history(m).count(P.rolling_back) == 1 and P.verifying not in h.history(m)
    assert [s for _, s in executor.calls] == [StepName.CUTOVER, StepName.ROLLBACK]
    assert m.downtime_ended_at is not None
    with pytest.raises(NotAllowed):
        await h.orch.rollback(mid, "bayu", "again")


async def test_cutover_requested_twice_transitions_once(tmp_path, store):
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm, "require_approval": True, "auto_cutover": False},
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.awaiting_cutover)
    await asyncio.gather(h.orch.request_cutover(m.id, "sari"), h.orch.request_cutover(m.id, "rina"))
    # ADV-17: neither request approves; a third approver opens the gate
    await h.orch.approve(m.id, "dewi", "Second pair of eyes")
    done = await h.wait_phase(m.id, P.completed)
    await h.orch.stop()
    assert h.history(done).count(P.cutover) == 1
    assert done.cutover_requested_by in {"sari", "rina"}
    assert [a.actor for a in done.approvals] == ["dewi"]
    with pytest.raises(NotAllowed):
        await h.orch.request_cutover(m.id, "sari")


async def test_retry_after_failure(tmp_path, store):
    settings = make_settings(tmp_path)
    attempts = {"precopy": 0}

    async def flaky_precopy(ctx):
        attempts["precopy"] += 1
        if attempts["precopy"] == 1:
            raise TransientStepError("HTTP 503 from cinder")  # retried transparently
        if attempts["precopy"] == 2:
            raise PermanentStepError("snapshot quota exceeded")
        return await executor.sim.run(StepName.PRECOPY, ctx)

    executor = ScriptedExecutor(settings, hooks={StepName.PRECOPY: flaky_precopy})
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    failed = await h.wait_phase(mid, P.failed)
    assert failed.downtime_started_at is None and "quota" in failed.error
    await asyncio.sleep(0.05)
    assert (await h.migration(mid)).phase == P.failed, "no auto-rollback without downtime"
    retried = await h.orch.retry(mid, "bayu")
    assert retried.phase == P.ready and retried.attempts == 1 and retried.error is None
    await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    assert attempts["precopy"] == 3
    with pytest.raises(NotAllowed):
        await h.orch.retry(mid, "bayu")


async def test_tick_relaunches_a_crashed_driver(tmp_path, store):
    """SDD §8: a driver that dies outside a step (here: an error while it handles the cutover, as
    a database error while persisting would) is resumed by the tick instead of leaving the
    migration in cutover without a driver until a restart, and the crash is a migration.error."""
    h, plan = await setup(tmp_path, store, [vm(1, "web-01")], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    mid = (await h.by_vm(plan.id, "vm-1")).id
    original = h.orch._do_cutover
    calls = {"n": 0}

    async def crash_once(m):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return await original(m)

    h.orch._do_cutover = crash_once
    await h.orch.start()
    await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    assert calls["n"] >= 2
    crashes = [
        e
        for e in h.store.events(since_seq=0, limit=100000)
        if e.kind == "migration.error" and (e.data or {}).get("step") == "driver"
    ]
    assert len(crashes) == 1 and crashes[0].migration_id == mid
    assert "database is locked" in crashes[0].data["message"]


async def test_crashed_driver_relaunch_backs_off(tmp_path, store):
    """A driver that keeps crashing is relaunched with a growing backoff (2^n x tick_s), not on
    every tick, and each crash is reported with its count (SDD §8)."""
    h, plan = await setup(tmp_path, store, [vm(1, "web-01")], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    mid = (await h.by_vm(plan.id, "vm-1")).id
    calls = {"n": 0}

    async def always_crash(m):
        calls["n"] += 1
        raise RuntimeError("database is locked")

    h.orch._do_cutover = always_crash
    await h.orch.start()
    await h.wait_phase(mid, P.cutover)
    await asyncio.sleep(60 * h.settings.tick_s)  # 60 ticks: one relaunch per tick would be ~60
    await h.orch.stop()
    assert 2 <= calls["n"] <= 10, calls
    counts = [
        e.data["crashes"]
        for e in h.store.events(since_seq=0, limit=100000)
        if e.kind == "migration.error" and (e.data or {}).get("step") == "driver"
    ]
    assert counts == list(range(1, len(counts) + 1)) and len(counts) == calls["n"]
    assert (await h.migration(mid)).phase == P.cutover


async def test_finalize_requires_confirm_name(tmp_path, store):
    h, plan = await setup(tmp_path, store, [vm(1, "web-01")], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    mid = (await h.by_vm(plan.id, "vm-1")).id
    with pytest.raises(NotAllowed):  # still ready: the loop has not started
        await h.orch.finalize(mid, "sari", delete_source=False, confirm="web-01")
    await h.orch.start()
    await h.wait_phase(mid, P.completed)
    with pytest.raises(BadRequest):
        await h.orch.finalize(mid, "sari", delete_source=True, confirm="web-1")
    out = await h.orch.finalize(mid, "sari", delete_source=True, confirm="web-01")
    await h.orch.stop()
    assert out.phase == P.finalized
    assert h.executor.calls[-1] == (mid, StepName.FINALIZE)
    with pytest.raises(NotAllowed):
        await h.orch.finalize(mid, "sari", delete_source=False, confirm="web-01")


async def test_cancel_and_set_strategy(tmp_path, store):
    shared = vm(2, disks=[make_disk(id="v2", multiattach=True)])
    h, plan = await setup(tmp_path, store, [vm(1), shared])
    await h.orch.validate_plan(plan.id, "alice")
    m1 = await h.by_vm(plan.id, "vm-1")
    m2 = await h.by_vm(plan.id, "vm-2")
    changed = await h.orch.set_strategy(m1.id, Strategy.cold, "rina")
    assert changed.strategy == Strategy.cold and changed.estimate.strategy == Strategy.cold
    assert (await h.plan(plan.id)).strategy_overrides[m1.vm.source_id] == Strategy.cold
    with pytest.raises(BadRequest, match="not eligible"):
        await h.orch.set_strategy(m2.id, Strategy.warm, "rina")
    cancelled = await h.orch.cancel(m2.id, "rina", "out of scope")
    assert cancelled.phase == P.cancelled
    with pytest.raises(NotAllowed):
        await h.orch.set_strategy(m2.id, Strategy.cold, "rina")
    with pytest.raises(NotAllowed):
        await h.orch.cancel(m2.id, "rina", "again")


async def test_a_strategy_change_during_a_validation_waits_for_it_and_is_kept(tmp_path, store):
    """SDD §8: a strategy change takes the plan's lock, then the migration's, as the advisor's
    override does: during a validation it waits for it and then applies, so the migration's
    strategy and the plan's override agree — no stale validation write undoes the change."""
    h, plan = await setup(tmp_path, store, [vm(1), vm(2)])
    await h.orch.validate_plan(plan.id, "alice")
    m2 = await h.by_vm(plan.id, "vm-2")
    other = next(e.strategy for e in m2.estimates if e.eligible and e.strategy != m2.strategy)
    gate, entered = asyncio.Event(), asyncio.Event()
    real_get_vm = h.source.get_vm

    async def slow_get_vm(source_id):
        if source_id == "vm-1":  # the validation has read the plan and fetches its VMs
            entered.set()
            await gate.wait()
        return await real_get_vm(source_id)

    h.source.get_vm = slow_get_vm
    validation = asyncio.create_task(h.orch.validate_plan(plan.id, "alice"))
    await asyncio.wait_for(entered.wait(), 5)
    change = asyncio.create_task(h.orch.set_strategy(m2.id, other, "bayu"))
    await asyncio.sleep(0.05)
    assert not change.done(), "the change waits for the running validation"
    gate.set()
    await validation
    assert (await change).strategy == other
    stored, stored_plan = await h.migration(m2.id), await h.plan(plan.id)
    assert (stored.strategy, stored_plan.strategy_overrides.get("vm-2")) == (other, other)

    # the override is written under the plan's lock: a validation queued behind the change
    # reads the plan only once it holds the change
    under_lock: list[bool] = []
    write_all = h.orch.store.put_all_with_events

    def spying_write(writes, appended):
        under_lock.append(h.orch.plan_lock(plan.id).locked())
        return write_all(writes, appended)

    h.orch.store.put_all_with_events = spying_write
    await h.orch.set_strategy(m2.id, m2.strategy, "bayu")
    assert under_lock == [True]


async def test_a_change_to_a_migration_being_finalized_is_refused_before_the_plans_lock(
    tmp_path, store
):
    """SDD §8: a finalize holds the migration's lock across a provider deletion; a strategy change
    or an advisor decision sent for that completed migration is refused at once — before it takes
    the plan's lock the tick needs — not after waiting for the finalize."""
    from tests.test_policies import rec_for

    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    m.phase = P.completed
    store.put("migration", m)
    rec = rec_for(m.id).model_copy(update={"plan_id": plan.id})
    store.put("recommendation", rec)
    async with h.orch._lock(m.id):  # a finalize deleting the source VM
        for change in (
            h.orch.set_strategy(m.id, Strategy.cold, "bayu"),
            h.orch.approve_recommendation(rec.id, "sari"),
        ):
            task = asyncio.create_task(change)
            await asyncio.sleep(0.05)
            assert task.done(), "it waits for the finalize, holding the plan's lock"
            assert not h.orch.plan_lock(plan.id).locked()
            with pytest.raises(NotAllowed, match="the strategy cannot change in completed"):
                await task


@pytest.mark.parametrize(
    ("write", "row"),
    [
        ("UPDATE documents", "plan"),
        ("UPDATE documents", "migration"),
        ("INSERT INTO events", "migration.action"),
        ("INSERT INTO events", "plan.updated"),
    ],
)
async def test_a_strategy_change_a_failed_write_cuts_short_leaves_nothing_and_the_retry_makes_it(
    tmp_path, store, refuse_write, write, row
):
    """SDD §8 (reviews ST-08, RV48-02): a strategy change wrote the plan's entry, the migration and
    its two events each on its own - a failure between them left the plan pinning a strategy no
    event named, which the next validation applied under its own actor. They are one transaction:
    a write refused leaves none of them - the next validation keeps the strategy -, and the retry
    makes the whole change, with one event of each kind."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    chosen = next(e.strategy for e in m.estimates if e.eligible and e.strategy != m.strategy)

    def changes():
        events = store.events(since_seq=0, limit=1000)
        return [e.kind for e in events if e.actor == "rina"]

    refused = refuse_write(store, write, row)
    with pytest.raises(RuntimeError, match="briefly unavailable"):
        await h.orch.set_strategy(m.id, chosen, "rina")
    assert refused == [f"{write} {row}"]
    pinned = (await h.plan(plan.id)).strategy_overrides
    assert ((await h.migration(m.id)).strategy, pinned, changes()) == (m.strategy, {}, [])
    await h.orch.validate_plan(plan.id, "alice")
    assert (await h.migration(m.id)).strategy == m.strategy
    await h.orch.set_strategy(m.id, chosen, "rina")
    pinned = (await h.plan(plan.id)).strategy_overrides
    assert ((await h.migration(m.id)).strategy, pinned, changes()) == (
        chosen,
        {"vm-1": chosen},
        ["migration.action", "plan.updated"],
    )


async def test_a_strategy_change_another_writer_moved_the_plan_under_is_made_again(
    tmp_path, store, land_first
):
    """SDD §8: a plan moved between a strategy change's read and its transaction - the CLI, a
    driver - fails the versioned write, which leaves nothing; the change is read again and made
    again, the other writer's change kept."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    chosen = next(e.strategy for e in m.estimates if e.eligible and e.strategy != m.strategy)

    def rename():
        stored, version = store.get_versioned("plan", plan.id, Plan)
        renamed = stored.model_copy(update={"name": "renamed meanwhile"})
        return store.put("plan", renamed, expected_version=version)

    land_first(store, "UPDATE documents", "plan", rename)
    await h.orch.set_strategy(m.id, chosen, "rina")
    stored = await h.plan(plan.id)
    assert (stored.name, stored.strategy_overrides) == ("renamed meanwhile", {"vm-1": chosen})
    assert (await h.migration(m.id)).strategy == chosen


async def test_set_strategy_clears_force_window(tmp_path, store):
    """SDD §5.4: approvals, cutover_requested and a pending force_window belong to the assessment
    they were given for — a strategy change clears all three."""
    from seamless_migrate.domain.models import Approval, Migration

    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    m.approvals = [Approval(actor="sari", at=h.orch.now())]
    m.cutover_requested = True
    m.force_window = True
    store.put("migration", m)
    await h.orch.set_strategy(m.id, Strategy.cold, "rina")
    changed = store.get("migration", m.id, Migration)
    assert changed.approvals == [] and changed.cutover_requested is False
    assert changed.force_window is False


async def test_begin_rechecks_gate_under_lock(tmp_path, store):
    """The tick evaluates the cutover gate on its snapshot; an action under the migration's lock
    (e.g. set_strategy clearing the approvals) may change it before _begin runs. _begin
    re-checks the gate on the fresh copy, so a source VM is never stopped without approval."""
    from seamless_migrate.domain.models import Migration

    h, plan = await setup(
        tmp_path, store, [vm(1)], {"require_approval": True, "auto_cutover": True}
    )
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    m.phase = P.awaiting_cutover
    m.approvals = []  # cleared after the tick's snapshot saw an approval
    store.put("migration", m)
    began = await h.orch._begin(m.id, {P.awaiting_cutover}, P.cutover, "cutover gate open")
    assert began is False
    assert store.get("migration", m.id, Migration).phase == P.awaiting_cutover


async def test_pause_stops_new_work(tmp_path, store):
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    paused = await h.orch.pause_plan(plan.id, "alice")
    assert paused.status == PlanStatus.paused
    await h.orch.start()
    await asyncio.sleep(0.1)
    assert (await h.by_vm(plan.id, "vm-1")).phase == P.ready
    await h.orch.start_plan(plan.id, "alice")
    await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert "plan.paused" in h.kinds()


async def test_resume_mid_cutover_is_idempotent(tmp_path, store):
    """Restart in the middle of cutover: no second source stop, no second destination server."""
    settings = make_settings(tmp_path)
    cloud = {"stops": 0, "servers": {}}
    crash_point = asyncio.Event()

    async def cutover(ctx):
        m = ctx.migration
        if m.downtime_started_at is None:  # executor state: source still running
            cloud["stops"] += 1
            await ctx.mark_downtime_start()
        await crash_point.wait()  # the control plane dies here on the first run
        if m.vm.name not in cloud["servers"]:  # os-migrate skips existing servers by name
            cloud["servers"][m.vm.name] = f"dst-{len(cloud['servers']) + 1}"
        return StepResult(destination_server_id=cloud["servers"][m.vm.name])

    executor = ScriptedExecutor(settings, hooks={StepName.CUTOVER: cutover})
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.cold},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    deadline = asyncio.get_running_loop().time() + 5
    while cloud["stops"] == 0 or (await h.migration(mid)).downtime_started_at is None:
        assert asyncio.get_running_loop().time() < deadline, "stop or downtime mark missing"
        await asyncio.sleep(0.01)
    first = await h.migration(mid)
    assert first.phase == P.cutover and first.downtime_started_at is not None
    # its event follows the clock: crash once both are in, so only a second one counts
    await h.wait_kind("migration.downtime_started")
    await h.orch.stop()  # crash

    crash_point.set()
    h2 = build_harness(tmp_path, store, [vm(1)], executor=executor, settings=settings)
    await h2.orch.start()  # resumes migrations in precopy|syncing|cutover|verifying|rolling_back
    done = await h2.wait_phase(mid, P.completed)
    await h2.orch.stop()
    assert cloud["stops"] == 1 and len(cloud["servers"]) == 1
    assert done.downtime_started_at == first.downtime_started_at
    assert h2.history(done).count(P.cutover) == 1
    assert done.destination_server_id == "dst-1"
    kinds = h2.kinds()
    assert kinds.count("migration.downtime_started") == 1


def _tie_advisor(selected: str, confidence: float):
    """A Jev that answers ``selected`` at ``confidence`` for a VM whose cold and warm tie."""
    jev_settings = Settings(jev_mode="stdio")
    decide = load_fixture("decide")
    decide["recommendation"].update(selected=selected, confidence=confidence)
    factory, session = session_factory(fixture_responder({"decide": decide}))
    return Advisor(JevClient(jev_settings, session_factory=factory), jev_settings), session


def _recommendations(store) -> list[AdvisorRecommendation]:
    return store.list("recommendation", AdvisorRecommendation)


def _audit(store) -> list[Event]:
    return store.events(since_seq=0, limit=1000, kinds=AUDIT_KINDS)


def _answering_advisor(**answers):
    """A Jev that answers each tool with ``answers[tool]``: a payload, or a responder of its
    arguments; the others with their recorded fixtures."""
    jev_settings = Settings(jev_mode="stdio")
    factory, session = session_factory(fixture_responder(answers))
    return Advisor(JevClient(jev_settings, session_factory=factory), jev_settings), session


@pytest.mark.parametrize(
    "answer",
    [
        {"recommendation": {"selected": "warm", "confidence": 0.9, "status": ["escalate"]}},
        {"recommendation": {"selected": "warm", "confidence": 0.9, "probabilities": ["warm"]}},
        {"recommendation": "warm"},
        {"recommendation": {"selected": ["warm"], "confidence": 0.9}},
        {"recommendation": {"selected": "warm", "confidence": 0.9}, "model": 1},
        # a status that is falsy is no absent one (review RV50-03)
        *(
            {"recommendation": {"selected": "warm", "confidence": 0.99, "status": status}}
            for status in ([], {}, 0, False)
        ),
    ],
    ids=[
        "status-a-list",
        "probabilities-a-list",
        "a-string",
        "selected-a-list",
        "model-a-number",
        "status-an-empty-list",
        "status-an-empty-map",
        "status-zero",
        "status-false",
    ],
)
async def test_a_strategy_answer_of_another_shape_falls_back_to_the_rules(tmp_path, store, answer):
    """SDD §14.1, §14.4 rule 2 (audit AD-01): Jev is an outside service. An answer of another
    shape - a list where a string goes, a string where an object goes - raised out of the
    validation, a 500 for every validation while Jev answered so. It is invalid_response: the
    validation records fallback_rules and keeps the deterministic option."""
    advisor, _ = _answering_advisor(decide=answer)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    m = await h.by_vm(plan.id, "vm-1")
    assert (rec.decision.status, m.strategy) == (DecisionStatus.fallback_rules, Strategy.cold)
    assert "invalid_response" in (rec.decision.reason_text or ""), rec.decision.reason_text


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"results": ["item-1"]}, ["invalid response: a result is no object"]),
        (
            {"results": {"item-1": {"classification": "stateless_web"}}},
            ["invalid response: results is no list"],
        ),
        ({"results": [{"id": "item-1", "classification": ["db"], "decision": "auto"}]}, []),
    ],
    ids=["a-result-a-string", "results-a-mapping", "a-classification-a-list"],
)
async def test_a_classification_answer_of_another_shape_falls_back_to_the_heuristic(
    tmp_path, store, answer, error
):
    """SDD §14.1, §14.2 (audit AD-01): a result that is no object, or a classification that is no
    tier, raised out of auto-waves; such an answer's items take the heuristic's tier, the note
    names the invalid response, and the waves are planned."""
    advisor, _ = _answering_advisor(classify=answer)
    h, plan = await setup(tmp_path, store, [vm(1)], advisor=advisor)
    await h.orch.auto_waves(plan.id, 10, "alice")
    assert (await h.plan(plan.id)).waves
    events = store.events(since_seq=0, limit=1000, kinds=["advisor.classification"])
    assert [(e.data["fallback"], e.data["errors"]) for e in events] == [(["vm-1"], error)]


@pytest.mark.parametrize(
    "confidence",
    ["NaN", "Infinity", "true", "1.5", "-0.1", "1" + "0" * 400, "-1" + "0" * 400, "1e999"],
    ids=["NaN", "Infinity", "true", "1.5", "-0.1", "400-digits", "-400-digits", "1e999"],
)
async def test_a_confidence_that_is_no_probability_is_never_applied(tmp_path, store, confidence):
    """SDD §4.2, §14.1, §14.4 (audit AD-02, review RV50-03): a confidence is a number from 0 to 1,
    the only number compared with a threshold. NaN, Infinity, a JSON true and 1.5 all passed
    'confidence < threshold' and the global default applied Jev's option; an integer of 400
    digits raised OverflowError out of the validation. Such an answer falls back."""
    decide = load_fixture("decide")
    decide["recommendation"].update(selected="warm", confidence=0.25)
    text = json.dumps(decide).replace('"confidence": 0.25', f'"confidence": {confidence}')
    assert f'"confidence": {confidence}' in text
    advisor, _ = _answering_advisor(decide=lambda arguments: tool_result(text))
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    m = await h.by_vm(plan.id, "vm-1")
    assert (rec.decision.status, m.strategy) == (DecisionStatus.fallback_rules, Strategy.cold)


@pytest.mark.parametrize("bad", ["\x00", "\ud83d"], ids=["nul", "lone-surrogate"])
async def test_a_jev_model_name_never_holds_a_migration_for_a_missing_recommendation(
    tmp_path, store, bad
):
    """SDD §11, §14.1 (review RV49-06): Jev's model name went into the recommendation as sent; with
    a NUL or a lone surrogate the recommendation's write was refused after the migration's, which
    it already held: approve answered 404 and the strategy could not change. The answer's strings
    arrive replaced, so the recommendation is stored and approvable."""
    decide = load_fixture("decide")
    decide["recommendation"].update(selected="warm", confidence=0.99)
    decide["model"] = "jev-1.13.0" + bad
    advisor, _ = _answering_advisor(decide=decide)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    store.put("advisor_policy", _finance(plan))
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    [rec] = _recommendations(store)
    assert (m.advisor_hold, rec.model.version) == (rec.id, "jev-1.13.0�")
    approved = await h.orch.approve_recommendation(rec.id, "rina")
    assert approved.decision.status == DecisionStatus.approved


@pytest.mark.parametrize(
    ("where", "bad"),
    [("screen", "\x00"), ("screen", "\ud83d"), ("verify", "\x00"), ("verify", "\ud83d")],
    ids=["screen-nul", "screen-lone", "verify-nul", "verify-lone"],
)
async def test_a_jev_verification_answer_never_stalls_a_cut_over_migration(
    tmp_path, store, where, bad
):
    """SDD §11, §14.1 (review RV49-01): Jev's screen reason and verify results went into the
    migration's advisor note as sent; with a NUL or a lone surrogate the verifying save was refused
    on every attempt - the driver crashed, resumed, verified and crashed again, the cut-over VM
    (source stopped) never completing. The answer's strings arrive replaced: it completes."""
    jev_settings = Settings(jev_mode="stdio", jev_deployment="on_prem")
    answers: dict = {"screen": {"recommendation": {"action": "pass", "reason": "ok"}}}
    if where == "screen":
        answers["screen"] = {
            "recommendation": {"action": "pass", "reason": f"booted to a login prompt {bad}"}
        }
    else:
        verify = load_fixture("verify")
        verify["results"][0]["claim"] = f"The guest operating system finished booting {bad}"
        answers["verify"] = verify
    factory, _ = session_factory(fixture_responder(answers))
    advisor = Advisor(JevClient(jev_settings, session_factory=factory), jev_settings)
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold}, advisor=advisor
    )
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    try:
        await h.wait_phase(mid, P.completed, P.failed, timeout=5.0)
    except AssertionError:
        pass
    finally:
        crashes = h.orch._driver_crashes.get(mid, (0, 0.0))[0]
        await h.orch.stop()
    errors = [e for e in h.store.events(since_seq=0, limit=10000) if e.kind == "migration.error"]
    assert ((await h.migration(mid)).phase, crashes, errors) == (P.completed, 0, [])


@pytest.mark.parametrize(
    "field",
    [{"tags": {"note": "backup\x00window"}}, {"os_type": "ubuntu\x00 22.04"}],
    ids=["nova-metadata-value", "image-property-os-type"],
)
async def test_a_tenants_metadata_value_never_fails_the_whole_validation(tmp_path, store, field):
    """SDD §4.2, §11 (review RV49-02): Nova lets a server metadata value hold any string, and an
    image's os_type is a free property; with a NUL in one, the VM's migration was refused by the
    store and the validation of the whole plan failed - web-02, a VM with nothing wrong, got no
    migration. Free text no filter or mapping reads is written U+FFFD: both VMs validate."""
    h, plan = await setup(
        tmp_path, store, [vm(1, **field), vm(2)], {"default_strategy": Strategy.cold}
    )
    await h.orch.validate_plan(plan.id, "alice")
    assert sorted(m.vm.name for m in await h.migrations(plan.id)) == ["web-01", "web-02"]


@pytest.mark.parametrize("redact_names", [False, True])
async def test_auto_waves_never_send_the_destination_providers_name_to_jev(
    tmp_path, store, redact_names
):
    """SDD §13.4 (audit AD-09): the destination provider's name never reaches Jev, whatever
    SEAMLESS_MEMORY_REDACT_NAMES says. The strategy question replaced it even inside a VM's name;
    jev_classify's items did not - a VM named after the destination site sent it as it is. The
    classification replaces it by the destination's kind and series, as the strategy question
    does, the VM's own name too while names are redacted."""
    jev_settings = Settings(jev_mode="stdio", memory_redact_names=redact_names)
    factory, session = session_factory(fixture_responder({}))
    advisor = Advisor(JevClient(jev_settings, session_factory=factory), jev_settings)
    h, plan = await setup(tmp_path, store, [vm(1, name="jakarta-dc3-prod-db01")], advisor=advisor)
    store.put("provider", DST_PROVIDER.model_copy(update={"name": "jakarta-dc3-prod"}))
    await h.orch.auto_waves(plan.id, 10, "alice")
    [(_, arguments, _)] = [c for c in session.calls if c[0] == "jev_classify"]
    sent = json.dumps(arguments)
    assert "jakarta" not in sent, arguments["items"]
    [item] = arguments["items"]
    assert item["text"].startswith("name=[vm] " if redact_names else "name=RHOSO 18.0-db01 ")


async def test_auto_waves_need_no_destination_to_name(tmp_path, store):
    """SDD §13.4 (audit AD-09): the classification names the destination by kind and series; a
    plan whose destination provider is gone meanwhile has no name to leave either - its waves
    are planned as before."""
    advisor, _ = _answering_advisor()
    h, plan = await setup(tmp_path, store, [vm(1)], advisor=advisor)
    store.delete("provider", DST_PROVIDER.id)
    await h.orch.auto_waves(plan.id, 10, "alice")
    assert (await h.plan(plan.id)).waves


async def test_advisor_tie_break_recorded(tmp_path, store):
    """SDD §14.4: the tie is recorded as a recommendation and its decision; the strategy note of
    earlier versions is no longer written."""
    # 1 GiB used on a 4 GiB disk: cold and warm downtimes tie (within 60 s)
    advisor, session = _tie_advisor("warm", 0.9)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    assert m.strategy == Strategy.warm
    assert [n for n in m.advisor_notes if n.kind == "strategy"] == []
    [rec] = _recommendations(store)
    assert (rec.decision_type, rec.migration_id, rec.plan_id, rec.provider_id) == (
        "migration_strategy",
        m.id,
        plan.id,
        plan.source_provider_id,
    )
    assert (rec.subject.kind, rec.subject.id, rec.subject.name) == ("vm", "vm-1", m.vm.name)
    assert (rec.model.name, rec.model.version) == ("jev", "jev-1.13.0")
    assert (rec.recommendation.option, rec.recommendation.confidence) == ("warm", 0.9)
    assert (rec.decision.status, rec.decision.final_option) == (DecisionStatus.auto_applied, "warm")
    assert [e.kind for e in _audit(store)] == ["recommendation.created", "decision.auto_applied"]
    assert not [e for e in store.events(since_seq=0, limit=1000) if e.kind == "advisor.strategy"]
    assert session.calls and session.calls[0][0] == "jev_decide"


@pytest.mark.parametrize(
    ("confidence", "kind", "status"),
    [
        (0.9, "decision.auto_applied", "auto_applied"),
        (0.57, "decision.below_threshold", "below_threshold"),
    ],
)
async def test_a_recommendation_is_audited_once_with_its_redacted_input(
    tmp_path, store, confidence, kind, status
):
    """SDD §14.5 (ADV-05): recommendation.created, then exactly one decision event with its actor
    and model version; the input snapshot is stored under the hash of its content."""
    advisor, _ = _tie_advisor("warm", confidence)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    [rec] = _recommendations(store)
    created, decided = _audit(store)
    assert (created.kind, created.actor, created.migration_id) == (
        "recommendation.created",
        "system:advisor",
        m.id,
    )
    assert created.data == {
        "target_id": rec.id,
        "model_version": "jev-1.13.0",
        "decision_type": "migration_strategy",
        "subject": {"kind": "vm", "id": "vm-1", "name": m.vm.name},
        "option": "warm",
        "confidence": confidence,
        "snapshot_sha256": rec.input_snapshot_sha256,
    }
    assert (decided.kind, decided.actor, decided.plan_id) == (
        kind,
        "policy:global-default",
        plan.id,
    )
    assert decided.data == {
        "target_id": rec.id,
        "model_version": "jev-1.13.0",
        "before": None,
        "after": {
            "status": status,
            "final_option": rec.decision.final_option,
            "reason_code": str(rec.decision.reason_code),
            "reason_text": None,
            "policy_id": "global-default",
            "threshold_used": 0.6,
        },
    }
    snapshot = store.get("advisor_snapshot", rec.input_snapshot_sha256, AdvisorSnapshot)
    assert snapshot.sha256 == hashlib.sha256(canonical_json(snapshot.payload).encode()).hexdigest()
    assert snapshot.payload["tool"] == "jev_decide"
    assert snapshot.payload["arguments"]["candidates"]
    assert (snapshot.payload["deterministic"], snapshot.payload["scope"]) == (
        "cold",
        ["cold", "warm"],
    )


async def test_the_question_to_jev_names_the_destination_by_kind_never_its_providers_name(
    tmp_path, store
):
    """SDD §1 naming, §13.4 (review RV20-01): validation asks the advisor with the destination's
    kind and series; its provider's name, operator free text, reaches neither the call to Jev nor
    the stored input snapshot."""
    advisor, session = _tie_advisor("warm", 0.9)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    store.put("provider", DST_PROVIDER.model_copy(update={"name": "ACME Bank - Jakarta DC3"}))
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    snapshot = store.get("advisor_snapshot", rec.input_snapshot_sha256, AdvisorSnapshot)
    assert snapshot.payload["arguments"]["decision"].endswith(" moving to RHOSO 18.0?")
    assert "ACME" not in json.dumps(snapshot.payload)
    assert session.calls and session.calls[0][0] == "jev_decide"
    assert "ACME" not in json.dumps(session.calls, default=str)


class _Settling(StaticDestination):
    """A community destination whose check - the one validation runs - settles 2026.2."""

    async def check(self):
        return {**await super().check(), "release_range": ["2026.2"], "release": "2026.2"}


@pytest.mark.parametrize("stored", [{"release": "2025.1"}, {}], ids=["earlier", "reset"])
async def test_the_question_to_jev_names_the_series_the_validations_own_check_settled(
    tmp_path, store, stored
):
    """SDD §13.4 (review RV21-01): the question names the destination by the series the check
    this validation runs settled - the report it judges with - never by the report stored before
    it: an earlier series, or none after a credentials reset."""
    advisor, _ = _tie_advisor("warm", 0.9)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor, destination=_Settling()
    )
    store.put(
        "provider",
        DST_PROVIDER.model_copy(update={"kind": ProviderKind.openstack, "capabilities": stored}),
    )
    await h.orch.validate_plan(plan.id, "alice")
    assert store.get("provider", DST_PROVIDER.id, Provider).capabilities["release"] == "2026.2"
    [rec] = _recommendations(store)
    snapshot = store.get("advisor_snapshot", rec.input_snapshot_sha256, AdvisorSnapshot)
    decision = snapshot.payload["arguments"]["decision"]
    assert decision.endswith(" moving to a community OpenStack 2026.2 destination?"), decision


def _finance(plan) -> AdvisorPolicy:
    return AdvisorPolicy(
        id="pol-finance",
        name="Finance — approval required",
        scope=PolicyScope(provider_ids=[plan.source_provider_id]),
        mode="always_require_approval",
        threshold=0.6,
        require_override_reason=True,
        updated_by="rina",
    )


#: a plan that would cut a cold migration over at once: only the advisor hold can stop it
NO_GATE = {"require_approval": False, "auto_cutover": True}


async def test_with_the_finance_policy_a_decision_waits_and_holds_its_migration(tmp_path, store):
    """ADV-04: never auto_applied under the Finance policy, whatever the confidence; the migration
    does not start until a human decides (SDD §5.4, §14.6)."""
    advisor, _ = _tie_advisor("warm", 0.99)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    store.put("advisor_policy", _finance(plan))
    await run_plan(h, plan)
    m = await h.by_vm(plan.id, "vm-1")
    [rec] = _recommendations(store)
    assert (rec.decision.status, rec.decision.final_option, rec.decision.policy_id) == (
        DecisionStatus.pending_approval,
        None,
        "pol-finance",
    )
    assert (m.advisor_hold, m.strategy) == (rec.id, Strategy.cold)
    assert [e.kind for e in _audit(store)][-1] == "decision.pending_approval"
    await asyncio.sleep(0.3)  # thirty ticks: no timeout applies the advice
    assert (await h.migration(m.id)).phase == P.ready

    approved = await h.orch.approve_recommendation(rec.id, "rina")
    assert (approved.decision.status, approved.decision.final_option) == (
        DecisionStatus.approved,
        "warm",
    )
    m = await h.wait_phase(m.id, P.precopy, P.syncing, P.awaiting_cutover, P.cutover, P.completed)
    assert (m.strategy, m.advisor_hold) == (Strategy.warm, None)
    decided = _audit(store)[-1]
    assert (decided.kind, decided.actor, decided.data["before"]["status"]) == (
        "decision.approved",
        "user:rina",
        "pending_approval",
    )


async def test_an_override_records_its_reason_in_the_audit_trail(tmp_path, store):
    """SDD §14.5: the trail itself says why a decision was overridden - the decision event carries
    the reason a person gave (``after``) and the decision it replaced (``before``, none given)."""
    advisor, _ = _tie_advisor("warm", 0.99)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    store.put("advisor_policy", _finance(plan))
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    waiting = _audit(store)[-1]
    assert (waiting.kind, waiting.data["after"]["reason_text"]) == (
        "decision.pending_approval",
        None,
    )
    reason = "the batch window allows a longer downtime"
    await h.orch.override_recommendation(rec.id, "cold", reason, "rina")
    overridden = _audit(store)[-1]
    assert (overridden.kind, overridden.actor) == ("decision.overridden", "user:rina")
    assert overridden.data["after"]["reason_text"] == reason
    assert overridden.data["before"] == waiting.data["after"]


async def test_an_approval_cut_short_on_a_running_plan_leaves_its_migration_held(
    tmp_path, store, refuse_write
):
    """SDD §14.6 (review RV48-01): an approval wrote the migration - its hold cleared - before the
    decision, so when the decision's write failed a running plan's tick started the released
    migration at once, and the approver's retry was refused "the strategy cannot change in
    cutover": the migration ran a strategy its decision never recorded. One transaction keeps
    the migration held until the decision is written: the tick waits, the retry approves, then
    the migration runs the approved strategy, with one decision.approved."""
    advisor, _ = _tie_advisor("warm", 0.99)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    store.put("advisor_policy", _finance(plan))
    await run_plan(h, plan)
    try:
        m = await h.by_vm(plan.id, "vm-1")
        [rec] = _recommendations(store)
        assert (m.advisor_hold, m.phase) == (rec.id, P.ready)
        refused = refuse_write(store, "UPDATE documents", "recommendation")
        with pytest.raises(RuntimeError, match="briefly unavailable"):
            await h.orch.approve_recommendation(rec.id, "rina")
        assert refused == ["UPDATE documents recommendation"]
        await asyncio.sleep(0.2)  # many ticks: a held migration is never started
        held = await h.migration(m.id)
        assert (held.phase, held.advisor_hold) == (P.ready, rec.id)
        approved = await h.orch.approve_recommendation(rec.id, "rina")
        later = (P.precopy, P.syncing, P.awaiting_cutover, P.cutover, P.verifying, P.completed)
        started = await h.wait_phase(m.id, *later)
    finally:
        await h.orch.stop()
    decided = [e for e in _audit(store) if e.kind == "decision.approved"]
    assert (approved.decision.status, started.strategy, len(decided)) == (
        DecisionStatus.approved,
        Strategy.warm,
        1,
    )


def _decide_answer(selected: str = "warm", confidence: float = 0.99, **extra) -> dict:
    decide = load_fixture("decide")
    decide["recommendation"].update(selected=selected, confidence=confidence, **extra)
    return decide


def _policy(pid: str, mode: str, **kw) -> AdvisorPolicy:
    return AdvisorPolicy(id=pid, name=pid, mode=mode, threshold=0.6, updated_by="rina", **kw)


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        ({"status": "escalate"}, "the advisor's answer was escalate"),
        ({"status": "invalid_response"}, "the advisor's answer was invalid_response"),
        ({"escaped": True}, "the advisor answered warm, not an option"),
    ],
    ids=["escalate", "invalid_response", "escaped"],
)
async def test_an_answer_that_could_not_be_applied_is_never_approved_later(
    tmp_path, store, extra, why
):
    """SDD §4.2, §14.6 (audit AD-03): an answer Jev escalated, marked invalid or escaped was
    recorded as fallback_rules, but nothing stored said it could not be applied: a later approve
    ran the option Jev itself would not stand behind - and, under a policy that requires an
    override reason, without one. The recommendation keeps why; no approve takes it, while an
    override to that option stays a person's call, with its reason."""
    advisor, _ = _answering_advisor(decide=_decide_answer("warm", 0.9, **extra))
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    store.put(
        "advisor_policy",
        _policy("pol-reason", "auto_apply_above_threshold", require_override_reason=True),
    )
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    assert (rec.decision.status, rec.decision.reason_text, rec.recommendation.unusable) == (
        DecisionStatus.fallback_rules,
        why,
        why,
    )
    with pytest.raises(NotAllowed) as refused:
        await h.orch.approve_recommendation(rec.id, "rina")
    assert str(refused.value) == f"nothing to approve: {why}"
    with pytest.raises(Unprocessable):
        await h.orch.override_recommendation(rec.id, "warm", None, "rina")
    reason = "The owner chose warm after the review"
    overridden = await h.orch.override_recommendation(rec.id, "warm", reason, "rina")
    assert overridden.decision.status == DecisionStatus.overridden


async def test_a_usable_answer_records_nothing_unusable(tmp_path, store):
    """SDD §4.2: a recommendation whose answer could be applied records `unusable` null."""
    advisor, _ = _answering_advisor(decide=_decide_answer("warm", 0.99))
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    assert (rec.decision.status, rec.recommendation.unusable) == (DecisionStatus.auto_applied, None)


@pytest.mark.parametrize("stored", ["absent", "null", "absent-without-a-why"])
async def test_a_fallback_stored_before_unusable_is_never_approved(tmp_path, store, stored):
    """SDD §4.2, §14.6 (review RV50-07): v0.1.0-rc.2 stored a recommendation without `unusable`;
    its fallback decision recorded why the answer could not be applied, which nothing read - the
    approve ran Jev's escalated option. Such a record reads that why as `unusable`: no approve
    takes it, an override to that option stays a person's call, and the record keeps the why."""
    import re

    import sqlalchemy as sa

    from seamless_migrate.store import documents

    advisor, _ = _answering_advisor(decide=_decide_answer("warm", 0.9, status="escalate"))
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    why = "the advisor's answer was escalate"
    data = rec.model_dump(mode="json")
    if stored == "null":
        data["recommendation"]["unusable"] = None
    else:
        del data["recommendation"]["unusable"]  # as rc.2 wrote it: no such field yet
    if stored == "absent-without-a-why":
        data["decision"]["reason_text"] = None
        why = "the advisor's answer could not be applied"
    with store.engine.begin() as conn:
        conn.execute(
            sa.update(documents)
            .where(documents.c.kind == "recommendation", documents.c.id == rec.id)
            .values(data=data)
        )
    assert store.get("recommendation", rec.id, AdvisorRecommendation).recommendation.unusable == why
    with pytest.raises(NotAllowed, match=f"^nothing to approve: {re.escape(why)}$"):
        await h.orch.approve_recommendation(rec.id, "rina")
    reason = "The owner chose warm after the review"
    overridden = await h.orch.override_recommendation(rec.id, "warm", reason, "rina")
    assert (overridden.decision.status, overridden.decision.final_option) == (
        DecisionStatus.overridden,
        "warm",
    )
    kept = store.get("recommendation", rec.id, AdvisorRecommendation)
    assert kept.recommendation.unusable == why


async def test_a_held_migration_stays_decidable_after_the_clock_steps_back(tmp_path, store):
    """SDD §14.6 (review RV50-09): a decision acted on the newest recommendation by created_at,
    the wall clock, and not on one the migration's hold did not name. After the clock stepped
    back between two validations (an NTP step, a VM resumed from a snapshot) the hold named the
    second while the first was newer by time: each check refused the other, and the held VM never
    started. The hold names the one to decide, whatever its time."""
    advisor, _ = _answering_advisor(decide=_decide_answer("warm", 0.99))
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    store.put("advisor_policy", _finance(plan))
    await h.orch.validate_plan(plan.id, "alice")
    h.clock.advance(-60)
    await h.orch.validate_plan(plan.id, "alice")
    stale, held = sorted(_recommendations(store), key=lambda r: r.created_at, reverse=True)
    assert (await h.by_vm(plan.id, "vm-1")).advisor_hold == held.id
    assert stale.created_at > held.created_at
    with pytest.raises(NotAllowed, match=rf"^a newer recommendation \({held.id}\) replaced "):
        await h.orch.approve_recommendation(stale.id, "rina")
    approved = await h.orch.approve_recommendation(held.id, "rina")
    assert approved.decision.status == DecisionStatus.approved
    assert (await h.by_vm(plan.id, "vm-1")).advisor_hold is None


async def test_the_recommendation_the_last_validation_stored_decides_after_the_clock_steps_back(
    tmp_path, store
):
    """SDD §14.6 (review RV51-08): without a hold a decision took the newest recommendation by
    created_at. Validation 1 held rec-A for approval; the policy went, the clock stepped back and
    validation 2 stored rec-B auto_applied, clearing the hold - rec-A, which rec-B replaced, was
    approvable and set the strategy back, and rec-B was refused. The migration names the one its
    last validation stored."""
    advisor, _ = _answering_advisor(decide=_decide_answer("warm", 0.99))
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    store.put("advisor_policy", _finance(plan))
    await h.orch.validate_plan(plan.id, "alice")
    [first] = _recommendations(store)
    assert first.decision.status == DecisionStatus.pending_approval
    store.delete("advisor_policy", "pol-finance")
    h.clock.advance(-60)
    await h.orch.validate_plan(plan.id, "alice")
    [second] = [r for r in _recommendations(store) if r.id != first.id]
    m = await h.by_vm(plan.id, "vm-1")
    assert (second.decision.status, m.advisor_hold) == (DecisionStatus.auto_applied, None)
    assert first.created_at > second.created_at  # the clock stepped back
    assert m.last_recommendation == second.id
    with pytest.raises(NotAllowed, match=rf"^a newer recommendation \({second.id}\) replaced "):
        await h.orch.approve_recommendation(first.id, "rina")
    approved = await h.orch.approve_recommendation(second.id, "rina")
    assert approved.decision.status == DecisionStatus.approved


async def test_a_migration_validated_before_the_field_decides_its_newest_by_time(tmp_path, store):
    """SDD §14.6 (review RV51-08): a migration no validation stored a recommendation for since
    last_recommendation came (an earlier version's) decides the newest by created_at, as before."""
    advisor, _ = _answering_advisor(decide=_decide_answer("warm", 0.99))
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    store.put("advisor_policy", _finance(plan))
    await h.orch.validate_plan(plan.id, "alice")
    store.delete("advisor_policy", "pol-finance")
    await h.orch.validate_plan(plan.id, "alice")
    older, newer = sorted(_recommendations(store), key=lambda r: r.created_at)
    m = await h.by_vm(plan.id, "vm-1")
    m.last_recommendation = None  # as an earlier version stored it
    store.put("migration", m)
    with pytest.raises(NotAllowed, match=rf"^a newer recommendation \({newer.id}\) replaced "):
        await h.orch.approve_recommendation(older.id, "rina")
    approved = await h.orch.approve_recommendation(newer.id, "rina")
    assert approved.decision.status == DecisionStatus.approved


async def test_no_decision_once_the_plans_default_strategy_decides(tmp_path, store):
    """SDD §14.6 (audit AD-04): a validation honours the plan's default_strategy as it honours an
    override, recording no recommendation - the earlier one no longer stands. It stayed the newest
    and approvable: an approve set the migration to warm against the plan, which the next
    validation silently set back. Approve and override are refused as for a pinned VM."""
    advisor, _ = _answering_advisor(decide=_decide_answer("warm", 0.99))
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    store.put("advisor_policy", _finance(plan))
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    assert (await h.by_vm(plan.id, "vm-1")).advisor_hold == rec.id
    fresh = store.get("plan", plan.id, Plan)
    fresh.default_strategy, fresh.status = Strategy.cold, PlanStatus.draft
    store.put("plan", fresh)
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    assert (m.strategy, m.advisor_hold, len(_recommendations(store))) == (Strategy.cold, None, 1)
    for decide in (
        lambda: h.orch.approve_recommendation(rec.id, "rina"),
        lambda: h.orch.override_recommendation(rec.id, "warm", "the owner chose warm", "rina"),
    ):
        with pytest.raises(NotAllowed) as refused:
            await decide()
        assert str(refused.value) == (
            "The plan sets the strategy of web-01 (cold): change it in the plan."
        )
    assert (await h.migration(m.id)).strategy == Strategy.cold


async def test_a_persons_decision_names_the_policy_whose_rules_it_follows(tmp_path, store):
    """SDD §14.5, §14.6 (audit AD-06): a person's decision follows the rules its decision recorded
    - pol-strict's, which a later policy does not rewrite - but recorded the policy in force,
    pol-relaxed, as its policy_id, and the reason's 422 named pol-relaxed, which requires none.
    It names the policy whose rules it follows."""
    advisor, _ = _answering_advisor(decide=_decide_answer("warm", 0.99))
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    strict = _policy("pol-strict", "always_require_approval", require_override_reason=True)
    store.put("advisor_policy", strict)
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    store.delete("advisor_policy", "pol-strict")
    relaxed = _policy(
        "pol-relaxed",
        "auto_apply_above_threshold",
        require_override_reason=False,
        require_distinct_approver=False,
    )
    store.put("advisor_policy", relaxed)
    approved = await h.orch.approve_recommendation(rec.id, "rina")
    assert (approved.decision.policy_id, approved.decision.require_override_reason) == (
        "pol-strict",
        True,
    )
    events = [e for e in _audit(store) if e.kind == "decision.approved"]
    assert [e.data["after"]["policy_id"] for e in events] == ["pol-strict"]
    with pytest.raises(Unprocessable) as refused:
        await h.orch.override_recommendation(rec.id, "cold", "too short", "rina")
    assert str(refused.value).startswith("policy pol-strict requires an override reason")


def _rec_events(store, mid: str) -> list[str]:
    return [
        e.kind
        for e in store.events(since_seq=0, limit=1000)
        if e.migration_id == mid and e.kind.startswith(("recommendation.", "decision."))
    ]


@pytest.mark.parametrize(
    ("write", "row"),
    [
        ("UPDATE documents", "migration"),
        ("INSERT INTO events", "migration.phase"),
        ("INSERT INTO documents", "recommendation"),
        ("INSERT INTO events", "recommendation.created"),
        ("INSERT INTO events", "decision.auto_applied"),
    ],
)
async def test_a_store_failure_in_validation_leaves_no_decision_without_its_record_and_events(
    tmp_path, store, refuse_write, write, row
):
    """SDD §5.4, §14.4 (audit AD-05): a validation wrote the migration - the advisor's strategy -,
    then the recommendation and its two events, each on its own: a store failure in between left
    the migration on the advisor's warm with the newest recommendation saying cold, or a decision
    without its event. They are one transaction: a write refused leaves the migration as the
    earlier validation left it and nothing of the new decision, and the next validation records
    it whole."""
    state: dict = {"answer": RuntimeError("jev unreachable")}
    advisor, _ = _answering_advisor(decide=lambda arguments: state["answer"])
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")  # Jev unreachable: the rules keep cold
    m = await h.by_vm(plan.id, "vm-1")
    before = (m.strategy, len(_recommendations(store)), _rec_events(store, m.id))
    assert before == (Strategy.cold, 1, ["recommendation.created", "decision.fallback"])
    state["answer"] = _decide_answer("warm", 0.9)
    refused = refuse_write(store, write, row)
    with pytest.raises(RuntimeError, match="briefly unavailable"):
        await h.orch.validate_plan(plan.id, "alice")
    assert refused == [f"{write} {row}"]
    m = await h.by_vm(plan.id, "vm-1")
    assert (m.strategy, len(_recommendations(store)), _rec_events(store, m.id)) == before
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    newest = max(_recommendations(store), key=lambda r: (r.created_at, r.id))
    assert (m.strategy, newest.decision.status, _rec_events(store, m.id)[2:]) == (
        Strategy.warm,
        DecisionStatus.auto_applied,
        ["recommendation.created", "decision.auto_applied"],
    )


async def test_a_store_failure_in_validation_leaves_no_hold_on_a_missing_recommendation(
    tmp_path, store, refuse_write
):
    """SDD §5.4, §14.6 (audit AD-05, review RV49-06): under an approval-required policy the
    migration was written held by the new recommendation's id before that recommendation was
    stored; when its write failed the hold named a recommendation that did not exist - approve a
    404, the strategy unchangeable, the tick never starting it. A refused write leaves the
    migration as it was."""
    state: dict = {"answer": RuntimeError("jev unreachable")}
    advisor, _ = _answering_advisor(decide=lambda arguments: state["answer"])
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")  # the rules' cold, no hold
    store.put("advisor_policy", _finance(plan))
    state["answer"] = _decide_answer("warm", 0.9)
    refused = refuse_write(store, "INSERT INTO documents", "recommendation")
    with pytest.raises(RuntimeError, match="briefly unavailable"):
        await h.orch.validate_plan(plan.id, "alice")
    assert refused == ["INSERT INTO documents recommendation"]
    m = await h.by_vm(plan.id, "vm-1")
    assert (m.advisor_hold, m.strategy) == (None, Strategy.cold)


class _CancelDuringSearch:
    """agentmemory answering slowly (up to its 5 s timeout) while an operator cancels the VM: the
    cancel takes the migration's lock only (SDD §8)."""

    def __init__(self) -> None:
        self.h = None
        self.plan_id = ""
        self.cancel = False

    async def similar_for(self, option, vm):
        if self.cancel:
            m = await self.h.by_vm(self.plan_id, vm.source_id)
            await self.h.orch.cancel(m.id, "bob", "the VM is retired")
        return []


async def test_a_migration_cancelled_while_validation_searches_takes_no_decision(tmp_path, store):
    """SDD §5.4, §14.6 (audit AD-11): a validation wrote the migration, then searched agentmemory
    and recorded the decision without looking at the migration again - a cancel landing in between
    was followed by recommendation.created and decision.pending_approval for the cancelled
    migration. The search comes before the migration's assessment is written: the validation
    finds it cancelled and leaves it alone."""
    knowledge = _CancelDuringSearch()
    advisor, _ = _answering_advisor(decide=_decide_answer("warm", 0.99))
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor, knowledge=knowledge
    )
    knowledge.h, knowledge.plan_id = h, plan.id
    await h.orch.validate_plan(plan.id, "alice")  # recorded, auto-applied
    store.put("advisor_policy", _finance(plan))
    knowledge.cancel = True
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    assert (m.phase, m.advisor_hold) == (P.cancelled, None)
    assert [r.decision.status for r in _recommendations(store)] == [DecisionStatus.auto_applied]
    assert _rec_events(store, m.id) == ["recommendation.created", "decision.auto_applied"]


async def test_a_phase_change_and_its_event_are_one_transaction(tmp_path, store, refuse_write):
    """SDD §5.1 (audit AD-05): a transition saved the migration, then appended its
    migration.phase event on its own - a failed event write left the phase changed without its
    event, in the audit trail and on the live stream. Both are one transaction: a refused event
    leaves the migration where it was, and the retry writes both."""
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")
    m, v = await h.orch._load((await h.by_vm(plan.id, "vm-1")).id)
    refused = refuse_write(store, "INSERT INTO events", "migration.phase")
    with pytest.raises(RuntimeError, match="briefly unavailable"):
        await h.orch._transition(m, v, P.validating, "re-assessed")
    assert refused == ["INSERT INTO events migration.phase"]
    stored, version = await h.orch._load(m.id)
    assert (stored.phase, version) == (P.ready, v)
    moved, _ = await h.orch._transition(stored, version, P.validating, "re-assessed")
    phases = [
        e.data["to"]
        for e in store.events(since_seq=0, limit=1000)
        if e.kind == "migration.phase" and e.migration_id == m.id
    ]
    assert (moved.phase, phases[-1], phases.count("validating")) == (P.validating, "validating", 2)


async def test_no_decision_on_a_recommendation_the_plan_overrides_and_no_second_approve(
    tmp_path, store
):
    """SDD §14.6 (advisor Phase 1 audit A4, review RV48-03): once the plan sets the VM's strategy,
    the newest recommendation no longer stands - approving it, or overriding it to any option,
    would flip the strategy back against the plan or decide what the plan already did; and an
    approved decision is not approved again (one event per decision status change)."""
    advisor, _ = _tie_advisor("warm", 0.99)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    store.put("advisor_policy", _finance(plan))
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    pinned = store.get("plan", plan.id, Plan)
    pinned.strategy_overrides = {"vm-1": Strategy.cold}
    store.put("plan", pinned)
    for decide in (
        lambda: h.orch.approve_recommendation(rec.id, "rina"),
        lambda: h.orch.override_recommendation(rec.id, "warm", "the owner chose warm", "rina"),
        lambda: h.orch.override_recommendation(rec.id, "cold", "the owner chose cold", "rina"),
    ):
        with pytest.raises(NotAllowed) as refused:
            await decide()
        assert str(refused.value) == (
            "The plan sets the strategy of web-01 (cold): change it in the plan."
        )
    pinned.strategy_overrides = {}
    store.put("plan", pinned)
    approved = await h.orch.approve_recommendation(rec.id, "rina")
    assert approved.decision.status == DecisionStatus.approved
    events = len([e for e in _audit(store) if e.kind == "decision.approved"])
    with pytest.raises(NotAllowed) as again:
        await h.orch.approve_recommendation(rec.id, "rina")
    assert str(again.value) == "The decision is already approved."
    assert len([e for e in _audit(store) if e.kind == "decision.approved"]) == events
    # another option is still a person's call while nothing started (SDD §14.6)
    overridden = await h.orch.override_recommendation(
        rec.id, "cold", "the owner chose cold", "rina"
    )
    assert overridden.decision.status == DecisionStatus.overridden


@pytest.mark.parametrize(
    ("action", "answer", "attempt", "passes"),
    [
        ("override", ("warm", 0.99), UNCUT, 2),
        ("override", ("warm", 0.99), (P.precopy, P.rolling_back, P.rolled_back), 0),
        ("approve", ("storage_handover", 0.5), (P.cutover, P.rolling_back, P.rolled_back), 0),
    ],
    ids=[
        "an override to it after a warm attempt's passes",
        "an override to it after a warm attempt rolled back in its first pass",
        "an approve of its recommendation after a cold rollback that kept the volumes",
    ],
)
@pytest.mark.parametrize("marked", [True, False], ids=["marked by the Retry", "stored before"])
async def test_an_advisor_decision_takes_no_handover_while_an_earlier_attempt_holds_volumes(
    tmp_path, store, action, answer, attempt, passes, marked
):
    """SDD §14.6, §9.2 (review RV41B-01): approve and override judge eligible at the request, as
    PUT …/strategy does (RV40-01). After an attempt whose record keeps its destination volumes - a
    warm one's passes, a first pass cut off after creating them, a cold rollback that could not
    delete them - and a Retry, the stored estimates still call storage_handover eligible: an
    override to it is refused (422), an approve of a recommendation of it (the advisor's
    below-threshold answer, the rules having chosen cold) too (409), each naming the held volumes,
    and neither the strategy nor the plan changes - a paused plan's Start would have run the
    handover over them. Another option is still a person's call. The Retry marks the stored
    estimate ineligible (review 41b's lead); one stored before - an upgraded deployment's - is
    judged at the request all the same."""
    from seamless_migrate.executors.ansible import run_dir
    from seamless_migrate.planning.selector import HELD_VOLUMES_REASON

    advisor, _ = _tie_advisor(*answer)
    handover = {"enabled": True, "backend_map": {"ceph-ssd": "rhoso@ceph#rbd"}}
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1, size_gb=4, used_gb=1.0)],
        {"handover": handover, "auto_cutover": False},
        advisor=advisor,
    )
    try:
        await h.orch.validate_plan(plan.id, "alice")
        [rec] = _recommendations(store)
        first = await h.by_vm(plan.id, "vm-1")
        assert (rec.recommendation.option, first.strategy) == (
            answer[0],
            Strategy.warm if action == "override" else Strategy.cold,
        )
        assert first.estimate_for(Strategy.storage_handover).eligible  # nothing held then
        first.phase = P.rolled_back
        first.phase_history = [*first.phase_history, *_history(*attempt)]
        first.sync_passes = _passes(passes, first.phase_history)
        record = run_dir(h.settings, plan.id, first.id) / "osm" / "workload_warm" / "vm-1.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"dest_volumes": {"vol-1": "dst-vol-1"}}))
        store.put("migration", first)
        paused = await h.orch._plan(plan.id)
        paused.status = PlanStatus.paused
        store.put("plan", paused)
        retried = await h.orch.retry(first.id, "alice")
        marking = retried.estimate_for(Strategy.storage_handover)
        assert (marking.eligible, marking.reasons) == (False, [HELD_VOLUMES_REASON])
        if not marked:  # the estimate as a version before the marking stored it
            retried.estimates = [
                e.model_copy(update={"eligible": True, "reasons": []})
                if e.strategy == Strategy.storage_handover
                else e
                for e in retried.estimates
            ]
            store.put("migration", retried)
        if action == "override":
            with pytest.raises(Unprocessable, match=HELD_VOLUMES_REASON):
                await h.orch.override_recommendation(
                    rec.id, "storage_handover", "the owner wants the handover", "rina"
                )
        else:
            with pytest.raises(NotAllowed, match=HELD_VOLUMES_REASON):
                await h.orch.approve_recommendation(rec.id, "rina")
        left = await h.migration(first.id)
        assert left.strategy == first.strategy
        assert (await h.plan(plan.id)).strategy_overrides == {}
        assert store.get("recommendation", rec.id, AdvisorRecommendation).decision == rec.decision
        overridden = await h.orch.override_recommendation(
            rec.id, "warm", "the owner chose warm", "rina"
        )
        assert overridden.decision.final_option == "warm"
    finally:
        await h.orch.stop()


async def test_a_pending_source_snapshot_alone_leaves_a_handover_to_an_advisor_override(
    tmp_path, store
):
    """SDD §14.6, §9.2 (review RV41B-03): what holds a handover out is destination volumes an
    earlier attempt created - a pending source snapshot alone holds none, so an override to the
    handover stands after a Retry."""
    from seamless_migrate.executors.ansible import run_dir

    advisor, _ = _tie_advisor("warm", 0.99)
    handover = {"enabled": True, "backend_map": {"ceph-ssd": "rhoso@ceph#rbd"}}
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1, size_gb=4, used_gb=1.0)],
        {"handover": handover, "auto_cutover": False},
        advisor=advisor,
    )
    try:
        await h.orch.validate_plan(plan.id, "alice")
        [rec] = _recommendations(store)
        first = await h.by_vm(plan.id, "vm-1")
        first.phase = P.rolled_back
        first.phase_history = [*first.phase_history, *_history(*UNCUT)]
        record = run_dir(h.settings, plan.id, first.id) / "osm" / "workload_warm" / "vm-1.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"dest_volumes": {}, "pending_snapshot": "snap-1"}))
        store.put("migration", first)
        paused = await h.orch._plan(plan.id)
        paused.status = PlanStatus.paused
        store.put("plan", paused)
        await h.orch.retry(first.id, "alice")
        assert await h.orch.copied_data(first) is True  # the snapshot is copied data, no volume
        overridden = await h.orch.override_recommendation(
            rec.id, "storage_handover", "the owner wants the handover", "rina"
        )
        assert overridden.decision.final_option == "storage_handover"
    finally:
        await h.orch.stop()


async def test_a_decision_keeps_the_rules_of_the_policy_that_took_it(tmp_path, store):
    """SDD §14.6 (advisor Phase 1 audit, A3): a policy change applies from the next validation and
    never rewrites a decision - switching maker-checker or the override reason off afterwards lets
    nobody decide what the policy put to another person, also after a first person's decision."""
    advisor, _ = _tie_advisor("warm", 0.99)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    finance = _finance(plan)
    store.put("advisor_policy", finance)
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    assert (rec.requested_by, rec.decision.require_distinct_approver) == ("alice", True)
    assert rec.decision.require_override_reason is True
    relaxed = finance.model_copy(
        update={"require_distinct_approver": False, "require_override_reason": False}
    )
    store.put("advisor_policy", relaxed)
    with pytest.raises(NotAllowed) as own:
        await h.orch.approve_recommendation(rec.id, "alice")
    assert str(own.value) == OWN_DECISION
    with pytest.raises(Unprocessable, match="requires an override reason"):
        await h.orch.override_recommendation(rec.id, "cold", None, "rina")
    approved = await h.orch.approve_recommendation(rec.id, "rina")
    # the rules go on with the person's decision
    assert approved.decision.require_distinct_approver is True
    with pytest.raises(NotAllowed) as again:
        await h.orch.override_recommendation(rec.id, "cold", "the owner chose cold", "alice")
    assert str(again.value) == OWN_DECISION


async def test_a_decision_that_recorded_no_rules_follows_the_policy_in_force(tmp_path, store):
    """SDD §14.6: a decision stored before it recorded its policy's rules (or the fallback's)
    follows the policy in force - strict while it is strict, relaxed once it is relaxed."""
    advisor, _ = _tie_advisor("warm", 0.99)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    finance = _finance(plan)
    store.put("advisor_policy", finance)
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    older = rec.decision.model_copy(
        update={"require_distinct_approver": None, "require_override_reason": None}
    )
    store.put("recommendation", rec.model_copy(update={"decision": older}))
    with pytest.raises(NotAllowed) as own:
        await h.orch.approve_recommendation(rec.id, "alice")
    assert str(own.value) == OWN_DECISION
    with pytest.raises(Unprocessable, match="requires an override reason"):
        await h.orch.override_recommendation(rec.id, "cold", None, "rina")
    store.put(
        "advisor_policy",
        finance.model_copy(
            update={"require_distinct_approver": False, "require_override_reason": False}
        ),
    )
    overridden = await h.orch.override_recommendation(rec.id, "cold", None, "alice")
    assert overridden.decision.status == DecisionStatus.overridden


async def test_a_person_may_still_approve_an_auto_applied_decision(tmp_path, store):
    """SDD §14.6: approve acts on the newest recommendation of an unstarted migration whatever its
    decision - only an approved one is not approved again."""
    advisor, _ = _tie_advisor("warm", 0.99)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    assert rec.decision.status == DecisionStatus.auto_applied
    approved = await h.orch.approve_recommendation(rec.id, "rina")
    assert approved.decision.status == DecisionStatus.approved


async def test_an_override_reaches_the_plan_before_a_waiting_validation_reads_it(tmp_path, store):
    """SDD §14.6: the override is written into the plan under the plan's lock, so a validation
    that waited for the lock honours it - it neither asks the advisor again nor holds the
    migration on a new recommendation no one could decide (the plan pins the VM)."""
    advisor, _ = _tie_advisor("warm", 0.99)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    store.put("advisor_policy", _finance(plan))
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    put = h.orch.db.put

    async def slow_plan_put(kind, model, expected_version=None):
        if kind == "plan":
            await asyncio.sleep(0.1)  # the override's plan write takes a moment
        return await put(kind, model, expected_version)

    h.orch.db.put = slow_plan_put
    overriding = asyncio.create_task(
        h.orch.override_recommendation(rec.id, "cold", "the owner chose cold", "rina")
    )
    while not h.orch.plan_lock(plan.id).locked():  # the override holds the plan's lock
        await asyncio.sleep(0.001)
    validating = asyncio.create_task(h.orch.validate_plan(plan.id, "bob"))
    await overriding
    await validating
    m = await h.by_vm(plan.id, "vm-1")
    assert (m.advisor_hold, m.strategy) == (None, Strategy.cold)
    assert (await h.plan(plan.id)).strategy_overrides == {"vm-1": Strategy.cold}
    assert [r.id for r in _recommendations(store)] == [rec.id]


async def test_a_cancel_clears_the_advisor_hold(tmp_path, store):
    """SDD §14.6 (advisor Phase 1 audit, A5): a cancelled migration takes no decision - the cancel
    action and a validation's cancel of a VM that left the plan both clear the hold, or Pending
    approvals would list a decision nobody can take."""
    advisor, _ = _tie_advisor("warm", 0.99)
    vms = [vm(1, size_gb=4, used_gb=1.0), vm(2, size_gb=4, used_gb=1.0)]
    h, plan = await setup(tmp_path, store, vms, NO_GATE, advisor=advisor)
    store.put("advisor_policy", _finance(plan))
    await h.orch.validate_plan(plan.id, "alice")
    one, two = await h.by_vm(plan.id, "vm-1"), await h.by_vm(plan.id, "vm-2")
    assert one.advisor_hold is not None and two.advisor_hold is not None
    cancelled = await h.orch.cancel(one.id, "bayu", "not this one")
    assert (cancelled.phase, cancelled.advisor_hold) == (P.cancelled, None)
    assert (await h.migration(one.id)).advisor_hold is None
    edited = store.get("plan", plan.id, Plan)
    edited.vm_ids = []  # both VMs leave the plan: the validation cancels vm-2's migration
    store.put("plan", edited)
    await h.orch.validate_plan(plan.id, "alice")
    removed = await h.migration(two.id)
    assert (removed.phase, removed.advisor_hold) == (P.cancelled, None)


async def test_after_an_override_the_engine_runs_the_chosen_option(tmp_path, store):
    """ADV-04 scenario 4: override to cold with a reason; the engine runs cold, the plan keeps
    the choice and the audit trail records who overrode what."""
    advisor, _ = _tie_advisor("warm", 0.9)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    store.put("advisor_policy", _finance(plan))
    await run_plan(h, plan)
    [rec] = _recommendations(store)
    with pytest.raises(Unprocessable, match="reason"):
        await h.orch.override_recommendation(rec.id, "cold", "too short", "rina")
    with pytest.raises(Unprocessable, match="not an eligible option"):
        await h.orch.override_recommendation(
            rec.id, "storage_handover", "No shared Ceph between the clouds", "rina"
        )
    overridden = await h.orch.override_recommendation(
        rec.id, "cold", "The change window fits a cold cutover", "rina"
    )
    assert (overridden.decision.status, overridden.decision.final_option) == (
        DecisionStatus.overridden,
        "cold",
    )
    assert overridden.decision.reason_text == "The change window fits a cold cutover"
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.cutover, P.verifying, P.completed)
    assert m.strategy == Strategy.cold and m.advisor_hold is None
    assert (await h.plan(plan.id)).strategy_overrides == {"vm-1": Strategy.cold}
    assert _audit(store)[-1].kind == "decision.overridden"


async def test_approve_and_override_act_only_on_a_waiting_choice_that_can_still_change(
    tmp_path, store
):
    """SDD §14.6: the migration's newest recommendation, while it is pending, blocked or ready;
    PUT …/strategy cannot bypass a hold."""
    advisor, _ = _tie_advisor("warm", 0.9)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    store.put("advisor_policy", _finance(plan))
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    with pytest.raises(NotAllowed, match="waits for approval"):
        await h.orch.set_strategy(m.id, Strategy.warm, "bayu")
    await h.orch.validate_plan(plan.id, "alice")  # a newer recommendation replaces the hold
    older, newer = sorted(_recommendations(store), key=lambda r: r.created_at)
    assert (await h.migration(m.id)).advisor_hold == newer.id
    with pytest.raises(NotAllowed, match="newer"):
        await h.orch.approve_recommendation(older.id, "rina")
    store.put("migration", (await h.migration(m.id)).model_copy(update={"phase": P.precopy}))
    with pytest.raises(NotAllowed, match="precopy"):
        await h.orch.approve_recommendation(newer.id, "rina")


async def test_a_validation_without_the_policy_lets_the_migration_go(tmp_path, store):
    """A policy change applies from the next validation (SDD §14.6): the hold goes with it."""
    advisor, _ = _tie_advisor("warm", 0.9)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    store.put("advisor_policy", _finance(plan))
    await h.orch.validate_plan(plan.id, "alice")
    store.delete("advisor_policy", "pol-finance")
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    assert (m.advisor_hold, m.strategy) == (None, Strategy.warm)


async def test_a_revalidation_asks_and_records_nothing_for_a_migration_it_leaves_alone(
    tmp_path, store
):
    """SDD §14.4: validation makes no choice for a migration already in flight, so the advisor is
    not asked about it and no recommendation claims a strategy the engine is not running."""
    advisor, session = _tie_advisor("warm", 0.9)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    store.put("migration", m.model_copy(update={"phase": P.precopy}))
    asked = len(session.calls)

    await h.orch.validate_plan(plan.id, "alice")
    assert len(session.calls) == asked
    assert [r.migration_id for r in _recommendations(store)] == [m.id]
    assert (await h.migration(m.id)).phase == P.precopy


async def test_a_migration_that_goes_in_flight_during_validation_gets_no_recommendation(
    tmp_path, store
):
    """SDD §14.4: a migration that left ready while the advisor answered is left alone by the
    validation, so its decision is not recorded either."""
    advisor, _ = _tie_advisor("warm", 0.9)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    apply = h.orch._apply_validation

    async def started_meanwhile(mid, *args, **kwargs):
        store.put("migration", (await h.migration(mid)).model_copy(update={"phase": P.precopy}))
        return await apply(mid, *args, **kwargs)

    h.orch._apply_validation = started_meanwhile
    await h.orch.validate_plan(plan.id, "alice")
    assert [r.migration_id for r in _recommendations(store)] == [m.id]
    assert (await h.migration(m.id)).strategy == Strategy.warm


@pytest.mark.parametrize(
    ("confidence", "status", "first_step"),
    [
        (0.57, DecisionStatus.below_threshold, StepName.CUTOVER),
        (0.9, DecisionStatus.auto_applied, StepName.PRECOPY),
    ],
)
async def test_the_engine_runs_the_decision_not_the_recommendation(
    tmp_path, store, confidence, status, first_step
):
    """SDD §14.4 (ADV-01): Jev recommends warm either way; at 0.57 against the 0.6 threshold the
    decision keeps the deterministic cold and the migration runs cold (no pre-copy)."""
    advisor, _ = _tie_advisor("warm", confidence)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    done = await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    [rec] = _recommendations(store)
    assert rec.recommendation.option == "warm" and rec.decision.status == status
    assert done.strategy == Strategy(rec.decision.final_option)
    assert [step for m_id, step in h.executor.calls if m_id == mid][0] == first_step


async def test_with_jev_off_an_automatic_choice_is_recorded_as_a_rules_fallback(tmp_path, store):
    """SDD §14.4: Jev off is no outage of the migration - the rules decide, and it is recorded."""
    settings = make_settings(tmp_path)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1, size_gb=4, used_gb=1.0)],
        advisor=Advisor(None, settings),
        settings=settings,
    )
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    [rec] = _recommendations(store)
    assert (rec.model.name, rec.recommendation.option) == ("rules", None)
    assert (rec.decision.status, rec.decision.reason_code) == (
        DecisionStatus.fallback_rules,
        ReasonCode.ADVISOR_UNAVAILABLE_FALLBACK,
    )
    assert (rec.decision.actor.kind, rec.decision.actor.id) == ("system", "fallback")
    assert m.strategy == Strategy(rec.decision.final_option) == Strategy.cold
    # SDD §14.5: the fallback is audited too, and the input the rules decided on is kept
    [created, decided] = _audit(store)
    assert (created.data["model_version"], created.data["option"]) == ("rules", None)
    assert (decided.kind, decided.actor) == ("decision.fallback", "system:fallback")
    assert decided.data["after"]["status"] == "fallback_rules"
    # and why: the advisor's answer could not be used (SDD §14.5)
    assert decided.data["after"]["reason_text"] == rec.decision.reason_text
    assert rec.decision.reason_text and rec.decision.reason_text.startswith("advisor unavailable")
    snapshot = store.get("advisor_snapshot", rec.input_snapshot_sha256, AdvisorSnapshot)
    assert "vCPU" in snapshot.payload["arguments"]["evidence"]


async def test_validation_keeps_the_notes_a_migration_already_has(tmp_path, store):
    """SDD §14.4: strategy notes of earlier versions stay (they are listed as legacy)."""
    advisor, _ = _tie_advisor("warm", 0.9)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    old = AdvisorNote(kind="strategy", source="jev", summary="Jev recommends warm (0.70); applied")
    m.advisor_notes = [old]
    store.put("migration", m)
    await h.orch.validate_plan(plan.id, "alice")
    again = await h.migration(m.id)
    assert again.advisor_notes == [old]
    assert len(_recommendations(store)) == 2, "each validation adds a recommendation"


ONE_INCIDENT = [
    {"title": "Attach timeout on conv host", "content": "Raise os_migrate_timeout", "score": 0.9}
]


async def _fail_with_memory(tmp_path, store, seen: list[str], results: list[dict]):
    """A warm migration whose pre-copy fails, with agentmemory answering ``results`` to the
    similar-incident search."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("smart-search"):
            return httpx.Response(200, json={"results": results})
        return httpx.Response(201, json={"success": True})

    memory = MemoryClient("http://m", None, "p", transport=httpx.MockTransport(handler))
    settings = make_settings(tmp_path)

    async def failing(ctx):
        raise PermanentStepError("snapshot creation timed out")

    executor = ScriptedExecutor(settings, hooks={StepName.PRECOPY: failing})
    h = build_harness(tmp_path, store, [vm(1)], executor=executor, settings=settings)
    h.orch.knowledge = KnowledgeService(memory, store, h.bus)
    plan = plan_for([vm(1)], default_strategy=Strategy.warm)
    store.put("plan", plan)
    await run_plan(h, plan)
    return h, plan


async def test_each_tick_has_the_streams_send_what_another_process_wrote(tmp_path, store):
    """SDD §12: the CLI beside the running server writes an event to the store - no event of the
    server's own follows it, yet within about a tick it reaches the open streams."""
    h = build_harness(tmp_path, store, [vm(1)])
    await h.orch.start()
    agen = h.bus.subscribe(since=store.max_seq())
    waiting = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0.05)  # the stream has read the store and waits for live events
    try:
        cli = await emit(store, EventBus(store), "plan.updated", "from the CLI", actor="cli")
        got = await asyncio.wait_for(waiting, 2)
        assert (got.seq, got.message, got.actor) == (cli.seq, "from the CLI", "cli")
    finally:
        waiting.cancel()
        await agen.aclose()
        await h.orch.stop()


async def test_similar_incidents_attached_on_failure(tmp_path, store):
    seen = []
    h, plan = await _fail_with_memory(tmp_path, store, seen, ONE_INCIDENT)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.failed)
    for _ in range(100):
        m = await h.migration(m.id)
        if any(n.kind == "similar_incidents" for n in m.advisor_notes):
            break
        await asyncio.sleep(0.01)
    # the note is saved before its event (SDD §14.3): stopping on the note alone would cancel it
    await h.wait_kind("advisor.similar_incidents")
    await h.orch.stop()
    [note] = [n for n in m.advisor_notes if n.kind == "similar_incidents"]
    assert note.source == "memory" and note.data["hits"][0]["title"].startswith("Attach")
    assert "advisor.similar_incidents" in h.kinds()
    assert seen.count("/agentmemory/remember") == 1


async def test_the_similar_incidents_event_comes_after_the_note_is_saved(
    tmp_path, store, monkeypatch
):
    """SDD §14.3: advisor.similar_incidents asks for the notes to be read again (§16), so it is
    written once the note is on the migration - never while agentmemory is still asked."""
    held: list[bool] = []
    append = store.append_event

    def recording(event: Event) -> Event:
        if event.kind == "advisor.similar_incidents":
            m = store.get("migration", event.migration_id, Migration)
            held.append(any(n.kind == "similar_incidents" for n in m.advisor_notes))
        return append(event)

    monkeypatch.setattr(store, "append_event", recording)
    h, plan = await _fail_with_memory(tmp_path, store, [], ONE_INCIDENT)
    await h.wait_kind("advisor.similar_incidents")
    await h.orch.stop()
    assert held == [True]
    [event] = [e for e in store.events(since_seq=0) if e.kind == "advisor.similar_incidents"]
    m = await h.migration(event.migration_id)
    [note] = [n for n in m.advisor_notes if n.kind == "similar_incidents"]
    assert (event.plan_id, event.actor) == (plan.id, "system")
    assert (event.message, event.data) == (note.summary, note.data)
    assert event.data["hits"][0]["title"] == "Attach timeout on conv host"


async def test_a_failure_with_no_similar_incident_announces_none(tmp_path, store):
    """SDD §14.3: agentmemory knows nothing like the failure - no note and no event announcing one,
    and the failure is still remembered as a lesson."""
    seen: list[str] = []
    h, plan = await _fail_with_memory(tmp_path, store, seen, [])
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await h.wait_phase(mid, P.failed)
    deadline = asyncio.get_running_loop().time() + 10
    # the driver leaves once the failure is handled, the lookup once agentmemory answered (§8)
    while mid in h.orch._drivers or h.orch._lookups:
        assert asyncio.get_running_loop().time() < deadline, (
            "the failure was not handled to its end"
        )
        await asyncio.sleep(0.01)
    await h.orch.stop()
    assert mid not in h.orch._driver_crashes
    m = await h.migration(mid)
    assert not any(n.kind == "similar_incidents" for n in m.advisor_notes)
    assert "advisor.similar_incidents" not in h.kinds()
    assert seen.count("/agentmemory/remember") == 1 and "memory.lesson_saved" in h.kinds()


async def test_verification_review_flags_without_flipping(tmp_path, store):
    jev_settings = Settings(jev_mode="stdio")
    contradicted = load_fixture("verify")
    contradicted["results"][1].update(verdict="contradicted", action="auto")
    factory, _ = session_factory(
        fixture_responder(
            {"screen": {"recommendation": {"action": "pass"}}, "verify": contradicted}
        )
    )
    advisor = Advisor(JevClient(jev_settings, session_factory=factory), jev_settings)
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold}, advisor=advisor
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert m.review_required is True and "No kernel panic" in m.review_reason
    assert "advisor.verification" in h.kinds()


async def test_prestage_runs_even_if_a_vm_was_cancelled_before_start(tmp_path, store):
    gpu = vm(3, "gpu-01", flavor_extra_specs={"resources:VGPU": "1"})
    h, plan = await setup(tmp_path, store, [vm(1), gpu], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.cancel((await h.by_vm(plan.id, "vm-3")).id, "alice", "out of scope")
    await h.orch.start_plan(plan.id, "alice")
    await h.orch.start()
    await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert h.executor.prestaged == [plan.id]


async def test_prestage_names_the_plans_flavors_rhoso_lacks(tmp_path, store):
    """SDD §7.2: with "flavors" pre-staged the orchestrator passes the flavors of the plan's
    VMs that have no mapping and no same-named destination flavor - the destination flavor
    m1.small and a mapped flavor are never pre-staged over."""
    from seamless_migrate.domain.models import DEFAULT_PRESTAGE, Mappings

    vms = [
        vm(1, flavor="c4.custom"),
        vm(2, flavor="m1.small"),  # exists in RHOSO
        vm(3, flavor="g2.mapped"),  # mapped by the plan
        vm(4, flavor="c4.custom"),
        vm(5, flavor="r8.(big)"),
    ]
    h, plan = await setup(
        tmp_path,
        store,
        vms,
        {
            "default_strategy": Strategy.cold,
            "prestage_resources": [*DEFAULT_PRESTAGE, "flavors"],
            "mappings": Mappings(flavors={"g2.mapped": "m1.small"}),
        },
    )
    seen: list[list[str] | None] = []

    async def prestage(plan, source, destination, *, prestage_flavors=None):
        seen.append(prestage_flavors)
        h.executor.prestaged.append(plan.id)

    h.executor.prestage = prestage
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    await h.orch.start()
    await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert seen == [["c4.custom", "r8.(big)"]]


async def test_validation_blocks_a_vm_whose_keypair_the_destination_user_lacks(tmp_path, store):
    """SDD §9.3: before, such a VM validated and its cutover failed at the destination check,
    right before the source was stopped; now validation blocks it, and pre-staging keypairs the
    source user owns clears the blocker."""
    import dataclasses

    from seamless_migrate.domain.models import DEFAULT_PRESTAGE

    h, plan = await setup(
        tmp_path, store, [vm(1, key_name="ops-key"), vm(2)], {"default_strategy": Strategy.cold}
    )
    src_inventory = h.source.inventory

    async def source_inventory():
        return dataclasses.replace(await src_inventory(), keypairs=["ops-key"])

    h.source.inventory = source_inventory
    await h.orch.validate_plan(plan.id, "alice")
    blocked = await h.by_vm(plan.id, "vm-1")
    assert blocked.phase == P.blocked
    assert [f.code for f in blocked.findings if f.code.endswith("KEYPAIR_MISSING")] == [
        "DST_KEYPAIR_MISSING"
    ]
    assert (await h.by_vm(plan.id, "vm-2")).phase != P.blocked

    current = await h.plan(plan.id)
    current.prestage_resources = [*DEFAULT_PRESTAGE, "keypairs"]
    store.put("plan", current)
    await h.orch.validate_plan(plan.id, "alice")
    cleared = await h.by_vm(plan.id, "vm-1")
    assert cleared.phase != P.blocked
    assert "MAP_KEYPAIR_PRESTAGED" in [f.code for f in cleared.findings]


async def test_validation_blocks_a_vm_whose_port_cold_and_warm_cannot_recreate(tmp_path, store):
    """SDD §9.2/§9.3: a dual-stack port failed os-migrate's Nova port creation after the source was
    stopped; now validation blocks the VM (no eligible strategy) and NET_PORT_IPS says why."""
    from seamless_migrate.domain.models import Nic

    dual = vm(1, nics=[Nic(network="app-net", mtu=1500, fixed_ips=["10.0.0.1", "fd00::1"])])
    h, plan = await setup(tmp_path, store, [dual, vm(2)], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")
    blocked = await h.by_vm(plan.id, "vm-1")
    assert blocked.phase == P.blocked
    assert "NET_PORT_IPS" in [f.code for f in blocked.findings]
    assert "app-net (10.0.0.1, fd00::1)" in (blocked.error or "")
    assert (await h.by_vm(plan.id, "vm-2")).phase != P.blocked


async def test_validation_blocks_a_vm_whose_nic_override_cannot_be_applied(tmp_path, store):
    """SDD §9.3: a NIC override to a network RHOSO lacks is found at validation, before anything
    stops; the VM is blocked with MAP_NIC_OVERRIDE_INVALID, the plan's other VMs are not."""
    from seamless_migrate.domain.models import Nic

    nic = Nic(network="app-net", mac="fa:16:3e:00:00:0a", mtu=1500, fixed_ips=["10.0.0.1"])
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1, nics=[nic]), vm(2)],
        {
            "default_strategy": Strategy.cold,
            "nic_overrides": {"vm-1": {"FA:16:3E:00:00:0A": {"network": "rhoso-missing"}}},
        },
    )
    await h.orch.validate_plan(plan.id, "alice")
    blocked = await h.by_vm(plan.id, "vm-1")
    assert blocked.phase == P.blocked
    [f] = [f for f in blocked.findings if f.code == "MAP_NIC_OVERRIDE_INVALID"]
    assert "fa:16:3e:00:00:0a: RHOSO 18.0 has no network 'rhoso-missing'" in f.message
    assert (await h.by_vm(plan.id, "vm-2")).phase != P.blocked


async def test_prestage_names_the_keypairs_the_destination_user_lacks(tmp_path, store):
    """SDD §7.2: with "keypairs" pre-staged the orchestrator passes the VMs' key_names the
    source credential owns and the destination credential lacks."""
    import dataclasses

    from seamless_migrate.domain.models import DEFAULT_PRESTAGE

    vms = [
        vm(1, key_name="ops-key"),
        vm(2, key_name="shared-key"),
        vm(3),
        vm(4, key_name="ops-key"),
    ]
    h, plan = await setup(
        tmp_path,
        store,
        vms,
        {"default_strategy": Strategy.cold, "prestage_resources": [*DEFAULT_PRESTAGE, "keypairs"]},
    )
    src_inventory, dst_inventory = h.source.inventory, h.destination.inventory

    async def source_inventory():
        return dataclasses.replace(await src_inventory(), keypairs=["ops-key", "shared-key"])

    async def destination_inventory():
        return dataclasses.replace(await dst_inventory(), keypairs=["shared-key"])

    h.source.inventory, h.destination.inventory = source_inventory, destination_inventory
    seen: list[list[str] | None] = []

    async def prestage(plan, source, destination, *, prestage_keypairs=None):
        seen.append(prestage_keypairs)
        h.executor.prestaged.append(plan.id)

    h.executor.prestage = prestage
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    await h.orch.start()
    await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert seen == [["ops-key"]]


def recent_base():
    """A base time within the 900 s keep-warm interval, so keep-warm passes stay idle."""
    from seamless_migrate.domain.models import utcnow

    return utcnow() - timedelta(seconds=600)


def scripted_pass(base, started_offset_s, duration_s, changed, kind, scanned=20 * 2**30):
    """A warm pass with scripted byte counts, timestamped relative to ``base``."""
    from seamless_migrate.domain.models import SyncPass

    async def hook(ctx):
        t0 = base + timedelta(seconds=started_offset_s)
        return StepResult(
            sync_pass=SyncPass(
                number=99,
                kind=kind,
                started_at=t0,
                ended_at=t0 + timedelta(seconds=duration_s),
                bytes_scanned=scanned,
                bytes_changed=changed,
                bytes_transferred=changed,
                duration_s=duration_s,
            )
        )

    return hook


async def test_warm_passes_calibrate_change_rate_scan_rate_and_estimate(tmp_path, store):
    settings = make_settings(tmp_path)
    base = recent_base()
    executor = ScriptedExecutor(
        settings,
        hooks={
            StepName.PRECOPY: scripted_pass(base, 0, 100.0, 10 * 2**30, "full"),
            StepName.SYNC: scripted_pass(base, 200, 50.0, 2**30, "delta"),
        },
    )
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm, "downtime_slo_s": 300, "require_approval": True},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.awaiting_cutover)
    await h.orch.stop()
    assert [p.number for p in m.sync_passes] == [1, 2]  # control-plane numbering
    # c = bytes changed by the delta pass / time between the two snapshots (200 s)
    assert m.vm.change_rate_bps == pytest.approx(2**30 / 200)
    # per-stream scan rate of the delta pass: 20 GiB / 50 s / min(P=4, V=1)
    assert m.observed_scan_bps == pytest.approx(20 * 2**30 / 50)
    # the estimate is recomputed with both calibrated values: scan = 20 GiB / S' = 50 s
    from dataclasses import replace

    from seamless_migrate.planning.estimator import estimate, params_for_plan

    expected = estimate(
        m.vm, Strategy.warm, replace(params_for_plan(plan), scan_bps=m.observed_scan_bps), 300
    )
    assert m.estimate.downtime_s == pytest.approx(expected.downtime_s)
    assert m.estimate.downtime_s == pytest.approx(
        60 + 30 + max(50.0, expected.final_delta_bytes / plan.link_bps) + 60 + 120
    )
    assert m.estimate.eligible is True


async def test_first_pass_alone_does_not_calibrate(tmp_path, store):
    settings = make_settings(tmp_path)
    executor = ScriptedExecutor(
        settings,
        hooks={
            StepName.PRECOPY: scripted_pass(recent_base(), 0, 100.0, 2**20, "full"),
        },
    )
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm, "require_approval": True},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.awaiting_cutover)
    await h.orch.stop()
    assert len(m.sync_passes) == 1
    assert m.observed_scan_bps is None, "pass 1 is link-bound: never a scan calibration"
    assert m.vm.change_rate_bps == 2 * 2**20, "a full pass does not measure the write rate"


async def test_prestage_failure_fails_the_plan(tmp_path, store):
    settings = make_settings(tmp_path)
    executor = ScriptedExecutor(settings)

    async def broken_prestage(plan, source, destination):
        raise RuntimeError("export_networks.yml failed: HTTP 503")

    executor.prestage = broken_prestage
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold},
        executor=executor, settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    failed = await h.wait_plan(plan.id, PlanStatus.failed)
    # the failure is announced after the plan is saved failed: stopping now would cancel it
    await h.wait_kind("plan.updated")
    await h.orch.stop()
    assert failed.status == PlanStatus.failed
    assert (await h.by_vm(plan.id, "vm-1")).phase == P.ready, "no migration started"
    assert "plan.updated" in h.kinds()


async def _hanging_prestage(tmp_path, store):
    """A started plan whose pre-staging hangs until ``release`` is set, then fails."""
    settings = make_settings(tmp_path)
    executor = ScriptedExecutor(settings)
    release = asyncio.Event()

    async def hanging_prestage(plan, source, destination):
        await release.wait()
        raise RuntimeError("deploy_conversion_hosts.yml failed")

    executor.prestage = hanging_prestage
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold},
        executor=executor, settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    deadline = asyncio.get_running_loop().time() + 5
    while plan.id not in h.orch._prestage_tasks:  # the tick launched it; it hangs
        assert asyncio.get_running_loop().time() < deadline, "pre-staging never started"
        await asyncio.sleep(0.01)
    return h, plan, release


async def _prestage_failure_recorded(h):
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        failures = [
            e
            for e in h.store.events(since_seq=0, limit=1000)
            if e.kind == "plan.updated" and "pre-staging failed" in e.message
        ]
        if failures:
            return failures
        assert asyncio.get_running_loop().time() < deadline, "the failure was never recorded"
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("meanwhile", ["paused", "validated", "edited"])
async def test_a_late_prestage_failure_leaves_a_plan_changed_meanwhile_as_it_is(
    tmp_path, store, meanwhile
):
    """SDD §8: only a plan that still runs fails with its pre-staging. One paused, validated
    again or edited meanwhile keeps its status - an edited draft must not become a failed plan
    that start_plan accepts without a validation - and the failure is still recorded."""
    h, plan, release = await _hanging_prestage(tmp_path, store)
    await h.orch.pause_plan(plan.id, "alice")
    expected = PlanStatus.paused
    if meanwhile in ("validated", "edited"):
        await h.orch.validate_plan(plan.id, "alice")
        expected = PlanStatus.validated
    if meanwhile == "edited":
        async with h.orch.plan_lock(plan.id):  # what PATCH /plans/{id} does
            edited = store.get("plan", plan.id, Plan)
            edited.status = PlanStatus.draft
            store.put("plan", edited)
        expected = PlanStatus.draft
    release.set()
    failures = await _prestage_failure_recorded(h)
    await asyncio.sleep(0.05)
    assert (await h.plan(plan.id)).status == expected
    assert len(failures) == 1 and "status" not in failures[0].data
    if meanwhile == "edited":
        with pytest.raises(NotAllowed, match="validate it first"):
            await h.orch.start_plan(plan.id, "alice")
    await h.orch.stop()


async def test_a_prestage_failure_writes_the_status_under_the_plans_lock(tmp_path, store):
    """SDD §8: a pre-staging failure sets the status like the other status writers - under the
    plan's lock - so it never writes between a pause's read and its write."""
    h, plan, release = await _hanging_prestage(tmp_path, store)
    async with h.orch.plan_lock(plan.id):
        release.set()
        await asyncio.sleep(0.1)
        status = (await h.plan(plan.id)).status
        assert status == PlanStatus.running, "the failure waits for the lock"
    await _prestage_failure_recorded(h)
    await h.orch.stop()
    assert (await h.plan(plan.id)).status == PlanStatus.failed


async def test_verification_review_error_is_logged_not_fatal(tmp_path, store, caplog):
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")

    async def exploding_review(vm_ref, result, review_reason=None):
        raise RuntimeError("jev_screen: HTTP 500")

    h.orch.advisor = SimpleNamespace(review_verification=exploding_review)
    await h.orch.start_plan(plan.id, "alice")
    await h.orch.start()
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert m.review_required is False and m.advisor_notes == []
    assert any("verification review failed" in r.message for r in caplog.records)


async def test_rollback_with_new_source_ids_rewrites_the_plan(tmp_path, store):
    """A storage-handover rollback recreates the source VM: the plan, its waves and the
    strategy override follow the new id (SDD §7.3)."""
    settings = make_settings(tmp_path)

    async def failing_cutover(ctx):
        await ctx.mark_downtime_start()
        raise PermanentStepError("manage at the destination failed")

    async def recreating_rollback(ctx):
        old = ctx.migration.vm.source_id
        new_vm = ctx.migration.vm.model_copy(update={"source_id": f"{old}-recreated"})
        return StepResult(details={"source_running": True, "vm": new_vm.model_dump(mode="json")})

    executor = ScriptedExecutor(
        settings, hooks={StepName.CUTOVER: failing_cutover, StepName.ROLLBACK: recreating_rollback}
    )
    probe = AppProbe(kind="tcp", port=5432)
    nic = Nic(network="app-net", mac="fa:16:3e:00:00:0a", mtu=1500, fixed_ips=["10.0.0.1"])
    h, plan = await setup(
        tmp_path, store, [vm(1, nics=[nic]), vm(2)],
        {"default_strategy": Strategy.cold, "strategy_overrides": {"vm-1": Strategy.cold},
         "probe_overrides": {"vm-1": [probe]},
         "nic_overrides": {"vm-1": {"fa:16:3e:00:00:0a": {"fixed_ips": ["10.0.0.9"]}}},
         "acknowledged_findings": {"vm-1": ["SRC_VM_AUTOSCALED"]},
         "network_cutover": {"dns_targets": {"vm-1": {
             "kind": "designate", "zone": "example.com.", "record": "web-01"}}}},
        executor=executor, settings=settings,
    )  # fmt: skip
    await h.orch.auto_waves(plan.id, 10, "alice")  # resets the plan to draft: validate after
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    await h.orch.start()
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.wait_plan(plan.id, PlanStatus.completed)
    await h.orch.stop()
    assert m.vm.source_id == "vm-1-recreated" and m.destination_server_id is None
    updated = await h.plan(plan.id)
    # both VMs rolled back concurrently: neither rewrite may be lost (optimistic plan writes)
    assert sorted(updated.vm_ids) == ["vm-1-recreated", "vm-2-recreated"]
    assert sorted(v for w in updated.waves for v in w.vm_ids) == [
        "vm-1-recreated",
        "vm-2-recreated",
    ]
    assert updated.strategy_overrides == {"vm-1-recreated": Strategy.cold}
    assert updated.probe_overrides == {"vm-1-recreated": [probe]}
    # its NICs keep their MACs (the rollback recreates its ports with them): the overrides follow
    assert list(updated.nic_overrides) == ["vm-1-recreated"]
    # and so does what the plan acknowledges for it (SDD §4.2, §9.3, plan task N2)
    assert updated.acknowledged_findings == {"vm-1-recreated": ["SRC_VM_AUTOSCALED"]}
    # and its DNS target (SDD §7.3, plan task N3)
    assert list(updated.network_cutover.dns_targets) == ["vm-1-recreated"]
    assert (await h.by_vm(plan.id, "vm-1-recreated")).id == m.id
    assert updated.status == PlanStatus.completed


def _recreating_executor(settings):
    async def failing_cutover(ctx):
        await ctx.mark_downtime_start()
        raise PermanentStepError("manage at the destination failed")

    async def recreating_rollback(ctx):
        new_vm = ctx.migration.vm.model_copy(update={"source_id": "vm-1-recreated"})
        return StepResult(details={"source_running": True, "vm": new_vm.model_dump(mode="json")})

    return ScriptedExecutor(
        settings, hooks={StepName.CUTOVER: failing_cutover, StepName.ROLLBACK: recreating_rollback}
    )


async def test_a_recreated_source_vm_is_in_the_plan_before_the_migration_is_rolled_back(
    tmp_path, store
):
    """SDD §7.3: the plan follows the new id before the migration becomes rolled_back - a stop
    in between resumes rolling_back; rolled_back is never resumed, so a later rewrite could be
    lost for good - and in the same write as the migration's new vm (audit OC-06)."""
    settings = make_settings(tmp_path)
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold},
        executor=_recreating_executor(settings), settings=settings,
    )  # fmt: skip
    put_all = store.put_all_with_events
    seen: list[tuple[Phase, str]] = []

    def watching(writes, events):
        written = {kind: model for kind, model, _ in writes}
        if "vm-1-recreated" in getattr(written.get("plan"), "vm_ids", []) and not seen:
            # the migration the write that first names the recreated VM in the plan holds
            seen.append((written["migration"].phase, written["migration"].vm.source_id))
        return put_all(writes, events)

    store.put_all_with_events = watching
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.orch.stop()
    assert seen == [(P.rolling_back, "vm-1-recreated")]
    assert m.vm.source_id == "vm-1-recreated"
    assert (await h.plan(plan.id)).vm_ids == ["vm-1-recreated"]


async def test_a_rollback_resumed_after_its_rewrite_changes_the_plan_no_further(tmp_path, store):
    """SDD §7.3: stopped after the plan was rewritten but before the migration was rolled back,
    the resumed rollback finds the new id already there: no second write, no second event."""
    settings = make_settings(tmp_path)
    h, plan = await setup(
        tmp_path, store, [vm(1)],
        {"default_strategy": Strategy.cold, "vm_ids": ["vm-1-recreated"],
         "strategy_overrides": {"vm-1-recreated": Strategy.cold}},
        executor=_recreating_executor(settings), settings=settings,
    )  # fmt: skip
    _, version = store.get_versioned("plan", plan.id, Plan)
    stopped = make_migration(
        plan_id=plan.id, vm=vm(1), strategy=Strategy.cold, phase=Phase.rolling_back
    )
    store.put("migration", stopped)
    await h.orch.start()
    m = await h.wait_phase(stopped.id, P.rolled_back)
    await h.orch.stop()
    assert m.vm.source_id == "vm-1-recreated"
    rewritten, after = store.get_versioned("plan", plan.id, Plan)
    assert (rewritten.vm_ids, after) == (["vm-1-recreated"], version)
    assert not [
        e for e in store.events(since_seq=0, limit=1000) if "vm_id_changed" in (e.data or {})
    ]


@pytest.mark.parametrize(
    ("write", "kind"),
    [("INSERT INTO events", "plan.updated"), ("UPDATE documents", "migration")],
    ids=["the-plans-event", "the-migration"],
)
async def test_a_recreated_source_and_its_plan_are_written_together(
    tmp_path, store, refuse_write, write, kind
):
    """SDD §7.3, §11 (audit OC-06): the rollback that recreated the source wrote the plan's new id,
    then the migration's new vm, apart: a stop or a store error between them left the plan naming
    a VM no migration named, and a validation meanwhile planned a second migration of it - Retry
    and Start took both, and the tick cut the VM over twice. The plan and the migration are one
    write: refused, neither changes, and the plan keeps one migration of the VM."""
    settings = make_settings(tmp_path)
    executor = _recreating_executor(settings)
    recreating = executor.hooks[StepName.ROLLBACK]
    attempts: list[str] = []
    again, release = asyncio.Event(), asyncio.Event()

    async def rollback(ctx):
        attempts.append(ctx.migration.id)
        if len(attempts) == 1:
            refuse_write(store, write, kind)  # a part of the rewrite's write fails
        else:
            again.set()
            await release.wait()  # the relaunched rollback runs again: minutes for a handover
        return await recreating(ctx)

    executor.hooks[StepName.ROLLBACK] = rollback
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.cold},
        executor=executor,
        settings=settings,
    )
    h.source.vms["vm-1-recreated"] = vm(1).model_copy(update={"source_id": "vm-1-recreated"})
    await run_plan(h, plan)
    await asyncio.wait_for(again.wait(), 10)
    await h.orch.pause_plan(plan.id, "alice")
    await h.orch.validate_plan(plan.id, "alice")  # the paused plan, validated meanwhile
    [m] = await h.migrations(plan.id)
    assert (m.vm.source_id, (await h.plan(plan.id)).vm_ids) == ("vm-1", ["vm-1"])
    release.set()
    done = await h.wait_phase(m.id, P.rolled_back)
    await h.orch.stop()
    assert done.vm.source_id == "vm-1-recreated"
    assert (await h.plan(plan.id)).vm_ids == ["vm-1-recreated"]
    assert len(await h.migrations(plan.id)) == 1


async def test_a_plan_holding_two_migrations_of_one_vm_is_refused(tmp_path, store):
    """SDD §5.4 (audit OC-06): two migrations of one VM would both cut it over - an earlier
    version's rollback could leave a second one beside the first. A validation of such a plan is
    refused, naming the VM and both migrations, and a retry of one while the other names its VM."""
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    first = make_migration(plan_id=plan.id, vm=vm(1), strategy=Strategy.cold, phase=P.ready)
    second = make_migration(plan_id=plan.id, vm=vm(1), strategy=Strategy.cold, phase=P.rolled_back)
    store.put("migration", first)
    store.put("migration", second)
    with pytest.raises(NotAllowed) as refused:
        await h.orch.validate_plan(plan.id, "alice")
    assert "web-01" in str(refused.value)
    assert first.id in str(refused.value) and second.id in str(refused.value)
    with pytest.raises(NotAllowed, match=f"another migration of this plan \\({first.id}\\)"):
        await h.orch.retry(second.id, "sari")
    assert (await h.migration(second.id)).phase == P.rolled_back
    # one cancelled - listed after the other -, the plan goes on with the one left
    await h.orch.cancel(second.id, "sari", "a second migration of web-01")
    report = await h.orch.validate_plan(plan.id, "alice")
    assert [(i.migration_id, i.phase) for i in report.migrations] == [(first.id, P.ready)]


async def test_a_start_refuses_a_plan_holding_two_migrations_of_one_vm(tmp_path, store):
    """SDD §5.4 (review RV51-18): a plan an earlier version validated with two ready migrations of
    one VM - its recreating rollback's second migration beside the first one, retried - was
    started, and the tick cut the VM over twice. The start is refused as the validation is, and
    the tick starts neither of a plan an earlier version left running so - until one is
    cancelled."""
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    first = make_migration(plan_id=plan.id, vm=vm(1), strategy=Strategy.cold, phase=P.ready)
    second = make_migration(plan_id=plan.id, vm=vm(1), strategy=Strategy.cold, phase=P.ready)
    store.put("migration", first)
    store.put("migration", second)
    validated = await h.plan(plan.id)
    validated.status = PlanStatus.validated  # as the earlier version's validation left it
    store.put("plan", validated)
    with pytest.raises(NotAllowed) as refused:
        await h.orch.start_plan(plan.id, "alice")
    assert "web-01" in str(refused.value)
    assert first.id in str(refused.value) and second.id in str(refused.value)
    assert (await h.plan(plan.id)).status == PlanStatus.validated
    running = await h.plan(plan.id)
    running.status = PlanStatus.running  # as the earlier version's start left it
    store.put("plan", running)
    await h.orch.start()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while plan.id not in h.orch._prestage_done:
        assert loop.time() < deadline, "the plan was never pre-staged"
        await asyncio.sleep(0.01)
    for _ in range(30):  # ticks enough to cut both over
        h.orch.wake()
        await asyncio.sleep(0.01)
    assert [mid for mid, step in h.executor.calls if step == StepName.CUTOVER] == []
    await h.orch.cancel(second.id, "sari", "a second migration of web-01")
    await h.wait_phase(first.id, P.completed)
    await h.orch.stop()
    assert [mid for mid, step in h.executor.calls if step == StepName.CUTOVER] == [first.id]


async def test_a_validation_leaves_a_stopped_source_as_it_is(tmp_path, store):
    """SDD §5.4 (audit OC-02): a migration retried after a failed cutover keeps its stopped
    source (§5.2). A validation re-assessed it: the volume the failed export left detached moved
    it to blocked, where rollback, cancel, retry and start are all refused - nothing restarted
    the source. It is left as it is, like one in flight: the plan starts, and the cutover runs
    again."""
    settings = make_settings(tmp_path)
    two_disks = vm(
        1,
        disks=[
            make_disk(id="vol-1", size_gb=20, used_gb=10.0),
            make_disk(id="vol-1b", size_gb=10, used_gb=2.0, bootable=False, device="/dev/vdb"),
        ],
    )
    # the advisor chooses the strategy: a validation that re-assessed the VM would ask it again
    advisor, session = _answering_advisor(decide=_decide_answer("cold", 0.99))
    h, plan = await setup(
        tmp_path,
        store,
        [two_disks],
        {
            "default_strategy": "auto",
            "auto_cutover": False,
            "verification": VerificationConfig(timeout_s=2, auto_rollback=False),
        },
        executor=_stopping_cutover(settings),
        settings=settings,
        advisor=advisor,
    )
    await run_plan(h, plan)
    m = await h.by_vm(plan.id, "vm-1")
    assert m.strategy == Strategy.cold
    await h.orch.request_cutover(m.id, "sari")
    m = await h.wait_phase(m.id, P.failed)
    assert m.downtime_started_at is not None and m.downtime_ended_at is None
    await h.orch.retry(m.id, "sari")  # keeps the open clock (SDD §5.2)
    await h.orch.pause_plan(plan.id, "alice")
    # the failed export left the data volume detached from the stopped source VM (SDD §6.6)
    src = h.source.vms["vm-1"]
    h.source.vms["vm-1"] = src.model_copy(update={"disks": [src.disks[0]]})
    retried = await h.migration(m.id)
    session.calls.clear()
    report = await h.orch.validate_plan(plan.id, "alice")
    left = await h.migration(m.id)
    assert (left.phase, left.findings, left.vm) == (P.ready, retried.findings, retried.vm)
    assert left.phase_history == retried.phase_history  # no validating, no blocked
    # reported as recorded, like one in flight: its VM is not read again, the advisor not asked
    [item] = report.migrations
    assert (item.phase, item.estimates) == (P.ready, retried.estimates)
    assert "SRC_VM_VOLUME_DETACHED" not in {f.code for f in item.findings}
    assert [name for name, *_ in session.calls if name == "jev_decide"] == []
    # its VM is not read again: gone from the source, the plan still validates (SDD §5.4)
    del h.source.vms["vm-1"]
    await h.orch.validate_plan(plan.id, "alice")
    assert (await h.migration(m.id)).phase_history == retried.phase_history
    started = await h.orch.start_plan(plan.id, "alice")
    await h.orch.stop()
    assert started.status == PlanStatus.running


@pytest.mark.parametrize("left_in", [P.blocked, P.validating], ids=["blocked", "validating"])
async def test_a_stopped_source_migration_an_earlier_version_left_blocked_is_re_assessed(
    tmp_path, store, left_in
):
    """SDD §5.4 (review RV51-01): an earlier version's validation re-assessed a migration retried
    after a failed cutover, its source still stopped: the volume the failed export left detached
    moved it to blocked - or an interrupted validation left it in validating. Cancel, rollback,
    retry and start are refused there, so leaving it as it is would keep its source stopped for
    good: once the volume is attached again a validation frees it, and the plan starts."""
    settings = make_settings(tmp_path)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {
            "default_strategy": Strategy.cold,
            "auto_cutover": False,
            "verification": VerificationConfig(timeout_s=2, auto_rollback=False),
        },
        executor=_stopping_cutover(settings),
        settings=settings,
    )
    await run_plan(h, plan)
    m = await h.by_vm(plan.id, "vm-1")
    await h.orch.request_cutover(m.id, "sari")
    m = await h.wait_phase(m.id, P.failed)
    await h.orch.retry(m.id, "sari")  # keeps the open clock (SDD §5.2)
    await h.orch.pause_plan(plan.id, "alice")
    m = await h.migration(m.id)
    assert m.phase == P.ready and m.downtime_started_at and m.downtime_ended_at is None
    # what the earlier version's validation left: blocked by the detached volume, or validating
    m.phase_history.append(
        PhaseChange(from_phase=P.ready, to_phase=P.validating, reason="validation", actor="alice")
    )
    if left_in == P.blocked:
        m.phase_history.append(
            PhaseChange(
                from_phase=P.validating, to_phase=P.blocked, reason="1 blocker", actor="alice"
            )
        )
        detached = Finding(
            code="SRC_VM_VOLUME_DETACHED",
            severity="blocker",
            message="vol-1b is detached from the source VM",
        )
        m.findings = [*m.findings, detached]
    m.phase = left_in
    store.put("migration", m)
    if left_in == P.blocked:
        with pytest.raises(NotAllowed, match="blocked: web-01"):
            await h.orch.start_plan(plan.id, "alice")
    # the volume is attached again: the source VM is as validated
    await h.orch.validate_plan(plan.id, "alice")
    freed = await h.migration(m.id)
    assert freed.phase == P.ready
    assert [c.to_phase for c in freed.phase_history][-2:] == [P.validating, P.ready]
    assert "SRC_VM_VOLUME_DETACHED" not in {f.code for f in freed.findings}
    assert (freed.downtime_started_at, freed.downtime_ended_at) == (m.downtime_started_at, None)
    started = await h.orch.start_plan(plan.id, "alice")
    await h.orch.stop()
    assert started.status == PlanStatus.running


@dataclasses.dataclass
class _PrestageRecorder(ScriptedExecutor):
    """Records the flavors each pre-staging run was asked to create."""

    flavors: list[list[str]] = dataclasses.field(default_factory=list)

    async def prestage(self, plan, source, destination, *, prestage_flavors=None):
        self.flavors.append(list(prestage_flavors or []))


async def test_a_vm_added_after_the_first_start_gets_its_flavor_pre_staged(tmp_path, store):
    """SDD §8, §9.3 (audit OC-05): a plan was pre-staged once per process: a VM added after its
    first start was validated ready on MAP_FLAVOR_PRESTAGED's promise - pre-staging creates the
    flavor - and its flavor was never created, so each pass or cutover of it failed the
    destination check. A plan is pre-staged again for a migration validated since."""
    settings = make_settings(tmp_path)
    executor = _PrestageRecorder(settings)
    big = vm(2, flavor="m1.xlarge")  # no such flavor at the destination, no mapping
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1), big],
        {
            "vm_ids": ["vm-1"],
            "default_strategy": Strategy.cold,
            "auto_cutover": False,
            "prestage_resources": ["networks", "flavors"],
        },
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    deadline = asyncio.get_running_loop().time() + 5
    while not executor.flavors:
        assert asyncio.get_running_loop().time() < deadline, "the plan was never pre-staged"
        await asyncio.sleep(0.01)
    await h.orch.pause_plan(plan.id, "alice")
    edited = await h.plan(plan.id)  # PATCH /plans/{id}: nothing in flight, back to draft
    edited.vm_ids = ["vm-1", "vm-2"]
    edited.status = PlanStatus.draft
    store.put("plan", edited)
    await h.orch.validate_plan(plan.id, "alice")
    added = await h.by_vm(plan.id, "vm-2")
    assert added.phase == P.ready and "MAP_FLAVOR_PRESTAGED" in {f.code for f in added.findings}
    await h.orch.start_plan(plan.id, "alice")
    while len(executor.flavors) < 2:
        assert asyncio.get_running_loop().time() < deadline + 5, executor.flavors
        await asyncio.sleep(0.01)
    await h.orch.stop()
    assert executor.flavors[-1] == ["m1.xlarge"]


@dataclasses.dataclass
class _GatedPrestageRecorder(ScriptedExecutor):
    """Records the flavors each pre-staging was asked for; the first one runs until released."""

    flavors: list[list[str]] = dataclasses.field(default_factory=list)
    started: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    release: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)

    async def prestage(self, plan, source, destination, *, prestage_flavors=None):
        first = not self.flavors
        self.flavors.append(list(prestage_flavors or []))
        if first:
            self.started.set()
            await self.release.wait()


def _flavor_plan():
    """web-01 planned cold, its flavor at the destination; web-02 not yet in the plan, its flavor
    one the destination lacks and no mapping names (MAP_FLAVOR_PRESTAGED when planned)."""
    return (
        [vm(1), vm(2, flavor="m1.xlarge")],
        {
            "vm_ids": ["vm-1"],
            "default_strategy": Strategy.cold,
            "auto_cutover": False,
            "prestage_resources": ["networks", "flavors"],
        },
    )


def _add_web_02(store, plan):
    """What PATCH /plans/{id} (or `seamless plan apply`) does to the paused plan: web-02 added,
    the plan back to draft."""
    edited = store.get("plan", plan.id, Plan)
    edited.vm_ids = ["vm-1", "vm-2"]
    edited.status = PlanStatus.draft
    store.put("plan", edited)


async def test_a_vm_validated_while_the_plan_pre_stages_is_pre_staged_too(tmp_path, store):
    """SDD §8 (review RV51-17): a plan paused, edited, validated and started again while its first
    pre-staging still ran: that pre-staging, which read the VMs before the edit, marked the plan
    pre-staged when it ended, and the added VM's flavor (MAP_FLAVOR_PRESTAGED) was never
    created. A pre-staging covers only the assessments it read: the tick pre-stages again."""
    settings = make_settings(tmp_path)
    executor = _GatedPrestageRecorder(settings)
    vms, plan_kw = _flavor_plan()
    h, plan = await setup(tmp_path, store, vms, plan_kw, executor=executor, settings=settings)
    await run_plan(h, plan)
    await asyncio.wait_for(executor.started.wait(), 10)  # the first pre-staging runs
    await h.orch.pause_plan(plan.id, "alice")
    _add_web_02(store, plan)
    await h.orch.validate_plan(plan.id, "alice")
    added = await h.by_vm(plan.id, "vm-2")
    assert added.phase == P.ready and "MAP_FLAVOR_PRESTAGED" in {f.code for f in added.findings}
    await h.orch.start_plan(plan.id, "alice")
    executor.release.set()  # the first pre-staging ends now
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while ["m1.xlarge"] not in executor.flavors:
        assert loop.time() < deadline, executor.flavors
        h.orch.wake()
        await asyncio.sleep(0.01)
    await h.orch.stop()
    assert executor.flavors == [[], ["m1.xlarge"]]


async def test_a_vm_another_process_validated_is_pre_staged_by_the_servers_tick(tmp_path, store):
    """SDD §8, §15 (review RV51-17): the pre-staged mark was the process's own: `seamless plan
    validate` and `seamless plan start` beside the running server (the CLI opens the store
    directly) added a VM, and the server's tick, which read the plan as pre-staged since its first
    start, never created its flavor. The tick reads the assessments from the store."""
    settings = make_settings(tmp_path)
    executor = _PrestageRecorder(settings)
    vms, plan_kw = _flavor_plan()
    h, plan = await setup(tmp_path, store, vms, plan_kw, executor=executor, settings=settings)
    await run_plan(h, plan)  # the server
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not executor.flavors:
        assert loop.time() < deadline, "the plan was never pre-staged"
        await asyncio.sleep(0.01)
    await h.orch.pause_plan(plan.id, "alice")  # POST /plans/{id}/pause
    _add_web_02(store, plan)
    cli = build_harness(tmp_path, store, vms, settings=settings)  # the CLI's process
    await cli.orch.validate_plan(plan.id, "cli")
    await cli.orch.start_plan(plan.id, "cli")
    while len(executor.flavors) < 2:
        assert loop.time() < deadline + 5, executor.flavors
        h.orch.wake()  # the server's next tick
        await asyncio.sleep(0.01)
    await h.orch.stop()
    assert executor.flavors == [[], ["m1.xlarge"]]


async def test_a_vm_validated_again_with_another_flavor_is_pre_staged_again(tmp_path, store):
    """SDD §8: a migration's assessment is its last move to validating. web-01, resized at the
    source while its plan was paused, is validated again with a flavor the destination lacks
    (MAP_FLAVOR_PRESTAGED): the same migration, assessed anew, is pre-staged again."""
    settings = make_settings(tmp_path)
    executor = _PrestageRecorder(settings)
    vms, plan_kw = _flavor_plan()
    h, plan = await setup(tmp_path, store, vms, plan_kw, executor=executor, settings=settings)
    await run_plan(h, plan)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not executor.flavors:
        assert loop.time() < deadline, "the plan was never pre-staged"
        await asyncio.sleep(0.01)
    await h.orch.pause_plan(plan.id, "alice")
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"flavor": "m1.xlarge"})
    await h.orch.validate_plan(plan.id, "alice")
    resized = await h.by_vm(plan.id, "vm-1")
    assert "MAP_FLAVOR_PRESTAGED" in {f.code for f in resized.findings}
    await h.orch.start_plan(plan.id, "alice")
    while len(executor.flavors) < 2:
        assert loop.time() < deadline + 5, executor.flavors
        h.orch.wake()
        await asyncio.sleep(0.01)
    await h.orch.stop()
    assert executor.flavors == [[], ["m1.xlarge"]]


@dataclasses.dataclass
class _CountingPrestage(ScriptedExecutor):
    """Counts the pre-stagings; each after the first fails, as a destination API blip would."""

    runs: int = 0

    async def prestage(self, plan, source, destination):
        self.runs += 1
        if self.runs > 1:
            raise RuntimeError("destination API briefly unavailable")


async def test_a_plan_paused_and_resumed_is_not_pre_staged_again(tmp_path, store):
    """SDD §8 (review RV51-19): each start pre-staged the whole plan again, so a plan merely paused
    and resumed - nothing added or validated, web-01 completed - held every gate and step until
    the pre-staging (minutes of playbooks) ended, and a pre-staging error failed it. Nothing was
    assessed anew: web-02's requested cutover starts at once, and the plan keeps running."""
    settings = make_settings(tmp_path)
    executor = _CountingPrestage(settings)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1), vm(2)],
        {"default_strategy": Strategy.cold, "auto_cutover": False},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    first = await h.by_vm(plan.id, "vm-1")
    await h.orch.request_cutover(first.id, "sari")
    await h.wait_phase(first.id, P.completed)
    await h.orch.pause_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")  # resumed: nothing was added or changed
    second = await h.by_vm(plan.id, "vm-2")
    await h.orch.request_cutover(second.id, "sari")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while (second.id, StepName.CUTOVER) not in h.executor.calls:
        status = (await h.plan(plan.id)).status
        assert status == PlanStatus.running, f"{status}: a second pre-staging failed the plan"
        assert loop.time() < deadline, "web-02's cutover waits for a second pre-staging"
        h.orch.wake()
        await asyncio.sleep(0.01)
    await h.orch.stop()
    assert executor.runs == 1


@dataclasses.dataclass
class _HostsRecorder(ScriptedExecutor):
    """Records whether each pre-staging was asked to deploy the conversion hosts."""

    hosts: list[bool] = dataclasses.field(default_factory=list)

    async def prestage(self, plan, source, destination, *, deploy_conversion_hosts=None):
        self.hosts.append(bool(deploy_conversion_hosts))


async def test_a_strategy_that_needs_a_conversion_host_pre_stages_the_plan_again(tmp_path, store):
    """SDD §8 (review RV51-19): an assessment holds whether the migration's strategy needs a
    conversion host. A storage handover plan was pre-staged without one; a migration given the cold
    strategy while the plan runs is pre-staged again - the hosts deployed - before it starts."""
    settings = make_settings(tmp_path)
    executor = _HostsRecorder(settings)
    handover = {"enabled": True, "backend_map": {"ceph-ssd": "rhoso@ceph#rbd"}}
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {
            "handover": handover,
            "auto_cutover": False,
            "default_strategy": Strategy.storage_handover,
        },
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not executor.hosts:
        assert loop.time() < deadline, "the plan was never pre-staged"
        await asyncio.sleep(0.01)
    m = await h.by_vm(plan.id, "vm-1")
    assert (m.strategy, executor.hosts) == (Strategy.storage_handover, [False])
    await h.orch.set_strategy(m.id, Strategy.cold, "alice")
    while len(executor.hosts) < 2:
        assert loop.time() < deadline + 5, executor.hosts
        h.orch.wake()
        await asyncio.sleep(0.01)
    await h.orch.stop()
    assert executor.hosts == [False, True]


async def test_a_strategy_change_racing_the_tick_starts_nothing_no_pre_staging_covered(
    tmp_path, store
):
    """SDD §8 (review RV52-O4): the tick chose a storage handover's cutover on its snapshot - its
    window just opened - while PUT .../strategy, holding the plan's lock, made it cold. The step's
    start re-checked the strategy and the gate, not the pre-staging: a cold cutover began that no
    pre-staging had deployed conversion hosts for. It starts nothing, and the tick pre-stages."""
    settings = make_settings(tmp_path)
    executor = _HostsRecorder(settings)
    handover = {"enabled": True, "backend_map": {"ceph-ssd": "rhoso@ceph#rbd"}}
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"handover": handover, "default_strategy": Strategy.storage_handover},
        executor=executor,
        settings=settings,
    )
    now = h.orch.now()
    p = store.get("plan", plan.id, Plan)
    p.cutover_window = CutoverWindow(start=now + timedelta(hours=1), end=now + timedelta(hours=2))
    store.put("plan", p)  # auto_cutover: the gate opens with the window
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    await h.orch.tick()  # pre-stages: no conversion host needed
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not executor.hosts:
        assert loop.time() < deadline, "the plan was never pre-staged"
        await asyncio.sleep(0.01)
    await h.orch.tick()  # the window is closed: nothing starts
    assert executor.hosts == [False] and (await h.migration(m.id)).phase == P.ready
    h.clock.advance(3600 + 60)  # the window opens
    lock = h.orch.plan_lock(plan.id)
    await lock.acquire()  # PUT .../strategy holds the plan's lock ...
    tick = asyncio.create_task(h.orch.tick())  # ... while the tick, on its snapshot, begins
    await asyncio.sleep(0.2)
    _, writes, events = await h.orch._strategy_writes(m.id, Strategy.cold, "alice")
    await put_all_and_emit(store, h.bus, writes, events)  # what set_strategy writes
    lock.release()
    await tick
    after = await h.migration(m.id)
    await h.orch.tick()  # the next tick pre-stages it, the hosts deployed
    deadline = loop.time() + 5
    while len(executor.hosts) < 2:
        assert loop.time() < deadline, f"no second pre-staging: {executor.hosts}"
        await asyncio.sleep(0.01)
    await h.orch.stop()
    assert (after.phase, after.strategy) == (P.ready, Strategy.cold)
    assert executor.hosts == [False, True]


async def test_a_restart_pre_stages_a_running_plan_again_before_its_next_ready_migration(
    tmp_path, store
):
    """SDD §8 (review RV52-O2): what a pre-staging covered lives in the process. After a restart
    nothing counts as pre-staged: web-02's requested cutover follows a pre-staging, and a plan
    paused across a restart is pre-staged again when it next runs with a ready migration."""
    settings = make_settings(tmp_path)
    vms = [vm(1), vm(2), vm(3)]
    h, plan = await setup(
        tmp_path,
        store,
        vms,
        {"default_strategy": Strategy.cold, "auto_cutover": False},
        executor=_PrestageRecorder(settings),
        settings=settings,
    )
    await run_plan(h, plan)
    first = await h.by_vm(plan.id, "vm-1")
    await h.orch.request_cutover(first.id, "sari")
    await h.wait_phase(first.id, P.completed)
    await h.orch.stop()
    assert len(h.executor.flavors) == 1
    restarted = build_harness(
        tmp_path, store, vms, executor=_PrestageRecorder(settings), settings=settings
    )
    await restarted.orch.start()
    second = await restarted.by_vm(plan.id, "vm-2")
    await restarted.orch.request_cutover(second.id, "sari")
    await restarted.wait_phase(second.id, P.completed)
    await restarted.orch.pause_plan(plan.id, "alice")
    await restarted.orch.stop()
    assert len(restarted.executor.flavors) == 1
    again = build_harness(
        tmp_path, store, vms, executor=_PrestageRecorder(settings), settings=settings
    )
    await again.orch.start()
    await again.orch.start_plan(plan.id, "alice")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not again.executor.flavors:
        assert loop.time() < deadline, "web-03's plan was never pre-staged again"
        again.orch.wake()
        await asyncio.sleep(0.01)
    await again.orch.stop()
    assert len(again.executor.flavors) == 1


@dataclasses.dataclass
class _SecondGated(ScriptedExecutor):
    """Records the flavors each pre-staging was asked for; the second one runs until released."""

    flavors: list[list[str]] = dataclasses.field(default_factory=list)
    started: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    release: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)

    async def prestage(self, plan, source, destination, *, prestage_flavors=None):
        self.flavors.append(list(prestage_flavors or []))
        if len(self.flavors) == 2:
            self.started.set()
            await self.release.wait()


async def test_a_restart_during_a_pre_staging_still_pre_stages_what_it_was_to_cover(
    tmp_path, store
):
    """SDD §8 (review RV52-O2): web-02 was added to a plan whose work started and validated
    (MAP_FLAVOR_PRESTAGED); the server restarted while its pre-staging ran. The restart marked
    every migration of a plan whose work started as covered, so web-02 cut over without its
    flavor. After a restart nothing counts as covered: web-02's flavor is pre-staged first."""
    settings = make_settings(tmp_path)
    executor = _SecondGated(settings)
    vms, plan_kw = _flavor_plan()
    h, plan = await setup(tmp_path, store, vms, plan_kw, executor=executor, settings=settings)
    await run_plan(h, plan)
    first = await h.by_vm(plan.id, "vm-1")
    await h.orch.request_cutover(first.id, "sari")
    await h.wait_phase(first.id, P.completed)  # work started
    await h.wait_plan(plan.id, PlanStatus.completed)
    _add_web_02(store, plan)
    await h.orch.validate_plan(plan.id, "alice")
    added = await h.by_vm(plan.id, "vm-2")
    assert "MAP_FLAVOR_PRESTAGED" in {f.code for f in added.findings}
    await h.orch.start_plan(plan.id, "alice")
    await asyncio.wait_for(executor.started.wait(), 5)  # web-02's pre-staging runs ...
    await h.orch.stop()  # ... when the server restarts
    again = build_harness(
        tmp_path, store, vms, executor=_PrestageRecorder(settings), settings=settings
    )
    await again.orch.start()
    await again.orch.request_cutover(added.id, "sari")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while (added.id, StepName.CUTOVER) not in again.executor.calls:
        assert loop.time() < deadline, "web-02 never started"
        again.orch.wake()
        await asyncio.sleep(0.01)
    await again.orch.stop()
    assert ["m1.xlarge"] in again.executor.flavors, "web-02 cut over, its flavor never created"


@dataclasses.dataclass
class _FailingPrestage(ScriptedExecutor):
    """Pre-staging that always fails, counting its runs."""

    runs: int = 0

    async def prestage(self, plan, source, destination, **_):
        self.runs += 1
        raise RuntimeError("destination API briefly unavailable")


async def test_a_plan_resumed_after_a_restart_with_no_ready_migration_is_not_pre_staged(
    tmp_path, store
):
    """SDD §8 (review RV52-O3): a warm plan held overnight - both VMs in awaiting_cutover - was
    paused, the server upgraded, the plan resumed. Nothing was ready, yet the tick pre-staged it,
    and a failing pre-staging failed the plan. A plan with no ready migration is not pre-staged."""
    settings = make_settings(tmp_path)
    vms = [vm(1), vm(2)]
    h, plan = await setup(
        tmp_path,
        store,
        vms,
        {"default_strategy": Strategy.warm, "auto_cutover": False},
        settings=settings,
    )
    await run_plan(h, plan)
    for v in ("vm-1", "vm-2"):
        await h.wait_phase((await h.by_vm(plan.id, v)).id, P.awaiting_cutover)
    await h.orch.pause_plan(plan.id, "alice")
    await h.orch.stop()
    executor = _FailingPrestage(settings)
    again = build_harness(tmp_path, store, vms, executor=executor, settings=settings)
    await again.orch.start()
    await again.orch.start_plan(plan.id, "alice")
    for _ in range(20):
        again.orch.wake()
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)
    status = (await again.plan(plan.id)).status
    await again.orch.stop()
    assert (executor.runs, status) == (0, PlanStatus.running)


@dataclasses.dataclass
class _SlowPrestage(ScriptedExecutor):
    """Pre-staging that runs until released - the playbooks take minutes on a real cloud."""

    started: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    release: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    finished: list[str] = dataclasses.field(default_factory=list)

    async def prestage(self, plan, source, destination):
        self.started.set()
        await self.release.wait()
        self.finished.append(plan.id)


async def _restart_with_vm_1_at_its_gate_and_vm_2_ready(tmp_path, store, executor_cls):
    """A warm vm-1 waiting at its cutover gate and a cold vm-2 in ready; the server restarts with
    ``executor_cls``'s pre-staging, and vm-1's cutover is requested (PR #58 review)."""
    settings = make_settings(tmp_path)
    vms = [vm(1), vm(2)]
    h, plan = await setup(
        tmp_path,
        store,
        vms,
        {
            "default_strategy": Strategy.warm,
            "auto_cutover": False,
            "strategy_overrides": {"vm-2": Strategy.cold},
        },
        settings=settings,
    )
    await run_plan(h, plan)
    first = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.awaiting_cutover)
    assert (await h.by_vm(plan.id, "vm-2")).phase == P.ready
    await h.orch.stop()
    executor = executor_cls(settings)
    again = build_harness(tmp_path, store, vms, executor=executor, settings=settings)
    await again.orch.start()
    await again.orch.request_cutover(first.id, "sari")
    return again, plan, first, executor


async def test_a_migration_waiting_for_its_pre_staging_holds_back_no_other(tmp_path, store):
    """SDD §8 (PR #58 review): after a restart vm-2, ready, waits for a pre-staging, and the tick
    returned before every gate while it ran: vm-1's requested cutover waited for the pre-staging,
    minutes on a real cloud. Only the uncovered migration waits."""
    again, plan, first, executor = await _restart_with_vm_1_at_its_gate_and_vm_2_ready(
        tmp_path, store, _SlowPrestage
    )
    try:
        await asyncio.wait_for(executor.started.wait(), 5)
        done = await again.wait_phase(first.id, P.completed, timeout=10)
        second = await again.by_vm(plan.id, "vm-2")
        assert (done.phase, second.phase, executor.finished) == (P.completed, P.ready, [])
    finally:
        executor.release.set()
        await again.orch.stop()


async def test_a_failing_pre_staging_never_fails_a_plan_with_work_in_flight(tmp_path, store):
    """SDD §8 (PR #58 review): the pre-staging vm-2 waits for after a restart failed - a
    destination API blip - and failed the plan while vm-1 waited at its gate with its cutover
    requested: vm-1 got no cutover and no keep-warm pass until a person started the plan again.
    The plan keeps running, the failure is recorded, and the pre-staging is tried again only
    after 60 s, not at every tick."""
    again, plan, first, executor = await _restart_with_vm_1_at_its_gate_and_vm_2_ready(
        tmp_path, store, _FailingPrestage
    )
    try:
        done = await again.wait_phase(first.id, P.completed, timeout=10)
        for _ in range(20):
            again.orch.wake()
            await asyncio.sleep(0.01)
        status = (await again.plan(plan.id)).status
        second = await again.by_vm(plan.id, "vm-2")
        failures = [
            e.data
            for e in store.events(since_seq=0, limit=1000)
            if e.kind == "plan.updated" and "pre-staging failed" in e.message
        ]
    finally:
        await again.orch.stop()
    assert (done.phase, second.phase, status) == (P.completed, P.ready, PlanStatus.running)
    assert executor.runs == 1 and len(failures) == 1 and "status" not in failures[0], failures


async def test_a_cancelled_plan_pre_stages_nothing(tmp_path, store):
    """SDD §12 (audit OC-07): a plan paused while its pre-staging ran could be cancelled - none of
    its migrations started - and the pre-staging went on creating its networks, flavors and
    conversion hosts, then announced "resources pre-staged" for the cancelled plan. The cancel
    stops it first."""
    settings = make_settings(tmp_path)
    executor = _SlowPrestage(settings)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.cold, "auto_cutover": False},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    await asyncio.wait_for(executor.started.wait(), 10)
    await h.orch.pause_plan(plan.id, "alice")
    cancelled = await h.orch.cancel_plan(plan.id, "alice", "the project was dropped")
    assert cancelled.status == PlanStatus.cancelled
    assert plan.id not in h.orch._prestage_tasks
    executor.release.set()  # were the playbooks still running, they would finish now
    for _ in range(20):
        await asyncio.sleep(0.01)
    announced = [
        e.message
        for e in store.events(since_seq=0, limit=1000)
        if e.kind == "plan.updated" and e.data.get("prestaged")
    ]
    await h.orch.stop()
    assert (executor.finished, announced) == ([], [])


async def test_tick_ignores_migrations_of_finished_plans_and_records_timing(tmp_path, store):
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    await run_plan(h, plan)
    await h.wait_plan(plan.id, PlanStatus.completed)
    await h.orch.stop()
    stats = h.orch.tick_stats
    assert stats["count"] >= 1 and stats["max"] >= stats["last"] >= 0 and stats["sum"] >= 0
    # a stale active-step migration left in the completed plan must not consume the global
    # cutover budget of a new running plan: the tick only loads running/paused plans
    stale = await h.by_vm(plan.id, "vm-1")
    stale.phase = P.cutover
    store.put("migration", stale)
    settings = make_settings(tmp_path, max_concurrent_cutovers=1)
    # the stale cutover, if the tick ever resumed it, hangs forever: a tick that counted it
    # would hold the single cutover slot and plan 2 could never cut over
    gates: dict[str, asyncio.Event] = {}
    started: list[str] = []
    gates["vm-2"] = asyncio.Event()
    gates["vm-2"].set()
    executor = ScriptedExecutor(
        settings, hooks={StepName.CUTOVER: blocking_cutover(gates, started)}
    )
    h2 = build_harness(tmp_path, store, [vm(2)], settings=settings, executor=executor)
    plan2 = plan_for([vm(2)], default_strategy=Strategy.cold)
    store.put("plan", plan2)
    await run_plan(h2, plan2)
    done = await h2.wait_phase((await h2.by_vm(plan2.id, "vm-2")).id, P.completed)
    await h2.orch.stop()
    assert done.phase == P.completed
    # start() resumes the in-flight stale cutover (SDD §8) and it hangs on its gate: plan 2
    # still cut over, so the tick never counted it against the single cutover slot
    assert "vm-2" in started
    assert (await h2.migration(stale.id)).phase == P.cutover


async def test_orchestrator_health_reports_loop_state(tmp_path, store):
    h, plan = await setup(tmp_path, store, [vm(1)])
    before = h.orch.health()
    assert before == {"running": False, "last_tick_age_s": None, "ticks": 0, "healthy": True}
    await h.orch.start()
    await asyncio.sleep(0.05)
    live = h.orch.health()
    assert live["running"] is True and live["healthy"] is True and live["ticks"] >= 1
    assert live["last_tick_age_s"] is not None and live["last_tick_age_s"] >= 0
    await h.orch.stop()
    stopped = h.orch.health()
    assert stopped["running"] is False and stopped["healthy"] is True, "stopped on purpose"
    # a loop that died after start() is unhealthy
    h.orch._started = True
    assert h.orch.health()["healthy"] is False
    h.orch._started = False
    # a loop whose ticks keep raising is unhealthy even though it runs
    await h.orch.start()
    await asyncio.sleep(0.02)
    h.orch._tick_errors = 3
    assert h.orch.health()["healthy"] is False
    h.orch._tick_errors = 0
    assert h.orch.health()["healthy"] is True
    await h.orch.stop()


async def test_executor_supplied_downtime_start_is_kept(tmp_path, store):
    settings = make_settings(tmp_path)
    stopped_at = datetime.now(UTC) - timedelta(seconds=90)

    async def precise_cutover(ctx):
        await ctx.mark_downtime_start(at=stopped_at)
        await ctx.mark_downtime_start()  # later calls never move the clock
        return StepResult(destination_server_id="dst-1")

    executor = ScriptedExecutor(settings, hooks={StepName.CUTOVER: precise_cutover})
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold},
        executor=executor, settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert m.downtime_started_at == stopped_at
    assert m.actual_downtime_s is not None and m.actual_downtime_s >= 90


# -- ADV-17: maker-checker (SDD §5.4, §14.6) --------------------------------------------------

#: the API's answer to a requester approving their own cutover (SDD §5.4)
OWN_CUTOVER = "You requested this cutover. Another approver must approve it."
#: and to the validating user deciding their own advisor decision (SDD §14.6)
OWN_DECISION = "You requested this decision. Another approver must decide it."
#: a warm plan whose cutover waits for a request and an approval
GATED = {"default_strategy": Strategy.warm, "require_approval": True, "auto_cutover": False}


async def test_a_cutover_request_is_not_an_approval_when_a_distinct_approver_is_required(
    tmp_path, store
):
    """ADV-17: the request records who asked and when; only another approver opens the gate."""
    h, plan = await setup(tmp_path, store, [vm(1)], GATED)
    assert plan.require_distinct_approver is True  # the default
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.awaiting_cutover)
    requested = await h.orch.request_cutover(m.id, "sari", comment="Window agreed with the owner")
    assert (requested.cutover_requested, requested.cutover_requested_by) == (True, "sari")
    assert requested.cutover_requested_at is not None and requested.approvals == []
    events = h.store.events(since_seq=0, limit=100000)
    action = [e for e in events if e.kind == "migration.action"][-1]
    assert action.data["comment"] == "Window agreed with the owner"
    await asyncio.sleep(0.1)  # many ticks: a request alone opens no gate
    assert (await h.migration(m.id)).phase == P.awaiting_cutover
    with pytest.raises(NotAllowed) as refused:
        await h.orch.approve(m.id, "sari", "approving my own request")
    assert str(refused.value) == OWN_CUTOVER
    assert (await h.migration(m.id)).approvals == []
    await h.orch.approve(m.id, "rina", "Checked the pre-flight warnings")
    await h.wait_phase(m.id, P.completed)
    await h.orch.stop()


async def test_a_principal_whose_name_holds_a_nul_records_no_request_to_approve_itself(
    tmp_path, store
):
    """SDD §5.4, §11 (review RV48-05): the store stripped a requester's NUL while the approval
    compared the principal's own name, NUL included, so 'ops\\0lead' approved its own cutover
    request. The store keeps no NUL: the request is refused, nothing recorded to approve."""
    h, plan = await setup(tmp_path, store, [vm(1)], GATED)
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.awaiting_cutover)
    with pytest.raises(UnstorableValue, match="holds a NUL character"):
        await h.orch.request_cutover(m.id, "ops\x00lead")
    held = await h.migration(m.id)
    assert (held.cutover_requested, held.cutover_requested_by) == (False, None)
    await h.orch.stop()


async def test_an_approval_the_requester_gave_before_requesting_does_not_count(tmp_path, store):
    h, plan = await setup(tmp_path, store, [vm(1)], GATED)
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.awaiting_cutover)
    await h.orch.approve(m.id, "sari", "Looks good")
    await h.orch.request_cutover(m.id, "sari")
    await asyncio.sleep(0.1)
    assert (await h.migration(m.id)).phase == P.awaiting_cutover, "sari's own approval"
    await h.orch.approve(m.id, "rina", "A second pair of eyes")
    await h.wait_phase(m.id, P.completed)
    await h.orch.stop()


async def test_a_repeated_cutover_request_keeps_the_first_requester(tmp_path, store):
    h, plan = await setup(tmp_path, store, [vm(1)], GATED)
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    first = await h.orch.request_cutover(m.id, "sari")
    again = await h.orch.request_cutover(m.id, "rina", force_window=True)
    assert (again.cutover_requested_by, again.cutover_requested_at) == (
        "sari",
        first.cutover_requested_at,
    )
    assert again.force_window is True and again.approvals == []


async def test_without_a_distinct_approver_a_request_is_an_approval_as_before(tmp_path, store):
    h, plan = await setup(tmp_path, store, [vm(1)], {**GATED, "require_distinct_approver": False})
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.awaiting_cutover)
    out = await h.orch.request_cutover(m.id, "sari", comment="emergency")
    assert out.cutover_requested_by == "sari"
    assert (out.approvals[-1].actor, out.approvals[-1].comment) == ("sari", "emergency")
    await h.wait_phase(m.id, P.completed)
    await h.orch.stop()


async def test_the_gate_counts_approvals_by_anyone_but_the_cutover_requester(tmp_path, store):
    """SDD §5.4 rule 1; data stored before ADV-17 has no requester, so its approvals count."""
    h, plan = await setup(tmp_path, store, [vm(1)], GATED)
    now = h.clock()

    def gate(plan_kw=None, **m_kw) -> bool:
        p = plan.model_copy(update=plan_kw or {})
        m = make_migration(plan_id=plan.id, phase=P.awaiting_cutover, **m_kw)
        return h.orch._gate(m, p, now)

    def approval(actor: str) -> Approval:
        return Approval(actor=actor, at=now, comment=None)

    asked = {"cutover_requested": True, "cutover_requested_by": "sari"}
    assert gate(approvals=[approval("sari")], **asked) is False
    assert gate(approvals=[approval("sari"), approval("rina")], **asked) is True
    assert gate(approvals=[], **asked) is False
    assert gate(approvals=[approval("sari")], cutover_requested=True) is True  # no requester
    off = {"require_distinct_approver": False}
    assert gate(off, approvals=[approval("sari")], **asked) is True
    auto = {"auto_cutover": True}
    assert gate(auto, approvals=[approval("sari")]) is True  # nobody requested


#: the API's answer to the approver who forced a cutover approving it (SDD §5.4)
OWN_FORCE = "You forced this cutover. Another approver must approve it."


async def test_the_approver_who_forced_a_cutover_never_approves_it(tmp_path, store):
    """SDD §5.4 (advisor Phase 1 audit, A2): bob requested the cutover and sari forced it - sari's
    approval neither counts nor is accepted, before or after the force; another approver decides."""
    h, plan = await setup(tmp_path, store, [vm(1)], GATED)
    now = h.clock()
    forced = ForceCutover(
        actor="sari", at=now, reason="the window closes", delta_bytes=0, estimated_downtime_s=60.0
    )

    def gate(*actors: str, distinct: bool = True) -> bool:
        m = make_migration(
            plan_id=plan.id,
            phase=P.awaiting_cutover,
            cutover_requested=True,
            cutover_requested_by="bob",
            force_cutover=forced,
            approvals=[Approval(actor=a, at=now, comment=None) for a in actors],
        )
        return h.orch._gate(m, plan.model_copy(update={"require_distinct_approver": distinct}), now)

    assert gate("sari") is False
    assert gate("bob", "sari") is False
    assert gate("sari", "rina") is True
    assert gate("sari", distinct=False) is True
    syncing = make_migration(plan_id=plan.id, vm=vm(1), strategy=Strategy.warm, phase=P.syncing)
    store.put("migration", syncing)
    await h.orch.approve(syncing.id, "sari", "looked fine before the force")
    await h.orch.request_cutover(syncing.id, "bob")
    await h.orch.force_cutover(syncing.id, "sari", "the window closes in ten minutes")
    with pytest.raises(NotAllowed) as refused:
        await h.orch.approve(syncing.id, "sari", "my own force")
    assert str(refused.value) == OWN_FORCE
    m = await h.migration(syncing.id)
    assert [a.actor for a in m.approvals] == ["sari"]
    assert counted_approvals(m, plan) == []
    approved = await h.orch.approve(syncing.id, "rina", "a second pair of eyes")
    assert [a.actor for a in counted_approvals(approved, plan)] == ["rina"]
    # without maker-checker the forcer may approve, as the requester may (SDD §5.4)
    free = plan.model_copy(update={"id": "plan-0000f4ee", "require_distinct_approver": False})
    store.put("plan", free)
    other = make_migration(plan_id=free.id, vm=vm(2), strategy=Strategy.warm, phase=P.syncing)
    store.put("migration", other)
    await h.orch.request_cutover(other.id, "bob")
    await h.orch.force_cutover(other.id, "sari", "the window closes in ten minutes")
    again = await h.orch.approve(other.id, "sari", "my own force, no maker-checker here")
    assert [a.actor for a in counted_approvals(again, free)] == ["bob", "sari", "sari"]


async def test_a_migration_is_forced_once(tmp_path, store):
    """SDD §5.4: while a Force cutover is recorded, another is refused - the second forcer would
    take the first one's place, whose own approval would then count again, and the first record
    would be lost."""
    h, plan = await setup(tmp_path, store, [vm(1)], GATED)
    syncing = make_migration(plan_id=plan.id, vm=vm(1), strategy=Strategy.warm, phase=P.syncing)
    store.put("migration", syncing)
    await h.orch.approve(syncing.id, "sari", "looked fine before the force")
    await h.orch.force_cutover(syncing.id, "sari", "the window closes in ten minutes")
    with pytest.raises(NotAllowed) as refused:
        await h.orch.force_cutover(syncing.id, "rina", "I would rather force it myself")
    assert str(refused.value) == "Already forced by sari: the cutover follows the running pass."
    m = await h.migration(syncing.id)
    assert (m.force_cutover.actor, m.force_cutover.reason) == (
        "sari",
        "the window closes in ten minutes",
    )
    assert counted_approvals(m, plan) == [], "the forcer's own approval still does not count"
    assert h.kinds().count("migration.force_cutover") == 1


@pytest.mark.parametrize(
    ("action", "kind", "distinct"),
    [
        ("force", "migration.force_cutover", True),
        ("approve", "migration.approved", True),
        ("request", "migration.action", True),
        ("request", "migration.approved", False),
    ],
    ids=["force", "approve", "request", "request-approving"],
)
async def test_an_approval_request_or_force_is_never_kept_without_its_event(
    tmp_path, store, refuse_write, action, kind, distinct
):
    """SDD §5.4, §11 (audit OC-03): a Force cutover was saved, then its audit event written apart -
    refused, the force stood with no record in the trail, and forcing again answered 409, so the
    record could never be written; an approval that opens the gate, and a cutover request,
    lacked their events the same way. Each is one transaction with its events: a store error
    leaves none of them, and the approver's retry is taken anew."""
    h, plan = await setup(
        tmp_path, store, [vm(1)], {**GATED, "require_distinct_approver": distinct}
    )
    syncing = make_migration(plan_id=plan.id, vm=vm(1), strategy=Strategy.warm, phase=P.syncing)
    store.put("migration", syncing)
    act = {
        "force": lambda: h.orch.force_cutover(
            syncing.id, "sari", "the window closes in ten minutes"
        ),
        "approve": lambda: h.orch.approve(syncing.id, "sari", "looked fine"),
        "request": lambda: h.orch.request_cutover(syncing.id, "sari", comment="ready"),
    }[action]
    refused = refuse_write(store, "INSERT INTO events", kind)
    with pytest.raises(RuntimeError):
        await act()
    assert refused, "the event's write was refused"
    kept = await h.migration(syncing.id)
    assert (kept.force_cutover, kept.approvals, kept.cutover_requested) == (None, [], False)
    assert h.kinds() == []
    await act()  # the retry, as a person makes it
    assert h.kinds().count(kind) == 1
    done = await h.migration(syncing.id)
    assert (done.force_cutover is not None) == (action == "force")
    assert done.cutover_requested == (action != "approve")


async def test_a_force_cutover_in_precopy_expects_the_running_first_pass(tmp_path, store):
    """SDD §5.4 (ADV-16..20 audit, F2): in pre-copy no pass has ended - the recorded estimate is
    the downtime after the running first pass, not after a 0 s one."""
    h, plan = await setup(tmp_path, store, [vm(1)], GATED)
    fast = vm(1).model_copy(update={"change_rate_bps": 60 * 2**20})  # the delta sets the copy
    precopy = make_migration(plan_id=plan.id, vm=fast, strategy=Strategy.warm, phase=P.precopy)
    store.put("migration", precopy)
    forced = await h.orch.force_cutover(precopy.id, "sari", "the window closes in ten minutes")
    params = params_for_plan(plan)
    first = first_pass_seconds(fast, Strategy.warm, params)
    recorded = forced.force_cutover.estimated_downtime_s
    assert recorded == pytest.approx(estimate_final_downtime(fast, Strategy.warm, first, params))
    assert recorded > estimate_final_downtime(fast, Strategy.warm, 0.0, params)


@pytest.mark.parametrize("strategy", [Strategy.warm, Strategy.vmware_warm])
async def test_a_force_cutover_after_a_pass_counts_the_pass_whole_time(tmp_path, store, strategy):
    """SDD §5.4, §9.1 (review RV39-07): after a pass ended, the recorded figure is the downtime
    after a pass as long as that one's whole time - a warm pass records the part after its
    snapshot, so its snapshot counts too; a CBT pass records the whole. The figure does not drop
    when the expected first pass ends as expected."""
    h, plan = await setup(tmp_path, store, [vm(1)], GATED)
    fast = vm(1).model_copy(update={"change_rate_bps": 60 * 2**20})  # the delta sets the copy
    params = params_for_plan(plan)
    snapshot = params.snapshot_s if strategy == Strategy.warm else 0.0
    expected_first = first_pass_seconds(fast, strategy, params)
    precopy = make_migration(plan_id=plan.id, vm=fast, strategy=strategy, phase=P.precopy)
    store.put("migration", precopy)
    before = await h.orch.force_cutover(precopy.id, "sari", "the window closes in ten minutes")
    # the same VM whose first pass ended as expected: it records the part after its snapshot
    ended = SyncPass(
        number=1, kind="full", started_at=utcnow(), duration_s=expected_first - snapshot
    )
    syncing = make_migration(
        plan_id=plan.id, vm=fast, strategy=strategy, phase=P.syncing, sync_passes=[ended]
    )
    store.put("migration", syncing)
    after = await h.orch.force_cutover(syncing.id, "sari", "the window closes in ten minutes")
    recorded = after.force_cutover.estimated_downtime_s
    assert recorded == pytest.approx(
        estimate_final_downtime(fast, strategy, ended.duration_s + snapshot, params)
    )
    assert recorded == pytest.approx(before.force_cutover.estimated_downtime_s)


async def test_the_slo_rule_and_the_estimate_it_records_count_the_last_pass_whole_time(
    tmp_path, store
):
    """SDD §5.3, §9.1 (review RV39-07): a warm pass records the part after its snapshot, while the
    guest writes through the snapshot too: the final-downtime estimate after it takes its whole
    time. A fast writer's final delta then outlasts an SLO the recorded part alone would meet, and
    the estimate the rule records is the one after the whole pass."""
    from seamless_migrate.planning.estimator import estimate

    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm})
    fast = vm(1, size_gb=200, used_gb=100.0).model_copy(update={"change_rate_bps": 100 * 2**20})
    params = params_for_plan(plan)
    delta = SyncPass(
        number=2,
        kind="delta",
        started_at=utcnow(),
        duration_s=600.0,
        bytes_scanned=fast.disk_bytes,
        bytes_changed=40 * 2**30,
        bytes_transferred=40 * 2**30,
    )
    whole = estimate_final_downtime(fast, Strategy.warm, 600.0 + params.snapshot_s, params)
    part = estimate_final_downtime(fast, Strategy.warm, 600.0, params)
    assert part < whole
    between = plan.model_copy(update={"downtime_slo_s": int((part + whole) / 2)})
    m = make_migration(
        plan_id=plan.id, vm=fast, strategy=Strategy.warm, phase=P.syncing, sync_passes=[delta]
    )
    m.estimate = estimate(fast, Strategy.warm, params, between.downtime_slo_s)
    try:
        assert h.orch._converged(m, between)[0] != "slo"
        h.orch._converge(m, between, "max_passes")
        assert m.estimate.downtime_s == pytest.approx(whole)
    finally:
        await h.orch.stop()


async def test_a_force_cutover_is_in_the_audit_trail(tmp_path, store):
    """SDD §14.5 (the owner's ADV-16 acceptance): a Force cutover is recorded in the audit trail
    with its approver, its reason and the migration it forced."""
    h, plan = await setup(tmp_path, store, [vm(1)], GATED)
    syncing = make_migration(plan_id=plan.id, vm=vm(1), strategy=Strategy.warm, phase=P.syncing)
    store.put("migration", syncing)
    await h.orch.force_cutover(syncing.id, "sari", "the window closes in ten minutes")
    [event] = [e for e in _audit(store) if e.kind == "migration.force_cutover"]
    assert (event.actor, event.migration_id) == ("sari", syncing.id)
    assert {k: event.data[k] for k in ("target_id", "vm", "reason")} == {
        "target_id": syncing.id,
        "vm": "web-01",
        "reason": "the window closes in ten minutes",
    }


async def test_validation_and_a_strategy_change_clear_the_cutover_requester(tmp_path, store):
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    await h.orch.request_cutover(m.id, "sari")
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.migration(m.id)
    assert (m.cutover_requested, m.cutover_requested_by, m.cutover_requested_at) == (
        False,
        None,
        None,
    )
    await h.orch.request_cutover(m.id, "sari")
    await h.orch.set_strategy(m.id, Strategy.warm, "rina")
    m = await h.migration(m.id)
    assert (m.cutover_requested_by, m.cutover_requested_at) == (None, None)


async def test_a_validation_names_its_user_as_the_requester_of_the_decisions_it_records(
    tmp_path, store
):
    advisor, _ = _tie_advisor("warm", 0.9)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    assert rec.requested_by == "alice"


async def test_the_requester_of_a_decision_neither_approves_nor_overrides_it(tmp_path, store):
    """ADV-17 with the Finance policy (a distinct approver by default): another admin decides."""
    advisor, _ = _tie_advisor("warm", 0.9)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    store.put("advisor_policy", _finance(plan))
    await h.orch.validate_plan(plan.id, "rina")
    [rec] = _recommendations(store)
    for decide in (
        h.orch.approve_recommendation(rec.id, "rina"),
        h.orch.override_recommendation(rec.id, "cold", "The change window fits a cold one", "rina"),
    ):
        with pytest.raises(NotAllowed) as refused:
            await decide
        assert str(refused.value) == OWN_DECISION
    approved = await h.orch.approve_recommendation(rec.id, "sari")
    assert (approved.decision.status, approved.decision.actor.id) == (
        DecisionStatus.approved,
        "sari",
    )


async def test_a_policy_without_a_distinct_approver_lets_the_requester_decide(tmp_path, store):
    advisor, _ = _tie_advisor("warm", 0.9)
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], NO_GATE, advisor=advisor
    )
    store.put(
        "advisor_policy", _finance(plan).model_copy(update={"require_distinct_approver": False})
    )
    await h.orch.validate_plan(plan.id, "rina")
    [rec] = _recommendations(store)
    approved = await h.orch.approve_recommendation(rec.id, "rina")
    assert approved.decision.status == DecisionStatus.approved


# -- ADV-06: where the model runs (SDD §4, §13.4) ---------------------------------------------


async def test_a_recommendation_jev_answered_names_where_the_model_runs(tmp_path, store):
    advisor, _ = _tie_advisor("warm", 0.9)
    settings = make_settings(
        tmp_path, jev_deployment="on_prem", jev_endpoint_host="jev.dc1.example.internal"
    )
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor, settings=settings
    )
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    assert (rec.model.name, rec.model.deployment, rec.model.endpoint_host) == (
        "jev",
        "on_prem",
        "jev.dc1.example.internal",
    )


async def test_a_recommendation_without_jev_names_no_deployment(tmp_path, store):
    """The rules decided locally: no model location to show (SDD §4)."""
    advisor = Advisor(None, make_settings(tmp_path))
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")
    for rec in _recommendations(store):
        assert (rec.model.name, rec.model.deployment, rec.model.endpoint_host) == (
            "rules",
            "unknown",
            None,
        )


# -- ADV-16: invariants, the rule that converged, model time in demo mode (SDD §5.2-§5.5) ---------


async def test_a_completed_migration_records_its_rule_its_estimate_parts_and_no_data_issue(
    tmp_path, store
):
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm, "downtime_slo_s": 300}
    )
    await run_plan(h, plan)
    m = await h.by_vm(plan.id, "vm-1")
    done = await h.wait_phase(m.id, P.completed)
    await h.orch.stop()
    assert done.converged_by in {"threshold", "slo", "max_passes"}
    assert set(done.estimate.downtime_parts) == {
        "shutdown_s",
        "copy_s",
        "conversion_s",
        "create_s",
        "boot_s",
    }
    assert sum(done.estimate.downtime_parts.values()) == pytest.approx(done.estimate.downtime_s)
    assert done.data_issues == []
    assert "migration.data_inconsistent" not in h.kinds()


async def test_a_completed_migration_whose_numbers_do_not_hold_is_flagged(tmp_path, store):
    """A final pass recorded before the stop breaks invariant 1: stored and announced (SDD §5.5)."""
    early: dict = {}

    async def cutover(ctx):
        early["at"] = utcnow() - timedelta(seconds=30)
        await ctx.mark_downtime_start()
        return StepResult(
            destination_server_id="dst-1",
            sync_pass=SyncPass(
                number=next_pass_number(ctx.migration),
                kind="final",
                started_at=early["at"],
                ended_at=utcnow(),
                bytes_changed=1,
                bytes_transferred=1,
                duration_s=0.0,
            ),
        )

    settings = make_settings(tmp_path)
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1)],
        {"default_strategy": Strategy.warm, "downtime_slo_s": 300},
        settings=settings,
        executor=ScriptedExecutor(settings, hooks={StepName.CUTOVER: cutover}),
    )
    await run_plan(h, plan)
    done = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert [i.split(":")[0] for i in done.data_issues] == ["FINAL_SYNC_MISSING"]
    assert "migration.data_inconsistent" in h.kinds()


async def test_a_simulated_downtime_counts_model_seconds(tmp_path, store):
    """ADV-16 (SDD §5.2): after a simulated cutover the downtime is its model seconds plus the
    plan's boot time, like the passes' durations - so it compares with its estimate."""
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    await run_plan(h, plan)
    done = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    params = params_for_plan(plan)
    copy = done.sync_passes[-1].duration_s
    # its clock starts at the stop: the shutdown counts, as the estimate's parts do (SDD §5.2, §7.4)
    assert done.downtime_model_s == pytest.approx(params.shutdown_s + copy + params.create_s)
    assert done.actual_downtime_s == pytest.approx(
        params.shutdown_s + copy + params.create_s + params.boot_s
    )
    parts = done.estimate.downtime_parts
    assert done.actual_downtime_s - copy == pytest.approx(
        sum(seconds for part, seconds in parts.items() if part != "copy_s")
    )
    wall = (done.downtime_ended_at - done.downtime_started_at).total_seconds()
    assert done.actual_downtime_s > wall, "the simulator compresses wall time"


async def test_a_simulated_rollback_counts_model_seconds(tmp_path, store):
    """SDD §5.2 (the ADV-16 follow-up): a rollback after a simulated cutover failed ends the
    downtime in model seconds - the cutover's up to the failure and the rollback's own 90 s - not
    in the wall seconds the demo compresses, which dropped the clock from minutes to seconds."""
    settings = make_settings(tmp_path)

    async def failing(ctx):
        await ctx.mark_downtime_start()
        raise PermanentStepError("attach timed out", details={"downtime_model_s": 600.0})

    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold},
        executor=ScriptedExecutor(settings, hooks={StepName.CUTOVER: failing}), settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.orch.stop()
    assert (m.downtime_model_s, m.actual_downtime_s) == (690.0, 690.0)
    assert (m.downtime_ended_at - m.downtime_started_at).total_seconds() < 690.0
    events = h.store.events(since_seq=0, limit=1000)
    [ended] = [e for e in events if e.kind == "migration.downtime_ended"]
    assert ended.data == {"actual_downtime_s": 690.0}


async def test_a_rollback_that_interrupts_a_simulated_cutover_counts_its_model_seconds(
    tmp_path, store
):
    """SDD §5.2 (ADV-16..20 audit, F8): a rollback cancels the running simulated cutover, which
    reports nothing; the downtime counts the model seconds that cutover kept the source stopped,
    with the rollback's own 90 s - not only the 90 s."""
    settings = make_settings(tmp_path)
    executor = ScriptedExecutor(settings)
    calls = 0
    blocked = asyncio.Event()

    async def sleep(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:  # the shutdown, the copy, then the create: the rollback interrupts it
            blocked.set()
            await asyncio.Event().wait()

    executor.sim = SimulatedExecutor(settings, sleep=sleep)
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold},
        executor=executor, settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await asyncio.wait_for(blocked.wait(), 10)
    # the same seeded copy, run to its end by another simulator: its model seconds without the
    # create - the shutdown and the copy
    ctx, _ = make_ctx(plan, await h.migration(mid), SRC_PROVIDER, DST_PROVIDER, settings)
    whole = (await SimulatedExecutor(settings).run(StepName.CUTOVER, ctx)).details
    copy = whole["downtime_model_s"] - params_for_plan(plan).create_s
    await h.orch.rollback(mid, "bayu", "the copy is stuck")
    rolled = await h.wait_phase(mid, P.rolled_back)
    await h.orch.stop()
    assert rolled.actual_downtime_s == pytest.approx(copy + 90.0)
    assert rolled.actual_downtime_s > 90.0


async def test_a_rollback_no_step_reported_model_seconds_for_keeps_wall_time(tmp_path, store):
    """SDD §5.2: a real executor reports no model seconds - its rollback ends the downtime in
    wall seconds, as before."""
    settings = make_settings(tmp_path)

    async def failing(ctx):
        await ctx.mark_downtime_start()
        raise PermanentStepError("dst volume attach failed")

    async def rollback(ctx):
        return StepResult(details={"source_running": True})

    hooks = {StepName.CUTOVER: failing, StepName.ROLLBACK: rollback}
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold},
        executor=ScriptedExecutor(settings, hooks=hooks), settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.orch.stop()
    wall = (m.downtime_ended_at - m.downtime_started_at).total_seconds()
    assert (m.downtime_model_s, m.actual_downtime_s) == (None, wall)


async def test_a_simulated_rollback_after_its_cutover_finished_counts_the_boot_too(tmp_path, store):
    """SDD §5.2: the destination booted for its verification before the rollback, so the boot
    time counts too, as it does for a verified migration."""
    settings = make_settings(tmp_path)

    async def cutover(ctx):
        await ctx.mark_downtime_start()
        return StepResult(destination_server_id="dst-1", details={"downtime_model_s": 500.0})

    h, plan = await setup(
        tmp_path, store, [vm(1)],
        {
            "default_strategy": Strategy.cold,
            "verification": VerificationConfig(timeout_s=0, tcp_ports=[]),
        },
        executor=ScriptedExecutor(settings, hooks={StepName.CUTOVER: cutover}), settings=settings,
    )  # fmt: skip
    h.destination.status = "ERROR"
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.orch.stop()
    assert h.history(m)[-4:] == [P.verifying, P.failed, P.rolling_back, P.rolled_back]
    boot = params_for_plan(plan).boot_s
    assert m.actual_downtime_s == pytest.approx(500.0 + boot + 90.0)


async def test_a_retry_with_the_source_still_stopped_keeps_its_model_seconds(tmp_path, store):
    """SDD §5.2: a cutover that failed without an automatic rollback leaves the clock open; the
    retry keeps it and, in demo mode, its model seconds - the next attempt's add to them."""
    settings = make_settings(tmp_path)
    calls = 0

    async def cutover(ctx):
        nonlocal calls
        calls += 1
        await ctx.mark_downtime_start()
        if calls == 1:
            raise PermanentStepError("attach timed out", details={"downtime_model_s": 600.0})
        return StepResult(destination_server_id="dst-1", details={"downtime_model_s": 400.0})

    h, plan = await setup(
        tmp_path, store, [vm(1)],
        {
            "default_strategy": Strategy.cold,
            "verification": VerificationConfig(auto_rollback=False),
        },
        executor=ScriptedExecutor(settings, hooks={StepName.CUTOVER: cutover}), settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    failed = await h.wait_phase(mid, P.failed)
    assert failed.downtime_model_s == 600.0
    assert (await h.orch.retry(mid, "bayu")).downtime_model_s == 600.0
    done = await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    assert done.downtime_model_s == 1000.0
    assert done.actual_downtime_s == pytest.approx(1000.0 + params_for_plan(plan).boot_s)


async def test_a_retry_after_a_rollback_starts_its_model_seconds_afresh(tmp_path, store):
    """SDD §5.2: the source ran again after the rollback, so the clock closed - the next attempt's
    downtime counts its own model seconds, not the rolled-back attempt's."""
    settings = make_settings(tmp_path)
    calls = 0

    async def cutover(ctx):
        nonlocal calls
        calls += 1
        await ctx.mark_downtime_start()
        if calls == 1:
            raise PermanentStepError("attach timed out", details={"downtime_model_s": 600.0})
        return StepResult(destination_server_id="dst-1", details={"downtime_model_s": 400.0})

    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold},
        executor=ScriptedExecutor(settings, hooks={StepName.CUTOVER: cutover}), settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    assert (await h.wait_phase(mid, P.rolled_back)).actual_downtime_s == 690.0
    assert (await h.orch.retry(mid, "bayu")).downtime_model_s is None
    done = await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    assert done.downtime_model_s == 400.0
    assert done.actual_downtime_s == pytest.approx(400.0 + params_for_plan(plan).boot_s)


async def test_model_seconds_count_only_while_the_source_is_stopped(tmp_path, store):
    """SDD §5.2: the model seconds add up while the clock is open - a step that failed before the
    source stopped adds nothing to a downtime."""
    settings = make_settings(tmp_path)

    async def failing_precopy(ctx):
        raise PermanentStepError("snapshot quota exceeded", details={"downtime_model_s": 50.0})

    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm},
        executor=ScriptedExecutor(settings, hooks={StepName.PRECOPY: failing_precopy}),
        settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.failed)
    await h.orch.stop()
    assert (m.downtime_started_at, m.downtime_model_s) == (None, None)


# -- ADV-16 P4: Force cutover (SDD §5.4) ------------------------------------------------------


async def test_a_pass_alone_never_cuts_over_before_convergence_but_a_force_cutover_does(
    tmp_path, store
):
    """The spec's gating test: a 16.8 GiB delta above the 1 GiB threshold, 2 of 5 passes, an
    estimate over the SLO - no rule ends the passes; a recorded Force cutover does."""
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm, "downtime_slo_s": 60}
    )
    big = [
        SyncPass(
            number=1,
            kind="full",
            started_at=utcnow(),
            bytes_changed=374 * 2**30,
            bytes_transferred=374 * 2**30,
            duration_s=3000.0,
        ),
        SyncPass(
            number=2,
            kind="delta",
            started_at=utcnow(),
            bytes_changed=int(16.8 * 2**30),
            bytes_transferred=int(16.8 * 2**30),
            duration_s=300.0,
        ),
    ]
    m = make_migration(plan_id=plan.id, phase=P.syncing, sync_passes=big, vm=vm(1))
    assert h.orch._converged(m, plan)[0] is None
    forced = m.model_copy(
        update={
            "force_cutover": ForceCutover(
                actor="sari",
                at=utcnow(),
                reason="The window closes in ten minutes",
                delta_bytes=int(16.8 * 2**30),
                estimated_downtime_s=480.0,
            )
        }
    )
    assert h.orch._converged(forced, plan)[0] == "force"


async def test_a_force_cutover_is_recorded_audited_and_still_needs_another_approver(
    tmp_path, store
):
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1, size_gb=200, used_gb=150.0)],
        {
            "default_strategy": Strategy.warm,
            "require_approval": True,
            "auto_cutover": False,
            "downtime_slo_s": 1,
            "convergence_threshold_bytes": 0,
            "max_sync_passes": 50,
        },
    )
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.syncing)
    with pytest.raises(Unprocessable):
        await h.orch.force_cutover(m.id, "sari", "too short")
    forced = await h.orch.force_cutover(m.id, "sari", "The change window closes at 22:00")
    record = forced.force_cutover
    assert (record.actor, record.reason) == ("sari", "The change window closes at 22:00")
    assert record.estimated_downtime_s > 0 and forced.cutover_requested_by == "sari"
    event = [
        e for e in h.store.events(since_seq=0, limit=100000) if e.kind == "migration.force_cutover"
    ][-1]
    assert (event.actor, event.data["reason"]) == ("sari", "The change window closes at 22:00")
    m = await h.wait_phase(m.id, P.awaiting_cutover)
    assert m.converged_by == "force"
    await asyncio.sleep(0.1)  # maker-checker: the forcer's request is not an approval
    assert (await h.migration(m.id)).phase == P.awaiting_cutover
    await h.orch.approve(m.id, "rina", "Agreed to cut over outside convergence")
    done = await h.wait_phase(m.id, P.completed)
    await h.orch.stop()
    assert done.data_issues == []
    with pytest.raises(NotAllowed):
        await h.orch.force_cutover(m.id, "sari", "The change window closes at 22:00")


async def test_a_final_pass_keeps_the_estimate_the_convergence_rule_used(tmp_path, store):
    """SDD §5.3 (ADV-16): the Downtime panel compares with the estimate the gate saw."""
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm})
    full = SyncPass(
        number=1,
        kind="full",
        started_at=utcnow(),
        bytes_changed=2**30,
        bytes_transferred=2**30,
        duration_s=600.0,
    )
    m = make_migration(
        plan_id=plan.id, phase=P.syncing, sync_passes=[full], vm=vm(1), estimate=estimate_for_test()
    )
    h.orch._converge(m, plan, "slo")
    gate = m.estimate
    final = SyncPass(
        number=2,
        kind="final",
        started_at=utcnow(),
        bytes_changed=2**28,
        bytes_transferred=2**28,
        duration_s=30.0,
    )
    m.sync_passes.append(final)
    h.orch._calibrate(m, plan, final)
    assert m.estimate == gate


def estimate_for_test():
    from seamless_migrate.domain.models import Estimate

    return Estimate(
        strategy="warm",
        eligible=True,
        precopy_s=600,
        passes=1,
        downtime_s=900,
        total_s=1500,
        final_delta_bytes=0,
        meets_slo=False,
    )


def test_clearing_a_cutover_request_drops_its_force_cutover():
    from seamless_migrate.orchestrator import _clear_request

    m = make_migration(
        cutover_requested=True,
        cutover_requested_by="sari",
        force_cutover=ForceCutover(
            actor="sari",
            at=utcnow(),
            reason="The window closes soon",
            delta_bytes=1,
            estimated_downtime_s=10.0,
        ),
    )
    _clear_request(m)
    assert (m.cutover_requested, m.cutover_requested_by, m.force_cutover) == (False, None, None)


# -- application probes (SDD §7.5, §9.3, §9.6, ADV-18) ----------------------------------------
DB_REASON = "database VM without an application probe: only its boot was verified"


async def test_a_database_vm_without_an_application_probe_completes_with_review_required(
    tmp_path, store
):
    """Boot alone does not verify a database: the rules ask for review, with or without Jev."""
    db = vm(1, "ledger-db-01", tags={"role": "postgresql"})
    h, plan = await setup(tmp_path, store, [db], {"default_strategy": Strategy.cold})
    reasons: list[object] = []

    async def review(vm_ref, result, review_reason=None):
        reasons.append(review_reason)
        return None

    h.orch.advisor = SimpleNamespace(review_verification=review)
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert (m.review_required, m.review_reason) == (True, DB_REASON)
    assert reasons == [DB_REASON]
    # validation flagged it and recorded what to probe; nothing was probed
    assert "APP_PROBE_MISSING" in [f.code for f in m.findings]
    assert m.suggested_probes == [AppProbe(kind="tcp", port=5432)]
    assert m.verification is not None and m.verification.passed and m.verification.probes == []
    assert [c["name"] for c in m.verification.checks][:2] == ["server_active", "ports_up"]


async def test_a_failed_application_probe_rolls_the_vm_back(tmp_path, store):
    """A refused probe fails the verification; the plan's automatic rollback runs (SDD §7.5)."""
    db = vm(1, "ledger-db-01", tags={"role": "postgresql"})
    h, plan = await setup(
        tmp_path,
        store,
        [db],
        {
            "default_strategy": Strategy.cold,
            "verification": VerificationConfig(timeout_s=0),
            "probe_overrides": {"vm-1": [AppProbe(kind="tcp", port=5432)]},
        },
    )
    h.destination.services = {}  # the guest runs nothing on 5432
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.orch.stop()
    assert h.history(m)[-5:] == [P.cutover, P.verifying, P.failed, P.rolling_back, P.rolled_back]
    assert "app:tcp:5432" in (m.error or "")
    assert m.verification is not None and m.verification.passed is False
    (probe,) = m.verification.probes
    assert (probe.target, probe.result) == ("10.0.0.5:5432", "failed")
    phase_events = [
        e for e in store.events(since_seq=0, limit=10000) if e.kind == "migration.phase"
    ]
    assert any(e.data.get("verification", {}).get("probes") for e in phase_events)


async def test_an_unreachable_application_probe_passes_with_review_required(tmp_path, store):
    """Owner's open question 15: unreachable from the control plane is not a failure."""
    db = vm(1, "ledger-db-01", tags={"role": "postgresql"})
    h, plan = await setup(
        tmp_path,
        store,
        [db],
        {
            "default_strategy": Strategy.cold,
            "verification": VerificationConfig(timeout_s=0),
            "probe_overrides": {"vm-1": [AppProbe(kind="tcp", port=5432)]},
        },
    )
    h.destination.services = {5432: "unreachable"}
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert m.review_required is True
    assert m.review_reason == "application probe unreachable from the control plane: 10.0.0.5:5432"
    assert [p.result for p in m.verification.probes] == ["unreachable"]
    # the probe is configured: the database is not flagged for missing one
    assert "APP_PROBE_MISSING" not in [f.code for f in m.findings]


async def test_a_database_vm_whose_probe_answers_needs_no_review(tmp_path, store):
    db = vm(1, "ledger-db-01", tags={"role": "postgresql"})
    h, plan = await setup(
        tmp_path,
        store,
        [db],
        {
            "default_strategy": Strategy.cold,
            "verification": VerificationConfig(
                timeout_s=0, app_probes=[AppProbe(kind="tcp", port=5432)]
            ),
        },
    )
    h.destination.services = {5432: "passed"}
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.completed)
    await h.orch.stop()
    assert (m.review_required, m.review_reason) == (False, None)
    assert [(p.target, p.result) for p in m.verification.probes] == [("10.0.0.5:5432", "passed")]


async def test_validate_refuses_invalid_application_probes(tmp_path, store):
    """SDD §12: a probe port outside 1-65535, a path without '/', a status outside 100-599, or
    more than 10 probes for one VM would fail every verification or flood it."""
    from seamless_migrate.domain.models import invalid_plan_settings

    bad = plan_for(
        [vm(1)],
        verification=VerificationConfig(
            app_probes=[AppProbe(kind="tcp", port=0), AppProbe(kind="http", port=80, path="health")]
        ),
        probe_overrides={
            "vm-1": [AppProbe(kind="https", port=443, expect_status=99)],
            "vm-2": [AppProbe(kind="tcp", port=1000 + i) for i in range(11)],
        },
    )
    problems = invalid_plan_settings(bad)
    assert problems == [
        "verification.app_probes: port 0 outside 1-65535",
        "verification.app_probes: path 'health' must start with '/' (at most 200 characters)",
        "probe_overrides[vm-1]: expect_status 99 outside 100-599",
        "probe_overrides[vm-2]: 11 probes, at most 10",
    ]


# -- the finalize queue: bulk finalize and the source watch (SDD §8.1, ADV-19) -----------------
def _waved(vms):
    return [
        Wave(id="wave-1", name="Pilot", order=1, vm_ids=["vm-1", "vm-2"]),
        Wave(id="wave-2", name="Rest", order=2, vm_ids=["vm-3"], depends_on=["wave-1"]),
    ]


async def _completed_plan(tmp_path, store, n=3, **plan_kw):
    vms = [vm(i) for i in range(1, n + 1)]
    h, plan = await setup(
        tmp_path, store, vms, {"default_strategy": Strategy.cold, "waves": _waved(vms), **plan_kw}
    )
    await run_plan(h, plan)
    for i in range(1, n + 1):
        await h.wait_phase((await h.by_vm(plan.id, f"vm-{i}")).id, P.completed)
    await h.orch.stop()
    return h, plan


async def test_finalize_plan_finalizes_a_wave_with_one_approval_and_audits_each_vm(tmp_path, store):
    h, plan = await _completed_plan(tmp_path, store)
    with pytest.raises(BadRequest, match="Pilot"):
        await h.orch.finalize_plan(plan.id, "rina", wave_id="wave-1", confirm="pilot")
    with pytest.raises(NotFound):
        await h.orch.finalize_plan(plan.id, "rina", wave_id="wave-9", confirm="Pilot")
    out = await h.orch.finalize_plan(plan.id, "rina", wave_id="wave-1", confirm="Pilot")
    ms = {m.vm.source_id: m for m in store.list("migration", Migration)}
    assert out == {"finalized": [ms["vm-1"].id, ms["vm-2"].id], "skipped": []}
    assert [ms[v].phase for v in ("vm-1", "vm-2", "vm-3")] == [
        P.finalized,
        P.finalized,
        P.completed,
    ]
    events = store.events(since_seq=0, limit=100000)
    actions = [
        e for e in events if e.kind == "migration.action" and e.data.get("action") == "finalize"
    ]
    assert sorted((e.migration_id, e.actor) for e in actions) == sorted(
        [(ms["vm-1"].id, "rina"), (ms["vm-2"].id, "rina")]
    )
    [batch] = [e for e in events if e.kind == "plan.finalized"]
    assert (batch.actor, batch.plan_id) == ("rina", plan.id)
    assert batch.data == {"wave_id": "wave-1", "finalized": ["web-01", "web-02"], "skipped": []}
    # the rest of the plan, by its name
    rest = await h.orch.finalize_plan(plan.id, "rina", wave_id=None, confirm="Finance")
    assert rest == {"finalized": [ms["vm-3"].id], "skipped": []}
    with pytest.raises(NotAllowed, match="no completed migration"):
        await h.orch.finalize_plan(plan.id, "rina", wave_id=None, confirm="Finance")


async def test_a_failed_finalize_that_began_deleting_the_source_cannot_be_rolled_back(
    tmp_path, store
):
    """SDD §8.1: a finalize that deletes the source records checkpoint "finalizing" before its
    step. When the step fails — a deletion that timed out may still finish — the migration stays
    in the finalize queue to be finalized again, and a rollback answers 409: it would delete the
    destination server and find no source to start. A finalize that keeps the source changes
    nothing a rollback needs."""
    settings = make_settings(tmp_path)
    failed_once: set[str] = set()

    async def finalize(ctx):
        vm_id = ctx.migration.vm.source_id
        if vm_id not in failed_once:
            failed_once.add(vm_id)
            raise TransientStepError("finalize: src-osp: call timed out after 300 s")
        return StepResult(details={"source_deleted": bool(ctx.options.get("delete_source"))})

    executor = ScriptedExecutor(settings, hooks={StepName.FINALIZE: finalize})
    h, plan = await setup(
        tmp_path,
        store,
        [vm(1), vm(2)],
        {"default_strategy": Strategy.cold},
        executor=executor,
        settings=settings,
    )
    await run_plan(h, plan)
    m1, m2 = [await h.by_vm(plan.id, v) for v in ("vm-1", "vm-2")]
    for m in (m1, m2):
        await h.wait_phase(m.id, P.completed)
    await h.orch.stop()

    with pytest.raises(NotAllowed, match="finalize failed"):
        await h.orch.finalize(m1.id, "rina", delete_source=True, confirm=m1.vm.name)
    stuck = await h.migration(m1.id)
    assert (stuck.phase, stuck.checkpoint) == (P.completed, "finalizing")
    with pytest.raises(
        NotAllowed, match="the finalize began deleting the source VM: finalize it again"
    ):
        await h.orch.rollback(m1.id, "bayu", "revert to the source")
    assert (await h.migration(m1.id)).phase == P.completed
    done = await h.orch.finalize(m1.id, "rina", delete_source=True, confirm=m1.vm.name)
    assert (done.phase, done.checkpoint) == (P.finalized, "finalized")

    # a failed finalize that keeps the source leaves the rollback to the operator
    with pytest.raises(NotAllowed, match="finalize failed"):
        await h.orch.finalize(m2.id, "rina", delete_source=False, confirm=m2.vm.name)
    kept = await h.migration(m2.id)
    assert kept.phase == P.completed and kept.checkpoint != "finalizing"
    rolled = await h.orch.rollback(m2.id, "bayu", "revert to the source")
    assert rolled.phase == P.rolling_back
    await h.orch.stop()


async def test_finalize_plan_skips_a_vm_whose_finalize_raised_any_error(tmp_path, store, caplog):
    """SDD §8.1: an unexpected error in one VM's finalize — a provider error, an OSError — skips
    that VM with its redacted reason instead of aborting the approval: the VMs after it are still
    finalized, the skipped ones stay completed, one plan.finalized event reports both, and the
    traceback goes to the service log."""
    h, plan = await _completed_plan(tmp_path, store, n=3, waves=[])
    password = "Sup3r" + "S3cretPw"

    async def flaky(ctx):
        if ctx.migration.vm.source_id == "vm-1":
            raise ProviderError(
                f"src-osp: Unauthorized: POST https://svc:{password}@keystone.example/v3"
            )
        if ctx.migration.vm.source_id == "vm-2":
            raise OSError(28, "No space left on device")
        return StepResult(details={"finalized": True})

    h.executor.hooks[StepName.FINALIZE] = flaky
    with caplog.at_level(logging.WARNING, logger="seamless_migrate.orchestrator"):
        out = await h.orch.finalize_plan(plan.id, "rina", wave_id=None, confirm="Finance")
    ms = {v: await h.by_vm(plan.id, v) for v in ("vm-1", "vm-2", "vm-3")}
    logged = [r for r in caplog.records if r.getMessage().startswith("finalize of ")]
    assert [(r.getMessage(), r.exc_info[0]) for r in logged] == [
        (f"finalize of {ms['vm-1'].id} failed", ProviderError),
        (f"finalize of {ms['vm-2'].id} failed", OSError),
    ]
    assert out["finalized"] == [ms["vm-3"].id]
    assert [s["migration_id"] for s in out["skipped"]] == [ms["vm-1"].id, ms["vm-2"].id]
    assert [s["reason"] for s in out["skipped"]] == [
        "finalize failed: src-osp: Unauthorized: POST https://[REDACTED]@keystone.example/v3",
        "finalize failed: [Errno 28] No space left on device",
    ]
    assert [ms[v].phase for v in ("vm-1", "vm-2", "vm-3")] == [
        P.completed,
        P.completed,
        P.finalized,
    ]
    [batch] = [e for e in store.events(since_seq=0, limit=100000) if e.kind == "plan.finalized"]
    assert batch.data["finalized"] == ["web-03"]
    assert [s["vm"] for s in batch.data["skipped"]] == ["web-01", "web-02"]
    assert password not in str(batch.data)


async def test_finalize_plan_reports_the_vms_it_could_not_finalize(tmp_path, store):
    h, plan = await _completed_plan(tmp_path, store, n=2, waves=[])

    async def refuse(ctx):
        if ctx.migration.vm.source_id == "vm-2":
            raise PermanentStepError("source cleanup failed: HTTP 409")
        return StepResult(details={"finalized": True})

    h.executor.hooks[StepName.FINALIZE] = refuse
    out = await h.orch.finalize_plan(plan.id, "rina", wave_id=None, confirm="Finance")
    m2 = await h.by_vm(plan.id, "vm-2")
    assert out["finalized"] == [(await h.by_vm(plan.id, "vm-1")).id]
    assert out["skipped"] == [
        {"migration_id": m2.id, "reason": "finalize failed: source cleanup failed: HTTP 409"}
    ]
    assert m2.phase == P.completed


async def test_a_finalize_step_errors_reason_is_redacted_and_cut(tmp_path, store):
    """SDD §8.1: a skipped VM's reason is 'finalize failed: ...', the error redacted and cut - a
    step error too, not only an unexpected one."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])

    async def long_failure(ctx):
        raise PermanentStepError("source cleanup failed: " + "x" * 3000)

    h.executor.hooks[StepName.FINALIZE] = long_failure
    out = await h.orch.finalize_plan(plan.id, "rina", wave_id=None, confirm=plan.name)
    [skipped] = out["skipped"]
    assert skipped["reason"].startswith("finalize failed: source cleanup failed: xxx")
    assert len(skipped["reason"]) <= len("finalize failed: ") + 500


RUNS_AGAIN = "the source VM runs again: stop it, or roll it back, before finalizing"


async def _watch(h, plan, vm_id="vm-1"):
    """One source watch, past its interval, and the migration it looked at (SDD §8.1)."""
    h.clock.advance(h.settings.source_watch_s + 1)
    await h.orch.tick()
    if h.orch._watch_task is not None:
        await h.orch._watch_task
    return await h.by_vm(plan.id, vm_id)


async def test_finalize_is_refused_while_the_source_vm_runs_again(tmp_path, store):
    """SDD §8.1 (the owner's choice, 2026-10-13): while the source watch sees the source VM running
    again, a finalize answers 409 - it would keep a source that diverged, or delete it with what it
    wrote since - and a bulk finalize skips that VM with the reason. Seen stopped again, it
    finalizes."""
    h, plan = await _completed_plan(tmp_path, store, n=2, waves=[])
    m1, m2 = [await h.by_vm(plan.id, v) for v in ("vm-1", "vm-2")]
    # the fake source keeps its VMs running after a simulated cutover: only vm-1 runs again
    for vm_id, state in (("vm-1", "running"), ("vm-2", "stopped")):
        h.source.vms[vm_id] = h.source.vms[vm_id].model_copy(update={"power_state": state})
    assert (await _watch(h, plan)).source_running_at is not None
    assert (await h.by_vm(plan.id, "vm-2")).source_running_at is None
    for delete_source in (False, True):
        with pytest.raises(NotAllowed, match=f"^{RUNS_AGAIN}$"):
            await h.orch.finalize(m1.id, "rina", delete_source=delete_source, confirm=m1.vm.name)
    assert (await h.migration(m1.id)).phase == P.completed
    out = await h.orch.finalize_plan(plan.id, "rina", wave_id=None, confirm=plan.name)
    assert out == {"finalized": [m2.id], "skipped": [{"migration_id": m1.id, "reason": RUNS_AGAIN}]}
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "stopped"})
    assert (await _watch(h, plan)).source_running_at is None
    assert (await h.orch.finalize(m1.id, "rina", confirm=m1.vm.name)).phase == P.finalized
    await h.orch.stop()


async def test_a_finalize_that_began_deleting_the_source_finishes_while_it_runs(tmp_path, store):
    """SDD §8.1: the exception - a migration whose finalize began deleting the source (checkpoint
    "finalizing") cannot be rolled back, so finalizing it again from its own page stays its way
    out even while the source watch sees the VM running; a bulk finalize skips it, as it skips
    every VM whose source runs again (the plan's summaries carry no checkpoint)."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    m = await h.by_vm(plan.id, "vm-1")
    failed: list[str] = []

    async def finalize(ctx):
        if not failed:
            failed.append(ctx.migration.id)
            raise TransientStepError("finalize: src-osp: call timed out after 300 s")
        return StepResult(details={"source_deleted": True})

    h.executor.hooks[StepName.FINALIZE] = finalize
    with pytest.raises(NotAllowed, match="finalize failed"):
        await h.orch.finalize(m.id, "rina", delete_source=True, confirm=m.vm.name)
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})
    seen = await _watch(h, plan)
    assert (seen.checkpoint, seen.source_running_at is not None) == ("finalizing", True)
    out = await h.orch.finalize_plan(plan.id, "rina", wave_id=None, confirm=plan.name)
    assert out == {"finalized": [], "skipped": [{"migration_id": m.id, "reason": RUNS_AGAIN}]}
    done = await h.orch.finalize(m.id, "rina", delete_source=True, confirm=m.vm.name)
    assert done.phase == P.finalized
    await h.orch.stop()


async def test_a_running_source_is_never_kept_by_a_finalize(tmp_path, store):
    """SDD §8.1 (audit OC-09): the exception to the running-source refusal - a finalize that began
    deleting the source - looked at the checkpoint alone: after a deleting finalize failed and
    the source ran again, a finalize with delete_source false was accepted, the diverged source
    kept and the finalized migration watched no more. Only a deleting finalize passes."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    m = await h.by_vm(plan.id, "vm-1")
    failed: list[str] = []

    async def finalize(ctx):
        if not failed:
            failed.append(ctx.migration.id)
            raise TransientStepError("finalize: src-osp: call timed out after 300 s")
        return StepResult(details={"source_deleted": True})

    h.executor.hooks[StepName.FINALIZE] = finalize
    with pytest.raises(NotAllowed, match="finalize failed"):
        await h.orch.finalize(m.id, "rina", delete_source=True, confirm=m.vm.name)
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})
    seen = await _watch(h, plan)
    assert (seen.checkpoint, seen.source_running_at is not None) == ("finalizing", True)
    with pytest.raises(NotAllowed) as refused:
        await h.orch.finalize(m.id, "rina", delete_source=False, confirm=m.vm.name)
    assert str(refused.value) == RUNS_AGAIN
    assert (await h.migration(m.id)).phase == P.completed
    done = await h.orch.finalize(m.id, "rina", delete_source=True, confirm=m.vm.name)
    assert done.phase == P.finalized
    await h.orch.stop()


async def test_the_source_watch_flags_a_completed_vms_source_running_again(tmp_path, store):
    """SDD §8.1: the source VM of a migration awaiting finalize runs again: its data diverges."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    m = await h.by_vm(plan.id, "vm-1")
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "stopped"})

    async def watch(seconds=h.settings.source_watch_s + 1):
        h.clock.advance(seconds)
        await h.orch.tick()
        if h.orch._watch_task is not None:
            await h.orch._watch_task  # the watch runs beside the tick (SDD §8.1)
        return await h.by_vm(plan.id, "vm-1")

    assert (await watch()).source_running_at is None
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})
    # not before the interval is up
    assert (await watch(seconds=1)).source_running_at is None
    seen = await watch()
    assert seen.source_running_at is not None
    alerts = [
        e for e in store.events(since_seq=0, limit=100000) if e.kind == "migration.source_running"
    ]
    assert [(e.migration_id, e.data) for e in alerts] == [(m.id, {"power_state": "running"})]
    # once: a running source is not reported again
    again = await watch()
    assert again.source_running_at == seen.source_running_at
    assert (
        len(
            [
                e
                for e in store.events(since_seq=0, limit=100000)
                if e.kind == "migration.source_running"
            ]
        )
        == 1
    )
    # stopped again: the alert clears and migration.source_stopped records it, once; the stopped
    # source of the first check, never flagged, reported nothing
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "stopped"})
    assert (await watch()).source_running_at is None
    assert (await watch()).source_running_at is None
    events = store.events(since_seq=0, limit=100000)
    assert [e.kind for e in events].count("migration.source_running") == 1
    stopped = [(e.migration_id, e.data) for e in events if e.kind == "migration.source_stopped"]
    assert stopped == [(m.id, {"power_state": "stopped"})]


async def test_the_source_watch_records_when_the_provider_answered(tmp_path, store):
    """SDD §8.1 (ADV-16..20 audit, F9): a source provider may take minutes per call - the alert's
    time is when it answered that the VM runs, not when the watch began."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})
    get_vm = h.source.get_vm

    async def slow_get_vm(vm_id):
        h.clock.advance(600)  # the provider answers ten minutes later
        return await get_vm(vm_id)

    h.source.get_vm = slow_get_vm
    h.clock.advance(h.settings.source_watch_s + 1)
    began = h.orch.now()
    await h.orch.tick()
    await h.orch._watch_task
    seen = (await h.by_vm(plan.id, "vm-1")).source_running_at
    assert (seen - began).total_seconds() == pytest.approx(600, abs=1)


async def test_the_source_watch_clears_the_alert_only_when_it_sees_the_vm_stopped(tmp_path, store):
    """SDD §8.1 (ADV-16..20 audit, F1): a reboot or a resize (transitioning), a paused VM, an error
    or an unknown state is not "seen stopped" - the alert stays, with the time it was first seen."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    m = await h.by_vm(plan.id, "vm-1")

    async def watch(state):
        h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": state})
        h.clock.advance(h.settings.source_watch_s + 1)
        await h.orch.tick()
        if h.orch._watch_task is not None:
            await h.orch._watch_task  # the watch runs beside the tick (SDD §8.1)
        return await h.by_vm(plan.id, "vm-1")

    assert (await watch("transitioning")).source_running_at is None, "never seen running"
    seen = (await watch("running")).source_running_at
    assert seen is not None
    for state in ("transitioning", "paused", "error", "unknown", "running"):
        assert (await watch(state)).source_running_at == seen, state
    assert (await watch("stopped")).source_running_at is None
    kinds = [e.kind for e in store.events(since_seq=0, limit=100000) if e.migration_id == m.id]
    assert [k for k in kinds if k.startswith("migration.source_")] == [
        "migration.source_running",
        "migration.source_stopped",
    ]


async def test_a_slow_source_never_holds_the_tick(tmp_path, store):
    """SDD §8.1 (advisor Phase 1 audit, A7): the tick starts the source watch and never waits for
    it - a hanging source provider delays the watch only, one watch runs at a time, and stopping
    the orchestrator cancels it."""
    h, plan = await _completed_plan(tmp_path, store, n=2, waves=[])
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})
    release, calls = asyncio.Event(), []
    get_vm = h.source.get_vm

    async def hanging_get_vm(vm_id):
        calls.append(vm_id)
        await release.wait()
        return await get_vm(vm_id)

    h.source.get_vm = hanging_get_vm
    h.clock.advance(h.settings.source_watch_s + 1)
    await asyncio.wait_for(h.orch.tick(), 1)  # the tick does not wait for the provider
    h.clock.advance(h.settings.source_watch_s + 1)
    await asyncio.wait_for(h.orch.tick(), 1)  # due again, but the first watch still runs
    await asyncio.sleep(0.05)
    assert calls == ["vm-1"]
    release.set()
    await h.orch._watch_task
    assert (await h.by_vm(plan.id, "vm-1")).source_running_at is not None
    # a watch still waiting when the orchestrator stops is cancelled with it
    release.clear()
    h.clock.advance(h.settings.source_watch_s + 1)
    await h.orch.tick()
    await asyncio.sleep(0.05)
    watch = h.orch._watch_task
    await asyncio.wait_for(h.orch.stop(), 1)
    assert watch.cancelled()


async def test_a_source_watch_that_fails_is_logged_and_the_next_one_runs(tmp_path, store, caplog):
    """SDD §8.1: the watch's own failure is only logged - its task ends cleanly, never with an
    exception nobody retrieves, and the next watch runs when it is due."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    once = h.orch._watch_sources_once
    failures = []

    async def failing_once():
        if not failures:
            failures.append(1)
            raise RuntimeError("the store was briefly unavailable")
        await once()

    h.orch._watch_sources_once = failing_once
    h.clock.advance(h.settings.source_watch_s + 1)
    await h.orch.tick()
    first = h.orch._watch_task
    await first
    assert first.exception() is None
    assert "the source watch failed" in caplog.text
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})
    h.clock.advance(h.settings.source_watch_s + 1)
    await h.orch.tick()
    await h.orch._watch_task
    assert (await h.by_vm(plan.id, "vm-1")).source_running_at is not None


async def test_a_rollback_clears_the_source_running_flag(tmp_path, store):
    """SDD §8.1 (advisor Phase 1 audit, A9): a rollback starts the source on purpose; a migration
    that completes again is watched afresh, not flagged by a stale alert."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    m = await h.by_vm(plan.id, "vm-1")
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})
    h.clock.advance(h.settings.source_watch_s + 1)
    await h.orch.tick()
    await h.orch._watch_task
    assert (await h.migration(m.id)).source_running_at is not None
    rolled = await h.orch.rollback(m.id, "bayu", "the application misbehaves on RHOSO")
    assert rolled.source_running_at is None
    assert (await h.migration(m.id)).source_running_at is None


async def test_the_source_watch_reads_each_plan_once_per_pass(tmp_path, store, monkeypatch):
    """SDD §8.1: one pass over a plan's completed VMs reads the plan once, not once per VM (it
    ran every 5 s in demo and re-read a 22 KB plan per VM); a plan that is gone is asked once."""
    h, plan = await _completed_plan(tmp_path, store, n=3, waves=[])
    asked: list[str] = []
    real_plan = h.orch._plan

    async def counting_plan(plan_id):
        asked.append(plan_id)
        return await real_plan(plan_id)

    monkeypatch.setattr(h.orch, "_plan", counting_plan)
    h.source.vms["vm-2"] = h.source.vms["vm-2"].model_copy(update={"power_state": "running"})
    await h.orch._watch_sources_once()
    assert asked == [plan.id]
    assert (await h.by_vm(plan.id, "vm-2")).source_running_at is not None

    asked.clear()

    async def gone(plan_id):
        asked.append(plan_id)
        raise NotFound("plan", plan_id)

    monkeypatch.setattr(h.orch, "_plan", gone)
    await h.orch._watch_sources_once()
    assert asked == [plan.id]


async def test_a_store_error_reading_the_destination_never_loses_the_source_running_alert(
    tmp_path, store, monkeypatch, caplog
):
    """SDD §8.1 (review RV20-03): the alert names the destination provider (T6), read before the
    flag is saved. A store error on that read leaves the flag unsaved for this pass, logged, and
    the next pass records the flag and emits migration.source_running together: never a flag
    without its alert."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    m = await h.by_vm(plan.id, "vm-1")
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})
    real_get = h.orch.db.get
    failed: list[str] = []

    async def flaky_get(kind, key, *args, **kwargs):
        if (kind, key) == ("provider", plan.destination_provider_id) and not failed:
            failed.append(key)
            raise RuntimeError("the store was briefly unavailable")
        return await real_get(kind, key, *args, **kwargs)

    monkeypatch.setattr(h.orch.db, "get", flaky_get)

    def alerts():
        events = store.events(since_seq=0, limit=100000)
        return [e for e in events if e.kind == "migration.source_running"]

    await h.orch._watch_sources()
    assert failed == [plan.destination_provider_id]
    assert (await h.migration(m.id)).source_running_at is None
    assert alerts() == []
    assert f"source watch failed for {m.id}" in caplog.text
    await h.orch._watch_sources()
    assert (await h.migration(m.id)).source_running_at is not None
    [alert] = alerts()
    assert (alert.migration_id, alert.data) == (m.id, {"power_state": "running"})
    assert f"its data diverges from {DST_PROVIDER.name};" in alert.message


#: each half of a write the source watch makes, refused in turn when it runs (reviews RV23-08,
#: RV24-03): the event's INSERT, or the migration's UPDATE
HALVES = {"event": "INSERT INTO events", "flag": "UPDATE documents"}


@pytest.mark.parametrize("half", list(HALVES))
async def test_a_store_error_writing_the_alert_never_leaves_the_flag_without_it(
    tmp_path, store, refuse_write, caplog, half
):
    """SDD §8.1 (review RV21-04, the owner's choice): the flag and its migration.source_running
    alert are one store transaction. Either half the store cannot write leaves neither, logged,
    and the next pass writes both - never a flag without its alert (finalize answering 409 while
    no page shows why), nor an alert without its flag."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    m = await h.by_vm(plan.id, "vm-1")
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})
    of = "migration.source_running" if half == "event" else "migration"
    failed = refuse_write(store, HALVES[half], of)

    def alerts():
        events = store.events(since_seq=0, limit=100000)
        return [e for e in events if e.kind == "migration.source_running"]

    await h.orch._watch_sources()
    assert failed == [f"{HALVES[half]} {of}"]
    assert (await h.migration(m.id)).source_running_at is None
    assert alerts() == []
    assert "the source watch failed" in caplog.text
    await h.orch._watch_sources()
    assert (await h.migration(m.id)).source_running_at is not None
    assert len(alerts()) == 1


@pytest.mark.parametrize("half", list(HALVES))
@pytest.mark.parametrize("seen", ["stopped", "gone"])
async def test_a_store_error_writing_the_stopped_event_never_clears_the_flag_without_it(
    tmp_path, store, refuse_write, seen, half
):
    """SDD §8.1 (review RV21-04): clearing the flag and its migration.source_stopped event are one
    store transaction too, for a source seen stopped or gone - either half the store cannot write
    leaves the flag set and no event, and the next pass clears it with its event."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    m = await h.by_vm(plan.id, "vm-1")
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})
    await h.orch._watch_sources()
    assert (await h.migration(m.id)).source_running_at is not None
    if seen == "stopped":
        h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "stopped"})
    else:
        del h.source.vms["vm-1"]
    of = "migration.source_stopped" if half == "event" else "migration"
    failed = refuse_write(store, HALVES[half], of)

    def stops():
        events = store.events(since_seq=0, limit=100000)
        return [e for e in events if e.kind == "migration.source_stopped"]

    await h.orch._watch_sources()
    assert failed == [f"{HALVES[half]} {of}"]
    assert (await h.migration(m.id)).source_running_at is not None
    assert stops() == []
    await h.orch._watch_sources()
    assert (await h.migration(m.id)).source_running_at is None
    assert len(stops()) == 1


async def test_a_source_watch_racing_another_write_never_overwrites_it(
    tmp_path, store, land_first, caplog
):
    """SDD §8.1, §11 (review RV25-04): another write to the migration that lands between the source
    watch's read and its write - an operator's, the CLI's - makes the watch's write a conflict: it
    keeps that write, writes no flag and no alert, and the next pass writes both over it."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    m = await h.by_vm(plan.id, "vm-1")
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})

    def other():
        current, version = store.get_versioned("migration", m.id, Migration)
        theirs = current.model_copy(update={"review_reason": "an operator wrote this"})
        return store.put("migration", theirs, version)

    landed = land_first(store, "UPDATE documents", "migration", other)

    def alerts():
        events = store.events(since_seq=0, limit=100000)
        return [e for e in events if e.kind == "migration.source_running"]

    await h.orch._watch_sources()
    assert len(landed) == 1
    seen = await h.migration(m.id)
    assert (seen.review_reason, seen.source_running_at, alerts()) == (
        "an operator wrote this",
        None,
        [],
    )
    assert "the source watch failed" in caplog.text
    await h.orch._watch_sources()
    seen = await h.migration(m.id)
    assert seen.review_reason == "an operator wrote this" and seen.source_running_at is not None
    assert len(alerts()) == 1


async def test_the_source_watch_skips_a_source_the_provider_no_longer_has(tmp_path, store):
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    del h.source.vms["vm-1"]  # finalized with delete_source, or rebuilt by a handover
    h.clock.advance(h.settings.source_watch_s + 1)
    await h.orch.tick()
    assert h.orch._watch_task is not None
    await h.orch._watch_task
    assert (await h.by_vm(plan.id, "vm-1")).source_running_at is None


async def test_the_source_watch_clears_a_source_running_again_once_its_cloud_says_it_is_gone(
    tmp_path, store
):
    """SDD §8.1 (RR-02, the owner's choice): a source that ran again and was then deleted - or that
    its cloud no longer has - leaves nothing to keep or roll back to: the watch clears the flag
    with migration.source_stopped marked gone, and Finalize works again."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    m = await h.by_vm(plan.id, "vm-1")

    async def watch():
        h.clock.advance(h.settings.source_watch_s + 1)
        await h.orch.tick()
        if h.orch._watch_task is not None:
            await h.orch._watch_task  # the watch runs beside the tick (SDD §8.1)
        return await h.by_vm(plan.id, "vm-1")

    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})
    assert (await watch()).source_running_at is not None
    with pytest.raises(NotAllowed, match="runs again"):
        await h.orch.finalize(m.id, "ana", confirm=m.vm.name)
    del h.source.vms["vm-1"]  # deleted in the source cloud after it diverged
    assert (await watch()).source_running_at is None
    events = [e for e in store.events(since_seq=0, limit=100000) if e.migration_id == m.id]
    [gone] = [e for e in events if e.kind == "migration.source_stopped"]
    assert gone.data == {"power_state": None, "gone": True}
    assert "no longer exists" in gone.message
    assert (await h.orch.finalize(m.id, "ana", confirm=m.vm.name)).phase == P.finalized
    await h.orch.stop()


async def test_the_source_watch_keeps_a_source_running_again_while_its_cloud_fails(tmp_path, store):
    """SDD §8.1: only a cloud that answers can say the VM is gone - an error keeps the flag."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "running"})
    h.clock.advance(h.settings.source_watch_s + 1)
    await h.orch.tick()
    await h.orch._watch_task
    seen = (await h.by_vm(plan.id, "vm-1")).source_running_at
    assert seen is not None

    async def unreachable(source_id):
        raise ProviderError("src-osp: Unable to establish connection")

    h.source.get_vm = unreachable
    h.clock.advance(h.settings.source_watch_s + 1)
    await h.orch.tick()
    await h.orch._watch_task
    assert (await h.by_vm(plan.id, "vm-1")).source_running_at == seen
    await h.orch.stop()


async def test_the_earlier_attempts_evidence_names_the_strategy_those_attempts_used(
    tmp_path, store
):
    """SDD §14.4 (advisor Phase 1 audit, A6): a retry records the strategy of the attempt it
    retries; after a failed warm attempt and a validation that applied cold, the evidence names
    warm and does not oppose cold - before, it named cold and opposed it, a false fact."""
    jev_settings = Settings(jev_mode="stdio")
    answer = load_fixture("decide")
    answer["recommendation"].update(selected="warm", confidence=0.99)
    factory, _ = session_factory(fixture_responder({"decide": answer}))
    advisor = Advisor(JevClient(jev_settings, session_factory=factory), jev_settings)
    settings = make_settings(tmp_path)
    executor = ScriptedExecutor(settings)

    async def failing_precopy(ctx):
        raise PermanentStepError("pre-copy failed")

    executor.hooks[StepName.PRECOPY] = failing_precopy
    h, plan = await setup(
        tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)],
        advisor=advisor, executor=executor, settings=settings,
    )  # fmt: skip
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.failed)
    await h.orch.stop()
    await h.orch.pause_plan(plan.id, "alice")
    retried = await h.orch.retry(m.id, "alice")
    assert (retried.attempts, retried.attempt_strategies) == (1, [Strategy.warm])
    answer["recommendation"].update(selected="cold")
    await h.orch.validate_plan(plan.id, "alice")
    assert (await h.migration(m.id)).strategy == Strategy.cold
    await h.orch.validate_plan(plan.id, "alice")
    newest = max(_recommendations(store), key=lambda r: (r.created_at, r.id))
    [earlier] = [e for e in newest.evidence if e.key == "earlier_runs"]
    assert (earlier.display, earlier.effect) == ("1 earlier attempt with Warm migration", "context")


async def test_a_retry_records_the_attempts_strategies_oldest_first(tmp_path, store):
    """SDD §4.2: ``attempt_strategies`` lists each retried attempt's strategy, oldest first - the
    order the earlier-attempts evidence names them in (§14.4)."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    assert m.strategy == Strategy.cold
    m.phase = P.rolled_back
    m.attempts = 1
    m.attempt_strategies = [Strategy.warm]  # its first attempt ran warm
    store.put("migration", m)
    again = await h.orch.retry(m.id, "alice")
    assert (again.attempts, again.attempt_strategies) == (2, [Strategy.warm, Strategy.cold])


def _strict(minimum: int) -> AdvisorPolicy:
    """A policy that applies a confident decision only when it rests on ``minimum`` facts."""
    return AdvisorPolicy(
        id="pol-strict",
        name="Strict",
        mode="auto_apply_above_threshold",
        threshold=0.6,
        min_evidence_for_auto_apply=minimum,
        updated_by="rina",
    )


async def test_validation_records_the_evidence_and_counts_what_bears_on_the_decision(
    tmp_path, store
):
    """ADV-02 (SDD §14.4): the recommendation stores the evidence validation held, measured against
    the recommended option; a decision below the policy's minimum is never applied on its own."""
    advisor, _ = _tie_advisor("warm", 0.9)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    store.put("advisor_policy", _strict(99))
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    [rec] = _recommendations(store)
    assert rec.evidence_option == "warm"
    keys = [i.key for i in rec.evidence]
    assert keys[0] == "downtime_estimate" and {
        "mappings",
        "findings",
        "disk_total",
        "source_kind",
    } <= set(keys)
    assert {i.source.ref_id for i in rec.evidence} <= {
        rec.subject.id,
        m.id,
        plan.id,
        plan.source_provider_id,
    }
    assert next(i for i in rec.evidence if i.key == "disk_total").value == m.vm.disk_bytes
    bearing = sum(1 for i in rec.evidence if i.effect != "context")
    assert 0 < bearing < 99
    assert (rec.decision.status, rec.decision.reason_code, rec.decision.final_option) == (
        DecisionStatus.pending_approval,
        ReasonCode.INSUFFICIENT_EVIDENCE,
        None,
    )
    assert m.advisor_hold == rec.id

    # as many facts as the policy asks for: applied
    store.put("advisor_policy", _strict(bearing))
    await h.orch.validate_plan(plan.id, "alice")
    newest = max(_recommendations(store), key=lambda r: r.created_at)
    assert (newest.decision.status, newest.decision.final_option) == (
        DecisionStatus.auto_applied,
        "warm",
    )
    assert (await h.by_vm(plan.id, "vm-1")).advisor_hold is None


async def test_a_recommendation_keeps_the_similar_incidents_memory_knew(tmp_path, store):
    """ADV-02 (SDD §14.3, §14.4): at most three incidents, searched once at validation; a memory
    outage stores none, fails nothing, and is not asked again in the same validation."""
    seen = []
    hits = [
        {"title": f"incident {i}", "content": "warm cutover " * 10, "score": 0.9 - i / 10}
        for i in range(5)
    ]
    knowledge = KnowledgeService(memory_with(hits, seen), store, EventBus(store))
    advisor, _ = _tie_advisor("warm", 0.9)
    vms = [vm(1, size_gb=4, used_gb=1.0), vm(2, size_gb=4, used_gb=1.0)]
    h, plan = await setup(tmp_path, store, vms, advisor=advisor, knowledge=knowledge)
    await h.orch.validate_plan(plan.id, "alice")
    for rec in _recommendations(store):
        assert [(i.title, i.score) for i in rec.similar_incidents] == [
            ("incident 0", 0.9),
            ("incident 1", 0.8),
            ("incident 2", 0.7),
        ]

    down = []
    knowledge = KnowledgeService(memory_with([], down, fail=True), store, EventBus(store))
    advisor, _ = _tie_advisor("warm", 0.9)
    store2 = store  # same store: a new plan of other VMs
    h2, plan2 = await setup(
        tmp_path,
        store2,
        [vm(3, size_gb=4, used_gb=1.0), vm(4, size_gb=4, used_gb=1.0)],
        advisor=advisor,
        knowledge=knowledge,
    )
    report = await h2.orch.validate_plan(plan2.id, "alice")
    assert {i.phase for i in report.migrations} == {Phase.ready}
    recs = [r for r in _recommendations(store) if r.plan_id == plan2.id]
    assert len(recs) == 2 and all(r.similar_incidents == [] for r in recs)
    assert len([path for path, _ in down if path.endswith("/smart-search")]) == 1


async def test_jev_reasons_that_name_no_evidence_key_are_dropped_and_logged(
    tmp_path, store, caplog
):
    """ADV-02 (SDD §14.4): only a reason naming an evidence key of the recommendation is kept."""
    jev_settings = Settings(jev_mode="stdio")
    answer = load_fixture("decide")
    answer["recommendation"].update(
        selected="warm",
        confidence=0.9,
        reasons=[
            {"key": "downtime_estimate", "text": "The warm estimate fits the SLO."},
            {"key": "licence", "text": "Cheaper licence."},
        ],
    )
    factory, _ = session_factory(fixture_responder({"decide": answer}))
    advisor = Advisor(JevClient(jev_settings, session_factory=factory), jev_settings)
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    with caplog.at_level("WARNING"):
        await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    assert [(r.key, r.text) for r in rec.reasons] == [
        ("downtime_estimate", "The warm estimate fits the SLO.")
    ]
    assert any("licence" in message for message in caplog.messages)


def _stored_json(store: Store, kind: str, doc_id: str) -> object:
    """A document as the store wrote it, read as JSON that holds no NaN or infinity - what
    PostgreSQL's JSONB takes (SDD §11)."""
    import sqlalchemy as sa

    def refuse(name: str) -> object:
        raise ValueError(f"{name} is no JSON number")

    with store.engine.connect() as conn:
        raw = conn.execute(
            sa.text("SELECT CAST(data AS TEXT) FROM documents WHERE kind = :k AND id = :i"),
            {"k": kind, "i": doc_id},
        ).scalar_one()
    return json.loads(raw, parse_constant=refuse)


@pytest.mark.parametrize(
    ("literal", "summary"),
    [
        ("1e999", "invalid response"),
        ("-1e999", "invalid response"),
        ("1" + "0" * 400, "Boot verified"),
    ],
    ids=["1e999", "-1e999", "400-digits"],
)
async def test_a_jev_verification_number_past_a_float_never_reaches_the_store(
    tmp_path, store, literal, summary
):
    """SDD §11, §14.1 (reviews RV50-03, RV50-04): json.loads reads the literal 1e999 as an
    infinity, with no NaN or Infinity token: a verify answer whose claims carried it gave the
    migration's note the confidence inf, which SQLite kept as Infinity and PostgreSQL's JSONB
    refuses - the verifying save of the cut-over VM would fail there on every attempt. Such an
    answer is an invalid response; a claim's confidence of 400 digits, which no float holds, is
    no probability and counts for none. The cut-over VM completes, and its document is JSON
    PostgreSQL takes."""
    verify = load_fixture("verify")
    for result in verify["results"]:
        result["confidence"] = 0.25
    text = json.dumps(verify).replace('"confidence": 0.25', f'"confidence": {literal}')
    assert text.count(literal) == len(verify["results"])
    jev_settings = Settings(jev_mode="stdio", jev_deployment="on_prem")
    answers = {
        "screen": {"recommendation": {"action": "pass", "reason": "ok"}},
        "verify": lambda arguments: tool_result(text),
    }
    factory, _ = session_factory(fixture_responder(answers))
    advisor = Advisor(JevClient(jev_settings, session_factory=factory), jev_settings)
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold}, advisor=advisor
    )
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    try:
        await h.wait_phase(mid, P.completed, P.failed, timeout=5.0)
    finally:
        await h.orch.stop()
    m = await h.migration(mid)
    [note] = [n for n in m.advisor_notes if n.kind == "verification"]
    assert (m.phase, note.confidence) == (P.completed, None)
    assert summary in note.summary, note.summary
    _stored_json(store, "migration", mid)


@pytest.mark.parametrize(
    "score",
    ["NaN", "Infinity", "-Infinity", "1e999", '"nan"', '"inf"', "true", "1" + "0" * 400],
    ids=["nan", "infinity", "-infinity", "1e999", "nan-text", "inf-text", "true", "400-digits"],
)
async def test_an_agentmemory_score_that_is_no_finite_number_never_reaches_the_store(
    tmp_path, store, score
):
    """SDD §11, §14.3 (review RV50-04): agentmemory is an outside service whose answer httpx reads
    with NaN and Infinity, and a hit's score went through float() - "nan" too. The
    recommendation's similar incidents kept a NaN score, which SQLite wrote as NaN and
    PostgreSQL's JSONB refuses: there the validation failed for as long as agentmemory answered
    so. Such a score is none."""
    body = (
        '{"results": [{"title": "warm pre-copy", "content": "a warm pre-copy timed out", '
        f'"score": {score}}}]}}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("smart-search"):
            return httpx.Response(
                200, content=body.encode(), headers={"content-type": "application/json"}
            )
        return httpx.Response(201, json={"success": True})

    memory = MemoryClient("http://m", None, "p", transport=httpx.MockTransport(handler))
    advisor, _ = _answering_advisor(decide=_decide_answer("warm", 0.99))
    h, plan = await setup(tmp_path, store, [vm(1, size_gb=4, used_gb=1.0)], advisor=advisor)
    h.orch.knowledge = KnowledgeService(memory, store, h.bus)
    await h.orch.validate_plan(plan.id, "alice")
    [rec] = _recommendations(store)
    assert [i.score for i in rec.similar_incidents] == [None]
    _stored_json(store, "recommendation", rec.id)


def _write_as_rc2(store, migration: Migration, notes: list[dict]) -> None:
    """The migration as v0.1.0-rc.2 left it on SQLite: advisor notes holding numbers json wrote as
    NaN and Infinity tokens, which SQLite kept (SDD §11, review RV50-04)."""
    import sqlalchemy as sa

    from seamless_migrate.store import documents

    data = store.get("migration", migration.id, Migration).model_dump(mode="json")
    with store.engine.begin() as conn:
        conn.execute(
            sa.update(documents)
            .where(documents.c.kind == "migration", documents.c.id == migration.id)
            .values(data={**data, "advisor_notes": notes})
        )


#: the note rc.2's knowledge service attached to a failure: a hit's score as rc.2 read
#: agentmemory's "nan" (review RV50-04)
_RC2_NOTE = {
    "kind": "similar_incidents",
    "source": "memory",
    "summary": "1 similar past incident(s) found",
    "data": {
        "query": "cold cutover",
        "hits": [{"title": "t", "content": "c", "score": float("nan")}],
    },
}


async def test_a_stopped_source_an_earlier_version_stored_with_a_nan_score_is_rolled_back(
    tmp_path, store
):
    """SDD §11 (review RV51-06): a cutover failed after the stop under rc.2, whose note kept
    agentmemory's NaN score on SQLite; every write of the migration was refused since - the
    rollback, the one action that restarts the source, answered 422. The NaN reads as null."""
    h, plan = await setup(tmp_path, store, [vm(1)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    m.phase = P.failed
    m.downtime_started_at = h.orch.now() - timedelta(seconds=900)
    store.put("migration", m)
    _write_as_rc2(store, m, [_RC2_NOTE])
    rolled = await h.orch.rollback(m.id, "bayu", "restart the source")
    assert rolled.phase == P.rolling_back
    assert rolled.advisor_notes[0].data["hits"][0]["score"] is None


async def test_a_plan_holding_a_migration_an_earlier_version_stored_with_a_nan_score_validates(
    tmp_path, store
):
    """SDD §11 (review RV51-06): a validation writes its migrations in one transaction, so web-01's
    rc.2 note refused web-02's validation too, and the migration could not be cancelled out of
    the plan either."""
    h, plan = await setup(tmp_path, store, [vm(1), vm(2)])
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    _write_as_rc2(store, m, [_RC2_NOTE])
    report = await h.orch.validate_plan(plan.id, "alice")
    assert {i.phase for i in report.migrations} == {P.ready}
    cancelled = await h.orch.cancel(m.id, "alice", "remove it from the plan")
    assert cancelled.phase == P.cancelled


async def test_one_plans_error_never_stops_the_tick_of_the_others(tmp_path, store, monkeypatch):
    """SDD §8 (review RV51-06): the tick ticked the running plans one after another, so one plan's
    error - a write the store refused - failed it before every plan after, each tick, and the
    health turned unhealthy after three (GET /ready 503). Each plan is ticked on its own: the
    error is logged, plan B's VM is cut over, and the loop stays healthy."""
    first, second = vm(1), vm(2)
    h = build_harness(tmp_path, store, [first, second])
    plan_a = plan_for([first], name="A", default_strategy=Strategy.cold)
    plan_b = plan_for([second], name="B", default_strategy=Strategy.cold)
    store.put("plan", plan_a)  # created first: ticked first
    store.put("plan", plan_b)
    for plan in (plan_a, plan_b):
        await h.orch.validate_plan(plan.id, "alice")
        await h.orch.start_plan(plan.id, "alice")
    tick_plan = h.orch._tick_plan
    failed: list[str] = []

    async def failing(plan, *args):
        if plan.id == plan_a.id:
            failed.append(plan.id)
            raise UnstorableValue("migration mig-x: advisor_notes.0.confidence: is NaN")
        await tick_plan(plan, *args)

    monkeypatch.setattr(h.orch, "_tick_plan", failing)
    await h.orch.start()
    try:
        other = await h.by_vm(plan_b.id, "vm-2")
        await h.wait_phase(other.id, P.completed)
        for _ in range(5):  # ticks enough to count three errors
            h.orch.wake()
            await asyncio.sleep(0.02)
        health = h.orch.health()
    finally:
        await h.orch.stop()
    assert len(failed) >= 3 and health["healthy"], health


@pytest.mark.parametrize("action", ["sync", "rollback", "cancel", "retry"])
async def test_a_persons_action_and_its_record_are_one_write(tmp_path, store, refuse_write, action):
    """SDD §5.4 (review RV51-12): a delta pass, a rollback, a cancel and a retry wrote the
    migration, then its migration.action apart - a store error on the record left the action
    standing without it: a cancel without its cleanup (until a restart) and its retry refused.
    Both or neither: the migration is as it was, and the person's retry is taken."""
    phase = {
        "sync": P.awaiting_cutover,
        "rollback": P.failed,
        "cancel": P.precopy,
        "retry": P.failed,
    }[action]
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm})
    m = make_migration(plan_id=plan.id, vm=vm(1), strategy=Strategy.warm, phase=phase)
    store.put("migration", m)

    def act():
        return {
            "sync": lambda: h.orch.request_sync(m.id, "rina"),
            "rollback": lambda: h.orch.rollback(m.id, "rina", "restart the source"),
            "cancel": lambda: h.orch.cancel(m.id, "rina", "window closed"),
            "retry": lambda: h.orch.retry(m.id, "rina"),
        }[action]()

    refused = refuse_write(store, "INSERT INTO events", "migration.action")
    with pytest.raises(RuntimeError, match="briefly unavailable"):
        await act()
    assert refused, "the action's record was refused"
    assert (await h.migration(m.id)).phase == phase  # nothing of the action stands
    done = await act()  # the person's retry is taken
    assert done.phase != phase
    records = [
        e.data["action"]
        for e in store.events(since_seq=0, limit=1000)
        if e.kind == "migration.action" and e.migration_id == m.id
    ]
    assert records == [action]
    if action == "cancel":  # and its cleanup runs
        for _ in range(200):
            if (m.id, StepName.ROLLBACK) in h.executor.calls:
                break
            await asyncio.sleep(0.01)
        assert (m.id, StepName.ROLLBACK) in h.executor.calls
    await h.orch.stop()


async def test_a_bulk_finalize_reports_a_vm_whose_record_was_refused_as_it_stands(
    tmp_path, store, refuse_write
):
    """SDD §8.1 (review RV51-13): finalize wrote finalized, then its migration.action apart: a store
    error on the record raised after the VM was finalized, and the bulk finalize reported it
    skipped - "stays completed for a later finalize" - which no finalize could take. Both or
    neither: it stays completed, as reported, and the next finalize takes it."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    m = await h.by_vm(plan.id, "vm-1")
    refused = refuse_write(store, "INSERT INTO events", "migration.action")
    out = await h.orch.finalize_plan(plan.id, "rina", wave_id=None, confirm=plan.name)
    assert refused, "the finalize's record was refused"
    assert out["finalized"] == [] and [s["migration_id"] for s in out["skipped"]] == [m.id]
    assert (await h.migration(m.id)).phase == P.completed
    again = await h.orch.finalize_plan(plan.id, "rina", wave_id=None, confirm=plan.name)
    await h.orch.stop()
    assert again["finalized"] == [m.id]


async def test_a_retry_whose_plan_reopen_was_refused_is_reopened_by_the_tick(
    tmp_path, store, refuse_write
):
    """SDD §5.1, §8 (review RV51-14): a retry wrote ready, then reopened the completed plan in a
    second write; a store error there left the attempt waiting in ready in a plan the tick drives
    no more, and the person's retry was refused (ready). The tick reopens a completed plan
    holding a ready migration: the attempt runs."""
    h, plan, tries = _first_cutover_fails(tmp_path, store)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await h.wait_phase(mid, P.rolled_back)
    await h.wait_plan(plan.id, PlanStatus.completed)
    await h.orch.stop()  # no tick between the retry's two writes: the store refuses the retry's
    refused = refuse_write(store, "UPDATE documents", "plan")
    with pytest.raises(RuntimeError, match="briefly unavailable"):
        await h.orch.retry(mid, "alice")
    assert refused, "the plan's reopen was refused"
    stranded = ((await h.migration(mid)).phase, (await h.plan(plan.id)).status)
    assert stranded == (P.ready, PlanStatus.completed)
    await h.orch.start()  # the next tick reopens the plan
    done = await h.wait_phase(mid, P.completed)
    reopened = [e for e in store.events(since_seq=0, limit=1000) if e.kind == "plan.updated"]
    await h.orch.stop()
    assert (len(tries), done.attempts) == (2, 1)
    assert any("reopened: a migration waits in ready" in e.message for e in reopened)


async def test_the_tick_never_waits_for_a_validation_of_a_completed_plan(tmp_path, store):
    """SDD §8 (review RV52-O1): the tick reopens a completed plan holding a ready migration under
    the plan's lock. A validation of that plan held the lock while its source answered slowly, and
    the tick waited for it: every running plan's gates stalled, /ready answered 503. The tick
    leaves a plan whose lock is held to a later tick."""
    h = build_harness(tmp_path, store, [vm(1), vm(2)])
    a = plan_for([vm(1)], name="A", default_strategy=Strategy.cold, auto_cutover=False)
    b = plan_for([vm(2)], name="B", default_strategy=Strategy.cold)
    store.put("plan", a)
    store.put("plan", b)
    await h.orch.validate_plan(a.id, "alice")
    await h.orch.validate_plan(b.id, "alice")
    done = store.get("plan", a.id, Plan)
    done.status = PlanStatus.completed  # a retry left vm-1 ready in its completed plan
    store.put("plan", done)
    await h.orch.start_plan(b.id, "alice")
    await h.orch._prestage(store.get("plan", b.id, Plan), await h.migrations(b.id))
    entered, gate = asyncio.Event(), asyncio.Event()
    real_get_vm = h.source.get_vm

    async def slow_get_vm(source_id):
        if source_id == "vm-1":
            entered.set()
            await gate.wait()
        return await real_get_vm(source_id)

    h.source.get_vm = slow_get_vm
    validating = asyncio.create_task(h.orch.validate_plan(a.id, "rina"))
    await asyncio.wait_for(entered.wait(), 5)
    tick = asyncio.create_task(h.orch.tick())
    finished, _ = await asyncio.wait({tick}, timeout=2.0)
    b_phase = (await h.by_vm(b.id, "vm-2")).phase
    gate.set()
    await validating
    await tick
    await h.orch.stop()
    assert finished, f"the tick waited for A's validation; plan B's vm-2 stayed {b_phase}"
    assert b_phase != P.ready, "plan B's gate was not ticked"


async def test_the_tick_never_queues_behind_a_validation_waiting_for_a_completed_plan(
    tmp_path, store
):
    """SDD §8 (PR #58 review): an edit held completed plan A's lock while a validation of A waited
    for it; the edit released as the tick resumed from plan_phases, locked() read False, and the
    reopen queued behind the validation - the tick waited through it, as before RV52-O1. A lock a
    task waits for is busy too."""
    h = build_harness(tmp_path, store, [vm(1), vm(2)])
    a = plan_for([vm(1)], name="A", default_strategy=Strategy.cold, auto_cutover=False)
    b = plan_for([vm(2)], name="B", default_strategy=Strategy.cold)
    store.put("plan", a)
    store.put("plan", b)
    await h.orch.validate_plan(a.id, "alice")
    await h.orch.validate_plan(b.id, "alice")
    done = store.get("plan", a.id, Plan)
    done.status = PlanStatus.completed  # a retry left vm-1 ready in its completed plan
    store.put("plan", done)
    await h.orch.start_plan(b.id, "alice")
    await h.orch._prestage(store.get("plan", b.id, Plan), await h.migrations(b.id))
    gate = asyncio.Event()
    real_get_vm = h.source.get_vm

    async def slow_get_vm(source_id):
        if source_id == "vm-1":
            await gate.wait()
        return await real_get_vm(source_id)

    h.source.get_vm = slow_get_vm
    lock = h.orch.plan_lock(a.id)
    await lock.acquire()  # an edit of A ...
    validating = asyncio.create_task(h.orch.validate_plan(a.id, "rina"))  # ... a validation waits
    for _ in range(5):
        await asyncio.sleep(0)
    real_phases = h.orch.db.plan_phases

    async def plan_phases(plan_ids):
        phases = await real_phases(plan_ids)
        lock.release()  # the edit ends as the tick resumes: the validation is next in line
        return phases

    h.orch.db.plan_phases = plan_phases
    tick = asyncio.create_task(h.orch.tick())
    finished, _ = await asyncio.wait({tick}, timeout=2.0)
    b_phase = (await h.by_vm(b.id, "vm-2")).phase
    gate.set()
    await validating
    await tick
    await h.orch.stop()
    assert finished, f"the tick queued behind A's validation; plan B's vm-2 stayed {b_phase}"
    assert b_phase != P.ready


async def test_the_tick_never_waits_for_the_lock_of_a_plan_it_would_start_a_step_of(
    tmp_path, store
):
    """SDD §8 (PR #58 review): plan C ran in the tick's snapshot when a person paused it and
    validated it against a slow source; _begin waited for C's lock through the validation, and
    plan B's ready VM was not ticked. The tick leaves a busy plan to a later tick."""
    h = build_harness(tmp_path, store, [vm(1), vm(2)])
    c = plan_for([vm(1)], name="C", default_strategy=Strategy.cold)
    b = plan_for([vm(2)], name="B", default_strategy=Strategy.cold)
    store.put("plan", c)  # C first: the tick reaches it before B
    store.put("plan", b)
    for plan in (c, b):
        await h.orch.validate_plan(plan.id, "alice")
        await h.orch.start_plan(plan.id, "alice")
        await h.orch._prestage(store.get("plan", plan.id, Plan), await h.migrations(plan.id))
    lock = h.orch.plan_lock(c.id)
    await lock.acquire()  # the pause and validation of C hold its lock
    tick = asyncio.create_task(h.orch.tick())
    finished, _ = await asyncio.wait({tick}, timeout=2.0)
    phases = ((await h.by_vm(c.id, "vm-1")).phase, (await h.by_vm(b.id, "vm-2")).phase)
    lock.release()
    await tick
    await h.orch.stop()
    assert finished, f"the tick waited for plan C's lock; phases {phases}"
    assert phases[0] == P.ready and phases[1] != P.ready, phases


async def test_a_driver_that_moved_on_starts_a_new_row_of_crashes(tmp_path, store):
    """SDD §8 (review RV51-16): while a driver keeps crashing its relaunch backs off, 2^n x tick_s.
    The count was cleared only when the driver found no handler, so four crashes of the cutover's
    driver (a database blip), the cutover done, then one crash in verifying - the source still
    stopped - was crash 5: its relaunch waited 32 s at the default tick_s, 300 s after eight. A
    step that returned starts a new row."""
    h = build_harness(tmp_path, store, [vm(1)])
    plan = plan_for([vm(1)], default_strategy=Strategy.cold)
    store.put("plan", plan)
    real_cutover, real_verify = h.orch._do_cutover, h.orch._do_verify
    left = {"cutover": 4, "verify": 1}

    async def cutover(m):
        if left["cutover"]:
            left["cutover"] -= 1
            raise RuntimeError("the store was briefly unavailable")  # outside the step
        await real_cutover(m)

    async def verify(m):
        if left["verify"]:
            left["verify"] -= 1
            raise RuntimeError("the store was briefly unavailable")
        await real_verify(m)

    h.orch._do_cutover, h.orch._do_verify = cutover, verify
    await h.orch.validate_plan(plan.id, "alice")
    await h.orch.start_plan(plan.id, "alice")
    await h.orch.start()
    mid = (await h.by_vm(plan.id, "vm-1")).id
    try:
        done = await h.wait_phase(mid, P.completed, timeout=20)
    finally:
        await h.orch.stop()
    assert [str(c.to_phase) for c in done.phase_history][-3:] == [
        "cutover",
        "verifying",
        "completed",
    ]
    crashes = [
        e.data["crashes"]
        for e in store.events(since_seq=0)
        if e.kind == "migration.error" and e.data.get("step") == "driver"
    ]
    # the cutover's row, then the verifying crash starts a new one
    assert crashes == [1, 2, 3, 4, 1], crashes


async def test_a_relaunch_failing_for_the_first_migration_still_relaunches_the_next(
    tmp_path, store
):
    """SDD §8 (audit OC-01, review RV51-28): a relaunch that fails for one migration skips only it.
    The tests saw only another plan go on - started by the tick's gate, not relaunched - so a try
    around the whole relaunch loop, which skipped every migration after the first failing one,
    passed them all."""
    h = build_harness(tmp_path, store, [vm(1), vm(2)])
    first = make_migration(vm=vm(1), phase=P.cutover)
    second = make_migration(vm=vm(2), phase=P.cutover)
    store.put("migration", first)
    store.put("migration", second)
    calls: list[str] = []

    def launch(mid):
        calls.append(mid)
        if len(calls) == 1:
            raise RuntimeError("the relaunch itself fails")

    h.orch._launch = launch
    await h.orch._resume_orphans()
    assert sorted(calls) == sorted([first.id, second.id]), calls


async def test_a_retrys_action_names_the_attempts_it_wrote(tmp_path, store):
    """SDD §5.1 (review RV52-O6): a failed migration's retry wrote migration.action with the
    attempts from before the move to ready counted the new one - one less than it stored; a
    rolled-back one's, counted by the retry itself, was right. Both name what they wrote."""
    vms = [vm(1), vm(2)]
    h = build_harness(tmp_path, store, vms)
    plan = plan_for(vms, default_strategy=Strategy.cold)
    store.put("plan", plan)
    failed = make_migration(plan_id=plan.id, vm=vm(1), strategy=Strategy.cold, phase=P.failed)
    rolled = make_migration(plan_id=plan.id, vm=vm(2), strategy=Strategy.cold, phase=P.rolled_back)
    store.put("migration", failed)
    store.put("migration", rolled)
    a = await h.orch.retry(failed.id, "alice")
    b = await h.orch.retry(rolled.id, "alice")
    said = {
        e.migration_id: e.data["attempts"]
        for e in store.events(since_seq=0, limit=100)
        if e.kind == "migration.action" and e.data.get("action") == "retry"
    }
    await h.orch.stop()
    assert (a.attempts, b.attempts) == (failed.attempts + 1, rolled.attempts + 1)
    assert said == {a.id: a.attempts, b.id: b.attempts}


async def test_a_plan_cancel_keeps_its_reason_as_python_s_strip_leaves_it(tmp_path, store):
    """SDD §12 (review RV52-S3): a plan's cancel kept its reason as sent - white space only was a
    reason, padding stayed - where a migration's cancel keeps it as Python's strip leaves it, and
    the dashboard's mock said the API did."""
    reasons = {}
    for n, sent in enumerate(("  late window\u0085 ", " \u2028\u3000 ")):
        h, plan = await setup(tmp_path / str(n), store, [vm(n + 1)], {"name": f"p{n}"})
        await h.orch.validate_plan(plan.id, "alice")
        await h.orch.cancel_plan(plan.id, "rina", sent)
        [m] = await h.migrations(plan.id)
        [event] = [
            e
            for e in store.events(since_seq=0, limit=1000)
            if e.kind == "plan.cancelled" and e.plan_id == plan.id
        ]
        reasons[n] = (event.data["reason"], m.phase_history[-1].reason)
        await h.orch.stop()
    assert reasons == {
        0: ("late window", "cancelled: plan cancelled: late window"),
        1: (None, "cancelled: plan cancelled"),
    }


async def test_the_text_a_person_gives_with_an_action_is_kept_as_pythons_strip_leaves_it(
    tmp_path, store
):
    """SDD §12 (review RV51-26): an approval's comment and a cutover request's, a rollback's and
    a cancel's reason were stored as sent while the dashboard's mock stripped them, saying the
    API did - a rollback reason of white space only reached the trail from any other client.
    Each is kept as Python's strip leaves it: an empty comment or cancel reason is none, an empty
    rollback reason a 422."""
    vms = [vm(i) for i in range(1, 6)]
    h, plan = await setup(tmp_path, store, vms, {"default_strategy": Strategy.warm})
    phases = [P.awaiting_cutover, P.awaiting_cutover, P.failed, P.ready, P.ready]
    ms = [
        make_migration(plan_id=plan.id, vm=v, strategy=Strategy.warm, phase=phase)
        for v, phase in zip(vms, phases, strict=True)
    ]
    for m in ms:
        store.put("migration", m)
    approved = await h.orch.approve(ms[0].id, "rina", "  looks good\u0085 ")
    blank = await h.orch.approve(ms[1].id, "rina", " \u00a0\u0085 ")
    assert [approved.approvals[-1].comment, blank.approvals[-1].comment] == ["looks good", None]
    await h.orch.request_cutover(ms[1].id, "sari", comment="\tgo\u2003")
    with pytest.raises(Unprocessable, match="reason is required"):
        await h.orch.rollback(ms[2].id, "rina", " \u0085\u3000")
    assert (await h.migration(ms[2].id)).phase == P.failed
    rolled = await h.orch.rollback(ms[2].id, "rina", "  restart it\u0085")
    assert rolled.phase_history[-1].reason == "rollback requested: restart it"
    kept = await h.orch.cancel(ms[3].id, "rina", "  window closed ")
    none = await h.orch.cancel(ms[4].id, "rina", " \u2028 ")
    assert [kept.phase_history[-1].reason, none.phase_history[-1].reason] == [
        "cancelled: window closed",
        "cancelled: no reason given",
    ]
    actions = {
        (e.migration_id, e.data["action"]): e.data
        for e in store.events(since_seq=0, limit=1000)
        if e.kind == "migration.action"
    }
    await h.orch.stop()
    assert actions[(ms[1].id, "cutover")]["comment"] == "go"
    assert actions[(ms[3].id, "cancel")]["reason"] == "window closed"
    assert actions[(ms[4].id, "cancel")]["reason"] is None
