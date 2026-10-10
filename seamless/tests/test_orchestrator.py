import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from seamless_migrate.ai.advisor import Advisor
from seamless_migrate.ai.jev import JevClient
from seamless_migrate.ai.knowledge import KnowledgeService
from seamless_migrate.ai.memory import MemoryClient
from seamless_migrate.ai.snapshots import canonical_json
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import DecisionStatus, Phase, PlanStatus, ReasonCode, Strategy
from seamless_migrate.domain.models import (
    AdvisorNote,
    AdvisorPolicy,
    AdvisorRecommendation,
    AdvisorSnapshot,
    AppProbe,
    Approval,
    CutoverWindow,
    Event,
    ForceCutover,
    Migration,
    Nic,
    Plan,
    PolicyScope,
    Provider,
    SyncPass,
    VerificationConfig,
    Wave,
    next_pass_number,
    utcnow,
)
from seamless_migrate.events import AUDIT_KINDS, EventBus
from seamless_migrate.executors.base import (
    PermanentStepError,
    StepName,
    StepResult,
    TransientStepError,
)
from seamless_migrate.orchestrator import (
    VM_CLAIMS,
    BadRequest,
    NotAllowed,
    Unprocessable,
    counted_approvals,
)
from seamless_migrate.planning.estimator import params_for_plan
from seamless_migrate.providers.base import ProviderError
from seamless_migrate.store import ConflictError, NotFound
from tests.factories import make_disk, make_migration, make_vm
from tests.jev_fakes import fixture_responder, load_fixture, session_factory
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


async def test_validate_creates_migrations_with_findings_and_estimates(tmp_path, store):
    gpu = vm(3, "gpu-01", flavor_extra_specs={"resources:VGPU": "1"})
    vms = [vm(1), vm(2, "db-02", size_gb=600, used_gb=300.0), gpu]
    h, plan = await setup(tmp_path, store, vms)
    report = await h.orch.validate_plan(plan.id, "alice")
    assert report.plan_id == plan.id and report.ok is False
    assert len(report.migrations) == 3
    by_name = {item.vm_name: item for item in report.migrations}
    assert by_name["gpu-01"].phase == P.blocked
    assert [f.code for f in by_name["gpu-01"].findings] == ["VM_VGPU"]
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


def _park_vms(h, monkeypatch):
    """Make the orchestrator's next source inventory reads wait until released."""
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.orch._vms

    async def parked(plan, impl):
        entered.set()
        await release.wait()
        return await original(plan, impl)

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


async def test_a_validation_takes_over_a_migration_an_interrupted_one_left_validating(
    tmp_path, store
):
    """SDD §5.4: a validation whose second write failed (a database error, a restart) left a
    migration in validating; the next validation of the plan re-assesses it like a ready one, so
    the plan can start and the VM is not held for good."""
    h, plan = await setup(tmp_path, store, [vm(1), vm(2)])
    transition = h.orch._transition
    failed: list[str] = []

    async def failing_transition(m, version, to, reason, actor="system", data=None):
        if to in (P.ready, P.blocked) and m.vm.source_id == "vm-2" and not failed:
            failed.append(m.id)
            raise ConnectionError("database connection lost")
        return await transition(m, version, to, reason, actor, data)

    h.orch._transition = failing_transition
    with pytest.raises(ConnectionError):
        await h.orch.validate_plan(plan.id, "alice")
    stuck = await h.by_vm(plan.id, "vm-2")
    assert stuck.phase == P.validating

    report = await h.orch.validate_plan(plan.id, "alice")
    assert {item.migration_id: item.phase for item in report.migrations}[stuck.id] == P.ready
    assert (await h.migration(stuck.id)).phase == P.ready
    assert (await h.plan(plan.id)).status == PlanStatus.validated


async def _left_validating(tmp_path, store):
    """A plan of two VMs whose first validation failed between vm-2's two writes (SDD §5.4)."""
    h, plan = await setup(tmp_path, store, [vm(1), vm(2)])
    transition = h.orch._transition
    failed: list[str] = []

    async def failing_transition(m, version, to, reason, actor="system", data=None):
        if to in (P.ready, P.blocked) and m.vm.source_id == "vm-2" and not failed:
            failed.append(m.id)
            raise ConnectionError("database connection lost")
        return await transition(m, version, to, reason, actor, data)

    h.orch._transition = failing_transition
    with pytest.raises(ConnectionError):
        await h.orch.validate_plan(plan.id, "alice")
    stuck = await h.by_vm(plan.id, "vm-2")
    assert stuck.phase == P.validating
    return h, plan, stuck


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
    """A stored plan with link_bps = inf (written before the API check) still loads, and
    validation refuses it instead of estimating every transfer as instant (SDD §9.1)."""
    from seamless_migrate.domain.models import Plan

    h, plan = await setup(tmp_path, store, [vm(1)])
    current = store.get("plan", plan.id, Plan)
    current.link_bps = float("inf")
    store.put("plan", current)
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
    """Plans validated before the rule (SDD §5.4) may hold one VM several times, under one name:
    the refusal counts VMs, not migrations, and names each holder once."""
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
    assert str(refused.value).startswith(
        '2 VM(s) already have a migration in another plan: web-01 (plan "Finance", ready), '
        'web-02 (plan "Finance", ready); '
    )


async def test_cross_plan_refusal_says_how_many_holders_it_leaves_out(tmp_path, store):
    vms = [vm(i) for i in range(1, 13)]
    h, first = await setup(tmp_path, store, vms)
    await h.orch.validate_plan(first.id, "alice")
    second = plan_for(vms, name="Second wave")
    store.put("plan", second)

    with pytest.raises(
        NotAllowed, match=r"^12 VM\(s\) .*web-10 \(plan \"Finance\", ready\) and 2 more; "
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
    await h.orch.stop()
    assert len(tries) == 2 and m.attempts == 1
    reopened = [
        e
        for e in store.events(since_seq=0, limit=1000)
        if e.kind == "plan.updated" and e.data.get("status") == "running"
    ]
    assert len(reopened) == 1 and reopened[0].actor == "alice" and "web-01" in reopened[0].message
    assert h.kinds().count("plan.completed") == 2


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


async def test_warm_flow_reaches_completed_with_downtime(tmp_path, store):
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.warm, "downtime_slo_s": 300}
    )
    await run_plan(h, plan)
    m = await h.by_vm(plan.id, "vm-1")
    done = await h.wait_phase(m.id, P.completed)
    await h.wait_plan(plan.id, PlanStatus.completed)
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
    still recorded: a migration.error naming the manual rollback_workloads.yml run."""
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
    assert "interrupted" in errors[0].message and "rollback_workloads.yml" in errors[0].message


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


async def test_no_decision_on_a_recommendation_the_plan_overrides_and_no_second_approve(
    tmp_path, store
):
    """SDD §14.6 (advisor Phase 1 audit, A4): once the plan sets the VM's strategy, the newest
    recommendation no longer stands - approving it would flip the strategy back against the plan;
    and an approved decision is not approved again (one event per decision status change)."""
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


async def test_similar_incidents_attached_on_failure(tmp_path, store):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("smart-search"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Attach timeout on conv host",
                            "content": "Raise os_migrate_timeout",
                            "score": 0.9,
                        }
                    ]
                },
            )
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
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.failed)
    for _ in range(100):
        m = await h.migration(m.id)
        if any(n.kind == "similar_incidents" for n in m.advisor_notes):
            break
        await asyncio.sleep(0.01)
    await h.orch.stop()
    [note] = [n for n in m.advisor_notes if n.kind == "similar_incidents"]
    assert note.source == "memory" and note.data["hits"][0]["title"].startswith("Attach")
    assert "advisor.similar_incidents" in h.kinds()
    assert seen.count("/agentmemory/remember") == 1


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
    h, plan = await setup(
        tmp_path, store, [vm(1), vm(2)],
        {"default_strategy": Strategy.cold, "strategy_overrides": {"vm-1": Strategy.cold},
         "probe_overrides": {"vm-1": [probe]}},
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
    lost for good."""
    settings = make_settings(tmp_path)
    h, plan = await setup(
        tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold},
        executor=_recreating_executor(settings), settings=settings,
    )  # fmt: skip
    put = h.orch.db.put
    seen: list[Phase] = []

    async def watching_put(kind, model, expected_version=None):
        version = await put(kind, model, expected_version)
        if kind == "plan" and "vm-1-recreated" in model.vm_ids and not seen:
            # the phase the migration had when its plan first named the recreated VM
            seen.extend(m.phase for m in await h.migrations(plan.id))
        return version

    h.orch.db.put = watching_put
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.orch.stop()
    assert seen == [P.rolling_back]
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
    assert done.downtime_model_s == pytest.approx(copy + params.create_s)
    assert done.actual_downtime_s == pytest.approx(copy + params.create_s + params.boot_s)
    wall = (done.downtime_ended_at - done.downtime_started_at).total_seconds()
    assert done.actual_downtime_s > wall, "the simulator compresses wall time"


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


async def test_the_source_watch_flags_a_completed_vms_source_running_again(tmp_path, store):
    """SDD §8.1: the source VM of a migration awaiting finalize runs again: its data diverges."""
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    m = await h.by_vm(plan.id, "vm-1")
    h.source.vms["vm-1"] = h.source.vms["vm-1"].model_copy(update={"power_state": "stopped"})

    async def watch(seconds=h.settings.source_watch_s + 1):
        h.clock.advance(seconds)
        await h.orch.tick()
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


async def test_the_source_watch_skips_a_source_the_provider_no_longer_has(tmp_path, store):
    h, plan = await _completed_plan(tmp_path, store, n=1, waves=[])
    del h.source.vms["vm-1"]  # finalized with delete_source, or rebuilt by a handover
    h.clock.advance(h.settings.source_watch_s + 1)
    await h.orch.tick()
    assert (await h.by_vm(plan.id, "vm-1")).source_running_at is None


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
