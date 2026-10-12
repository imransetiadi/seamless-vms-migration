"""The plan status every view shows (SDD §8.1, ADV-19): derived from the plan and its migrations."""

import json
from pathlib import Path

import pytest

from seamless_migrate.domain.enums import DerivedPlanStatus, Phase
from seamless_migrate.plan_status import derive_plan_status
from tests.factories import make_migration, make_plan

P = Phase

CASES = json.loads((Path(__file__).parent / "fixtures" / "plan_status_cases.json").read_text())[
    "cases"
]


@pytest.mark.parametrize(
    ("stored", "phases", "expected"),
    [(c["stored"], [Phase(p) for p in c["phases"]], c["expected"]) for c in CASES],
    ids=[f"{c['stored']}-{'+'.join(sorted(set(c['phases']))) or 'none'}" for c in CASES],
)
def test_the_plan_status_follows_its_migrations(stored, phases, expected):
    """One case table for the control plane and the dashboard's mock (SDD §8.1)."""
    plan = make_plan(status=stored)
    migrations = [
        make_migration(id=f"mig-{i:010x}", plan_id=plan.id, phase=phase)
        for i, phase in enumerate(phases)
    ]
    assert derive_plan_status(plan, migrations) == expected


def test_every_derived_status_is_covered():
    assert {c["expected"] for c in CASES} == {s.value for s in DerivedPlanStatus}


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
