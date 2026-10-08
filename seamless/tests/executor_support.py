"""Helpers for executor tests: a recording StepContext."""

from __future__ import annotations

from dataclasses import dataclass, field

from seamless_migrate.config import Settings
from seamless_migrate.domain.models import Migration, Plan, Provider
from seamless_migrate.executors.base import StepContext


@dataclass
class Recorder:
    progress: list[tuple[float, int, int]] = field(default_factory=list)
    downtime_marks: int = 0
    logs: list[str] = field(default_factory=list)

    async def report_progress(self, pct: float, done: int, total: int) -> None:
        self.progress.append((pct, done, total))

    async def mark_downtime_start(self) -> None:
        self.downtime_marks += 1

    async def log(self, line: str) -> None:
        self.logs.append(line)


def make_ctx(
    plan: Plan,
    migration: Migration,
    source: Provider,
    destination: Provider,
    settings: Settings,
    recorder: Recorder | None = None,
    **options,
) -> tuple[StepContext, Recorder]:
    rec = recorder or Recorder()
    ctx = StepContext(
        plan=plan,
        migration=migration,
        source=source,
        destination=destination,
        settings=settings,
        report_progress=rec.report_progress,
        mark_downtime_start=rec.mark_downtime_start,
        log=rec.log,
        options=dict(options),
    )
    return ctx, rec
