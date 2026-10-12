import asyncio

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Strategy, SyncPassKind
from seamless_migrate.domain.models import Nic
from seamless_migrate.executors.base import PermanentStepError, StepName
from seamless_migrate.executors.simulated import SimulatedExecutor
from seamless_migrate.planning.estimator import params_for_plan
from tests.executor_support import make_ctx
from tests.factories import make_disk, make_migration, make_plan, make_provider, make_vm

SRC = make_provider()
DST = make_provider(id="dst", kind=ProviderKind.rhoso, role=ProviderRole.destination)
FAST = Settings(demo=True, demo_speed=1e9, demo_failure_rate=0.0)


def mig(strategy=Strategy.warm, mid="mig-0000000001", **kw):
    vm = make_vm(disks=[make_disk(size_gb=100, used_gb=60.0)], change_rate_bps=2 * 2**20)
    return make_migration(id=mid, strategy=strategy, vm=vm, **kw)


async def test_simulated_warm_pass_deltas_shrink():
    ex = SimulatedExecutor(FAST)
    assert all(ex.supports(s) for s in Strategy)
    m = mig()
    ctx, rec = make_ctx(make_plan(), m, SRC, DST, FAST)
    first = await ex.run(StepName.PRECOPY, ctx)
    p1 = first.sync_pass
    assert (p1.number, p1.kind) == (1, SyncPassKind.full)
    assert p1.bytes_changed == p1.bytes_transferred and p1.bytes_scanned == m.vm.disk_bytes
    assert p1.bytes_changed == pytest.approx(m.vm.used_bytes, rel=0.11)
    m.sync_passes.append(p1)
    passes = [p1]
    for _ in range(2):
        ctx, _ = make_ctx(make_plan(), m, SRC, DST, FAST)
        result = await ex.run(StepName.SYNC, ctx)
        assert result.sync_pass.kind == SyncPassKind.delta
        passes.append(result.sync_pass)
        m.sync_passes.append(result.sync_pass)
    assert [p.number for p in passes] == [1, 2, 3]
    assert passes[0].bytes_changed > passes[1].bytes_changed > passes[2].bytes_changed
    # bytes_changed = min(disk, change_rate * previous duration) (with jitter)
    expected = 2 * 2**20 * passes[0].duration_s
    assert passes[1].bytes_changed == pytest.approx(expected, rel=0.11)
    assert rec.progress and rec.progress[-1][0] == 100.0
    assert rec.downtime_marks == 0, "pre-copy never stops the source"


#: in the VMware executor's words (SDD §7.2): another port holds the NIC's address
REFUSAL = (
    "the NIC 00:50:56:aa:bb:cc of dc2-portal-02 on 'DC2-Prod': port demo-port-1 holds 10.2.0.32"
)


def faulty(strategy=Strategy.vmware_warm, mid="mig-0000000009", fixed_ips=("10.2.0.32",)):
    """A demo VM tagged `demo-fault: cutover-refusal` (SDD §7.4)."""
    vm = make_vm(
        name="dc2-portal-02",
        tags={"demo-fault": "cutover-refusal"},
        nics=[Nic(network="DC2-Prod", mac="00:50:56:aa:bb:cc", fixed_ips=list(fixed_ips))],
        disks=[make_disk(size_gb=40, used_gb=10.0)],
        change_rate_bps=2**20,
    )
    return make_migration(id=mid, strategy=strategy, vm=vm)


@pytest.mark.parametrize("strategy", [Strategy.warm, Strategy.vmware_warm])
async def test_a_demo_vm_tagged_for_it_meets_what_the_cutover_will_refuse_until_its_third_pass(
    strategy,
):
    """SDD §7.4: its first two warm passes report one cutover refusal, the later ones none - the
    demo's fix while the passes run; an untagged VM's passes learn nothing of the kind."""
    ex = SimulatedExecutor(FAST)
    m = faulty(strategy)
    seen = []
    for step in (StepName.PRECOPY, StepName.SYNC, StepName.SYNC, StepName.SYNC):
        ctx, _ = make_ctx(make_plan(), m, SRC, DST, FAST)
        result = await ex.run(step, ctx)
        seen.append(result.cutover_refusals)
        m.sync_passes.append(result.sync_pass)
    assert seen == [[REFUSAL], [REFUSAL], [], []]
    ctx, _ = make_ctx(make_plan(), mig(), SRC, DST, FAST)
    assert (await ex.run(StepName.PRECOPY, ctx)).cutover_refusals is None


async def test_its_refusal_names_the_network_the_plan_maps_its_nic_to():
    """SDD §7.4, §7.2: as the executor words it - the RHOSO network the plan maps the NIC's port
    group to. A tagged VM whose NIC holds no address meets nothing another port could hold."""
    ex = SimulatedExecutor(FAST)
    plan = make_plan(mappings={"networks": {"DC2-Prod": "rhoso-prod"}})
    ctx, _ = make_ctx(plan, faulty(), SRC, DST, FAST)
    assert (await ex.run(StepName.PRECOPY, ctx)).cutover_refusals == [
        REFUSAL.replace("'DC2-Prod'", "'rhoso-prod'")
    ]
    ctx, _ = make_ctx(make_plan(), faulty(fixed_ips=()), SRC, DST, FAST)
    assert (await ex.run(StepName.PRECOPY, ctx)).cutover_refusals is None
    ctx, rec = make_ctx(make_plan(), faulty(fixed_ips=()), SRC, DST, FAST)
    assert (await ex.run(StepName.CUTOVER, ctx)).destination_server_id
    assert rec.downtime_marks == 1


async def test_its_cutover_refuses_before_the_source_stops_until_its_third_pass_ran():
    """SDD §7.4: like the destination check (§6.6), the cutover refuses before the source stops
    while the problem lasts; after the third pass it cuts over. Migrated cold it is not refused."""
    ex = SimulatedExecutor(FAST)
    m = faulty()
    for step in (StepName.PRECOPY, StepName.SYNC):
        ctx, _ = make_ctx(make_plan(), m, SRC, DST, FAST)
        m.sync_passes.append((await ex.run(step, ctx)).sync_pass)
    ctx, rec = make_ctx(make_plan(), m, SRC, DST, FAST)
    with pytest.raises(PermanentStepError) as exc:
        await ex.run(StepName.CUTOVER, ctx)
    assert str(exc.value) == REFUSAL
    assert rec.downtime_marks == 0
    ctx, _ = make_ctx(make_plan(), m, SRC, DST, FAST)
    m.sync_passes.append((await ex.run(StepName.SYNC, ctx)).sync_pass)
    ctx, rec = make_ctx(make_plan(), m, SRC, DST, FAST)
    result = await ex.run(StepName.CUTOVER, ctx)
    assert rec.downtime_marks == 1 and result.destination_server_id
    ctx, rec = make_ctx(make_plan(), faulty(Strategy.cold), SRC, DST, FAST)
    assert (await ex.run(StepName.CUTOVER, ctx)).destination_server_id
    assert rec.downtime_marks == 1


async def test_simulated_cutover_marks_downtime():
    ex = SimulatedExecutor(FAST)
    m = mig()
    m.sync_passes.append(
        (await ex.run(StepName.PRECOPY, make_ctx(make_plan(), m, SRC, DST, FAST)[0])).sync_pass
    )
    ctx, rec = make_ctx(make_plan(), m, SRC, DST, FAST)
    result = await ex.run(StepName.CUTOVER, ctx)
    assert rec.downtime_marks == 1
    assert result.destination_server_id
    assert result.sync_pass.kind == SyncPassKind.final and result.sync_pass.number == 2
    for strategy in (Strategy.cold, Strategy.storage_handover, Strategy.vmware_cold):
        ctx, rec = make_ctx(make_plan(), mig(strategy), SRC, DST, FAST)
        result = await ex.run(StepName.CUTOVER, ctx)
        assert rec.downtime_marks == 1 and result.destination_server_id
    rb, rec = make_ctx(make_plan(), mig(), SRC, DST, FAST)
    assert (await ex.run(StepName.ROLLBACK, rb)).details["source_running"] is True
    assert (await ex.run(StepName.FINALIZE, rb)).details == {"finalized": True}


async def test_simulated_failure_rate_seeded():
    settings = Settings(demo=True, demo_speed=1e9, demo_failure_rate=0.5, demo_seed=7)

    async def outcomes(executor):
        out = []
        for i in range(40):
            ctx, rec = make_ctx(
                make_plan(), mig(Strategy.cold, mid=f"mig-{i:010d}"), SRC, DST, settings
            )
            try:
                await executor.run(StepName.CUTOVER, ctx)
                out.append(False)
            except PermanentStepError:
                out.append(True)
                assert rec.downtime_marks == 1, "failures happen after the source stopped"
        return out

    first = await outcomes(SimulatedExecutor(settings))
    again = await outcomes(SimulatedExecutor(settings))
    assert first == again, "failures are reproducible for a given seed"
    assert 8 <= sum(first) <= 32
    other_seed = await outcomes(SimulatedExecutor(settings.model_copy(update={"demo_seed": 8})))
    assert other_seed != first

    never = Settings(demo=True, demo_speed=1e9, demo_failure_rate=0.0)
    always = Settings(demo=True, demo_speed=1e9, demo_failure_rate=1.0)
    assert not any(await outcomes(SimulatedExecutor(never)))
    assert all(await outcomes(SimulatedExecutor(always)))
    # a retried migration (attempts + 1) gets a fresh draw
    m = mig(Strategy.cold, mid="mig-retry00001", attempts=1)
    ctx, _ = make_ctx(make_plan(), m, SRC, DST, never)
    await SimulatedExecutor(never).run(StepName.CUTOVER, ctx)


async def test_a_simulated_step_reports_the_model_seconds_it_kept_the_source_stopped():
    """SDD §5.2/§7.4: a failed cutover reports its model seconds up to the failure, in its
    PermanentStepError's details; the rollback reports its own 90 s."""
    never = Settings(demo=True, demo_speed=1e9, demo_failure_rate=0.0)
    always = Settings(demo=True, demo_speed=1e9, demo_failure_rate=1.0)
    ctx, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, never)
    whole = (await SimulatedExecutor(never).run(StepName.CUTOVER, ctx)).details
    copy = whole["downtime_model_s"] - params_for_plan(make_plan()).create_s
    ctx, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, always)
    failing = SimulatedExecutor(always)
    with pytest.raises(PermanentStepError) as failed:
        await failing.run(StepName.CUTOVER, ctx)
    # the same seeded copy, failed half-way through and before the create
    assert failed.value.details == {"downtime_model_s": pytest.approx(copy / 2)}
    # the failure reported them: the rollback after it counts only its own 90 s
    rb, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, always)
    assert (await failing.run(StepName.ROLLBACK, rb)).details["downtime_model_s"] == 90.0
    rb, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, never)
    assert (await SimulatedExecutor(never).run(StepName.ROLLBACK, rb)).details == {
        "source_running": True,
        "downtime_model_s": 90.0,
    }
    assert PermanentStepError("no details").details == {}


async def test_a_rollback_reports_the_model_seconds_of_the_cutover_it_interrupted():
    """SDD §5.2/§7.4 (ADV-16..20 audit, F8): a rollback cancels the running cutover, which then
    reports nothing; the rollback reports the model seconds that cutover kept the source stopped,
    with its own 90 s, once."""
    never = Settings(demo=True, demo_speed=1e9, demo_failure_rate=0.0)
    ctx, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, never)
    full = SimulatedExecutor(never)
    whole = (await full.run(StepName.CUTOVER, ctx)).details
    copy = whole["downtime_model_s"] - params_for_plan(make_plan()).create_s
    # a cutover that ran to its end reported its own: a later rollback counts only its 90 s
    rb, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, never)
    assert (await full.run(StepName.ROLLBACK, rb)).details["downtime_model_s"] == 90.0
    calls = 0
    blocked = asyncio.Event()

    async def sleep(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:  # the shutdown, the copy, then the create: the rollback cancels it here
            blocked.set()
            await asyncio.Event().wait()

    ex = SimulatedExecutor(never, sleep=sleep)
    ctx, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, never)
    cutover = asyncio.create_task(ex.run(StepName.CUTOVER, ctx))
    await blocked.wait()
    cutover.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cutover
    rb, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, never)
    assert (await ex.run(StepName.ROLLBACK, rb)).details["downtime_model_s"] == pytest.approx(
        copy + 90.0
    )
    # reported once: another rollback of the migration counts only its own
    assert (await ex.run(StepName.ROLLBACK, rb)).details["downtime_model_s"] == 90.0


async def test_simulated_time_is_compressed_by_demo_speed():
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    settings = Settings(demo=True, demo_speed=60, demo_failure_rate=0.0)
    ex = SimulatedExecutor(settings, sleep=fake_sleep)
    m = mig()
    result = await ex.run(StepName.PRECOPY, make_ctx(make_plan(), m, SRC, DST, settings)[0])
    assert sum(slept) == pytest.approx(result.sync_pass.duration_s / 60, rel=1e-6)


async def test_a_simulated_final_sync_starts_once_the_source_is_stopped():
    """ADV-16 (SDD §7.4): the final pass is recorded after the stop, like a real cutover's."""
    slow = Settings(
        demo=True, demo_speed=1e4, demo_failure_rate=0.0
    )  # 60 model s of shutdown = 6 ms
    ex = SimulatedExecutor(slow)
    m = mig()
    m.sync_passes.append(
        (await ex.run(StepName.PRECOPY, make_ctx(make_plan(), m, SRC, DST, slow)[0])).sync_pass
    )
    ctx, rec = make_ctx(make_plan(), m, SRC, DST, slow)
    final = (await ex.run(StepName.CUTOVER, ctx)).sync_pass
    assert final.kind == SyncPassKind.final
    assert final.started_at >= rec.downtime_at[0]
