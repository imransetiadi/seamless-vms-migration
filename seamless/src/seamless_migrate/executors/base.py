"""Executor contract (SDD §7.1)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ..config import Settings
from ..domain.enums import Strategy
from ..domain.models import Migration, Plan, Provider, SyncPass


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
    log: Callable[[str], Awaitable[None]]
    #: step options, e.g. ``{"delete_source": True}`` for FINALIZE (extension of SDD §7.1)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    sync_pass: SyncPass | None = None
    destination_server_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class TransientStepError(Exception):
    """Retried with backoff (``max_step_retries``)."""


class PermanentStepError(Exception):
    """The migration moves to ``failed``."""


@runtime_checkable
class Executor(Protocol):
    name: str

    def supports(self, strategy: Strategy) -> bool: ...

    async def prestage(self, plan: Plan, source: Provider, destination: Provider) -> None: ...

    async def run(self, step: StepName, ctx: StepContext) -> StepResult: ...
