import threading

import pytest

from seamless_migrate.domain.enums import Phase, PlanStatus
from seamless_migrate.domain.models import Event, Migration, Plan, Provider
from seamless_migrate.store import ConflictError, NotFound, Store
from tests.factories import make_migration, make_plan, make_provider


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
