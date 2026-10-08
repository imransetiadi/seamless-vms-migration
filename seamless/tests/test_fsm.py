from datetime import UTC, datetime

import pytest

from seamless_migrate.domain import fsm
from seamless_migrate.domain.enums import Phase
from tests.factories import make_migration

P = Phase
# SDD §5.1, transcribed independently of the implementation.
ALLOWED = {
    P.pending: {P.validating, P.cancelled},
    P.validating: {P.ready, P.blocked, P.failed},
    P.blocked: {P.validating, P.cancelled},
    P.ready: {P.precopy, P.cutover, P.validating, P.cancelled},
    P.precopy: {P.syncing, P.awaiting_cutover, P.failed, P.cancelled},
    P.syncing: {P.awaiting_cutover, P.failed, P.cancelled},
    P.awaiting_cutover: {P.syncing, P.cutover, P.cancelled},
    P.cutover: {P.verifying, P.failed, P.rolling_back},
    P.verifying: {P.completed, P.failed, P.rolling_back},
    P.completed: {P.finalized, P.rolling_back},
    P.failed: {P.rolling_back, P.ready, P.cancelled},
    P.rolling_back: {P.rolled_back, P.failed},
    P.rolled_back: {P.ready},
    P.finalized: set(),
    P.cancelled: set(),
}


def test_table_matches_sdd():
    assert {k: set(v) for k, v in fsm.TRANSITIONS.items()} == ALLOWED


@pytest.mark.parametrize("src,dst", [(s, d) for s, ds in ALLOWED.items() for d in ds])
def test_every_allowed_transition_succeeds(src, dst):
    m = make_migration(phase=src)
    before = m.updated_at
    out = fsm.transition(m, dst, "because", actor="alice")
    assert out.phase == dst
    change = out.phase_history[-1]
    assert (change.from_phase, change.to_phase, change.reason, change.actor) == (
        src,
        dst,
        "because",
        "alice",
    )
    assert out.updated_at >= before
    assert m.phase == src, "input migration must not be mutated"


@pytest.mark.parametrize("src,dst", [(s, d) for s in Phase for d in Phase if d not in ALLOWED[s]])
def test_every_other_transition_raises(src, dst):
    m = make_migration(phase=src)
    with pytest.raises(fsm.InvalidTransition):
        fsm.transition(m, dst, "nope")


def test_failed_to_cancelled_requires_no_downtime():
    m = make_migration(phase=Phase.failed)
    assert fsm.transition(m, Phase.cancelled, "never stopped").phase == Phase.cancelled
    stopped = make_migration(
        phase=Phase.failed, downtime_started_at=datetime(2026, 10, 8, tzinfo=UTC)
    )
    with pytest.raises(fsm.InvalidTransition):
        fsm.transition(stopped, Phase.cancelled, "source was stopped")


def test_retry_increments_attempts():
    m = make_migration(phase=Phase.failed, attempts=1)
    assert fsm.transition(m, Phase.ready, "retry").attempts == 2
    # rolled_back -> ready is not a retry of a failed step
    rb = make_migration(phase=Phase.rolled_back, attempts=1)
    assert fsm.transition(rb, Phase.ready, "again").attempts == 1


def test_phase_sets():
    assert fsm.is_terminal(Phase.finalized) and fsm.is_terminal(Phase.cancelled)
    assert not fsm.is_terminal(Phase.rolled_back) and not fsm.is_terminal(Phase.completed)
    assert fsm.SUCCESS_PHASES == frozenset({Phase.completed, Phase.finalized})
    assert fsm.WAVE_COMPLETE_PHASES == frozenset(
        {Phase.completed, Phase.finalized, Phase.cancelled, Phase.rolled_back}
    )
