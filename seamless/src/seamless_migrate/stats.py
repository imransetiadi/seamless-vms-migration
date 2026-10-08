"""Dashboard aggregates for ``GET /stats`` (SDD §12)."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from pydantic import BaseModel

from .domain import fsm
from .domain.enums import Phase
from .domain.models import Migration, Plan, utcnow

IN_PROGRESS = frozenset(
    {
        Phase.validating,
        Phase.precopy,
        Phase.syncing,
        Phase.awaiting_cutover,
        Phase.cutover,
        Phase.verifying,
        Phase.rolling_back,
    }
)
SERIES_MINUTES = 60


class ThroughputPoint(BaseModel):
    ts: datetime
    bps: float


class Stats(BaseModel):
    total: int
    by_phase: dict[str, int]
    completed: int
    failed: int
    in_progress: int
    bytes_transferred: int
    avg_downtime_s: float | None
    p95_downtime_s: float | None
    max_downtime_s: float | None
    slo_compliance_pct: float | None
    downtime_by_strategy: dict[str, float]
    throughput_series: list[ThroughputPoint]


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def compute_stats(
    migrations: Iterable[Migration], plans: Mapping[str, Plan], now: datetime | None = None
) -> Stats:
    migrations = list(migrations)
    now = now or utcnow()
    by_phase = {p.value: 0 for p in Phase}
    for m in migrations:
        by_phase[str(m.phase)] += 1

    measured = [
        m for m in migrations if m.phase in fsm.SUCCESS_PHASES and m.actual_downtime_s is not None
    ]
    downtimes = [float(m.actual_downtime_s or 0.0) for m in measured]
    within = [
        m
        for m in measured
        if m.plan_id in plans and (m.actual_downtime_s or 0.0) <= plans[m.plan_id].downtime_slo_s
    ]
    judged = [m for m in measured if m.plan_id in plans]
    per_strategy: dict[str, list[float]] = defaultdict(list)
    for m in measured:
        per_strategy[str(m.strategy)].append(float(m.actual_downtime_s or 0.0))

    end = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    start = end - timedelta(minutes=SERIES_MINUTES)
    buckets = [0.0] * SERIES_MINUTES
    for m in migrations:
        for sync_pass in m.sync_passes:
            ended = sync_pass.ended_at
            if ended is None or not (start <= ended < end):
                continue
            index = int((ended - start).total_seconds() // 60)
            buckets[index] += sync_pass.bytes_transferred
    series = [
        ThroughputPoint(ts=start + timedelta(minutes=i), bps=round(total / 60.0, 3))
        for i, total in enumerate(buckets)
    ]
    return Stats(
        total=len(migrations),
        by_phase=by_phase,
        completed=sum(1 for m in migrations if m.phase in fsm.SUCCESS_PHASES),
        failed=by_phase[Phase.failed.value],
        in_progress=sum(1 for m in migrations if m.phase in IN_PROGRESS),
        bytes_transferred=sum(m.bytes_transferred for m in migrations),
        avg_downtime_s=sum(downtimes) / len(downtimes) if downtimes else None,
        p95_downtime_s=percentile(downtimes, 95),
        max_downtime_s=max(downtimes) if downtimes else None,
        slo_compliance_pct=round(100.0 * len(within) / len(judged), 2) if judged else None,
        downtime_by_strategy={k: sum(v) / len(v) for k, v in sorted(per_strategy.items())},
        throughput_series=series,
    )
