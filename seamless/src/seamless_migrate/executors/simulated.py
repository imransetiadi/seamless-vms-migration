"""Simulated executor for demo mode and tests (SDD §7.4).

Durations follow the estimator model (§9.1) in *model seconds* (recorded in
``SyncPass.duration_s``); wall-clock time is compressed by ``settings.demo_speed``. Byte counts
carry seeded jitter; each pass changes ``min(disk_bytes, change_rate · previous_pass_duration)``.
Cutover fails with probability ``settings.demo_failure_rate`` after the source was stopped.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import Settings
from ..domain.enums import Strategy, SyncPassKind
from ..domain.models import Plan, Provider, SyncPass, next_pass_number, utcnow
from ..planning.estimator import VMWARE_PASS_OVERHEAD_S, params_for_plan
from ..providers.base import ProviderError
from .base import PermanentStepError, StepContext, StepName, StepResult

Sleep = Callable[[float], Awaitable[None]]
JITTER = 0.1
PROGRESS_TICK_WALL_S = 0.5
MAX_PROGRESS_TICKS = 40
#: model seconds a simulated rollback takes, the source starting again at its end
ROLLBACK_MODEL_S = 90.0


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
        client = self.providers.get(ctx.source)
        action = getattr(client, "power_on" if on else "power_off", None)
        if action is None:
            return
        try:
            await action(ctx.migration.vm.source_id)
        except ProviderError:
            pass  # a VM the fake does not hold: nothing to stop or start, never a failed step

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
        passes = ctx.migration.sync_passes
        return float(passes[-1].duration_s or 0.0) if passes else 0.0

    def _pass_duration(self, ctx: StepContext, changed: float, first: bool) -> float:
        p = params_for_plan(ctx.plan)
        disk = ctx.migration.vm.disk_bytes
        if ctx.migration.strategy == Strategy.vmware_warm:
            return changed / p.link_bps if first else VMWARE_PASS_OVERHEAD_S + changed / p.link_bps
        return p.snapshot_s + max(disk / p.scan_bps, changed / p.link_bps)

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
        duration = self._pass_duration(ctx, changed, first)
        started = utcnow()
        await ctx.log(f"simulated {'full' if first else 'delta'} pass {number} started")
        await self._elapse(ctx, duration, int(changed))
        return StepResult(
            sync_pass=SyncPass(
                number=number,
                kind=SyncPassKind.full if first else SyncPassKind.delta,
                started_at=started,
                ended_at=utcnow(),
                bytes_scanned=vm.disk_bytes,
                bytes_changed=int(changed),
                bytes_transferred=int(changed),
                duration_s=duration,
            )
        )

    async def _cutover(self, ctx: StepContext) -> StepResult:
        m = ctx.migration
        vm = m.vm
        p = params_for_plan(ctx.plan)
        rng = self._rng(ctx, StepName.CUTOVER)
        await ctx.log("simulated cutover: stopping the source VM")
        await self._elapse(ctx, p.shutdown_s, 0)
        await self._source_power(ctx, on=False)
        await ctx.mark_downtime_start()
        self._stopped[m.id] = 0.0  # counted from the stop; the cutover's result reports them
        # the final sync (or the copy) runs once the source is stopped (SDD §7.4, ADV-16)
        started = utcnow()

        sync_pass: SyncPass | None = None
        if m.strategy in (Strategy.warm, Strategy.vmware_warm):
            changed = min(
                float(vm.disk_bytes),
                self._change_rate(ctx) * self._previous_duration(ctx) * self._jitter(rng),
            )
            if m.strategy == Strategy.warm:
                copy_s = p.snapshot_s + max(vm.disk_bytes / p.scan_bps, changed / p.link_bps)
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
                details={"downtime_model_s": copied_s},
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
                duration_s=copy_s,
            )
        await self._elapse(ctx, p.create_s, 0, stopped=True)
        self._stopped.pop(m.id, None)
        # the model seconds the source stayed down here: the downtime counts them (SDD §5.2)
        return StepResult(
            sync_pass=sync_pass,
            destination_server_id=f"sim-{m.id}",
            details={"downtime_model_s": copy_s + p.create_s},
        )
