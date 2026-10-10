"""The plan status every view shows (SDD §8.1, ADV-19): derived from the plan and its migrations."""

import pytest

from seamless_migrate.domain.enums import DerivedPlanStatus, Phase
from seamless_migrate.plan_status import derive_plan_status
from tests.factories import make_migration, make_plan

P = Phase

CASES = [
    # 1. a draft plan is a draft (an edited plan is validated again), whatever its migrations did
    ("draft", [P.finalized], "draft"),
    # 2. no migration: the stored status
    ("validated", [], "validated"),
    ("running", [], "running"),
    # 3. a migration at work: running, or paused when the plan is
    ("running", [P.completed, P.syncing], "running"),
    ("paused", [P.awaiting_cutover, P.ready], "paused"),
    ("validated", [P.cutover], "running"),  # re-validated while a cutover still ran
    ("running", [P.rolling_back], "running"),
    # 4. a running or paused plan with a migration still to start keeps its status
    ("running", [P.completed, P.ready], "running"),
    ("paused", [P.ready, P.failed], "paused"),
    # 5. a failed migration, nothing at work
    ("running", [P.completed, P.failed], "failed"),
    ("validated", [P.failed, P.ready], "failed"),
    # 6. all done: completed while one awaits finalize, else finalized
    ("validated", [P.completed] * 12, "completed"),  # the demo's DC2 plan after a re-validation
    ("completed", [P.completed, P.finalized, P.rolled_back], "completed"),
    ("completed", [P.finalized, P.rolled_back], "finalized"),
    ("completed", [P.rolled_back], "completed"),  # nothing migrated: nothing to finalize
    # cancelled migrations do not count
    ("completed", [P.finalized, P.cancelled], "finalized"),
    ("validated", [P.cancelled], "validated"),
    # 7. otherwise the stored status
    ("validated", [P.ready, P.blocked], "validated"),
    ("failed", [P.ready], "failed"),  # pre-staging failed (SDD §8)
]


@pytest.mark.parametrize(("stored", "phases", "expected"), CASES)
def test_the_plan_status_follows_its_migrations(stored, phases, expected):
    plan = make_plan(status=stored)
    migrations = [
        make_migration(id=f"mig-{i:010x}", plan_id=plan.id, phase=phase)
        for i, phase in enumerate(phases)
    ]
    assert derive_plan_status(plan, migrations) == expected


def test_every_derived_status_is_covered():
    assert {expected for _, _, expected in CASES} == {s.value for s in DerivedPlanStatus}


def test_only_the_plans_own_migrations_count():
    plan = make_plan(status="validated")
    mine = make_migration(id="mig-0000000001", plan_id=plan.id, phase=P.completed)
    other = make_migration(id="mig-0000000002", plan_id="plan-other000", phase=P.syncing)
    assert derive_plan_status(plan, [mine, other]) == "completed"


def test_the_statistics_split_completed_into_awaiting_finalize_and_finalized():
    """SDD §4.2, §8.1: Completed stays the verified ones; two figures tell them apart."""
    from seamless_migrate.stats import compute_stats

    plan = make_plan(status="completed")
    migrations = [
        make_migration(id=f"mig-{i:010x}", plan_id=plan.id, phase=phase)
        for i, phase in enumerate([P.completed, P.completed, P.finalized, P.rolled_back])
    ]
    stats = compute_stats(migrations, {plan.id: plan})
    assert (stats.completed, stats.awaiting_finalize, stats.finalized) == (3, 2, 1)
