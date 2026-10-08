import asyncio
from datetime import timedelta

import httpx
import pytest

from seamless_migrate.ai.advisor import Advisor
from seamless_migrate.ai.jev import JevClient
from seamless_migrate.ai.knowledge import KnowledgeService
from seamless_migrate.ai.memory import MemoryClient
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import Phase, PlanStatus, Strategy
from seamless_migrate.domain.models import CutoverWindow, Nic, Plan, VerificationConfig, Wave
from seamless_migrate.executors.base import (
    PermanentStepError,
    StepName,
    StepResult,
    TransientStepError,
)
from seamless_migrate.orchestrator import BadRequest, NotAllowed
from seamless_migrate.store import NotFound
from tests.factories import make_disk, make_vm
from tests.jev_fakes import fixture_responder, load_fixture, session_factory
from tests.orch_support import (
    DST_PROVIDER,
    SRC_PROVIDER,
    Clock,
    ScriptedExecutor,
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
    assert out.cutover_requested and out.approvals[-1].comment == "emergency"
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
    await run_plan(h, plan)
    m = await h.wait_phase((await h.by_vm(plan.id, "vm-1")).id, P.rolled_back)
    await h.orch.stop()
    assert h.history(m)[-5:] == [P.cutover, P.verifying, P.failed, P.rolling_back, P.rolled_back]
    assert "server_active" in m.error


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
    while not started:
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
    while cloud["stops"] == 0:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
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
