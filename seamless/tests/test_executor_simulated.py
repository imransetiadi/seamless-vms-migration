import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Strategy, SyncPassKind
from seamless_migrate.executors.base import PermanentStepError, StepName
from seamless_migrate.executors.simulated import SimulatedExecutor
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


async def test_simulated_time_is_compressed_by_demo_speed():
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    settings = Settings(demo=True, demo_speed=60, demo_failure_rate=0.0)
    ex = SimulatedExecutor(settings, sleep=fake_sleep)
    m = mig()
    result = await ex.run(StepName.PRECOPY, make_ctx(make_plan(), m, SRC, DST, settings)[0])
    assert sum(slept) == pytest.approx(result.sync_pass.duration_s / 60, rel=1e-6)
