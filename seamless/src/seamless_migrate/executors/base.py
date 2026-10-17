"""Executor contract (SDD §7.1)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

from ..config import Settings
from ..domain.enums import Strategy
from ..domain.models import Migration, Plan, Provider, SyncPass

T = TypeVar("T")


class StepName(StrEnum):
    PRESTAGE = "prestage"
    PRECOPY = "precopy"
    SYNC = "sync"
    CUTOVER = "cutover"
    ROLLBACK = "rollback"
    FINALIZE = "finalize"


@dataclass
class StepContext:
    plan: Plan
    migration: Migration
    source: Provider
    destination: Provider
    settings: Settings
    report_progress: Callable[[float, int, int], Awaitable[None]]  # pct, bytes_done, bytes_total
    #: ``await mark_downtime_start()`` or ``mark_downtime_start(at=<datetime>)`` when the
    #: executor learned the exact moment the source stopped (idempotent: first call wins)
    mark_downtime_start: Callable[..., Awaitable[None]]
    #: ``await mark_downtime_end()`` once the source runs again - a rollback's power-on: the clock
    #: closes then, not when the whole step succeeds (SDD §5.2; idempotent)
    mark_downtime_end: Callable[[], Awaitable[None]]
    log: Callable[[str], Awaitable[None]]
    #: step options, e.g. ``{"delete_source": True}`` for FINALIZE (extension of SDD §7.1)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    sync_pass: SyncPass | None = None
    destination_server_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    #: a warm pre-copy or sync pass: what it met that the cutover refuses before the source
    #: stops, for the migration's cutover_refusals (SDD §7.2); None when the step learned nothing
    cutover_refusals: list[str] | None = None


class TransientStepError(Exception):
    """Retried with backoff (``max_step_retries``)."""


class PermanentStepError(Exception):
    """The migration moves to ``failed``."""

    def __init__(self, *args: object, details: dict[str, Any] | None = None) -> None:
        super().__init__(*args)
        #: what the step reported before it failed, e.g. the simulator's ``downtime_model_s``
        #: (SDD §5.2, §7.1)
        self.details: dict[str, Any] = dict(details or {})


async def outlive_cancel(work: Awaitable[T]) -> T:
    """Await ``work`` to its end however often the step is cancelled, then re-raise the cancel:
    what a step began and no cancel can undo - a provider call in flight, the downtime clock a stop
    that may have happened opens - finishes before the step ends (SDD §7.2, §7.3; audit EX-01,
    EX-02)."""
    task = asyncio.ensure_future(work)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
    if cancelled is not None:
        if not task.cancelled():
            task.exception()  # retrieved: the cancelled step reports nothing of it
        raise cancelled
    return task.result()


@runtime_checkable
class Executor(Protocol):
    name: str

    def supports(self, strategy: Strategy) -> bool: ...

    async def prestage(self, plan: Plan, source: Provider, destination: Provider) -> None: ...

    async def run(self, step: StepName, ctx: StepContext) -> StepResult: ...
