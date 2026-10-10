"""In-process event bus and the ``emit`` helper (SDD §4.3, §12 SSE).

Persisted events get a monotonically increasing ``seq`` from the store; ephemeral events
(``migration.progress``, ``migration.log``, ``heartbeat``) are published with ``seq == 0`` and never
stored. A subscription replays persisted events after ``since`` from the store and then streams
live events, delivering every persisted event exactly once and in ``seq`` order.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic_core import to_jsonable_python

from .domain.models import Event
from .store import Store

log = logging.getLogger(__name__)

PERSISTED_KINDS = frozenset(
    {
        "plan.created",
        "plan.updated",
        "plan.validated",
        "plan.started",
        "plan.paused",
        "plan.completed",
        "wave.started",
        "wave.completed",
        "migration.created",
        "migration.phase",
        "migration.sync_pass",
        "migration.downtime_started",
        "migration.downtime_ended",
        "migration.error",
        "migration.approved",
        "migration.action",
        "advisor.strategy",
        "advisor.classification",
        "advisor.verification",
        "advisor.similar_incidents",
        "memory.lesson_saved",
        "provider.created",
        "provider.updated",
        "provider.credentials_updated",
        "provider.deleted",
        "provider.checked",
        "auth.denied",
    }
)
EPHEMERAL_KINDS = frozenset({"migration.progress", "migration.log", "heartbeat"})

_LAGGED = object()


class Subscription:
    """Async iterator over events; registered with the bus as soon as it is created."""

    def __init__(self, bus: EventBus, since: int, max_queue: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max_queue)
        self._last = max(0, int(since))
        self._replay_pending = bus.store is not None
        self._buffer: list[Event] = []
        self._closed = False
        bus._subscribers.add(self)

    @property
    def last_seq(self) -> int:
        return self._last

    def _offer(self, event: Event) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Slow consumer: drop what is queued and re-read persisted events from the store.
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(_LAGGED)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        while True:
            if self._closed:
                raise StopAsyncIteration
            if self._buffer:
                event = self._buffer.pop(0)
                self._last = max(self._last, event.seq)
                return event
            if self._replay_pending and self._bus.store is not None:
                page = await asyncio.to_thread(
                    self._bus.store.events, since_seq=self._last, limit=self._bus.replay_page
                )
                self._buffer.extend(page)
                if len(page) < self._bus.replay_page:
                    self._replay_pending = False
                continue
            item = await self._queue.get()
            if item is _LAGGED:
                self._replay_pending = self._bus.store is not None
                continue
            event: Event = item
            if event.seq:
                if event.seq <= self._last:
                    continue  # already delivered by the replay
                self._last = event.seq
            return event

    async def aclose(self) -> None:
        self._closed = True
        self._bus._subscribers.discard(self)


class EventBus:
    def __init__(
        self, store: Store | None = None, *, max_queue: int = 10000, replay_page: int = 500
    ) -> None:
        self.store = store
        self.max_queue = max_queue
        self.replay_page = replay_page
        self._subscribers: set[Subscription] = set()
        self._emit_lock: asyncio.Lock | None = None

    @property
    def emit_lock(self) -> asyncio.Lock:
        if self._emit_lock is None:
            self._emit_lock = asyncio.Lock()
        return self._emit_lock

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: Event, persist: bool) -> None:
        """Fan ``event`` out to live subscribers (non-blocking).

        ``persist`` states whether the event was stored (it then carries its ``seq``).
        """
        if persist and not event.seq:
            raise ValueError("a persisted event must carry its store sequence number")
        for sub in list(self._subscribers):
            sub._offer(event)

    def subscribe(self, since: int = 0) -> Subscription:
        """Replay persisted events with ``seq > since`` from the store, then stream live ones."""
        return Subscription(self, since, self.max_queue)


async def emit(
    store: Store | None,
    bus: EventBus,
    kind: str,
    message: str,
    *,
    plan_id: str | None = None,
    migration_id: str | None = None,
    actor: str = "system",
    data: dict[str, Any] | None = None,
    persist: bool = True,
) -> Event:
    """Build an event, persist it (unless ephemeral) and publish it."""
    if kind not in PERSISTED_KINDS and kind not in EPHEMERAL_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    event = Event(
        kind=kind,
        plan_id=plan_id,
        migration_id=migration_id,
        actor=actor,
        message=message,
        data=to_jsonable_python(data or {}),
    )
    if persist and kind in PERSISTED_KINDS and store is not None:
        # Serialize persist+publish so live delivery order equals seq order.
        async with bus.emit_lock:
            event = await asyncio.to_thread(store.append_event, event)
            bus.publish(event, persist=True)
    else:
        bus.publish(event, persist=False)
    return event
