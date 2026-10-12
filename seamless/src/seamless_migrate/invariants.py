"""Migration invariants (SDD §5.5, ADV-16): a completed migration's numbers must hold together.

``check_invariants`` returns one ``"CODE: text"`` per broken invariant. The orchestrator stores the
result in ``Migration.data_issues`` when a migration completes; the statistics leave such migrations
out of the downtime figures, and the dashboard badges them.
"""

from __future__ import annotations

from .config import Settings
from .domain.enums import Strategy, SyncPassKind
from .domain.models import Migration, Plan

WARM = frozenset({Strategy.warm, Strategy.vmware_warm})
#: how far apart the estimate's parts and its total may be (rounding)
PARTS_TOLERANCE_S = 1.0


def _gib(n: float) -> str:
    return f"{n / 2**30:.1f} GiB"


def check_invariants(m: Migration, plan: Plan, settings: Settings) -> list[str]:
    issues: list[str] = []
    strategy = Strategy(m.strategy)
    warm = strategy in WARM
    # TODO(ADV-16): P6 - the real VMware warm cutover does not record its final CBT sync yet (lab)
    final_unrecorded = strategy == Strategy.vmware_warm and not settings.demo
    finals = [p for p in m.sync_passes if p.kind == SyncPassKind.final]
    final = finals[-1] if finals else None

    # 1. a warm migration's final sync ran after the source stopped
    if warm and not final_unrecorded:
        stopped = m.downtime_started_at
        if final is None:
            issues.append("FINAL_SYNC_MISSING: no final sync recorded after the source stopped")
        elif stopped is not None and final.started_at < stopped:
            issues.append(
                "FINAL_SYNC_MISSING: the final sync is recorded as starting before the stop"
            )

    # 2. the transferred bytes add up
    total = m.sync_bytes_dropped + sum(p.bytes_transferred for p in m.sync_passes)
    if m.bytes_transferred != total:
        issues.append(
            f"BYTES_MISMATCH: {m.bytes_transferred} bytes transferred, the passes add up to {total}"
        )

    # 3. a warm cutover followed the rule it records (data before ADV-16 records none)
    if warm and m.converged_by is not None:
        before = [p for p in m.sync_passes if p.kind != SyncPassKind.final]
        last = before[-1] if before else None
        rule = m.converged_by
        if rule == "threshold":
            ok = last is not None and last.bytes_changed <= plan.convergence_threshold_bytes
            why = f"the last pass changed {_gib(last.bytes_changed) if last else 'nothing'}"
        elif rule == "max_passes":
            ok = last is not None and last.number >= plan.max_sync_passes
            why = f"{last.number if last else 0} passes, the limit is {plan.max_sync_passes}"
        elif rule == "slo":
            ok = m.estimate is not None and m.estimate.downtime_s <= plan.downtime_slo_s
            why = "the estimate recorded then does not fit the SLO"
        else:  # force
            ok = m.force_cutover is not None
            why = "no Force cutover is recorded"
        if not ok:
            issues.append(f"CUTOVER_NOT_CONVERGED: converged by {rule}, but {why}")

    # 4. the downtime contains the final sync
    if (
        warm
        and not final_unrecorded
        and final is not None
        and final.duration_s is not None
        and m.actual_downtime_s is not None
        and m.actual_downtime_s < final.duration_s
    ):
        issues.append(
            f"DOWNTIME_BELOW_FINAL_SYNC: {m.actual_downtime_s:.0f} s of downtime around a "
            f"{final.duration_s:.0f} s final sync"
        )

    # 5. the estimate's parts add up to it
    est = m.estimate
    if est is not None and est.downtime_parts:
        parts = sum(est.downtime_parts.values())
        if abs(parts - est.downtime_s) > PARTS_TOLERANCE_S:
            issues.append(
                f"ESTIMATE_PARTS_MISMATCH: the parts add up to {parts:.0f} s, "
                f"the estimate says {est.downtime_s:.0f} s"
            )
    return issues
