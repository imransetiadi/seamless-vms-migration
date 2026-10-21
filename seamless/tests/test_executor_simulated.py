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
    # bytes_changed = min(disk, change_rate * the previous pass's whole time, its snapshot
    # included - a warm pass records the part after it (SDD §7.4, review RV38A-05)) (with jitter)
    expected = 2 * 2**20 * (passes[0].duration_s + params_for_plan(make_plan()).snapshot_s)
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
    """SDD §5.2/§7.4: a cutover's clock starts when it stops the source, so it reports the
    shutdown, the copy and the create - the parts its estimate has; a failed cutover reports its
    model seconds up to the failure, in its PermanentStepError's details; the rollback reports
    its own 90 s."""
    never = Settings(demo=True, demo_speed=1e9, demo_failure_rate=0.0)
    always = Settings(demo=True, demo_speed=1e9, demo_failure_rate=1.0)
    p = params_for_plan(make_plan())
    ctx, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, never)
    whole = await SimulatedExecutor(never).run(StepName.CUTOVER, ctx)
    copy = whole.sync_pass.duration_s
    assert whole.details["downtime_model_s"] == pytest.approx(p.shutdown_s + copy + p.create_s)
    ctx, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, always)
    failing = SimulatedExecutor(always)
    with pytest.raises(PermanentStepError) as failed:
        await failing.run(StepName.CUTOVER, ctx)
    # the same seeded copy, failed half-way through and before the create
    assert failed.value.details == {"downtime_model_s": pytest.approx(p.shutdown_s + copy / 2)}
    # the failure reported them: the rollback after it counts only its own 90 s
    rb, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, always)
    assert (await failing.run(StepName.ROLLBACK, rb)).details["downtime_model_s"] == 90.0
    rb, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, never)
    assert (await SimulatedExecutor(never).run(StepName.ROLLBACK, rb)).details == {
        "source_running": True,
        "downtime_model_s": 90.0,
    }
    assert PermanentStepError("no details").details == {}


async def test_a_simulated_cutover_starts_its_clock_before_the_shutdown():
    """SDD §5.2, §7.4: the clock starts when the cutover stops the source - before the shutdown
    elapses, as an Ansible-driven cutover's starts with the source-stop task (§7.2) - so the
    running clock and the model seconds count the same downtime."""
    never = Settings(demo=True, demo_speed=1e9, demo_failure_rate=0.0)
    ctx, rec = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, never)
    marks_at_each_sleep: list[int] = []

    async def sleep(seconds: float) -> None:
        marks_at_each_sleep.append(rec.downtime_marks)

    await SimulatedExecutor(never, sleep=sleep).run(StepName.CUTOVER, ctx)
    # the first sleep is the shutdown's
    assert marks_at_each_sleep[0] == 1, marks_at_each_sleep


async def test_a_rollback_reports_the_model_seconds_of_the_cutover_it_interrupted():
    """SDD §5.2/§7.4 (ADV-16..20 audit, F8): a rollback cancels the running cutover, which then
    reports nothing; the rollback reports the model seconds that cutover kept the source stopped,
    with its own 90 s, once."""
    never = Settings(demo=True, demo_speed=1e9, demo_failure_rate=0.0)
    ctx, _ = make_ctx(make_plan(), mig(Strategy.cold), SRC, DST, never)
    full = SimulatedExecutor(never)
    copy = (await full.run(StepName.CUTOVER, ctx)).sync_pass.duration_s
    shutdown = params_for_plan(make_plan()).shutdown_s
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
    # the cutover kept the source stopped through its shutdown and its copy
    assert (await ex.run(StepName.ROLLBACK, rb)).details["downtime_model_s"] == pytest.approx(
        shutdown + copy + 90.0
    )
    # reported once: another rollback of the migration counts only its own
    assert (await ex.run(StepName.ROLLBACK, rb)).details["downtime_model_s"] == 90.0


@pytest.mark.parametrize("sizes_gb", [[20], [20, 200]], ids=["20 GiB", "20+200 GiB"])
async def test_a_simulated_delta_pass_reads_back_the_scan_rate_it_ran_with(sizes_gb):
    """SDD §7.4, §9.1 (review RV38A-05): a simulated warm pass takes the model's
    snapshot + max(scan, Δ/L), its scan as §9.1 models it, and records the part after the snapshot
    as the collection's warm sync does: the calibration reads back the plan's S, and the
    final-downtime estimate after it is the one the plan's S gives."""
    from seamless_migrate.planning.estimator import estimate_final_downtime, observed_scan_rate

    disks = [make_disk(id=f"d{i}", size_gb=gb, used_gb=gb / 2) for i, gb in enumerate(sizes_gb)]
    m = make_migration(vm=make_vm(disks=disks, change_rate_bps=2 * 2**20))
    plan = make_plan()
    ex = SimulatedExecutor(FAST)
    for step in (StepName.PRECOPY, StepName.SYNC):
        ctx, _ = make_ctx(plan, m, SRC, DST, FAST)
        m.sync_passes.append((await ex.run(step, ctx)).sync_pass)
    delta = m.sync_passes[-1]
    params = params_for_plan(plan)
    assert observed_scan_rate(delta, m.vm, params) == pytest.approx(params.scan_bps)
    assert estimate_final_downtime(m.vm, Strategy.warm, delta.duration_s, params) > 0


@pytest.mark.parametrize("writes_mib", [100, 10], ids=["its delta sets the copy", "its scan does"])
async def test_a_simulated_warm_cutover_takes_the_downtime_the_gate_estimates(
    monkeypatch, writes_mib
):
    """SDD §7.4, §9.1 (reviews RV38A-05, RV39-05, RV39-07): the cutover's final pass of a warm
    migration copies what the guest wrote through the last pass's whole time, takes the model's
    snapshot + max(scan, Δf/L) - four disks scanned in parallel - and records the part after its
    snapshot; with no jitter the source then stays down exactly as long as the final-downtime
    estimate after that pass's whole time says."""
    from seamless_migrate.planning.estimator import estimate_final_downtime, scan_seconds

    monkeypatch.setattr(SimulatedExecutor, "_jitter", lambda self, rng: 1.0)
    disks = [make_disk(id=f"d{i}", size_gb=100, used_gb=50.0) for i in range(4)]
    m = make_migration(vm=make_vm(disks=disks, change_rate_bps=writes_mib * 2**20))
    plan = make_plan()
    p = params_for_plan(plan)
    ex = SimulatedExecutor(FAST)
    for step in (StepName.PRECOPY, StepName.SYNC):
        ctx, _ = make_ctx(plan, m, SRC, DST, FAST)
        m.sync_passes.append((await ex.run(step, ctx)).sync_pass)
    whole = m.sync_passes[-1].duration_s + p.snapshot_s
    ctx, _ = make_ctx(plan, m, SRC, DST, FAST)
    result = await ex.run(StepName.CUTOVER, ctx)
    final = result.sync_pass
    delta = min(m.vm.disk_bytes, m.vm.change_rate_bps * whole)
    assert final.bytes_changed == int(delta)
    assert final.duration_s == pytest.approx(max(scan_seconds(m.vm, p), delta / p.link_bps))
    stopped = result.details["downtime_model_s"]
    assert stopped == pytest.approx(p.shutdown_s + p.snapshot_s + final.duration_s + p.create_s)
    assert stopped + p.boot_s == pytest.approx(
        estimate_final_downtime(m.vm, Strategy.warm, whole, p)
    )


#: what the demo's A100-4C vGPU flavor asks for: one vGPU of the type its trait names (SDD §22.10)
A100_4C = {"resources:VGPU": "1", "trait:CUSTOM_NVIDIA_A100_4C": "required"}
#: the free units of the demo destination's three pools, by pool name, before any server (§22.10)
ROOM = {"VGPU CUSTOM_NVIDIA_A100_4C": 4, "VGPU CUSTOM_NVIDIA_A100_3_20C": 2, "VGPU": 6}


def demo_destination():
    """The demo's rhoso18 as the demo registry builds it, the registry with it (SDD §10)."""
    from seamless_migrate.demo import DEMO_PROVIDERS
    from seamless_migrate.providers.registry import ProviderRegistry

    rhoso = next(p for p in DEMO_PROVIDERS if p.id == "rhoso18")
    registry = ProviderRegistry(FAST)
    return rhoso, registry, registry.get(rhoso)


async def gpu_free(fake) -> dict[str, int]:
    """The free units of each pool the fake destination's inventory reads, by pool name (§22.3)."""
    from seamless_migrate.planning.gpu import pool_name

    capacity = (await fake.inventory()).gpu_capacity
    return {
        pool_name(p.resource_class, p.traits, p.forbidden_traits): p.free for p in capacity.pools
    }


async def test_a_simulated_gpu_cutover_takes_its_units_and_its_rollback_gives_them_back():
    """SDD §7.4, §10, §22.10 (plan task G9): a simulated cutover creates its server on the demo's
    fake destination with the VM's GPU flavor, which takes one unit of the A100-4C pool and of its
    class - the check reads them taken, as the provider card does -, and the server reports that
    flavor and holds that unit. The rollback deletes the server before it starts the source
    again, and the units are free once more."""
    from seamless_migrate.planning.gpu import gpu_requests
    from seamless_migrate.providers.base import ProviderError

    rhoso, registry, fake = demo_destination()
    vm = make_vm(flavor="g1.vgpu", flavor_extra_specs=A100_4C, gpus=gpu_requests(A100_4C))
    plan = make_plan(mappings={"flavors": {"g1.vgpu": "g1.a100-4c"}})
    m = make_migration(id="mig-000000000a", strategy=Strategy.cold, vm=vm)
    ex = SimulatedExecutor(FAST, providers=registry)
    assert await gpu_free(fake) == ROOM
    ctx, _ = make_ctx(plan, m, SRC, rhoso, FAST)
    server = (await ex.run(StepName.CUTOVER, ctx)).destination_server_id
    assert await gpu_free(fake) == {**ROOM, "VGPU CUSTOM_NVIDIA_A100_4C": 3, "VGPU": 5}
    assert [p["free"] for p in (await fake.check())["gpu_capacity"]["pools"]] == [3, 2, 5]
    flavor = (await fake.get_server(server))["flavor"]
    assert flavor == {"name": "g1.a100-4c", "extra_specs": A100_4C}
    assert await fake.server_allocations(server) == {"VGPU": 1}

    started: list = []

    async def power_on(source_id: str) -> None:
        # the server is gone before the source starts again, as §7.2's rollback does
        started.append(await fake.server_allocations(server))

    registry.get(SRC).power_on = power_on
    m.destination_server_id = server
    ctx, _ = make_ctx(plan, m, SRC, rhoso, FAST)
    await ex.run(StepName.ROLLBACK, ctx)
    assert started == [None]
    assert await gpu_free(fake) == ROOM
    with pytest.raises(ProviderError):
        await fake.get_server(server)


async def test_a_simulated_cutover_that_finds_no_gpu_free_fails_after_the_source_stopped():
    """SDD §7.4, §10, §22.7 (plan task G9): a VM without a GPU that the plan's flavor mapping sends
    to a vGPU flavor takes its units as a GPU VM does - the pre-flight and the gate count it so -;
    once its pool has none free, the create fails as Nova finds no valid host: after the source
    stopped, with its model seconds, naming the flavor and the pool, and no server is recorded."""
    rhoso, registry, fake = demo_destination()
    ex = SimulatedExecutor(FAST, providers=registry)
    plan = make_plan(mappings={"flavors": {"m1.small": "g1.a100-3-20c"}})
    for mid in ("mig-00000000b1", "mig-00000000b2"):
        ctx, _ = make_ctx(plan, mig(Strategy.cold, mid=mid), SRC, rhoso, FAST)
        await ex.run(StepName.CUTOVER, ctx)
    assert await gpu_free(fake) == {**ROOM, "VGPU CUSTOM_NVIDIA_A100_3_20C": 0, "VGPU": 4}
    ctx, rec = make_ctx(plan, mig(Strategy.cold, mid="mig-00000000b3"), SRC, rhoso, FAST)
    with pytest.raises(PermanentStepError) as failed:
        await ex.run(StepName.CUTOVER, ctx)
    assert "'g1.a100-3-20c'" in str(failed.value)
    assert "VGPU CUSTOM_NVIDIA_A100_3_20C" in str(failed.value)
    assert rec.downtime_marks == 1 and failed.value.details["downtime_model_s"] > 0
    assert await fake.server_allocations("sim-mig-00000000b3") is None
    assert await gpu_free(fake) == {**ROOM, "VGPU CUSTOM_NVIDIA_A100_3_20C": 0, "VGPU": 4}
    # a VM whose flavor no mapping names takes nothing
    ctx, _ = make_ctx(make_plan(), mig(Strategy.cold, mid="mig-00000000b4"), SRC, rhoso, FAST)
    server = (await ex.run(StepName.CUTOVER, ctx)).destination_server_id
    assert await fake.server_allocations(server) is None
    assert (await fake.get_server(server))["flavor"] == {"name": None, "extra_specs": {}}


async def test_simulated_time_is_compressed_by_demo_speed():
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    settings = Settings(demo=True, demo_speed=60, demo_failure_rate=0.0)
    ex = SimulatedExecutor(settings, sleep=fake_sleep)
    m = mig()
    result = await ex.run(StepName.PRECOPY, make_ctx(make_plan(), m, SRC, DST, settings)[0])
    # the pass's whole time: its snapshot and the part after it, which it records (SDD §7.4)
    whole = result.sync_pass.duration_s + params_for_plan(make_plan()).snapshot_s
    assert sum(slept) == pytest.approx(whole / 60, rel=1e-6)


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
