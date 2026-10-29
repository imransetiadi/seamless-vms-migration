"""Simulated executor for demo mode and tests (SDD §7.4).

Durations follow the estimator model (§9.1) in *model seconds* (recorded in
``SyncPass.duration_s``); wall-clock time is compressed by ``settings.demo_speed``. Byte counts
carry seeded jitter; each pass changes ``min(disk_bytes, change_rate · previous_pass_duration)``.
Cutover fails with probability ``settings.demo_failure_rate`` after the source was stopped. A
cutover creates its server on the fake destination with the flavor the plan names, where it takes
that flavor's vGPU units; the rollback deletes it (§7.4, §10, §22.10).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import Settings
from ..credentials import Step, for_step
from ..domain.enums import WARM_STRATEGIES, Strategy, SyncPassKind
from ..domain.models import (
    ExtentUse,
    Migration,
    Plan,
    Provider,
    SyncPass,
    VMRef,
    next_pass_number,
    utcnow,
)
from ..planning.estimator import (
    VMWARE_PASS_OVERHEAD_S,
    final_copy_seconds,
    params_for_plan,
    scan_seconds,
    whole_pass_seconds,
)
from ..planning.gpu import gpu_flavor, vm_gpus
from ..providers.base import ProviderError
from .base import PermanentStepError, StepContext, StepName, StepResult

Sleep = Callable[[float], Awaitable[None]]
JITTER = 0.1
PROGRESS_TICK_WALL_S = 0.5
MAX_PROGRESS_TICKS = 40
#: model seconds a simulated rollback takes, the source starting again at its end
ROLLBACK_MODEL_S = 90.0


#: SDD §7.4: a VM tagged ``demo-fault: cutover-refusal`` migrated warm meets, on its first passes, a
#: problem the cutover refuses before the source stops - fixed before its third pass
CUTOVER_REFUSAL_FAULT = "cutover-refusal"
REFUSING_PASSES = 2


def _demo_refusal(m: Migration, plan: Plan) -> str | None:
    """What the demo VM tagged for it meets on its warm passes, as the VMware executor words it
    (SDD §7.4, §7.2): another port of the RHOSO network the plan maps its NIC to holds the NIC's
    address. None for any other migration, and when no NIC holds an address."""
    vm = m.vm
    if vm.tags.get("demo-fault") != CUTOVER_REFUSAL_FAULT or m.strategy not in WARM_STRATEGIES:
        return None
    nic = next((n for n in vm.nics if n.fixed_ips), None)
    if nic is None:  # nothing another port could hold
        return None
    network = plan.mappings.networks.get(nic.network, nic.network)
    return (
        f"the NIC {nic.mac} of {vm.name} on {network!r}: port demo-port-1 holds {nic.fixed_ips[0]}"
    )


def server_id(m: Migration) -> str:
    """The id of the server a simulated cutover of ``m`` creates (SDD §7.4)."""
    return f"sim-{m.id}"


def server_flavor(vm: VMRef, plan: Plan, source: Provider) -> str | None:
    """The flavor a simulated server is created with, as the plan names it (SDD §7.4): a GPU VM's
    GPU flavor - its GPUs read with the profile its source declares for its flavor, as the
    executors read them (§7.2, §22.4) -, else the flavor ``mappings.flavors`` maps the VM's flavor
    to, which the pre-flight and the gate count when it asks for a vGPU (§22.5, §22.7); None for
    any other VM."""
    declared = source.gpu_profiles.get(vm.flavor) if vm.flavor else None
    if vm_gpus(vm, declared):
        return gpu_flavor(vm, plan, source.kind, declared)
    return plan.mappings.flavors.get(vm.flavor) if vm.flavor else None


def _step_of(ctx: StepContext) -> Step:
    """The row of SDD §27.5's table the simulated run is: the migration kit's for a VMware strategy,
    else a migration's - as the Ansible executor reads it (§7.2)."""
    vmware = Strategy(ctx.migration.strategy) in (Strategy.vmware_cold, Strategy.vmware_warm)
    return Step.KIT if vmware else Step.MIGRATION


class SimulatedExecutor:
    name = "simulated"

    def __init__(
        self, settings: Settings, *, sleep: Sleep = asyncio.sleep, providers: Any = None
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        #: the provider registry: the demo's fake source follows the simulated stop and restart
        self.providers = providers
        #: by migration: the model seconds the running cutover has kept the source stopped so far -
        #: a rollback that cancels it reports them, as the cutover cannot (SDD §5.2, §7.4)
        self._stopped: dict[str, float] = {}

    async def _source_power(self, ctx: StepContext, on: bool) -> None:
        """Stop or start the fake source VM the way the real steps do (SDD §7.4, §8.1)."""
        if self.providers is None:
            return
        try:
            # the step's credential at the source (SDD §27.5)
            client = for_step(self.providers, _step_of(ctx), ctx.source, ctx.plan)
            action = getattr(client, "power_on" if on else "power_off", None)
            if action is None:
                return
            await action(ctx.migration.vm.source_id)
        except ProviderError:
            pass  # a VM the fake does not hold: nothing to stop or start, never a failed step

    def _destination_call(self, ctx: StepContext, name: str) -> Any:
        """The fake destination's ``name`` method, which holds the simulated servers and their
        GPU units (SDD §7.4, §10); None without one."""
        if self.providers is None:
            return None
        try:
            # the step's credential at the destination (SDD §27.5)
            client = for_step(self.providers, _step_of(ctx), ctx.destination, ctx.plan)
        except ProviderError:
            return None  # a fake that holds no such credential (plan task TC9): nothing simulated
        return getattr(client, name, None)

    def supports(self, strategy: Strategy) -> bool:
        return True

    def _rng(self, ctx: StepContext, step: StepName, salt: object = "") -> random.Random:
        m = ctx.migration
        return random.Random(f"{self.settings.demo_seed}:{m.id}:{step}:{m.attempts}:{salt}")

    def _jitter(self, rng: random.Random) -> float:
        return rng.uniform(1 - JITTER, 1 + JITTER)

    async def _elapse(
        self, ctx: StepContext, model_s: float, total_bytes: int, *, stopped: bool = False
    ) -> None:
        """Let ``model_s`` simulated seconds pass while reporting progress; with ``stopped``, count
        each tick as time the source stayed stopped."""
        wall = max(0.0, model_s) / max(self.settings.demo_speed, 1e-9)
        ticks = max(1, min(MAX_PROGRESS_TICKS, int(wall / PROGRESS_TICK_WALL_S)))
        mid = ctx.migration.id
        for i in range(1, ticks + 1):
            await self._sleep(wall / ticks)
            if stopped:
                self._stopped[mid] = self._stopped.get(mid, 0.0) + max(0.0, model_s) / ticks
            pct = 100.0 * i / ticks
            await ctx.report_progress(pct, int(total_bytes * i / ticks), total_bytes)

    async def prestage(self, plan: Plan, source: Provider, destination: Provider) -> None:
        await self._sleep(30.0 / max(self.settings.demo_speed, 1e-9))

    async def run(self, step: StepName, ctx: StepContext) -> StepResult:
        step = StepName(step)
        if step in (StepName.PRECOPY, StepName.SYNC):
            return await self._pass(step, ctx)
        if step == StepName.CUTOVER:
            return await self._cutover(ctx)
        if step == StepName.ROLLBACK:
            # a cutover this rollback cancelled reported nothing: its stopped seconds come here
            interrupted = self._stopped.pop(ctx.migration.id, 0.0)
            await self._elapse(ctx, ROLLBACK_MODEL_S, 0)
            # the server goes first, its GPU units free again, then the source starts, as §7.2's
            # rollback does (SDD §7.4)
            delete = self._destination_call(ctx, "delete_server")
            if delete is not None:
                await delete(server_id(ctx.migration))
            await self._source_power(ctx, on=True)
            # the model seconds the rollback kept the source stopped: the downtime counts them
            # (SDD §5.2)
            return StepResult(
                details={
                    "source_running": True,
                    "downtime_model_s": ROLLBACK_MODEL_S + interrupted,
                }
            )
        if step == StepName.FINALIZE:
            return StepResult(details={"finalized": True})
        await self.prestage(ctx.plan, ctx.source, ctx.destination)
        return StepResult()

    def _change_rate(self, ctx: StepContext) -> float:
        vm = ctx.migration.vm
        params = params_for_plan(ctx.plan)
        return vm.change_rate_bps if vm.change_rate_bps is not None else params.change_rate_bps

    def _previous_duration(self, ctx: StepContext) -> float:
        """The previous pass's whole model time: a warm pass records the part after its snapshot,
        and the guest writes through the snapshot too (SDD §7.4, §9.1, reviews RV38A-05,
        RV39-07)."""
        passes = ctx.migration.sync_passes
        if not passes:
            return 0.0
        return whole_pass_seconds(passes[-1], ctx.migration.strategy, params_for_plan(ctx.plan))

    def _pass_times(self, ctx: StepContext, changed: float, first: bool) -> tuple[float, float]:
        """The pass's whole model time and the duration it records (SDD §7.4): a warm pass takes
        the model's snapshot + max(scan, Δ/L), its scan as §9.1 models it - a tracked migration's
        later passes query the extents too, snapshot + q + max(scan, Δ/L) (§28.6) - and records
        the part after the snapshot, as the collection's warm sync does (review RV38A-05)."""
        p = params_for_plan(ctx.plan)
        if ctx.migration.strategy == Strategy.vmware_warm:
            took = changed / p.link_bps if first else VMWARE_PASS_OVERHEAD_S + changed / p.link_bps
            return took, took
        sync = max(scan_seconds(ctx.migration.vm, p), changed / p.link_bps)
        if not first and ctx.migration.tracked_disks:
            sync += p.extent_query_s
        return p.snapshot_s + sync, sync

    def _extent_use(self, ctx: StepContext, mode: str) -> dict[str, ExtentUse]:
        """How the pass used each disk's extents (SDD §28.5, plan task CE11): {} for a migration
        without tracked disks; else its first pass scans every disk, a later one checks or reads
        the tracked disks and scans the others, which are no volume of the cluster."""
        m = ctx.migration
        if m.strategy != Strategy.warm or not m.tracked_disks:
            return {}
        p = params_for_plan(ctx.plan)
        tracked = set(m.tracked_disks)
        uses: dict[str, ExtentUse] = {}
        for disk in m.vm.disks:
            dev = disk.device or disk.id
            if mode == "first_pass":
                uses[dev] = ExtentUse(mode="scanned", reason="first_pass")
            elif disk.id in tracked:
                uses[dev] = ExtentUse(mode=mode, query_s=p.extent_query_s)  # type: ignore[arg-type]
            else:
                reason = "encrypted" if disk.encrypted else "not_tracked"
                uses[dev] = ExtentUse(mode="scanned", reason=reason)
        return uses

    async def _pass(self, step: StepName, ctx: StepContext) -> StepResult:
        vm = ctx.migration.vm
        number = next_pass_number(ctx.migration)
        rng = self._rng(ctx, step, number)
        first = number == 1
        if first:
            changed = vm.used_bytes * self._jitter(rng)
        else:
            changed = self._change_rate(ctx) * self._previous_duration(ctx) * self._jitter(rng)
        changed = float(min(vm.disk_bytes, changed))
        took, duration = self._pass_times(ctx, changed, first)
        started = utcnow()
        await ctx.log(f"simulated {'full' if first else 'delta'} pass {number} started")
        await self._elapse(ctx, took, int(changed))
        result = StepResult(
            sync_pass=SyncPass(
                number=number,
                kind=SyncPassKind.full if first else SyncPassKind.delta,
                started_at=started,
                ended_at=utcnow(),
                bytes_scanned=vm.disk_bytes,
                bytes_changed=int(changed),
                bytes_transferred=int(changed),
                duration_s=duration,
                extents=self._extent_use(ctx, "first_pass" if first else "checked"),
            )
        )
        refusal = _demo_refusal(ctx.migration, ctx.plan)
        if refusal is not None:
            # SDD §7.4: met on the first passes, fixed before the third
            result.cutover_refusals = [refusal] if number <= REFUSING_PASSES else []
        return result

    async def _cutover(self, ctx: StepContext) -> StepResult:
        m = ctx.migration
        vm = m.vm
        p = params_for_plan(ctx.plan)
        rng = self._rng(ctx, StepName.CUTOVER)
        refusal = _demo_refusal(m, ctx.plan)
        if refusal is not None and next_pass_number(m) <= REFUSING_PASSES + 1:
            # SDD §7.4: like the destination check (§6.6), refused before the source stops while
            # the problem lasts - the third pass finds it fixed
            raise PermanentStepError(refusal)
        await ctx.log("simulated cutover: stopping the source VM")
        # the clock starts at the stop, as an Ansible-driven cutover's does: the shutdown counts, as
        # it does in the estimate's parts (SDD §5.2, §7.2, §7.4)
        await ctx.mark_downtime_start()
        self._stopped[m.id] = 0.0  # counted from the stop; the cutover's result reports them
        await self._elapse(ctx, p.shutdown_s, 0, stopped=True)
        await self._source_power(ctx, on=False)
        # the final sync (or the copy) runs once the source is stopped (SDD §7.4, ADV-16)
        started = utcnow()

        sync_pass: SyncPass | None = None
        if m.strategy in (Strategy.warm, Strategy.vmware_warm):
            changed = min(
                float(vm.disk_bytes),
                self._change_rate(ctx) * self._previous_duration(ctx) * self._jitter(rng),
            )
            if m.strategy == Strategy.warm:
                # the model's final pass: its snapshot, then its scan as §9.1 models it or its
                # transfer (SDD §7.4, review RV38A-05); over the tracked disks' extents, §28.6's
                # term (plan task CE11)
                copy_s = final_copy_seconds(vm, p, changed, m.tracked_disks)
            else:
                copy_s = changed / p.link_bps + p.v2v_inplace_s
            kind, number = SyncPassKind.final, next_pass_number(m)
        elif m.strategy == Strategy.storage_handover:
            changed, copy_s = 0.0, len(vm.disks) * p.handover_per_volume_s
            kind, number = None, 0
        else:
            changed = vm.used_bytes * self._jitter(rng)
            copy_s = p.snapshot_s + changed / p.link_bps
            if m.strategy == Strategy.vmware_cold:
                copy_s = changed / p.link_bps + p.v2v_s
            kind, number = SyncPassKind.full, next_pass_number(m)

        # A failure, if drawn, happens part-way through the copy (the source is already down).
        fails = rng.random() < self.settings.demo_failure_rate
        copied_s = copy_s * (0.5 if fails else 1.0)
        await self._elapse(ctx, copied_s, int(changed), stopped=True)
        if fails:
            self._stopped.pop(m.id, None)
            # the model seconds the source stayed down up to the failure (SDD §5.2)
            raise PermanentStepError(
                "simulated cutover failure: destination volume attach timed out on the "
                "conversion host",
                details={"downtime_model_s": p.shutdown_s + copied_s},
            )
        if kind is not None:
            sync_pass = SyncPass(
                number=number,
                kind=kind,
                started_at=started,
                ended_at=utcnow(),
                bytes_scanned=vm.disk_bytes,
                bytes_changed=int(changed),
                bytes_transferred=int(changed),
                # a warm final pass records the part after its snapshot, as the others do
                duration_s=copy_s - p.snapshot_s if m.strategy == Strategy.warm else copy_s,
                extents=self._extent_use(ctx, "read") if kind == SyncPassKind.final else {},
            )
        await self._elapse(ctx, p.create_s, 0, stopped=True)
        self._stopped.pop(m.id, None)
        # the model seconds the source stayed down here: the downtime counts them (SDD §5.2)
        stopped = {"downtime_model_s": p.shutdown_s + copy_s + p.create_s}
        create = self._destination_call(ctx, "create_server")
        if create is not None:
            try:
                # its flavor's vGPU units taken at the fake destination (SDD §7.4, §10)
                await create(server_id(m), server_flavor(vm, ctx.plan, ctx.source))
            except ProviderError as exc:
                # as Nova finds no valid host: after the source stopped (§22.7)
                raise PermanentStepError(
                    f"simulated cutover failure: {exc}", details=stopped
                ) from None
        return StepResult(sync_pass=sync_pass, destination_server_id=server_id(m), details=stopped)
