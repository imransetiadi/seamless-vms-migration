"""Migration invariants (SDD §5.5, ADV-16): a completed migration's numbers must hold together."""

from datetime import UTC, datetime, timedelta

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import Phase, Strategy
from seamless_migrate.domain.models import Estimate, ForceCutover, SyncPass
from seamless_migrate.invariants import check_invariants
from seamless_migrate.stats import compute_stats
from tests.factories import make_migration, make_plan

T0 = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
GIB = 2**30
PARTS = {"shutdown_s": 60.0, "copy_s": 60.0, "conversion_s": 0.0, "create_s": 60.0, "boot_s": 120.0}
PLAN = make_plan(
    id="plan-1", convergence_threshold_bytes=GIB, max_sync_passes=5, downtime_slo_s=600
)
REAL = Settings()
DEMO = Settings(demo=True)


def sync_pass(number, kind, gib, start_s, duration_s):
    size = int(gib * GIB)
    return SyncPass(
        number=number,
        kind=kind,
        started_at=T0 + timedelta(seconds=start_s),
        ended_at=T0 + timedelta(seconds=start_s + duration_s),
        bytes_scanned=size,
        bytes_changed=size,
        bytes_transferred=size,
        duration_s=duration_s,
    )


def warm(strategy=Strategy.warm, **kw):
    """A warm migration whose numbers hold: converged at 0.5 GiB, final sync after the stop."""
    passes = kw.pop("passes", None) or [
        sync_pass(1, "full", 100, 0, 800),
        sync_pass(2, "delta", 0.5, 800, 60),
        sync_pass(3, "final", 0.2, 1000, 30),
    ]
    data = {
        "plan_id": "plan-1",
        "strategy": strategy,
        "phase": Phase.completed,
        "sync_passes": passes,
        "bytes_transferred": sum(p.bytes_transferred for p in passes),
        "downtime_started_at": T0 + timedelta(seconds=990),
        "downtime_ended_at": T0 + timedelta(seconds=1300),
        "actual_downtime_s": 310.0,
        "converged_by": "threshold",
        "estimate": Estimate(
            strategy=strategy,
            eligible=True,
            precopy_s=860,
            passes=2,
            downtime_s=300,
            total_s=1160,
            final_delta_bytes=0,
            meets_slo=True,
            downtime_parts=dict(PARTS),
        ),
    }
    data.update(kw)
    return make_migration(**data)


def codes(m, plan=PLAN, settings=REAL):
    return [issue.split(":", 1)[0] for issue in check_invariants(m, plan, settings)]


def test_a_migration_whose_numbers_hold_has_no_issue():
    assert codes(warm()) == []


def test_a_warm_migration_needs_a_final_sync_after_the_source_stopped():
    full, delta, _final = warm().sync_passes
    assert "FINAL_SYNC_MISSING" in codes(
        warm(
            passes=[full, delta], bytes_transferred=full.bytes_transferred + delta.bytes_transferred
        )
    )
    early = sync_pass(3, "final", 0.2, 980, 30)  # started before the stop at 990 s
    assert "FINAL_SYNC_MISSING" in codes(warm(passes=[full, delta, early]))


def test_the_transferred_bytes_must_add_up():
    assert "BYTES_MISMATCH" in codes(warm(bytes_transferred=1))
    assert (
        codes(warm(sync_bytes_dropped=GIB, bytes_transferred=warm().bytes_transferred + GIB)) == []
    )


@pytest.mark.parametrize(
    ("change", "flagged"),
    [
        # the last delta (16.8 GiB) is above the 1 GiB threshold the record names
        (
            {
                "passes": [
                    sync_pass(1, "full", 374, 0, 3000),
                    sync_pass(2, "final", 16.8, 3100, 258),
                ]
            },
            True,
        ),
        ({"converged_by": "max_passes"}, True),  # 2 passes, the limit is 5
        ({"converged_by": "slo"}, False),  # the recorded estimate (300 s) fits the 600 s SLO
        ({"converged_by": "force"}, True),  # no Force cutover recorded
        (
            {
                "converged_by": "force",
                "force_cutover": ForceCutover(
                    actor="sari",
                    at=T0,
                    reason="Window closes in 10 minutes",
                    delta_bytes=GIB,
                    estimated_downtime_s=320,
                ),
            },
            False,
        ),
        ({"converged_by": None}, False),  # data before ADV-16 names no rule
    ],
)
def test_a_cutover_must_follow_the_rule_it_records(change, flagged):
    if "passes" in change:
        change["bytes_transferred"] = sum(p.bytes_transferred for p in change["passes"])
        change["downtime_started_at"] = T0 + timedelta(seconds=3050)
        change["actual_downtime_s"] = 400.0
    assert ("CUTOVER_NOT_CONVERGED" in codes(warm(**change))) is flagged


def test_an_slo_rule_needs_an_estimate_within_the_slo():
    over = warm(converged_by="slo").estimate.model_copy(
        update={"downtime_s": 900, "downtime_parts": {**PARTS, "copy_s": 660.0}}
    )
    assert "CUTOVER_NOT_CONVERGED" in codes(warm(converged_by="slo", estimate=over))


def test_the_downtime_cannot_be_shorter_than_the_final_sync_it_contains():
    """dc2-db-01: 5.98 s of downtime around a 257.8 s final sync (ADV-16)."""
    assert "DOWNTIME_BELOW_FINAL_SYNC" in codes(warm(actual_downtime_s=5.98), settings=DEMO)
    assert "DOWNTIME_BELOW_FINAL_SYNC" not in codes(warm(actual_downtime_s=31.0), settings=DEMO)


def test_the_estimate_parts_must_add_up_to_the_estimate():
    bad = warm().estimate.model_copy(update={"downtime_parts": {**PARTS, "boot_s": 0.0}})
    assert "ESTIMATE_PARTS_MISMATCH" in codes(warm(estimate=bad))


def test_the_real_vmware_warm_final_sync_is_not_checked_until_the_lab():
    """TODO(ADV-16) P6: the kit's final CBT sync is not recorded outside demo mode."""
    full, delta, _final = warm().sync_passes
    unrecorded = warm(
        strategy=Strategy.vmware_warm,
        passes=[full, delta],
        bytes_transferred=full.bytes_transferred + delta.bytes_transferred,
        converged_by="slo",
    )
    assert codes(unrecorded, settings=REAL) == []
    assert "FINAL_SYNC_MISSING" in codes(unrecorded, settings=DEMO)


def test_single_shot_strategies_have_no_warm_rules():
    cold = make_migration(
        plan_id="plan-1",
        strategy=Strategy.cold,
        phase=Phase.completed,
        sync_passes=[sync_pass(1, "full", 10, 1000, 90)],
        bytes_transferred=int(10 * GIB),
        downtime_started_at=T0 + timedelta(seconds=990),
        downtime_ended_at=T0 + timedelta(seconds=1300),
        actual_downtime_s=310.0,
    )
    assert codes(cold) == []


def test_the_statistics_leave_out_a_migration_whose_numbers_do_not_hold():
    good = warm(id="mig-0000000001")
    bad = warm(
        id="mig-0000000002",
        actual_downtime_s=5.98,
        data_issues=["DOWNTIME_BELOW_FINAL_SYNC: 6 s of downtime around a 30 s final sync"],
    )
    stats = compute_stats([good, bad], {"plan-1": PLAN})
    assert (stats.completed, stats.data_inconsistent) == (2, 1)
    assert stats.avg_downtime_s == pytest.approx(310.0)
    assert (stats.max_downtime_s, stats.slo_compliance_pct) == (310.0, 100.0)
