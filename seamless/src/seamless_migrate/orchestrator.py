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
import weakref
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Literal

from .ai.decision import Advice, Approve, Override, decide
from .ai.evidence import build_strategy_evidence, evidence_count, keep_reasons
from .ai.policies import resolve_policy
from .ai.recommendations import LEGACY_PREFIX
from .ai.redaction import redact, redact_cut
from .ai.snapshots import created_event, decision_event, store_snapshot
from .config import Settings
from .domain import fsm
from .domain.enums import (
    SINGLE_SHOT_STRATEGIES,
    WARM_STRATEGIES,
    DecisionStatus,
    DecisionType,
    Phase,
    PlanStatus,
    ProviderRole,
    Strategy,
    SyncPassKind,
)
from .domain.models import (
    PLAN_EDITABLE_FIELDS,
    AdvisorModelInfo,
    AdvisorNote,
    AdvisorPolicy,
    AdvisorRecommendation,
    AdvisorSubject,
    Approval,
    Decision,
    Estimate,
    EvidenceItem,
    Finding,
    ForceCutover,
    Mappings,
    Migration,
    PhaseChange,
    Plan,
    Provider,
    RecommendedOption,
    SimilarIncident,
    SyncPass,
    ValidationItem,
    ValidationReport,
    VMRef,
    invalid_plan_settings,
    keep_sync_history,
    new_migration_id,
    new_recommendation_id,
    next_pass_number,
    repeated_vm_ids,
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
from .invariants import check_invariants
from .planning.estimator import (
    EstimatorParams,
    calibrated_change_rate,
    estimate,
    estimate_final_downtime,
    estimate_forced_downtime,
    final_downtime_parts,
    observed_scan_rate,
    params_for_plan,
)
from .planning.preflight import flavor_prestaged, has_blocker, resolve_mappings, run_preflight
from .planning.probes import suggest_probes
from .planning.selector import eligibility, select_strategy
from .planning.waves import heuristic_tier, plan_waves
from .providers.base import ProviderError
from .store import AsyncStore, ConflictError, NotFound, Store, VersionedDocuments
from .verification import VerificationResult, Verifier, review_reason

log = logging.getLogger(__name__)
P = Phase

PROGRESS_MIN_INTERVAL_S = 1.0
#: a migration.log line is cut to this many characters, after redaction (SDD §4)
LOG_LINE_CHARS = 2000
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
#: phases whose migration validation cancels when its VM leaves ``vm_ids`` (SDD §5.4); a
#: migration it finds in validating is a leftover of an interrupted validation (plan lock)
REMOVABLE = REVALIDATABLE | {P.failed, P.validating}
#: a migration in any other phase holds its VM: no other plan may migrate it (SDD §5.4)
RELEASES_VM = frozenset({P.cancelled, P.finalized, P.rolled_back})
#: validations and retries claim VMs across plans one at a time under this lock (SDD §5.4);
#: it is taken after a plan lock and before a migration lock
VM_CLAIMS = "vm-claims"
#: a provider's deletion checks and deletes under this lock, and a plan's creation, edit and retry
#: check their providers under it, so none saves a plan with a provider deleted meanwhile (SDD
#: §12); it is taken last and never held while another lock is awaited
PROVIDERS = "providers"
#: a migration in one of these depends on the plan's providers, mappings and strategy as they were
#: when it started: the plan is not edited or re-waved meanwhile (SDD §12)
IN_FLIGHT_PHASES = frozenset(
    {P.precopy, P.syncing, P.awaiting_cutover, P.cutover, P.verifying, P.rolling_back, P.completed}
)
#: what releases a migration that keeps its plan from changing, most urgent first (SDD §12): a
#: failed one holds the plan only while its cutover keeps the source stopped (the VM is down), a
#: completed one is finished and awaits finalize (§8.1); singular and plural of what it is
_RELEASES: tuple[tuple[str, str, str], ...] = (
    (
        "roll back or retry",
        "the failed migration whose source VM is stopped",
        "the failed migrations whose source VM is stopped",
    ),
    ("finish, roll back or cancel", "the migration in flight", "the migrations in flight"),
    ("finalize or roll back", "the completed migration", "the completed migrations"),
)


def blocks_plan_change(m: Migration) -> bool:
    """A migration in flight or whose source VM is stopped keeps its plan as it is (SDD §12)."""
    return m.phase in IN_FLIGHT_PHASES or fsm.source_stopped(m)


#: what a validation compares before it marks the plan `validated` (SDD §8): another process may
#: have edited, re-waved, started or paused the plan meanwhile under its own plan lock
_VALIDATION_WATCHES = PLAN_EDITABLE_FIELDS | {"waves", "status"}

#: a plan's clouds: a failed migration's rollback acts on the ones its attempt used (SDD §12)
PROVIDER_FIELDS = ("source_provider_id", "destination_provider_id")


def failed_provider_change_refusal(failed: list[str]) -> str:
    """The 409 of a change of a plan's providers while migrations named ``failed`` are failed
    (SDD §12), the same from PATCH /plans/{id} and from `seamless plan apply` (§15)."""
    noun = "migration" if len(failed) == 1 else "migrations"
    return (
        f"roll back or cancel the failed {noun} ({', '.join(failed[:10])}) before changing the "
        "plan's providers: its rollback acts on the clouds its attempt used"
    )


def in_flight_refusal(busy: Iterable[Migration], before: str, limit: int = 10) -> str:
    """The 409 of an edit or re-wave while ``busy`` are in flight (SDD §12): each VM, at most
    ``limit`` per kind and how many are left out, with what releases it."""
    names: list[list[str]] = [[], [], []]
    for m in busy:
        names[0 if m.phase == P.failed else 2 if m.phase == P.completed else 1].append(m.vm.name)
    clauses = []
    for (action, one, many), group in zip(_RELEASES, names, strict=True):
        if not group:
            continue
        group.sort()
        rest = len(group) - limit
        listed = ", ".join(group[:limit]) + (f" and {rest} more" if rest > 0 else "")
        clauses.append(f"{action} {one if len(group) == 1 else many} ({listed})")
    return f"{'; '.join(clauses)} before {before}"


class OrchestratorError(Exception):
    """Base class for errors raised to API callers."""


class NotAllowed(OrchestratorError):
    """The action is not allowed in the current state (HTTP 409)."""


class BadRequest(OrchestratorError, ValueError):
    """The request is invalid (HTTP 400)."""


class Unprocessable(OrchestratorError, ValueError):
    """The request is well formed but cannot be applied as given (HTTP 422)."""


class VerificationFailed(PermanentStepError):
    pass


def _add_model_s(m: Migration, details: dict[str, Any]) -> None:
    """Add the model seconds a simulated step kept the source stopped (demo, SDD §5.2)."""
    model_s = details.get("downtime_model_s")
    if isinstance(model_s, int | float):
        m.downtime_model_s = (m.downtime_model_s or 0.0) + float(model_s)


def _phase_started(m: Migration, phase: Phase) -> datetime | None:
    """When ``m`` last entered ``phase`` (None when it never did)."""
    for change in reversed(m.phase_history):
        if change.to_phase == phase:
            return change.at
    return None


#: a Force cutover: where it applies and its shortest reason (SDD §5.4, ADV-16)
FORCEABLE = frozenset({Phase.precopy, Phase.syncing})
FORCE_REASON_MIN = 10
#: the convergence rules (SDD §5.3)
ConvergedBy = Literal["threshold", "slo", "max_passes", "force"]

#: maker-checker refusals (SDD §5.4, §14.6, ADV-17)
OWN_CUTOVER = "You requested this cutover. Another approver must approve it."
#: the approver who forces a cutover is held to the requester's rule (SDD §5.4)
OWN_FORCE = "You forced this cutover. Another approver must approve it."
OWN_DECISION = "You requested this decision. Another approver must decide it."


def _refuse_unchangeable(m: Migration) -> None:
    """A strategy decision applies to pending, ready and blocked migrations only (SDD §8)."""
    if m.phase not in REVALIDATABLE:
        raise NotAllowed(f"the strategy cannot change in {m.phase}")


def _clear_request(m: Migration) -> None:
    """Drop the cutover request with its requester and the window bypass granted with it (§5.4)."""
    m.cutover_requested = False
    m.cutover_requested_by = None
    m.cutover_requested_at = None
    m.force_window = False
    m.force_cutover = None  # a Force cutover belongs to its assessment too (ADV-16)


def counted_approvals(m: Migration, plan: Plan) -> list[Approval]:
    """The approvals the gate counts: all, but under a distinct approver neither the requester's
    nor the forcer's (§5.4)."""
    if not plan.require_distinct_approver:
        return list(m.approvals)
    own = {m.cutover_requested_by, m.force_cutover.actor if m.force_cutover else None}
    return [a for a in m.approvals if a.actor not in own]


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
        # what the tick reads every second: only the migrations that changed are loaded again
        # (SDD §11, Performance.md); read-only - every change goes through _load/_save
        self._migrations = VersionedDocuments(self.db, "migration", Migration)
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
        # SDD §8: a lock lives while a coroutine holds it or waits for it
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        #: when the next source watch is due (SDD §8.1); set by the first tick
        self._source_watch_due: datetime | None = None
        #: the source watch in progress: the tick starts it and never waits for it (SDD §8.1)
        self._watch_task: asyncio.Task[None] | None = None
        self._drivers: dict[str, asyncio.Task[None]] = {}
        #: migration id -> (crashes in a row, monotonic time of the last one): relaunch backoff
        self._driver_crashes: dict[str, tuple[int, float]] = {}
        self._steps: dict[str, asyncio.Task[Any]] = {}
        self._prestage_done: set[str] = set()
        self._prestage_tasks: dict[str, asyncio.Task[None]] = {}
        #: best-effort rollback of the data path after a cancel in precopy/syncing (SDD §7.2)
        self._cleanups: dict[str, asyncio.Task[None]] = {}
        self._wave_events_seen: dict[str, set[tuple[str, str]]] = {}
        self._progress_at: dict[str, float] = {}
        self._tick_task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False
        #: step -> [sum_seconds, count] for seamless_step_duration_seconds
        self.step_stats: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
        #: tick timing (seconds): count, sum, last, max, slow (over half of tick_s)
        self.tick_stats: dict[str, float] = {
            "count": 0,
            "sum": 0.0,
            "last": 0.0,
            "max": 0.0,
            "slow": 0,
        }
        self._last_tick_at: float | None = None
        self._tick_errors = 0
        self._started = False

    # ------------------------------------------------------------------------------------------
    # plumbing
    def plan_lock(self, plan_id: str) -> asyncio.Lock:
        """The plan's lock (SDD §8): an edit through the API takes it, like the status writers."""
        return self._lock(f"plan:{plan_id}")

    def provider_lock(self) -> asyncio.Lock:
        """The lock of provider deletions and the provider checks of plans (SDD §12)."""
        return self._lock(PROVIDERS)

    async def _missing_provider(self, plan: Plan) -> tuple[str, ProviderRole] | None:
        """The first provider of ``plan`` that no longer exists, with its role (SDD §5.1)."""
        for provider_id, role in (
            (plan.source_provider_id, ProviderRole.source),
            (plan.destination_provider_id, ProviderRole.destination),
        ):
            try:
                await self.db.get("provider", provider_id, Provider)
            except NotFound:
                return provider_id, role
        return None

    def _lock(self, mid: str) -> asyncio.Lock:
        lock = self._locks.get(mid)
        if lock is None:
            lock = self._locks[mid] = asyncio.Lock()
        return lock

    @contextlib.asynccontextmanager
    async def _claims(self) -> AsyncIterator[None]:
        """The VM-claim lock (SDD §5.4): this process's, then the database-wide one, so a
        `seamless plan validate` beside the server cannot claim a VM the server claims."""
        async with self._lock(VM_CLAIMS), self.db.cross_process_lock(VM_CLAIMS):
            yield

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

    async def _update_plan(self, plan_id: str, mutate: Callable[[Plan], bool | None]) -> Plan:
        """Read-modify-write ``plan`` under optimistic concurrency.

        Several drivers and the tick loop touch the same plan concurrently (ids rewritten by a
        rollback, status changes, strategy overrides): ``mutate`` runs on the freshest copy and
        the write is retried when another writer got in between. ``mutate`` returns ``False``
        to leave the plan untouched.
        """
        for _ in range(8):
            plan, version = await self.db.get_versioned("plan", plan_id, Plan)
            if mutate(plan) is False:
                return plan
            plan.updated_at = self._now()
            try:
                await self.db.put("plan", plan, expected_version=version)
                return plan
            except ConflictError:
                continue
        raise RuntimeError(f"plan {plan_id} changed too often to update it")

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
        return await self._transitions(m, version, [(to, reason, data)], actor)

    async def _transitions(
        self,
        m: Migration,
        version: int,
        steps: list[tuple[Phase, str, dict[str, Any] | None]],
        actor: str = "system",
    ) -> tuple[Migration, int]:
        """Several transitions in one write — a crash or a failed event write never splits them
        (SDD §8) — then one ``migration.phase`` event each."""
        new = m
        for to, reason, _ in steps:
            new = fsm.transition(new, to, reason, actor, now=self._now())
        version = await self._save(new, version)
        before = m.phase
        for to, reason, data in steps:
            await self._emit(
                "migration.phase",
                f"{new.vm.name}: {before} -> {to} ({reason})",
                migration=new,
                actor=actor,
                data={"from": str(before), "to": str(to), "reason": reason, **(data or {})},
            )
            before = to
        return new, version

    # ------------------------------------------------------------------------------------------
    # providers
    async def check_provider(self, provider_id: str, actor: str = "system") -> Provider:
        # one check per provider at a time (SDD §8): a validation's check and a manual Check would
        # otherwise collide on the versioned write below; the innermost lock, it takes no other
        async with self._lock(f"provider:{provider_id}"):
            # versioned write: a provider deleted or edited while the check ran is not re-inserted
            provider, version = await self.db.get_versioned("provider", provider_id, Provider)
            impl = self.providers.get(provider)
            try:
                caps = await impl.check()
                update = {"capabilities": dict(caps), "status": "ok", "status_message": None}
            except ProviderError as exc:
                update = {"status": "error", "status_message": redact(str(exc))[:500]}
            update["last_checked_at"] = self._now()
            updated = provider.model_copy(update=update)
            await self.db.put("provider", updated, expected_version=version)
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
                # still a provider error: the API answers 502 with the redacted message (SDD §12),
                # never a 400 carrying the SDK's raw text
                raise ProviderError(f"VM {vm_id!r}: {exc}") from None
        return vms

    # ------------------------------------------------------------------------------------------
    # planning actions
    async def _held_elsewhere(self, plan: Plan, vm_ids: set[str]) -> dict[str, list[str]]:
        """The VMs of ``vm_ids`` that migrations of other plans with the same source provider hold
        (SDD §5.4: one VM, one migration across plans), by source id: ``name (plan "…", plan-id,
        phase)`` once per holding plan — plans validated before the rule may hold a VM several
        times, and plan names need not be unique, so the id tells two plans apart."""
        others = {
            other.id: other
            for other in await self.db.list("plan", Plan)
            if other.id != plan.id and other.source_provider_id == plan.source_provider_id
        }
        if not others or not vm_ids:
            return {}
        held: dict[str, set[str]] = {}
        for m in await self.db.list("migration", Migration, plan_id=list(others)):
            if m.vm.source_id in vm_ids and m.phase not in RELEASES_VM:
                holder = f'{m.vm.name} (plan "{others[m.plan_id].name}", {m.plan_id}, {m.phase})'
                held.setdefault(m.vm.source_id, set()).add(holder)
        return {vm_id: sorted(holders) for vm_id, holders in held.items()}

    @staticmethod
    def _holders(held: dict[str, list[str]], limit: int = 10) -> str:
        """At most ``limit`` holders from :meth:`_held_elsewhere`, and how many are left out."""
        entries = sorted(holder for holders in held.values() for holder in holders)
        rest = len(entries) - limit
        return ", ".join(entries[:limit]) + (f" and {rest} more" if rest > 0 else "")

    async def validate_plan(self, plan_id: str, actor: str) -> ValidationReport:
        # one validation per plan at a time: concurrent runs would create duplicate migrations;
        # and one claim of VMs at a time across plans (SDD §5.4)
        async with self._lock(f"plan:{plan_id}"):
            # SDD §8: refused before waiting for the claims — another plan's validation may hold
            # them for minutes, and the tick starts a running plan's steps under this lock
            if (await self._plan(plan_id)).status == PlanStatus.running:
                raise NotAllowed("pause the plan before validating it again")
            async with self._claims():
                return await self._validate_plan(plan_id, actor)

    async def _validate_plan(self, plan_id: str, actor: str) -> ValidationReport:
        plan = await self._plan(plan_id)
        # SDD §8: the settings, waves and status this validation assesses - a change by another
        # process (its own plan lock: an edit, auto-waves, a start, a pause) must never end up
        # `validated` with settings no validation saw, nor lose its status to this one
        seen = plan.model_dump(mode="json", include=_VALIDATION_WATCHES)
        if plan.status == PlanStatus.running:
            raise NotAllowed("pause the plan before validating it again")
        repeated = repeated_vm_ids(plan.vm_ids)
        if repeated:
            # SDD §5.4: two migrations of one VM would both cut it over
            raise BadRequest(f"vm_ids lists a VM more than once: {', '.join(repeated[:10])}")
        problems = invalid_plan_settings(plan)
        if problems:
            # SDD §12: a port outside 1-65535 fails every verification, a backwards window never
            # opens the gate
            raise BadRequest("; ".join(problems))
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
            raise ProviderError(f"inventory failed: {exc}") from None  # 502, redacted (SDD §12)
        vms = await self._vms(plan, src_impl)
        existing = {
            m.vm.source_id: m for m in await self.db.list("migration", Migration, plan_id=plan.id)
        }
        cancelled = sorted(
            m.vm.name for vm in vms if (m := existing.get(vm.source_id)) and m.phase == P.cancelled
        )
        if cancelled:
            # cancelled is terminal (SDD §5.1): the plan cannot carry the VM any further
            raise BadRequest(
                f"{len(cancelled)} VM(s) have a cancelled migration: {', '.join(cancelled[:10])}; "
                "remove them from vm_ids or create a new plan for them"
            )
        selected = set(plan.vm_ids)
        # the removed VMs' migrations cancelled below (SDD §5.4) — a failed one too, or it would
        # hold its VM and keep the plan from completing
        removed = [
            m for vm_id, m in existing.items() if vm_id not in selected and m.phase in REMOVABLE
        ]
        stopped = sorted(m.vm.name for m in removed if fsm.source_stopped(m))
        if stopped:
            # SDD §5.1 refuses those cancels while a source is stopped: refuse before any change
            raise NotAllowed(
                f"{', '.join(stopped[:10])}: the source VM is stopped after a failed cutover; keep "
                "the VM in the plan until it is cut over or rolled back"
            )
        restarted = sorted(
            m.vm.name for m in removed if m.phase == P.failed and m.downtime_started_at is not None
        )
        if restarted:
            # a failed migration whose cutover stopped the source is rolled back, never cancelled
            raise NotAllowed(
                f"{', '.join(restarted[:10])}: the source VM was stopped by a failed cutover; keep "
                "the VM in the plan until it is rolled back"
            )
        held = await self._held_elsewhere(plan, selected)
        if held:
            # SDD §5.4: two plans would both stop the source and cut it over
            raise NotAllowed(
                f"{len(held)} VM(s) already have a migration in another plan: "
                f"{self._holders(held)}; finish, roll back or cancel it there, or remove the VM "
                "from vm_ids"
            )
        params = params_for_plan(plan)
        # SDD §14.6: the policy in force for the plan's strategy decisions
        policy = resolve_policy(
            await self.db.list("advisor_policy", AdvisorPolicy),
            plan.id,
            plan.source_provider_id,
            DecisionType.migration_strategy,
            self.settings,
        )
        items: list[ValidationItem] = []
        # a memory outage is met once per validation: the search is not repeated for every VM
        memory_up = True
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
            automatic = (
                plan.default_strategy == "auto" and vm.source_id not in plan.strategy_overrides
            )
            # SDD §14.4: validation makes no choice for a migration in flight or finished (it
            # leaves it alone), so the advisor is not asked about it
            current = existing.get(vm.source_id)
            chooses = current is None or current.phase in (*REVALIDATABLE, P.validating)
            advice = None
            if self.advisor is not None and automatic and chooses:
                advice = await self.advisor.recommend_strategy(vm, estimates, plan, findings)
            decision = None
            rec_id = new_recommendation_id()
            # the evidence names the migration this validation writes, created below if new
            migration_id = current.id if current is not None else new_migration_id()
            evidence_option = None
            evidence: list[EvidenceItem] = []
            if advice is not None:
                # SDD §14.4 (ADV-02): the facts measured against the recommended option, or the
                # deterministic one when the advisor gave none; decide() counts those that bear
                evidence_option = (
                    advice.option if advice.option in advice.options else strategy.value
                )
                evidence = build_strategy_evidence(
                    vm,
                    plan,
                    source.kind,
                    estimates,
                    findings,
                    evidence_option,
                    current,
                    migration_id=migration_id,
                )
                # SDD §14.4: decide() takes the decision; the engine reads only its final option
                decision = decide(
                    advice, policy, evidence_count(evidence), strategy.value, now=self._now()
                )
                final = decision.final_option
                match = next((e for e in estimates if e.strategy == final), None)
                if match is not None and match.eligible and match.strategy != strategy:
                    strategy = match.strategy
                    why = (
                        f"advisor decision {decision.status}: {final} "
                        f"(confidence {advice.confidence:.2f})"
                    )
            migration = existing.get(vm.source_id) or await self._create_migration(
                plan, vm, strategy, actor, migration_id=migration_id
            )
            # a decision that waits for approval holds the migration from the same write on
            # (SDD §5.4, §14.6); any other validation clears an earlier hold
            waiting = decision is not None and decision.status == DecisionStatus.pending_approval
            migration = await self._apply_validation(
                migration.id,
                plan,
                vm,
                strategy,
                why,
                estimates,
                findings,
                actor,
                resolved,
                hold=rec_id if waiting else None,
            )
            # recorded only when this validation took the choice (ready or blocked now): one that
            # went in flight meanwhile was left alone and runs what it ran
            validated = migration.phase in (P.ready, P.blocked)
            if advice is not None and decision is not None and validated:
                incidents: list[SimilarIncident] = []
                if memory_up and self.knowledge is not None and evidence_option is not None:
                    found = await self.knowledge.similar_for(evidence_option, vm)
                    memory_up = found is not None
                    incidents = found or []
                await self._record_strategy_recommendation(
                    plan,
                    vm,
                    migration,
                    advice,
                    decision,
                    rec_id,
                    actor,
                    evidence_option=evidence_option,
                    evidence=evidence,
                    incidents=incidents,
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
        kept: list[dict[str, str]] = []
        for stale in removed:
            try:
                await self.cancel(stale.id, actor, "removed from the plan", by_validation=True)
            except NotAllowed:
                # SDD §5.4: an operator moved it meanwhile (a rollback): it stays as it is, and the
                # validation finishes
                moved, _ = await self._load(stale.id)
                if moved.phase != P.cancelled:
                    kept.append({"vm": moved.vm.name, "phase": str(moved.phase)})

        edited = False

        def mark_validated(fresh: Plan) -> bool | None:
            nonlocal edited
            edited = fresh.model_dump(mode="json", include=_VALIDATION_WATCHES) != seen
            if edited:
                return False  # as that change left it
            fresh.status = PlanStatus.validated
            return None

        plan = await self._update_plan(plan_id, mark_validated)
        if edited:
            raise ConflictError(
                f"plan {plan_id} changed while it was being validated (now {plan.status}); "
                "it was left as that change left it"
            )
        ok = all(item.phase != P.blocked for item in items)
        blocked = sum(1 for item in items if item.phase == P.blocked)
        left = "".join(f"; {k['vm']} left as it is ({k['phase']})" for k in kept)
        await self._emit(
            "plan.validated",
            f"{plan.name}: {len(items)} VM(s) validated, {blocked} blocked{left}",
            plan_id=plan.id,
            actor=actor,
            data={
                "ok": ok,
                "migrations": len(items),
                "blocked": blocked,
                **({"kept": kept} if kept else {}),
            },
        )
        return ValidationReport(plan_id=plan.id, ok=ok, migrations=items)

    async def _create_migration(
        self,
        plan: Plan,
        vm: VMRef,
        strategy: Strategy,
        actor: str,
        *,
        migration_id: str | None = None,
    ) -> Migration:
        now = self._now()
        wave = plan.wave_of(vm.source_id)
        migration = Migration(
            id=migration_id or new_migration_id(),
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
        actor: str,
        resolved: Mappings | None = None,
        *,
        hold: str | None = None,
    ) -> Migration:
        no_eligible = not any(e.eligible for e in estimates)
        blocked = has_blocker(findings) or no_eligible
        async with self._lock(mid):
            m, v = await self._load(mid)
            # SDD §5.4: one left in validating by an interrupted validation is taken over as it is
            # (validations of a plan run one at a time under its lock: none is at work on it)
            if m.phase not in REVALIDATABLE and m.phase != P.validating:
                return m  # already in flight or finished: leave it alone
            if m.phase != P.validating:
                m, v = await self._transition(m, v, P.validating, "pre-flight validation", actor)
            wave = plan.wave_of(vm.source_id)
            m.vm = vm
            m.strategy = strategy
            m.estimates = estimates
            m.estimate = next((e for e in estimates if e.strategy == strategy), None)
            m.findings = findings
            # what the rules would probe (SDD §9.6): inactive until a person adds it to the plan
            m.suggested_probes = suggest_probes(vm)
            m.resolved_mappings = resolved or Mappings()
            m.wave_id = wave.id if wave else None
            m.bytes_total = vm.used_bytes
            m.advisor_hold = hold
            # SDD §14.4: strategy decisions are recommendations now; the notes a migration has
            # stay - strategy notes of earlier versions are listed as legacy recommendations
            m.error = None
            # a fresh assessment needs a fresh approval (SDD §5.4): nothing approved before
            # this validation carries over to the new strategy, findings and estimate
            m.approvals = []
            _clear_request(m)
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
        return m

    async def _record_strategy_recommendation(
        self,
        plan: Plan,
        vm: VMRef,
        migration: Migration,
        advice: Advice,
        decision: Decision,
        rec_id: str,
        requested_by: str,
        *,
        evidence_option: str | None = None,
        evidence: Sequence[EvidenceItem] = (),
        incidents: Sequence[SimilarIncident] = (),
    ) -> AdvisorRecommendation:
        """Store the strategy recommendation, its decision, its evidence and its input; audit both
        (SDD §14.4, §14.5)."""
        answered = advice.unavailable is None
        snapshot = await store_snapshot(
            self.db,
            {
                "tool": "jev_decide",
                "arguments": dict(advice.request or {}),
                "scope": list(advice.scope),
                "options": list(advice.options),
                "deterministic": advice.deterministic,
                "deterministic_reason": advice.deterministic_reason,
            },
            now=decision.decided_at,
        )
        rec = AdvisorRecommendation(
            id=rec_id,
            created_at=decision.decided_at,
            decision_type=DecisionType.migration_strategy,
            subject=AdvisorSubject(kind="vm", id=vm.source_id, name=vm.name),
            plan_id=plan.id,
            migration_id=migration.id,
            provider_id=plan.source_provider_id,
            model=AdvisorModelInfo(
                name="jev" if answered else "rules",
                version=(advice.model_version or "unknown") if answered else "rules",
                # where the model that answered runs (SDD §4, §13.4); the rules ran here
                deployment=self.settings.jev_deployment if answered else "unknown",
                endpoint_host=self.settings.jev_endpoint_host if answered else None,
            ),
            recommendation=RecommendedOption(
                option=advice.option,
                confidence=advice.confidence,
                distribution=dict(advice.distribution),
            ),
            decision=decision,
            input_snapshot_sha256=snapshot,
            requested_by=requested_by,
            evidence_option=evidence_option,
            evidence=list(evidence),
            similar_incidents=list(incidents),
            reasons=keep_reasons(advice.reasons, evidence, recommendation_id=rec_id),
        )
        await self.db.put("recommendation", rec)
        for kind, message, actor, data in (created_event(rec), decision_event(rec)):
            await self._emit(kind, message, migration=migration, actor=actor, data=data)
        return rec

    async def in_flight(self, plan_id: str) -> list[Migration]:
        """The plan's migrations in flight (SDD §12: no edit, no re-wave meanwhile)."""
        return [
            m
            for m in await self.db.list("migration", Migration, plan_id=plan_id)
            if blocks_plan_change(m)
        ]

    async def auto_waves(self, plan_id: str, max_wave_size: int, actor: str) -> Plan:
        # status changes serialize on the plan's lock (SDD §8): a start waits instead of being
        # turned back into a draft
        async with self._lock(f"plan:{plan_id}"):
            return await self._auto_waves(plan_id, max_wave_size, actor)

    async def _auto_waves(self, plan_id: str, max_wave_size: int, actor: str) -> Plan:
        plan = await self._plan(plan_id)
        if plan.status not in (PlanStatus.draft, PlanStatus.validated, PlanStatus.paused):
            raise NotAllowed(f"waves cannot change while the plan is {plan.status}")
        busy = await self.in_flight(plan.id)
        if busy:
            raise NotAllowed(in_flight_refusal(busy, "re-planning the waves"))
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
        waves = plan_waves(vms, tiers, max_wave_size)

        def set_waves(fresh: Plan) -> None:
            if fresh.status not in (PlanStatus.draft, PlanStatus.validated, PlanStatus.paused):
                raise NotAllowed(f"waves cannot change while the plan is {fresh.status}")
            fresh.waves = waves
            fresh.status = PlanStatus.draft

        plan = await self._update_plan(plan.id, set_waves)
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
        async with self._lock(f"plan:{plan_id}"):  # SDD §8: after a validation or auto-waves
            return await self._start_plan(plan_id, actor)

    async def _start_plan(self, plan_id: str, actor: str) -> Plan:
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
        if plan.waves:
            wave_ids = {w.id for w in plan.waves}
            stray = [
                m.vm.name
                for m in migrations
                if m.phase not in fsm.WAVE_COMPLETE_PHASES
                and (m.wave_id is None or m.wave_id not in wave_ids)
            ]
            if stray:
                # a VM added after the waves were planned would otherwise start at once,
                # outside every wave's order and max_parallel (SDD §9.4)
                raise NotAllowed(
                    f"{len(stray)} migration(s) belong to no wave: {', '.join(stray[:10])}; "
                    "re-run the automatic wave planning or add them to a wave"
                )

        def start(fresh: Plan) -> None:
            if fresh.status not in (PlanStatus.validated, PlanStatus.paused, PlanStatus.failed):
                raise NotAllowed(f"a {fresh.status} plan cannot be started; validate it first")
            fresh.status = PlanStatus.running

        plan = await self._update_plan(plan.id, start)
        await self._emit("plan.started", f"{plan.name} started", plan_id=plan.id, actor=actor)
        self.wake()
        return plan

    async def pause_plan(self, plan_id: str, actor: str) -> Plan:
        async with self._lock(f"plan:{plan_id}"):  # SDD §8: status changes serialize
            return await self._pause_plan(plan_id, actor)

    async def _pause_plan(self, plan_id: str, actor: str) -> Plan:
        plan = await self._plan(plan_id)
        if plan.status == PlanStatus.paused:
            return plan
        if plan.status != PlanStatus.running:
            raise NotAllowed(f"only running plans can be paused (plan is {plan.status})")

        def pause(fresh: Plan) -> bool:
            if fresh.status != PlanStatus.running:
                return False
            fresh.status = PlanStatus.paused
            return True

        plan = await self._update_plan(plan.id, pause)
        await self._emit("plan.paused", f"{plan.name} paused", plan_id=plan.id, actor=actor)
        return plan

    # ------------------------------------------------------------------------------------------
    # migration actions (each serialized with the driver by the migration lock)
    async def approve(self, mid: str, actor: str, comment: str | None = None) -> Migration:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase not in APPROVABLE:
                raise NotAllowed(f"a migration in {m.phase} cannot be approved")
            plan = await self._plan(m.plan_id)
            if plan.require_distinct_approver and m.cutover_requested_by == actor:
                raise NotAllowed(OWN_CUTOVER)
            forcer = m.force_cutover.actor if m.force_cutover is not None else None
            if plan.require_distinct_approver and forcer == actor:
                raise NotAllowed(OWN_FORCE)
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
            # maker-checker (SDD §5.4): the request is an approval only without a distinct approver
            approves = not (await self._plan(m.plan_id)).require_distinct_approver
            if approves:
                m.approvals.append(Approval(actor=actor, at=self._now(), comment=comment))
            if m.cutover_requested_by is None:  # a repeated request keeps the first requester
                m.cutover_requested_by = actor
                m.cutover_requested_at = self._now()
            m.cutover_requested = True
            m.force_window = m.force_window or force_window
            await self._save(m, v)
        if approves:
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
            data={"action": "cutover", "force_window": force_window, "comment": comment},
        )
        self.wake()
        return m

    async def force_cutover(self, mid: str, actor: str, reason: str) -> Migration:
        """Cut over before convergence: recorded, audited; the gate still applies (SDD §5.4)."""
        reason = reason.strip()
        if len(reason) < FORCE_REASON_MIN:
            raise Unprocessable(
                f"a Force cutover needs a reason of at least {FORCE_REASON_MIN} characters"
            )
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase not in FORCEABLE or m.strategy not in WARM_STRATEGIES:
                raise NotAllowed(
                    "a Force cutover applies to a warm migration in pre-copy or syncing, "
                    f"not to {m.strategy} in {m.phase}"
                )
            if m.force_cutover is not None:
                # forced once (SDD §5.4): a second forcer would take the first one's place, whose
                # own approval would then count again, and the first record would be lost
                raise NotAllowed(
                    f"Already forced by {m.force_cutover.actor}: "
                    "the cutover follows the running pass."
                )
            plan = await self._plan(m.plan_id)
            last = m.sync_passes[-1] if m.sync_passes else None
            # after the running pass: as long as the last one that ended, or the first pass
            # while none has (SDD §5.4)
            estimated = estimate_forced_downtime(
                m.vm,
                m.strategy,
                float(last.duration_s or 0.0) if last is not None else None,
                self._params(m, plan),
            )
            now = self._now()
            m.force_cutover = ForceCutover(
                actor=actor,
                at=now,
                reason=reason,
                delta_bytes=last.bytes_changed if last is not None else 0,
                estimated_downtime_s=estimated,
            )
            # a request too, under maker-checker like POST ...cutover (SDD §5.4, ADV-17)
            if not plan.require_distinct_approver:
                m.approvals.append(Approval(actor=actor, at=now, comment=reason))
            if m.cutover_requested_by is None:
                m.cutover_requested_by = actor
                m.cutover_requested_at = now
            m.cutover_requested = True
            await self._save(m, v)
        await self._emit(
            "migration.force_cutover",
            f"cutover of {m.vm.name} forced by {actor}: {reason}",
            migration=m,
            actor=actor,
            data={
                # in the audit trail (SDD §14.5): its target is the migration
                "target_id": m.id,
                "vm": m.vm.name,
                "reason": reason,
                "delta_bytes": m.force_cutover.delta_bytes,
                "estimated_downtime_s": estimated,
            },
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
            if m.phase == P.completed and m.checkpoint == "finalizing":
                # SDD §8.1: the rollback would delete the destination server and find no source
                raise NotAllowed("the finalize began deleting the source VM: finalize it again")
            # SDD §5.1: without its providers the rollback fails, and a migration whose cutover
            # stopped its source could then be neither rolled back, retried nor cancelled
            missing = await self._missing_provider(await self._plan(m.plan_id))
            if missing is not None:
                provider_id, role = missing
                raise NotAllowed(
                    f"{m.vm.name} cannot be rolled back: the plan's {role} provider "
                    f"{provider_id!r} no longer exists"
                )
            # SDD §8.1: the rollback starts the source on purpose; a migration that completes
            # again is watched afresh
            m.source_running_at = None
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
        # the provider check and the save of ready share the deletions' lock (SDD §12): a ready
        # migration holds its VM, so a deletion after it refuses and one before it is seen here
        async with self._claims(), self._lock(mid), self._lock(PROVIDERS):
            m, v = await self._load(mid)
            if m.phase not in (P.failed, P.rolled_back):
                raise NotAllowed(f"a migration in {m.phase} cannot be retried")
            plan = await self._plan(m.plan_id)
            held = await self._held_elsewhere(plan, {m.vm.source_id})
            if held:
                # SDD §5.4: a rolled-back migration let its VM go; another plan may have taken it
                raise NotAllowed(
                    f"{m.vm.name} cannot be retried: a migration in another plan holds the VM: "
                    f"{self._holders(held)}; finish, roll back or cancel it there first"
                )
            # only a completed plan's providers can be deleted (SDD §12), and the plan may still
            # complete before the reopen below: the new attempt needs both
            missing = await self._missing_provider(plan)
            if missing is not None:
                provider_id, role = missing
                raise NotAllowed(
                    f"{m.vm.name} cannot be retried: the plan's {role} provider "
                    f"{provider_id!r} no longer exists; create a new plan for the VM"
                )
            if m.phase == P.rolled_back:
                # the FSM counts failed -> ready only; a retry after an automatic rollback
                # is still a new attempt of the cutover (executors key their behaviour on it)
                m.attempts += 1
            # everything below is saved in the same write as the move to ready (SDD §5.1): a crash
            # never leaves a ready migration with the earlier attempt's request and bypass
            # the strategy of the attempt being retried: the evidence names it (SDD §14.4), even
            # after a later validation chose another
            m.attempt_strategies = [*m.attempt_strategies, m.strategy]
            m.error = None
            # a new attempt is requested anew: the window bypass granted with the old request goes
            # with it, approvals stay (SDD §5.1, §5.4)
            _clear_request(m)
            # what the earlier attempt found about itself goes with it: the new attempt records
            # its own (SDD §5.1, §5.3, §5.5)
            m.data_issues = []
            m.converged_by = None
            m.checkpoint = None
            m.progress_pct = 0
            m.review_required = False
            m.review_reason = None
            # the downtime clock belongs to the outage (SDD §5.2): a closed clock — verified boot,
            # or the source running again after a rollback — starts afresh; an open one means the
            # source has not run since it stopped, so the next cutover counts from that first stop
            # (in demo mode its model seconds add to those the clock kept)
            if not fsm.source_stopped(m):
                m.downtime_started_at = None
                m.downtime_ended_at = None
                m.actual_downtime_s = None
                m.downtime_model_s = None
            m, v = await self._transition(m, v, P.ready, f"retry requested by {actor}", actor)
        reopened = False

        def reopen(fresh: Plan) -> bool:
            nonlocal reopened
            reopened = fresh.status == PlanStatus.completed  # re-evaluated on every retry
            if not reopened:
                return False
            fresh.status = PlanStatus.running
            return True

        # SDD §5.1/§8: a completed plan is driven no more, so the new attempt reopens it - under
        # the plan's lock, taken only now: held across the claim wait above, it would hold up the
        # tick; a completion that wrote before the transition is seen here, a later one sees ready
        async with self._lock(f"plan:{m.plan_id}"):
            plan = await self._update_plan(m.plan_id, reopen)
            if reopened:
                await self._emit(
                    "plan.updated",
                    f"{plan.name} reopened: retry of {m.vm.name}",
                    plan_id=plan.id,
                    actor=actor,
                    data={"status": "running"},
                )
        await self._emit(
            "migration.action",
            f"retry of {m.vm.name}",
            migration=m,
            actor=actor,
            data={"action": "retry", "attempts": m.attempts},
        )
        self.wake()
        return m

    async def cancel(
        self, mid: str, actor: str, reason: str | None = None, *, by_validation: bool = False
    ) -> Migration:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase == P.validating and not by_validation:
                # SDD §5.1: a validation of the plan may be moving it; only a validation cancels
                # one - under the plan's lock it knows it is a leftover whose VM left vm_ids
                raise NotAllowed(
                    f"{m.vm.name} is being validated and cannot be cancelled; validate the plan "
                    "again - it finishes the migration, or cancels it once its VM left vm_ids"
                )
            why = fsm.refusal(m, P.cancelled)
            if why is not None:
                if P.cancelled not in fsm.TRANSITIONS[m.phase]:
                    raise NotAllowed(f"a migration in {m.phase} cannot be cancelled")
                raise NotAllowed(f"{m.vm.name} cannot be cancelled: {why}")  # e.g. source stopped
            m.advisor_hold = None  # a cancelled migration takes no decision (SDD §14.6)
            # SDD §5.1: in precopy or syncing a pass runs, waits for its retry (the cancel ends
            # the retries, §8) or has just finished unrecorded; with the passes a migration
            # recorded, that data path left snapshots, temporary and destination volumes behind,
            # and `cancelled` is terminal: no later action could remove them
            data_path = m.phase in (P.precopy, P.syncing) or bool(m.sync_passes)
            if data_path:
                m.checkpoint = "cleanup"  # saved with cancelled: a crash cannot lose the cleanup
            m, v = await self._transition(
                m, v, P.cancelled, f"cancelled: {reason or 'no reason given'}", actor
            )
            step = self._steps.get(mid)
            if step is not None:
                step.cancel()
        await self._emit(
            "migration.action",
            f"{m.vm.name} cancelled",
            migration=m,
            actor=actor,
            data={"action": "cancel", "reason": reason},
        )
        if data_path and not self._stopping:
            # the rollback step removes them (a cancel never leaves the source stopped, §5.1)
            self._cleanups[mid] = asyncio.create_task(
                self._cleanup_after_cancel(m, step, actor), name=f"cleanup:{mid}"
            )
        self.wake()
        return m

    async def _cleanup_after_cancel(
        self, m: Migration, step: asyncio.Task[Any] | None, actor: str
    ) -> None:
        recorded = False

        async def record(outcome: str, report: Coroutine[Any, Any, None]) -> None:
            """The step ran: its outcome event and checkpoint are saved even when a stop() comes
            meanwhile — the stop waits for them, and the outcome stands (SDD §5.1)."""
            nonlocal recorded

            async def save() -> None:
                await report
                await self._cleanup_recorded(m.id, outcome)

            saving = asyncio.ensure_future(save())
            try:
                await asyncio.shield(saving)
            except asyncio.CancelledError:
                await asyncio.wait({saving})
                recorded = not saving.cancelled() and saving.exception() is None
                raise
            recorded = True

        try:
            if step is not None:
                # the executor kills the playbook and removes its secret files; the step's own
                # outcome does not matter, but a stop() cancelling this task is not swallowed
                await asyncio.wait({step})
            try:
                plan = await self._plan(m.plan_id)
                source = await self.db.get("provider", plan.source_provider_id, Provider)
                destination = await self.db.get("provider", plan.destination_provider_id, Provider)
                ctx = self._context(
                    m, plan, source, destination, options={"delete_dest_volumes": True}, locked=True
                )
                await self.executors.for_strategy(m.strategy).run(StepName.ROLLBACK, ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await record(
                    "cleanup_failed",
                    self._emit(
                        "migration.error",
                        f"{m.vm.name}: cleanup after the cancel failed: "
                        f"{redact(str(exc))[:300]}; run rollback_workloads.yml for the workload to "
                        "remove its temporary resources",
                        migration=m,
                        data={"step": "cleanup", "error_class": type(exc).__name__},
                    ),
                )
                return
            await record(
                "cleaned",
                self._emit(
                    "migration.action",
                    f"{m.vm.name}: temporary resources of the cancelled migration removed",
                    migration=m,
                    actor=actor,
                    data={"action": "cleanup"},
                ),
            )
        except asyncio.CancelledError:
            # a shutdown interrupted the cleanup before its outcome was recorded: its mark stays,
            # so the next start runs it, and the event says so; a recorded outcome stands (SDD §5.1)
            if not recorded:
                with contextlib.suppress(Exception):
                    await self._emit(
                        "migration.error",
                        f"{m.vm.name}: cleanup after the cancel was interrupted by a shutdown; it "
                        "runs again when the orchestrator starts",
                        migration=m,
                        data={"step": "cleanup", "error_class": "CancelledError"},
                    )
            raise
        finally:
            # tracked until it has finished, its outcome event included: whoever waits for the
            # cleanup (or cancels it, as stop() does) never finds it gone before its outcome
            self._cleanups.pop(m.id, None)

    async def _cleanup_recorded(self, mid: str, outcome: str) -> None:
        """The cancel's cleanup recorded its outcome: its mark goes, so no start runs it again
        (SDD §5.1)."""
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase == P.cancelled and m.checkpoint == "cleanup":
                m.checkpoint = outcome
                await self._save(m, v)

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
            if delete_source:
                # SDD §8.1: from here the source may be gone, even if the step fails — a failed
                # finalize is finalized again, never rolled back
                m.checkpoint = "finalizing"
                v = await self._save(m, v)
            ctx = self._context(
                m, plan, source, destination, options={"delete_source": delete_source}, locked=True
            )
            try:
                result = await executor.run(StepName.FINALIZE, ctx)
            except (TransientStepError, PermanentStepError) as exc:
                # redacted and cut like any reason a skipped VM reports (SDD §8.1)
                raise NotAllowed(f"finalize failed: {redact_cut(str(exc), 500)}") from exc
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

    async def finalize_plan(
        self,
        plan_id: str,
        actor: str,
        *,
        wave_id: str | None,
        confirm: str,
        delete_source: bool = False,
    ) -> dict[str, Any]:
        """Finalize a wave or the plan with one approval, each VM as ``finalize`` (SDD §8.1)."""
        plan = await self._plan(plan_id)
        if wave_id is not None:
            wave = next((w for w in plan.waves if w.id == wave_id), None)
            if wave is None:
                raise NotFound("wave", wave_id)
            scope, name = set(wave.vm_ids), wave.name
        else:
            scope, name = None, plan.name
        if confirm != name:
            raise BadRequest(f"confirm must equal the name {name!r}")
        migrations = await self.db.list("migration", Migration, plan_id=plan_id)
        waiting = [
            m
            for m in migrations
            if m.phase == P.completed and (scope is None or m.vm.source_id in scope)
        ]
        if not waiting:
            raise NotAllowed(f"no completed migration to finalize in {name}")
        finalized: list[Migration] = []
        skipped: list[dict[str, str]] = []
        for m in sorted(waiting, key=lambda x: x.vm.name):
            try:
                finalized.append(
                    await self.finalize(m.id, actor, delete_source=delete_source, confirm=m.vm.name)
                )
            except NotAllowed as exc:  # left completed meanwhile, or the cleanup failed
                skipped.append({"migration_id": m.id, "reason": str(exc)})
            except Exception as exc:  # any other error skips this VM, not the others (SDD §8.1)
                log.warning("finalize of %s failed", m.id, exc_info=True)
                reason = redact_cut(str(exc), 500).strip() or type(exc).__name__
                skipped.append({"migration_id": m.id, "reason": f"finalize failed: {reason}"})
        names = {m.id: m.vm.name for m in waiting}
        await self._emit(
            "plan.finalized",
            f"{name}: {len(finalized)} finalized by {actor}"
            + (f", {len(skipped)} skipped" if skipped else ""),
            plan_id=plan_id,
            actor=actor,
            data={
                "wave_id": wave_id,
                "finalized": [m.vm.name for m in finalized],
                "skipped": [{"vm": names[s["migration_id"]], **s} for s in skipped],
            },
        )
        return {"finalized": [m.id for m in finalized], "skipped": skipped}

    async def source_client(self, plan: Plan) -> Any:
        """The client of the plan's source provider (the source watch, the demo controls)."""
        provider = await self.db.get("provider", plan.source_provider_id, Provider)
        return self.providers.get(provider)

    async def demo_power(self, source_id: str, *, on: bool) -> VMRef:
        """Power a fake source VM on or off (demo mode, SDD §8.1); NotFound for an unknown VM."""
        for provider in await self.db.list("provider", Provider):
            if provider.role != ProviderRole.source:
                continue
            client = self.providers.get(provider)
            try:
                await client.get_vm(source_id)
            except ProviderError:
                continue
            await (client.power_on if on else client.power_off)(source_id)
            vm: VMRef = await client.get_vm(source_id)
            return vm
        raise NotFound("source VM", source_id)

    def _start_source_watch(self) -> None:
        """Start the source watch when it is due, as a task of its own, one at a time; the tick
        never waits for it - a slow source provider must not hold the gates and passes (SDD
        §8.1)."""
        if self._watch_task is not None and not self._watch_task.done():
            return  # a slow watch delays the next one, nothing else
        now = self._now()
        scale = max(self.settings.demo_speed, 1.0) if self.settings.demo else 1.0
        interval = timedelta(seconds=self.settings.source_watch_s / scale)
        if self._source_watch_due is None:
            self._source_watch_due = now + interval
        if now < self._source_watch_due:
            return
        self._source_watch_due = now + interval
        self._watch_task = asyncio.create_task(self._watch_sources(), name="seamless-source-watch")

    async def _watch_sources(self) -> None:
        """A completed migration's source VM seen running again is flagged once, and cleared once
        seen stopped again; each change is an event (SDD §8.1)."""
        try:
            await self._watch_sources_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("the source watch failed", exc_info=True)

    async def _watch_sources_once(self) -> None:
        clients: dict[str, Any] = {}
        # each plan read once per pass, a plan that is gone too: a plan of 1,000 VMs is ~22 KB,
        # and in demo the watch runs every few seconds
        plans: dict[str, Plan | None] = {}
        for m in await self.db.list("migration", Migration, phase=P.completed):
            try:
                if m.plan_id not in plans:
                    try:
                        plans[m.plan_id] = await self._plan(m.plan_id)
                    except NotFound:
                        plans[m.plan_id] = None
                plan = plans[m.plan_id]
                if plan is None:
                    continue
                if plan.source_provider_id not in clients:
                    clients[plan.source_provider_id] = await self.source_client(plan)
                vm = await clients[plan.source_provider_id].get_vm(m.vm.source_id)
            except (ProviderError, NotFound):
                continue  # gone (a finalize deleted it, a handover rebuilt it) or unreachable
            except Exception:
                log.warning("source watch failed for %s", m.id, exc_info=True)
                continue
            seen = self._now()  # when the provider answered: one call may take minutes (§8.1)
            state = str(vm.power_state)
            if state not in ("running", "stopped"):
                # a reboot or a resize, a paused VM, an error, an unknown state: not seen
                # stopped - the alert stays as it is (SDD §8.1)
                continue
            running = state == "running"
            if running == (m.source_running_at is not None):
                continue
            async with self._lock(m.id):
                cur, v = await self._load(m.id)
                if cur.phase != P.completed or running == (cur.source_running_at is not None):
                    continue
                cur.source_running_at = seen if running else None
                await self._save(cur, v)
            if running:
                await self._emit(
                    "migration.source_running",
                    f"{cur.vm.name}: the source VM runs again - its data diverges from RHOSO; "
                    "stop it, or roll back",
                    migration=cur,
                    data={"power_state": str(vm.power_state)},
                )
            else:
                await self._emit(
                    "migration.source_stopped",
                    f"{cur.vm.name}: the source VM is stopped again",
                    migration=cur,
                    data={"power_state": str(vm.power_state)},
                )

    async def set_strategy(self, mid: str, strategy: Strategy | str, actor: str) -> Migration:
        strategy = Strategy(strategy)
        current, _ = await self._load(mid)
        _refuse_unchangeable(current)  # before the plan's lock (SDD §8); repeated under it
        plan_id = current.plan_id
        # SDD §8: the plan's lock first, as the advisor's override (§14.6): a running validation
        # finishes before the change, and no validation undoes it afterwards
        async with self._lock(f"plan:{plan_id}"), self._lock(mid):
            m, v = await self._load(mid)
            if m.phase not in REVALIDATABLE:
                raise NotAllowed(f"the strategy cannot change in {m.phase}")
            if m.advisor_hold is not None:
                raise NotAllowed(
                    f"the advisor's decision ({m.advisor_hold}) waits for approval: approve or "
                    "override it (advisor.approve)"
                )
            est = m.estimate_for(strategy)
            if est is None:
                raise BadRequest(f"strategy {strategy} does not apply to this VM (validate first)")
            if not est.eligible:
                raise BadRequest(f"strategy {strategy} is not eligible: {'; '.join(est.reasons)}")
            m.strategy = strategy
            m.estimate = est
            m.approvals = []  # the approval was given for the previous strategy (SDD §5.4)
            _clear_request(m)
            await self._save(m, v)
            source_id = m.vm.source_id

            def override(plan: Plan) -> None:
                plan.strategy_overrides[source_id] = strategy

            plan = await self._update_plan(m.plan_id, override)
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

    async def approve_recommendation(self, rec_id: str, actor: str) -> AdvisorRecommendation:
        """Apply the advisor's recommended option (SDD §14.6)."""
        return await self._decide_recommendation(rec_id, Approve(user=actor), actor)

    async def override_recommendation(
        self, rec_id: str, option: str, reason: str | None, actor: str
    ) -> AdvisorRecommendation:
        """Choose another eligible option than the advisor did (SDD §14.6)."""
        reason = (reason or "").strip() or None
        return await self._decide_recommendation(
            rec_id, Override(user=actor, option=option, reason=reason), actor
        )

    async def _decide_recommendation(
        self, rec_id: str, action: Approve | Override, actor: str
    ) -> AdvisorRecommendation:
        if rec_id.startswith(LEGACY_PREFIX):
            raise NotAllowed("a legacy entry is history: it cannot be approved or overridden")
        rec = await self.db.get("recommendation", rec_id, AdvisorRecommendation)
        if rec.migration_id is None or rec.plan_id is None:
            raise NotAllowed(f"recommendation {rec_id} decides nothing a person can change yet")
        mid, plan_id = rec.migration_id, rec.plan_id
        _refuse_unchangeable((await self._load(mid))[0])  # before the plan's lock (SDD §8)
        async with self._lock(f"plan:{plan_id}"), self._lock(mid):
            stored = await self.db.list("recommendation", AdvisorRecommendation, migration_id=mid)
            newest = max(stored, key=lambda r: (r.created_at, r.id))
            if newest.id != rec_id:
                raise NotAllowed(f"a newer recommendation ({newest.id}) replaced {rec_id}")
            rec = newest
            plan = await self._plan(plan_id)
            policies = await self.db.list("advisor_policy", AdvisorPolicy)
            policy = resolve_policy(
                policies, plan.id, plan.source_provider_id, rec.decision_type, self.settings
            )
            # SDD §14.6: the rules of the policy that took the decision, as they were then; the
            # policy in force only for a decision that recorded none (the fallback, older data)
            distinct = rec.decision.require_distinct_approver
            if distinct is None:
                distinct = policy.require_distinct_approver
            reason_required = rec.decision.require_override_reason
            if reason_required is None:
                reason_required = policy.require_override_reason
            if distinct and rec.requested_by == actor:
                raise NotAllowed(OWN_DECISION)  # maker-checker (SDD §14.6, ADV-17)
            if isinstance(action, Override) and reason_required:
                if len(action.reason or "") < 10:
                    raise Unprocessable(
                        f"policy {rec.decision.policy_id or policy.id} requires an override "
                        "reason of at least 10 characters"
                    )
            m, v = await self._load(mid)
            if m.phase not in REVALIDATABLE:
                raise NotAllowed(f"the strategy cannot change in {m.phase}")
            pinned = plan.strategy_overrides.get(m.vm.source_id)
            if pinned is not None:
                # SDD §14.6: a person set the strategy in the plan, which a validation honours
                # without a recommendation - the newest one no longer stands
                raise NotAllowed(
                    f"The plan sets the strategy of {m.vm.name} ({pinned}): change it in the plan."
                )
            if isinstance(action, Approve) and rec.decision.status == DecisionStatus.approved:
                raise NotAllowed("The decision is already approved.")  # one event per change
            eligible = tuple(e.strategy.value for e in m.estimates if e.eligible)
            advice = Advice(
                option=rec.recommendation.option,
                confidence=rec.recommendation.confidence,
                scope=eligible,
                options=eligible,
            )
            try:
                decision = decide(
                    advice, policy, 0, m.strategy.value, action=action, now=self._now()
                )
            except ValueError as exc:
                if isinstance(action, Override):
                    raise Unprocessable(str(exc)) from None
                raise NotAllowed(str(exc)) from None
            before = rec.decision
            # the rules go on with a person's decision: a later one follows them too
            decision = decision.model_copy(
                update={
                    "require_distinct_approver": distinct,
                    "require_override_reason": reason_required,
                }
            )
            rec = rec.model_copy(update={"decision": decision})
            await self.db.put("recommendation", rec)
            final = Strategy(str(decision.final_option))
            changed = final != m.strategy
            m.strategy = final
            m.estimate = m.estimate_for(final)
            m.advisor_hold = None
            if changed:  # the approval was given for the previous strategy (SDD §5.4)
                m.approvals = []
                _clear_request(m)
            await self._save(m, v)
            if isinstance(action, Override):
                source_id = m.vm.source_id

                def keep(fresh: Plan) -> None:
                    fresh.strategy_overrides[source_id] = final

                # SDD §14.6: under the plan's lock, with the decision - a validation waiting for
                # the lock honours the override instead of asking the advisor again
                plan = await self._update_plan(plan_id, keep)
        if isinstance(action, Override):
            await self._emit(
                "plan.updated",
                f"{plan.name}: strategy override for {m.vm.name}",
                plan_id=plan.id,
                actor=actor,
                data={"strategy_overrides": {source_id: str(final)}},
            )
        kind, message, by, data = decision_event(rec, before)
        await self._emit(kind, message, migration=m, actor=by, data=data)
        if changed:
            await self._emit(
                "migration.action",
                f"{m.vm.name}: strategy set to {final} by the advisor decision",
                migration=m,
                actor=actor,
                data={"action": "strategy", "strategy": str(final)},
            )
        return rec

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
            elif (
                m.phase == P.cancelled and m.checkpoint == "cleanup" and m.id not in self._cleanups
            ):
                # a cleanup a stop or a crash cut short runs again (SDD §5.1)
                self._cleanups[m.id] = asyncio.create_task(
                    self._cleanup_after_cancel(m, None, "system"), name=f"cleanup:{m.id}"
                )
            if any(c.to_phase in (P.precopy, P.cutover) for c in m.phase_history):
                self._prestage_done.add(m.plan_id)  # work started: resources were pre-staged
        self._tick_task = asyncio.create_task(self._loop(), name="seamless-orchestrator")
        self._started = True

    def pending_cleanups(self) -> int:
        """How many cancels' cleanups this process runs now (SDD §5.1)."""
        return len(self._cleanups)

    async def drain_cleanups(self) -> None:
        """Wait for the cancels' cleanups this process started - the CLI exits after them, as
        its end would cut them short (SDD §15)."""
        while self._cleanups:
            await asyncio.gather(*list(self._cleanups.values()), return_exceptions=True)

    async def stop(self) -> None:
        self._stopping = True
        tasks = [
            t
            for t in (
                self._tick_task,
                self._watch_task,
                *self._drivers.values(),
                *self._prestage_tasks.values(),
                *self._steps.values(),
                *self._cleanups.values(),
            )
            if t
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tick_task = None
        self._watch_task = None
        self._started = False
        self._drivers.clear()
        self._steps.clear()
        self._prestage_tasks.clear()
        self._cleanups.clear()

    async def _loop(self) -> None:
        assert self._wake is not None
        while not self._stopping:
            started = time.monotonic()
            self._last_tick_at = started  # heartbeat: the loop is alive even during a long tick
            try:
                await self.tick()
                self._tick_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._tick_errors += 1
                log.exception("orchestrator tick failed")
            self._record_tick(time.monotonic() - started)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.tick_s)
            self._wake.clear()

    def health(self) -> dict[str, Any]:
        """Liveness of the tick loop for ``GET /health`` (readiness probes, SDD §12).

        ``running`` is true while the loop task is alive; ``last_tick_age_s`` is the time since
        the last completed tick (None before the first one); ``healthy`` is false when the loop
        is dead after ``start()`` or has not ticked for five tick intervals.
        """
        task = self._tick_task
        running = task is not None and not task.done()
        age = None if self._last_tick_at is None else time.monotonic() - self._last_tick_at
        # a tick heartbeats when it starts and when it ends: a long tick is not a dead loop
        stale = age is not None and age > max(5 * self.settings.tick_s, 30.0)
        failing = self._tick_errors >= 3  # the loop runs but every tick raises
        return {
            "running": running,
            "last_tick_age_s": None if age is None else round(age, 3),
            "ticks": int(self.tick_stats["count"]),
            "healthy": (running and not stale and not failing)
            or (not self._started and not running),
        }

    def _record_tick(self, seconds: float) -> None:
        """Tick timing for ``GET /metrics`` (QASuite PERF-CP-02: p95 below half of tick_s)."""
        stats = self.tick_stats
        self._last_tick_at = time.monotonic()
        stats["count"] += 1
        stats["sum"] += seconds
        stats["last"] = seconds
        stats["max"] = max(stats["max"], seconds)
        budget = self.settings.tick_s / 2
        if seconds > budget:
            stats["slow"] += 1
            log.warning("orchestrator tick took %.3f s (budget %.3f s)", seconds, budget)

    async def tick(self) -> None:
        await self._resume_orphans()
        self._start_source_watch()
        all_plans = await self.db.list("plan", Plan)
        plans = [p for p in all_plans if p.status == PlanStatus.running]
        if not plans:
            return
        # only the migrations of plans that can still have work: running ones, plus paused and
        # re-validated ones whose in-flight steps still count against the concurrency limits
        live = (PlanStatus.running, PlanStatus.paused, PlanStatus.validated)
        active = {p.id for p in all_plans if p.status in live}
        migrations = [m for m in await self._migrations.get() if m.plan_id in active]
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
        if plan.require_approval and not counted_approvals(m, plan):
            return False
        window = plan.cutover_window
        if window is not None and not m.force_window and not window.contains(now):
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
            if counts["cutover"] >= self.settings.max_concurrent_cutovers:
                continue  # waits for a cutover slot: keep-warm passes go on meanwhile
            if counts["steps"] >= limit_steps or (m.phase == P.ready and not wave_room(m)):
                continue
            if await self._begin(
                m.id, {P.ready, P.awaiting_cutover}, P.cutover, "cutover gate open"
            ):
                gate_open.add(m.id)
                counts["cutover"] += 1
                counts["steps"] += 1
                in_flight[m.wave_id] += 1

        # 2. keep-warm delta passes while waiting for the gate (or for a cutover slot)
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
            await self._complete(plan)

    async def _complete(self, plan: Plan) -> None:
        # SDD §8: under the plan's lock, on the migrations re-read there — a retry that returned
        # after the tick's snapshot keeps the plan running (SDD §5.1)
        async with self._lock(f"plan:{plan.id}"):
            migs = await self.db.list("migration", Migration, plan_id=plan.id)
            if not all(m.phase in fsm.WAVE_COMPLETE_PHASES for m in migs):
                return
            completed = False

            def finish(fresh: Plan) -> bool:
                nonlocal completed
                completed = fresh.status == PlanStatus.running  # re-evaluated on every retry
                if not completed:
                    return False
                fresh.status = PlanStatus.completed
                return True

            await self._update_plan(plan.id, finish)
            if completed:
                done = sum(1 for m in migs if m.phase in fsm.SUCCESS_PHASES)
                await self._emit(
                    "plan.completed",
                    f"{plan.name} completed",
                    plan_id=plan.id,
                    data={"migrations": len(migs), "succeeded": done},
                )

    async def _begin(self, mid: str, allowed: set[Phase], to: Phase, reason: str) -> bool:
        plan_id = (await self._load(mid))[0].plan_id
        # SDD §8: under the plan's lock (then the migration's, the documented order), so a pause
        # that returned starts nothing the tick chose on its snapshot
        async with self._lock(f"plan:{plan_id}"), self._lock(mid):
            if (await self._plan(plan_id)).status != PlanStatus.running:
                return False
            m, v = await self._load(mid)
            if m.phase not in allowed:
                return False
            if m.advisor_hold is not None:
                return False  # its advisor decision waits for approval (SDD §5.4, §14.6)
            # the tick chose it for its strategy on its snapshot; set_strategy under this lock may
            # have changed it since: a pre-copy is a warm migration's, a cutover from ready a
            # single-shot one's (SDD §8)
            if to == P.precopy and m.strategy not in WARM_STRATEGIES:
                return False
            if to == P.cutover and m.phase == P.ready and m.strategy not in SINGLE_SHOT_STRATEGIES:
                return False
            if to == P.cutover and not self._gate(m, await self._plan(m.plan_id), self._now()):
                # the tick saw the gate open on its snapshot; an action under this lock (e.g.
                # set_strategy clearing the approvals) may have closed it since (SDD §5.4)
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
                if "flavors" in plan.prestage_resources and (
                    "prestage_flavors" in inspect.signature(executor.prestage).parameters
                ):
                    # only the plan's flavors RHOSO lacks: a destination flavor is shared by
                    # every project and never pre-staged over (SDD §7.2)
                    dst_inv = await self.providers.get(destination).inventory()
                    kwargs["prestage_flavors"] = sorted(
                        {
                            m.vm.flavor
                            for m in migs
                            if m.vm.flavor and flavor_prestaged(m.vm, plan, dst_inv)
                        }
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
            failed = False

            def fail(fresh: Plan) -> bool:
                nonlocal failed
                failed = fresh.status == PlanStatus.running  # re-evaluated on every retry
                if not failed:
                    return False
                fresh.status = PlanStatus.failed
                return True

            # SDD §8: a status writer like the others, under the plan's lock; only a plan that
            # still runs fails. One paused, validated again or edited meanwhile keeps its status
            # (an edited draft must not become a failed plan that start_plan accepts unvalidated)
            # and pre-stages again at its next start
            async with self._lock(f"plan:{plan.id}"):
                await self._update_plan(plan.id, fail)
                error = redact(str(exc))[:500]
                await self._emit(
                    "plan.updated",
                    f"{plan.name}: pre-staging failed",
                    plan_id=plan.id,
                    data={"status": "failed", "error": error} if failed else {"error": error},
                )
        finally:
            self._prestage_tasks.pop(plan.id, None)

    # ------------------------------------------------------------------------------------------
    # migration driver
    async def _resume_orphans(self) -> None:
        """SDD §8: drive again a migration in a resumable phase whose driver task died — after a
        backoff while it keeps crashing, and never while its step task still runs."""
        now = time.monotonic()
        migrations = await self._migrations.get()
        for m in (m for m in migrations if m.phase in fsm.RESUMABLE_PHASES):
            driver, step = self._drivers.get(m.id), self._steps.get(m.id)
            if (driver is not None and not driver.done()) or (step is not None and not step.done()):
                continue
            crashes, at = self._driver_crashes.get(m.id, (0, 0.0))
            if crashes and now - at < min(300.0, 2**crashes * self.settings.tick_s):
                continue
            self._launch(m.id)

    async def _report_driver_crash(self, mid: str, exc: Exception, crashes: int) -> None:
        """Best effort: the store may be what failed."""
        message = redact(str(exc)).strip() or type(exc).__name__
        try:
            m, _ = await self._load(mid)
            await self._emit(
                "migration.error",
                f"{m.vm.name}: the migration driver stopped unexpectedly ({type(exc).__name__}); "
                "it resumes from its checkpoint",
                migration=m,
                data={
                    "step": "driver",
                    "error_class": type(exc).__name__,
                    "message": message[:1000],
                    "crashes": crashes,
                },
            )
        except Exception:
            log.exception("could not report the crash of the driver of %s", mid)

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
                    self._driver_crashes.pop(mid, None)
                    return
                await handler(m)
        except asyncio.CancelledError:
            raise
        except NotFound:
            return
        except Exception as exc:
            log.exception("driver of %s crashed", mid)
            crashes = self._driver_crashes.get(mid, (0, 0.0))[0] + 1
            self._driver_crashes[mid] = (crashes, time.monotonic())
            await self._report_driver_crash(mid, exc, crashes)
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

        async def mark_downtime_start(at: datetime | None = None) -> None:
            if not locked:
                await self._mark_downtime(mid, at)

        async def log_line(line: str) -> None:
            text = redact_cut(line, LOG_LINE_CHARS)
            await self._emit("migration.log", text, plan_id=m.plan_id, persist=False, migration=m)

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
                cur.bytes_transferred = (
                    cur.sync_bytes_dropped
                    + sum(p.bytes_transferred for p in cur.sync_passes)
                    + int(done)
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

    async def _mark_downtime(self, mid: str, at: datetime | None = None) -> None:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.downtime_started_at is not None:
                return
            m.downtime_started_at = at or self._now()
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
        """Run ``step`` as a cancellable task with transient retries; ``None`` if cancelled or once
        the migration left the step's phase."""
        plan = await self._plan(m.plan_id)
        source = await self.db.get("provider", plan.source_provider_id, Provider)
        destination = await self.db.get("provider", plan.destination_provider_id, Provider)
        executor = self.executors.for_strategy(m.strategy)
        attempt = 0
        loop = asyncio.get_running_loop()
        while True:
            async with self._lock(m.id):
                # SDD §8: an attempt starts under the migration's lock and only while the
                # migration is still in the step's phase, so a cancel either finds it running
                # or ends the retries (and cleans up after the failed attempt, §5.1)
                current, _ = await self._load(m.id)
                if current.phase != m.phase:
                    return None
                ctx = self._context(current, plan, source, destination)
                work = runner(ctx) if runner is not None else executor.run(step, ctx)
                task = asyncio.create_task(work, name=f"{step}:{m.id}")
                self._steps[m.id] = task
            started = loop.time()
            timeout = self.settings.step_timeout_s or None
            timed_out = False
            try:
                done, _ = await asyncio.wait({task}, timeout=timeout)
                if not done:
                    # SDD §15.1: the attempt exceeded its wall-clock ceiling — cancel it (the
                    # executor kills the playbook) and fail it like any other permanent error
                    timed_out = True
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
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
            if timed_out:
                raise PermanentStepError(
                    f"{step} exceeded the step timeout of {self.settings.step_timeout_s:g} s"
                )
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
        if sync_pass.kind == SyncPassKind.final:
            return  # the estimate stays the one the convergence rule used (SDD §5.3, ADV-16)
        fresh = estimate(m.vm, m.strategy, self._params(m, plan), plan.downtime_slo_s)
        if m.estimate is not None:
            fresh = fresh.model_copy(
                update={"eligible": m.estimate.eligible, "reasons": list(m.estimate.reasons)}
            )
        m.estimate = fresh

    async def _record_pass(
        self, cur: Migration, v: int, sync_pass: SyncPass, plan: Plan | None = None
    ) -> tuple[Migration, int]:
        numbered = sync_pass.model_copy(update={"number": next_pass_number(cur)})
        cur.sync_passes.append(numbered)
        if plan is not None:
            # SDD §5.4: a long wait's keep-warm passes must not grow the migration without bound
            keep_sync_history(cur, plan.max_sync_passes)
        cur.bytes_transferred = cur.sync_bytes_dropped + sum(
            p.bytes_transferred for p in cur.sync_passes
        )
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

    def _converged(self, m: Migration, plan: Plan) -> tuple[ConvergedBy | None, str]:
        """The rule that ends the passes, or ``None`` and why another pass runs (SDD §5.3)."""
        last = m.sync_passes[-1]
        if m.force_cutover is not None:  # the only way past the rules below (SDD §5.4)
            force = m.force_cutover
            return "force", f"Force cutover by {force.actor}: {force.reason}"
        counted = Strategy(m.strategy) != Strategy.vmware_warm  # CBT passes report no bytes
        if counted and last.bytes_changed <= plan.convergence_threshold_bytes:
            return "threshold", f"converged: last pass changed {_fmt_bytes(last.bytes_changed)}"
        final = estimate_final_downtime(
            m.vm, m.strategy, float(last.duration_s or 0.0), self._params(m, plan)
        )
        if final <= plan.downtime_slo_s:
            return "slo", f"estimated final downtime {final:.0f} s is within the SLO"
        if len(m.sync_passes) >= plan.max_sync_passes:
            return "max_passes", f"max_sync_passes ({plan.max_sync_passes}) reached"
        return None, (
            f"another delta pass: changed {_fmt_bytes(last.bytes_changed)}, "
            f"estimated downtime {final:.0f} s"
            if counted
            else f"another CBT pass: estimated downtime {final:.0f} s"
        )

    def _converge(self, m: Migration, plan: Plan, rule: ConvergedBy) -> None:
        """Record the rule, and make the estimate the one it used: the final-downtime estimate
        with its parts (SDD §5.3, ADV-16)."""
        m.converged_by = rule
        last = m.sync_passes[-1]
        parts = final_downtime_parts(
            m.vm, m.strategy, float(last.duration_s or 0.0), self._params(m, plan)
        )
        if m.estimate is not None:
            downtime = sum(parts.values())
            m.estimate = m.estimate.model_copy(
                update={
                    "downtime_s": downtime,
                    "downtime_parts": parts,
                    "total_s": m.estimate.precopy_s + downtime,
                    "meets_slo": downtime <= plan.downtime_slo_s,
                }
            )

    def _downtime_s(self, m: Migration, ended: datetime, plan: Plan) -> float:
        """The downtime of a verified migration (SDD §5.2): wall time, or - after a simulated
        cutover - its model seconds plus the plan's boot time, like the passes' durations."""
        if m.downtime_model_s is not None:
            return m.downtime_model_s + self._params(m, plan).boot_s
        assert m.downtime_started_at is not None
        return (ended - m.downtime_started_at).total_seconds()

    def _rollback_downtime_s(self, m: Migration, ended: datetime, plan: Plan) -> float:
        """The downtime a rollback ends (SDD §5.2): wall time, or - in demo mode - the model
        seconds the simulated steps reported, plus the plan's boot time when the cutover had
        finished (the destination booted for its verification)."""
        if m.downtime_model_s is not None:
            booted = m.checkpoint == "cutover"
            return m.downtime_model_s + (self._params(m, plan).boot_s if booted else 0.0)
        assert m.downtime_started_at is not None
        return (ended - m.downtime_started_at).total_seconds()

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
                number=next_pass_number(cur),
                kind=SyncPassKind.full if not cur.sync_passes else SyncPassKind.delta,
                started_at=now,
                ended_at=now,
                duration_s=0.0,
            )
            cur.progress_pct = 100.0
            cur.checkpoint = f"{step}:{next_pass_number(cur)}"
            cur, v = await self._record_pass(cur, v, sync_pass, plan)
            rule, why = self._converged(cur, plan)
            if rule is not None:
                self._converge(cur, plan, rule)
                await self._transition(cur, v, P.awaiting_cutover, why)
            elif cur.phase == P.precopy:
                await self._transition(cur, v, P.syncing, why)
        if rule is not None:
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
            # a simulated cutover counts its downtime in model seconds, added to those an open
            # clock kept from an earlier attempt (SDD §5.2, ADV-16)
            _add_model_s(cur, result.details)
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
        # what the rules ask review for (SDD §7.5, ADV-18): an unreachable probe, a database VM
        # that only booted - with or without the advisor
        rules_reason = review_reason(m.vm, plan, result) if result.passed else None
        # SDD §14.2: the advisor may flag a *passed* verification; a failed one carries
        # provider error text (endpoints) the review cannot change and must not ship
        if plan.verification.use_advisor and self.advisor is not None and result.passed:
            try:
                note = await self.advisor.review_verification(
                    m.vm, result, review_reason=rules_reason
                )
            except Exception:
                log.warning("verification review failed", exc_info=True)
        summary = result.summary()
        async with self._lock(m.id):
            cur, v = await self._load(m.id)
            if cur.phase != P.verifying:
                return
            # the checks and probe results, passed or failed (SDD §7.5)
            cur.verification = result.record(self._now())
            if rules_reason:
                cur.review_required = True
                cur.review_reason = rules_reason
            if note is not None:
                cur.advisor_notes.append(note)
                if note.data.get("review_required"):
                    cur.review_required = True
                    cur.review_reason = note.data.get("review_reason")
            if result.passed:
                now = self._now()
                # SDD §5.2: a verification run again (a stop or a failed event write came before
                # completed) keeps the end the first one recorded: the outage ended then
                if cur.downtime_started_at is not None and cur.downtime_ended_at is None:
                    cur.downtime_ended_at = now
                    cur.actual_downtime_s = self._downtime_s(cur, now, plan)
                cur.checkpoint = "verified"
                # the numbers of a completed migration must hold together (SDD §5.5, ADV-16)
                cur.data_issues = check_invariants(
                    cur, await self._plan(cur.plan_id), self.settings
                )
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
                if cur.data_issues:
                    await self._emit(
                        "migration.data_inconsistent",
                        f"{cur.vm.name}: its numbers do not hold together - "
                        + "; ".join(cur.data_issues),
                        migration=cur,
                        data={"issues": list(cur.data_issues)},
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
        plan = await self._plan(m.plan_id)
        vm = result.details.get("vm")
        recreated = VMRef.model_validate(vm) if isinstance(vm, dict) else None
        if recreated is not None and recreated.source_id != m.vm.source_id:
            # SDD §7.3: the plan follows a recreated source VM before the migration is rolled
            # back - rolled_back is never resumed, so a stop after it would lose the rewrite; a
            # stop before it resumes rolling_back, and the rewrite changes nothing the second time
            await self._replace_vm_id(m.plan_id, m.vm.source_id, recreated.source_id)
        async with self._lock(m.id):
            cur, v = await self._load(m.id)
            if cur.phase != P.rolling_back:
                return
            reason = next(
                (c.reason for c in reversed(cur.phase_history) if c.to_phase == P.rolling_back),
                "rollback",
            )
            if recreated is not None:
                cur.vm = recreated
            ended = fsm.source_stopped(cur)
            if ended:
                now = self._now()
                cur.downtime_ended_at = now
                _add_model_s(cur, result.details)
                cur.actual_downtime_s = self._rollback_downtime_s(cur, now, plan)
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
        if self.knowledge is not None:
            await self.knowledge.on_rolled_back(cur, reason)
        self.wake()

    async def _replace_vm_id(self, plan_id: str, old: str, new: str) -> None:
        """The plan follows a recreated source VM (SDD §7.3): every field keyed by its id; once
        done nothing names ``old``, and a repeat writes nothing."""
        changed = False

        def rewrite(plan: Plan) -> bool:
            nonlocal changed
            before = plan.model_dump()
            plan.vm_ids = [new if v == old else v for v in plan.vm_ids]
            for wave in plan.waves:
                wave.vm_ids = [new if v == old else v for v in wave.vm_ids]
            if old in plan.strategy_overrides:
                plan.strategy_overrides[new] = plan.strategy_overrides.pop(old)
            if old in plan.probe_overrides:
                plan.probe_overrides[new] = plan.probe_overrides.pop(old)
            changed = plan.model_dump() != before
            return changed

        plan = await self._update_plan(plan_id, rewrite)
        if not changed:
            return
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
            if isinstance(exc, PermanentStepError) and fsm.source_stopped(cur):
                # the model seconds a failed simulated step kept the source stopped (SDD §5.2)
                _add_model_s(cur, exc.details)
            plan = await self._plan(cur.plan_id)
            steps: list[tuple[Phase, str, dict[str, Any] | None]] = [
                (P.failed, f"{step} failed: {message[:200]}", data)
            ]
            rollback = (
                expected != P.rolling_back
                and cur.downtime_started_at is not None
                and plan.verification.auto_rollback
            )
            if rollback:
                # SDD §8: saved with the failure — a restart or a failed event write right after
                # it resumes the rollback, never leaving the stopped VM in failed
                steps.append((P.rolling_back, f"automatic rollback after {step} failure", None))
            cur, v = await self._transitions(cur, v, steps)
        await self._emit(
            "migration.error",
            f"{cur.vm.name}: {step} failed: {message[:300]}",
            migration=cur,
            data={"step": str(step), "error_class": type(exc).__name__, "message": message[:1000]},
        )
        if rollback:
            self._launch(mid)
        if self.knowledge is not None:
            note = await self.knowledge.on_failure(cur, str(step), exc)
            if note is not None:
                async with self._lock(mid):
                    cur, v = await self._load(mid)
                    cur.advisor_notes.append(note)
                    await self._save(cur, v)


def phases(migrations: Iterable[Migration]) -> Counter[str]:
    """Count migrations per phase (helper for stats/metrics)."""
    return Counter(str(m.phase) for m in migrations)
