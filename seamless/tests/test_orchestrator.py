import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from seamless_migrate.ai.advisor import Advisor
from seamless_migrate.ai.jev import JevClient
from seamless_migrate.ai.knowledge import KnowledgeService
from seamless_migrate.ai.memory import MemoryClient
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import Phase, PlanStatus, Strategy
from seamless_migrate.domain.models import (
    CutoverWindow,
    Migration,
    Nic,
    Plan,
    Provider,
    SyncPass,
    VerificationConfig,
    Wave,
)
from seamless_migrate.executors.base import (
    PermanentStepError,
    StepName,
    StepResult,
    TransientStepError,
)
from seamless_migrate.orchestrator import BadRequest, NotAllowed
from seamless_migrate.providers.base import ProviderError
from seamless_migrate.store import ConflictError, NotFound
from tests.factories import make_disk, make_vm
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

    async def slow_create(plan, vm_ref, strategy, actor):
        if plan.id == first.id:
            creating.set()
            await release.wait()
        return await create(plan, vm_ref, strategy, actor)

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
    assert done.actual_downtime_s == pytest.approx(
        (done.downtime_ended_at - done.downtime_started_at).total_seconds()
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
    assert len(m.approvals) == 2 and m.cutover_requested is True
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
    assert (converged, reason) == (True, "estimated final downtime 365 s is within the SLO")
    tight = plan.model_copy(update={"downtime_slo_s": 100})
    converged, reason = h.orch._converged(cbt, tight)
    assert converged is False and reason.startswith("another CBT pass: estimated downtime")
    warm = cbt.model_copy(update={"strategy": Strategy.warm})
    assert h.orch._converged(warm, plan) == (True, "converged: last pass changed 0 B")


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

    async def review(vm_ref, result):
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
    done = await h.wait_phase(m.id, P.completed)
    await h.orch.stop()
    assert h.history(done).count(P.cutover) == 1
    assert len(done.approvals) == 2
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


async def test_advisor_tie_break_recorded(tmp_path, store):
    # 1 GiB used on a 4 GiB disk: cold and warm downtimes tie (within 60 s)
    tie_vm = vm(1, size_gb=4, used_gb=1.0)
    jev_settings = Settings(jev_mode="stdio")
    decide = load_fixture("decide")
    decide["recommendation"].update(selected="warm", confidence=0.9)
    factory, session = session_factory(fixture_responder({"decide": decide}))
    advisor = Advisor(JevClient(jev_settings, session_factory=factory), jev_settings)
    h, plan = await setup(tmp_path, store, [tie_vm], advisor=advisor)
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    assert m.strategy == Strategy.warm
    [note] = [n for n in m.advisor_notes if n.kind == "strategy"]
    assert note.source == "jev" and note.data["applied"] is True
    assert note.data["deterministic"] == "cold"
    assert "advisor.strategy" in h.kinds()
    assert session.calls and session.calls[0][0] == "jev_decide"


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


async def test_verification_review_error_is_logged_not_fatal(tmp_path, store, caplog):
    h, plan = await setup(tmp_path, store, [vm(1)], {"default_strategy": Strategy.cold})
    await h.orch.validate_plan(plan.id, "alice")

    async def exploding_review(vm_ref, result):
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
    h, plan = await setup(
        tmp_path, store, [vm(1), vm(2)],
        {"default_strategy": Strategy.cold, "strategy_overrides": {"vm-1": Strategy.cold}},
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
    assert (await h.by_vm(plan.id, "vm-1-recreated")).id == m.id
    assert updated.status == PlanStatus.completed


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
