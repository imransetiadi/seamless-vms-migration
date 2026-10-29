"""In-process event bus and the ``emit`` helper (SDD §4.3, §12 SSE).

Persisted events get a monotonically increasing ``seq`` from the store; ephemeral events
(``migration.progress``, ``migration.log``, ``heartbeat``) are published with ``seq == 0`` and never
stored. A subscription replays persisted events after ``since`` from the store and then streams
live events, delivering every persisted event exactly once and in ``seq`` order.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Any

from pydantic_core import to_jsonable_python

from .domain.models import Event, utcnow
from .store import Store

log = logging.getLogger(__name__)

#: Audit events (SDD §14.5): ``GET /events?audit=true`` returns only these.
AUDIT_KINDS = frozenset(
    {
        "recommendation.created",
        "decision.auto_applied",
        "decision.pending_approval",
        "decision.approved",
        "decision.overridden",
        "decision.below_threshold",
        "decision.fallback",
        "policy.created",
        "policy.updated",
        "policy.deleted",
        # a cutover before convergence, with its approver and reason (SDD §5.4, §14.5, ADV-16)
        "migration.force_cutover",
    }
)
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
        "migration.force_cutover",
        "migration.data_inconsistent",
        "migration.source_running",
        "migration.source_stopped",
        # a cutover that waits for GPU capacity, once when the wait begins and once when it ends
        # (SDD §4.3, §5.4 rule 5, §22.7)
        "migration.gpu_wait",
        "plan.finalized",
        "plan.cancelled",
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
        # the provider check pinned its conversion host from the console (SDD §4.3, §10, §26.5)
        "conversion_host.key_pinned",
        "conversion_host.key_refused",
        # the hook catalog's changes (SDD §4.3, §23.3)
        "hook.created",
        "hook.updated",
        "hook.deleted",
        # the notification catalog's and the subscriptions' changes (SDD §4.3, §24.3, §24.4)
        "channel.created",
        "channel.updated",
        "channel.deleted",
        "subscription.created",
        "subscription.deleted",
        "auth.denied",
        "user.created",
        "events.pruned",
        *AUDIT_KINDS,
    }
)
EPHEMERAL_KINDS = frozenset({"migration.progress", "migration.log", "heartbeat"})

_LAGGED = object()

#: How long a stream holds what follows a skipped seq the store does not hold yet, and how often it
#: reads the store again meanwhile: another process's insert may commit after a later one (SDD §12)
HOLE_WAIT_S = 2.0
HOLE_POLL_S = 0.05


class Subscription:
    """Async iterator over events; registered with the bus as soon as it is created."""

    def __init__(self, bus: EventBus, since: int, max_queue: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max_queue)
        self._last = max(0, int(since))
        self._replay_pending = bus.store is not None
        self._buffer: list[Event] = []
        self._closed = False
        #: the gap held for: the seq sent before it, and since when (time.monotonic)
        self._hole: tuple[int, float] | None = None
        bus._subscribers.add(self)

    @property
    def last_seq(self) -> int:
        return self._last

    def _held_from(self, page: list[Event]) -> int | None:
        """Where ``page`` stops for now: at the first event after a skipped seq that may still be
        committed - the event created less than HOLE_WAIT_S ago, while that gap's wait lasts (SDD
        §12). None when nothing is held: no gap, an older one, one right after seq 0 (a store whose
        events were all deleted numbers on from where they ended), or one waited for long enough."""
        before = self._last
        for i, event in enumerate(page):
            recent = (utcnow() - event.ts).total_seconds() < HOLE_WAIT_S
            if before and event.seq > before + 1 and recent:
                now = time.monotonic()
                if self._hole is None or self._hole[0] != before:
                    self._hole = (before, now)
                if now - self._hole[1] < HOLE_WAIT_S:
                    return i
            before = event.seq
        return None

    def _offer(self, event: Event | object) -> None:
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
                held = self._held_from(page)
                if held is not None:
                    # an insert of another process may not be committed yet: send what comes
                    # before it, and read again a moment later, in seq order (SDD §12)
                    self._buffer.extend(page[:held])
                    if not held:
                        await asyncio.sleep(HOLE_POLL_S)
                    continue
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
                if event.seq > self._last + 1 and self._bus.store is not None:
                    # seqs skipped: another process wrote events to the store meanwhile (the CLI
                    # beside the server, SDD §12) - read them first, this one with them, in order
                    self._replay_pending = True
                    continue
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
        #: the newest seq published here: a newer one in the store came from another process
        self._published = 0

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
        self._published = max(self._published, event.seq)
        for sub in list(self._subscribers):
            sub._offer(event)

    async def catch_up(self) -> None:
        """Have every stream read what another process wrote to the store - the CLI beside a
        running server (SDD §12), called each tick: one look at the store's newest seq, and the
        streams read the store only when it is ahead of the newest published here."""
        if self.store is None or not self._subscribers:
            return
        newest = await asyncio.to_thread(self.store.max_seq)
        if newest <= self._published:
            return
        self._published = newest
        for sub in list(self._subscribers):
            sub._offer(_LAGGED)

    def subscribe(self, since: int = 0) -> Subscription:
        """Replay persisted events with ``seq > since`` from the store, then stream live ones."""
        return Subscription(self, since, self.max_queue)


def new_event(
    kind: str,
    message: str,
    *,
    plan_id: str | None = None,
    migration_id: str | None = None,
    actor: str = "system",
    data: dict[str, Any] | None = None,
) -> Event:
    """An event of a known kind, its data made JSON-ready (SDD §4)."""
    if kind not in PERSISTED_KINDS and kind not in EPHEMERAL_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    return Event(
        kind=kind,
        plan_id=plan_id,
        migration_id=migration_id,
        actor=actor,
        message=message,
        data=to_jsonable_python(data or {}),
    )


async def put_and_emit(
    store: Store, bus: EventBus, kind: str, model: Any, expected_version: int, event: Event
) -> int:
    """Write ``model`` and ``event`` in one store transaction, then publish the event, in seq order
    with every other (SDD §8.1, §11, review RV21-04): a store error leaves neither, and the event is
    timed as it is written (SDD §4, review RV52-O5) - put_all_and_emit's one document's case."""
    [version] = await put_all_and_emit(store, bus, [(kind, model, expected_version)], [event])
    return version


def _written_now(events: Sequence[Event]) -> list[Event]:
    """``events`` timed now, under the emit lock that writes and numbers them - one transaction's
    sharing one time -, so their order in time is their seq order (SDD §4): one built before it
    waited for the lock was listed after a newer one by the Events page, which sorts by time."""
    now = utcnow()
    return [event.model_copy(update={"ts": now}) for event in events]


async def put_all_and_emit(
    store: Store,
    bus: EventBus,
    writes: Sequence[tuple[str, Any, int]],
    events: Sequence[Event],
) -> list[int]:
    """Write ``writes`` and ``events`` in one store transaction, then publish the events, in seq
    order with every other (SDD §8, §11, §14.6): a store error leaves none of them, published or
    written."""
    async with bus.emit_lock:
        versions, written = await asyncio.to_thread(
            store.put_all_with_events, writes, _written_now(events)
        )
        for event in written:
            bus.publish(event, persist=True)
    return versions


async def delete_and_emit(
    store: Store,
    bus: EventBus,
    kind: str,
    id_: str,
    expected_version: int,
    events: Sequence[Event],
) -> None:
    """Delete the document at ``expected_version`` and append ``events`` in one store
    transaction, then publish them (SDD §11, §14.5, review RV51-11): a store error leaves the
    document and no event."""
    async with bus.emit_lock:
        written = await asyncio.to_thread(
            store.delete_with_events, kind, id_, expected_version, _written_now(events)
        )
        for event in written:
            bus.publish(event, persist=True)


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
    event = new_event(
        kind, message, plan_id=plan_id, migration_id=migration_id, actor=actor, data=data
    )
    if persist and kind in PERSISTED_KINDS and store is not None:
        # Serialize persist+publish so live delivery order equals seq order.
        async with bus.emit_lock:
            [event] = _written_now([event])
            event = await asyncio.to_thread(store.append_event, event)
            bus.publish(event, persist=True)
    else:
        bus.publish(event, persist=False)
    return event
