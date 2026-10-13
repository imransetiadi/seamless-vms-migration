import asyncio
from datetime import timedelta

import pytest
import sqlalchemy as sa

from seamless_migrate import events as events_module
from seamless_migrate.domain.models import Event, utcnow
from seamless_migrate.events import EPHEMERAL_KINDS, PERSISTED_KINDS, EventBus, emit
from seamless_migrate.store import Store
from seamless_migrate.store import events as events_table


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
        "migration.force_cutover",
        "migration.data_inconsistent",
        "migration.source_running",
        "migration.source_stopped",
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
        "auth.denied",
        "events.pruned",
        # audit events (SDD §14.5)
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


async def _from_another_process(store: Store, message: str) -> Event:
    """An event the CLI beside the server writes: to the same store, through its own bus."""
    return await emit(store, EventBus(store), "plan.updated", message, plan_id="p", actor="cli")


async def test_a_stream_sends_what_another_process_wrote_before_the_next_event(store: Store):
    """SDD §12: an event whose seq skips some comes after the skipped ones, read from the store."""
    bus = EventBus(store)
    agen = bus.subscribe(since=store.max_seq())
    task = asyncio.ensure_future(_take(agen, 3))
    await asyncio.sleep(0.05)
    first = await emit(store, bus, "plan.updated", "server-1", plan_id="p")
    cli = await _from_another_process(store, "cli")
    second = await emit(store, bus, "plan.updated", "server-2", plan_id="p")
    got = await task
    assert [e.message for e in got] == ["server-1", "cli", "server-2"]
    assert [e.seq for e in got] == [first.seq, cli.seq, second.seq]
    # once each: the live copy of server-2 the replay already sent is dropped
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agen.__anext__(), 0.2)
    await agen.aclose()


async def test_a_stream_sends_what_another_process_wrote_while_the_server_emits_nothing(
    store: Store,
):
    """SDD §12: each tick the bus compares the store's newest seq with the newest it published
    and has every stream read what it lacks - and when the store is not ahead no stream reads it."""
    bus = EventBus(store)
    agen = bus.subscribe(since=store.max_seq())
    await emit(store, bus, "plan.updated", "server", plan_id="p")
    assert [e.message for e in await _take(agen, 1)] == ["server"]
    reads: list[int] = []
    events = store.events

    def counted(*args, **kwargs):
        reads.append(1)
        return events(*args, **kwargs)

    store.events = counted  # type: ignore[method-assign]
    await bus.catch_up()  # nothing new: no stream reads the store
    await asyncio.sleep(0.05)
    assert reads == []
    cli = [await _from_another_process(store, f"cli-{i}") for i in range(2)]
    await bus.catch_up()
    got = await _take(agen, 2)
    assert [e.seq for e in got] == [e.seq for e in cli]
    assert len(reads) == 1
    await bus.catch_up()  # sent already: nothing to read again
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agen.__anext__(), 0.2)
    assert len(reads) == 1
    live = await emit(store, bus, "plan.updated", "server-2", plan_id="p")
    assert [e.seq for e in await _take(agen, 1)] == [live.seq]
    assert len(reads) == 1, "the server's own next event comes live, without a read"
    await agen.aclose()


async def test_no_stream_no_look_at_the_store(store: Store):
    """SDD §12: with no stream open the tick does not read the store's newest seq."""
    bus = EventBus(store)
    looked: list[int] = []
    newest = store.max_seq

    def counted() -> int:
        looked.append(1)
        return newest()

    store.max_seq = counted  # type: ignore[method-assign]
    await _from_another_process(store, "cli")
    await bus.catch_up()
    assert looked == []


def _uncommitted(
    store: Store,
    seqs: set[int],
    reads: int | None = None,
    aged: set[int] = frozenset(),
    ahead: set[int] = frozenset(),
):
    """store.events as another process's insert not committed yet shows it (PostgreSQL): ``seqs``
    are missing for the first ``reads`` reads, or for good; ``aged`` events look a minute old, and
    ``ahead`` ones a minute in the future (a writer whose clock is ahead)."""
    events = store.events
    calls = {"n": 0}

    def shifted(e: Event) -> Event:
        if e.seq in aged:
            return e.model_copy(update={"ts": e.ts - timedelta(minutes=1)})
        if e.seq in ahead:
            return e.model_copy(update={"ts": e.ts + timedelta(minutes=1)})
        return e

    def read(*args, **kwargs):
        calls["n"] += 1
        got = events(*args, **kwargs)
        if reads is None or calls["n"] <= reads:
            got = [e for e in got if e.seq not in seqs]
        return [shifted(e) for e in got]

    store.events = read  # type: ignore[method-assign]
    return calls


async def test_a_stream_holds_what_follows_an_insert_not_committed_yet(store: Store):
    """SDD §12: PostgreSQL may commit another process's insert after a later one - the stream holds
    the later event until the earlier one appears, so both come once and in seq order."""
    bus = EventBus(store)
    agen = bus.subscribe(since=store.max_seq())
    first = await emit(store, bus, "plan.updated", "server-1", plan_id="p")
    assert [e.seq for e in await _take(agen, 1)] == [first.seq]
    cli = await _from_another_process(store, "cli")
    calls = _uncommitted(store, {cli.seq}, reads=2)
    second = await emit(store, bus, "plan.updated", "server-2", plan_id="p")
    got = await _take(agen, 2)
    assert [e.seq for e in got] == [cli.seq, second.seq]
    assert calls["n"] == 3  # two reads without it, a moment apart, then the one with it
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agen.__anext__(), 0.2)
    await agen.aclose()


async def test_a_seq_that_never_comes_holds_the_stream_back_only_a_while(store: Store, monkeypatch):
    """SDD §12: a rolled-back insert's seq never appears - after one wait the stream goes on."""
    monkeypatch.setattr(events_module, "HOLE_WAIT_S", 0.3)
    bus = EventBus(store)
    agen = bus.subscribe(since=store.max_seq())
    await emit(store, bus, "plan.updated", "server-1", plan_id="p")
    await _take(agen, 1)
    lost = await _from_another_process(store, "rolled back")
    calls = _uncommitted(store, {lost.seq})
    loop = asyncio.get_running_loop()
    second = await emit(store, bus, "plan.updated", "server-2", plan_id="p")
    started = loop.time()
    got = await _take(agen, 1)
    assert [e.seq for e in got] == [second.seq]
    assert 0.25 <= loop.time() - started < 1.5
    assert calls["n"] <= 0.3 / events_module.HOLE_POLL_S + 3, "it reads again a moment apart"
    third = await emit(store, bus, "plan.updated", "server-3", plan_id="p")
    started = loop.time()
    assert [e.seq for e in await _take(agen, 1)] == [third.seq]
    assert loop.time() - started < 0.2, "one wait per gap, not for each event after it"
    await agen.aclose()


async def test_a_hold_ends_after_its_wait_even_when_the_writer_s_clock_is_ahead(
    store: Store, monkeypatch
):
    """SDD §12: the wait is counted from when the stream began holding, so an event stamped by a
    clock that runs ahead - it looks recent for a minute - holds the stream only that long."""
    monkeypatch.setattr(events_module, "HOLE_WAIT_S", 0.3)
    bus = EventBus(store)
    agen = bus.subscribe(since=store.max_seq())
    await emit(store, bus, "plan.updated", "server-1", plan_id="p")
    await _take(agen, 1)
    lost = await _from_another_process(store, "rolled back")
    second = await emit(store, bus, "plan.updated", "server-2", plan_id="p")
    _uncommitted(store, {lost.seq}, ahead={second.seq})
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert [e.seq for e in await _take(agen, 1)] == [second.seq]
    assert 0.25 <= loop.time() - started < 1.5
    await agen.aclose()


async def test_each_gap_is_held_for_on_its_own(store: Store, monkeypatch):
    """SDD §12: a gap waited out does not use up the wait of the next one."""
    monkeypatch.setattr(events_module, "HOLE_WAIT_S", 0.3)
    bus = EventBus(store)
    agen = bus.subscribe(since=store.max_seq())
    await emit(store, bus, "plan.updated", "server-1", plan_id="p")
    await _take(agen, 1)
    lost = await _from_another_process(store, "rolled back")
    _uncommitted(store, {lost.seq})
    second = await emit(store, bus, "plan.updated", "server-2", plan_id="p")
    assert [e.seq for e in await _take(agen, 1)] == [second.seq]
    late = await _from_another_process(store, "late")
    _uncommitted(store, {late.seq}, reads=2)
    third = await emit(store, bus, "plan.updated", "server-3", plan_id="p")
    assert [e.seq for e in await _take(agen, 2)] == [late.seq, third.seq]
    await agen.aclose()


async def test_what_comes_before_a_gap_is_sent_at_once(store: Store, monkeypatch):
    """SDD §12: the stream holds only what follows the gap - what was read before it goes out."""
    monkeypatch.setattr(events_module, "HOLE_WAIT_S", 0.5)
    bus = EventBus(store)
    since = store.max_seq()
    before = [await emit(store, bus, "plan.updated", f"b{i}", plan_id="p") for i in range(2)]
    lost = await _from_another_process(store, "rolled back")
    after = await emit(store, bus, "plan.updated", "after", plan_id="p")
    _uncommitted(store, {lost.seq})
    agen = bus.subscribe(since=since)
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert [e.seq for e in await _take(agen, 2)] == [e.seq for e in before]
    assert loop.time() - started < 0.2
    assert [e.seq for e in await _take(agen, 1)] == [after.seq]
    assert loop.time() - started >= 0.45
    await agen.aclose()


async def test_no_hold_for_a_gap_right_after_seq_0(any_store):
    """SDD §12: a store whose events were all deleted goes on numbering from where they ended - a
    stream that starts from seq 0 sends its first event at once."""
    store = any_store
    bus = EventBus(store)
    for i in range(3):
        await emit(store, bus, "plan.updated", f"old-{i}", plan_id="p")
    assert store.delete_events_before(utcnow() + timedelta(seconds=1)) == 3
    assert store.max_seq() == 0
    agen = bus.subscribe(since=store.max_seq())
    task = asyncio.ensure_future(_take(agen, 1))
    await asyncio.sleep(0.05)
    loop = asyncio.get_running_loop()
    fresh = await emit(store, bus, "plan.updated", "new", plan_id="p")
    started = loop.time()
    assert [e.seq for e in await task] == [fresh.seq]
    assert fresh.seq > 3 and loop.time() - started < 0.2
    await agen.aclose()


async def test_a_replay_without_gaps_is_one_read(store: Store):
    """SDD §12: consecutive seqs are sent as they are read - no gap, nothing held, no read again."""
    bus = EventBus(store)
    since = store.max_seq()
    sent = [await emit(store, bus, "plan.updated", f"e{i}", plan_id="p") for i in range(3)]
    calls = _uncommitted(store, set())
    agen = bus.subscribe(since=since)
    assert [e.seq for e in await _take(agen, 3)] == [e.seq for e in sent]
    assert calls["n"] == 1
    await agen.aclose()


async def test_a_gap_before_an_older_event_is_not_waited_for(store: Store):
    """SDD §12: a resume's replay meets old gaps (pruned events) - they hold nothing back."""
    bus = EventBus(store)
    first = await emit(store, bus, "plan.updated", "a", plan_id="p")
    gone = await _from_another_process(store, "pruned")
    old = await emit(store, bus, "plan.updated", "b", plan_id="p")
    _uncommitted(store, {gone.seq}, aged={old.seq})
    agen = bus.subscribe(since=first.seq - 1)
    loop = asyncio.get_running_loop()
    started = loop.time()
    got = await _take(agen, 2)
    assert [e.seq for e in got] == [first.seq, old.seq]
    assert loop.time() - started < 0.2
    await agen.aclose()


async def test_a_stream_holds_an_event_behind_another_process_s_uncommitted_insert(any_store):
    """SDD §12 on PostgreSQL: the CLI's insert takes its seq, the server commits a later event
    first - the stream holds that event until the CLI's commits, and sends both in seq order."""
    store = any_store
    if store.engine.dialect.name != "postgresql":
        pytest.skip("SQLite serializes writers: no insert stays uncommitted beside a commit")
    bus = EventBus(store)
    agen = bus.subscribe(since=store.max_seq())
    first = await emit(store, bus, "plan.updated", "server-1", plan_id="p")
    assert [e.seq for e in await _take(agen, 1)] == [first.seq]
    other = sa.create_engine(store.engine.url)  # another process's own connection
    try:
        with other.connect() as conn:
            tx = conn.begin()
            row = {"ts": utcnow(), "kind": "plan.updated", "plan_id": "p", "actor": "cli"}
            conn.execute(events_table.insert().values(**row, message="cli", data={}))
            second = await emit(store, bus, "plan.updated", "server-2", plan_id="p")
            task = asyncio.ensure_future(_take(agen, 2))
            await asyncio.sleep(0.3)
            assert not task.done(), "server-2 waits for the CLI's event"
            tx.commit()
        got = await task
    finally:
        other.dispose()
    assert [e.message for e in got] == ["cli", "server-2"]
    assert got[0].seq < got[1].seq == second.seq
    await agen.aclose()


def test_event_kinds_match_sdd_4_3_and_the_dashboard():
    """events.py, SDD §4.3 and the dashboard's kind lists name the same event kinds."""
    import re

    from seamless_migrate.config import find_repo_root
    from seamless_migrate.events import EPHEMERAL_KINDS, PERSISTED_KINDS

    root = find_repo_root()
    sdd = (root / "docs" / "SDD.md").read_text(encoding="utf-8")
    section = sdd[sdd.index("### 4.3") : sdd.index("\n## 5", sdd.index("### 4.3"))]
    persisted_doc = set(re.findall(r"`([a-z_]+\.[a-z_]+)`", section.split("Ephemeral")[0]))
    ephemeral_doc = set(re.findall(r"`([a-z_.]+)`", section.split("Ephemeral")[1]))
    assert persisted_doc == set(PERSISTED_KINDS)
    assert ephemeral_doc == set(EPHEMERAL_KINDS)
    types_ts = (root / "dashboard" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
    persisted_ui = set(
        re.findall(
            r"'([a-z_.]+)'", types_ts[types_ts.index("PERSISTED_EVENT_KINDS") :].split("]")[0]
        )
    )
    assert persisted_ui == set(PERSISTED_KINDS)
    assert set(EPHEMERAL_KINDS) <= set(re.findall(r"'([a-z_.]+)'", types_ts))
