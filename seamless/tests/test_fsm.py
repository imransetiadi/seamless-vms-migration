from datetime import UTC, datetime, timedelta

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


def test_no_cancel_while_the_source_vm_is_stopped():
    """SDD §5.1: with the downtime clock open the source VM is stopped, and a cancel would leave it
    stopped with nothing left to restart it; once the clock closed, a cancel is fine again."""
    stopped_at = datetime(2026, 10, 8, tzinfo=UTC)
    for phase in (Phase.ready, Phase.awaiting_cutover, Phase.precopy, Phase.pending):
        open_clock = make_migration(phase=phase, downtime_started_at=stopped_at)
        with pytest.raises(fsm.InvalidTransition, match="stopped"):
            fsm.transition(open_clock, Phase.cancelled, "abandon")
        closed = make_migration(
            phase=phase,
            downtime_started_at=stopped_at,
            downtime_ended_at=stopped_at + timedelta(minutes=5),
        )
        assert fsm.transition(closed, Phase.cancelled, "abandon").phase == Phase.cancelled
    # the refusal says what to do instead: a failed one retries or rolls back, a ready one cuts over
    failed_open = make_migration(phase=Phase.failed, downtime_started_at=stopped_at)
    assert "roll back or retry" in fsm.refusal(failed_open, Phase.cancelled)
    ready_open = make_migration(phase=Phase.ready, downtime_started_at=stopped_at)
    assert "cut it over" in fsm.refusal(ready_open, Phase.cancelled)
    failed_closed = make_migration(
        phase=Phase.failed,
        downtime_started_at=stopped_at,
        downtime_ended_at=stopped_at + timedelta(minutes=5),
    )
    assert (
        fsm.refusal(failed_closed, Phase.cancelled)
        == "the source VM was stopped; roll back instead"
    )
    assert fsm.refusal(make_migration(phase=Phase.ready), Phase.cancelled) is None


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
