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

from ..config import Settings
from ..domain.enums import Strategy, SyncPassKind
from ..domain.models import Plan, Provider, SyncPass, utcnow
from ..planning.estimator import VMWARE_PASS_OVERHEAD_S, params_for_plan
from .base import PermanentStepError, StepContext, StepName, StepResult

Sleep = Callable[[float], Awaitable[None]]
JITTER = 0.1
PROGRESS_TICK_WALL_S = 0.5
MAX_PROGRESS_TICKS = 40


class SimulatedExecutor:
    name = "simulated"

    def __init__(self, settings: Settings, *, sleep: Sleep = asyncio.sleep) -> None:
        self.settings = settings
        self._sleep = sleep

    def supports(self, strategy: Strategy) -> bool:
        return True

    def _rng(self, ctx: StepContext, step: StepName, salt: object = "") -> random.Random:
        m = ctx.migration
        return random.Random(f"{self.settings.demo_seed}:{m.id}:{step}:{m.attempts}:{salt}")

    def _jitter(self, rng: random.Random) -> float:
        return rng.uniform(1 - JITTER, 1 + JITTER)

    async def _elapse(self, ctx: StepContext, model_s: float, total_bytes: int) -> None:
        """Let ``model_s`` simulated seconds pass while reporting progress."""
        wall = max(0.0, model_s) / max(self.settings.demo_speed, 1e-9)
        ticks = max(1, min(MAX_PROGRESS_TICKS, int(wall / PROGRESS_TICK_WALL_S)))
        for i in range(1, ticks + 1):
            await self._sleep(wall / ticks)
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
            await self._elapse(ctx, 90.0, 0)
            return StepResult(details={"source_running": True})
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
        number = len(ctx.migration.sync_passes) + 1
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
        started = utcnow()
        await ctx.log("simulated cutover: stopping the source VM")
        await self._elapse(ctx, p.shutdown_s, 0)
        await ctx.mark_downtime_start()

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
            kind, number = SyncPassKind.final, len(m.sync_passes) + 1
        elif m.strategy == Strategy.storage_handover:
            changed, copy_s = 0.0, len(vm.disks) * p.handover_per_volume_s
            kind, number = None, 0
        else:
            changed = vm.used_bytes * self._jitter(rng)
            copy_s = p.snapshot_s + changed / p.link_bps
            if m.strategy == Strategy.vmware_cold:
                copy_s = changed / p.link_bps + p.v2v_s
            kind, number = SyncPassKind.full, len(m.sync_passes) + 1

        # A failure, if drawn, happens part-way through the copy (the source is already down).
        fails = rng.random() < self.settings.demo_failure_rate
        await self._elapse(ctx, copy_s * (0.5 if fails else 1.0), int(changed))
        if fails:
            raise PermanentStepError(
                "simulated cutover failure: destination volume attach timed out on the "
                "conversion host"
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
        await self._elapse(ctx, p.create_s, 0)
        return StepResult(sync_pass=sync_pass, destination_server_id=f"sim-{m.id}")
