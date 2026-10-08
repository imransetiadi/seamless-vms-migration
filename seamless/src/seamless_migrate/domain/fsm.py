"""Migration state machine (SDD §5.1)."""

from __future__ import annotations

from datetime import datetime

from .enums import Phase
from .models import Migration, PhaseChange, utcnow

P = Phase

TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    P.pending: frozenset({P.validating, P.cancelled}),
    P.validating: frozenset({P.ready, P.blocked, P.failed}),
    P.blocked: frozenset({P.validating, P.cancelled}),
    P.ready: frozenset({P.precopy, P.cutover, P.validating, P.cancelled}),
    P.precopy: frozenset({P.syncing, P.awaiting_cutover, P.failed, P.cancelled}),
    P.syncing: frozenset({P.awaiting_cutover, P.failed, P.cancelled}),
    P.awaiting_cutover: frozenset({P.syncing, P.cutover, P.cancelled}),
    P.cutover: frozenset({P.verifying, P.failed, P.rolling_back}),
    P.verifying: frozenset({P.completed, P.failed, P.rolling_back}),
    P.completed: frozenset({P.finalized, P.rolling_back}),
    P.failed: frozenset({P.rolling_back, P.ready, P.cancelled}),
    P.rolling_back: frozenset({P.rolled_back, P.failed}),
    P.rolled_back: frozenset({P.ready}),
    P.finalized: frozenset(),
    P.cancelled: frozenset(),
}

TERMINAL_PHASES = frozenset({P.finalized, P.cancelled})
SUCCESS_PHASES = frozenset({P.completed, P.finalized})
WAVE_COMPLETE_PHASES = frozenset({P.completed, P.finalized, P.cancelled, P.rolled_back})
#: Phases in which an executor step (or verification) is running for the migration.
ACTIVE_STEP_PHASES = frozenset({P.precopy, P.syncing, P.cutover, P.verifying, P.rolling_back})
#: Phases resumed by the orchestrator after a restart (SDD §8).
RESUMABLE_PHASES = ACTIVE_STEP_PHASES


class InvalidTransition(Exception):
    def __init__(self, from_phase: Phase, to_phase: Phase, detail: str | None = None) -> None:
        self.from_phase = Phase(from_phase)
        self.to_phase = Phase(to_phase)
        message = f"invalid transition {self.from_phase} -> {self.to_phase}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


def is_terminal(phase: Phase | str) -> bool:
    return Phase(phase) in TERMINAL_PHASES


def can_transition(migration: Migration, to: Phase | str) -> bool:
    try:
        _check(migration, Phase(to))
    except InvalidTransition:
        return False
    return True


def _check(migration: Migration, to: Phase) -> None:
    current = Phase(migration.phase)
    if to not in TRANSITIONS[current]:
        raise InvalidTransition(current, to)
    if current == P.failed and to == P.cancelled and migration.downtime_started_at is not None:
        raise InvalidTransition(current, to, "the source VM was stopped; roll back instead")


def transition(
    migration: Migration,
    to: Phase | str,
    reason: str,
    actor: str = "system",
    *,
    now: datetime | None = None,
) -> Migration:
    """Return a copy of ``migration`` moved to ``to``; raise ``InvalidTransition`` otherwise."""
    target = Phase(to)
    _check(migration, target)
    at = now or utcnow()
    out = migration.model_copy(deep=True)
    out.phase_history.append(
        PhaseChange(from_phase=migration.phase, to_phase=target, at=at, reason=reason, actor=actor)
    )
    if migration.phase == P.failed and target == P.ready:
        out.attempts += 1
    out.phase = target
    out.updated_at = at
    return out
