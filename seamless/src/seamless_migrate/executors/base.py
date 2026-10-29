"""Executor contract (SDD §7.1)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

from ..config import Settings
from ..domain.enums import Strategy
from ..domain.models import Migration, Plan, Provider, SyncPass

T = TypeVar("T")

log = logging.getLogger(__name__)


class StepName(StrEnum):
    PRESTAGE = "prestage"
    PRECOPY = "precopy"
    SYNC = "sync"
    CUTOVER = "cutover"
    ROLLBACK = "rollback"
    FINALIZE = "finalize"


class HostKeys(Protocol):
    """What a run records of its conversion hosts' keys, through the orchestrator (SDD §7.2
    "Conversion host keys", §26.4, §26.8)."""

    async def pin_from_console(self, provider: Provider) -> Provider:
        """The provider once the provider check's path (§10) pinned its conversion host from its
        console - read once, for a host without a pin -; as it is stored when nothing was
        pinned."""
        ...

    async def pin_injected(
        self, provider: Provider, side: str, server_id: str, keys: Sequence[str]
    ) -> None:
        """The server a pre-staging's role created with the key the run injected, pinned with
        those ``keys`` - source injected - and conversion_host.key_pinned in one transaction
        (§4.3, §26.3)."""
        ...

    async def refused(self, message: str, data: Mapping[str, Any]) -> None:
        """conversion_host.key_refused with ``message`` and ``data`` - the provider, side,
        server, pinned and offered of a refusal in the run's output -, the run's plan and
        migration added (§4.3, §7.2)."""
        ...


class NoHostKeys:
    """The book of a run the orchestrator handed none: it reads no console and records
    nothing."""

    async def pin_from_console(self, provider: Provider) -> Provider:
        return provider

    async def pin_injected(
        self, provider: Provider, side: str, server_id: str, keys: Sequence[str]
    ) -> None:
        return None

    async def refused(self, message: str, data: Mapping[str, Any]) -> None:
        return None


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
    #: what the run records of its conversion hosts' keys: the orchestrator's book of the step's
    #: plan and migration (SDD §7.2 "Conversion host keys"; extension of SDD §7.1)
    host_keys: HostKeys | None = None


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


def _sigkill(pgid: int) -> None:
    """SIGKILL to the process group ``pgid``; one already gone is no error."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


class ProcessGroups:
    """The book of the process groups of the playbooks a replica runs (SDD §7.1, §25.4 layer 3).

    Each playbook - a step's and a playbook hook's - starts in a session of its own, so its pid
    is its process group's id; its runner registers the group from the playbook's start until it
    ended. :meth:`kill` sends SIGKILL to every registered group from whatever thread calls it - a
    deposition's, never waiting for the event loop - and closes the book: a group registered
    after it is killed at once, until :meth:`open`. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._groups: set[int] = set()
        self._closed = False

    def add(self, pgid: int) -> None:
        """Register the group of a playbook that started; killed at once while the book is closed:
        a replica that does not lead runs no playbook (SDD §7.1)."""
        with self._lock:
            if not self._closed:
                self._groups.add(pgid)
                return
        log.warning("killed the process group %d of a playbook: this replica does not lead", pgid)
        _sigkill(pgid)

    def discard(self, pgid: int) -> None:
        """Forget the group of a playbook that ended."""
        with self._lock:
            self._groups.discard(pgid)

    @contextlib.contextmanager
    def running(self, pgid: int) -> Iterator[None]:
        """The group registered while the block runs - the playbook's life."""
        self.add(pgid)
        try:
            yield
        finally:
            self.discard(pgid)

    def kill(self) -> list[int]:
        """Close the book and send SIGKILL to every group registered (SDD §25.4 layer 3); the
        groups signalled. Their runners discard them once they ended."""
        with self._lock:
            self._closed = True
            groups = sorted(self._groups)
        for pgid in groups:
            _sigkill(pgid)
        return groups

    def open(self) -> None:
        """Accept groups again: the replica leads (SDD §8)."""
        with self._lock:
            self._closed = False

    def groups(self) -> frozenset[int]:
        """The groups registered now."""
        with self._lock:
            return frozenset(self._groups)


@runtime_checkable
class Executor(Protocol):
    name: str

    def supports(self, strategy: Strategy) -> bool:
        """Whether this executor runs the strategy."""

    async def prestage(self, plan: Plan, source: Provider, destination: Provider) -> None:
        """Prestage the plan's resources in the destination before its migrations (SDD §7.1)."""

    async def run(self, step: StepName, ctx: StepContext) -> StepResult:
        """Run one step of a migration (SDD §7.1)."""
