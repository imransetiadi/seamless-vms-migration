import asyncio

import pytest

from seamless_migrate.domain.models import Event
from seamless_migrate.events import EPHEMERAL_KINDS, PERSISTED_KINDS, EventBus, emit
from seamless_migrate.store import Store


async def _take(agen, n: int, wait_s: float = 2.0) -> list[Event]:
    out: list[Event] = []

    async def run() -> None:
        async for ev in agen:
            out.append(ev)
            if len(out) == n:
                return

    await asyncio.wait_for(run(), wait_s)
    return out


def test_kind_catalog_matches_sdd():
    assert PERSISTED_KINDS == {
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
        "provider.deleted",
        "provider.checked",
        "auth.denied",
    }
    assert EPHEMERAL_KINDS == {"migration.progress", "migration.log", "heartbeat"}


async def test_emit_persists_and_publishes(store: Store):
    bus = EventBus(store)
    ev = await emit(
        store, bus, "plan.created", "created", plan_id="plan-a", actor="alice", data={"n": 1}
    )
    assert ev.seq > 0
    [stored] = store.events(since_seq=0)
    assert (stored.seq, stored.kind, stored.actor, stored.data) == (
        ev.seq,
        "plan.created",
        "alice",
        {"n": 1},
    )
    with pytest.raises(ValueError):
        await emit(store, bus, "plan.exploded", "nope")


async def test_ephemeral_events_not_persisted(store: Store):
    bus = EventBus(store)
    agen = bus.subscribe(since=store.max_seq())
    first = asyncio.ensure_future(_take(agen, 2))
    await asyncio.sleep(0)
    progress = await emit(store, bus, "migration.progress", "42%", migration_id="mig-1")
    # persist=True is ignored for ephemeral kinds; persist=False skips the DB for any kind
    log = await emit(store, bus, "migration.log", "line", migration_id="mig-1", persist=True)
    assert progress.seq == 0 and log.seq == 0
    assert store.events(since_seq=0) == []
    received = await first
    assert [e.kind for e in received] == ["migration.progress", "migration.log"]
    await agen.aclose()


async def test_subscribe_replays_since_then_live(store: Store):
    bus = EventBus(store, replay_page=2)
    old = [await emit(store, bus, "plan.updated", f"old-{i}", plan_id="p") for i in range(5)]
    agen = bus.subscribe(since=old[1].seq)
    task = asyncio.ensure_future(_take(agen, 5))
    await asyncio.sleep(0.05)
    live = await emit(store, bus, "plan.updated", "live", plan_id="p")
    eph = await emit(store, bus, "migration.progress", "eph")
    got = await task
    assert [e.message for e in got] == ["old-2", "old-3", "old-4", "live", "eph"]
    assert got[3].seq == live.seq and eph.seq == 0
    await agen.aclose()


async def test_subscribe_has_no_duplicates_when_live_overlaps_replay(store: Store):
    """Events committed while a subscriber replays are delivered exactly once."""
    bus = EventBus(store, replay_page=1)
    for i in range(3):
        await emit(store, bus, "plan.updated", f"e{i}")
    agen = bus.subscribe(since=0)
    collected: list[Event] = []

    async def consume() -> None:
        async for ev in agen:
            collected.append(ev)
            if len(collected) == 1:
                # publish more while the replay is still in progress
                for j in range(3):
                    await emit(store, bus, "plan.updated", f"x{j}")
            if len(collected) == 6:
                return

    await asyncio.wait_for(consume(), 2)
    seqs = [e.seq for e in collected]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)) == 6
    assert [e.message for e in collected] == ["e0", "e1", "e2", "x0", "x1", "x2"]
    # the live copies of x0..x2 that were queued during the replay are dropped
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agen.__anext__(), 0.2)
    await agen.aclose()


async def test_slow_subscriber_recovers_persisted_events(store: Store):
    bus = EventBus(store, max_queue=2)
    agen = bus.subscribe(since=store.max_seq())
    waiter = asyncio.ensure_future(_take(agen, 1))
    await asyncio.sleep(0)
    first = await emit(store, bus, "plan.updated", "a")
    assert [e.seq for e in await waiter] == [first.seq]
    # overflow the queue while nobody is reading
    for i in range(5):
        await emit(store, bus, "plan.updated", f"burst-{i}")
    rest = await _take(agen, 5)
    assert [e.message for e in rest] == [f"burst-{i}" for i in range(5)]
    await agen.aclose()
    assert bus.subscriber_count == 0
