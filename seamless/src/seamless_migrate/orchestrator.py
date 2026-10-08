"""Orchestrator: asyncio FSM driver for plans, waves and migrations (SDD §8).

* A tick loop (``settings.tick_s``) starts work for ``running`` plans: active waves (all
  ``depends_on`` complete), pre-copy starts within ``wave.max_parallel`` and
  ``max_concurrent_migrations``, the cutover gate of §5.4 (approval, window, request/auto,
  ``max_concurrent_cutovers``), keep-warm passes, and plan completion.
* One driver task per active migration executes the executor step of its phase, persists the
  migration after every state change (with optimistic versions) and records ``checkpoint``.
* One ``asyncio.Lock`` per migration serializes API actions with the driver. An action that
  moves a migration away from a running step (rollback, cancel) cancels the in-flight step; the
  driver then continues from the new phase, so transitions are never lost or duplicated.
* On start, migrations in ``precopy|syncing|cutover|verifying|rolling_back`` are resumed;
  executors are idempotent per step and ``mark_downtime_start`` is idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from datetime import datetime
from typing import Any

from .ai.memory import redact
from .config import Settings
from .domain import fsm
from .domain.enums import (
    SINGLE_SHOT_STRATEGIES,
    WARM_STRATEGIES,
    Phase,
    PlanStatus,
    ProviderRole,
    Strategy,
    SyncPassKind,
)
from .domain.models import (
    AdvisorNote,
    Approval,
    Estimate,
    Finding,
    Mappings,
    Migration,
    PhaseChange,
    Plan,
    Provider,
    SyncPass,
    ValidationItem,
    ValidationReport,
    VMRef,
    utcnow,
)
from .events import EventBus, emit
from .executors.base import (
    PermanentStepError,
    StepContext,
    StepName,
    StepResult,
    TransientStepError,
)
from .planning.estimator import (
    EstimatorParams,
    calibrated_change_rate,
    estimate,
    estimate_final_downtime,
    observed_scan_rate,
    params_for_plan,
)
from .planning.preflight import has_blocker, resolve_mappings, run_preflight
from .planning.selector import eligibility, select_strategy
from .planning.waves import heuristic_tier, plan_waves
from .providers.base import ProviderError
from .store import AsyncStore, NotFound, Store
from .verification import VerificationResult, Verifier

log = logging.getLogger(__name__)
P = Phase

PROGRESS_MIN_INTERVAL_S = 1.0
APPROVABLE = frozenset(
    {
        P.pending,
        P.validating,
        P.blocked,
        P.ready,
        P.precopy,
        P.syncing,
        P.awaiting_cutover,
        P.failed,
        P.rolled_back,
    }
)
CUTOVER_REQUESTABLE = frozenset({P.ready, P.precopy, P.syncing, P.awaiting_cutover})
REVALIDATABLE = frozenset({P.pending, P.blocked, P.ready})


class OrchestratorError(Exception):
    """Base class for errors raised to API callers."""


class NotAllowed(OrchestratorError):
    """The action is not allowed in the current state (HTTP 409)."""


class BadRequest(OrchestratorError, ValueError):
    """The request is invalid (HTTP 400)."""


class VerificationFailed(PermanentStepError):
    pass


def _phase_started(m: Migration, phase: Phase) -> datetime | None:
    """When ``m`` last entered ``phase`` (None when it never did)."""
    for change in reversed(m.phase_history):
        if change.to_phase == phase:
            return change.at
    return None


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"  # pragma: no cover


class Orchestrator:
    def __init__(
        self,
        store: Store,
        bus: EventBus,
        providers: Any,
        executors: Any,
        advisor: Any,
        knowledge: Any,
        settings: Settings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = utcnow,
        verify_poll_s: float = 10.0,
        verifier_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.store = store
        self.db = AsyncStore(store)
        self.bus = bus
        self.providers = providers
        self.executors = executors
        self.advisor = advisor
        self.knowledge = knowledge
        self.settings = settings
        self._sleep = sleep
        self._now = clock
        self._verifier_factory = verifier_factory or (
            lambda dst: Verifier(dst, settings, poll_s=verify_poll_s)
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._drivers: dict[str, asyncio.Task[None]] = {}
        self._steps: dict[str, asyncio.Task[Any]] = {}
        self._force_window: set[str] = set()
        self._prestage_done: set[str] = set()
        self._prestage_tasks: dict[str, asyncio.Task[None]] = {}
        self._wave_events_seen: dict[str, set[tuple[str, str]]] = {}
        self._progress_at: dict[str, float] = {}
        self._tick_task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False
        #: step -> [sum_seconds, count] for seamless_step_duration_seconds
        self.step_stats: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])

    # ------------------------------------------------------------------------------------------
    # plumbing
    def _lock(self, mid: str) -> asyncio.Lock:
        lock = self._locks.get(mid)
        if lock is None:
            lock = self._locks[mid] = asyncio.Lock()
        return lock

    def now(self) -> datetime:
        """The orchestrator's clock (UTC)."""
        return self._now()

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _load(self, mid: str) -> tuple[Migration, int]:
        return await self.db.get_versioned("migration", mid, Migration)

    async def _save(self, m: Migration, version: int) -> int:
        m.updated_at = self._now()
        return await self.db.put("migration", m, expected_version=version)

    async def _plan(self, plan_id: str) -> Plan:
        return await self.db.get("plan", plan_id, Plan)

    async def _save_plan(self, plan: Plan) -> None:
        plan.updated_at = self._now()
        await self.db.put("plan", plan)

    async def _provider(self, provider_id: str, role: ProviderRole) -> Provider:
        try:
            provider = await self.db.get("provider", provider_id, Provider)
        except NotFound:
            raise BadRequest(f"{role} provider {provider_id!r} does not exist") from None
        if provider.role != role:
            raise BadRequest(f"provider {provider_id!r} is not a {role} provider")
        return provider

    async def _emit(
        self,
        kind: str,
        message: str,
        *,
        migration: Migration | None = None,
        plan_id: str | None = None,
        actor: str = "system",
        data: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> None:
        await emit(
            self.store,
            self.bus,
            kind,
            message,
            plan_id=plan_id or (migration.plan_id if migration else None),
            migration_id=migration.id if migration else None,
            actor=actor,
            data=data,
            persist=persist,
        )

    async def _transition(
        self,
        m: Migration,
        version: int,
        to: Phase,
        reason: str,
        actor: str = "system",
        data: dict[str, Any] | None = None,
    ) -> tuple[Migration, int]:
        new = fsm.transition(m, to, reason, actor, now=self._now())
        version = await self._save(new, version)
        await self._emit(
            "migration.phase",
            f"{new.vm.name}: {m.phase} -> {to} ({reason})",
            migration=new,
            actor=actor,
            data={"from": str(m.phase), "to": str(to), "reason": reason, **(data or {})},
        )
        return new, version

    # ------------------------------------------------------------------------------------------
    # providers
    async def check_provider(self, provider_id: str, actor: str = "system") -> Provider:
        provider = await self.db.get("provider", provider_id, Provider)
        impl = self.providers.get(provider)
        try:
            caps = await impl.check()
            update = {"capabilities": dict(caps), "status": "ok", "status_message": None}
        except ProviderError as exc:
            update = {"status": "error", "status_message": redact(str(exc))[:500]}
        update["last_checked_at"] = self._now()
        updated = provider.model_copy(update=update)
        await self.db.put("provider", updated)
        await self._emit(
            "provider.checked",
            f"provider {provider.id}: {updated.status}",
            actor=actor,
            data={
                "provider_id": provider.id,
                "status": updated.status,
                "message": updated.status_message,
            },
        )
        return updated

    async def _caps(self, provider: Provider, actor: str) -> dict[str, Any]:
        checked = await self.check_provider(provider.id, actor)
        if checked.status != "ok":
            raise BadRequest(f"provider {provider.id} check failed: {checked.status_message}")
        return {**checked.capabilities, "conversion_host": checked.conversion_host is not None}

    async def _vms(self, plan: Plan, impl: Any) -> list[VMRef]:
        vms = []
        for vm_id in plan.vm_ids:
            try:
                vms.append(await impl.get_vm(vm_id))
            except ProviderError as exc:
                raise BadRequest(f"VM {vm_id!r}: {exc}") from None
        return vms

    # ------------------------------------------------------------------------------------------
    # planning actions
    async def validate_plan(self, plan_id: str, actor: str) -> ValidationReport:
        # one validation per plan at a time: concurrent runs would create duplicate migrations
        async with self._lock(f"plan:{plan_id}"):
            return await self._validate_plan(plan_id, actor)

    async def _validate_plan(self, plan_id: str, actor: str) -> ValidationReport:
        plan = await self._plan(plan_id)
        if plan.status == PlanStatus.running:
            raise NotAllowed("pause the plan before validating it again")
        source = await self._provider(plan.source_provider_id, ProviderRole.source)
        destination = await self._provider(plan.destination_provider_id, ProviderRole.destination)
        src_impl = self.providers.get(source)
        dst_impl = self.providers.get(destination)
        src_caps = await self._caps(source, actor)
        dst_caps = await self._caps(destination, actor)
        try:
            src_inv = await src_impl.inventory()
            dst_inv = await dst_impl.inventory()
        except ProviderError as exc:
            raise BadRequest(f"inventory failed: {exc}") from None
        vms = await self._vms(plan, src_impl)
        existing = {
            m.vm.source_id: m for m in await self.db.list("migration", Migration, plan_id=plan.id)
        }
        params = params_for_plan(plan)
        items: list[ValidationItem] = []
        for vm in vms:
            findings = run_preflight(
                vm, plan, src_inv, dst_inv, vms, source=source, destination=destination
            )
            resolved = resolve_mappings(vm, plan, dst_inv)
            reasons = eligibility(vm, source.kind, plan, src_caps, dst_caps, findings)
            estimates = [
                estimate(vm, strategy, params, plan.downtime_slo_s).model_copy(
                    update={"eligible": not why, "reasons": list(why)}
                )
                for strategy, why in reasons.items()
            ]
            strategy, why = select_strategy(vm, estimates, plan)
            notes: list[AdvisorNote] = []
            automatic = (
                plan.default_strategy == "auto" and vm.source_id not in plan.strategy_overrides
            )
            if self.advisor is not None and automatic:
                note = await self.advisor.recommend_strategy(vm, estimates, plan, findings)
                if note is not None:
                    notes.append(note)
                    chosen = note.data.get("selected")
                    match = next((e for e in estimates if e.strategy == chosen), None)
                    if note.data.get("applied") and match is not None and match.eligible:
                        strategy, why = match.strategy, f"advisor ({note.source}): {note.summary}"
            migration = existing.get(vm.source_id) or await self._create_migration(
                plan, vm, strategy, actor
            )
            migration = await self._apply_validation(
                migration.id, plan, vm, strategy, why, estimates, findings, notes, actor, resolved
            )
            items.append(
                ValidationItem(
                    migration_id=migration.id,
                    vm_name=migration.vm.name,
                    strategy=migration.strategy,
                    phase=migration.phase,
                    findings=migration.findings,
                    estimates=migration.estimates,
                )
            )
        selected = set(plan.vm_ids)
        for vm_id, stale in existing.items():
            if vm_id not in selected and stale.phase in REVALIDATABLE:
                await self.cancel(stale.id, actor, "removed from the plan")
        plan = await self._plan(plan_id)
        plan.status = PlanStatus.validated
        await self._save_plan(plan)
        ok = all(item.phase != P.blocked for item in items)
        blocked = sum(1 for item in items if item.phase == P.blocked)
        await self._emit(
            "plan.validated",
            f"{plan.name}: {len(items)} VM(s) validated, {blocked} blocked",
            plan_id=plan.id,
            actor=actor,
            data={"ok": ok, "migrations": len(items), "blocked": blocked},
        )
        return ValidationReport(plan_id=plan.id, ok=ok, migrations=items)

    async def _create_migration(
        self, plan: Plan, vm: VMRef, strategy: Strategy, actor: str
    ) -> Migration:
        now = self._now()
        wave = plan.wave_of(vm.source_id)
        migration = Migration(
            plan_id=plan.id,
            wave_id=wave.id if wave else None,
            vm=vm,
            strategy=strategy,
            phase=P.pending,
            phase_history=[
                PhaseChange(
                    from_phase=None,
                    to_phase=P.pending,
                    at=now,
                    reason="created by validation",
                    actor=actor,
                )
            ],
            bytes_total=vm.used_bytes,
            created_at=now,
            updated_at=now,
        )
        await self.db.put("migration", migration, expected_version=0)
        await self._emit(
            "migration.created",
            f"migration of {vm.name} created",
            migration=migration,
            actor=actor,
            data={"vm": vm.name, "source_id": vm.source_id, "strategy": str(strategy)},
        )
        return migration

    async def _apply_validation(
        self,
        mid: str,
        plan: Plan,
        vm: VMRef,
        strategy: Strategy,
        why: str,
        estimates: list[Estimate],
        findings: list[Finding],
        notes: list[AdvisorNote],
        actor: str,
        resolved: Mappings | None = None,
    ) -> Migration:
        no_eligible = not any(e.eligible for e in estimates)
        blocked = has_blocker(findings) or no_eligible
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase not in REVALIDATABLE:
                return m  # already in flight or finished: leave it alone
            m, v = await self._transition(m, v, P.validating, "pre-flight validation", actor)
            wave = plan.wave_of(vm.source_id)
            m.vm = vm
            m.strategy = strategy
            m.estimates = estimates
            m.estimate = next((e for e in estimates if e.strategy == strategy), None)
            m.findings = findings
            m.resolved_mappings = resolved or Mappings()
            m.wave_id = wave.id if wave else None
            m.bytes_total = vm.used_bytes
            # keep one strategy note per validation (the latest)
            m.advisor_notes = [n for n in m.advisor_notes if n.kind != "strategy"] + notes
            m.error = None
            if no_eligible:
                details = "; ".join(f"{e.strategy}: {', '.join(e.reasons)}" for e in estimates)
                m.error = f"no eligible strategy ({details})"[:1000]
            if blocked:
                codes = [f.code for f in findings if f.severity == "blocker"]
                reason = f"blocked by {', '.join(codes)}" if codes else "no eligible strategy"
                m, v = await self._transition(
                    m, v, P.blocked, reason, actor, data={"findings": codes}
                )
            else:
                m, v = await self._transition(
                    m, v, P.ready, why, actor, data={"strategy": str(strategy)}
                )
        for note in notes:
            await self._emit(
                "advisor.strategy",
                note.summary,
                migration=m,
                data={"source": note.source, **note.data},
            )
        return m

    async def auto_waves(self, plan_id: str, max_wave_size: int, actor: str) -> Plan:
        plan = await self._plan(plan_id)
        if plan.status not in (PlanStatus.draft, PlanStatus.validated, PlanStatus.paused):
            raise NotAllowed(f"waves cannot change while the plan is {plan.status}")
        if max_wave_size < 1:
            raise BadRequest("max_wave_size must be >= 1")
        source = await self._provider(plan.source_provider_id, ProviderRole.source)
        vms = await self._vms(plan, self.providers.get(source))
        if self.advisor is not None:
            tiers, note = await self.advisor.classify_workloads(vms)
        else:
            tiers = {vm.source_id: heuristic_tier(vm) for vm in vms}
            note = AdvisorNote(
                kind="classification",
                source="rules",
                summary=f"Classified {len(vms)} VM(s) with the heuristic",
                data={"tiers": dict(tiers)},
            )
        plan.waves = plan_waves(vms, tiers, max_wave_size)
        plan.status = PlanStatus.draft
        await self._save_plan(plan)
        await self._emit(
            "advisor.classification",
            note.summary,
            plan_id=plan.id,
            actor=actor,
            data={"source": note.source, **note.data},
        )
        await self._emit(
            "plan.updated",
            f"{plan.name}: {len(plan.waves)} wave(s) planned",
            plan_id=plan.id,
            actor=actor,
            data={"waves": [w.model_dump(mode="json") for w in plan.waves]},
        )
        return plan

    async def start_plan(self, plan_id: str, actor: str) -> Plan:
        plan = await self._plan(plan_id)
        if plan.status == PlanStatus.running:
            return plan
        if plan.status not in (PlanStatus.validated, PlanStatus.paused, PlanStatus.failed):
            raise NotAllowed(f"a {plan.status} plan cannot be started; validate it first")
        migrations = await self.db.list("migration", Migration, plan_id=plan.id)
        if not migrations:
            raise NotAllowed("the plan has no migrations; validate it first")
        blocked = [m.vm.name for m in migrations if m.phase == P.blocked]
        if blocked:
            raise NotAllowed(f"{len(blocked)} migration(s) are blocked: {', '.join(blocked[:10])}")
        plan.status = PlanStatus.running
        await self._save_plan(plan)
        await self._emit("plan.started", f"{plan.name} started", plan_id=plan.id, actor=actor)
        self.wake()
        return plan

    async def pause_plan(self, plan_id: str, actor: str) -> Plan:
        plan = await self._plan(plan_id)
        if plan.status == PlanStatus.paused:
            return plan
        if plan.status != PlanStatus.running:
            raise NotAllowed(f"only running plans can be paused (plan is {plan.status})")
        plan.status = PlanStatus.paused
        await self._save_plan(plan)
        await self._emit("plan.paused", f"{plan.name} paused", plan_id=plan.id, actor=actor)
        return plan

    # ------------------------------------------------------------------------------------------
    # migration actions (each serialized with the driver by the migration lock)
    async def approve(self, mid: str, actor: str, comment: str | None = None) -> Migration:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase not in APPROVABLE:
                raise NotAllowed(f"a migration in {m.phase} cannot be approved")
            m.approvals.append(Approval(actor=actor, at=self._now(), comment=comment))
            await self._save(m, v)
        await self._emit(
            "migration.approved",
            f"{m.vm.name} approved by {actor}",
            migration=m,
            actor=actor,
            data={"comment": comment},
        )
        self.wake()
        return m

    async def request_cutover(
        self, mid: str, actor: str, force_window: bool = False, comment: str | None = None
    ) -> Migration:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase not in CUTOVER_REQUESTABLE:
                raise NotAllowed(f"cutover cannot be requested in {m.phase}")
            m.approvals.append(Approval(actor=actor, at=self._now(), comment=comment))
            m.cutover_requested = True
            await self._save(m, v)
            if force_window:
                self._force_window.add(mid)
        await self._emit(
            "migration.approved",
            f"{m.vm.name} approved by {actor}",
            migration=m,
            actor=actor,
            data={"comment": comment},
        )
        await self._emit(
            "migration.action",
            f"cutover of {m.vm.name} requested by {actor}",
            migration=m,
            actor=actor,
            data={"action": "cutover", "force_window": force_window},
        )
        self.wake()
        return m

    async def request_sync(self, mid: str, actor: str) -> Migration:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase != P.awaiting_cutover or m.strategy not in WARM_STRATEGIES:
                raise NotAllowed("a sync pass can only be requested in awaiting_cutover")
            m, v = await self._transition(
                m, v, P.syncing, f"delta pass requested by {actor}", actor
            )
        await self._emit(
            "migration.action",
            f"sync of {m.vm.name} requested",
            migration=m,
            actor=actor,
            data={"action": "sync"},
        )
        self._launch(mid)
        return m

    async def rollback(self, mid: str, actor: str, reason: str) -> Migration:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if not fsm.can_transition(m, P.rolling_back):
                raise NotAllowed(f"a migration in {m.phase} cannot be rolled back")
            m, v = await self._transition(
                m, v, P.rolling_back, f"rollback requested: {reason}", actor
            )
            step = self._steps.get(mid)
            if step is not None:
                step.cancel()
        await self._emit(
            "migration.action",
            f"rollback of {m.vm.name} requested",
            migration=m,
            actor=actor,
            data={"action": "rollback", "reason": reason},
        )
        self._launch(mid)
        return m

    async def retry(self, mid: str, actor: str) -> Migration:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase not in (P.failed, P.rolled_back):
                raise NotAllowed(f"a migration in {m.phase} cannot be retried")
            if m.phase == P.rolled_back:
                # the FSM counts failed -> ready only; a retry after an automatic rollback
                # is still a new attempt of the cutover (executors key their behaviour on it)
                m.attempts += 1
            m, v = await self._transition(m, v, P.ready, f"retry requested by {actor}", actor)
            m.error = None
            m.cutover_requested = False
            m.checkpoint = None
            m.progress_pct = 0
            m.review_required = False
            m.review_reason = None
            await self._save(m, v)
        self._force_window.discard(mid)
        await self._emit(
            "migration.action",
            f"retry of {m.vm.name}",
            migration=m,
            actor=actor,
            data={"action": "retry", "attempts": m.attempts},
        )
        self.wake()
        return m

    async def cancel(self, mid: str, actor: str, reason: str | None = None) -> Migration:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if not fsm.can_transition(m, P.cancelled):
                raise NotAllowed(f"a migration in {m.phase} cannot be cancelled")
            m, v = await self._transition(
                m, v, P.cancelled, f"cancelled: {reason or 'no reason given'}", actor
            )
            step = self._steps.get(mid)
            if step is not None:
                step.cancel()
        self._force_window.discard(mid)
        await self._emit(
            "migration.action",
            f"{m.vm.name} cancelled",
            migration=m,
            actor=actor,
            data={"action": "cancel", "reason": reason},
        )
        self.wake()
        return m

    async def finalize(
        self, mid: str, actor: str, delete_source: bool = False, confirm: str = ""
    ) -> Migration:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase != P.completed:
                raise NotAllowed(f"only completed migrations can be finalized ({m.phase})")
            if confirm != m.vm.name:
                raise BadRequest("confirm must equal the VM name")
            plan = await self._plan(m.plan_id)
            source = await self.db.get("provider", plan.source_provider_id, Provider)
            destination = await self.db.get("provider", plan.destination_provider_id, Provider)
            executor = self.executors.for_strategy(m.strategy)
            ctx = self._context(
                m, plan, source, destination, options={"delete_source": delete_source}, locked=True
            )
            try:
                result = await executor.run(StepName.FINALIZE, ctx)
            except (TransientStepError, PermanentStepError) as exc:
                raise NotAllowed(f"finalize failed: {redact(str(exc))}") from exc
            m.checkpoint = "finalized"
            m, v = await self._transition(
                m,
                v,
                P.finalized,
                f"finalized by {actor}",
                actor,
                data={"delete_source": delete_source, **result.details},
            )
        await self._emit(
            "migration.action",
            f"{m.vm.name} finalized",
            migration=m,
            actor=actor,
            data={"action": "finalize", "delete_source": delete_source},
        )
        return m

    async def set_strategy(self, mid: str, strategy: Strategy | str, actor: str) -> Migration:
        strategy = Strategy(strategy)
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase not in REVALIDATABLE:
                raise NotAllowed(f"the strategy cannot change in {m.phase}")
            est = m.estimate_for(strategy)
            if est is None:
                raise BadRequest(f"strategy {strategy} does not apply to this VM (validate first)")
            if not est.eligible:
                raise BadRequest(f"strategy {strategy} is not eligible: {'; '.join(est.reasons)}")
            m.strategy = strategy
            m.estimate = est
            await self._save(m, v)
        plan = await self._plan(m.plan_id)
        plan.strategy_overrides[m.vm.source_id] = strategy
        await self._save_plan(plan)
        await self._emit(
            "migration.action",
            f"{m.vm.name}: strategy set to {strategy}",
            migration=m,
            actor=actor,
            data={"action": "strategy", "strategy": str(strategy)},
        )
        await self._emit(
            "plan.updated",
            f"{plan.name}: strategy override for {m.vm.name}",
            plan_id=plan.id,
            actor=actor,
            data={"strategy_overrides": {m.vm.source_id: str(strategy)}},
        )
        return m

    # ------------------------------------------------------------------------------------------
    # lifecycle and tick loop
    async def start(self) -> None:
        if self._tick_task is not None and not self._tick_task.done():
            return
        self._stopping = False
        self._wake = asyncio.Event()
        for m in await self.db.list("migration", Migration):
            if m.phase in fsm.RESUMABLE_PHASES:
                self._launch(m.id)
            if any(c.to_phase in (P.precopy, P.cutover) for c in m.phase_history):
                self._prestage_done.add(m.plan_id)  # work started: resources were pre-staged
        self._tick_task = asyncio.create_task(self._loop(), name="seamless-orchestrator")

    async def stop(self) -> None:
        self._stopping = True
        tasks = [
            t
            for t in (
                self._tick_task,
                *self._drivers.values(),
                *self._prestage_tasks.values(),
                *self._steps.values(),
            )
            if t
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tick_task = None
        self._drivers.clear()
        self._steps.clear()
        self._prestage_tasks.clear()

    async def _loop(self) -> None:
        assert self._wake is not None
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("orchestrator tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.tick_s)
            self._wake.clear()

    async def tick(self) -> None:
        plans = [p for p in await self.db.list("plan", Plan) if p.status == PlanStatus.running]
        if not plans:
            return
        migrations = await self.db.list("migration", Migration)
        by_plan: dict[str, list[Migration]] = defaultdict(list)
        for m in migrations:
            by_plan[m.plan_id].append(m)
        counts = {
            "steps": sum(1 for m in migrations if m.phase in fsm.ACTIVE_STEP_PHASES),
            "cutover": sum(1 for m in migrations if m.phase == P.cutover),
        }
        now = self._now()
        for plan in plans:
            await self._tick_plan(plan, by_plan.get(plan.id, []), counts, now)

    def _gate(self, m: Migration, plan: Plan, now: datetime) -> bool:
        if plan.require_approval and not m.approvals:
            return False
        window = plan.cutover_window
        if window is not None and m.id not in self._force_window and not window.contains(now):
            return False
        return plan.auto_cutover or m.cutover_requested

    async def _tick_plan(
        self, plan: Plan, migs: list[Migration], counts: dict[str, int], now: datetime
    ) -> None:
        if not migs:
            return
        if plan.id not in self._prestage_done:
            self._ensure_prestage(plan, migs)
            return
        waves = {w.id: w for w in plan.waves}
        complete = {
            w.id
            for w in plan.waves
            if all(m.phase in fsm.WAVE_COMPLETE_PHASES for m in migs if m.wave_id == w.id)
        }
        active = {w.id for w in plan.waves if all(d in complete for d in w.depends_on)}
        await self._wave_events(plan, active, complete)

        def is_active(m: Migration) -> bool:
            return m.wave_id is None or m.wave_id not in waves or m.wave_id in active

        in_flight = Counter(m.wave_id for m in migs if m.phase in fsm.ACTIVE_STEP_PHASES)

        def wave_room(m: Migration) -> bool:
            wave = waves.get(m.wave_id or "")
            return wave is None or in_flight[m.wave_id] < wave.max_parallel

        limit_steps = self.settings.max_concurrent_migrations
        ordered = sorted(
            (m for m in migs if is_active(m)),
            key=lambda m: (waves[m.wave_id].order if m.wave_id in waves else 0, m.created_at),
        )
        gate_open: set[str] = set()

        # 1. cutover gate (SDD §5.4)
        for m in ordered:
            waiting = m.phase == P.awaiting_cutover or (
                m.phase == P.ready and m.strategy in SINGLE_SHOT_STRATEGIES
            )
            if not waiting or not self._gate(m, plan, now):
                continue
            gate_open.add(m.id)
            if counts["cutover"] >= self.settings.max_concurrent_cutovers:
                continue
            if counts["steps"] >= limit_steps or (m.phase == P.ready and not wave_room(m)):
                continue
            if await self._begin(
                m.id, {P.ready, P.awaiting_cutover}, P.cutover, "cutover gate open"
            ):
                counts["cutover"] += 1
                counts["steps"] += 1
                in_flight[m.wave_id] += 1

        # 2. keep-warm delta passes while waiting for the gate
        for m in ordered:
            if m.phase != P.awaiting_cutover or m.id in gate_open or not m.sync_passes:
                continue
            ended = m.sync_passes[-1].ended_at or m.sync_passes[-1].started_at
            if (now - ended).total_seconds() < plan.keep_warm_interval_s:
                continue
            if counts["steps"] >= limit_steps:
                break
            if await self._begin(m.id, {P.awaiting_cutover}, P.syncing, "keep-warm delta pass"):
                counts["steps"] += 1
                in_flight[m.wave_id] += 1

        # 3. start pre-copy of warm migrations
        for m in ordered:
            if m.phase != P.ready or m.strategy not in WARM_STRATEGIES:
                continue
            if counts["steps"] >= limit_steps:
                break
            if not wave_room(m):
                continue
            if await self._begin(m.id, {P.ready}, P.precopy, "pre-copy started"):
                counts["steps"] += 1
                in_flight[m.wave_id] += 1

        # 4. plan completion
        if all(m.phase in fsm.WAVE_COMPLETE_PHASES for m in migs):
            fresh = await self._plan(plan.id)
            if fresh.status == PlanStatus.running:
                fresh.status = PlanStatus.completed
                await self._save_plan(fresh)
                done = sum(1 for m in migs if m.phase in fsm.SUCCESS_PHASES)
                await self._emit(
                    "plan.completed",
                    f"{plan.name} completed",
                    plan_id=plan.id,
                    data={"migrations": len(migs), "succeeded": done},
                )

    async def _begin(self, mid: str, allowed: set[Phase], to: Phase, reason: str) -> bool:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase not in allowed:
                return False
            await self._transition(m, v, to, reason)
        self._launch(mid)
        return True

    async def _wave_events(self, plan: Plan, active: set[str], complete: set[str]) -> None:
        seen = self._wave_events_seen.get(plan.id)
        if seen is None:
            past = await self.db.events(
                plan_id=plan.id, kinds=["wave.started", "wave.completed"], limit=100000
            )
            seen = {(e.kind, str(e.data.get("wave_id"))) for e in past}
            self._wave_events_seen[plan.id] = seen
        for wave in sorted(plan.waves, key=lambda w: w.order):
            for kind, members in (("wave.started", active), ("wave.completed", complete)):
                if wave.id in members and (kind, wave.id) not in seen:
                    seen.add((kind, wave.id))
                    verb = "started" if kind == "wave.started" else "completed"
                    await self._emit(
                        kind,
                        f"{plan.name}: {wave.name} {verb}",
                        plan_id=plan.id,
                        data={"wave_id": wave.id, "name": wave.name},
                    )

    def _ensure_prestage(self, plan: Plan, migs: list[Migration]) -> None:
        task = self._prestage_tasks.get(plan.id)
        if task is None or task.done():
            self._prestage_tasks[plan.id] = asyncio.create_task(
                self._prestage(plan, migs), name=f"prestage:{plan.id}"
            )

    async def _prestage(self, plan: Plan, migs: list[Migration]) -> None:
        try:
            source = await self.db.get("provider", plan.source_provider_id, Provider)
            destination = await self.db.get("provider", plan.destination_provider_id, Provider)
            executor = self.executors.prestage_executor(source.kind)
            if executor is not None:
                kwargs: dict[str, Any] = {}
                if "deploy_conversion_hosts" in inspect.signature(executor.prestage).parameters:
                    kwargs["deploy_conversion_hosts"] = any(
                        m.strategy in (Strategy.cold, Strategy.warm) for m in migs
                    )
                await executor.prestage(plan, source, destination, **kwargs)
            self._prestage_done.add(plan.id)
            await self._emit(
                "plan.updated",
                f"{plan.name}: resources pre-staged",
                plan_id=plan.id,
                data={"prestaged": True},
            )
            self.wake()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("prestage of %s failed", plan.id)
            fresh = await self._plan(plan.id)
            fresh.status = PlanStatus.failed
            await self._save_plan(fresh)
            await self._emit(
                "plan.updated",
                f"{plan.name}: pre-staging failed",
                plan_id=plan.id,
                data={"status": "failed", "error": redact(str(exc))[:500]},
            )
        finally:
            self._prestage_tasks.pop(plan.id, None)

    # ------------------------------------------------------------------------------------------
    # migration driver
    def _launch(self, mid: str) -> None:
        if self._stopping:
            return
        task = self._drivers.get(mid)
        if task is not None and not task.done():
            return
        self._drivers[mid] = asyncio.create_task(self._drive(mid), name=f"migration:{mid}")

    async def _drive(self, mid: str) -> None:
        handlers = {
            P.precopy: self._do_pass,
            P.syncing: self._do_pass,
            P.cutover: self._do_cutover,
            P.verifying: self._do_verify,
            P.rolling_back: self._do_rollback,
        }
        try:
            while not self._stopping:
                m, _ = await self._load(mid)
                handler = handlers.get(m.phase)
                if handler is None:
                    return
                await handler(m)
        except asyncio.CancelledError:
            raise
        except NotFound:
            return
        except Exception:
            log.exception("driver of %s crashed", mid)
        finally:
            if self._drivers.get(mid) is asyncio.current_task():
                del self._drivers[mid]

    def _context(
        self,
        m: Migration,
        plan: Plan,
        source: Provider,
        destination: Provider,
        *,
        options: dict[str, Any] | None = None,
        locked: bool = False,
    ) -> StepContext:
        mid, phase = m.id, m.phase

        async def report_progress(pct: float, done: int, total: int) -> None:
            await self._on_progress(mid, phase, pct, done, total, persist=not locked)

        async def mark_downtime_start() -> None:
            if not locked:
                await self._mark_downtime(mid)

        async def log_line(line: str) -> None:
            await self._emit(
                "migration.log", line[:2000], plan_id=m.plan_id, persist=False, migration=m
            )

        return StepContext(
            plan=plan,
            migration=m.model_copy(deep=True),
            source=source,
            destination=destination,
            settings=self.settings,
            report_progress=report_progress,
            mark_downtime_start=mark_downtime_start,
            log=log_line,
            options=dict(options or {}),
        )

    async def _on_progress(
        self, mid: str, phase: Phase, pct: float, done: int, total: int, persist: bool
    ) -> None:
        now = time.monotonic()
        if pct < 100 and now - self._progress_at.get(mid, 0.0) < PROGRESS_MIN_INTERVAL_S:
            return
        self._progress_at[mid] = now
        m: Migration | None = None
        if persist:
            async with self._lock(mid):
                cur, v = await self._load(mid)
                if cur.phase != phase:
                    return
                cur.progress_pct = round(float(pct), 2)
                cur.bytes_transferred = sum(p.bytes_transferred for p in cur.sync_passes) + int(
                    done
                )
                await self._save(cur, v)
                m = cur
        await emit(
            self.store,
            self.bus,
            "migration.progress",
            f"{pct:.0f}%",
            plan_id=m.plan_id if m else None,
            migration_id=mid,
            persist=False,
            data={
                "pct": round(float(pct), 2),
                "bytes_done": int(done),
                "bytes_total": int(total),
                "phase": str(phase),
            },
        )

    async def _mark_downtime(self, mid: str) -> None:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.downtime_started_at is not None:
                return
            m.downtime_started_at = self._now()
            await self._save(m, v)
        await self._emit(
            "migration.downtime_started",
            f"{m.vm.name}: source VM stopped",
            migration=m,
            data={"at": m.downtime_started_at.isoformat()},
        )

    async def _run_step(
        self,
        m: Migration,
        step: StepName | str,
        runner: Callable[[StepContext], Awaitable[Any]] | None = None,
    ) -> Any:
        """Run ``step`` as a cancellable task with transient retries; ``None`` if cancelled."""
        plan = await self._plan(m.plan_id)
        source = await self.db.get("provider", plan.source_provider_id, Provider)
        destination = await self.db.get("provider", plan.destination_provider_id, Provider)
        executor = self.executors.for_strategy(m.strategy)
        current = m
        attempt = 0
        loop = asyncio.get_running_loop()
        while True:
            ctx = self._context(current, plan, source, destination)
            work = runner(ctx) if runner is not None else executor.run(step, ctx)
            task = asyncio.create_task(work, name=f"{step}:{m.id}")
            self._steps[m.id] = task
            started = loop.time()
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                raise
            finally:
                if self._steps.get(m.id) is task:
                    del self._steps[m.id]
            stats = self.step_stats[str(step)]
            stats[0] += loop.time() - started
            stats[1] += 1
            if task.cancelled():
                return None
            exc = task.exception()
            if exc is None:
                return task.result()
            if isinstance(exc, TransientStepError) and attempt < self.settings.max_step_retries:
                attempt += 1
                delay = float(2**attempt)
                if self.settings.demo:
                    delay /= max(self.settings.demo_speed, 1.0)
                await self._emit(
                    "migration.log",
                    f"{step}: transient failure ({redact(str(exc))[:200]}); retry "
                    f"{attempt}/{self.settings.max_step_retries} in {delay:g} s",
                    migration=current,
                    persist=False,
                )
                await self._sleep(delay)
                current, _ = await self._load(m.id)
                if current.phase != m.phase:
                    return None
                continue
            raise exc

    def _params(self, m: Migration, plan: Plan) -> EstimatorParams:
        """Plan estimator parameters with this migration's calibrated scan rate."""
        params = params_for_plan(plan)
        if m.observed_scan_bps:
            params = replace(params, scan_bps=float(m.observed_scan_bps))
        return params

    def _calibrate(self, m: Migration, plan: Plan, sync_pass: SyncPass) -> None:
        """SDD §9.1: refine the change rate and scan rate after a warm pass, re-estimate."""
        if m.strategy not in WARM_STRATEGIES:
            return
        if not (sync_pass.bytes_scanned or sync_pass.bytes_changed or sync_pass.bytes_transferred):
            return  # the tool reported no byte counts (e.g. the VMware kit): nothing measured
        previous = m.sync_passes[-2] if len(m.sync_passes) >= 2 else None
        scale = max(self.settings.demo_speed, 1.0) if self.settings.demo else 1.0
        if sync_pass.kind != SyncPassKind.full:
            rate = calibrated_change_rate(previous, sync_pass, time_scale=scale)
            if rate is not None:
                m.vm = m.vm.model_copy(update={"change_rate_bps": rate})
        if m.strategy == Strategy.warm:
            observed = observed_scan_rate(sync_pass, m.vm, params_for_plan(plan))
            if observed is not None:
                m.observed_scan_bps = observed
        fresh = estimate(m.vm, m.strategy, self._params(m, plan), plan.downtime_slo_s)
        if m.estimate is not None:
            fresh = fresh.model_copy(
                update={"eligible": m.estimate.eligible, "reasons": list(m.estimate.reasons)}
            )
        m.estimate = fresh

    async def _record_pass(
        self, cur: Migration, v: int, sync_pass: SyncPass, plan: Plan | None = None
    ) -> tuple[Migration, int]:
        numbered = sync_pass.model_copy(update={"number": len(cur.sync_passes) + 1})
        cur.sync_passes.append(numbered)
        cur.bytes_transferred = sum(p.bytes_transferred for p in cur.sync_passes)
        if plan is not None:
            self._calibrate(cur, plan, numbered)
        v = await self._save(cur, v)
        await self._emit(
            "migration.sync_pass",
            f"{cur.vm.name}: pass {numbered.number} ({numbered.kind}) changed "
            f"{_fmt_bytes(numbered.bytes_changed)}",
            migration=cur,
            data=numbered.model_dump(mode="json"),
        )
        return cur, v

    def _converged(self, m: Migration, plan: Plan) -> tuple[bool, str]:
        last = m.sync_passes[-1]
        if last.bytes_changed <= plan.convergence_threshold_bytes:
            return True, f"converged: last pass changed {_fmt_bytes(last.bytes_changed)}"
        final = estimate_final_downtime(
            m.vm, m.strategy, float(last.duration_s or 0.0), self._params(m, plan)
        )
        if final <= plan.downtime_slo_s:
            return True, f"estimated final downtime {final:.0f} s is within the SLO"
        if len(m.sync_passes) >= plan.max_sync_passes:
            return True, f"max_sync_passes ({plan.max_sync_passes}) reached"
        return False, (
            f"another delta pass: changed {_fmt_bytes(last.bytes_changed)}, "
            f"estimated downtime {final:.0f} s"
        )

    async def _do_pass(self, m: Migration) -> None:
        step = StepName.PRECOPY if m.phase == P.precopy else StepName.SYNC
        try:
            result: StepResult | None = await self._run_step(m, step)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(m.id, m.phase, step, exc)
            return
        if result is None:
            return
        plan = await self._plan(m.plan_id)
        async with self._lock(m.id):
            cur, v = await self._load(m.id)
            if cur.phase != m.phase:
                return
            now = self._now()
            sync_pass = result.sync_pass or SyncPass(
                number=len(cur.sync_passes) + 1,
                kind=SyncPassKind.full if not cur.sync_passes else SyncPassKind.delta,
                started_at=now,
                ended_at=now,
                duration_s=0.0,
            )
            cur.progress_pct = 100.0
            cur.checkpoint = f"{step}:{len(cur.sync_passes) + 1}"
            cur, v = await self._record_pass(cur, v, sync_pass, plan)
            converged, why = self._converged(cur, plan)
            if converged:
                await self._transition(cur, v, P.awaiting_cutover, why)
            elif cur.phase == P.precopy:
                await self._transition(cur, v, P.syncing, why)
        if converged:
            self.wake()

    async def _do_cutover(self, m: Migration) -> None:
        try:
            result: StepResult | None = await self._run_step(m, StepName.CUTOVER)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(m.id, P.cutover, StepName.CUTOVER, exc)
            return
        if result is None:
            return
        plan = await self._plan(m.plan_id)
        async with self._lock(m.id):
            cur, v = await self._load(m.id)
            if cur.phase != P.cutover:
                if result.destination_server_id and not cur.destination_server_id:
                    cur.destination_server_id = result.destination_server_id
                    await self._save(cur, v)
                return
            if result.sync_pass is not None:
                cur, v = await self._record_pass(cur, v, result.sync_pass, plan)
            cur.destination_server_id = result.destination_server_id or cur.destination_server_id
            cur.checkpoint = "cutover"
            cur.progress_pct = 100.0
            if cur.downtime_started_at is None:
                # the executor did not report the stop: SDD §7.2 bounds the clock by the
                # start of the cutover step
                cur.downtime_started_at = _phase_started(cur, P.cutover) or self._now()
                v = await self._save(cur, v)
                await self._emit(
                    "migration.downtime_started", f"{cur.vm.name}: source VM stopped", migration=cur
                )
            await self._transition(
                cur,
                v,
                P.verifying,
                "cutover finished; verifying",
                data={"destination_server_id": cur.destination_server_id},
            )

    async def _do_verify(self, m: Migration) -> None:
        plan = await self._plan(m.plan_id)
        destination = await self.db.get("provider", plan.destination_provider_id, Provider)
        verifier = self._verifier_factory(self.providers.get(destination))
        try:
            result: VerificationResult | None = await self._run_step(
                m, "verify", runner=verifier.verify
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(m.id, P.verifying, "verify", exc)
            return
        if result is None:
            return
        note: AdvisorNote | None = None
        if plan.verification.use_advisor and self.advisor is not None:
            try:
                note = await self.advisor.review_verification(m.vm, result)
            except Exception:
                log.warning("verification review failed", exc_info=True)
        summary = result.summary()
        async with self._lock(m.id):
            cur, v = await self._load(m.id)
            if cur.phase != P.verifying:
                return
            if note is not None:
                cur.advisor_notes.append(note)
                if note.data.get("review_required"):
                    cur.review_required = True
                    cur.review_reason = note.data.get("review_reason")
            if result.passed:
                now = self._now()
                if cur.downtime_started_at is not None:
                    cur.downtime_ended_at = now
                    cur.actual_downtime_s = (now - cur.downtime_started_at).total_seconds()
                cur.checkpoint = "verified"
            v = await self._save(cur, v)
            # events first, then the terminal transition (nothing is lost on a stop)
            if note is not None:
                await self._emit(
                    "advisor.verification",
                    note.summary,
                    migration=cur,
                    data={"source": note.source, **note.data},
                )
            if result.passed:
                if cur.downtime_ended_at is not None:
                    await self._emit(
                        "migration.downtime_ended",
                        f"{cur.vm.name}: verified after {cur.actual_downtime_s:.0f} s of downtime",
                        migration=cur,
                        data={"actual_downtime_s": cur.actual_downtime_s},
                    )
                cur, v = await self._transition(
                    cur, v, P.completed, "verification passed", data={"verification": summary}
                )
        if not result.passed:
            failed = [
                f"{c['name']} ({c.get('detail')})"
                for c in result.checks
                if not c.get("ok") and not c.get("skipped")
            ]
            await self._fail(
                m.id,
                P.verifying,
                "verify",
                VerificationFailed("verification failed: " + "; ".join(failed)),
                data={"verification": summary},
            )
            return
        if self.knowledge is not None:
            await self.knowledge.on_completed(cur)
        self.wake()

    async def _do_rollback(self, m: Migration) -> None:
        try:
            result: StepResult | None = await self._run_step(m, StepName.ROLLBACK)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(m.id, P.rolling_back, StepName.ROLLBACK, exc)
            return
        if result is None:
            return
        old_source_id = m.vm.source_id
        async with self._lock(m.id):
            cur, v = await self._load(m.id)
            if cur.phase != P.rolling_back:
                return
            reason = next(
                (c.reason for c in reversed(cur.phase_history) if c.to_phase == P.rolling_back),
                "rollback",
            )
            if isinstance(result.details.get("vm"), dict):
                cur.vm = VMRef.model_validate(result.details["vm"])
            ended = cur.downtime_started_at is not None and cur.downtime_ended_at is None
            if ended:
                now = self._now()
                cur.downtime_ended_at = now
                cur.actual_downtime_s = (now - cur.downtime_started_at).total_seconds()
            cur.destination_server_id = None
            cur.checkpoint = "rolled_back"
            v = await self._save(cur, v)
            if ended:
                await self._emit(
                    "migration.downtime_ended",
                    f"{cur.vm.name}: source running again after {cur.actual_downtime_s:.0f} s",
                    migration=cur,
                    data={"actual_downtime_s": cur.actual_downtime_s},
                )
            cur, v = await self._transition(
                cur, v, P.rolled_back, "rollback finished; the source VM runs again"
            )
        if cur.vm.source_id != old_source_id:
            await self._replace_vm_id(cur.plan_id, old_source_id, cur.vm.source_id)
        if self.knowledge is not None:
            await self.knowledge.on_rolled_back(cur, reason)
        self.wake()

    async def _replace_vm_id(self, plan_id: str, old: str, new: str) -> None:
        plan = await self._plan(plan_id)
        plan.vm_ids = [new if v == old else v for v in plan.vm_ids]
        for wave in plan.waves:
            wave.vm_ids = [new if v == old else v for v in wave.vm_ids]
        if old in plan.strategy_overrides:
            plan.strategy_overrides[new] = plan.strategy_overrides.pop(old)
        await self._save_plan(plan)
        await self._emit(
            "plan.updated",
            f"{plan.name}: source VM {old} is now {new}",
            plan_id=plan.id,
            data={"vm_id_changed": {"from": old, "to": new}},
        )

    async def _fail(
        self,
        mid: str,
        expected: Phase,
        step: StepName | str,
        exc: BaseException,
        data: dict[str, Any] | None = None,
    ) -> None:
        message = redact(str(exc)).strip() or type(exc).__name__
        async with self._lock(mid):
            cur, v = await self._load(mid)
            if cur.phase != expected:
                return  # an operator action moved the migration meanwhile
            cur.error = f"{step}: {message}"[:1000]
            cur, v = await self._transition(
                cur, v, P.failed, f"{step} failed: {message[:200]}", data=data
            )
        await self._emit(
            "migration.error",
            f"{cur.vm.name}: {step} failed: {message[:300]}",
            migration=cur,
            data={"step": str(step), "error_class": type(exc).__name__, "message": message[:1000]},
        )
        if self.knowledge is not None:
            note = await self.knowledge.on_failure(cur, str(step), exc)
            if note is not None:
                async with self._lock(mid):
                    cur, v = await self._load(mid)
                    cur.advisor_notes.append(note)
                    await self._save(cur, v)
        plan = await self._plan(cur.plan_id)
        if (
            expected != P.rolling_back
            and cur.downtime_started_at is not None
            and plan.verification.auto_rollback
        ):
            async with self._lock(mid):
                cur, v = await self._load(mid)
                if cur.phase == P.failed:
                    await self._transition(
                        cur, v, P.rolling_back, f"automatic rollback after {step} failure"
                    )
            self._launch(mid)


def phases(migrations: Iterable[Migration]) -> Counter[str]:
    """Count migrations per phase (helper for stats/metrics)."""
    return Counter(str(m.phase) for m in migrations)
