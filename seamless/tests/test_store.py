import hashlib
import re
import secrets
import threading
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from seamless_migrate.domain.enums import Phase, PlanStatus, Role
from seamless_migrate.domain.models import (
    Event,
    Mappings,
    Migration,
    Nic,
    Plan,
    Provider,
    RecommendedOption,
    SimilarIncident,
)
from seamless_migrate.events import AUDIT_KINDS
from seamless_migrate.security.auth import DashboardUser
from seamless_migrate.store import (
    ConflictError,
    NotFound,
    Store,
    UnstorableValue,
    documents,
    events,
)
from tests.conftest import PG_URL
from tests.factories import make_migration, make_plan, make_provider, make_vm


def test_put_get_roundtrip(any_store: Store):
    plan = make_plan(name="Finance apps")
    assert any_store.put("plan", plan) == 1
    loaded = any_store.get("plan", plan.id, Plan)
    assert loaded == plan
    loaded2, version = any_store.get_versioned("plan", plan.id, Plan)
    assert version == 1 and loaded2 == plan

    prov = make_provider()
    any_store.put("provider", prov)
    assert any_store.get("provider", prov.id, Provider) == prov

    with pytest.raises(NotFound):
        any_store.get("plan", "plan-missing", Plan)


def test_put_with_event_writes_the_document_and_its_event_together(any_store: Store, refuse_write):
    """SDD §11, §8.1 (review RV21-04): a versioned update and its event in one transaction - both
    are written, or neither: a version conflict, or an event the store cannot append, leaves the
    document as it was and no event. Each half is refused when it runs, after the other ran: a write
    split over two transactions fails this, whichever half it writes first (RV23-08, RV24-03)."""
    mig = make_migration()
    version = any_store.put("migration", mig)
    flagged = mig.model_copy(
        update={"source_running_at": datetime(2026, 10, 16, 12, 0, tzinfo=UTC)}
    )
    event = Event(
        kind="migration.source_running",
        plan_id=mig.plan_id,
        migration_id=mig.id,
        message="web-01: the source VM runs again",
        data={"power_state": "running"},
    )

    def neither():
        assert any_store.get("migration", mig.id, Migration).source_running_at is None
        assert any_store.events(kinds=["migration.source_running"]) == []

    with pytest.raises(ConflictError):
        any_store.put_with_event("migration", flagged, event, version + 1)
    neither()

    for write, of in (("INSERT INTO events", event.kind), ("UPDATE documents", "migration")):
        refused = refuse_write(any_store, write, of)
        with pytest.raises(RuntimeError, match="briefly unavailable"):
            any_store.put_with_event("migration", flagged, event, version)
        assert refused == [f"{write} {of}"]
        neither()
    saved, written = any_store.put_with_event("migration", flagged, event, version)
    assert saved == version + 1 and written.seq is not None
    assert (
        any_store.get("migration", mig.id, Migration).source_running_at == flagged.source_running_at
    )
    assert [e.seq for e in any_store.events(kinds=["migration.source_running"])] == [written.seq]


def test_put_with_event_refuses_a_write_that_landed_after_its_read(any_store: Store, land_first):
    """SDD §11 (review RV25-04): a versioned update is conditional in its own statement - another
    process's write landing between the caller's read and the update makes it a conflict, which
    keeps that write and appends no event; never an update by key over it."""
    mig = make_migration()
    version = any_store.put("migration", mig)
    flagged = mig.model_copy(
        update={"source_running_at": datetime(2026, 10, 17, 12, 0, tzinfo=UTC)}
    )
    theirs = mig.model_copy(update={"review_reason": "another process wrote this"})
    event = Event(
        kind="migration.source_running",
        plan_id=mig.plan_id,
        migration_id=mig.id,
        message="web-01: the source VM runs again",
        data={"power_state": "running"},
    )
    landed = land_first(
        any_store,
        "UPDATE documents",
        "migration",
        lambda: any_store.put("migration", theirs, version),
    )
    with pytest.raises(ConflictError, match=f"is at version {version + 1}, expected {version}"):
        any_store.put_with_event("migration", flagged, event, version)
    assert landed == [version + 1]
    kept = any_store.get("migration", mig.id, Migration)
    assert (kept.review_reason, kept.source_running_at) == ("another process wrote this", None)
    assert any_store.events(kinds=[event.kind]) == []


def test_optimistic_conflict_raises(any_store: Store):
    mig = make_migration()
    assert any_store.put("migration", mig, expected_version=0) == 1
    with pytest.raises(ConflictError):  # already exists
        any_store.put("migration", mig, expected_version=0)
    mig.progress_pct = 10
    assert any_store.put("migration", mig, expected_version=1) == 2
    mig.progress_pct = 20
    with pytest.raises(ConflictError):  # stale version
        any_store.put("migration", mig, expected_version=1)
    assert any_store.get("migration", mig.id, Migration).progress_pct == 10
    # unconditional put still works and bumps the version
    assert any_store.put("migration", mig) == 3
    with pytest.raises(ConflictError):  # update of a missing document
        any_store.put("migration", make_migration(), expected_version=4)


def test_a_versioned_put_is_one_statement(any_store: Store):
    """SDD §11 (control-plane perf audit CP-P6): a write with an expected version is one statement -
    an INSERT for a new document, an UPDATE ... WHERE version for an update; only a conflict reads
    the version, to say what it found. An unconditional write still reads the version first."""
    statements: list[str] = []

    def count(conn, cursor, statement, params, context, executemany):
        statements.append(statement.split()[0].upper())

    mig = make_migration()
    sa.event.listen(any_store.engine, "before_cursor_execute", count)
    try:
        assert any_store.put("migration", mig, expected_version=0) == 1
        assert statements == ["INSERT"]
        statements.clear()
        mig.progress_pct = 10
        assert any_store.put("migration", mig, expected_version=1) == 2
        assert statements == ["UPDATE"]
        with pytest.raises(ConflictError, match="is at version 2, expected 0"):
            any_store.put("migration", mig, expected_version=0)
        with pytest.raises(ConflictError, match="is at version 2, expected 1"):
            any_store.put("migration", mig.model_copy(update={"progress_pct": 99.0}), 1)
        with pytest.raises(ConflictError, match="does not exist"):
            any_store.put("migration", make_migration(), expected_version=4)
        statements.clear()
        assert any_store.put("migration", mig) == 3
        assert statements == ["SELECT", "UPDATE"]
    finally:
        sa.event.remove(any_store.engine, "before_cursor_execute", count)
    # a refused write changed nothing
    assert any_store.get("migration", mig.id, Migration).progress_pct == 10


def test_change_stamp_moves_on_insert_update_and_delete(any_store: Store):
    assert any_store.change_stamp("migration") == (0, 0)
    m = make_migration()
    any_store.put("migration", m)
    first = any_store.change_stamp("migration")
    assert first == (1, 1)
    any_store.put("migration", m.model_copy(update={"progress_pct": 50.0}), expected_version=1)
    second = any_store.change_stamp("migration")
    assert second == (1, 2) and second != first
    any_store.delete("migration", m.id)
    assert any_store.change_stamp("migration") == (0, 0)
    assert any_store.change_stamp("plan") == (0, 0)  # per kind


def test_plan_phases_counts_each_plans_migrations_by_phase(any_store: Store):
    """SDD §8.1, §11: each plan's phases with how many of its migrations are in each, in one
    query, without loading a document (PF-02: the plan routes' migration_counts)."""
    for plan_id, phase in (
        ("plan-a", Phase.ready),
        ("plan-a", Phase.blocked),
        ("plan-a", Phase.ready),
        ("plan-b", Phase.completed),
        ("plan-c", Phase.syncing),
    ):
        any_store.put("migration", make_migration(plan_id=plan_id, phase=phase))
    got = any_store.plan_phases(["plan-a", "plan-b", "plan-x"])
    assert got == {"plan-a": {Phase.ready: 2, Phase.blocked: 1}, "plan-b": {Phase.completed: 1}}
    assert all(isinstance(p, Phase) for phases in got.values() for p in phases)
    assert any_store.plan_phases([]) == {}


def test_versions_and_get_many_read_only_what_is_asked(any_store: Store):
    """SDD §11: (id, version) in creation order, no document read; get_many loads what exists."""
    a, b, c = (make_migration() for _ in range(3))
    for m in (a, b, c):
        any_store.put("migration", m)
    any_store.put("migration", b.model_copy(update={"progress_pct": 40.0}), expected_version=1)
    any_store.delete("migration", c.id)
    assert any_store.versions("migration") == [(a.id, 1), (b.id, 2)]
    got = any_store.get_many("migration", [b.id, c.id, "mig-missing"], Migration)
    assert set(got) == {b.id}
    version, model = got[b.id]
    assert (version, model.progress_pct) == (2, 40.0)
    assert any_store.get_many("migration", [], Migration) == {}
    assert any_store.versions("plan") == []


def test_list_filters(any_store: Store):
    a = make_migration(plan_id="plan-a", phase=Phase.ready, wave_id="wave-1")
    b = make_migration(plan_id="plan-a", phase=Phase.blocked, wave_id="wave-2")
    c = make_migration(plan_id="plan-b", phase=Phase.ready)
    for m in (a, b, c):
        any_store.put("migration", m)
    any_store.put("plan", make_plan(status=PlanStatus.running))

    ids = lambda ms: {m.id for m in ms}  # noqa: E731
    assert ids(any_store.list("migration", Migration)) == {a.id, b.id, c.id}
    assert ids(any_store.list("migration", Migration, plan_id="plan-a")) == {a.id, b.id}
    assert ids(any_store.list("migration", Migration, plan_id="plan-a", phase="ready")) == {a.id}
    assert ids(any_store.list("migration", Migration, phase=[Phase.ready, Phase.blocked])) == {
        a.id,
        b.id,
        c.id,
    }
    assert ids(any_store.list("migration", Migration, wave_id=None)) == {c.id}
    assert len(any_store.list("plan", Plan, status=PlanStatus.running)) == 1
    assert any_store.list("plan", Plan, status="draft") == []


def test_delete(any_store: Store):
    prov = make_provider()
    any_store.put("provider", prov)
    any_store.delete("provider", prov.id)
    with pytest.raises(NotFound):
        any_store.get("provider", prov.id, Provider)
    with pytest.raises(NotFound):
        any_store.delete("provider", prov.id)


def test_events_since_and_filters(any_store: Store):
    stored = [
        any_store.append_event(Event(kind="plan.created", plan_id="plan-a", message="p")),
        any_store.append_event(
            Event(kind="migration.phase", plan_id="plan-a", migration_id="mig-1", message="1")
        ),
        any_store.append_event(
            Event(kind="migration.phase", plan_id="plan-b", migration_id="mig-2", message="2")
        ),
        any_store.append_event(
            Event(kind="migration.error", plan_id="plan-a", migration_id="mig-1", message="3")
        ),
    ]
    seqs = [e.seq for e in stored]
    assert seqs == sorted(seqs) and len(set(seqs)) == 4 and seqs[0] > 0
    base = seqs[0] - 1

    assert [e.seq for e in any_store.events(since_seq=base)] == seqs
    assert [e.seq for e in any_store.events(since_seq=seqs[1])] == seqs[2:]
    assert [e.message for e in any_store.events(since_seq=base, plan_id="plan-a")] == [
        "p",
        "1",
        "3",
    ]
    assert [e.message for e in any_store.events(since_seq=base, migration_id="mig-1")] == [
        "1",
        "3",
    ]
    assert [e.seq for e in any_store.events(since_seq=base, limit=2)] == seqs[:2]
    assert any_store.max_seq() == seqs[-1]
    roundtrip = any_store.events(since_seq=seqs[2], limit=1)[0]
    assert roundtrip.kind == "migration.error" and roundtrip.ts.tzinfo is not None


def test_events_tail_returns_the_newest_matching_events_in_seq_order(any_store: Store):
    """SDD §12: ``tail`` selects the newest ``limit`` matching events after ``since``, ascending."""
    stored = [
        any_store.append_event(
            Event(kind="migration.phase", plan_id=plan, migration_id=f"mig-{i}", message=str(i))
        )
        for i, plan in enumerate(["plan-a", "plan-b", "plan-a", "plan-b", "plan-a"])
    ]
    seqs = [e.seq for e in stored]
    base = seqs[0] - 1

    assert [e.seq for e in any_store.events(since_seq=base, limit=2, tail=True)] == seqs[3:]
    plan_a = any_store.events(since_seq=base, plan_id="plan-a", limit=2, tail=True)
    assert [e.message for e in plan_a] == ["2", "4"]
    # only events after since, and fewer than limit when fewer match
    assert [e.seq for e in any_store.events(since_seq=seqs[3], limit=10, tail=True)] == seqs[4:]
    assert any_store.events(since_seq=seqs[-1], limit=10, tail=True) == []
    # without tail the same query still pages forward
    assert [e.seq for e in any_store.events(since_seq=base, limit=2)] == seqs[:2]


def test_events_before_pages_back_from_the_oldest_shown(any_store: Store):
    """SDD §12: ``before`` keeps events below a seq; with ``tail`` the newest ``limit`` of them."""
    stored = [
        any_store.append_event(Event(kind="migration.phase", plan_id=plan, message=str(i)))
        for i, plan in enumerate(["plan-a", "plan-b", "plan-a", "plan-b", "plan-a", "plan-a"])
    ]
    seqs = [e.seq for e in stored]
    newest = any_store.events(limit=2, tail=True)
    assert [e.seq for e in newest] == seqs[4:]
    older = any_store.events(limit=2, tail=True, before_seq=newest[0].seq)
    assert [e.seq for e in older] == seqs[2:4]
    oldest = any_store.events(limit=2, tail=True, before_seq=older[0].seq)
    assert [e.seq for e in oldest] == seqs[:2]
    assert any_store.events(limit=2, tail=True, before_seq=oldest[0].seq) == []
    # with a plan: that plan's events below the seq
    plan_a = any_store.events(plan_id="plan-a", limit=2, tail=True, before_seq=seqs[5])
    assert [e.message for e in plan_a] == ["2", "4"]
    # without tail: the first events after since, below before
    assert [e.seq for e in any_store.events(since_seq=seqs[0], before_seq=seqs[3])] == seqs[1:3]


def test_create_schema_adds_the_events_indexes_to_an_existing_table(any_store: Store):
    """SDD §11: an events table from before ix_events_kind_seq gets it at the next startup;
    create_all alone skips the indexes of a table that exists."""
    with any_store.engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_events_kind_seq")
    any_store.append_event(Event(kind="plan.created", plan_id="plan-a", message="kept"))
    any_store.create_schema()
    any_store.create_schema()
    indexes = {
        i["name"]: i["column_names"] for i in sa.inspect(any_store.engine).get_indexes("events")
    }
    assert indexes["ix_events_kind_seq"] == ["kind", "seq"]
    assert indexes["ix_events_plan_id"] == ["plan_id"]
    assert indexes["ix_events_migration_id"] == ["migration_id"]
    assert [e.message for e in any_store.events()] == ["kept"]


def test_the_audit_filter_reads_the_kind_index_not_the_whole_table(store: Store):
    """SDD §11, §12: GET /events?audit=true (and its ``before`` pages) finds the sparse audit
    events through ix_events_kind_seq instead of reading the events table backwards."""
    statements: list[tuple[str, object]] = []

    def capture(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, params))

    sa.event.listen(store.engine, "before_cursor_execute", capture)
    try:
        store.events(kinds=AUDIT_KINDS, tail=True, limit=500)
        store.events(kinds=AUDIT_KINDS, tail=True, limit=500, before_seq=1000)
    finally:
        sa.event.remove(store.engine, "before_cursor_execute", capture)
    assert len(statements) == 2
    with store.engine.connect() as conn:
        for statement, params in statements:
            plan = [
                row[-1] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, params)
            ]
            assert any("ix_events_kind_seq" in step for step in plan), plan


def test_ping(any_store: Store, tmp_path):
    assert any_store.ping() is True
    broken = Store("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")
    assert broken.ping() is False
    broken.dispose()


def test_concurrent_writers_do_not_lose_updates(any_store: Store):
    mig = make_migration()
    any_store.put("migration", mig)
    errors: list[Exception] = []

    def bump() -> None:
        for _ in range(10):
            while True:
                current, version = any_store.get_versioned("migration", mig.id, Migration)
                current.attempts += 1
                try:
                    any_store.put("migration", current, expected_version=version)
                    break
                except ConflictError:
                    continue
                except Exception as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
                    return

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert any_store.get("migration", mig.id, Migration).attempts == 40


def test_sql_pushed_filters_match_python_semantics(any_store: Store):
    """String filters run in SQL (indexed JSON fields); other values stay Python-side."""
    from seamless_migrate.store import INDEXED_FIELDS, _sql_filter

    a = make_migration(plan_id="plan-a", phase=Phase.ready, attempts=2)
    b = make_migration(plan_id="plan-a", phase=Phase.failed, attempts=0)
    for m in (a, b):
        any_store.put("migration", m)
    ids = lambda ms: {m.id for m in ms}  # noqa: E731
    assert ids(any_store.list("migration", Migration, attempts=2)) == {a.id}  # int: Python
    assert ids(any_store.list("migration", Migration, phase=Phase.failed)) == {b.id}  # enum: SQL
    assert ids(any_store.list("migration", Migration, phase=("ready", "failed"))) == {a.id, b.id}
    assert ids(any_store.list("migration", Migration, plan_id="plan-a", attempts=0)) == {b.id}
    assert any_store.list("migration", Migration, plan_id="nope") == []
    assert any_store.list("migration", Migration, plan_id=["x", "y"]) == []
    # a field name that is not an identifier never reaches SQL; mixed/non-string values neither
    assert _sql_filter("plan id", "x") is None
    assert _sql_filter("attempts", 2) is None
    assert _sql_filter("phase", ["ready", 3]) is None
    assert _sql_filter("phase", []) is None
    assert _sql_filter("phase", Phase.ready) is not None
    assert all(f.isidentifier() for f in INDEXED_FIELDS)
    # schema creation is idempotent (expression indexes use IF NOT EXISTS)
    any_store.create_schema()
    any_store.create_schema()
    assert ids(any_store.list("migration", Migration, plan_id="plan-a")) == {a.id, b.id}


def test_list_limit_and_offset_in_sql_and_python(any_store: Store):
    ms = [make_migration(plan_id="plan-p", phase=Phase.ready, attempts=i % 2) for i in range(7)]
    for m in ms:
        any_store.put("migration", m)
    order = [m.id for m in any_store.list("migration", Migration, plan_id="plan-p")]
    assert len(order) == 7
    # pushed-down filters page in SQL
    assert [
        m.id for m in any_store.list("migration", Migration, plan_id="plan-p", limit=3)
    ] == order[:3]
    assert [
        m.id for m in any_store.list("migration", Migration, plan_id="plan-p", limit=2, offset=5)
    ] == order[5:7]
    assert any_store.list("migration", Migration, plan_id="plan-p", offset=7) == []
    assert any_store.list("migration", Migration, limit=0) == []
    # a Python-side filter pages after filtering, with the same semantics
    odd = [m.id for m in any_store.list("migration", Migration, attempts=1)]
    assert len(odd) == 3
    assert [m.id for m in any_store.list("migration", Migration, attempts=1, limit=2)] == odd[:2]
    assert [m.id for m in any_store.list("migration", Migration, attempts=1, offset=2)] == odd[2:]


def test_delete_events_before(any_store: Store):
    from datetime import UTC, datetime, timedelta

    from seamless_migrate.domain.models import Event

    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(5):
        any_store.append_event(
            Event(ts=t0 + timedelta(days=i), kind="plan.updated", actor="t", message=f"e{i}")
        )
    assert any_store.delete_events_before(t0 + timedelta(days=2)) == 2
    left = any_store.events(since_seq=0, limit=100)
    assert [e.message for e in left] == ["e2", "e3", "e4"]
    assert any_store.max_seq() == left[-1].seq, "sequence numbers are not reused"
    assert any_store.delete_events_before(datetime(2020, 1, 1)) == 0  # naive = UTC
    # an aware cutoff in another zone is normalised to UTC (SQLite stores UTC wall time)
    from datetime import timezone

    plus7 = timezone(timedelta(hours=7))
    # 06:59+07:00 is Oct 3 23:59 UTC: it removes e2 (Oct 3 00:00) but not e3 (Oct 4 00:00)
    assert any_store.delete_events_before(datetime(2026, 10, 4, 6, 59, tzinfo=plus7)) == 1
    assert [e.message for e in any_store.events(since_seq=0, limit=100)] == ["e3", "e4"]
    assert any_store.delete_events_before(datetime(2026, 10, 4, 7, 1, tzinfo=plus7)) == 1


# -- the cross-process VM-claim lock (SDD §5.4) ------------------------------------------------
def test_cross_process_lock_excludes_a_second_holder(any_store: Store):
    """Two holders of one name exclude each other as two processes would: two connections on
    PostgreSQL (advisory lock), two open files on SQLite (flock)."""
    other = Store(any_store.url)
    try:
        first, second = (
            any_store.cross_process_lock("vm-claims"),
            other.cross_process_lock("vm-claims"),
        )
        assert first.try_acquire()
        assert not second.try_acquire()
        first.release()
        assert second.try_acquire()
        assert not first.try_acquire()
        second.release()
        first.release()  # releasing a lock not held is a no-op
    finally:
        other.dispose()


def test_cross_process_lock_dies_with_its_process(any_store: Store):
    """A holder killed while it holds the lock never strands it: the database or the OS drops it."""
    import os
    import signal
    import subprocess
    import sys
    import time

    code = (
        "import os, time\n"
        "from seamless_migrate.store import Store\n"
        "lock = Store(os.environ['LOCK_TEST_DB_URL']).cross_process_lock('vm-claims')\n"
        "assert lock.try_acquire()\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    # the URL may carry a password: the environment, never the command line
    holder = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "LOCK_TEST_DB_URL": any_store.url},
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        lock = any_store.cross_process_lock("vm-claims")
        assert not lock.try_acquire()
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(10)
        deadline = time.monotonic() + 10
        while not lock.try_acquire():
            assert time.monotonic() < deadline, "the lock outlived its process"
            time.sleep(0.05)
        lock.release()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(10)


def test_an_in_memory_database_needs_no_cross_process_lock():
    store = Store("sqlite://")
    try:
        lock = store.cross_process_lock("vm-claims")
        assert lock.try_acquire() and lock.try_acquire()  # one process can reach it
        lock.release()
    finally:
        store.dispose()


async def test_a_cancelled_waiter_leaves_the_lock_free(any_store: Store):
    """AsyncStore.cross_process_lock polls; a waiter cancelled mid-wait holds nothing after."""
    import asyncio

    from seamless_migrate.store import AsyncStore

    other = Store(any_store.url)
    try:
        holder = other.cross_process_lock("vm-claims")
        assert holder.try_acquire()

        async def wait_for_it():
            async with AsyncStore(any_store).cross_process_lock("vm-claims"):
                pytest.fail("acquired a lock another holder holds")

        waiter = asyncio.create_task(wait_for_it())
        await asyncio.sleep(0.3)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        holder.release()
        async with AsyncStore(any_store).cross_process_lock("vm-claims"):
            assert not holder.try_acquire()
        assert holder.try_acquire()
        holder.release()
    finally:
        other.dispose()


def test_a_closed_cross_process_lock_acquires_nothing_more(any_store: Store):
    """close() - what a cancelled waiter does - also refuses an attempt still running in a
    worker thread after it, so such an attempt never takes the lock for nobody."""
    lock = any_store.cross_process_lock("vm-claims")
    lock.close()
    assert not lock.try_acquire()
    other = any_store.cross_process_lock("vm-claims")
    assert other.try_acquire()
    other.release()


def _statements_of(store: Store, call) -> list[str]:
    seen: list[str] = []

    def capture(conn, cursor, statement, params, context, executemany):
        seen.append(statement.strip())

    sa.event.listen(store.engine, "before_cursor_execute", capture)
    try:
        call()
    finally:
        sa.event.remove(store.engine, "before_cursor_execute", capture)
    return seen


def test_create_schema_on_a_complete_schema_issues_no_ddl(any_store: Store):
    """SDD §11 (docs drift DD-01): PostgreSQL lets only a table's owner run CREATE INDEX, even
    IF NOT EXISTS, and every server start calls create_schema - on a complete schema it issues no
    DDL, so a role that does not own the tables (Security.md §9.4) can start the control plane."""
    seen = _statements_of(any_store, any_store.create_schema)
    ddl = [s for s in seen if s.split()[0].upper() in ("CREATE", "ALTER", "DROP")]
    assert ddl == []


def test_create_schema_adds_only_the_index_a_table_lacks(any_store: Store):
    with any_store.engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX ix_events_kind_seq"))
    seen = _statements_of(any_store, any_store.create_schema)
    created = [s for s in seen if s.split()[0].upper() == "CREATE"]
    assert len(created) == 1 and "ix_events_kind_seq" in created[0]
    seen = _statements_of(any_store, any_store.create_schema)
    assert not [s for s in seen if s.split()[0].upper() == "CREATE"], "it is there again"


@pytest.mark.skipif(not PG_URL, reason="SEAMLESS_TEST_PG_URL is not set")
def test_a_role_that_does_not_own_the_tables_starts_once_the_owner_created_them():
    """SDD §11, Security.md §9.4: the application role - no table ownership, events insert-only -
    starts the control plane (create_schema) once the owner created the schema, writes documents and
    events, and cannot rewrite the audit trail."""
    owner = Store(PG_URL)
    owner.create_schema()
    url = sa.engine.make_url(PG_URL)
    role, password = f"seamless_app_{secrets.token_hex(4)}", secrets.token_urlsafe(16)
    with owner.engine.begin() as conn:
        conn.execute(sa.text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
        conn.execute(sa.text(f'GRANT CONNECT ON DATABASE "{url.database}" TO {role}'))
        conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO {role}"))
        conn.execute(sa.text(f"GRANT SELECT, INSERT ON events TO {role}"))
        conn.execute(sa.text(f"GRANT USAGE, SELECT ON SEQUENCE events_seq_seq TO {role}"))
    app = Store(url.set(username=role, password=password).render_as_string(hide_password=False))
    try:
        app.create_schema()  # what every server start and CLI command runs
        app.put("migration", make_migration(), expected_version=0)
        app.append_event(Event(kind="plan.created", message="as the application role"))
        with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
            with app.engine.begin() as conn:
                conn.execute(sa.text("UPDATE events SET message = 'rewritten'"))
    finally:
        app.dispose()
        with owner.engine.begin() as conn:
            conn.execute(sa.text(f"REVOKE ALL ON documents, events FROM {role}"))
            conn.execute(sa.text(f"REVOKE ALL ON SEQUENCE events_seq_seq FROM {role}"))
            conn.execute(sa.text(f'REVOKE CONNECT ON DATABASE "{url.database}" FROM {role}'))
            conn.execute(sa.text(f"DROP ROLE {role}"))
        owner.truncate_for_tests()
        owner.dispose()


@pytest.mark.skipif(not PG_URL, reason="SEAMLESS_TEST_PG_URL is not set")
def test_a_role_with_a_schema_of_its_own_starts_once_the_owner_created_the_tables():
    """SDD §11 (review RV-07): with PostgreSQL's secure schema usage pattern the application role
    owns a schema named after it, first in its search_path ("$user", public), while the tables
    are the owner's: create_schema reads the indexes of the tables its names resolve to - not of
    the current schema, the role's own and empty one - so it finds them all and runs no CREATE
    INDEX, which only the tables' owner may run."""
    owner = Store(PG_URL)
    owner.create_schema()
    url = sa.engine.make_url(PG_URL)
    role, password = f"seamless_app_{secrets.token_hex(4)}", secrets.token_urlsafe(16)
    with owner.engine.begin() as conn:
        conn.execute(sa.text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
        conn.execute(sa.text(f'GRANT CONNECT ON DATABASE "{url.database}" TO {role}'))
        conn.execute(sa.text(f"CREATE SCHEMA {role} AUTHORIZATION {role}"))
        conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO {role}"))
        conn.execute(sa.text(f"GRANT SELECT, INSERT ON events TO {role}"))
        conn.execute(sa.text(f"GRANT USAGE, SELECT ON SEQUENCE events_seq_seq TO {role}"))
    app = Store(url.set(username=role, password=password).render_as_string(hide_password=False))
    try:
        with app.engine.connect() as conn:  # the premise: its current schema is its own
            assert conn.execute(sa.text("SELECT current_schema()")).scalar() == role
        seen = _statements_of(app, app.create_schema)  # what every server start runs
        assert not [s for s in seen if s.split()[0].upper() == "CREATE"], seen
        migration = make_migration()
        app.put("migration", migration, expected_version=0)
        assert owner.get("migration", migration.id, Migration) == migration  # the owner's table
    finally:
        app.dispose()
        with owner.engine.begin() as conn:
            conn.execute(sa.text(f"DROP SCHEMA {role} CASCADE"))
            conn.execute(sa.text(f"REVOKE ALL ON documents, events FROM {role}"))
            conn.execute(sa.text(f"REVOKE ALL ON SEQUENCE events_seq_seq FROM {role}"))
            conn.execute(sa.text(f'REVOKE CONNECT ON DATABASE "{url.database}" FROM {role}'))
            conn.execute(sa.text(f"DROP ROLE {role}"))
        owner.truncate_for_tests()
        owner.dispose()


def _sent_values(store: Store) -> list:
    """Every parameter value the store sends to its database from now on."""
    sent: list = []

    def record(conn, cursor, statement, parameters, context, executemany):
        for row in parameters if executemany else [parameters]:
            if isinstance(row, dict):
                sent.extend(row.values())
            elif isinstance(row, list | tuple):
                sent.extend(row)
            else:
                sent.append(row)

    sa.event.listen(store.engine, "before_cursor_execute", record)
    return sent


#: a NUL or a lone surrogate escaped in a parameter's JSON
_ESCAPED_UNSTORABLE = re.compile(r"\\u(?:0000|[dD][89a-fA-F][0-9a-fA-F]{2})")


def _unstorable_sent(value) -> bool:
    """Whether a parameter carries a NUL or a lone surrogate - raw, or escaped in its JSON."""
    if isinstance(value, bytes):
        return b"\x00" in value
    if not isinstance(value, str):
        return False
    return bool(re.search("[\x00\ud800-\udfff]", value) or _ESCAPED_UNSTORABLE.search(value))


@pytest.mark.parametrize(
    ("bad", "what"),
    [("\x00", "a NUL character"), ("\ud800", "a lone surrogate")],
    ids=["nul", "lone-surrogate"],
)
def test_the_store_refuses_nul_and_lone_surrogates_and_finds_nothing_by_one(
    any_store: Store, bad: str, what: str
):
    """SDD §11 (reviews ST-03, ST-04, RV48-04, RV48-05, RV48-07, RV48-08, RV48-09): PostgreSQL
    stores no NUL - psycopg refuses one in a text parameter, JSONB refuses \\u0000 - and no lone
    surrogate, which SQLite cannot encode in a text column either. Stripping one made a stored
    value differ from the one a later step compares, looks up or digests - two map keys merged,
    'sys\\0tem' recorded as 'system', a snapshot no longer matching its SHA-256 -, so the store
    rewrites none: a write whose document or event holds one is refused before any statement,
    alike on both backends, naming the kind, the id and the field but never the value, and
    nothing of it is kept. As no stored string holds one, a lookup, a delete or a filter by a
    value holding one finds nothing, without sending it."""
    sent = _sent_values(any_store)
    m = make_migration(vm=make_vm(nics=[Nic(network=f"app{bad}-net", mac="fa:16:3e:00:00:01")]))
    with pytest.raises(UnstorableValue) as refused:
        any_store.put("migration", m, expected_version=0)
    assert str(refused.value) == (
        f"migration {m.id}: vm.nics.0.network: holds {what}, which the store cannot keep"
    )
    # a map's two keys stay two, neither merged into the other (review RV48-08)
    merged = make_plan(mappings=Mappings(networks={"app-net": "prod", f"app{bad}-net": "quiet"}))
    with pytest.raises(UnstorableValue, match=rf"^plan {merged.id}: mappings\.networks: a key "):
        any_store.put("plan", merged)
    with pytest.raises(NotFound):
        any_store.get("plan", merged.id, Plan)
    # an id holding one: the message names the kind alone, never the id
    named = make_migration(id=f"mig-{bad}1")
    with pytest.raises(UnstorableValue, match=rf"^migration: id: holds {what}, which the store "):
        any_store.put("migration", named)
    with pytest.raises(NotFound):
        any_store.get("migration", m.id, Migration)
    # a versioned write and its event: either holding one leaves both unwritten
    clean = make_migration()
    any_store.put("migration", clean, expected_version=0)
    changed = clean.model_copy(update={"error": f"step{bad}failed"})
    with pytest.raises(UnstorableValue, match=rf"^migration {clean.id}: error: holds {what}"):
        any_store.put_with_event("migration", changed, Event(kind="migration.updated"), 1)
    with pytest.raises(UnstorableValue, match=rf"^migration\.updated event: message: holds {what}"):
        any_store.put_with_event(
            "migration", clean, Event(kind="migration.updated", message=f"m{bad}"), 1
        )
    assert any_store.get_versioned("migration", clean.id, Migration) == (clean, 1)
    # every field of an event (review RV48-09)
    for field, event in (
        ("plan_id", Event(kind="plan.created", plan_id=f"p{bad}")),
        ("migration_id", Event(kind="plan.created", migration_id=f"m{bad}")),
        ("actor", Event(kind="plan.created", actor=f"sys{bad}tem")),
        ("message", Event(kind="plan.created", message=f"GET /x{bad}y")),
        ("data.path", Event(kind="plan.created", data={"path": [f"/x{bad}y"]})),
    ):
        with pytest.raises(UnstorableValue, match=rf"^plan\.created event: {field}(\.0)?: holds "):
            any_store.append_event(event)
    with pytest.raises(UnstorableValue, match=rf"^plan\.created event: data: a key holds {what}"):
        any_store.append_event(Event(kind="plan.created", data={f"pa{bad}th": "/x"}))
    with pytest.raises(UnstorableValue, match=rf"^an event: kind: holds {what}"):
        any_store.append_event(Event(kind=f"plan.created{bad}"))
    user = DashboardUser(id="u-1", name=f"op{bad}erator", role=Role.operator, sha256="0" * 64)
    with pytest.raises(UnstorableValue, match=rf"^user u-1: name: holds {what}"):
        any_store.create_user(user, actor="admin")
    named = DashboardUser(id="u-2", name="operator", role=Role.operator, sha256="0" * 64)
    with pytest.raises(UnstorableValue, match=rf"^user\.created event: actor: holds {what}"):
        any_store.create_user(named, actor=f"ad{bad}min")
    for user_id in ("u-1", "u-2"):
        with pytest.raises(NotFound):
            any_store.get("user", user_id, DashboardUser)
    assert any_store.events() == []
    # no stored string holds one: a lookup by a value that does finds nothing, sending nothing
    with pytest.raises(NotFound):
        any_store.get("migration", clean.id + bad, Migration)
    with pytest.raises(NotFound):
        any_store.delete("migration", clean.id + bad)
    assert any_store.list("migration", Migration, plan_id=clean.plan_id + bad) == []
    assert any_store.get_many("migration", [clean.id + bad], Migration) == {}
    assert any_store.plan_phases([clean.plan_id + bad]) == {}
    assert any_store.events(plan_id=f"p{bad}") == []
    assert any_store.events(migration_id=f"m{bad}") == []
    assert any_store.events(kinds=[f"plan.created{bad}"]) == []
    assert not [v for v in sent if _unstorable_sent(v)], [v for v in sent if _unstorable_sent(v)]


@pytest.mark.parametrize(
    ("bad", "what"),
    [(float("nan"), "NaN"), (float("inf"), "an infinity"), (float("-inf"), "an infinity")],
    ids=["nan", "infinity", "-infinity"],
)
def test_the_store_refuses_nan_and_the_infinities_naming_the_field(
    any_store: Store, bad: float, what: str
):
    """SDD §11 (review RV50-04): PostgreSQL's JSONB holds no NaN and no infinity, which Python's
    json writes as NaN and Infinity and SQLite keeps - a verification note's confidence of inf
    failed the verifying save on PostgreSQL only (a note now reads one as null, as one an earlier
    version stored, review RV51-06). A write whose document or event holds one is refused on
    every backend before any statement, naming the field, and nothing of it is kept."""
    m = make_migration(actual_downtime_s=bad)
    with pytest.raises(UnstorableValue) as refused:
        any_store.put("migration", m, expected_version=0)
    assert str(refused.value) == (
        f"migration {m.id}: actual_downtime_s: is {what}, which the store cannot keep"
    )
    with pytest.raises(NotFound):
        any_store.get("migration", m.id, Migration)
    with pytest.raises(UnstorableValue, match=rf"^plan\.created event: data\.hits\.0: is {what},"):
        any_store.append_event(Event(kind="plan.created", data={"hits": [bad]}))
    assert any_store.events() == []
    # an integer of any length is a JSON number, kept as it is
    any_store.append_event(Event(kind="plan.created", data={"bytes": 10**30}))
    assert [e.data for e in any_store.events()] == [{"bytes": 10**30}]


def test_a_document_an_earlier_version_stored_with_nan_or_an_infinity_is_written_again(
    any_store: Store,
):
    """SDD §11 (review RV51-06): v0.1.0-rc.2 kept agentmemory's NaN score and Jev's 1e999
    confidence on SQLite, as json wrote them; the store now refuses every write holding one, so
    such a migration could never be written again - its stopped source never rolled back. They
    read as null, a probability as absent, and the document is written again."""
    if any_store.engine.dialect.name != "sqlite":
        pytest.skip("PostgreSQL's JSONB never kept the tokens")
    m = make_migration()
    any_store.put("migration", m)
    nan, inf = float("nan"), float("inf")
    data = m.model_dump(mode="json")
    data["advisor_notes"] = [
        {
            "kind": "verification",
            "source": "jev",
            "summary": "s",
            "confidence": inf,
            "data": {"hits": [{"title": "t", "score": nan}], "results": [{"confidence": -inf}]},
        }
    ]
    with any_store.engine.begin() as conn:
        conn.execute(
            sa.update(documents)
            .where(documents.c.kind == "migration", documents.c.id == m.id)
            .values(data=data)
        )
    loaded, version = any_store.get_versioned("migration", m.id, Migration)
    [note] = loaded.advisor_notes
    assert note.confidence is None
    assert note.data == {"hits": [{"title": "t", "score": None}], "results": [{"confidence": None}]}
    assert any_store.put("migration", loaded, expected_version=version) == version + 1
    # a recommendation's confidence and probabilities, and a similar incident's score
    rec = {
        "option": "warm",
        "confidence": nan,
        "distribution": {"warm": inf, "cold": 0.25},
    }
    assert RecommendedOption.model_validate(rec).model_dump() == {
        "option": "warm",
        "confidence": None,
        "distribution": {"cold": 0.25},
        "unusable": None,
    }
    assert (
        SimilarIncident.model_validate({"title": "t", "content": "c", "score": inf}).score is None
    )


def test_the_unstorable_walk_reads_a_value_nested_however_deep():
    """SDD §11, §12 (review RV49-05): the walk that finds a NUL or a lone surrogate - for the store
    and for the request-body check - recursed one Python frame per level: 1,500 levels raised
    RecursionError. It walks without recursion, in document order, and names the path."""
    from seamless_migrate.store import unstorable

    clean: object = "x"
    bad: object = "a\x00"
    for _ in range(3000):
        clean, bad = [clean], {"k": bad}
    assert unstorable(clean) is None
    assert unstorable(bad) == (".".join(["k"] * 3000), "holds a NUL character")
    assert unstorable([{"a": "ok", "b\x00": "x"}, "\ud800"]) == ("0", "a key holds a NUL character")


def test_an_over_long_actor_keeps_its_start_and_a_digest_of_the_whole(any_store: Store):
    """SDD §11 (reviews ST-02, RV48-10): events.actor is VARCHAR(128) while the advisor records
    user:<name> for names of up to 128 characters - PostgreSQL refused the event of a change it
    stored, and cut to the column two admins whose names share their first 123 characters read
    as one. An over-long actor keeps its first 115 characters, then ~ and the first 12 hex digits
    of the whole actor's SHA-256: the event is always written, two actors never read as one, and
    one that fits is kept whole."""
    width = events.c.actor.type.length
    first, second = "user:" + "a" * 123 + "1", "user:" + "a" * 123 + "2"
    fits = "user:" + "b" * 123

    def kept(actor: str) -> str:
        return actor[:115] + "~" + hashlib.sha256(actor.encode()).hexdigest()[:12]

    any_store.append_event(Event(kind="policy.created", actor=first, message="appended"))
    plan = make_plan()
    any_store.put("plan", plan, expected_version=0)
    any_store.put_with_event(
        "plan", plan, Event(kind="plan.updated", actor=second, message="with its change"), 1
    )
    any_store.create_user(
        DashboardUser(id="u-1", name="u", role=Role.viewer, sha256="0" * 64), actor=fits
    )
    assert [(e.kind, e.actor) for e in any_store.events()] == [
        ("policy.created", kept(first)),
        ("plan.updated", kept(second)),
        ("user.created", fits),
    ]
    assert len(kept(first)) == width == len(fits) and kept(first) != kept(second)


def test_the_store_returns_each_event_as_it_wrote_it(any_store: Store):
    """SDD §11 (review RV48-14): what a write returns is what the bus publishes, and it is the row
    the store wrote - an over-long actor as its column keeps it -, so an open stream and a later
    read of one seq agree."""
    actor = "user:" + "c" * 128
    appended = any_store.append_event(
        Event(kind="policy.created", actor=actor, message="appended", data={"k": "v"})
    )
    plan = make_plan()
    any_store.put("plan", plan, expected_version=0)
    _, changed = any_store.put_with_event(
        "plan", plan, Event(kind="plan.updated", actor=actor, message="changed"), 1
    )
    created = any_store.create_user(
        DashboardUser(id="u-1", name="u", role=Role.viewer, sha256="0" * 64), actor=actor
    )
    stored = {e.seq: e for e in any_store.events()}
    assert [stored[e.seq] for e in (appended, changed, created)] == [appended, changed, created]


def test_events_never_sends_a_seq_outside_its_column(any_store: Store):
    """SDD §11 (review RV48-12): events.seq is PostgreSQL's integer, 1 to 2^31 - 1, and a bound
    beyond it was sent as is - 'integer out of range' there, an overflow on SQLite past 2^63 -,
    so `seamless events export --since-seq 2147483648` failed after creating its file. events
    answers alike on both backends, whoever asks: nothing after its end or before 1, everything
    since a bound below 0 or before one beyond its end."""
    sent = _sent_values(any_store)
    for n in range(3):
        any_store.append_event(Event(kind="plan.created", message=str(n)))
    seqs = [e.seq for e in any_store.events()]
    end = 2**31 - 1
    for since in (end, end + 1, 2**63):
        assert any_store.events(since_seq=since) == []
    for before in (1, 0, -(2**63)):
        assert any_store.events(before_seq=before) == []
    assert [e.seq for e in any_store.events(since_seq=-(2**63), before_seq=2**63)] == seqs
    assert not [v for v in sent if isinstance(v, int) and not -(2**31) <= v <= end]


@pytest.mark.parametrize(
    ("write", "row"),
    [
        ("UPDATE documents", "plan"),
        ("INSERT INTO documents", "migration"),
        ("INSERT INTO events", "plan.updated"),
        ("INSERT INTO events", "migration.action"),
    ],
)
def test_put_all_with_events_writes_every_document_and_event_or_none(
    any_store: Store, refuse_write, write, row
):
    """SDD §11 (reviews RV48-01, RV48-13): several versioned writes and their events in one
    transaction - a strategy change, an advisor decision (§8, §14.6). A statement refused when it
    runs, after the others ran, leaves none of them; a document at another version is a
    ConflictError naming it, nothing written; else each version and event comes back as
    written."""
    plan = make_plan()
    any_store.put("plan", plan, expected_version=0)
    changed = plan.model_copy(update={"name": "changed"})
    m = make_migration(plan_id=plan.id)
    events_ = [Event(kind="plan.updated", message="p"), Event(kind="migration.action", message="m")]
    refused = refuse_write(any_store, write, row)
    with pytest.raises(RuntimeError, match="briefly unavailable"):
        any_store.put_all_with_events([("plan", changed, 1), ("migration", m, 0)], events_)
    assert refused == [f"{write} {row}"]
    assert any_store.get_versioned("plan", plan.id, Plan) == (plan, 1)
    with pytest.raises(NotFound):
        any_store.get("migration", m.id, Migration)
    assert any_store.events() == []
    any_store.put("plan", plan, expected_version=1)  # another writer: now at version 2
    with pytest.raises(ConflictError, match=f"plan '{plan.id}' is at version 2, expected 1"):
        any_store.put_all_with_events([("plan", changed, 1), ("migration", m, 0)], events_)
    with pytest.raises(NotFound):
        any_store.get("migration", m.id, Migration)
    assert any_store.events() == []
    versions, written = any_store.put_all_with_events(
        [("plan", changed, 2), ("migration", m, 0)], events_
    )
    assert (versions, written) == ([3, 1], any_store.events())
    assert [e.kind for e in written] == ["plan.updated", "migration.action"]
    assert any_store.get_versioned("plan", plan.id, Plan) == (changed, 3)
    # a new document that exists meanwhile: a ConflictError naming it, nothing written
    with pytest.raises(ConflictError, match=f"migration '{m.id}' is at version 1, expected 0"):
        any_store.put_all_with_events([("plan", changed, 3), ("migration", m, 0)], events_)
    assert (any_store.get_versioned("plan", plan.id, Plan)[1], len(any_store.events())) == (3, 2)


def test_a_delete_at_a_version_refuses_a_document_that_moved(any_store: Store):
    """SDD §11 (review ST-05): a delete may name the version it read - DELETE ... WHERE
    version = n -, so a write landing in between is refused, never deleted unseen; a document
    gone is NotFound."""
    plan = make_plan()
    any_store.put("plan", plan, expected_version=0)
    any_store.put("plan", plan, expected_version=1)
    with pytest.raises(ConflictError, match="is at version 2, expected 1"):
        any_store.delete("plan", plan.id, expected_version=1)
    any_store.delete("plan", plan.id, expected_version=2)
    with pytest.raises(NotFound):
        any_store.delete("plan", plan.id, expected_version=2)
