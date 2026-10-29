"""Prometheus text exposition for ``GET /metrics`` (SDD §18)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .domain import fsm
from .domain.enums import Phase
from .domain.models import Migration

#: where what a warm pass found still stands before the cutover (SDD §7.2, §16)
REFUSABLE = frozenset({Phase.precopy, Phase.syncing, Phase.awaiting_cutover})


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _num(value: float) -> str:
    return (
        f"{value:.6g}" if isinstance(value, float) and not value.is_integer() else str(int(value))
    )


def render_metrics(
    migrations: Iterable[Migration],
    step_stats: Mapping[str, list[float]],
    advisor_calls: Mapping[tuple[str, str], int],
    tick_stats: Mapping[str, float] | None = None,
    notifications: Mapping[tuple[str, str], int] | None = None,
    *,
    leading: bool = False,
    lease_epoch: int | None = None,
) -> str:
    """The exposition (SDD §18). ``leading``: whether this replica leads the orchestrator;
    ``lease_epoch``: the epoch in the lease's row as this scrape read it, None while nobody took
    the lease (§25.8)."""
    migrations = list(migrations)
    lines: list[str] = []

    def family(name: str, kind: str, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")

    family("seamless_migrations", "gauge", "Migrations per phase.")
    counts = {p.value: 0 for p in Phase}
    for m in migrations:
        counts[str(m.phase)] += 1
    for phase, count in counts.items():
        lines.append(f'seamless_migrations{{phase="{phase}"}} {count}')

    # SDD §18: the migrations the dashboard lists as "Cutover will refuse" (§16)
    family(
        "seamless_cutover_refusals",
        "gauge",
        "Migrations whose cutover will refuse what their last pass found.",
    )
    refusing = sum(1 for m in migrations if m.phase in REFUSABLE and m.cutover_refusals)
    lines.append(f"seamless_cutover_refusals {refusing}")

    # SDD §18: the migrations whose cutover waits for GPU capacity, their sources running (§5.4
    # rule 5, §22.7) - a wait leaves with the phases it lives in (domain.fsm)
    family("seamless_gpu_waits", "gauge", "Migrations whose cutover waits for GPU capacity.")
    lines.append(f"seamless_gpu_waits {sum(1 for m in migrations if m.gpu_wait is not None)}")

    # SDD §18: the notification messages recorded per channel kind and outcome (§8.7, §24.7)
    family(
        "seamless_notifications_total",
        "counter",
        "Notification messages recorded as sent or failed, by channel kind.",
    )
    for (kind, outcome), count in sorted((notifications or {}).items()):
        lines.append(
            f'seamless_notifications_total{{kind="{_escape(kind)}",outcome="{_escape(outcome)}"}} '
            f"{count}"
        )

    family("seamless_bytes_transferred_total", "counter", "Bytes transferred by migrations.")
    lines.append(f"seamless_bytes_transferred_total {sum(m.bytes_transferred for m in migrations)}")

    downtimes = [
        float(m.actual_downtime_s)
        for m in migrations
        if m.phase in fsm.SUCCESS_PHASES and m.actual_downtime_s is not None
    ]
    family("seamless_downtime_seconds", "summary", "Measured downtime of completed migrations.")
    lines.append(f"seamless_downtime_seconds_sum {_num(sum(downtimes))}")
    lines.append(f"seamless_downtime_seconds_count {len(downtimes)}")
    family("seamless_downtime_seconds_max", "gauge", "Longest measured downtime.")
    lines.append(f"seamless_downtime_seconds_max {_num(max(downtimes) if downtimes else 0)}")

    ticks = tick_stats or {}
    family("seamless_tick_seconds", "summary", "Duration of orchestrator ticks.")
    lines.append(f"seamless_tick_seconds_sum {_num(float(ticks.get('sum', 0.0)))}")
    lines.append(f"seamless_tick_seconds_count {int(ticks.get('count', 0))}")
    family("seamless_tick_seconds_max", "gauge", "Longest orchestrator tick since start.")
    lines.append(f"seamless_tick_seconds_max {_num(float(ticks.get('max', 0.0)))}")
    family("seamless_tick_slow_total", "counter", "Ticks that took more than half of tick_s.")
    lines.append(f"seamless_tick_slow_total {int(ticks.get('slow', 0))}")

    family("seamless_step_duration_seconds", "summary", "Duration of executor steps.")
    for step, (total, count) in sorted(step_stats.items()):
        label = _escape(step)
        lines.append(f'seamless_step_duration_seconds_sum{{step="{label}"}} {_num(total)}')
        lines.append(f'seamless_step_duration_seconds_count{{step="{label}"}} {int(count)}')

    family("seamless_advisor_calls_total", "counter", "Jev tool calls by outcome.")
    for (tool, outcome), count in sorted(advisor_calls.items()):
        lines.append(
            f'seamless_advisor_calls_total{{tool="{_escape(tool)}",outcome="{_escape(outcome)}"}} '
            f"{count}"
        )

    # SDD §18, §25.8: the orchestrator's leadership, without a label - no replica's holder or
    # address, each a host name, leaves through /metrics
    family("seamless_leader", "gauge", "1 while this replica leads the orchestrator, else 0.")
    lines.append(f"seamless_leader {1 if leading else 0}")
    family(
        "seamless_leader_epoch",
        "gauge",
        "The epoch of the orchestrator's lease as this replica read it; 0 before any lease.",
    )
    lines.append(f"seamless_leader_epoch {0 if lease_epoch is None else int(lease_epoch)}")
    return "\n".join(lines) + "\n"


def advisor_calls(jev: Any) -> Mapping[tuple[str, str], int]:
    return dict(getattr(jev, "calls", {}) or {})
