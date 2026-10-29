"""The orchestrator's lease and the fence on a server's writes (SDD §11, §15, §25.3, §25.4; plan
task HA2).

Two replicas run in one test process, each with a store of its own - its own engine and pool, as
two processes have -, on one SQLite file and, with SEAMLESS_TEST_PG_URL, on PostgreSQL. Leases
last a second or two, and their expiry is the database's: no test fakes its clock. Replicas are
named seamless-a and seamless-b, their addresses in 192.0.2.0/24 (RFC 5737).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from datetime import time as time_of_day
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from fastapi.testclient import TestClient

from seamless_migrate import cli
from seamless_migrate import lease as lease_module
from seamless_migrate.api.app import create_app
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Role
from seamless_migrate.domain.models import Event, Plan, Provider
from seamless_migrate.lease import Lease
from seamless_migrate.security.auth import DashboardUser
from seamless_migrate.store import Fenced, Store, StoreError, documents, events
from tests.api_support import api_settings
from tests.conftest import PG_URL
from tests.factories import make_migration, make_provider

A = ("seamless-a", "192.0.2.10:8080")
B = ("seamless-b", "192.0.2.11:8080")
#: a lease of a second: the tests that wait for an expiry wait about that long
SHORT_S = 1.0

pg_only = pytest.mark.skipif(not PG_URL, reason="SEAMLESS_TEST_PG_URL is not set")


@pytest.fixture
def replicas(any_store: Store) -> Iterator[tuple[Store, Store]]:
    """Two replicas' stores on one database, unfenced: they drive the lease."""
    pair = (Store(any_store.url), Store(any_store.url))
    yield pair
    for store in pair:
        store.dispose()


def lease_of(store: Store, replica: tuple[str, str], lease_s: float = SHORT_S, **kw: Any) -> Lease:
    holder, address = replica
    return Lease(store.engine, holder, address, lease_s, **kw)


def wait_expired(lease: Lease, timeout_s: float = 15.0) -> None:
    """Until the database's clock - read by the lease's own SQL - passes the lease's expiry."""
    deadline = time.monotonic() + timeout_s
    while True:
        row = lease.read()
        if row is not None and row.expired:
            return
        assert time.monotonic() < deadline, "the lease never expired by the database's clock"
        time.sleep(0.05)


def took_over(any_store: Store) -> None:
    """seamless-a led with epoch 1 and released the lease; seamless-b took it with epoch 2."""
    a, b = lease_of(any_store, A, 30.0), lease_of(any_store, B, 30.0)
    assert a.campaign() == 1 and a.release(1)
    assert b.campaign() == 2


def contents(store: Store) -> tuple[list[tuple], list[tuple]]:
    """Every document and every event the database holds."""
    with store.engine.connect() as conn:
        docs = conn.execute(
            sa.select(
                documents.c.kind, documents.c.id, documents.c.version, documents.c.data
            ).order_by(documents.c.kind, documents.c.id)
        ).all()
        rows = conn.execute(
            sa.select(events.c.seq, events.c.kind, events.c.actor, events.c.message).order_by(
                events.c.seq
            )
        ).all()
    return [tuple(row) for row in docs], [tuple(row) for row in rows]


# -- the lease (SDD §11, §25.3) -----------------------------------------------------------------
def test_the_first_replica_takes_the_lease_with_epoch_1(replicas):
    """SDD §11: with no row the first replica inserts it with epoch 1, its holder and address, the
    database's now() as acquired and renewed, expiring a lease later; a second one finds it held. Of
    two campaigning at once on an empty table, exactly one takes it."""
    a, b = lease_of(replicas[0], A), lease_of(replicas[1], B)
    assert a.read() is None
    assert a.campaign() == 1
    row = a.read()
    assert (row.holder, row.address, row.epoch, row.expired) == (*A, 1, False)
    assert row.acquired_at == row.renewed_at
    assert row.expires_at - row.acquired_at == timedelta(seconds=SHORT_S)
    assert b.campaign() is None
    assert b.read().holder == "seamless-a"

    with replicas[0].engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM leases"))
    barrier = threading.Barrier(2)
    taken: list[int | None] = []

    def campaign(lease: Lease) -> None:
        barrier.wait()
        taken.append(lease.campaign())

    threads = [threading.Thread(target=campaign, args=(lease,)) for lease in (a, b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(taken, key=lambda epoch: epoch or 0) == [None, 1]


def test_a_held_lease_is_not_taken_before_it_expires(replicas):
    """SDD §11: a replica that finds the lease valid takes nothing, try after try; once the
    database's clock passes its expiry, the same replica takes it."""
    a, b = lease_of(replicas[0], A, 2.0), lease_of(replicas[1], B)
    assert a.campaign() == 1
    for _ in range(3):
        assert b.campaign() is None
        row = b.read()
        assert (row.holder, row.epoch) == ("seamless-a", 1)
        time.sleep(0.05)
    wait_expired(b)
    assert b.campaign() == 2


def test_an_expired_lease_is_taken_with_the_next_epoch(replicas):
    """SDD §11: an expired lease is taken in one transaction with epoch + 1, the taker's holder and
    address and a new expiry; the old holder then finds it held, and each takeover raises the
    epoch again."""
    a, b = lease_of(replicas[0], A), lease_of(replicas[1], B)
    assert a.campaign() == 1
    wait_expired(b)
    assert b.campaign() == 2
    row = b.read()
    assert (row.holder, row.address, row.epoch, row.expired) == (*B, 2, False)
    assert row.acquired_at == row.renewed_at
    assert row.expires_at - row.acquired_at == timedelta(seconds=SHORT_S)
    assert a.campaign() is None
    wait_expired(a)
    assert a.campaign() == 3
    assert (a.read().holder, a.read().epoch) == ("seamless-a", 3)


def test_a_renewal_after_a_takeover_renews_nothing(replicas):
    """SDD §11: a renewal moves the expiry of the replica's own lease - its holder and its epoch -;
    once another replica took the lease, the old holder's renewal and release change nothing, and
    neither does a renewal of an epoch the same holder held before it took the lease again."""
    a, b = lease_of(replicas[0], A), lease_of(replicas[1], B)
    assert a.campaign() == 1
    first = a.read()
    time.sleep(0.01)
    assert a.renew(1) is True
    renewed = a.read()
    assert renewed.epoch == 1 and renewed.acquired_at == first.acquired_at
    assert renewed.renewed_at > first.renewed_at
    assert renewed.expires_at - renewed.renewed_at == timedelta(seconds=SHORT_S)
    assert b.renew(1) is False, "another holder's renewal renews nothing"

    wait_expired(b)
    assert b.campaign() == 2
    taken = b.read()
    assert a.renew(1) is False
    assert a.release(1) is False
    after = b.read()
    assert (after.holder, after.epoch, after.renewed_at, after.expires_at) == (
        taken.holder,
        taken.epoch,
        taken.renewed_at,
        taken.expires_at,
    )

    wait_expired(a)
    assert a.campaign() == 3
    assert a.renew(1) is False, "its own holder, an epoch it no longer holds"
    assert a.renew(2) is False
    assert a.renew(3) is True


def test_a_released_lease_is_taken_at_the_next_try(replicas):
    """SDD §11: a release sets expires_at to the database's now(), so the lease is expired at once
    and the next try takes it - not half a minute later."""
    a, b = lease_of(replicas[0], A, 30.0), lease_of(replicas[1], B)
    assert a.campaign() == 1
    assert b.campaign() is None
    assert a.release(1) is True
    assert b.read().expired
    assert b.campaign() == 2
    assert a.release(1) is False, "a released lease taken by another is not released again"


def _database_clock(store: Store) -> datetime:
    """The database's own clock, read in SQL of this test's - not the lease module's."""
    with store.engine.connect() as conn:
        if conn.dialect.name == "postgresql":
            return conn.execute(sa.text("SELECT clock_timestamp()")).scalar_one()
        text = conn.execute(sa.text("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')")).scalar_one()
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


#: a date or a time of day written as text - never an address's port (192.0.2.10:8080)
_TIME_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}|\d:\d{2}:\d{2}")


def _time_like(value: Any) -> bool:
    """A time, a date or a duration as Python sends one, or a string holding a date or a time."""
    if isinstance(value, datetime | date | time_of_day | timedelta):
        return True
    return isinstance(value, str) and _TIME_TEXT.search(value) is not None


def _values(parameters: Any) -> list[Any]:
    """The values a statement sent, whatever shape its driver takes them in: a mapping, a sequence,
    or a sequence of either for an executemany."""
    if isinstance(parameters, dict):
        return list(parameters.values())
    if isinstance(parameters, list | tuple):
        if parameters and all(isinstance(p, dict | list | tuple) for p in parameters):
            return [value for group in parameters for value in _values(group)]
        return list(parameters)
    return []


def test_the_lease_reads_and_sets_only_the_databases_time(replicas):
    """SDD §11, §25.3: no statement of the lease sends a time - a replica's clock never reaches the
    row -, and every time the row holds lies between the database's own clock read before and
    after: a campaign, a renewal, a takeover and a release all stamp it with the database's
    now()."""
    a, b = lease_of(replicas[0], A), lease_of(replicas[1], B)
    sent: list[tuple[str, Any]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        sent.append((statement, parameters))

    for store in replicas:
        sa.event.listen(store.engine, "before_cursor_execute", record)
    before = _database_clock(replicas[0])
    try:
        assert a.campaign() == 1
        assert a.renew(1)
        wait_expired(b)
        assert b.campaign() == 2
        assert b.renew(2)
        assert b.release(2)
        row = a.read()
    finally:
        for store in replicas:
            sa.event.remove(store.engine, "before_cursor_execute", record)
    after = _database_clock(replicas[0])
    assert any("leases" in statement for statement, _ in sent)
    values = [value for _, parameters in sent for value in _values(parameters)]
    assert "seamless-b" in values and "192.0.2.11:8080" in values, "the parse read the values"
    assert [value for value in values if _time_like(value)] == []
    assert before <= row.acquired_at <= row.renewed_at <= row.expires_at <= after
    assert row.expired, "released: expired at the database's now()"


def test_the_lease_runs_no_returning_clause(replicas):
    """SDD §11: the SQLite of RHEL 9 - the UBI 9 image's, 3.34.1 - has no RETURNING (SQLite 3.35
    brought it), and a campaign that sent one stopped `seamless serve` in the image. No statement
    of a first campaign, a renewal, a takeover or a release has the clause, on either engine."""
    a, b = lease_of(replicas[0], A), lease_of(replicas[1], B)
    sent: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        sent.append(statement)

    for store in replicas:
        sa.event.listen(store.engine, "before_cursor_execute", record)
    try:
        assert a.campaign() == 1
        assert b.campaign() is None
        assert a.renew(1)
        wait_expired(b)
        assert b.campaign() == 2
        assert b.release(2)
    finally:
        for store in replicas:
            sa.event.remove(store.engine, "before_cursor_execute", record)
    assert any(statement.lstrip().upper().startswith("INSERT") for statement in sent)
    assert any(statement.lstrip().upper().startswith("UPDATE") for statement in sent)
    assert [s for s in sent if re.search(r"\bRETURNING\b", s, re.IGNORECASE)] == []


# -- the fence (SDD §11, §25.4 layer 1) ---------------------------------------------------------
def _an_event(kind: str = "plan.updated") -> Event:
    return Event(kind=kind, message="written by a replica")


#: each write of the store (SDD §11), against a provider src-osp at version 1 and an event of
#: January 2026
WRITES: dict[str, Callable[[Store], object]] = {
    "put": lambda s: s.put("provider", make_provider(name="renamed")),
    "put_new": lambda s: s.put("provider", make_provider(id="dst-new"), expected_version=0),
    "put_versioned": lambda s: s.put("provider", make_provider(name="renamed"), expected_version=1),
    "put_with_event": lambda s: s.put_with_event(
        "provider", make_provider(name="renamed"), _an_event(), 1
    ),
    "put_all_with_events": lambda s: s.put_all_with_events(
        [("provider", make_provider(name="renamed"), 1)], [_an_event()]
    ),
    "append_event": lambda s: s.append_event(_an_event()),
    "delete": lambda s: s.delete("provider", "src-osp"),
    "delete_versioned": lambda s: s.delete("provider", "src-osp", expected_version=1),
    "delete_with_events": lambda s: s.delete_with_events(
        "provider", "src-osp", 1, [_an_event("provider.deleted")]
    ),
    "prune_events": lambda s: s.prune_events(datetime.now(UTC), "cli"),
    "delete_events_before": lambda s: s.delete_events_before(datetime.now(UTC)),
    "create_user": lambda s: s.create_user(
        DashboardUser(id="usr-sari", name="sari", role=Role.admin, sha256="0" * 64), "admin"
    ),
}


@pytest.mark.parametrize("write", list(WRITES))
def test_a_write_of_an_old_epoch_writes_nothing(any_store, write):
    """SDD §11, §25.4: a server's write reads the lease's epoch first; at an epoch that is not the
    lease's it raises Fenced, a StoreError carrying both, and nothing of it is written - each write
    method. The same write at the lease's epoch is written."""
    any_store.put("provider", make_provider(), expected_version=0)
    any_store.append_event(
        Event(kind="plan.created", message="seeded", ts=datetime(2026, 1, 1, tzinfo=UTC))
    )
    took_over(any_store)
    old = Store(any_store.url, fenced=True)
    try:
        old.epoch = 1
        held = contents(any_store)
        with pytest.raises(Fenced) as refused:
            WRITES[write](old)
        assert isinstance(refused.value, StoreError)
        assert (refused.value.own, refused.value.lease) == (1, 2)
        assert contents(any_store) == held
        old.epoch = 2
        WRITES[write](old)
        assert contents(any_store) != held
    finally:
        old.dispose()


async def test_a_server_process_without_the_lease_writes_nothing(any_store, tmp_path, monkeypatch):
    """SDD §11, §15: the store `seamless serve` builds is fenced. While another replica holds the
    lease, the server writes nothing - its start waits: no demo seed, no orchestrator - and its
    store refuses a write; once the lease is released the server takes it at its next try, with
    the next epoch and a holder of its host name and 8 hex digits, and runs as before. Its stop
    releases the lease."""
    monkeypatch.setattr(lease_module, "RETRY_S", 0.05)
    other = lease_of(any_store, A, 30.0)
    assert other.campaign() == 1
    settings = Settings(
        data_dir=tmp_path / "data",
        db_url=any_store.url,
        secrets_dir=tmp_path / "secrets",
        demo=True,
        demo_speed=1.0,
        demo_failure_rate=0.0,
        auth_disabled=True,
        dashboard_dir=None,
    )
    app = cli.build_app(settings)
    services = app.state.services
    server = services.store
    assert server.fenced and server.epoch is None
    started, stop = asyncio.Event(), asyncio.Event()

    async def serve() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(serve())
    try:
        await asyncio.sleep(0.5)
        assert not started.is_set()
        assert not services.orchestrator.health()["running"]
        assert contents(any_store) == ([], [])
        with pytest.raises(Fenced):
            await asyncio.to_thread(server.put, "provider", make_provider())
        assert contents(any_store) == ([], [])

        assert other.release(1)
        await asyncio.wait_for(started.wait(), 30)
        assert server.epoch == 2
        row = other.read()
        assert row.epoch == 2 and not row.expired
        assert re.fullmatch(rf"{re.escape(socket.gethostname())}-[0-9a-f]{{8}}", row.holder)
        assert row.address.endswith(":8080")
        assert services.orchestrator.health()["running"]
        assert any_store.list("provider", Provider), "the demo seed ran under the lease"
    finally:
        stop.set()
        await asyncio.wait_for(task, 60)
        server.dispose()
    assert other.read().expired, "the stop released the lease"


def test_a_refused_write_is_logged_with_both_epochs(any_store, caplog):
    """SDD §11, §25.8: the store logs each write the fence refused, with this process's epoch and
    the lease's - what a deposed replica's log shows the lab (§25.10) -, also for a store that never
    took the lease, before anybody took it or after."""
    old, never = Store(any_store.url, fenced=True), Store(any_store.url, fenced=True)
    try:
        with caplog.at_level(logging.WARNING, logger="seamless_migrate.store"):
            with pytest.raises(Fenced) as nobody:
                never.put("migration", make_migration(id="mig-nobody-leads"))
            took_over(any_store)
            old.epoch = 1
            with pytest.raises(Fenced):
                old.put("migration", make_migration(id="mig-old-epoch"))
            with pytest.raises(Fenced) as refused:
                never.append_event(_an_event())
    finally:
        old.dispose()
        never.dispose()
    assert (nobody.value.own, nobody.value.lease) == (None, None)
    assert (refused.value.own, refused.value.lease) == (None, 2)
    assert contents(any_store) == ([], [])
    records = [r for r in caplog.records if r.name == "seamless_migrate.store"]
    assert [r.levelno for r in records] == [logging.WARNING] * 3
    first, second, third = (r.getMessage() for r in records)
    assert "mig-nobody-leads" in first and "epoch is None, the lease's is None" in first
    assert "mig-old-epoch" in second and "this process's epoch is 1, the lease's is 2" in second
    assert "plan.updated" in third and "this process's epoch is None, the lease's is 2" in third


def test_a_replica_that_does_not_lead_still_records_auth_denied(any_store, tmp_path):
    """SDD §11, §13.1: auth.denied is written without the fence - the replica that refuses a request
    records it, leading or not -, while every other write of that replica is refused."""
    assert lease_of(any_store, A, 30.0).campaign() == 1
    settings, _ = api_settings(tmp_path)
    follower = Store(any_store.url, fenced=True)
    try:
        client = TestClient(create_app(settings, follower))  # no lifespan: it does not lead
        answer = client.get("/api/v1/plans", headers={"Authorization": "Bearer smg_" + "x" * 43})
        assert answer.status_code == 401
        denied = [e for e in any_store.events(limit=100) if e.kind == "auth.denied"]
        assert [(e.data["reason"], e.data["path"]) for e in denied] == [
            ("missing or invalid bearer token", "/api/v1/plans")
        ]
        with pytest.raises(Fenced):
            follower.append_event(_an_event("plan.created"))
    finally:
        follower.dispose()
    assert [e.kind for e in any_store.events(limit=100)] == ["auth.denied"]


def test_the_clis_store_writes_without_a_lease(any_store, tmp_path, monkeypatch, capsys):
    """SDD §11, §15: the CLI's store holds no lease and checks none - its writes are a person's
    (actor cli) -, so `plan apply` and `events prune` write while a server holds the lease at an
    epoch the CLI never took."""
    took_over(any_store)
    any_store.put("provider", make_provider(id="src-osp"), expected_version=0)
    any_store.put(
        "provider",
        make_provider(id="dst-rhoso", kind=ProviderKind.rhoso, role=ProviderRole.destination),
        expected_version=0,
    )
    for key in list(os.environ):
        if key.startswith("SEAMLESS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SEAMLESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SEAMLESS_DB_URL", any_store.url)
    path = tmp_path / "plan.yaml"
    plan = {
        "name": "Finance web tier",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": ["vm-1"],
    }
    path.write_text(yaml.safe_dump(plan))
    assert cli.main(["plan", "apply", "-f", str(path)]) == 0, capsys.readouterr().err
    [stored] = any_store.list("plan", Plan)
    assert stored.name == "Finance web tier"
    assert [(e.kind, e.actor) for e in any_store.events(limit=10)] == [("plan.created", "cli")]
    assert cli.main(["events", "prune", "--older-than-days", "0", "--confirm"]) == 0
    assert [(e.kind, e.actor) for e in any_store.events(limit=10)] == [("events.pruned", "cli")]
    assert (lease_of(any_store, B).read().holder, lease_of(any_store, B).read().epoch) == (
        "seamless-b",
        2,
    )


# -- PostgreSQL's row locks (SDD §11, §25.4) ----------------------------------------------------
@contextlib.contextmanager
def a_write_in_flight(store: Store) -> Iterator[list[object]]:
    """A write of ``store`` held open right before its insert runs - its fence read done, its
    key-share lock held - until the block ends; yields what the write returned or raised."""
    in_flight, hold = threading.Event(), threading.Event()
    outcome: list[object] = []

    def pause(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO DOCUMENTS"):
            in_flight.set()
            hold.wait(30)

    def write() -> None:
        try:
            outcome.append(store.put("provider", make_provider(), expected_version=0))
        except Exception as exc:  # the test reads it
            outcome.append(exc)

    sa.event.listen(store.engine, "before_cursor_execute", pause)
    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert in_flight.wait(15), "the write never reached its insert"
        yield outcome
    finally:
        hold.set()
        writer.join(30)
        sa.event.remove(store.engine, "before_cursor_execute", pause)


def a_takeover_waits_for_a_write_in_flight(url: str) -> None:
    """The leader's write of epoch 1 is in flight, its fence read done. A campaign meanwhile: on
    the valid lease it returns at once - a valid lease is never locked -; once the lease expired, a
    try whose lock wait outlasts its retry period takes nothing, and a patient one waits until the
    write committed, then takes the lease. The old epoch's next write is refused."""
    lead, elector, other = Store(url, fenced=True), Store(url), Store(url)
    lead.create_schema()
    lead.truncate_for_tests()
    try:
        assert lease_of(elector, A).campaign() == 1
        lead.epoch = 1
        quick = lease_of(other, B, retry_s=1.0)
        patient = lease_of(other, B, retry_s=15.0)  # outlasts the write held here
        taken: list[int | None] = []
        with a_write_in_flight(lead) as written:
            started = time.monotonic()
            assert quick.campaign() is None
            assert time.monotonic() - started < 0.5, "a valid lease is never locked"
            wait_expired(patient)
            started = time.monotonic()
            assert quick.campaign() is None, "a try that waited out its retry period took it"
            assert time.monotonic() - started >= 0.9, "the try did not wait for the write"
            taker = threading.Thread(target=lambda: taken.append(patient.campaign()))
            taker.start()
            taker.join(0.75)
            assert taker.is_alive(), "the takeover did not wait for the write in flight"
            assert taken == [] and written == []
        taker.join(30)
        assert written == [1], "the write of the old epoch committed before the takeover"
        assert taken == [2]
        assert other.get("provider", "src-osp", Provider) == make_provider()
        with pytest.raises(Fenced):
            lead.put("provider", make_provider(name="after the takeover"))
    finally:
        for store in (lead, elector, other):
            store.truncate_for_tests()
            store.dispose()


@pg_only
def test_a_takeover_waits_for_a_write_of_the_old_epoch_in_flight():
    """SDD §11, §25.4: the takeover's FOR UPDATE waits for the key-share lock of a write of the old
    epoch in flight, so that write commits before the takeover or not at all; the campaign's
    lock_timeout of the retry period bounds the wait of a try."""
    a_takeover_waits_for_a_write_in_flight(PG_URL)


def test_a_takeover_on_sqlite_waits_for_a_write_of_the_old_epoch_in_flight(tmp_path):
    """SDD §11: on SQLite a server's write begins with BEGIN IMMEDIATE, which holds the database's
    write lock from its fence read to its commit, so a takeover - BEGIN IMMEDIATE too, its wait
    bounded by the retry period - comes before the write or after it, never between."""
    a_takeover_waits_for_a_write_in_flight(f"sqlite:///{tmp_path / 'seamless.db'}")


@pg_only
def test_a_renewal_waits_for_no_write_in_flight():
    """SDD §11, §25.4: a renewal changes no key column, so it takes no lock a write's key-share lock
    holds up: it renews at once while a write of the leader is in flight, and the write lands."""
    lead, elector = Store(PG_URL, fenced=True), Store(PG_URL)
    lead.create_schema()
    lead.truncate_for_tests()
    try:
        a = lease_of(elector, A, 30.0)
        assert a.campaign() == 1
        lead.epoch = 1
        before = a.read()
        with a_write_in_flight(lead) as written:
            started = time.monotonic()
            renewed = a.renew(1)
            took = time.monotonic() - started
            assert renewed is True and took < 1.0, took
            assert written == []
        assert written == [1]
        assert a.read().expires_at > before.expires_at
    finally:
        for store in (lead, elector):
            store.truncate_for_tests()
            store.dispose()


@pg_only
def test_two_replicas_creating_the_schema_at_once_both_start():
    """SDD §11, §25.10: two replicas starting at once on an empty database both create the schema
    without an error - concurrent CREATE TABLE of one name fails on PostgreSQL's catalog, so
    create_schema() runs under the cross-process lock - and then campaign: one of them leads.
    Each round runs in a schema of its own, so the shared test database keeps its tables."""
    owner = Store(PG_URL)
    url = sa.engine.make_url(PG_URL)

    def start(store: Store, barrier: threading.Barrier, failures: list[BaseException]) -> None:
        barrier.wait()
        try:
            store.create_schema()
        except BaseException as exc:  # the test reads it
            failures.append(exc)

    try:
        for _ in range(5):
            schema = f"race_{secrets.token_hex(4)}"
            with owner.engine.begin() as conn:
                conn.execute(sa.text(f"CREATE SCHEMA {schema}"))
            scoped = url.update_query_dict({"options": f"-csearch_path={schema}"})
            pair = [Store(scoped.render_as_string(hide_password=False)) for _ in range(2)]
            barrier = threading.Barrier(2)
            failures: list[BaseException] = []
            try:
                threads = [
                    threading.Thread(target=start, args=(store, barrier, failures))
                    for store in pair
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(60)
                assert failures == []
                replicas = zip(pair, (A, B), strict=True)
                epochs = [lease_of(store, replica, 30.0).campaign() for store, replica in replicas]
                assert epochs == [1, None]
            finally:
                for store in pair:
                    store.dispose()
                with owner.engine.begin() as conn:
                    conn.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
    finally:
        owner.dispose()


@pg_only
def test_a_role_that_does_not_own_the_tables_takes_the_lease_once_the_owner_created_them():
    """SDD §11, §25.9, Security.md §9.4: the application role - no table ownership, SELECT, INSERT
    and UPDATE on leases - starts the control plane once the owner created the schema
    (create_schema runs no DDL), takes, renews and releases the lease, and writes under its fence:
    FOR KEY SHARE asks PostgreSQL for UPDATE too."""
    owner = Store(PG_URL)
    owner.create_schema()
    owner.truncate_for_tests()
    url = sa.engine.make_url(PG_URL)
    role, password = f"seamless_app_{secrets.token_hex(4)}", secrets.token_urlsafe(16)
    with owner.engine.begin() as conn:
        conn.execute(sa.text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
        conn.execute(sa.text(f'GRANT CONNECT ON DATABASE "{url.database}" TO {role}'))
        conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO {role}"))
        conn.execute(sa.text(f"GRANT SELECT, INSERT ON events TO {role}"))
        conn.execute(sa.text(f"GRANT USAGE, SELECT ON SEQUENCE events_seq_seq TO {role}"))
        conn.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE ON leases TO {role}"))
    app_url = url.set(username=role, password=password).render_as_string(hide_password=False)
    app = Store(app_url, fenced=True)
    try:
        seen: list[str] = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement.strip())

        sa.event.listen(app.engine, "before_cursor_execute", capture)
        try:
            app.create_schema()  # what every server start runs
        finally:
            sa.event.remove(app.engine, "before_cursor_execute", capture)
        assert [s for s in seen if s.split()[0].upper() in ("CREATE", "ALTER", "DROP")] == []
        lease = Lease(app.engine, *A, 30.0)
        app.epoch = lease.campaign()
        assert app.epoch == 1
        migration = make_migration()
        assert app.put("migration", migration, expected_version=0) == 1
        app.append_event(Event(kind="plan.created", message="as the application role"))
        assert lease.renew(1) and lease.release(1)
        assert owner.get("migration", migration.id, type(migration)) == migration
    finally:
        app.dispose()
        with owner.engine.begin() as conn:
            conn.execute(sa.text(f"REVOKE ALL ON documents, events, leases FROM {role}"))
            conn.execute(sa.text(f"REVOKE ALL ON SEQUENCE events_seq_seq FROM {role}"))
            conn.execute(sa.text(f'REVOKE CONNECT ON DATABASE "{url.database}" FROM {role}'))
            conn.execute(sa.text(f"DROP ROLE {role}"))
        owner.truncate_for_tests()
        owner.dispose()
