"""The plan status every view shows (SDD §8.1, ADV-19).

``Plan.status`` is the lifecycle the orchestrator drives; a re-validation of a finished plan stores
``validated`` although its migrations completed, and it cannot tell a finished plan from a finalized
one. ``derive_plan_status`` reads the status from the plan's migrations instead.
"""

from __future__ import annotations

from collections.abc import Iterable

from .domain.enums import DerivedPlanStatus as D
from .domain.enums import Phase as P
from .domain.enums import PlanStatus
from .domain.models import Migration, Plan

#: a migration in these is at work: the plan runs (or is paused)
AT_WORK = frozenset(
    {P.precopy, P.syncing, P.awaiting_cutover, P.cutover, P.verifying, P.rolling_back}
)
#: still to start in a running or paused plan
TO_START = frozenset({P.pending, P.ready, P.blocked, P.validating})
DONE = frozenset({P.completed, P.finalized, P.rolled_back})


def derive_plan_status(plan: Plan, migrations: Iterable[Migration]) -> D:
    """The rules of SDD §8.1, in order, over the plan's migrations that are not cancelled."""
    return derive_plan_status_from_phases(
        plan, {m.phase for m in migrations if m.plan_id == plan.id}
    )


def derive_plan_status_from_phases(plan: Plan, phases: Iterable[P]) -> D:
    """The same rules over the set of the plan's phases, as ``Store.plan_phases`` gives it (§11)."""
    stored = D(str(plan.status))
    if plan.status == PlanStatus.draft:
        return D.draft
    phases = set(phases) - {P.cancelled}
    if not phases:
        return stored
    if phases & AT_WORK:
        return D.paused if plan.status == PlanStatus.paused else D.running
    if plan.status in (PlanStatus.running, PlanStatus.paused) and phases & TO_START:
        return stored
    if P.failed in phases:
        return D.failed
    if phases <= DONE:
        if P.completed in phases:
            return D.completed
        return D.finalized if P.finalized in phases else D.completed
    return stored
