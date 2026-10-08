"""Prometheus text exposition for ``GET /metrics`` (SDD §18)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .domain import fsm
from .domain.enums import Phase
from .domain.models import Migration


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
) -> str:
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
    return "\n".join(lines) + "\n"


def advisor_calls(jev: Any) -> Mapping[tuple[str, str], int]:
    return dict(getattr(jev, "calls", {}) or {})
