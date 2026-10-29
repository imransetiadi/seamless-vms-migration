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
* A server's orchestrator runs while its replica leads (``seamless_migrate.leader``, SDD §8): it
  checks its renew deadline and the lease's epoch before each action on a cloud, and a takeover's
  ``start()`` begins as a new process would.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import ipaddress
import logging
import random
import time
import weakref
from collections import Counter, defaultdict, deque
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from .ai.decision import Advice, Approve, Override, decide, unusable
from .ai.evidence import build_strategy_evidence, evidence_count, keep_reasons
from .ai.policies import resolve_policy
from .ai.recommendations import LEGACY_PREFIX
from .ai.redaction import redact, redact_cut
from .ai.snapshots import created_event, decision_event, store_snapshot
from .config import Settings
from .conversion_key import resolve as resolve_conversion_key
from .credentials import (
    Step,
    credential_project,
    for_step,
    host_address,
    host_pin,
    implementation,
    reservation,
    verification_view,
    with_host_pin,
)
from .dns_swap import (
    DnsSwapError,
    backend_for,
    describe,
    rdtype_for,
    record_name,
    record_ttls,
    swap_addresses,
)
from .domain import fsm
from .domain.enums import (
    SINGLE_SHOT_STRATEGIES,
    WARM_STRATEGIES,
    DecisionStatus,
    DecisionType,
    Phase,
    PlanStatus,
    ProviderKind,
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
    DnsSwap,
    Estimate,
    Event,
    EvidenceItem,
    Finding,
    FipMove,
    ForceCutover,
    GpuWait,
    Hook,
    HookPoint,
    HostKeyPin,
    LbMove,
    Mappings,
    Migration,
    Nic,
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
    to_utc,
    utc_text,
    utcnow,
)
from .events import EventBus, emit, new_event, put_all_and_emit, put_and_emit
from .executors.ansible import recorded_copied_data, recorded_destination_volumes, run_dir
from .executors.base import (
    PermanentStepError,
    ProcessGroups,
    StepContext,
    StepName,
    StepResult,
    TransientStepError,
    outlive_cancel,
)
from .hooks import HookRun, HookRunner, context_document, hook_action_event
from .invariants import check_invariants
from .lb_helper import (
    LbHelperError,
    Pace,
    canonical,
    join_address,
    member_of,
    vm_addresses,
)
from .lb_helper import describe as describe_lb
from .planning.estimator import (
    EstimatorParams,
    calibrated_change_rate,
    estimate,
    estimate_final_downtime,
    estimate_forced_downtime,
    final_downtime_parts,
    hooks_part,
    observed_extent_query_s,
    observed_scan_rate,
    params_for_plan,
    tracked_disks,
    whole_pass_seconds,
)
from .planning.gpu import PoolKey, gpu_flavor, gpu_units, pool_key, pool_name, vm_gpus
from .planning.platforms import destination_descriptor
from .planning.preflight import (
    PoolPair,
    QuotaShare,
    Shortfall,
    flavor_prestaged,
    gpu_shortfalls,
    has_blocker,
    keep_detached_volumes,
    keypair_fingerprint,
    keypair_prestaged,
    qualifying_disks,
    quota_share,
    resolve_mappings,
    run_preflight,
    server_gpu_flavor,
    tracking_ready,
)
from .planning.probes import suggest_probes
from .planning.selector import HELD_VOLUMES_REASON, eligibility, select_strategy
from .planning.waves import heuristic_tier, plan_waves
from .providers.base import ProviderError, VMNotFound
from .security.host_keys import console_keys, fingerprint
from .store import AsyncStore, ConflictError, NotFound, Store, VersionedDocuments
from .verification import VerificationResult, Verifier, review_reason

log = logging.getLogger(__name__)
P = Phase

PROGRESS_MIN_INTERVAL_S = 1.0
#: how long a plan with work in flight waits before a failed pre-staging is tried again (SDD §8)
PRESTAGE_RETRY_S = 60.0
#: a migration.log line is cut to this many characters, after redaction (SDD §4)
LOG_LINE_CHARS = 2000
#: the name the executor gives a side's conversion host that has none (SDD §7.2, §10)
CONVERSION_HOST_NAMES = {"source": "os_migrate_conv_src", "destination": "os_migrate_conv_dst"}


class _HostKeyBook:
    """What a run of a plan - a migration's step, or the plan's pre-staging - records of its
    conversion hosts' keys (SDD §7.2 "Conversion host keys", §26.4, §26.8): each pin with
    conversion_host.key_pinned in one transaction."""

    def __init__(self, orchestrator: Orchestrator, plan_id: str, migration_id: str | None) -> None:
        self._orchestrator = orchestrator
        self.plan_id = plan_id
        self.migration_id = migration_id

    async def pin_from_console(self, provider: Provider) -> Provider:
        """The provider once its conversion host, without a pin, was pinned from its console by
        the provider check's path (§10, §26.8); as stored when nothing was pinned. The host is
        looked up where the run reaches it - with the plan's credential (§7.2, §27.5) - and
        pinned as that credential's pin (§26.4)."""
        return await self._orchestrator._pin_from_console(provider.id, self.plan_id)

    async def pin_injected(
        self, provider: Provider, side: str, server_id: str, keys: Sequence[str]
    ) -> None:
        """The server the pre-staging's role created with the key the run injected, pinned with
        it (§26.3) as the pin of the plan's credential on that side (§26.4, §27.5)."""
        await self._orchestrator._pin_injected(provider.id, side, server_id, keys, self.plan_id)

    async def refused(self, message: str, data: Mapping[str, Any]) -> None:
        """conversion_host.key_refused (§4.3, §7.2): what the run's output refused, with the
        run's plan and migration."""
        ids = {"plan_id": self.plan_id, "migration_id": self.migration_id}
        await emit(
            self._orchestrator.store,
            self._orchestrator.bus,
            "conversion_host.key_refused",
            message,
            **ids,
            data={**data, **ids},
        )


def _address(address: str) -> str:
    """An address as Neutron writes it (SDD §8.3): canonical, else as given."""
    try:
        return ipaddress.ip_address(address).compressed
    except ValueError:
        return address


def _floating_addresses(vm: VMRef) -> list[str]:
    """The addresses of the floating IPs of the VM's NICs, each once (SDD §4.2, §8.3)."""
    return list(dict.fromkeys(_address(a) for nic in vm.nics for a in nic.floating_ips))


def _journal_entry(m: Migration, address: str) -> FipMove | None:
    return next((e for e in m.fip_moves if e.address == address), None)


async def _read_pool(provider: Any, pool_id: str) -> dict[str, Any] | None:
    """An Octavia pool as validation reads it (SDD §8.5, §10 ``lb_pool``): None when the provider
    has no Octavia - a VMware source - or the read fails, its kind logged, never a name."""
    read = getattr(provider, "lb_pool", None)
    if read is None:
        return None
    try:
        found: dict[str, Any] | None = await read(pool_id)
    except Exception as exc:
        log.info("an Octavia pool was not read: %s", type(exc).__name__)
        return None
    return found


async def _read_key(provider: Any, name: str) -> str | None:
    """The fingerprint of the credential's user's keypair called ``name`` as validation reads it
    (SDD §9.3 DST_KEYPAIR_KEY_MISMATCH, §10 ``keypair_public_key``): None when the provider reads
    no keypair, its user holds none of the name, its key is no OpenSSH public key, or the read
    fails - its kind logged, never a name."""
    read = getattr(provider, "keypair_public_key", None)
    if read is None:
        return None
    try:
        key = await read(name)
    except Exception as exc:
        log.info("a keypair's public key was not read: %s", type(exc).__name__)
        return None
    return keypair_fingerprint(key)


async def _keypair_fingerprints(
    vms: Iterable[VMRef], src_inv: Any, dst_inv: Any, source: Any, destination: Any
) -> dict[str, tuple[str | None, str | None]]:
    """For each key name the plan's VMs boot with that both inventories list - both credentials'
    users hold a keypair of it -, the fingerprints of the source's and the destination's key,
    each read with the plan's credential on its side (SDD §9.3, §27.5, §27.6, plan task TC6)."""
    held = set(getattr(src_inv, "keypairs", None) or []) & set(
        getattr(dst_inv, "keypairs", None) or []
    )
    names = sorted({vm.key_name for vm in vms if vm.key_name and vm.key_name in held})
    return {
        name: (await _read_key(source, name), await _read_key(destination, name)) for name in names
    }


#: the points whose hooks a rollback reverts (SDD §8.2)
_REVERTED = frozenset({"pre_stop", "post_create", "post_verify"})


class HookFailed(Exception):
    """A hook's failure at its point, which fails the migration there (SDD §8.2): named by the
    hook and the run's redacted detail."""

    def __init__(self, run: HookRun) -> None:
        super().__init__(f"hook {run.hook} failed: {run.detail or 'no detail'}")
        self.run = run


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
#: the migrations a validation re-assesses - one left in validating is an interrupted validation's
#: (plan lock) -, whose VMs it reads from the source; any other counts as its recorded vm (§5.4)
REASSESSED = REVALIDATABLE | {P.validating}
#: phases whose migration validation cancels when its VM leaves ``vm_ids`` (SDD §5.4); a
#: migration it finds in validating is a leftover of an interrupted validation (plan lock)
REMOVABLE = REVALIDATABLE | {P.failed, P.validating}
#: a migration in any other phase holds its VM: no other plan may migrate it (SDD §5.4)
RELEASES_VM = frozenset({P.cancelled, P.finalized, P.rolled_back})
#: a plan is cancelled only while none of its migrations started (SDD §12)
UNSTARTED = frozenset({P.pending, P.blocked, P.ready})
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


def cleanup_pending(m: Migration) -> bool:
    """A cancel's cleanup that has not recorded its outcome: it runs again at the next start, and
    it needs both providers to remove what the cancelled data path left (SDD §5.1, §12)."""
    return m.phase == P.cancelled and m.checkpoint == "cleanup"


def blocks_plan_change(m: Migration) -> bool:
    """A migration in flight or whose source VM is stopped keeps its plan as it is (SDD §12)."""
    return m.phase in IN_FLIGHT_PHASES or fsm.source_stopped(m)


#: what a validation compares before it marks the plan `validated` (SDD §8): another process may
#: have edited, re-waved, started or paused the plan meanwhile under its own plan lock
_VALIDATION_WATCHES = PLAN_EDITABLE_FIELDS | {"waves", "status"}

#: a plan's clouds: a failed migration's rollback acts on the ones its attempt used (SDD §12); its
#: projects (PROJECT_FIELDS) likewise, as it acts with its attempt's credentials (§27.4)
PROVIDER_FIELDS = ("source_provider_id", "destination_provider_id")


def failed_provider_change_refusal(failed: list[str], *, projects: bool = False) -> str:
    """The 409 of a change of a plan's providers - its projects with ``projects`` - while
    migrations named ``failed`` are failed (SDD §12, §27.4), the same from PATCH /plans/{id} and
    from `seamless plan apply` (§15)."""
    noun = "migration" if len(failed) == 1 else "migrations"
    changing, why = (
        ("projects", "its rollback acts with its attempt's credentials")
        if projects
        else ("providers", "its rollback acts on the clouds its attempt used")
    )
    return (
        f"roll back or cancel the failed {noun} ({', '.join(failed[:10])}) before changing the "
        f"plan's {changing}: {why}"
    )


def changed_clouds_refusal(changed: Collection[str], failed: list[str]) -> str | None:
    """The 409 of an edit that changes ``changed`` of a plan's PROVIDER_FIELDS and PROJECT_FIELDS
    while the migrations named ``failed`` are failed - the providers' when one of them changes -,
    None when it changes none or none is failed (SDD §12, §27.4)."""
    if not changed or not failed:
        return None
    return failed_provider_change_refusal(failed, projects=not set(changed) & set(PROVIDER_FIELDS))


def started_refusal(started: Iterable[Migration], limit: int = 10) -> str:
    """The 409 of a plan cancel while migrations of the plan have started (SDD §12): each VM with
    its phase, at most ``limit`` and how many are left out."""
    named = sorted(f"{m.vm.name} ({m.phase})" for m in started)
    rest = len(named) - limit
    listed = ", ".join(named[:limit]) + (f" and {rest} more" if rest > 0 else "")
    return (
        f"only a plan whose migrations have not started can be cancelled: {listed}; cancel a "
        "failed or rolled-back migration on its own first, which cleans up what it copied - on "
        "VMware it names the kit's copies it keeps"
    )


def stopped_refusal(stopped: Iterable[Migration], limit: int = 10) -> str:
    """The 409 of a plan cancel while a migration that has not started has its source stopped
    (SDD §12): an open downtime clock a retry kept (§5.2) - a cancel cannot restart the source
    (§5.1), so it is cut over first. At most ``limit`` VMs and how many are left out."""
    named = sorted(m.vm.name for m in stopped)
    rest = len(named) - limit
    listed = ", ".join(named[:limit]) + (f" and {rest} more" if rest > 0 else "")
    return (
        f"the source VM is stopped after a failed cutover: {listed}; cut it over before "
        "cancelling the plan - a cancel cannot restart it"
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


class NotLeading(asyncio.CancelledError):
    """An action on a cloud this replica may not start: its renew deadline passed, or the lease is
    at another epoch than its store's (SDD §8, §25.4 layer 2). A cancellation, so it ends the
    action as a stop does: nothing is recorded for it, and the next leader runs it from its
    checkpoint."""


class VerificationFailed(PermanentStepError):
    pass


def _add_model_s(m: Migration, details: dict[str, Any]) -> None:
    """Add the model seconds a simulated step kept the source stopped (demo, SDD §5.2)."""
    model_s = details.get("downtime_model_s")
    if isinstance(model_s, int | float):
        m.downtime_model_s = (m.downtime_model_s or 0.0) + float(model_s)


def _passes_since_the_kit_released_its_copies(m: Migration) -> list[SyncPass]:
    """A vmware_warm ``m``'s passes since the kit last released its copies (SDD §9.3, §7.2,
    reviews RV40-05, RV41B-02): those that started once the attempt after its last cutover entered
    pre-copy - a cutover's own final pass not among them -, every pass when no cutover ran, none
    when its last attempt ran one. The kit's rollback deletes its copies after a cutover, or marks
    them cutover; the copies an attempt with no cutover completed stay, marked, and a later run cut
    off - a CBT sync of them - keeps them."""
    entered = None  # when the first attempt after the last cutover entered pre-copy
    for change in reversed(m.phase_history):
        if change.to_phase == Phase.cutover:
            return [] if entered is None else [p for p in m.sync_passes if p.started_at >= entered]
        if change.to_phase == Phase.precopy:
            entered = change.at
    return list(m.sync_passes)


#: the phases in which a migration waits for its next attempt (SDD §9.3)
WAITING_FOR_AN_ATTEMPT = frozenset({Phase.pending, Phase.validating, Phase.blocked, Phase.ready})


def _copies_strategy(m: Migration, phase: Phase | None = None) -> Strategy:
    """The strategy of the attempt whose copies remain: while ``m`` waits for its next attempt -
    in ``phase``, its own by default -, the one a Retry recorded - a validation or a strategy
    change may have chosen another for the next attempt since -, else its own (SDD §9.3, review
    RV40-06)."""
    if (m.phase if phase is None else phase) in WAITING_FOR_AN_ATTEMPT and m.attempt_strategies:
        return m.attempt_strategies[-1]
    return m.strategy


def _cleanup_strategy(m: Migration) -> Strategy:
    """The strategy whose rollback step cleans up after ``m``'s cancel: the attempt's whose data
    the cancel decided to clean, judged in the phase the cancel left - never one a validation or
    ``PUT …/strategy`` chose since: a storage handover's step knows nothing of a warm attempt's
    snapshot (SDD §5.1, review RV43B-04)."""
    left = next(
        (c.from_phase for c in reversed(m.phase_history) if c.to_phase == Phase.cancelled), None
    )
    return _copies_strategy(m, left)


def _kept_after_rollback(m: Migration) -> bool:
    """What a rollback kept of ``m``'s copies where no workload record tells - the demo, the VMware
    kit (SDD §5.1, §9.3, reviews RV39-02, RV40-05, RV40-06, RV41B-02): a warm attempt's passes,
    and a vmware_warm one's passes since the kit last released its copies (§7.2) - the attempt
    whose copies remain -; a cold, vmware_cold or handover rollback keeps no copy."""
    strategy = _copies_strategy(m)
    if strategy == Strategy.warm:
        return bool(m.sync_passes)
    if strategy == Strategy.vmware_warm:
        return bool(_passes_since_the_kit_released_its_copies(m))
    return False


def _held_out(est: Estimate) -> Estimate:
    """``est`` ineligible with §9.2's reason when it is an eligible storage handover (SDD §12)."""
    if est.strategy != Strategy.storage_handover or not est.eligible:
        return est
    reasons = [*est.reasons, HELD_VOLUMES_REASON]
    return est.model_copy(update={"eligible": False, "reasons": reasons})


def _held_in(est: Estimate) -> Estimate:
    """``est`` without §9.2's held reason once nothing is held: eligible again when that reason
    was its only one, its other reasons kept (SDD §12, review RV43B-02)."""
    if est.strategy != Strategy.storage_handover or HELD_VOLUMES_REASON not in est.reasons:
        return est
    reasons = [reason for reason in est.reasons if reason != HELD_VOLUMES_REASON]
    return est.model_copy(update={"eligible": not reasons, "reasons": reasons})


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
#: a finalize refused while the source watch sees the source VM running (SDD §8.1)
SOURCE_RUNS_AGAIN = "the source VM runs again: stop it, or roll it back, before finalizing"
#: a GPU capacity read counts this long from its answer - model time in demo mode (SDD §5.4 rule
#: 5, §22.7); while rules 1-4 hold, an older one is read again
GPU_READ_S = 60.0
#: in demo mode a read counts at least this many ticks, wall time: the model runs faster, the
#: tick's own work and its locks do not (SDD §22.7)
GPU_READ_FLOOR_TICKS = 3
#: per destination, the cutovers begun that a read's re-check looks back on (SDD §22.7); a read
#: older than all of them no longer counts
CUTOVERS_BEGUN_KEPT = 512
#: what rule 5 tells the tick of a GPU VM with a cutover slot left for it (SDD §22.7)
GpuPlace = Literal["go", "hold", "wait"]
#: the gate's reads out at once, every destination's together (SDD §22.7): below asyncio's default
#: thread pool, never under 5 threads (min(32, CPUs + 4)), which the store's calls share - reads
#: that hang never take every thread the store needs. One destination has one fewer at most, so a
#: destination whose reads hang always leaves another one a read.
GPU_READS_MAX = 4


@dataclass(frozen=True)
class _GpuRead:
    """What one GPU capacity read found (SDD §5.4 rule 5, §22.7): whether the cutover may start,
    else what holds it - the pool, the units needed and free, why, and whether the read failed -;
    the units needed and free it found by pool name; and what the check under the locks needs -
    the pools the flavor asks of, the GPU flavors it listed and the cutovers begun before it."""

    room: bool
    pool: str = ""
    needed: int = 0
    free: int | None = None
    reason: str = ""
    failed: bool = False
    found: dict[str, tuple[int, int]] = field(default_factory=dict)
    keys: frozenset[PoolKey] = frozenset()
    flavors: tuple[dict[str, Any], ...] = ()
    #: how many cutovers the process had begun on the destination when the read began
    begun: int = 0


@dataclass
class _CutoversBegun:
    """The cutovers this process began on one destination (SDD §22.7): how many, and the latest of
    them, numbered by that count - the ones a read made earlier may not have counted."""

    count: int = 0
    latest: deque[tuple[int, str]] = field(
        default_factory=lambda: deque(maxlen=CUTOVERS_BEGUN_KEPT)
    )


@dataclass(frozen=True)
class _GpuOutcome:
    """A read's outcome, kept in the process (SDD §22.7): when the destination answered, on the
    orchestrator's clock, and the stay at its gate it was made for (``_wait_mark``)."""

    at: datetime
    mark: datetime | None
    read: _GpuRead


def _wait_mark(m: Migration) -> datetime | None:
    """When ``m``'s stay at its cutover gate began: its last move that was no keep-warm pass's. A
    read's outcome holds for that stay alone - a retry, a validation or a cutover begins another,
    whatever the clock says (SDD §22.7)."""
    for change in reversed(m.phase_history):
        if (change.from_phase, change.to_phase) not in fsm.KEEP_WARM_MOVES:
            return change.at
    return None


def _failure_kind(exc: BaseException) -> str:
    """A failed GPU read by its HTTP status or its kind, never its text, which may name a host or
    a Placement provider (SDD §22.12)."""
    cause = exc.__cause__ or exc.__context__ or exc
    for failure in (exc, cause):
        status = getattr(failure, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return f"HTTP {status}"
    return type(cause).__name__


def _shortfall_reason(short: Shortfall, where: str, flavor: str, own: int) -> str:
    """Why a GPU capacity read found no room, in words (SDD §22.7): the pool, the units it has
    free and the units the cutover needs there - ``own`` of them its flavor's -, the destination by
    its label (§1 naming)."""
    if short.kind == "refused":
        return (
            f"Placement of {where} refuses the request for {short.pool} ({short.detail}): no "
            f"server is created with flavor {flavor!r}"
        )
    if short.kind == "group":
        return (
            f"no provider of {short.pool} in {where} has the {short.needed} units free that a "
            f"request group of flavor {flavor!r} takes from one; the most on one is {short.free}"
        )
    across = " across its pools" if short.kind == "class" else ""
    who = (
        f"flavor {flavor!r} needs"
        if short.needed <= own
        else f"flavor {flavor!r} and the cutovers to {where} without a server yet need"
    )
    return f"{short.pool} of {where} has {short.free} units free{across}, and {who} {short.needed}"


def reassessed(m: Migration) -> bool:
    """Whether a validation re-assesses ``m`` (SDD §5.4): pending, blocked or ready, or left in
    validating - unless it is ready with its source stopped, its downtime clock open (§5.2): a
    re-assessment could block it, where no action restarts the source; it is left as it is, like
    one in flight (audit OC-02). One already blocked or left in validating is re-assessed whatever
    its source: cancel, rollback and retry are refused there, a validation is its way back to ready
    (review RV51-01)."""
    return m.phase in REASSESSED and not (m.phase == P.ready and fsm.source_stopped(m))


def _named_twice(migrations: Iterable[Migration]) -> str | None:
    """Why a plan cannot go on while two of its migrations - cancelled ones aside - name one VM,
    else None: both would cut it over (SDD §5.4, audit OC-06)."""
    by_vm: dict[str, list[Migration]] = {}
    for m in migrations:
        if m.phase != P.cancelled:
            by_vm.setdefault(m.vm.source_id, []).append(m)
    twice = sorted((ms for ms in by_vm.values() if len(ms) > 1), key=lambda ms: ms[0].vm.name)
    if not twice:
        return None
    named = "; ".join(f"{ms[0].vm.name} ({', '.join(sorted(m.id for m in ms))})" for ms in twice)
    return f"two migrations of one VM would both cut it over: {named}; cancel all but one first"


def _lock_busy(lock: asyncio.Lock) -> bool:
    """Whether taking ``lock`` would wait: it is held, or a task waits for it - a released lock
    goes to its first waiter, and acquire() queues behind that one. A free lock no task waits for
    is taken without yielding, so a check then an acquire is one step of the event loop (SDD §8,
    review RV52-O1, PR #58 review)."""
    waiters = getattr(lock, "_waiters", None)  # asyncio.Lock's queue: 3.11 to 3.13 keep it here
    return lock.locked() or any(not w.cancelled() for w in waiters or ())


def _assessment(m: Migration) -> tuple[str, datetime | None, bool]:
    """What a pre-staging covers of ``m`` (SDD §8, reviews RV51-17, RV51-19): its last move to
    validating - the VM that validation read, the flavor and keypair it promised pre-staged - and
    whether its strategy needs a conversion host."""
    validated = next((c.at for c in reversed(m.phase_history) if c.to_phase == P.validating), None)
    return m.id, validated, m.strategy in (Strategy.cold, Strategy.warm)


def _vms_named_twice(migrations: Iterable[Migration]) -> set[str]:
    """The VMs two of these migrations, cancelled ones aside, name (SDD §5.4, review RV51-18)."""
    named = Counter(m.vm.source_id for m in migrations if m.phase != P.cancelled)
    return {vm_id for vm_id, count in named.items() if count > 1}


def _text(given: str | None) -> str | None:
    """The text a person gives with an action - a comment, a cancel's reason - as Python's strip
    leaves it, none when nothing is left (SDD §12, review RV51-26)."""
    return (given or "").strip() or None


def _action(message: str, actor: str, data: dict[str, Any]) -> Joined:
    """A person's action's ``migration.action``, joined to its transition's write: a store error
    leaves neither, the migration as it was for the person's retry (SDD §5.4, reviews RV51-12,
    RV51-13)."""

    def joined(new: Migration) -> tuple[list[tuple[str, Any, int]], list[Event]]:
        event = new_event(
            "migration.action",
            message,
            plan_id=new.plan_id,
            migration_id=new.id,
            actor=actor,
            data=data,
        )
        return [], [event]

    return joined


def _by_vm(migrations: Iterable[Migration]) -> dict[str, Migration]:
    """Each VM's migration that is not cancelled, else its cancelled one (SDD §5.4)."""
    existing: dict[str, Migration] = {}
    for m in migrations:
        known = existing.get(m.vm.source_id)
        if known is None or known.phase == P.cancelled:
            existing[m.vm.source_id] = m
    return existing


def _rewrite_vm_id(plan: Plan, old: str, new: str) -> bool:
    """The plan follows a recreated source VM (SDD §7.3): every field keyed by its id; whether
    anything changed - once done nothing names ``old``."""
    before = plan.model_dump()
    plan.vm_ids = [new if v == old else v for v in plan.vm_ids]
    for wave in plan.waves:
        wave.vm_ids = [new if v == old else v for v in wave.vm_ids]
    if old in plan.strategy_overrides:
        plan.strategy_overrides[new] = plan.strategy_overrides.pop(old)
    if old in plan.probe_overrides:
        plan.probe_overrides[new] = plan.probe_overrides.pop(old)
    # its ports keep their MACs (the rollback recreates them with theirs, §7.3)
    if old in plan.nic_overrides:
        plan.nic_overrides[new] = plan.nic_overrides.pop(old)
    # what the plan acknowledges for the VM: a recreated server stays its group's member (§9.3)
    if old in plan.acknowledged_findings:
        plan.acknowledged_findings[new] = plan.acknowledged_findings.pop(old)
    # and the record its new address goes to (§7.3, §23.5)
    targets = plan.network_cutover.dns_targets
    if old in targets:
        targets[new] = targets.pop(old)
    return plan.model_dump() != before


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


#: a plan's read-modify-write (``_update_plan``) is tried this often, backing off between the
#: attempts, before it answers 409: several drivers and the tick loop rewrite the same plan
PLAN_UPDATE_ATTEMPTS = 20
#: the longest backoff between two attempts, in seconds (5 ms doubling up to it, jittered)
PLAN_UPDATE_BACKOFF_S = 0.25


#: what joins a migration's transitions in their transaction: given the migration as written, its
#: own documents and events - a validation's recommendation (SDD §5.4, §14.4)
Joined = Callable[[Migration], tuple[list[tuple[str, Any, int]], list[Event]]]


def _update_backoff(attempt: int) -> float:
    """Seconds to wait before retry ``attempt`` (from 1): 5 ms doubling to the cap, jittered so
    that writers which collided do not collide again in step."""
    return min(PLAN_UPDATE_BACKOFF_S, 0.005 * 2**attempt) * random.uniform(0.5, 1.0)


#: what a project's entry of the check report holds beside its status, message and time (SDD §10
#: "The check of project credentials")
PROJECT_CHECK_FACTS = ("project_name", "domain", "user_id", "roles", "restricted", "expires_at")


def _project_check(
    status: str, message: str | None, read: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """A project's entry of the check report, without its time (SDD §10): an error's facts are
    null, whatever its sign-in read."""
    facts = read if read is not None else {}
    return {"status": status, "message": message, **{k: facts.get(k) for k in PROJECT_CHECK_FACTS}}


def _projects_report(
    provider: Provider, signed_in: Mapping[str, dict[str, Any]], now: datetime
) -> dict[str, Any]:
    """The check report's ``projects`` (SDD §10, §27.7): the entries of the projects the check
    signed in with, at its time, and the other entries of the credentials the provider holds as
    the last check read them, by project id."""
    earlier = provider.capabilities.get("projects")
    kept = earlier if isinstance(earlier, dict) else {}
    checked_at = utc_text(now)
    report: dict[str, Any] = {}
    for project_id in sorted(provider.project_credentials):
        if project_id in signed_in:
            report[project_id] = {**signed_in[project_id], "checked_at": checked_at}
        elif project_id in kept:
            report[project_id] = kept[project_id]
    return report


async def _conversion_host_server(
    provider: Provider, impl: Any, project: str | None = None
) -> tuple[str, str | None] | None:
    """The server the conversion host the credential of ``project`` reaches - the provider's
    own for None - resolves to with ``impl``, and what found it (SDD §10 "Host key pins"): by
    its name - the side's as the executor names the host when it has none -, else by its
    address, the provider's own host's alone: a project's host is looked up by its name in its
    project alone (``host_address``, §26.10 decision 13). The server None, with what it was
    looked up by, when nothing answers; None when there is no host or nothing to look it up
    with. Raises ProviderError."""
    host = provider.conversion_host
    find = getattr(impl, "find_server", None)
    if host is None or find is None:
        return None
    found_by = host.name or CONVERSION_HOST_NAMES[str(provider.role)]
    server = await find(found_by)
    address = host_address(provider, project)
    by_address = getattr(impl, "server_of_address", None)
    if server is None and address and by_address is not None:
        found_by, server = address, await by_address(address)
    return found_by, None if server is None else str(server)


def _replaced(found_by: str, server: str, pin: HostKeyPin) -> str | None:
    """The report of a conversion host that resolves to another server than its pin's (SDD §10,
    §26.5), ``found_by`` being what found it; None for the pin's own server."""
    if pin.server_id == server:
        return None
    return (
        f"conversion host replaced: {found_by} is now server {server}; its pin belongs to server "
        f"{pin.server_id}"
    )


def _not_found(name: str, project: str, pin: HostKeyPin) -> str:
    """The report of a project's conversion host whose name no server of the project bears (SDD
    §10 "Host key pins", §26.10 decision 13), ``name`` being what it was looked up by."""
    return (
        f"conversion host not found: no server is named {name} in project {project}; its pin "
        f"belongs to server {pin.server_id}"
    )


def _foreign_vms(plan: Plan, source: Provider, vms: Iterable[VMRef]) -> str | None:
    """Why validation refuses a plan whose VMs are not all of its source project (SDD §27.5 "One
    project pair per plan", owner decision 7, plan task TC5): its source_project, else the source
    provider's own project as its check names it; each project named with its VM count. A VM whose
    project is not known (project_id null: VMware, or recorded before) and a source whose check
    names no project of its own are not judged."""
    expected = plan.source_project or source.own_project_id
    if expected is None or source.kind == ProviderKind.vmware:
        return None
    counts: Counter[str] = Counter()
    names: dict[str, str | None] = {}
    for vm in vms:
        if vm.project_id is not None:
            counts[vm.project_id] += 1
            names.setdefault(vm.project_id, vm.project)
    if set(counts) <= {expected}:
        return None
    listed = ", ".join(
        f"{pid}{f' ({names[pid]})' if names[pid] and names[pid] != pid else ''}: {n} VM(s)"
        for pid, n in sorted(counts.items())
    )
    whose = "its source project" if plan.source_project else "its source provider's own project"
    return (
        f"the plan's VMs are not all of {whose} {expected}: {listed}; a plan moves the VMs of one "
        "project - plan the others with their own (SDD §27.5)"
    )


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
        hook_runner: Any = None,
        process_groups: ProcessGroups | None = None,
        monotonic: Callable[[], float] = time.monotonic,
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
        #: the book of the process groups of this replica's playbooks, a hook's included: a
        #: deposition kills them (SDD §7.1, §25.4 layer 3)
        self.process_groups = process_groups if process_groups is not None else ProcessGroups()
        #: runs a plan's hooks at their points (SDD §7.2, §8.2)
        self.hook_runner = hook_runner or HookRunner(settings, groups=self.process_groups)
        #: a server's leadership (SDD §8): whether its replica leads, and its deposition; None for
        #: the CLI's orchestrator and a test's, which act without a lease
        self.leadership: Any = None
        #: the clock of the tick's heartbeat and health (SDD §12)
        self._monotonic = monotonic
        #: Octavia's pace for the member helper (SDD §8.5)
        self.lb_pace = Pace()
        # SDD §8: a lock lives while a coroutine holds it or waits for it
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        #: when the next source watch is due (SDD §8.1); set by the first tick
        self._source_watch_due: datetime | None = None
        #: the source watch in progress: the tick starts it and never waits for it (SDD §8.1)
        self._watch_task: asyncio.Task[None] | None = None
        #: per migration, its GPU capacity read in progress, one at a time: the tick starts it and
        #: never waits for it (SDD §5.4 rule 5, §22.7); and the destination each one reads
        self._gpu_reads: dict[str, asyncio.Task[None]] = {}
        self._gpu_read_destinations: dict[str, str] = {}
        #: per destination, the cutover slots its GPU VMs ahead hold in the running tick (§22.7)
        self._tick_holds: Counter[str] = Counter()
        #: per migration at its cutover gate, its last GPU capacity read's outcome (SDD §22.7)
        self._gpu_outcomes: dict[str, _GpuOutcome] = {}
        #: per destination provider, the cutovers this process began: a read notes their count
        #: when it begins, and one begun since makes it stale whatever phase it is in now (§22.7)
        self._cutovers_begun: defaultdict[str, _CutoversBegun] = defaultdict(_CutoversBegun)
        self._drivers: dict[str, asyncio.Task[None]] = {}
        #: migration id -> (crashes in a row, monotonic time of the last one): relaunch backoff
        self._driver_crashes: dict[str, tuple[int, float]] = {}
        self._steps: dict[str, asyncio.Task[Any]] = {}
        #: per plan, the assessments its pre-stagings in this process covered (SDD §8)
        self._prestage_done: dict[str, set[tuple[str, datetime | None, bool]]] = {}
        #: when a plan whose pre-staging failed with work in flight is pre-staged again (monotonic)
        self._prestage_retry_at: dict[str, float] = {}
        #: the migrations a retry is writing, from its first lock to its own reopen of the plan:
        #: the tick's reopen leaves their plans to it (SDD §5.1, §8, PR #58 CI)
        self._retrying: Counter[str] = Counter()
        self._prestage_tasks: dict[str, asyncio.Task[None]] = {}
        #: best-effort rollback of the data path after a cancel in precopy/syncing (SDD §7.2)
        self._cleanups: dict[str, asyncio.Task[None]] = {}
        #: a failure's lookup of similar incidents, never awaited by the driver (SDD §8)
        self._lookups: set[asyncio.Task[None]] = set()
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

    def _leads(self) -> bool:
        """Whether this orchestrator may act (SDD §8): always without a leadership - the CLI's, a
        test's -, else while its replica leads and its renew deadline has not passed."""
        return self.leadership is None or bool(self.leadership.leads())

    async def _hold_the_lease(self, what: str) -> None:
        """Before an action on a cloud - a step's attempt, a hook, a floating IP's move or return,
        a DNS swap or revert, an Octavia member's change - a server's orchestrator checks its renew
        deadline and reads the epoch in the lease row (SDD §8, §25.4 layer 2): past its deadline,
        or with a store whose epoch is not the row's - which deposes it -, it starts nothing
        (:class:`NotLeading`). The CLI's, whose store holds no lease, checks nothing."""
        if not self.store.fenced:
            return
        if not self._leads():
            raise NotLeading(f"{what}: this replica does not lead")
        own = self.store.epoch
        held = await self.db.lease_epoch()
        if own is not None and held == own:
            return
        reason = f"the lease is at epoch {held}, this replica's is {own}"
        if self.leadership is not None:
            self.leadership.depose(reason)
        raise NotLeading(f"{what}: {reason}")

    async def _load(self, mid: str) -> tuple[Migration, int]:
        return await self.db.get_versioned("migration", mid, Migration)

    async def _save(self, m: Migration, version: int) -> int:
        m.updated_at = self._now()
        return await self.db.put("migration", m, expected_version=version)

    async def _save_with_event(
        self, m: Migration, version: int, kind: str, message: str, data: dict[str, Any]
    ) -> int:
        """``_save`` and the event that announces it in one store transaction, the event published
        after it commits (SDD §8.1, §11, review RV21-04): a store error leaves neither."""
        m.updated_at = self._now()
        event = new_event(kind, message, plan_id=m.plan_id, migration_id=m.id, data=data)
        return await put_and_emit(self.store, self.bus, "migration", m, version, event)

    async def _save_with_events(self, m: Migration, version: int, events: list[Event]) -> int:
        """``_save`` and the events of a person's action in one store transaction, published once
        it commits (SDD §5.4, §11, audit OC-03): a store error leaves none of them, so the
        person's retry is taken anew."""
        m.updated_at = self._now()
        versions = await put_all_and_emit(self.store, self.bus, [("migration", m, version)], events)
        return versions[0]

    async def _plan(self, plan_id: str) -> Plan:
        return await self.db.get("plan", plan_id, Plan)

    async def _save_plan(self, plan: Plan) -> None:
        plan.updated_at = self._now()
        await self.db.put("plan", plan)

    async def _update_plan(self, plan_id: str, mutate: Callable[[Plan], bool | None]) -> Plan:
        """Read-modify-write ``plan`` under optimistic concurrency.

        Several drivers and the tick loop touch the same plan concurrently (ids rewritten by a
        rollback, status changes, strategy overrides): ``mutate`` runs on the freshest copy and
        the write is retried, after a jittered backoff, when another writer got in between.
        ``mutate`` returns ``False`` to leave the plan untouched. A plan rewritten under every
        one of PLAN_UPDATE_ATTEMPTS raises ``ConflictError`` (the API's 409, SDD §12).
        """
        for attempt in range(PLAN_UPDATE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_update_backoff(attempt))
            plan, version = await self.db.get_versioned("plan", plan_id, Plan)
            if mutate(plan) is False:
                return plan
            plan.updated_at = self._now()
            try:
                await self.db.put("plan", plan, expected_version=version)
                return plan
            except ConflictError:
                continue
        raise ConflictError(f"plan {plan_id} changed too often to update it; run the action again")

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
        *,
        created: dict[str, Any] | None = None,
        also: Joined | None = None,
    ) -> tuple[Migration, int]:
        """Several transitions in one write, with the events that announce them - for version 0,
        a new migration, ``migration.created`` with ``created`` as its data first, then one
        ``migration.phase`` each - and what ``also`` joins to them, all in one transaction: a
        crash or a failed event write never splits them (SDD §5.1, §5.4, §8, audit AD-05)."""
        new = m
        for to, reason, _ in steps:
            new = fsm.transition(new, to, reason, actor, now=self._now())
        new.updated_at = self._now()
        events: list[Event] = []
        if created is not None:
            events.append(
                new_event(
                    "migration.created",
                    f"migration of {new.vm.name} created",
                    plan_id=new.plan_id,
                    migration_id=new.id,
                    actor=actor,
                    data=created,
                )
            )
        before = m.phase
        for to, reason, data in steps:
            events.append(
                new_event(
                    "migration.phase",
                    f"{new.vm.name}: {before} -> {to} ({reason})",
                    plan_id=new.plan_id,
                    migration_id=new.id,
                    actor=actor,
                    data={"from": str(before), "to": str(to), "reason": reason, **(data or {})},
                )
            )
            before = to
        writes: list[tuple[str, Any, int]] = [("migration", new, version)]
        if also is not None:
            joined_writes, joined_events = also(new)
            writes += joined_writes
            events += joined_events
        versions = await put_all_and_emit(self.store, self.bus, writes, events)
        return new, versions[0]

    # ------------------------------------------------------------------------------------------
    # providers
    async def check_provider(
        self, provider_id: str, actor: str = "system", *, projects: Collection[str] | None = None
    ) -> Provider:
        """The provider check (SDD §8, §10): its own check() and its conversion host's pin, then a
        sign-in with each of its project credentials - with those of ``projects`` alone for a
        validation's check, the plan's (§10 "The check of project credentials", §27.7) -, one
        provider call each, whatever its own check answered."""
        # one check per provider at a time (SDD §8): a validation's check and a manual Check would
        # otherwise collide on the versioned write below; the innermost lock, it takes no other
        async with self._lock(f"provider:{provider_id}"):
            # versioned write: a provider deleted or edited while the check ran is not re-inserted
            provider, version = await self.db.get_versioned("provider", provider_id, Provider)
            # the check acts with the provider's own credential (SDD §27.5's first row)
            impl = for_step(self.providers, Step.CHECK, provider, None)
            pinned: Event | None = None
            update: dict[str, Any]
            try:
                caps = await impl.check()
                update = {"capabilities": dict(caps), "status": "ok", "status_message": None}
            except ProviderError as exc:
                update = {"status": "error", "status_message": redact(str(exc))[:500]}
            if update["status"] == "ok":
                # its own credential's host and pin; a project's are the project's (SDD §26.4)
                pin, replaced, pinned = await self._pin_conversion_host(provider, impl, actor)
                if pin is not None:
                    update["conversion_host_pin"] = pin
                if replaced is not None:
                    update["status_message"] = replaced
            signed_in = await self._check_projects(provider, impl, projects)
            update["last_checked_at"] = now = self._now()
            if provider.project_credentials:
                # a check that failed keeps its earlier report, its projects brought up to date
                report = update.get("capabilities", provider.capabilities)
                update["capabilities"] = {
                    **report,
                    "projects": _projects_report(provider, signed_in, now),
                }
            updated = provider.model_copy(update=update)
            if pinned is None:
                await self.db.put("provider", updated, expected_version=version)
            else:  # the pin and its event in one transaction (SDD §4.3, §10)
                await put_and_emit(self.store, self.bus, "provider", updated, version, pinned)
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

    async def _check_projects(
        self, provider: Provider, impl: Any, projects: Collection[str] | None
    ) -> dict[str, dict[str, Any]]:
        """Each project credential's entry of the check report, without its time (SDD §10 "The
        check of project credentials", §27.7): the provider's every credential, or those of
        ``projects`` it holds, signed in with one provider call each, one after the other in the
        order of their project ids - the bound ends a call's wait, not its thread (§10), so a
        Keystone that never answers pins one worker thread at a time."""
        held = provider.project_credentials
        chosen = held.keys() if projects is None else held.keys() & set(projects)
        return {pid: await self._check_project(provider, impl, pid) for pid in sorted(chosen)}

    async def _check_project(
        self, provider: Provider, impl: Any, project_id: str
    ) -> dict[str, Any]:
        """One project credential's entry (SDD §10, §27.7): ok with what its token and its record
        say - and the report of its project's conversion host found replaced -, else error with
        why - a failure, an implementation that cannot sign in with a project credential, or a
        token scoped to another project, which is never used."""
        sign_in = getattr(impl, "check_project", None)
        try:
            if sign_in is None:
                raise ProviderError(f"{provider.id}: it cannot sign in with a project credential")
            read = await sign_in(project_id)
        except ProviderError as exc:
            return _project_check("error", redact(str(exc))[:500])
        scoped = read.get("project_id")
        if scoped != project_id:
            return _project_check(
                "error", f"{provider.id}: its token is scoped to project {scoped}, not {project_id}"
            )
        return _project_check("ok", await self._replaced_project_host(provider, project_id), read)

    async def _replaced_project_host(self, provider: Provider, project_id: str) -> str | None:
        """The report of the project's conversion host found replaced, or not found (SDD §10
        "Host key pins", §26.4, §26.5): looked up with the project's credential, as a run of a
        plan with the project finds it - by its name alone, in the project, never by the
        provider's address (§26.10 decision 13) -, when that credential holds a pin, whatever
        keys the provider declares (decision 12). None when it holds none, when the host resolves
        to the pin's server, and when the lookup fails - logged by its provider error. A project
        host's console is never read, and nothing is pinned."""
        pin = host_pin(provider, project_id)
        if pin is None:
            return None
        try:
            impl = implementation(self.providers, provider, project_id)
            found = await _conversion_host_server(provider, impl, project_id)
        except ProviderError as exc:
            log.warning(
                "the conversion host of project %s of provider %s was not looked up: %s",
                project_id,
                provider.id,
                redact(str(exc)),
            )
            return None
        if found is None:
            return None
        found_by, server = found
        if server is None:
            return _not_found(found_by, project_id, pin)
        return _replaced(found_by, server, pin)

    async def _pin_conversion_host(
        self, provider: Provider, impl: Any, actor: str, project: str | None = None
    ) -> tuple[HostKeyPin | None, str | None, Event | None]:
        """What a check that answered, or a run, learns of the conversion host that ``impl`` -
        the credential of ``project``, the provider's own for None - reaches (SDD §10 "Host key
        pins", §26.3, §26.4): the pin its console gives and the event that announces it, or the
        report of a pin of another server. The host's server is found by its name - the side's as
        the executor names the host when it has none -, else, the provider's own host alone, by
        its address (§26.10 decision 13). Without a pin of that credential (``host_pin``), its
        whole console pins what the parser keeps; a pin of that server reads nothing more; a pin
        of another server is kept, neither used nor replaced, and reported. Nothing of the
        console is logged."""
        read = getattr(impl, "console_log", None)
        if provider.kind == ProviderKind.vmware or read is None:
            return None, None, None
        side = str(provider.role)
        try:
            found = await _conversion_host_server(provider, impl, project)
            found_by, server = found if found is not None else ("", None)
            if server is None:
                return None, None, None
            pin = host_pin(provider, project)
            if pin is not None:
                return None, _replaced(found_by, server, pin), None
            keys = console_keys(await read(server, None))
        except ProviderError as exc:
            log.warning(
                "the conversion host of provider %s was not pinned: %s",
                provider.id,
                redact(str(exc)),
            )
            return None, None, None
        if not keys:
            log.debug(
                "the conversion host %s of provider %s: its console shows no host key to pin",
                server,
                provider.id,
            )
            return None, None, None
        where = f" in project {project}" if project is not None else ""
        event = new_event(
            "conversion_host.key_pinned",
            f"the {side} conversion host {found_by} of provider {provider.id}{where} (server "
            f"{server}) pinned from its console",
            actor=actor,
            data={
                "provider_id": provider.id,
                "side": side,
                "server_id": server,
                "fingerprints": [fingerprint(key) for key in keys],
                "source": "console",
            },
        )
        pin = HostKeyPin(server_id=server, keys=keys, source="console", pinned_at=self._now())
        return pin, None, event

    def host_keys(self, plan_id: str, migration_id: str | None = None) -> _HostKeyBook:
        """The book of the conversion hosts' keys of a run of the plan - a migration's step, or
        the plan's pre-staging (SDD §7.2 "Conversion host keys")."""
        return _HostKeyBook(self, plan_id, migration_id)

    async def _pin_from_console(self, provider_id: str, plan_id: str | None = None) -> Provider:
        """A run's pin of a conversion host without one (SDD §7.2, §26.8): the provider check's
        path (§10) - the host's whole console read once, what the parser keeps pinned with
        conversion_host.key_pinned in one transaction -, under the provider's lock as a check
        holds it; the provider as stored when it was pinned meanwhile or nothing was pinned. The
        host of the run of ``plan_id`` is looked up with the plan's credential, in its project
        when it names one there (§7.2, §27.5), and pinned as that credential's pin (§26.4)."""
        plan = await self._plan_of_run(plan_id)
        async with self._lock(f"provider:{provider_id}"):
            provider, version = await self.db.get_versioned("provider", provider_id, Provider)
            project = credential_project(Step.PRESTAGE, provider.role, plan)
            if host_pin(provider, project) is not None:
                return provider
            pin, _, pinned = await self._pin_conversion_host(
                provider, implementation(self.providers, provider, project), "system", project
            )
            updated = with_host_pin(provider, project, pin) if pin is not None else None
            if updated is None or pinned is None:
                return provider
            await put_and_emit(self.store, self.bus, "provider", updated, version, pinned)
            return updated

    async def _pin_injected(
        self,
        provider_id: str,
        side: str,
        server_id: str,
        keys: Sequence[str],
        plan_id: str | None,
    ) -> None:
        """The pin of a server a pre-staging's role created with the key the run injected (SDD
        §7.2, §26.3, §26.4): its keys, source injected, as the pin of the credential the plan's
        pre-staging reaches the side's host with (§27.5), with conversion_host.key_pinned in one
        transaction - over any earlier pin of that credential, another server's -, under the
        provider's lock as a check holds it. A project whose credential went meanwhile takes no
        pin: its pin went with it (§12)."""
        plan = await self._plan_of_run(plan_id)
        project = credential_project(Step.PRESTAGE, side, plan)
        async with self._lock(f"provider:{provider_id}"):
            provider, version = await self.db.get_versioned("provider", provider_id, Provider)
            pin = HostKeyPin(
                server_id=server_id, keys=list(keys), source="injected", pinned_at=self._now()
            )
            updated = with_host_pin(provider, project, pin)
            if updated is None:
                log.warning(
                    "the %s conversion host of provider %s (server %s) was not pinned: the "
                    "credential of project %s went meanwhile",
                    side,
                    provider_id,
                    server_id,
                    project,
                )
                return
            where = f" in project {project}" if project is not None else ""
            event = new_event(
                "conversion_host.key_pinned",
                f"the {side} conversion host of provider {provider_id}{where} (server "
                f"{server_id}) pinned with the host key its pre-staging injected",
                plan_id=plan_id,
                data={
                    "provider_id": provider_id,
                    "side": side,
                    "server_id": server_id,
                    "fingerprints": [fingerprint(key) for key in keys],
                    "source": "injected",
                },
            )
            await put_and_emit(self.store, self.bus, "provider", updated, version, event)

    async def _plan_of_run(self, plan_id: str | None) -> Plan | None:
        """The plan of a run's book (SDD §7.2): the projects it names choose the credentials, and
        so the pins, of its conversion hosts (§26.4, §27.5); None without one, or for a plan
        that is gone."""
        if plan_id is None:
            return None
        with contextlib.suppress(NotFound):
            return await self._plan(plan_id)
        return None

    async def _caps(
        self, provider: Provider, actor: str, project: str | None = None
    ) -> dict[str, Any]:
        # a validation's check signs in with the plan's project credential on the provider's side
        # alone, none for a plan that names none (SDD §8, §10, §27.7)
        checked = await self.check_provider(
            provider.id, actor, projects=[project] if project else []
        )
        if checked.status != "ok":
            raise BadRequest(f"provider {provider.id} check failed: {checked.status_message}")
        return {**checked.capabilities, "conversion_host": checked.conversion_host is not None}

    def _project_refusal(self, provider: Provider, project: str | None) -> str | None:
        """Why validation refuses the plan's ``project`` on ``provider``'s side (SDD §8, §27.5,
        §27.7, plan task TC5), as the check just left the provider: no credential for it, its
        check's entry not ok, its expiry past, or an existing conversion host on that side, which
        serves the provider's own project alone. None for a side that names no project. It names
        the provider and the project, never the credential."""
        if project is None:
            return None
        where = f"provider {provider.label}"
        if project not in provider.project_credentials:
            return (
                f"{where} holds no credential for project {project}: an admin writes its "
                "application credential, or the plan names another project (SDD §27.5)"
            )
        report = provider.capabilities.get("projects")
        entry = report.get(project) if isinstance(report, dict) else None
        if not isinstance(entry, dict) or entry.get("status") != "ok":
            why = entry.get("message") if isinstance(entry, dict) else None
            return f"{where}: the credential of project {project} failed its check" + (
                f": {why}" if why else ""
            )
        expires = entry.get("expires_at")
        if expires is not None:
            try:
                expiry = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            except ValueError:
                return (
                    f"{where}: the credential of project {project} holds an expiry that is no time"
                )
            if to_utc(expiry) <= self._now():
                return (
                    f"{where}: the credential of project {project} expired at {expires}; an admin "
                    "writes a new one"
                )
        host = provider.conversion_host
        if host is not None and not host.manage:
            return (
                f"{where} has an existing conversion host, which serves its own project alone: "
                f"a plan cannot name project {project} on that side (SDD §27.5)"
            )
        return None

    async def _destination_view(self, destination: Provider, impl: Any, plan: Plan) -> Any:
        """The destination's inventory as validation judges it (SDD §8, §22.3, §27.5): read by
        ``impl``, the plan's destination credential - and, for a plan with a destination project,
        its GPU capacity read again with the destination provider's own credential over the GPU
        flavors that credential lists: Placement refuses a member's token."""
        inventory = await impl.inventory()
        if credential_project(Step.INVENTORY, ProviderRole.destination, plan) is None:
            return inventory  # the provider's own inventory read Placement itself
        requests = [g for f in getattr(inventory, "gpu_flavors", None) or [] for g in f["gpus"]]
        if not requests:
            return inventory
        placement = for_step(self.providers, Step.GPU, destination, plan)
        return replace(inventory, gpu_capacity=await placement.gpu_capacity(requests))

    async def _vms(self, plan: Plan, impl: Any, existing: Mapping[str, Migration]) -> list[VMRef]:
        """The plan's VMs as a validation and the wave planner read them (SDD §5.4): from the source
        each VM with no migration yet or one a validation re-assesses; any other migration's
        recorded vm - a VM that left the source, deleted by a storage handover or a finalize with
        delete_source, keeps its plan valid (review PL-09)."""
        vms = []
        for vm_id in plan.vm_ids:
            m = existing.get(vm_id)
            if m is not None and not reassessed(m):
                vms.append(m.vm)
                continue
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
            status = (await self._plan(plan_id)).status
            if status == PlanStatus.running:
                raise NotAllowed("pause the plan before validating it again")
            if status == PlanStatus.cancelled:
                raise NotAllowed("a cancelled plan cannot be validated")  # terminal (SDD §12)
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
        if plan.status == PlanStatus.cancelled:
            raise NotAllowed("a cancelled plan cannot be validated")
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
        # the plan's projects, by id (SDD §4.2, §10, plan task TC4): each side's check signs in
        # with the plan's project credential there alone
        src_caps = await self._caps(source, actor, plan.source_project)
        dst_caps = await self._caps(destination, actor, plan.destination_project)
        # as the checks left them: their pins, and a host they found replaced (SDD §9.3, §10)
        as_checked = {
            p.id: p
            for p in (
                await self._provider(plan.source_provider_id, ProviderRole.source),
                await self._provider(plan.destination_provider_id, ProviderRole.destination),
            )
        }
        # SDD §8, §27.5 (plan task TC5): a project credential missing, failed or expired, and a
        # project on an existing conversion host's side, refused before anything is read
        for provider, project in (
            (as_checked[source.id], plan.source_project),
            (as_checked[destination.id], plan.destination_project),
        ):
            refusal = self._project_refusal(provider, project)
            if refusal is not None:
                raise BadRequest(refusal)
        # the plan's credentials read its VMs and inventories; Placement, the provider's own (§27.5)
        src_impl = for_step(self.providers, Step.INVENTORY, source, plan)
        dst_impl = for_step(self.providers, Step.INVENTORY, destination, plan)
        try:
            src_inv = await src_impl.inventory()
            dst_inv = await self._destination_view(destination, dst_impl, plan)
        except ProviderError as exc:
            raise ProviderError(f"inventory failed: {exc}") from None  # 502, redacted (SDD §12)
        listed = await self.db.list("migration", Migration, plan_id=plan.id)
        twice = _named_twice(listed)
        if twice is not None:
            raise NotAllowed(twice)
        existing = _by_vm(listed)
        vms = await self._vms(plan, src_impl, existing)
        # one project pair per plan (SDD §27.5, owner decision 7), before any finding
        foreign = _foreign_vms(plan, as_checked[source.id], vms)
        if foreign is not None:
            raise BadRequest(foreign)
        # the keys of the keypairs its VMs boot with, with the plan's credentials, and the clock its
        # project credentials' expiry is judged by (SDD §9.3, §27.6, §27.7, plan task TC6)
        prints = await _keypair_fingerprints(vms, src_inv, dst_inv, src_impl, dst_impl)
        judged_at = self._now()
        # what a cutover of each VM will still create - its server alone when an earlier attempt
        # holds its destination volumes (SDD §9.3, reviews PL-02, RV38A-01)
        shares: dict[str, QuotaShare] = {}
        holding_volumes: set[str] = set()
        for vm_id, m in existing.items():
            share = quota_share(m.phase)
            if share == "all" and await self.holds_destination_volumes(m):
                share = quota_share(m.phase, holds_volumes=True)
                holding_volumes.add(vm_id)
            shares[vm_id] = share
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
        # the TTL of each DNS target's record, for NET_DNS_TTL_LONG (SDD §8.4, §9.3), with the
        # plan's destination credential (§27.5)
        dns = for_step(self.providers, Step.DNS, destination, plan)
        ttls = await record_ttls(plan, vms, dns, self.settings)
        # the plan's post_create hooks run inside the downtime: their timeouts (SDD §9.1)
        hooks_s = await self._post_create_hooks_s(plan)
        # each mapped pair of Octavia pools, for MAP_LB_POOL_INCOMPATIBLE (SDD §8.5, §9.3), each
        # read with the plan's credential on its side (§27.5)
        src_lb = for_step(self.providers, Step.NETWORK, source, plan)
        dst_lb = for_step(self.providers, Step.NETWORK, destination, plan)
        lb_pairs = {
            source_pool: PoolPair(
                source=await _read_pool(src_lb, source_pool),
                destination=await _read_pool(dst_lb, pool),
            )
            for source_pool, pool in plan.network_cutover.lb_pools.items()
        }
        for vm in vms:
            current = existing.get(vm.source_id)
            detached: list[Finding] = []
            if (
                current is not None
                and reassessed(current)
                and any(change.to_phase == P.cutover for change in current.phase_history)
            ):
                # a disk the VM had at a cutover attempt never leaves the migration by a
                # validation - an attempt's cold export may have left it detached (SDD §5.4) -,
                # and its blocker and the kept disk reach the eligibility and the estimates
                # (SDD §9.2, review PL-10)
                vm, detached = keep_detached_volumes(current.vm, vm)
            # dst_caps: what the check above just read (its token's roles, SDD §9.3), not the
            # capabilities `destination` held before it
            findings = run_preflight(
                vm,
                plan,
                src_inv,
                dst_inv,
                vms,
                source=source,
                destination=destination,
                dst_caps=dst_caps,
                # the quota counts what a cutover will still create (SDD §9.3, review PL-02)
                shares=shares,
                dns_ttl=ttls.get(vm.source_id),
                lb_pools=lb_pairs,
                # the misses its checks found so far (SDD §28.9)
                sync_passes=current.sync_passes if current is not None else None,
                # the conversion hosts' keys, as the checks just found them (SDD §9.3, §26.5)
                host_key_checking=self.settings.ansible_host_key_checking,
                checked=as_checked,
                # the plan's project credentials as the checks just read them, and its VMs' keys
                # (SDD §9.3, §27.6, §27.7, plan task TC6)
                src_caps=src_caps,
                now=judged_at,
                keypair_fingerprints=prints,
            )
            findings = [*findings, *detached]
            resolved = resolve_mappings(vm, plan, dst_inv)
            reasons = eligibility(
                vm,
                source.kind,
                plan,
                src_caps,
                dst_caps,
                findings,
                destination.label,
                # what an earlier attempt left at the destination (SDD §9.2, review 39 lead L1)
                holds_volumes=vm.source_id in holding_volumes,
            )
            # the DNS record's TTL, apart from the downtime (SDD §9.1)
            ttl = ttls.get(vm.source_id)
            # the disks a tracked plan's final pass is expected to read from extents (SDD §28.6)
            checked_source = source.model_copy(update={"capabilities": src_caps})
            tracked = (
                qualifying_disks(vm, checked_source) if tracking_ready(plan, checked_source) else []
            )
            estimates = [
                estimate(
                    vm,
                    strategy,
                    params,
                    plan.downtime_slo_s,
                    hooks_s=hooks_s,
                    dns_tail_s=float(ttl) if ttl is not None else None,
                    tracked=tracked,
                ).model_copy(update={"eligible": not why, "reasons": list(why)})
                for strategy, why in reasons.items()
            ]
            strategy, why = select_strategy(vm, estimates, plan)
            automatic = (
                plan.default_strategy == "auto" and vm.source_id not in plan.strategy_overrides
            )
            # SDD §14.4: validation makes no choice for a migration in flight or finished (it
            # leaves it alone), so the advisor is not asked about it
            chooses = current is None or reassessed(current)
            advice = None
            if self.advisor is not None and automatic and chooses:
                # the destination by kind and series: its provider's name never leaves for Jev
                # (SDD §1 naming, §13.4) - the series this validation's own check settled, the
                # report it judges with, never the one stored before it (review RV21-01)
                checked = destination.model_copy(update={"capabilities": dst_caps})
                advice = await self.advisor.recommend_strategy(
                    vm,
                    estimates,
                    plan,
                    findings,
                    destination_descriptor(checked),
                    destination.label,
                )
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
            # a decision that waits for approval holds the migration from the same write on
            # (SDD §5.4, §14.6); any other validation clears an earlier hold
            waiting = decision is not None and decision.status == DecisionStatus.pending_approval
            hold = rec_id if waiting else None
            # the recommendation joins the migration's assessment in its transaction, the
            # agentmemory search and the input snapshot before it: no store failure leaves the
            # strategy, the hold or the decision without its record and events, and a cancel
            # meanwhile finds the migration as it was (SDD §5.4, §14.4, audit AD-05, AD-11)
            also: Joined | None = None
            if advice is not None and decision is not None:
                incidents: list[SimilarIncident] = []
                if memory_up and self.knowledge is not None and evidence_option is not None:
                    found = await self.knowledge.similar_for(evidence_option, vm)
                    memory_up = found is not None
                    incidents = found or []
                also = await self._strategy_record(
                    plan,
                    vm,
                    migration_id,
                    advice,
                    decision,
                    rec_id,
                    actor,
                    evidence_option=evidence_option,
                    evidence=evidence,
                    incidents=incidents,
                )
            # each migration is written once (SDD §5.4): a new one created already assessed
            if current is None:
                migration = await self._create_migration(
                    plan,
                    vm,
                    strategy,
                    actor,
                    migration_id=migration_id,
                    why=why,
                    estimates=estimates,
                    tracked_disks=tracked,
                    findings=findings,
                    resolved=resolved,
                    hold=hold,
                    recommended=rec_id if also is not None else None,
                    also=also,
                )
            else:
                migration = await self._apply_validation(
                    current.id,
                    plan,
                    vm,
                    strategy,
                    why,
                    estimates,
                    findings,
                    actor,
                    resolved,
                    hold=hold,
                    recommended=rec_id if also is not None else None,
                    also=also,
                    tracked_disks=tracked,
                )
            # recorded only when this validation took the choice (ready or blocked now): one that
            # went in flight or was cancelled meanwhile was left alone, without the record
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
        why: str,
        estimates: list[Estimate],
        findings: list[Finding],
        resolved: Mappings | None = None,
        hold: str | None = None,
        recommended: str | None = None,
        also: Joined | None = None,
        tracked_disks: list[str] | None = None,
    ) -> Migration:
        """A new migration, created already assessed (SDD §5.4): its history from pending through
        validating to ready or blocked in one write with migration.created, the phase events and
        what ``also`` joins - one transaction."""
        now = self._now()
        migration = Migration(
            id=migration_id or new_migration_id(),
            plan_id=plan.id,
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
            created_at=now,
            updated_at=now,
        )
        final = self._assess(
            migration, plan, vm, strategy, why, estimates, findings, resolved, hold, recommended
        )
        migration.tracked_disks = list(tracked_disks or [])  # SDD §28.6, plan task CE8
        migration, _ = await self._transitions(
            migration,
            0,
            [(P.validating, "pre-flight validation", None), final],
            actor,
            created={"vm": vm.name, "source_id": vm.source_id, "strategy": str(strategy)},
            also=also,
        )
        return migration

    @staticmethod
    def _assess(
        m: Migration,
        plan: Plan,
        vm: VMRef,
        strategy: Strategy,
        why: str,
        estimates: list[Estimate],
        findings: list[Finding],
        resolved: Mappings | None,
        hold: str | None,
        recommended: str | None = None,
    ) -> tuple[Phase, str, dict[str, Any] | None]:
        """Write a validation's assessment into ``m``; return its last transition: to ready, or
        to blocked by a blocker or when no strategy is eligible (SDD §5.4, §9)."""
        no_eligible = not any(e.eligible for e in estimates)
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
        if recommended is not None:
            # the one a decision acts on while no hold names one (SDD §14.6, review RV51-08)
            m.last_recommendation = recommended
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
        if has_blocker(findings) or no_eligible:
            codes = [f.code for f in findings if f.severity == "blocker"]
            reason = f"blocked by {', '.join(codes)}" if codes else "no eligible strategy"
            return P.blocked, reason, {"findings": codes}
        return P.ready, why, {"strategy": str(strategy)}

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
        recommended: str | None = None,
        also: Joined | None = None,
        tracked_disks: list[str] | None = None,
    ) -> Migration:
        async with self._lock(mid):
            m, v = await self._load(mid)
            # SDD §5.4: one left in validating by an interrupted validation is taken over as it is
            # (validations of a plan run one at a time under its lock: none is at work on it)
            if not reassessed(m):
                # in flight, finished or its source stopped: left alone, but in its VM's wave in
                # the plan, so waves planned anew reach it before the plan starts (SDD §9.4,
                # review PL-11)
                wave = plan.wave_of(m.vm.source_id)
                if m.wave_id != (wave.id if wave else None):
                    m.wave_id = wave.id if wave else None
                    await self._save(m, v)
                return m
            steps: list[tuple[Phase, str, dict[str, Any] | None]] = []
            if m.phase != P.validating:
                steps.append((P.validating, "pre-flight validation", None))
            final = self._assess(
                m, plan, vm, strategy, why, estimates, findings, resolved, hold, recommended
            )
            m.tracked_disks = list(tracked_disks or [])  # a fresh assessment (SDD §28.6)
            # both transitions in one write, with what joins them (SDD §5.4)
            m, v = await self._transitions(m, v, [*steps, final], actor, also=also)
        return m

    async def _strategy_record(
        self,
        plan: Plan,
        vm: VMRef,
        migration_id: str,
        advice: Advice,
        decision: Decision,
        rec_id: str,
        requested_by: str,
        *,
        evidence_option: str | None = None,
        evidence: Sequence[EvidenceItem] = (),
        incidents: Sequence[SimilarIncident] = (),
    ) -> Joined:
        """The strategy recommendation, its decision, its evidence and its input, with their two
        audit events, to join the migration's assessment in its transaction; the input snapshot,
        content-addressed, is stored first (SDD §5.4, §14.4, §14.5, audit AD-05)."""
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
            migration_id=migration_id,
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
                unusable=unusable(advice),
            ),
            decision=decision,
            input_snapshot_sha256=snapshot,
            requested_by=requested_by,
            evidence_option=evidence_option,
            evidence=list(evidence),
            similar_incidents=list(incidents),
            reasons=keep_reasons(advice.reasons, evidence, recommendation_id=rec_id),
        )
        events = [
            new_event(
                kind, message, plan_id=plan.id, migration_id=migration_id, actor=by, data=data
            )
            for kind, message, by, data in (created_event(rec), decision_event(rec))
        ]

        def joined(migration: Migration) -> tuple[list[tuple[str, Any, int]], list[Event]]:
            return [("recommendation", rec, 0)], events

        return joined

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
        existing = _by_vm(await self.db.list("migration", Migration, plan_id=plan.id))
        # the VMs as validation reads them: the plan's source credential (SDD §8, §27.5)
        vms = await self._vms(
            plan, for_step(self.providers, Step.INVENTORY, source, plan), existing
        )
        if self.advisor is not None:
            # the destination by kind and series: its provider's name never leaves for Jev
            # (SDD §13.4, audit AD-09); one gone meanwhile has no name to leave
            try:
                destination = await self._provider(
                    plan.destination_provider_id, ProviderRole.destination
                )
                named = (destination.label, destination_descriptor(destination))
            except BadRequest:
                named = (None, "")
            tiers, note = await self.advisor.classify_workloads(vms, *named)
        else:
            tiers = {vm.source_id: heuristic_tier(vm) for vm in vms}
            note = AdvisorNote(
                kind="classification",
                source="rules",
                summary=f"Classified {len(vms)} VM(s) with the heuristic",
                data={"tiers": dict(tiers)},
            )
        waves = plan_waves(vms, tiers, max_wave_size, source.gpu_profiles)

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
        twice = _named_twice(migrations)
        if twice is not None:
            raise NotAllowed(twice)  # SDD §5.4 (review RV51-18): as the validation refuses it
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

        # PATCH /providers checks for running plans and writes under this lock (SDD §12, audit
        # AP-07): taken last and innermost, it holds no other (§8)
        async with self.provider_lock():
            plan = await self._update_plan(plan.id, start)
        # the tick pre-stages it again for any migration assessed since its last pre-staging, a VM
        # added included - not for a plan merely resumed (SDD §8, reviews OC-05, RV51-19)
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

    async def cancel_plan(self, plan_id: str, actor: str, reason: str | None = None) -> Plan:
        """Retire a plan none of whose migrations started (SDD §12): each is cancelled as
        :meth:`cancel` does, then the plan becomes ``cancelled``. Nothing is deleted (§11): its
        VMs are free for another plan and its providers for deletion; its history stays."""
        reason = _text(reason)  # as Python's strip leaves it (SDD §12, review RV52-S3)
        async with self._lock(f"plan:{plan_id}"):  # SDD §8: no validation, start or step meanwhile
            plan = await self._plan(plan_id)
            if plan.status == PlanStatus.cancelled:
                return plan
            if plan.status == PlanStatus.running:
                raise NotAllowed("pause the plan before cancelling it")
            # SDD §5.4: a validation in another process claims VMs under this lock too
            async with self._claims():
                migrations = await self.db.list("migration", Migration, plan_id=plan_id)
                started = [m for m in migrations if m.phase not in UNSTARTED | {P.cancelled}]
                if started:
                    raise NotAllowed(started_refusal(started))
                todo = sorted((m for m in migrations if m.phase != P.cancelled), key=lambda m: m.id)
                # SDD §12: refused before any migration is cancelled - one cancel() would refuse
                # midway, after the ones sorted before it were cancelled for good
                stopped = [m for m in todo if fsm.source_stopped(m)]
                if stopped:
                    raise NotAllowed(stopped_refusal(stopped))
                # SDD §12 (audit OC-07): a pre-staging that still runs - the plan was paused while
                # it ran - is stopped first, the executor killing its playbook as for a step
                prestaging = self._prestage_tasks.get(plan_id)
                if prestaging is not None and not prestaging.done():
                    prestaging.cancel()
                    await asyncio.wait({prestaging})
                why = f"plan cancelled: {reason}" if reason else "plan cancelled"
                for m in todo:
                    await self.cancel(m.id, actor, why)

                def retire(fresh: Plan) -> None:
                    fresh.status = PlanStatus.cancelled

                plan = await self._update_plan(plan_id, retire)
        await self._emit(
            "plan.cancelled",
            f"{plan.name} cancelled",
            plan_id=plan_id,
            actor=actor,
            data={"reason": reason, "migrations": len(todo)},
        )
        return plan

    # ------------------------------------------------------------------------------------------
    # migration actions (each serialized with the driver by the migration lock)
    async def approve(self, mid: str, actor: str, comment: str | None = None) -> Migration:
        comment = _text(comment)
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
            # SDD §5.4 (audit OC-03): an approval that opens the gate never lacks its event
            await self._save_with_events(m, v, [self._approved_event(m, actor, comment)])
        self.wake()
        return m

    def _approved_event(self, m: Migration, actor: str, comment: str | None) -> Event:
        return new_event(
            "migration.approved",
            f"{m.vm.name} approved by {actor}",
            plan_id=m.plan_id,
            migration_id=m.id,
            actor=actor,
            data={"comment": comment},
        )

    async def request_cutover(
        self, mid: str, actor: str, force_window: bool = False, comment: str | None = None
    ) -> Migration:
        comment = _text(comment)
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
            requested = new_event(
                "migration.action",
                f"cutover of {m.vm.name} requested by {actor}",
                plan_id=m.plan_id,
                migration_id=m.id,
                actor=actor,
                data={"action": "cutover", "force_window": force_window, "comment": comment},
            )
            # SDD §5.4 (audit OC-03): the request, its approval and their events in one write
            events = [self._approved_event(m, actor, comment)] if approves else []
            await self._save_with_events(m, v, [*events, requested])
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
            params = self._params(m, plan)
            # after the running pass: as long as the last one that ended - its whole time, §9.1
            # T_last -, or the first pass while none has (SDD §5.4, review RV39-07)
            estimated = estimate_forced_downtime(
                m.vm,
                m.strategy,
                whole_pass_seconds(last, m.strategy, params) if last is not None else None,
                params,
                # the post_create hooks' part of its estimate (SDD §9.1)
                hooks_part(m.estimate),
                m.tracked_disks,
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
            forced = new_event(
                "migration.force_cutover",
                f"cutover of {m.vm.name} forced by {actor}: {reason}",
                plan_id=m.plan_id,
                migration_id=m.id,
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
            # SDD §5.4 (audit OC-03): the force and its audit event in one write - a force with
            # no record could never get one, as a second force is refused
            await self._save_with_events(m, v, [forced])
        self.wake()
        return m

    async def request_sync(self, mid: str, actor: str) -> Migration:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase != P.awaiting_cutover or m.strategy not in WARM_STRATEGIES:
                raise NotAllowed("a sync pass can only be requested in awaiting_cutover")
            m, v = await self._transitions(
                m,
                v,
                [(P.syncing, f"delta pass requested by {actor}", None)],
                actor,
                also=_action(f"sync of {m.vm.name} requested", actor, {"action": "sync"}),
            )
        self._launch(mid)
        return m

    async def rollback(self, mid: str, actor: str, reason: str) -> Migration:
        reason = reason.strip()
        if not reason:
            raise Unprocessable("reason is required.")  # as the dashboard says (SDD §12)
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
            m, v = await self._transitions(
                m,
                v,
                [(P.rolling_back, f"rollback requested: {reason}", None)],
                actor,
                also=_action(
                    f"rollback of {m.vm.name} requested",
                    actor,
                    {"action": "rollback", "reason": reason},
                ),
            )
            step = self._steps.get(mid)
            if step is not None:
                step.cancel()
        self._launch(mid)
        return m

    async def retry(self, mid: str, actor: str) -> Migration:
        # marked from its first lock to its own reopen: releasing the claims awaits the
        # database, so a tick could otherwise reopen the plan in between, as "system"
        self._retrying[mid] += 1
        try:
            return await self._retry(mid, actor)
        finally:
            self._retrying[mid] -= 1
            if not self._retrying[mid]:
                del self._retrying[mid]

    async def _retry(self, mid: str, actor: str) -> Migration:
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
            siblings = sorted(
                x.id
                for x in await self.db.list("migration", Migration, plan_id=m.plan_id)
                if x.id != m.id and x.vm.source_id == m.vm.source_id and x.phase != P.cancelled
            )
            if siblings:
                # SDD §5.4 (audit OC-06): two migrations of one VM would both cut it over
                raise NotAllowed(
                    f"{m.vm.name} cannot be retried: another migration of this plan "
                    f"({', '.join(siblings)}) holds the VM; cancel one of them first"
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
            wave = plan.wave_of(m.vm.source_id)
            if plan.waves and wave is None:
                # it would run at once, outside every wave's order and max_parallel (SDD §9.4)
                raise NotAllowed(
                    f"{m.vm.name} cannot be retried: it belongs to no wave of the plan; re-run the "
                    "automatic wave planning or add it to a wave"
                )
            # the attempt runs in its VM's wave in the plan, never in one the waves planned anew
            # dropped - the tick reads that as no wave (SDD §9.4, review PL-11)
            m.wave_id = wave.id if wave else None
            if m.phase == P.rolled_back:
                # the FSM counts failed -> ready only; a retry after an automatic rollback
                # is still a new attempt of the cutover (executors key their behaviour on it)
                m.attempts += 1
            # everything below is saved in the same write as the move to ready (SDD §5.1): a crash
            # never leaves a ready migration with the earlier attempt's request and bypass
            # the strategy of the attempt being retried: the evidence names it (SDD §14.4), even
            # after a later validation chose another
            m.attempt_strategies = [*m.attempt_strategies, m.strategy]
            # nothing is assessed anew - the estimates the last validation stored stay -, but a
            # handover is ineligible while the attempt's destination volumes are held, which the
            # dashboard's strategy choice and the advisor's options then show (SDD §12, §9.2)
            if await self.holds_destination_volumes(m):
                m.estimates = [_held_out(e) for e in m.estimates]
                if m.estimate is not None:
                    m.estimate = _held_out(m.estimate)
            else:
                # an earlier Retry's mark comes off once nothing is held (SDD §12, RV43B-02)
                m.estimates = [_held_in(e) for e in m.estimates]
                if m.estimate is not None:
                    m.estimate = _held_in(m.estimate)
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

            def retried(new: Migration) -> tuple[list[tuple[str, Any, int]], list[Event]]:
                # the attempts of the migration as transitioned - the FSM counts failed -> ready
                # itself -, so the action names what is stored (SDD §5.1, review RV52-O6)
                data = {"action": "retry", "attempts": new.attempts}
                return _action(f"retry of {new.vm.name}", actor, data)(new)

            m, v = await self._transitions(
                m, v, [(P.ready, f"retry requested by {actor}", None)], actor, also=retried
            )
        # SDD §5.1/§8: a completed plan is driven no more, so the new attempt reopens it - under
        # the plan's lock, taken only now: held across the claim wait above, it would hold up the
        # tick; a completion that wrote before the transition is seen here, a later one sees ready.
        # One a store error keeps from it is made by the next tick (review RV51-14)
        await self._reopen_completed(m.plan_id, actor, f"retry of {m.vm.name}")
        self.wake()
        return m

    async def _reopen_completed(self, plan_id: str, actor: str, why: str) -> None:
        """Run a completed plan again while it holds a ``ready`` migration (SDD §5.1, §8): under
        the plan's lock, as a completion is written, on the migrations read there."""
        reopened = False

        def reopen(fresh: Plan) -> bool:
            nonlocal reopened
            reopened = fresh.status == PlanStatus.completed  # re-evaluated on every retry
            if not reopened:
                return False
            fresh.status = PlanStatus.running
            return True

        async with self._lock(f"plan:{plan_id}"):
            migrations = await self.db.list("migration", Migration, plan_id=plan_id)
            if not any(m.phase == P.ready for m in migrations):
                return
            plan = await self._update_plan(plan_id, reopen)
            if reopened:
                await self._emit(
                    "plan.updated",
                    f"{plan.name} reopened: {why}",
                    plan_id=plan.id,
                    actor=actor,
                    data={"status": "running"},
                )

    async def cancel(
        self, mid: str, actor: str, reason: str | None = None, *, by_validation: bool = False
    ) -> Migration:
        reason = _text(reason)
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase == P.validating and not by_validation:
                # SDD §5.1: a validation of the plan may be moving it; only a validation cancels
                # one - under the plan's lock it knows it is a leftover whose VM left vm_ids
                raise NotAllowed(
                    f"{m.vm.name} is being validated and cannot be cancelled; validate the plan "
                    "again - it finishes the migration, or cancels it once its VM left vm_ids"
                )
            if by_validation and m.phase not in REMOVABLE:
                # SDD §5.4: an operator moved it meanwhile (a rollback, even a finished one an
                # operator may cancel): the operator's move stands
                raise NotAllowed(f"{m.vm.name} moved to {m.phase} meanwhile")
            why = fsm.refusal(m, P.cancelled)
            if why is not None:
                if P.cancelled not in fsm.TRANSITIONS[m.phase]:
                    raise NotAllowed(f"a migration in {m.phase} cannot be cancelled")
                raise NotAllowed(f"{m.vm.name} cannot be cancelled: {why}")  # e.g. source stopped
            m.advisor_hold = None  # a cancelled migration takes no decision (SDD §14.6)
            # SDD §5.1: in precopy or syncing a pass runs, waits for its retry (the cancel ends
            # the retries, §8) or has just finished unrecorded; a migration that keeps copied data
            # left snapshots, temporary and destination volumes behind, and `cancelled` is
            # terminal: no later action could remove them
            data_path = m.phase in (P.precopy, P.syncing) or await self.copied_data(m)
            if data_path:
                m.checkpoint = "cleanup"  # saved with cancelled: a crash cannot lose the cleanup
            m, v = await self._transitions(
                m,
                v,
                [(P.cancelled, f"cancelled: {reason or 'no reason given'}", None)],
                actor,
                also=_action(
                    f"{m.vm.name} cancelled", actor, {"action": "cancel", "reason": reason}
                ),
            )
            step = self._steps.get(mid)
            if step is not None:
                step.cancel()
        # the cancel and its record are written: nothing can raise before its cleanup starts
        if data_path and not self._stopping:
            # the rollback step removes them (a cancel never leaves the source stopped, §5.1)
            self._cleanups[mid] = asyncio.create_task(
                self._cleanup_after_cancel(m, step, actor), name=f"cleanup:{mid}"
            )
        self.wake()
        return m

    async def copied_data(self, m: Migration) -> bool:
        """Whether the migration keeps copied data (SDD §5.1, §12): what its workload's record
        says - destination volumes or a pending source snapshot - and, where no record can tell,
        what its rollback kept (§9.3, review RV39-02)."""
        recorded = await asyncio.to_thread(recorded_copied_data, self.settings, m.plan_id, m)
        if recorded is not None:
            return recorded
        return _kept_after_rollback(m)

    async def holds_destination_volumes(self, m: Migration) -> bool:
        """Whether the migration holds destination volumes an earlier attempt created, which the
        next attempt uses again (SDD §9.3, reviews RV38A-01, RV39-02): what its workload's record
        says, and where no record can tell what its rollback kept."""
        recorded = await asyncio.to_thread(
            recorded_destination_volumes, self.settings, m.plan_id, m
        )
        if recorded is not None:
            return recorded
        return _kept_after_rollback(m)

    async def keeps_copied_data(self, m: Migration) -> bool:
        """A rolled-back migration that keeps copied data: a cancel of it cleans the data up, a
        cleanup that needs both providers (SDD §5.1, §12)."""
        return m.phase == P.rolled_back and await self.copied_data(m)

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
                # the step of the attempt whose data it cleans, run as that attempt (SDD §5.1)
                strategy = _cleanup_strategy(m)
                ctx = self._context(
                    m.model_copy(update={"strategy": strategy}),
                    plan,
                    source,
                    destination,
                    options={"delete_dest_volumes": True},
                    locked=True,
                )
                # SDD §8, §25.4 layer 2: a deposed leader leaves the cleanup to the next one
                await self._hold_the_lease(f"the cleanup of {m.vm.name}")
                result = await self.executors.for_strategy(strategy).run(StepName.ROLLBACK, ctx)
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
            message = f"{m.vm.name}: temporary resources of the cancelled migration removed"
            data: dict[str, Any] = {"action": "cleanup"}
            # the migration kit's volumes of the VM a VMware cleanup keeps: another plan's
            # migration of the VM takes them as its own (SDD §5.1, §7.2, review RV42B, lead NP-2)
            kept = result.details.get("kept_volumes")
            unread = result.details.get("kept_volumes_unread")
            if kept:

                def named(volume: dict[str, Any]) -> str:
                    # the kit's copy a cut-off pass left on the conversion host (RV44B-01)
                    held = volume.get("on_conversion_host")
                    where = ", attached to the conversion host" if held else ""
                    return f"{volume['id']} ({volume['name']}{where})"

                data["kept_volumes"] = [v["id"] for v in kept]
                message += (
                    f"; {destination.label} keeps the migration kit's volumes of the VM, "
                    + ", ".join(named(v) for v in kept)
                    + ": another plan's migration of the VM takes them as its own - delete them "
                    "once no plan migrates the VM"
                )
            elif unread:
                message += (
                    "; the migration kit's volumes of the VM it keeps could not be read from "
                    f"{destination.label} ({redact_cut(unread, 300)}): another plan's migration "
                    "of the VM takes them as its own - delete them once no plan migrates the VM"
                )
            # held by a server it could not tell - the conversion host's lookup failed: named with
            # the error, never among the volumes to delete (SDD §5.1, review RV45B-02)
            unsure = result.details.get("unsure_volumes")
            if unsure:
                message += (
                    "; it could not tell which server holds "
                    + ", ".join(f"{v['id']} ({v['name']})" for v in unsure)
                    + f" ({redact_cut(str(result.details.get('unsure_why') or ''), 300)}): each "
                    "may be the copy a cut-off pass left on the conversion host or another "
                    "server's disk - check which server holds it before deleting it"
                )
            await record(
                "cleaned",
                self._emit("migration.action", message, migration=m, actor=actor, data=data),
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
            if cleanup_pending(m):
                m.checkpoint = outcome
                await self._save(m, v)

    async def finalize(
        self, mid: str, actor: str, delete_source: bool = False, confirm: str = ""
    ) -> Migration:
        try:
            return await self._finalize(mid, actor, delete_source, confirm)
        except NotLeading as exc:
            # a person's action answers as one: the replica no longer leads (SDD §8, §12)
            raise NotAllowed(f"finalize refused: {exc}") from None

    async def _finalize(self, mid: str, actor: str, delete_source: bool, confirm: str) -> Migration:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if m.phase != P.completed:
                raise NotAllowed(f"only completed migrations can be finalized ({m.phase})")
            # SDD §8.1 (the owner's choice, 2026-10-13): a source running again diverged - a
            # finalize would keep it, or delete it with what it wrote since; one that began
            # deleting it finishes deleting it, as a rollback is refused then - keeping it is
            # refused too (audit OC-09)
            if m.source_running_at is not None and not (
                m.checkpoint == "finalizing" and delete_source
            ):
                raise NotAllowed(SOURCE_RUNS_AGAIN)
            if confirm != m.vm.name:
                raise BadRequest("confirm must equal the VM name")
            # SDD §8, §25.4 layer 2: the finalize's step starts only while the lease holds
            await self._hold_the_lease(f"the finalize of {m.vm.name}")
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
            # SDD §8.3: the source's allocation of each moved floating IP is released
            await self._release_floating_ips(m, plan, source)
            # SDD §8.2: on_finalize after the step; a failure is recorded, the VM finalized
            await self._run_hooks(m, "on_finalize", keep_going=True)
            m.checkpoint = "finalized"
            # a store error leaves it completed for a later finalize, as a bulk finalize reports
            # it (SDD §8.1, review RV51-13)
            m, v = await self._transitions(
                m,
                v,
                [
                    (
                        P.finalized,
                        f"finalized by {actor}",
                        {"delete_source": delete_source, **result.details},
                    )
                ],
                actor,
                also=_action(
                    f"{m.vm.name} finalized",
                    actor,
                    {"action": "finalize", "delete_source": delete_source},
                ),
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
            if m.source_running_at is not None:
                # SDD §8.1: every VM whose source runs again, also one whose finalize began deleting
                # it - its own Finalize is its way out - so the plan's summaries tell what this does
                skipped.append({"migration_id": m.id, "reason": SOURCE_RUNS_AGAIN})
                continue
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
        """The client of the plan's source provider (the source watch, the demo controls), with
        the plan's source credential (SDD §8.1, §27.5)."""
        provider = await self.db.get("provider", plan.source_provider_id, Provider)
        return for_step(self.providers, Step.MIGRATION, provider, plan)

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
        clients: dict[tuple[str, str | None], Any] = {}
        # each plan read once per pass, a plan that is gone too: a plan of 1,000 VMs is ~22 KB,
        # and in demo the watch runs every few seconds
        plans: dict[str, Plan | None] = {}
        #: the name of each plan's destination provider, as the alert names it (SDD §1 naming)
        destinations: dict[str, str] = {}
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
                # one client per provider and credential: plans of one source may name
                # different projects (SDD §8.1, §27.5)
                key = (plan.source_provider_id, plan.source_project)
                if key not in clients:
                    clients[key] = await self.source_client(plan)
                vm = await clients[key].get_vm(m.vm.source_id)
            except VMNotFound:
                # SDD §8.1 (the owner's choice, 2026-10-14): the cloud says the VM is gone - a
                # finalize deleted it, a handover rebuilt it, or someone deleted the source after
                # it ran again: nothing left to keep or roll back to, so a flag set clears
                if m.source_running_at is not None:
                    await self._source_gone(m.id)
                continue
            except (ProviderError, NotFound):
                continue  # unreachable, or the plan is gone: the flag stays as it is
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
            if running and plan.destination_provider_id not in destinations:
                # the alert names the destination provider (T6), read before the flag is saved: a
                # store error leaves both to the next pass, never a flag without its alert (SDD
                # §8.1, review RV20-03)
                try:
                    provider = await self.db.get("provider", plan.destination_provider_id, Provider)
                    destinations[plan.destination_provider_id] = provider.label
                except NotFound:
                    destinations[plan.destination_provider_id] = "the destination"
                except Exception:
                    log.warning("source watch failed for %s", m.id, exc_info=True)
                    continue
            async with self._lock(m.id):
                cur, v = await self._load(m.id)
                if cur.phase != P.completed or running == (cur.source_running_at is not None):
                    continue
                cur.source_running_at = seen if running else None
                # the flag and its event in one store transaction: a store error leaves neither,
                # and the next pass tries again (SDD §8.1, review RV21-04)
                if running:
                    await self._save_with_event(
                        cur,
                        v,
                        "migration.source_running",
                        f"{cur.vm.name}: the source VM runs again - its data diverges from "
                        f"{destinations[plan.destination_provider_id]}; stop it, or roll back",
                        {"power_state": str(vm.power_state)},
                    )
                else:
                    await self._save_with_event(
                        cur,
                        v,
                        "migration.source_stopped",
                        f"{cur.vm.name}: the source VM is stopped again",
                        {"power_state": str(vm.power_state)},
                    )

    async def _source_gone(self, mid: str) -> None:
        """Clear source_running_at of a completed migration whose source VM its cloud no longer
        has (SDD §8.1), with migration.source_stopped marked gone."""
        async with self._lock(mid):
            cur, v = await self._load(mid)
            if cur.phase != P.completed or cur.source_running_at is None:
                return
            cur.source_running_at = None
            await self._save_with_event(
                cur,
                v,
                "migration.source_stopped",
                f"{cur.vm.name}: the source VM no longer exists - nothing left to keep or roll "
                "back to",
                {"power_state": None, "gone": True},
            )

    # ------------------------------------------------------------------------------------------
    # the cutover gate's fifth rule: GPU capacity (SDD §5.4 rule 5, §22.7)
    def _gpu_read_s(self) -> float:
        """How long a GPU capacity read counts from its answer (SDD §5.4 rule 5, §22.7): GPU_READ_S;
        in demo mode ÷ demo_speed, as the source watch scales its interval (§8.1), but never under
        GPU_READ_FLOOR_TICKS ticks - the model runs faster, the tick's own work does not."""
        if not self.settings.demo:
            return GPU_READ_S
        scaled = GPU_READ_S / max(self.settings.demo_speed, 1.0)
        return max(scaled, GPU_READ_FLOOR_TICKS * self.settings.tick_s)

    def _gpu_outcome(self, m: Migration, now: datetime) -> _GpuOutcome | None:
        """``m``'s last GPU capacity read while it counts: answered at most ``_gpu_read_s`` ago, in
        its present stay at its gate (SDD §5.4 rule 5, §22.7)."""
        outcome = self._gpu_outcomes.get(m.id)
        if outcome is None or outcome.mark != _wait_mark(m):
            return None
        if (now - outcome.at).total_seconds() > self._gpu_read_s():
            return None
        return outcome

    def _gpu_place(self, m: Migration, now: datetime, destination_id: str) -> GpuPlace:
        """Rule 5 on the tick's snapshot for a GPU VM with a cutover slot left for it (SDD §5.4,
        §22.7): "go" on a read that counts and found room; "hold" while its read is out - started
        here, as a task of its own the tick never waits for, unless its destination already has
        max_concurrent_cutovers reads out -, so its place is kept; "wait" on a read that counts and
        found no room or failed, and while no read can start for it."""
        outcome = self._gpu_outcome(m, now)
        if outcome is not None:
            return "go" if outcome.read.room else "wait"
        task = self._gpu_reads.get(m.id)
        if task is not None and not task.done():
            return "hold"
        reading = [
            destination
            for mid, destination in self._gpu_read_destinations.items()
            if not self._gpu_reads[mid].done()
        ]
        per_destination = min(self.settings.max_concurrent_cutovers, GPU_READS_MAX - 1)
        if reading.count(destination_id) >= per_destination or len(reading) >= GPU_READS_MAX:
            return "wait"  # the reads out hold their worker threads: one ends first
        self._start_gpu_read(m.id, destination_id)
        return "hold"

    async def _gpu_room_now(self, m: Migration) -> bool:
        """Rule 5 under the locks (SDD §5.4, §22.7): ``m``'s read still counts and found room, and
        no cutover has begun on the destination since the read began, asking of the pools it read
        - whatever phase that migration is in now: its server may hold the unit the read found
        free (one the read counted only delays). Else the read is dropped, and the next tick reads
        again."""
        if not vm_gpus(m.vm):
            return True
        outcome = self._gpu_outcome(m, self._now())
        if outcome is None or not outcome.read.room:
            return False
        read = outcome.read
        if not read.keys:
            return True  # nothing was counted against free units: an alias, unknown capacity
        plan = await self._plan(m.plan_id)
        begun = self._cutovers_begun[plan.destination_provider_id]
        if begun.count == read.begun:
            return True
        latest = list(begun.latest)
        if latest[0][0] > read.begun + 1:
            return self._drop_gpu_outcome(m.id)  # some begun since are no longer kept: read again
        kinds: dict[str, ProviderKind] = {}
        for number, other_id in latest:
            if number <= read.begun or other_id == m.id:
                continue
            try:
                other, _ = await self._load(other_id)
                other_plan = await self._plan(other.plan_id)
                kind = await self._source_kind(other_plan, kinds)
            except NotFound:
                return self._drop_gpu_outcome(m.id)  # nothing tells what it asks: read again
            flavor = server_gpu_flavor(other.vm, other_plan, read.flavors, kind)
            if flavor is not None and not read.keys.isdisjoint(gpu_units(flavor["gpus"])):
                return self._drop_gpu_outcome(m.id)
        return True

    def _drop_gpu_outcome(self, mid: str) -> bool:
        """Drop ``mid``'s read and wake the tick to read again: False, the cutover waits."""
        self._gpu_outcomes.pop(mid, None)
        self.wake()
        return False

    def _cutover_begun(self, destination_id: str, mid: str) -> None:
        """Count a cutover begun on the destination, for the reads made before it (SDD §22.7)."""
        begun = self._cutovers_begun[destination_id]
        begun.count += 1
        begun.latest.append((begun.count, mid))

    def _start_gpu_read(self, mid: str, destination_id: str) -> None:
        """Start ``mid``'s GPU capacity read of the destination as a task of its own; the tick
        never waits for it, and ``_gpu_place`` starts none while one is out for ``mid`` - a slow
        read delays that cutover, nothing else (SDD §5.4 rule 5, §22.7)."""
        if self._stopping:
            return
        self._gpu_read_destinations[mid] = destination_id
        self._gpu_reads[mid] = asyncio.create_task(self._read_gpu(mid), name=f"gpu-read:{mid}")

    async def _read_gpu(self, mid: str) -> None:
        """A GPU capacity read for a migration at its cutover gate, and its outcome: its wait
        begins, goes on or ends (SDD §5.4 rule 5, §22.7). A failure outside the destination's
        answers - the store's, say - holds the cutover as a read without room does, logged, and
        the next read follows a minute later."""
        mark: datetime | None = None
        gated = False
        try:
            m, _ = await self._load(mid)
            if m.phase not in fsm.CUTOVER_WAIT_PHASES:
                return
            mark, gated = _wait_mark(m), True
            plan = await self._plan(m.plan_id)
            # the cutovers begun on its destination so far: one begun after this, which the read
            # may not count, makes it stale (SDD §22.7)
            begun = self._cutovers_begun[plan.destination_provider_id].count
            source = await self.db.get("provider", plan.source_provider_id, Provider)
            destination = await self.db.get("provider", plan.destination_provider_id, Provider)
            flavor = gpu_flavor(m.vm, plan, source.kind)
            read = _GpuRead(room=True)  # no GPU flavor of its plan: nothing to read for it
            if flavor is not None:
                others = await self._cutovers_without_server(destination.id, mid)
                # Placement's read, with the destination provider's own credential (SDD §27.5)
                client = for_step(self.providers, Step.GPU, destination, plan)
                read = await self._gpu_capacity_read(client, flavor, others, destination.label)
                read = replace(read, begun=begun)
            # when the destination answered: one read may take minutes (§8.1)
            await self._record_gpu_read(mid, mark, read, self._now(), destination.label)
        except asyncio.CancelledError:
            raise
        except Exception:
            # held, as a read without room: the source keeps running (SDD §22.7)
            log.warning(
                "the GPU capacity read of %s failed: its cutover waits for the next read",
                mid,
                exc_info=True,
            )
            if gated:
                self._gpu_outcomes[mid] = _GpuOutcome(self._now(), mark, _GpuRead(room=False))
        finally:
            if self._gpu_reads.get(mid) is asyncio.current_task():
                del self._gpu_reads[mid]
                self._gpu_read_destinations.pop(mid, None)

    async def _gpu_capacity_read(
        self, client: Any, flavor: str, others: list[tuple[Migration, Plan]], where: str
    ) -> _GpuRead:
        """The destination's GPU flavors, then the capacity of the vGPU requests of ``flavor``:
        room for its units plus those ``others`` - the destination's cutovers without a server
        yet - ask of its pools, by the pre-flight's rule (SDD §22.5, §22.7). Capacity Placement
        does not show, a PCI alias and a flavor without a vGPU request leave room; a failed read
        holds the cutover, named by its status or kind (§22.12)."""
        try:
            flavors = tuple(await client.gpu_flavors())
        except Exception as exc:
            reason = f"the GPU flavors of {where} could not be read ({_failure_kind(exc)})"
            return _GpuRead(room=False, failed=True, reason=reason)
        listed = next((f for f in flavors if f.get("name") == flavor), None)
        vgpus = [g for g in (listed or {}).get("gpus", []) if g.kind == "vgpu" and g.resource_class]
        if not vgpus:
            # a PCI alias, whose devices nova.conf configures, or a flavor that asks for no vGPU
            # or is gone - the cutover refuses such a mapping before the source stops (§22.4)
            return _GpuRead(room=True, flavors=flavors)
        own = gpu_units(vgpus)
        asked = dict(own)
        kinds: dict[str, ProviderKind] = {}
        for other, other_plan in others:
            served = server_gpu_flavor(
                other.vm, other_plan, flavors, await self._source_kind(other_plan, kinds)
            )
            for key, units in gpu_units(served["gpus"] if served else []).items():
                asked[key] = asked.get(key, 0) + units
        first = vgpus[0]
        first_key = pool_key(
            first.resource_class or "", first.traits, first.forbidden_traits, first.same_provider
        )
        held = {
            "pool": pool_name(first.resource_class or "", first.traits, first.forbidden_traits),
            "needed": asked[first_key],
        }
        common = {"keys": frozenset(own), "flavors": flavors}
        try:
            capacity = await client.gpu_capacity(vgpus)
        except Exception as exc:
            reason = f"the GPU capacity of {where} could not be read ({_failure_kind(exc)})"
            return _GpuRead(room=False, failed=True, reason=reason, **held, **common)
        if capacity.status != "ok":
            if capacity.reason in ("forbidden", "no_placement"):
                # nothing can be read, and the pre-flight warned (DST_GPU_CAPACITY_UNKNOWN)
                return _GpuRead(room=True, flavors=flavors)
            why = capacity.detail or capacity.reason or "error"
            reason = f"the GPU capacity of {where} could not be read ({why})"
            return _GpuRead(room=False, failed=True, reason=reason, **held, **common)
        found = {}
        for pool in capacity.pools:
            key = pool_key(
                pool.resource_class, pool.traits, pool.forbidden_traits, pool.same_provider
            )
            if key in own:
                name = pool_name(pool.resource_class, pool.traits, pool.forbidden_traits)
                found[name] = (asked[key], pool.free)
        shorts = gpu_shortfalls(vgpus, asked, capacity.pools)
        if not shorts:
            return _GpuRead(room=True, found=found, **common)
        short = shorts[0]
        return _GpuRead(
            room=False,
            pool=short.pool,
            needed=short.needed,
            free=short.free,
            reason=_shortfall_reason(short, where, flavor, own.get(short.key, 0)),
            found=found,
            **common,
        )

    async def _record_gpu_read(
        self, mid: str, mark: datetime | None, read: _GpuRead, answered: datetime, where: str
    ) -> None:
        """A read's outcome for the stay at its gate it was made for (SDD §22.7): a read without
        room begins the migration's wait - ``migration.gpu_wait`` in the same transaction - or
        updates it; one with room ends it, the event saying so once more. A failed read keeps the
        pool and units its wait names."""
        async with self._lock(mid):
            cur, v = await self._load(mid)
            if cur.phase not in fsm.CUTOVER_WAIT_PHASES or _wait_mark(cur) != mark:
                return  # it left its gate since: the read is not its present stay's
            wait = cur.gpu_wait
            if read.room and wait is not None:
                needed, free = read.found.get(wait.pool, (wait.needed, None))
                cur.gpu_wait = None
                await self._save_with_event(
                    cur,
                    v,
                    "migration.gpu_wait",
                    f"{cur.vm.name}: {where} has the GPU capacity its cutover needs",
                    {"waiting": False, "pool": wait.pool, "needed": needed, "free": free},
                )
            elif not read.room:
                pool, needed = read.pool, read.needed
                if read.failed and wait is not None and wait.pool:
                    pool, needed = wait.pool, wait.needed
                cur.gpu_wait = GpuWait(
                    since=wait.since if wait is not None else answered,
                    checked_at=answered,
                    pool=pool,
                    needed=needed,
                    free=read.free,
                    reason=read.reason,
                )
                if wait is None:
                    await self._save_with_event(
                        cur,
                        v,
                        "migration.gpu_wait",
                        f"{cur.vm.name}: its cutover waits for GPU capacity - "
                        f"{cur.gpu_wait.reason}; the source VM keeps running",
                        {"waiting": True, "pool": pool, "needed": needed, "free": read.free},
                    )
                else:
                    await self._save(cur, v)
            self._gpu_outcomes[mid] = _GpuOutcome(answered, mark, read)
        self.wake()  # a read counts for a while only: the tick takes it at once (SDD §22.7)

    async def _cutovers_without_server(
        self, destination_id: str, exclude: str
    ) -> list[tuple[Migration, Plan]]:
        """The destination's migrations in cutover that have recorded no server yet - Placement
        may not count their GPUs yet -, ``exclude`` aside, with their plans (SDD §22.7)."""
        plans: dict[str, Plan | None] = {}
        found: list[tuple[Migration, Plan]] = []
        for other in await self.db.list("migration", Migration, phase=P.cutover):
            if other.id == exclude or other.destination_server_id:
                continue
            if other.plan_id not in plans:
                try:
                    plans[other.plan_id] = await self._plan(other.plan_id)
                except NotFound:
                    plans[other.plan_id] = None
            other_plan = plans[other.plan_id]
            if other_plan is not None and other_plan.destination_provider_id == destination_id:
                found.append((other, other_plan))
        return found

    async def _source_kind(self, plan: Plan, kinds: dict[str, ProviderKind]) -> ProviderKind:
        """The kind of ``plan``'s source provider, read once per call site (SDD §22.4)."""
        if plan.source_provider_id not in kinds:
            source = await self.db.get("provider", plan.source_provider_id, Provider)
            kinds[plan.source_provider_id] = ProviderKind(source.kind)
        return kinds[plan.source_provider_id]

    async def set_strategy(self, mid: str, strategy: Strategy | str, actor: str) -> Migration:
        strategy = Strategy(strategy)
        current, _ = await self._load(mid)
        _refuse_unchangeable(current)  # before the plan's lock (SDD §8); repeated under it
        plan_id = current.plan_id
        # SDD §8: the plan's lock first, as the advisor's override (§14.6): a running validation
        # finishes before the change, and no validation undoes it afterwards
        async with self._lock(f"plan:{plan_id}"), self._lock(mid):
            for attempt in range(PLAN_UPDATE_ATTEMPTS):
                if attempt:
                    await asyncio.sleep(_update_backoff(attempt))
                m, writes, events = await self._strategy_writes(mid, strategy, actor)
                try:
                    await put_all_and_emit(self.store, self.bus, writes, events)
                except ConflictError:
                    continue  # another writer moved the plan or the migration: read them again
                return m
        raise ConflictError(f"migration {mid} or its plan changed too often; run the action again")

    async def _strategy_writes(
        self, mid: str, strategy: Strategy, actor: str
    ) -> tuple[Migration, list[tuple[str, Any, int]], list[Event]]:
        """A strategy change's checks on what is stored now, and its writes: the plan's entry, the
        migration and their events, which ``put_all_and_emit`` makes one transaction - a failure
        leaves none of them, and the retry makes the whole change (SDD §8, reviews ST-08,
        RV48-02)."""
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
        if strategy == Strategy.storage_handover and await self.holds_destination_volumes(m):
            # §9.2 judged now: a Retry does not re-assess the estimates the last validation
            # stored, and a handover would leave the held volumes behind (SDD §12, RV40-01)
            raise BadRequest(f"strategy {strategy} is not eligible: {HELD_VOLUMES_REASON}")
        plan, plan_version = await self.db.get_versioned("plan", m.plan_id, Plan)
        now = self._now()
        plan.strategy_overrides[m.vm.source_id] = strategy
        plan.updated_at = now
        m.strategy = strategy
        m.estimate = est
        m.approvals = []  # the approval was given for the previous strategy (SDD §5.4)
        _clear_request(m)
        m.updated_at = now
        events = [
            new_event(
                "migration.action",
                f"{m.vm.name}: strategy set to {strategy}",
                plan_id=m.plan_id,
                migration_id=m.id,
                actor=actor,
                data={"action": "strategy", "strategy": str(strategy)},
            ),
            new_event(
                "plan.updated",
                f"{plan.name}: strategy override for {m.vm.name}",
                plan_id=plan.id,
                actor=actor,
                data={"strategy_overrides": {m.vm.source_id: str(strategy)}},
            ),
        ]
        return m, [("plan", plan, plan_version), ("migration", m, v)], events

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
            for attempt in range(PLAN_UPDATE_ATTEMPTS):
                if attempt:
                    await asyncio.sleep(_update_backoff(attempt))
                decided, writes, events = await self._decision_writes(
                    rec_id, mid, plan_id, action, actor
                )
                try:
                    await put_all_and_emit(self.store, self.bus, writes, events)
                except ConflictError:
                    continue  # another writer moved a document: read them all again
                return decided
        raise ConflictError(f"recommendation {rec_id} changed too often; run the action again")

    async def _decision_writes(
        self, rec_id: str, mid: str, plan_id: str, action: Approve | Override, actor: str
    ) -> tuple[AdvisorRecommendation, list[tuple[str, Any, int]], list[Event]]:
        """A person's decision's checks on what is stored now, and its writes: an override's plan
        entry, the migration, the recommendation and their events, which ``put_all_and_emit``
        makes one transaction - a failure leaves none of them, so no migration starts released
        while its decision is unwritten, and the retry decides again (SDD §14.6, reviews ST-07,
        RV48-01, RV48-02, RV48-13)."""
        m, v = await self._load(mid)
        if m.advisor_hold is not None:
            # SDD §14.6: the hold names the one to decide - written with it, so whatever its time
            # after the clock stepped back; another process's validation may have held it for a
            # newer one (reviews RV49-04, RV50-09)
            if m.advisor_hold != rec_id:
                raise NotAllowed(f"a newer recommendation ({m.advisor_hold}) replaced {rec_id}")
        else:
            # the one the last validation stored - written with it, so whatever its time after a
            # clock step back (review RV51-08); one no validation stored since the field: the
            # newest by its time
            newest = m.last_recommendation
            if newest is None:
                stored = await self.db.list(
                    "recommendation", AdvisorRecommendation, migration_id=mid
                )
                newest = max(stored, key=lambda r: (r.created_at, r.id)).id
            if newest != rec_id:
                raise NotAllowed(f"a newer recommendation ({newest}) replaced {rec_id}")
        rec, rec_version = await self.db.get_versioned(
            "recommendation", rec_id, AdvisorRecommendation
        )
        plan, plan_version = await self.db.get_versioned("plan", plan_id, Plan)
        policies = await self.db.list("advisor_policy", AdvisorPolicy)
        policy = resolve_policy(
            policies, plan.id, plan.source_provider_id, rec.decision_type, self.settings
        )
        # SDD §14.6: the rules of the policy that took the decision, as they were then; the
        # policy in force only for a decision that recorded none (the fallback, older data) - and
        # the decision names the policy whose rules it follows (audit AD-06)
        distinct = rec.decision.require_distinct_approver
        if distinct is None:
            distinct = policy.require_distinct_approver
        reason_required = rec.decision.require_override_reason
        if reason_required is None:
            reason_required = policy.require_override_reason
        recorded_rules = (
            rec.decision.require_distinct_approver is not None
            or rec.decision.require_override_reason is not None
        )
        ruling = (rec.decision.policy_id if recorded_rules else None) or policy.id
        if distinct and rec.requested_by == actor:
            raise NotAllowed(OWN_DECISION)  # maker-checker (SDD §14.6, ADV-17)
        if isinstance(action, Override) and reason_required:
            if len(action.reason or "") < 10:
                raise Unprocessable(
                    f"policy {ruling} requires an override reason of at least 10 characters"
                )
        if m.phase not in REVALIDATABLE:
            raise NotAllowed(f"the strategy cannot change in {m.phase}")
        pinned = plan.strategy_overrides.get(m.vm.source_id)
        if pinned is None and plan.default_strategy != "auto":
            pinned = plan.default_strategy  # a validation honours it as an override (audit AD-04)
        if pinned is not None:
            # SDD §14.6: a person set the strategy in the plan, which a validation honours
            # without a recommendation - the newest one no longer stands, whatever option a
            # decision would choose (reviews A4, RV48-03)
            raise NotAllowed(
                f"The plan sets the strategy of {m.vm.name} ({pinned}): change it in the plan."
            )
        if isinstance(action, Approve) and rec.decision.status == DecisionStatus.approved:
            raise NotAllowed("The decision is already approved.")  # one event per change
        if isinstance(action, Approve) and rec.recommendation.unusable:
            # SDD §14.6 (audit AD-03): an answer that could not be applied stays unapplied - an
            # override to its option is a person's call, with its reason
            raise NotAllowed(f"nothing to approve: {rec.recommendation.unusable}")
        eligible = tuple(e.strategy.value for e in m.estimates if e.eligible)
        handover = Strategy.storage_handover.value
        chosen = action.option if isinstance(action, Override) else rec.recommendation.option
        held = m.estimate_for(Strategy.storage_handover)
        if (
            chosen == handover
            and held is not None
            and (
                HELD_VOLUMES_REASON in held.reasons  # a Retry or a validation saw them held
                or (held.eligible and await self.holds_destination_volumes(m))
            )
        ):
            # §9.2 judged now, as PUT …/strategy: a Retry does not re-assess the estimates
            # the last validation stored, and a handover would leave the held volumes behind
            # (SDD §14.6, review RV41B-01)
            refusal = f"strategy {handover} is not eligible: {HELD_VOLUMES_REASON}"
            if isinstance(action, Override):
                raise Unprocessable(refusal)
            raise NotAllowed(f"nothing to approve: {refusal}")
        advice = Advice(
            option=rec.recommendation.option,
            confidence=rec.recommendation.confidence,
            scope=eligible,
            options=eligible,
        )
        try:
            decision = decide(advice, policy, 0, m.strategy.value, action=action, now=self._now())
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
                "policy_id": ruling,
            }
        )
        rec = rec.model_copy(update={"decision": decision})
        final = Strategy(str(decision.final_option))
        now = self._now()
        writes: list[tuple[str, Any, int]] = []
        events: list[Event] = []
        if isinstance(action, Override):
            # under the plan's lock, with the decision - a validation waiting for the lock
            # honours the override instead of asking the advisor again
            plan.strategy_overrides[m.vm.source_id] = final
            plan.updated_at = now
            writes.append(("plan", plan, plan_version))
            events.append(
                new_event(
                    "plan.updated",
                    f"{plan.name}: strategy override for {m.vm.name}",
                    plan_id=plan.id,
                    actor=actor,
                    data={"strategy_overrides": {m.vm.source_id: str(final)}},
                )
            )
        changed = final != m.strategy
        m.strategy = final
        m.estimate = m.estimate_for(final)
        m.advisor_hold = None
        if changed:  # the approval was given for the previous strategy (SDD §5.4)
            m.approvals = []
            _clear_request(m)
        m.updated_at = now
        writes += [("migration", m, v), ("recommendation", rec, rec_version)]
        kind, message, by, data = decision_event(rec, before)
        events.append(
            new_event(kind, message, plan_id=m.plan_id, migration_id=m.id, actor=by, data=data)
        )
        if changed:
            events.append(
                new_event(
                    "migration.action",
                    f"{m.vm.name}: strategy set to {final} by the advisor decision",
                    plan_id=m.plan_id,
                    migration_id=m.id,
                    actor=actor,
                    data={"action": "strategy", "strategy": str(final)},
                )
            )
        return rec, writes, events

    # ------------------------------------------------------------------------------------------
    # lifecycle and tick loop
    async def start(self) -> None:
        if self._tick_task is not None and not self._tick_task.done():
            return
        # SDD §8: a takeover's start begins as a new process would
        self._forget()
        self._stopping = False
        self._wake = asyncio.Event()
        migrations = await self.db.list("migration", Migration)
        for m in migrations:
            if m.phase in fsm.RESUMABLE_PHASES:
                self._launch(m.id)
            elif cleanup_pending(m) and m.id not in self._cleanups:
                # a cleanup a stop or a crash cut short runs again (SDD §5.1)
                self._cleanups[m.id] = asyncio.create_task(
                    self._cleanup_after_cancel(m, None, "system"), name=f"cleanup:{m.id}"
                )
        # nothing counts as pre-staged after a restart: a plan is pre-staged again before its next
        # ready migration starts (SDD §8, review RV52-O2)
        self._tick_task = asyncio.create_task(self._loop(), name="seamless-orchestrator")
        self._started = True

    def _forget(self) -> None:
        """What a new process would not know (SDD §8): a replica that leads again starts as a
        restart does - nothing pre-staged, no GPU read or cutover begun, no driver crash, no wave
        event seen, a tick health of its own."""
        self._prestage_done.clear()
        self._prestage_retry_at.clear()
        self._gpu_outcomes.clear()
        self._cutovers_begun.clear()
        self._driver_crashes.clear()
        self._wave_events_seen.clear()
        self._progress_at.clear()
        self._source_watch_due = None
        self._last_tick_at = None
        self._tick_errors = 0

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
                *self._gpu_reads.values(),
                *self._drivers.values(),
                *self._prestage_tasks.values(),
                *self._steps.values(),
                *self._cleanups.values(),
                *self._lookups,
            )
            if t
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tick_task = None
        self._watch_task = None
        self._gpu_reads.clear()
        self._gpu_read_destinations.clear()
        self._started = False
        self._drivers.clear()
        self._steps.clear()
        self._prestage_tasks.clear()
        self._cleanups.clear()
        self._lookups.clear()

    async def _loop(self) -> None:
        assert self._wake is not None
        while not self._stopping:
            started = self._monotonic()
            self._last_tick_at = started  # heartbeat: the loop is alive even during a long tick
            try:
                await self.tick()
                # SDD §12: the streams send what another process (the CLI) wrote to the store
                await self.bus.catch_up()
                self._tick_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._tick_errors += 1
                log.exception("orchestrator tick failed")
            self._record_tick(self._monotonic() - started)
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
        age = None if self._last_tick_at is None else self._monotonic() - self._last_tick_at
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
        self._last_tick_at = self._monotonic()
        stats["count"] += 1
        stats["sum"] += seconds
        stats["last"] = seconds
        stats["max"] = max(stats["max"], seconds)
        budget = self.settings.tick_s / 2
        if seconds > budget:
            stats["slow"] += 1
            log.warning("orchestrator tick took %.3f s (budget %.3f s)", seconds, budget)

    async def tick(self) -> None:
        if not self._leads():
            return  # SDD §8: a replica past its renew deadline runs no tick; its stop follows
        await self._resume_orphans()
        self._start_source_watch()
        all_plans = await self.db.list("plan", Plan)
        completed = [p.id for p in all_plans if p.status == PlanStatus.completed]
        if completed:
            # a retry whose reopen a store error kept from its completed plan left the attempt
            # ready there: reopened, or it would wait for good (SDD §8, review RV51-14)
            phases = await self.db.plan_phases(completed)
            for plan_id in sorted(p for p, counts in phases.items() if counts.get(P.ready)):
                if _lock_busy(self.plan_lock(plan_id)):
                    # a validation or an edit holds or waits for it: a later tick reopens it,
                    # never this one waiting while every plan's gates stall (SDD §8, review
                    # RV52-O1, PR #58 review)
                    continue
                try:
                    ready = [
                        m.id
                        for m in await self.db.list("migration", Migration, plan_id=plan_id)
                        if m.phase == P.ready
                    ]
                    if _lock_busy(self.plan_lock(plan_id)) or any(
                        self._retrying[mid] or _lock_busy(self._lock(mid)) for mid in ready
                    ):
                        # a retry is still writing its migration: its own reopen names its actor
                        # (SDD §5.1, §8, PR #58 CI); a later tick reopens it if a store error
                        # kept that from it
                        continue
                    await self._reopen_completed(plan_id, "system", "a migration waits in ready")
                except Exception:
                    log.exception("the reopen of plan %s failed", plan_id)
        plans = [p for p in all_plans if p.status == PlanStatus.running]
        if not plans:
            return
        # only the migrations of plans that can still have work: running ones, plus paused and
        # re-validated ones whose in-flight steps still count against the concurrency limits
        live = (PlanStatus.running, PlanStatus.paused, PlanStatus.validated)
        active = {p.id for p in all_plans if p.status in live}
        migrations = [m for m in await self._migrations.get() if m.plan_id in active]
        # a GPU capacity read's outcome lives while its migration waits at its gate (SDD §22.7)
        gated = {m.id for m in migrations if m.phase in fsm.CUTOVER_WAIT_PHASES}
        for mid in [mid for mid in self._gpu_outcomes if mid not in gated]:
            del self._gpu_outcomes[mid]
        by_plan: dict[str, list[Migration]] = defaultdict(list)
        for m in migrations:
            by_plan[m.plan_id].append(m)
        counts = {
            "steps": sum(1 for m in migrations if m.phase in fsm.ACTIVE_STEP_PHASES),
            "cutover": sum(1 for m in migrations if m.phase == P.cutover),
        }
        self._tick_holds = Counter()  # no place is held yet in this tick (SDD §22.7)
        now = self._now()
        for plan in plans:
            # each plan on its own: one plan's error - a write the store refuses, a provider down -
            # is logged and the plans after it are ticked (SDD §8, review RV51-06)
            try:
                await self._tick_plan(plan, by_plan.get(plan.id, []), counts, now)
            except Exception:
                log.exception("the tick of plan %s failed", plan.id)

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
        covered = self._prestage_done.get(plan.id)
        uncovered = {
            m.id
            for m in migs
            if m.phase == P.ready and (covered is None or _assessment(m) not in covered)
        }
        if uncovered:
            # a ready migration no pre-staging covered - the plan's first, a VM added, validated
            # again or given a strategy that needs a conversion host - waits for one; a plan with
            # nothing ready is not pre-staged (SDD §8, review RV52-O3). Only the uncovered ones
            # wait: the plan's gates and passes go on meanwhile (PR #58 review)
            if time.monotonic() >= self._prestage_retry_at.get(plan.id, 0.0):
                self._ensure_prestage(plan, migs)
            migs = [m for m in migs if m.id not in uncovered]
        # two migrations of one VM, as an earlier version left a running plan: neither starts
        # (SDD §5.4, review RV51-18)
        twice = _vms_named_twice(migs)
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

        def wave_room(m: Migration, held: int = 0) -> bool:
            wave = waves.get(m.wave_id or "")
            return wave is None or in_flight[m.wave_id] + held < wave.max_parallel

        limit_steps = self.settings.max_concurrent_migrations
        ordered = sorted(
            (m for m in migs if is_active(m) and m.vm.source_id not in twice),
            key=lambda m: (waves[m.wave_id].order if m.wave_id in waves else 0, m.created_at),
        )
        gate_open: set[str] = set()

        # the places the destination's GPU VMs hold in this tick (SDD §22.7): each one a cutover
        # slot, a step and its wave's room, kept from the migrations of its destination behind it
        destination = plan.destination_provider_id
        holds = self._tick_holds
        held_by_earlier_plans = holds[destination]
        held_at: list[tuple[int, str | None]] = []  # this plan's: index in ordered, and wave

        def hold(i: int, m: Migration) -> None:
            holds[destination] += 1
            held_at.append((i, m.wave_id))

        def held_before(i: int) -> int:
            return held_by_earlier_plans + sum(1 for j, _ in held_at if j < i)

        def wave_held_before(i: int, wave_id: str | None) -> int:
            return sum(1 for j, wave in held_at if j < i and wave == wave_id)

        # 1. cutover gate (SDD §5.4)
        for i, m in enumerate(ordered):
            waiting = m.phase == P.awaiting_cutover or (
                m.phase == P.ready and m.strategy in SINGLE_SHOT_STRATEGIES
            )
            if not waiting or not self._gate(m, plan, now):
                continue
            held = held_before(i)
            if counts["cutover"] + held >= self.settings.max_concurrent_cutovers:
                continue  # waits for a cutover slot: keep-warm passes go on meanwhile
            if counts["steps"] + held >= limit_steps or (
                m.phase == P.ready and not wave_room(m, wave_held_before(i, m.wave_id))
            ):
                continue
            if m.advisor_hold is not None:
                continue  # its advisor decision waits for approval: no read, no step (§14.6)
            gpu = bool(vm_gpus(m.vm))
            if gpu:
                place = self._gpu_place(m, now, destination)
                if place != "go":
                    # it waits for GPU capacity, or for a read of it - holding its place while the
                    # read is out (SDD §5.4 rule 5, §22.7)
                    if place == "hold":
                        hold(i, m)
                    continue
            if await self._begin(
                m.id, {P.ready, P.awaiting_cutover}, P.cutover, "cutover gate open"
            ):
                gate_open.add(m.id)
                counts["cutover"] += 1
                counts["steps"] += 1
                in_flight[m.wave_id] += 1
            elif gpu and self._gpu_place(m, self._now(), destination) != "wait":
                # its read still counts, or was dropped and is read again at once: its place stays
                hold(i, m)

        # 2. keep-warm delta passes while waiting for the gate (or for a cutover slot)
        for i, m in enumerate(ordered):
            if m.phase != P.awaiting_cutover or m.id in gate_open or not m.sync_passes:
                continue
            ended = m.sync_passes[-1].ended_at or m.sync_passes[-1].started_at
            if (now - ended).total_seconds() < plan.keep_warm_interval_s:
                continue
            if counts["steps"] + held_before(i) >= limit_steps:
                break
            if await self._begin(m.id, {P.awaiting_cutover}, P.syncing, "keep-warm delta pass"):
                counts["steps"] += 1
                in_flight[m.wave_id] += 1

        # 3. start pre-copy of warm migrations
        for i, m in enumerate(ordered):
            if m.phase != P.ready or m.strategy not in WARM_STRATEGIES:
                continue
            if counts["steps"] + held_before(i) >= limit_steps:
                break
            if not wave_room(m, wave_held_before(i, m.wave_id)):
                continue
            if await self._begin(m.id, {P.ready}, P.precopy, "pre-copy started"):
                counts["steps"] += 1
                in_flight[m.wave_id] += 1

        # 4. plan completion (an uncovered ready migration is not complete either)
        if not uncovered and all(m.phase in fsm.WAVE_COMPLETE_PHASES for m in migs):
            await self._complete(plan)

    async def _complete(self, plan: Plan) -> None:
        # SDD §8: under the plan's lock, on the migrations re-read there — a retry that returned
        # after the tick's snapshot keeps the plan running (SDD §5.1); one held or waited for is
        # left to a later tick, which never waits for a lock (PR #58 review)
        lock = self._lock(f"plan:{plan.id}")
        if _lock_busy(lock):
            return
        async with lock:
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
        # that returned starts nothing the tick chose on its snapshot. A lock held or waited for
        # - a pause, a validation, a person's action - leaves the step to a later tick, which
        # never waits for one: every other plan would stall (PR #58 review)
        plan_lock, mig_lock = self._lock(f"plan:{plan_id}"), self._lock(mid)
        if _lock_busy(plan_lock) or _lock_busy(mig_lock):
            return False
        async with plan_lock, mig_lock:
            plan = await self._plan(plan_id)
            if plan.status != PlanStatus.running:
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
            if m.phase == P.ready and _assessment(m) not in self._prestage_done.get(plan_id, ()):
                # assessed anew since the tick's snapshot (set_strategy): the tick pre-stages it
                # first, so no cutover starts without its conversion host (SDD §8, RV52-O4)
                self.wake()
                return False
            if to == P.cutover and not self._gate(m, await self._plan(m.plan_id), self._now()):
                # the tick saw the gate open on its snapshot; an action under this lock (e.g.
                # set_strategy clearing the approvals) may have closed it since (SDD §5.4)
                return False
            if to == P.cutover and not await self._gpu_room_now(m):
                # its GPU capacity read no longer counts, or a cutover it did not count started
                # since: the tick reads again (SDD §5.4 rule 5, §22.7)
                return False
            await self._transition(m, v, to, reason)
            if to == P.cutover:
                self._gpu_outcomes.pop(mid, None)  # one read, one cutover: a retry reads anew
                self._cutover_begun(plan.destination_provider_id, mid)
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
            # SDD §8, §25.4 layer 2: a pre-staging is a step's attempt
            await self._hold_the_lease(f"the pre-staging of {plan.name}")
            source = await self.db.get("provider", plan.source_provider_id, Provider)
            destination = await self.db.get("provider", plan.destination_provider_id, Provider)
            executor = self.executors.prestage_executor(source.kind)
            if executor is not None:
                kwargs: dict[str, Any] = {}
                if "deploy_conversion_hosts" in inspect.signature(executor.prestage).parameters:
                    kwargs["deploy_conversion_hosts"] = any(
                        m.strategy in (Strategy.cold, Strategy.warm) for m in migs
                    )
                params = inspect.signature(executor.prestage).parameters
                scoped = {"flavors": "prestage_flavors", "keypairs": "prestage_keypairs"}
                if any(r in plan.prestage_resources and scoped[r] in params for r in scoped):
                    # only the plan's flavors RHOSO lacks (a destination flavor is shared by every
                    # project and never pre-staged over) and the keypairs the destination user
                    # lacks and the source user owns (SDD §7.2)
                    # read with the plan's credentials (SDD §7.2, §27.5)
                    src_inv = await for_step(
                        self.providers, Step.INVENTORY, source, plan
                    ).inventory()
                    dst_inv = await for_step(
                        self.providers, Step.INVENTORY, destination, plan
                    ).inventory()
                    if "flavors" in plan.prestage_resources and "prestage_flavors" in params:
                        kwargs["prestage_flavors"] = sorted(
                            {
                                m.vm.flavor
                                for m in migs
                                if m.vm.flavor and flavor_prestaged(m.vm, plan, dst_inv)
                            }
                        )
                    if "keypairs" in plan.prestage_resources and "prestage_keypairs" in params:
                        kwargs["prestage_keypairs"] = sorted(
                            {
                                m.vm.key_name
                                for m in migs
                                if m.vm.key_name and keypair_prestaged(m.vm, plan, src_inv, dst_inv)
                            }
                        )
                if kwargs.get("deploy_conversion_hosts") and "rekey_conversion_hosts" in params:
                    # one conversion key per installation: adopt the key the clouds' keypairs
                    # hold, name the sides to re-key, or refuse before any playbook runs (SDD
                    # §7.2 "Conversion key", issue #112)
                    kwargs["rekey_conversion_hosts"] = await resolve_conversion_key(
                        self.settings,
                        source,
                        destination,
                        # the keypair of the user of the plan's credential on each side (§27.5)
                        lambda provider: for_step(self.providers, Step.PRESTAGE, provider, plan),
                    )
                if "host_keys" in params:
                    # what the pre-staging pins and refuses of its conversion hosts (SDD §7.2)
                    kwargs["host_keys"] = self.host_keys(plan.id)
                await executor.prestage(plan, source, destination, **kwargs)
            # SDD §8.3: a keep_address plan reserves its floating IPs' addresses at the destination
            if plan.network_cutover.floating_ips == "keep_address":
                await self._reserve_floating_ips(migs, plan, destination)
            # what it read when it began: a migration assessed meanwhile waits for the next one
            self._prestage_done.setdefault(plan.id, set()).update(_assessment(m) for m in migs)
            self._prestage_retry_at.pop(plan.id, None)
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
            in_flight = False

            def fail(fresh: Plan) -> bool:
                nonlocal failed
                # re-evaluated on every retry; a plan with work in flight keeps running
                failed = fresh.status == PlanStatus.running and not in_flight
                if not failed:
                    return False
                fresh.status = PlanStatus.failed
                return True

            # SDD §8: a status writer like the others, under the plan's lock; only a plan that
            # still runs fails. One paused, validated again or edited meanwhile keeps its status
            # (an edited draft must not become a failed plan that start_plan accepts unvalidated)
            # and pre-stages again at its next start. One with a migration in flight - a step
            # running, or one waiting at its cutover gate - keeps running too: those go on, and
            # the tick pre-stages it again after PRESTAGE_RETRY_S (PR #58 review)
            async with self._lock(f"plan:{plan.id}"):
                in_flight = any(
                    m.phase in fsm.ACTIVE_STEP_PHASES or m.phase == P.awaiting_cutover
                    for m in await self.db.list("migration", Migration, plan_id=plan.id)
                )
                if in_flight:
                    self._prestage_retry_at[plan.id] = time.monotonic() + PRESTAGE_RETRY_S
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
            try:
                driver, step = self._drivers.get(m.id), self._steps.get(m.id)
                if (driver is not None and not driver.done()) or (
                    step is not None and not step.done()
                ):
                    continue
                crashes, at = self._driver_crashes.get(m.id, (0, 0.0))
                # the exponent capped first: 2**1024 is past a float (audit OC-01)
                backoff = min(300.0, 2 ** min(crashes, 64) * self.settings.tick_s)
                if crashes and now - at < backoff:
                    continue
                self._launch(m.id)
            except Exception:
                # one migration's relaunch never stops the tick (SDD §8, audit OC-01)
                log.exception("could not relaunch the driver of %s", m.id)

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
                # the step returned: a later crash starts a new row of crashes, so its relaunch
                # backs off from the start (SDD §8, review RV51-16)
                self._driver_crashes.pop(mid, None)
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

        async def mark_downtime_end() -> None:
            if not locked:
                try:
                    await outlive_cancel(self._close_clock(mid, plan))
                except Exception as exc:
                    # the source runs: a store error saving the close is retried with the
                    # attempt, which closes the clock again (SDD §5.2, review RV36B-03)
                    raise TransientStepError(f"close the downtime clock: {exc}") from exc

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
            mark_downtime_end=mark_downtime_end,
            log=log_line,
            options=dict(options or {}),
            # what the step pins and refuses of its conversion hosts (SDD §7.2)
            host_keys=self.host_keys(plan.id, mid),
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
        # a cancel of the step (the step timeout, a rollback request, a shutdown) waits for the
        # clock and its event: one that split them left an open clock unannounced - the clock is
        # saved first, so a failed event never leaves a stopped source without it (SDD §5,
        # review RV26-06)
        await outlive_cancel(self._open_clock(mid, at))

    async def _open_clock(self, mid: str, at: datetime | None) -> None:
        async with self._lock(mid):
            m, v = await self._load(mid)
            if fsm.source_stopped(m):
                return  # open: the first stop's time stays
            # none yet - or closed by the step that found the source running again and stops it
            # once more: a new outage (SDD §5.2, review RV33-04)
            m.downtime_started_at = at or self._now()
            m.downtime_ended_at = None
            m.actual_downtime_s = None
            m.downtime_model_s = None
            await self._save(m, v)
        await self._emit(
            "migration.downtime_started",
            f"{m.vm.name}: source VM stopped",
            migration=m,
            data={"at": m.downtime_started_at.isoformat()},
        )

    async def _close_clock(self, mid: str, plan: Plan) -> None:
        """The source runs again inside a step - a rollback's power-on: the clock closes now, so
        an attempt that fails after it leaves the source counted as running (SDD §5.2, review
        RV30-03). The close and its event are one store transaction, as a rollback's end writes
        them: a store error leaves neither, and the retry closes and announces it (review
        RV37B-01). A closed clock stays as it is."""
        async with self._lock(mid):
            m, v = await self._load(mid)
            if not fsm.source_stopped(m):
                return
            now = self._now()
            m.downtime_ended_at = now
            m.actual_downtime_s = self._rollback_downtime_s(m, now, plan)
            await self._save_with_event(
                m,
                v,
                "migration.downtime_ended",
                f"{m.vm.name}: source running again after {m.actual_downtime_s:.0f} s",
                {"actual_downtime_s": m.actual_downtime_s, "cause": "source_started"},
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
                # SDD §8, §25.4 layer 2: no attempt starts unless the lease holds
                await self._hold_the_lease(f"{step} of {current.vm.name}")
                # each attempt reads the providers anew: a retry after a credential rotation
                # signs in with the new credentials (SDD §8, §13.3, review RV26-07)
                source = await self.db.get("provider", plan.source_provider_id, Provider)
                destination = await self.db.get("provider", plan.destination_provider_id, Provider)
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
        if m.observed_extent_query_s:
            params = replace(params, extent_query_s=float(m.observed_extent_query_s))
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
            # the migration's current S: a pass the link bound may only raise it (SDD §9.1,
            # review RV38A-02)
            observed = observed_scan_rate(sync_pass, m.vm, self._params(m, plan))
            if observed is not None:
                m.observed_scan_bps = observed
            # a disk the pass scanned for a lasting reason leaves the tracked ones, and a checked
            # pass's longest query is the migration's q (SDD §9.1, §28.6)
            m.tracked_disks = tracked_disks(m.vm, m.tracked_disks, [sync_pass])
            query = observed_extent_query_s(sync_pass)
            if query is not None:
                m.observed_extent_query_s = query
        if sync_pass.kind == SyncPassKind.final:
            return  # the estimate stays the one the convergence rule used (SDD §5.3, ADV-16)
        # the hooks' part and the DNS tail as validation gave them (SDD §9.1)
        fresh = estimate(
            m.vm,
            m.strategy,
            self._params(m, plan),
            plan.downtime_slo_s,
            hooks_s=hooks_part(m.estimate),
            dns_tail_s=m.estimate.dns_tail_s if m.estimate is not None else None,
            tracked=m.tracked_disks,
        )
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
        params = self._params(m, plan)
        # after a pass as long as the last one's whole time (SDD §9.1 T_last, review RV39-07),
        # the post_create hooks' part of the estimate included (§9.1)
        final = estimate_final_downtime(
            m.vm,
            m.strategy,
            whole_pass_seconds(last, m.strategy, params),
            params,
            hooks_part(m.estimate),
            m.tracked_disks,
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
        params = self._params(m, plan)
        parts = final_downtime_parts(
            m.vm,
            m.strategy,
            whole_pass_seconds(last, m.strategy, params),
            params,
            hooks_part(m.estimate),
            m.tracked_disks,
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
            if result.cutover_refusals is not None:
                # SDD §7.2: what this pass met that the cutover refuses, saved with the pass
                cur.cutover_refusals = list(result.cutover_refusals)
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
        # SDD §8.2: pre_stop while the source runs - never again once the attempt's clock is open
        if m.downtime_started_at is None:
            # SDD §8.5: the member helper's drain first, then the point's hooks
            drained = await self._drain_lb_members(m)
            if drained is not None:
                await self._fail(m.id, P.cutover, "lb_member", drained)
                return
            failed = await self._run_hooks(m, "pre_stop")
            if failed is not None:
                await self._fail(m.id, P.cutover, "hook:pre_stop", HookFailed(failed))
                return
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
            # SDD §7.2: the cutover got past what the passes said it would refuse
            cur.cutover_refusals = []
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
        # SDD §8.3: the floating IPs move first - the server exists, the clock is open
        moved = await self._move_floating_ips(m)
        if moved is not None:
            await self._fail(m.id, P.verifying, "floating_ips", moved)
            return
        # SDD §8.4: then the DNS record, before the post_create hooks
        swapped = await self._swap_dns(m)
        if swapped is not None:
            await self._fail(m.id, P.verifying, "dns", swapped)
            return
        # SDD §8.2: post_create before the verification - the server exists, the clock is open
        failed = await self._run_hooks(m, "post_create")
        if failed is not None:
            await self._fail(m.id, P.verifying, "hook:post_create", HookFailed(failed))
            return
        plan = await self._plan(m.plan_id)
        destination = await self.db.get("provider", plan.destination_provider_id, Provider)
        try:
            # the plan's destination credential; its allocation read, Placement's, the
            # destination provider's own (SDD §7.5, §22.8, §27.5)
            view = verification_view(self.providers, destination, plan)
        except ProviderError as exc:
            await self._fail(m.id, P.verifying, "verify", exc)
            return
        verifier = self._verifier_factory(view)
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
        # SDD §5.2 (audit OC-08): the verification ends when its step returns - the advisor's
        # review below, Jev's screen and verify, is no outage
        verified_at = self._now()
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
        if result.passed:
            # SDD §8.5: the member helper's join first, then the point's hooks
            joined = await self._join_lb_members(m)
            if joined is not None:
                await self._fail(m.id, P.verifying, "lb_member", joined)
                return
            # SDD §8.2: post_verify once the verification passed - the downtime ended with it
            failed = await self._run_hooks(m, "post_verify")
            if failed is not None:
                await self._fail(m.id, P.verifying, "hook:post_verify", HookFailed(failed))
                return
        summary = result.summary()
        async with self._lock(m.id):
            cur, v = await self._load(m.id)
            if cur.phase != P.verifying:
                return
            # the checks and probe results, passed or failed (SDD §7.5)
            cur.verification = result.record(verified_at)
            if rules_reason:
                cur.review_required = True
                cur.review_reason = rules_reason
            if note is not None:
                cur.advisor_notes.append(note)
                if note.data.get("review_required"):
                    cur.review_required = True
                    cur.review_reason = note.data.get("review_reason")
            if result.passed:
                # SDD §5.2: a verification run again (a stop or a failed event write came before
                # completed) keeps the end the first one recorded: the outage ended then
                if cur.downtime_started_at is not None and cur.downtime_ended_at is None:
                    cur.downtime_ended_at = verified_at
                    cur.actual_downtime_s = self._downtime_s(cur, verified_at, plan)
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
                        data={"actual_downtime_s": cur.actual_downtime_s, "cause": "verified"},
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
        # SDD §8.4: the DNS record goes back first; a failure leaves the source stopped
        reverted = await self._revert_dns(m)
        if reverted is not None:
            await self._fail(m.id, P.rolling_back, "dns", reverted)
            return
        # SDD §8.3: then the floating IPs; a failure leaves the source stopped
        returned = await self._return_floating_ips(m)
        if returned is not None:
            await self._fail(m.id, P.rolling_back, "floating_ips", returned)
            return
        # SDD §8.5: then the Octavia memberships - a failure is recorded, the rollback goes on
        await self._restore_lb_members(m)
        # SDD §8.2: the reverts of the attempt's hooks, then on_rollback, before the source starts
        await self._run_rollback_hooks(m)
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
            # back - rolled_back is never resumed, so a stop after it would lose the rewrite
            await self._follow_recreated_vm(m.id, recreated)
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
            if ended:
                # the close and its event in one store transaction: a store error leaves neither,
                # and the resumed rollback closes the clock and announces it (SDD §5.2, review
                # RV37B-01)
                v = await self._save_with_event(
                    cur,
                    v,
                    "migration.downtime_ended",
                    f"{cur.vm.name}: source running again after {cur.actual_downtime_s:.0f} s",
                    {"actual_downtime_s": cur.actual_downtime_s, "cause": "source_started"},
                )
            else:
                v = await self._save(cur, v)
            cur, v = await self._transition(
                cur, v, P.rolled_back, "rollback finished; the source VM runs again"
            )
        if self.knowledge is not None:
            await self.knowledge.on_rolled_back(cur, reason)
        self.wake()

    async def _follow_recreated_vm(self, mid: str, recreated: VMRef) -> None:
        """The plan follows a recreated source VM in one transaction with the migration's new
        ``vm`` (SDD §7.3, §11, audit OC-06): a stop before it leaves both naming the old VM, after
        it both the new one - never a plan naming a VM no migration does, which a validation would
        plan a second migration of. A plan that already names it (an earlier version's rewrite,
        left by a stop) is written no further."""
        for attempt in range(PLAN_UPDATE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_update_backoff(attempt))
            async with self._lock(mid):
                cur, v = await self._load(mid)
                if cur.phase != P.rolling_back:
                    return
                plan, plan_version = await self.db.get_versioned("plan", cur.plan_id, Plan)
                old, new = cur.vm.source_id, recreated.source_id
                cur.vm = recreated
                cur.updated_at = self._now()
                writes: list[tuple[str, Any, int]] = [("migration", cur, v)]
                events: list[Event] = []
                if _rewrite_vm_id(plan, old, new):
                    plan.updated_at = self._now()
                    writes.insert(0, ("plan", plan, plan_version))
                    events.append(
                        new_event(
                            "plan.updated",
                            f"{plan.name}: source VM {old} is now {new}",
                            plan_id=plan.id,
                            data={"vm_id_changed": {"from": old, "to": new}},
                        )
                    )
                try:
                    await put_all_and_emit(self.store, self.bus, writes, events)
                    return
                except ConflictError:
                    continue  # another writer moved the plan: read both again
        raise ConflictError(f"plan of {mid} changed too often to follow its recreated source VM")

    async def _run_hooks(
        self,
        m: Migration,
        point: HookPoint,
        names: Sequence[str] | None = None,
        *,
        keep_going: bool = False,
    ) -> HookRun | None:
        """Run the plan's hooks at ``point`` - or ``names``, at it - one after the other, each
        recorded as its migration.action (SDD §7.2, §8.2); the first failure ends the point and is
        returned, unless ``keep_going``."""
        plan = await self._plan(m.plan_id)
        wanted = list(plan.network_cutover.hooks.get(point, []) if names is None else names)
        if not wanted:
            return None
        source = await self.db.get("provider", plan.source_provider_id, Provider)
        destination = await self.db.get("provider", plan.destination_provider_id, Provider)
        context = context_document(m, plan, point, source, destination)

        async def log_line(line: str) -> None:
            text = redact_cut(line, LOG_LINE_CHARS)
            await self._emit("migration.log", text, persist=False, migration=m)

        first: HookRun | None = None
        for name in wanted:
            try:
                hook = await self.db.get("hook", name, Hook)
            except NotFound:
                run = HookRun(
                    hook=name,
                    kind="unknown",
                    point=point,
                    idempotency_key=str(context["idempotency_key"]),
                    outcome="failed",
                    duration_s=0.0,
                    detail="no such hook in the catalog",
                )
            else:
                # SDD §8, §25.4 layer 2: no hook runs unless the lease holds
                await self._hold_the_lease(f"hook {name} at {point} of {m.vm.name}")
                run = await self.hook_runner.run(
                    hook, context, run_dir(self.settings, plan.id, m.id), log_line
                )
                if run.outcome == "succeeded" and hook.revert is not None and point in _REVERTED:
                    # SDD §8.2: what a rollback owes, saved before the run's record
                    await self._owe_revert(m.id, hook.revert)
            event = hook_action_event(m, run)
            await self._emit(
                event.kind, event.message, migration=m, actor=event.actor, data=event.data
            )
            if run.outcome == "failed" and first is None:
                first = run
                if not keep_going:
                    break
        return first

    async def _fip_action(
        self, m: Migration, address: str, step: str, error: BaseException | None = None
    ) -> None:
        """A floating IP step's migration.action (SDD §8.3); a failure's detail redacted."""
        outcome = "succeeded" if error is None else "failed"
        detail = None if error is None else redact_cut(str(error) or type(error).__name__, 500)
        await self._emit(
            "migration.action",
            f"{m.vm.name}: floating IP {address}: {step} {outcome}",
            migration=m,
            actor="orchestrator",
            data={
                "action": "floating_ip",
                "address": address,
                "step": step,
                "outcome": outcome,
                "detail": detail,
            },
        )

    async def _reserve(self, plan: Plan, destination: Provider, address: str) -> None:
        """One keep-address reservation (SDD §8.3, §27.5, §27.6): the plan's destination credential
        when it may choose an address, else the destination provider's own - its admin - for the
        plan's destination project (``project_id``); a plan without a destination project, the
        provider's own as before."""
        acting, for_project = reservation(plan, destination)
        dst = implementation(self.providers, destination, acting)
        if for_project is None:
            await dst.reserve_floating_ip(address)
        else:
            await dst.reserve_floating_ip(address, project_id=for_project)

    async def _reserve_floating_ips(
        self, migs: list[Migration], plan: Plan, destination: Provider
    ) -> None:
        """The keep-address move's reservations with a plan's pre-staging (SDD §8.3): a failure
        fails the pre-staging, before any source stops."""
        for m in migs:
            for address in _floating_addresses(m.vm):
                try:
                    await self._reserve(plan, destination, address)
                except Exception as exc:
                    await self._fip_action(m, address, "reserve", exc)
                    raise
                await self._fip_action(m, address, "reserve")

    async def _journal(self, mid: str, address: str, **update: Any) -> FipMove:
        """Save the floating IP's FipMove of the migration with ``update``, a new one when it has
        none (SDD §4.2, §8.3); the entry as saved."""
        async with self._lock(mid):
            cur, v = await self._load(mid)
            entry = _journal_entry(cur, address)
            if entry is None:
                entry = FipMove(address=address, **update)
                cur.fip_moves.append(entry)
            else:
                entry = entry.model_copy(update=update)
                cur.fip_moves = [entry if e.address == address else e for e in cur.fip_moves]
            await self._save(cur, v)
            return entry

    async def _move_floating_ips(self, m: Migration) -> BaseException | None:
        """SDD §8.3: each floating IP of the VM leaves the stopped source - the port it was on
        journaled first - and lands on the new server's port holding the NIC's address; a restart
        resumes from the journal. The failure, None when every one moved."""
        plan = await self._plan(m.plan_id)
        if plan.network_cutover.floating_ips != "keep_address" or not _floating_addresses(m.vm):
            return None
        source = await self.db.get("provider", plan.source_provider_id, Provider)
        destination = await self.db.get("provider", plan.destination_provider_id, Provider)
        try:
            # the plan's credentials (SDD §8.3, §27.5)
            src = for_step(self.providers, Step.NETWORK, source, plan)
            dst = for_step(self.providers, Step.NETWORK, destination, plan)
        except ProviderError as exc:
            return exc
        for nic in m.vm.nics:
            for address in dict.fromkeys(_address(a) for a in nic.floating_ips):
                try:
                    if await self._move_one(m, plan, nic, address, src, dst, destination):
                        await self._fip_action(m, address, "move")
                except Exception as exc:
                    await self._fip_action(m, address, "move", exc)
                    return exc
        return None

    async def _move_one(
        self, m: Migration, plan: Plan, nic: Nic, address: str, src: Any, dst: Any, at: Provider
    ) -> bool:
        """One floating IP's move from where its journal left it (SDD §8.3); False when it had
        moved already."""
        entry = _journal_entry(await self.db.get("migration", m.id, Migration), address)
        if entry is not None and entry.state in ("moved", "finalized"):
            return False
        # SDD §8, §25.4 layer 2: no floating IP moves unless the lease holds
        await self._hold_the_lease(f"the move of floating IP {address} of {m.vm.name}")
        if entry is None or entry.state == "returned":
            targets = plan.nic_target(m.vm.source_id, nic)[1]
            port = await src.floating_ip_port(address)
            entry = await self._journal(
                m.id,
                address,
                fixed_ip=targets[0] if targets else "",
                source_port=port,
                state="releasing",
            )
        if entry.state == "releasing":
            await src.disassociate_floating_ip(address)
            entry = await self._journal(m.id, address, state="released")
        await self._reserve(plan, at, address)
        server = (await self.db.get("migration", m.id, Migration)).destination_server_id
        port_id = await dst.server_port(str(server), entry.fixed_ip)
        if port_id is None:
            raise ProviderError(f"server {server} in {at.label} has no port with {entry.fixed_ip}")
        await dst.associate_floating_ip(address, port_id)
        await self._journal(m.id, address, state="moved")
        return True

    async def _return_floating_ips(self, m: Migration) -> BaseException | None:
        """SDD §8.3: before a rollback starts the source, each floating IP the journal has not
        returned leaves the destination - disassociated and released - and goes back to the port
        it was on at the source. The failure, None when every one returned."""
        cur = await self.db.get("migration", m.id, Migration)
        pending = [e for e in cur.fip_moves if e.state in ("releasing", "released", "moved")]
        if not pending:
            return None
        plan = await self._plan(m.plan_id)
        source = await self.db.get("provider", plan.source_provider_id, Provider)
        destination = await self.db.get("provider", plan.destination_provider_id, Provider)
        try:
            src = for_step(self.providers, Step.NETWORK, source, plan)
            dst = for_step(self.providers, Step.NETWORK, destination, plan)
        except ProviderError as exc:
            return exc  # the rollback fails before the source starts, as a call's failure does
        for entry in pending:
            try:
                # SDD §8, §25.4 layer 2: no floating IP goes back unless the lease holds
                await self._hold_the_lease(f"the return of floating IP {entry.address}")
                await dst.disassociate_floating_ip(entry.address)
                await dst.release_floating_ip(entry.address)
                if entry.source_port:
                    await src.associate_floating_ip(entry.address, entry.source_port)
                await self._journal(m.id, entry.address, state="returned")
            except Exception as exc:
                await self._fip_action(m, entry.address, "return", exc)
                return exc
            await self._fip_action(m, entry.address, "return")
        return None

    async def _release_floating_ips(self, m: Migration, plan: Plan, source: Provider) -> None:
        """SDD §8.3: a finalize deletes the source's floating IP of each moved address - its
        allocation -, with the plan's source credential (§27.5); a failure is recorded and the
        migration finalized. ``m`` is the finalize's, saved with its transition."""
        if not any(e.state == "moved" for e in m.fip_moves):
            return
        try:
            src = for_step(self.providers, Step.NETWORK, source, plan)
        except ProviderError as exc:
            for entry in m.fip_moves:
                if entry.state == "moved":
                    await self._fip_action(m, entry.address, "release", exc)
            return
        for i, entry in enumerate(m.fip_moves):
            if entry.state != "moved":
                continue
            try:
                await src.release_floating_ip(entry.address)
            except Exception as exc:
                await self._fip_action(m, entry.address, "release", exc)
                continue
            m.fip_moves[i] = entry.model_copy(update={"state": "finalized"})
            await self._fip_action(m, entry.address, "release")

    async def _dns_action(
        self, m: Migration, name: str, step: str, outcome: str, detail: str | None = None
    ) -> None:
        """A DNS swap's migration.action (SDD §8.4); its detail redacted."""
        await self._emit(
            "migration.action",
            f"{m.vm.name}: DNS record {name}: {step} {outcome}",
            migration=m,
            actor="orchestrator",
            data={
                "action": "dns",
                "name": name,
                "step": step,
                "outcome": outcome,
                "detail": None if detail is None else redact_cut(detail, 500),
            },
        )

    async def _save_swap(self, mid: str, swap: DnsSwap) -> DnsSwap:
        """Save the migration's DnsSwap (SDD §4.2, §8.4); the swap as saved."""
        async with self._lock(mid):
            cur, v = await self._load(mid)
            cur.dns_swap = swap
            await self._save(cur, v)
        return swap

    async def _swap_dns(self, m: Migration) -> BaseException | None:
        """SDD §8.4: the VM's DNS record moves from its address at the source to the one taking
        its place, by a compare-and-swap - saved as swapping before the first request, so a
        restart resumes the same swap; one swapped, unchanged or in conflict is not made again, one
        reverted is made anew. The failure, None otherwise."""
        plan = await self._plan(m.plan_id)
        target = plan.network_cutover.dns_targets.get(m.vm.source_id)
        if target is None:
            return None
        cur = await self.db.get("migration", m.id, Migration)
        swap = cur.dns_swap
        if swap is not None and swap.state not in ("swapping", "reverted"):
            return None
        destination = await self.db.get("provider", plan.destination_provider_id, Provider)
        name = record_name(target)
        try:
            # the plan's destination credential (SDD §8.4, §27.5)
            dst = for_step(self.providers, Step.DNS, destination, plan)
            if swap is None or swap.state == "reverted":
                server = None
                if any(nic.floating_ips for nic in cur.vm.nics):
                    server = await dst.get_server(str(cur.destination_server_id))
                old, new = swap_addresses(cur, plan, server)
                state = "unchanged" if old == new else "swapping"
                swap = await self._save_swap(
                    m.id, DnsSwap(name=name, rdtype=rdtype_for(old), old=old, new=new, state=state)
                )
                if state == "unchanged":
                    await self._dns_action(m, name, "swap", "unchanged", f"the address stays {old}")
                    return None
            # SDD §8, §25.4 layer 2: no DNS swap unless the lease holds
            await self._hold_the_lease(f"the DNS swap of {swap.name}")
            backend = backend_for(target, dst, self.settings)
            outcome, held = await backend.replace(swap.name, swap.rdtype, swap.old, swap.new)
        except Exception as exc:
            # its text alone, never what a library may have put in it (SDD §8.4)
            failure = DnsSwapError(describe(exc))
            await self._dns_action(m, swap.name if swap else name, "swap", "failed", str(failure))
            return failure
        if outcome == "swapped":
            await self._save_swap(m.id, swap.model_copy(update={"state": "swapped"}))
            await self._dns_action(m, swap.name, "swap", "succeeded", f"{swap.old} to {swap.new}")
        else:
            await self._save_swap(m.id, swap.model_copy(update={"state": "conflict"}))
            holds = ", ".join(held) or "no address"
            await self._dns_action(
                m, swap.name, "swap", "conflict", f"it holds {holds}, not {swap.old}: left as it is"
            )
        return None

    async def _revert_dns(self, m: Migration) -> BaseException | None:
        """SDD §8.4: before a rollback returns the floating IPs and starts the source, a swap
        swapping or swapped goes back, new to old, by the same compare-and-swap: reverted, or a
        conflict left as it is. The failure, None otherwise - a plan that dropped the VM's target
        has nothing to revert with, which is recorded."""
        cur = await self.db.get("migration", m.id, Migration)
        swap = cur.dns_swap
        if swap is None or swap.state not in ("swapping", "swapped"):
            return None
        plan = await self._plan(m.plan_id)
        target = plan.network_cutover.dns_targets.get(m.vm.source_id)
        if target is None:
            await self._dns_action(
                m,
                swap.name,
                "revert",
                "failed",
                f"the plan names no DNS target for the VM any more: the record may hold {swap.new}",
            )
            return None
        destination = await self.db.get("provider", plan.destination_provider_id, Provider)
        try:
            # SDD §8, §25.4 layer 2: no DNS revert unless the lease holds
            await self._hold_the_lease(f"the DNS revert of {swap.name}")
            dst = for_step(self.providers, Step.DNS, destination, plan)
            backend = backend_for(target, dst, self.settings)
            outcome, held = await backend.replace(swap.name, swap.rdtype, swap.new, swap.old)
        except Exception as exc:
            failure = DnsSwapError(describe(exc))
            await self._dns_action(m, swap.name, "revert", "failed", str(failure))
            return failure
        if outcome == "swapped":
            await self._save_swap(m.id, swap.model_copy(update={"state": "reverted"}))
            await self._dns_action(m, swap.name, "revert", "succeeded", f"{swap.new} to {swap.old}")
        else:
            await self._save_swap(m.id, swap.model_copy(update={"state": "conflict"}))
            holds = ", ".join(held) or "no address"
            await self._dns_action(
                m,
                swap.name,
                "revert",
                "conflict",
                f"it holds {holds}, not {swap.new}: left as it is",
            )
        return None

    async def _post_create_hooks_s(self, plan: Plan) -> float:
        """The timeouts of the plan's post_create hooks, the part of the downtime they may take
        (SDD §9.1, §23.3); a name the catalog no longer holds counts nothing."""
        total = 0.0
        for name in plan.network_cutover.hooks.get("post_create", []):
            try:
                total += (await self.db.get("hook", name, Hook)).timeout_s
            except NotFound:
                continue
        return total

    async def _lb_action(
        self, m: Migration, entry: LbMove, step: str, error: BaseException | None = None
    ) -> None:
        """A member helper step's migration.action (SDD §8.5); a failure's detail redacted."""
        outcome = "succeeded" if error is None else "failed"
        detail = None if error is None else redact_cut(describe_lb(error), 500)
        await self._emit(
            "migration.action",
            f"{m.vm.name}: Octavia member {entry.address}:{entry.port} of pool "
            f"{entry.source_pool}: {step} {outcome}",
            migration=m,
            actor="orchestrator",
            data={
                "action": "lb_member",
                "pool": entry.source_pool,
                "address": entry.address,
                "port": entry.port,
                "step": step,
                "outcome": outcome,
                "detail": detail,
            },
        )

    async def _lb_journal(self, mid: str, entry: LbMove) -> LbMove:
        """Save the LbMove of its source member, in its place or as a new one (SDD §4.2, §8.5); the
        entry as saved."""
        key = (entry.source_pool, entry.source_member)
        async with self._lock(mid):
            cur, v = await self._load(mid)
            moves = [entry if (e.source_pool, e.source_member) == key else e for e in cur.lb_moves]
            if entry not in moves:
                moves.append(entry)
            cur.lb_moves = moves
            await self._save(cur, v)
        return entry

    async def _lb_clouds(self, m: Migration) -> tuple[Plan, Any, Any]:
        """The plan, and its source's and destination's clouds as the member helper reaches them:
        each with the plan's credential on its side (SDD §8.5, §27.5)."""
        plan = await self._plan(m.plan_id)
        source = await self.db.get("provider", plan.source_provider_id, Provider)
        destination = await self.db.get("provider", plan.destination_provider_id, Provider)
        src = for_step(self.providers, Step.NETWORK, source, plan)
        return plan, src, for_step(self.providers, Step.NETWORK, destination, plan)

    async def _drain_lb_members(self, m: Migration) -> BaseException | None:
        """SDD §8.5: at pre_stop, each member of a mapped source pool whose address is one of the
        VM's is journaled draining, its admin_state_up set to false, and drained once Octavia reads
        it ACTIVE; one drained, joining or joined is not drained again, one restored is anew. The
        failure, None otherwise."""
        plan = await self._plan(m.plan_id)
        addresses = vm_addresses(m.vm)
        if not plan.network_cutover.lb_pools or not addresses:
            return None
        try:
            _, src, _ = await self._lb_clouds(m)
        except ProviderError as exc:
            return exc  # the drain fails at pre_stop, the source still running
        if getattr(src, "pool_members", None) is None:
            return None  # a VMware source has no Octavia pool
        for source_pool, pool in plan.network_cutover.lb_pools.items():
            try:
                members = await src.pool_members(source_pool)
            except Exception as exc:
                return LbHelperError(describe_lb(exc))
            for member in members:
                address = canonical(str(member.get("address")))
                if address not in addresses:
                    continue
                key = (source_pool, str(member.get("id")))
                cur = await self.db.get("migration", m.id, Migration)
                entry = next(
                    (e for e in cur.lb_moves if (e.source_pool, e.source_member) == key), None
                )
                if entry is not None and entry.state in ("drained", "joining", "joined"):
                    continue
                entry = LbMove(
                    source_pool=source_pool,
                    source_member=key[1],
                    address=address,
                    port=int(member.get("protocol_port") or 0),
                    pool=pool,
                )
                try:
                    # SDD §8, §25.4 layer 2: no member changes unless the lease holds
                    await self._hold_the_lease(f"the drain of member {address} of {source_pool}")
                    await self._lb_journal(m.id, entry)
                    await self.lb_pace.change(
                        functools.partial(src.set_pool_member, source_pool, key[1], False)
                    )
                    await self.lb_pace.active(
                        functools.partial(member_of, src, source_pool, key[1]),
                        f"member {address}:{entry.port} of pool {source_pool}",
                    )
                    entry = await self._lb_journal(
                        m.id, entry.model_copy(update={"state": "drained"})
                    )
                except Exception as exc:
                    failure = LbHelperError(describe_lb(exc))
                    await self._lb_action(m, entry, "drain", failure)
                    return failure
                await self._lb_action(m, entry, "drain")
        return None

    async def _join_lb_members(self, m: Migration) -> BaseException | None:
        """SDD §8.5: at post_verify, each entry drained gets a member of the address that takes its
        address's place, on the same port, in the destination pool - one the pool holds already, a
        restart's, is taken -, and is joined once Octavia reads it ACTIVE. The failure, None
        otherwise."""
        cur = await self.db.get("migration", m.id, Migration)
        pending = [e for e in cur.lb_moves if e.state in ("drained", "joining")]
        if not pending:
            return None
        try:
            plan, _, dst = await self._lb_clouds(m)
        except ProviderError as exc:
            return exc
        for entry in pending:
            try:
                # SDD §8, §25.4 layer 2: no member changes unless the lease holds
                await self._hold_the_lease(f"the join of member {entry.address} to {entry.pool}")
                address = join_address(cur, plan, entry.address)
                entry = await self._lb_journal(m.id, entry.model_copy(update={"state": "joining"}))
                held = [
                    str(x.get("id"))
                    for x in await dst.pool_members(entry.pool)
                    if canonical(str(x.get("address"))) == address
                    and int(x.get("protocol_port") or 0) == entry.port
                ]
                member = held[0] if held else None
                if member is None:
                    member = await self.lb_pace.change(
                        functools.partial(dst.add_pool_member, entry.pool, address, entry.port)
                    )
                entry = await self._lb_journal(m.id, entry.model_copy(update={"member": member}))
                await self.lb_pace.active(
                    functools.partial(member_of, dst, entry.pool, member),
                    f"member {address}:{entry.port} of pool {entry.pool}",
                )
                entry = await self._lb_journal(m.id, entry.model_copy(update={"state": "joined"}))
            except Exception as exc:
                failure = LbHelperError(describe_lb(exc))
                await self._lb_action(m, entry, "join", failure)
                return failure
            await self._lb_action(m, entry, "join")
        return None

    async def _restore_lb_members(self, m: Migration) -> None:
        """SDD §8.5: before a rollback starts the source, each entry not restored has its
        destination member deleted - a create a restart may have sent included - and its source
        member's admin_state_up set to true again; a failure is recorded and the rollback goes
        on."""
        cur = await self.db.get("migration", m.id, Migration)
        pending = [e for e in cur.lb_moves if e.state != "restored"]
        if not pending:
            return
        try:
            plan, src, dst = await self._lb_clouds(m)
        except ProviderError as exc:
            for entry in pending:  # recorded, and the rollback goes on (§8.5)
                await self._lb_action(m, entry, "restore", exc)
            return
        for entry in pending:
            try:
                # SDD §8, §25.4 layer 2: no member changes unless the lease holds
                await self._hold_the_lease(f"the restore of member {entry.address}")
                gone = {entry.member} if entry.member else set()
                if entry.state == "joining":
                    address = join_address(cur, plan, entry.address)
                    gone |= {
                        str(x.get("id"))
                        for x in await dst.pool_members(entry.pool)
                        if canonical(str(x.get("address"))) == address
                        and int(x.get("protocol_port") or 0) == entry.port
                    }
                for member in sorted(gone):
                    await self.lb_pace.change(
                        functools.partial(dst.delete_pool_member, entry.pool, member)
                    )
                await self.lb_pace.change(
                    functools.partial(
                        src.set_pool_member, entry.source_pool, entry.source_member, True
                    )
                )
                await self.lb_pace.active(
                    functools.partial(member_of, src, entry.source_pool, entry.source_member),
                    f"member {entry.address}:{entry.port} of pool {entry.source_pool}",
                )
                entry = await self._lb_journal(
                    m.id, entry.model_copy(update={"state": "restored", "member": None})
                )
            except Exception as exc:
                await self._lb_action(m, entry, "restore", LbHelperError(describe_lb(exc)))
                continue
            await self._lb_action(m, entry, "restore")

    async def _owe_revert(self, mid: str, revert: str) -> None:
        """Add ``revert`` to what the migration's rollback owes (SDD §4.2, §8.2)."""
        async with self._lock(mid):
            cur, v = await self._load(mid)
            cur.hook_reverts.append(revert)
            await self._save(cur, v)

    async def _run_rollback_hooks(self, m: Migration) -> None:
        """Before a rollback's step (SDD §8.2): the reverts the migration owes, the latest first,
        each dropped once it ran - a restart runs those still listed -, then the on_rollback
        hooks; a failure is recorded and the rollback goes on."""
        while True:
            async with self._lock(m.id):
                cur, _ = await self._load(m.id)
            if not cur.hook_reverts:
                break
            revert = cur.hook_reverts[-1]
            await self._run_hooks(cur, "on_rollback", [revert], keep_going=True)
            async with self._lock(m.id):
                cur, v = await self._load(m.id)
                if cur.hook_reverts and cur.hook_reverts[-1] == revert:
                    cur.hook_reverts.pop()
                    await self._save(cur, v)
        await self._run_hooks(cur, "on_rollback", keep_going=True)

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
                # SDD §4.3, §24.5: whether the downtime clock was open at the failure
                (
                    P.failed,
                    f"{step} failed: {message[:200]}",
                    {**(data or {}), "source_stopped": fsm.source_stopped(cur)},
                )
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
        # nothing of a hook, a DNS swap or the member helper reaches agentmemory (SDD §8.2, §8.4,
        # §8.5, §13.4)
        if (
            self.knowledge is not None
            and not str(step).startswith("hook:")
            and step not in ("dns", "lb_member")
        ):
            # SDD §8 (audit OC-04): a task of its own - the driver that runs this goes on to the
            # rollback at once, never waiting for agentmemory; a stop cancels it
            lookup = asyncio.create_task(
                self._attach_similar_incidents(cur, str(step), exc), name=f"similar:{mid}"
            )
            self._lookups.add(lookup)
            lookup.add_done_callback(self._lookups.discard)

    async def _attach_similar_incidents(
        self, failed: Migration, step: str, exc: BaseException
    ) -> None:
        """The incidents agentmemory knows like the failure, attached as a note once it answers
        (SDD §8, §14.3) - best effort, as every agentmemory call."""
        assert self.knowledge is not None
        try:
            note = await self.knowledge.on_failure(failed, step, exc)
            if note is None:
                return
            async with self._lock(failed.id):
                cur, v = await self._load(failed.id)
                cur.advisor_notes.append(note)
                await self._save(cur, v)
            # SDD §14.3: announced once saved - the refresh it asks for (§16) reads the note
            await self._emit(
                "advisor.similar_incidents", note.summary, migration=cur, data=note.data
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("the similar incidents of %s were not attached", failed.id, exc_info=True)


def phases(migrations: Iterable[Migration]) -> Counter[str]:
    """Count migrations per phase (helper for stats/metrics)."""
    return Counter(str(m.phase) for m in migrations)
