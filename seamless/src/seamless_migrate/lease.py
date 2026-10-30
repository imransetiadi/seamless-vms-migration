"""The orchestrator's lease (SDD §11, §25.3; plan task HA2).

One row of ``leases`` elects the replica that runs the orchestrator. Every time the lease sets or
compares is the database's - ``now()`` on PostgreSQL, ``'now'`` on SQLite - and no statement sends
one, so the replicas' clocks may disagree by any amount. The epoch that each takeover raises fences
a server's writes (``Store(fenced=True)``, §25.4). A server's elector (``seamless_migrate.leader``,
§8; plan task HA3) campaigns and renews on a schedule, on an engine of its own (``lease_engine``),
and writes ``leader.acquired`` and ``leader.released`` in the transactions that take and release
the lease; the leader writes its orchestrator's health into the row with each renewal and, while its
tick loop is unhealthy, alone, and a takeover clears it (§12, §25.6; plan task HA4). A replica takes
a lapsed lease that names itself - the term a deposition let lapse - only once it has been expired
for the pause, three retry periods, so the other replicas take it first (§8, §11; the owner's
decision of 2026-10-29). No statement sends RETURNING, which RHEL 9's SQLite (3.34.1) does not have.
"""

from __future__ import annotations

import contextlib
import logging
import math
import secrets
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement

from .store import ORCHESTRATOR_LEASE, SQLITE_BUSY_TIMEOUT_MS, build_engine, leases

log = logging.getLogger(__name__)

#: how often a replica that does not lead tries the lease, and how long one try waits for a lock
#: held on its row (SDD §11, §25.3)
RETRY_S = 2.0
#: how many retry periods a lapsed lease that names the replica itself must have been expired before
#: it takes the lease again (SDD §8, §11): a follower, trying every retry period, takes it first
PAUSE_PERIODS = 3

#: SQLite's clock to the millisecond, in the one format every time of the lease is written in, so
#: that two of them compare as text as they compare as times
_SQLITE_CLOCK = "strftime('%Y-%m-%d %H:%M:%f', 'now'{})"


class db_now(FunctionElement):  # noqa: N801 - named as the SQL it renders
    """The database's clock (SDD §11)."""

    type = sa.DateTime(timezone=True)
    inherit_cache = True


class db_later(FunctionElement):  # noqa: N801 - named as the SQL it renders
    """The database's clock plus a number of seconds, its one argument (SDD §11)."""

    type = sa.DateTime(timezone=True)
    inherit_cache = True


@compiles(db_now, "postgresql")
def _postgresql_now(element: Any, compiler: Any, **kw: Any) -> str:
    return "now()"


@compiles(db_now, "sqlite")
def _sqlite_now(element: Any, compiler: Any, **kw: Any) -> str:
    return _SQLITE_CLOCK.format("")


@compiles(db_later, "postgresql")
def _postgresql_later(element: Any, compiler: Any, **kw: Any) -> str:
    seconds = compiler.process(element.clauses, **kw)
    return f"now() + make_interval(secs => CAST({seconds} AS DOUBLE PRECISION))"


@compiles(db_later, "sqlite")
def _sqlite_later(element: Any, compiler: Any, **kw: Any) -> str:
    seconds = compiler.process(element.clauses, **kw)
    # the sign always written: SQLite reads '+-0.300 seconds' as no time at all
    return _SQLITE_CLOCK.format(f", printf('%+.3f seconds', {seconds})")


def _aware(value: datetime) -> datetime:
    """A time of the row as UTC: SQLite keeps no offset, PostgreSQL's timestamptz has one."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class LeaseRow:
    """The lease as one read found it; ``expired`` is the database's judgement at that read, and
    ``health`` the orchestrator's object its leader last wrote, None since a takeover (SDD §12)."""

    holder: str
    address: str
    epoch: int
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime
    expired: bool
    health: dict[str, Any] | None = None


def _expired() -> Any:
    """The lease is expired once the database's clock reaches its expiry (SDD §11)."""
    return leases.c.expires_at <= db_now()


def read_lease(engine: Engine, name: str = ORCHESTRATOR_LEASE) -> LeaseRow | None:
    """The row as it is now on ``engine``, whether it expired judged on the database's clock, or
    None while nobody ever took the lease (SDD §11, §12): a read alone, which locks nothing - the
    elector's on its own engine, a replica's health and relay on the store's, and ``seamless
    status``'s, which takes no lease (§15)."""
    query = sa.select(
        leases.c.holder,
        leases.c.address,
        leases.c.epoch,
        leases.c.acquired_at,
        leases.c.renewed_at,
        leases.c.expires_at,
        _expired().label("expired"),
        leases.c.health,
    ).where(leases.c.name == name)
    with engine.connect() as conn:
        row = conn.execute(query).first()
    if row is None:
        return None
    return LeaseRow(
        holder=row.holder,
        address=row.address,
        epoch=int(row.epoch),
        acquired_at=_aware(row.acquired_at),
        renewed_at=_aware(row.renewed_at),
        expires_at=_aware(row.expires_at),
        expired=bool(row.expired),
        health=row.health if isinstance(row.health, dict) else None,
    )


def new_holder(host: str | None = None) -> str:
    """A replica's holder: its host name and 8 random hex digits, new at each process start (SDD
    §11)."""
    return f"{socket.gethostname() if host is None else host}-{secrets.token_hex(4)}"


def replica_address(port: int, host: str | None = None) -> str:
    """Where the other replicas reach this one's API: its host name's address and ``port``, or the
    host name itself when it resolves to no address (SDD §11)."""
    name = socket.gethostname() if host is None else host
    try:
        address = str(socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)[0][4][0])
    except (OSError, IndexError):
        address = name
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


#: PostgreSQL's lock_timeout (lock_not_available) and statement_timeout (query_canceled)
_WAITS_ENDED = frozenset({"55P03", "57014"})


def _lock_wait_ended(exc: sa.exc.OperationalError) -> bool:
    """Whether a statement gave up waiting within the retry period: PostgreSQL's lock_timeout
    (SQLSTATE 55P03) or statement_timeout (57014), SQLite's wait for another connection's write
    lock."""
    cause = exc.orig
    return getattr(cause, "sqlstate", None) in _WAITS_ENDED or "database is locked" in str(cause)


@dataclass(frozen=True)
class Taken:
    """What a campaign took (SDD §11): the epoch, and the holder and epoch it took over from -
    None for the first lease."""

    epoch: int
    previous_holder: str | None
    previous_epoch: int | None


def lease_engine(url: str, retry_s: float | None = None) -> Engine:
    """The elector's own engine (SDD §8): one connection outside the store's pool, its connects
    bounded by the retry period - PostgreSQL's ``connect_timeout``, whole seconds and 2 at least;
    each transaction bounds its statements and its lock waits (``Lease._bounded``)."""
    retry = RETRY_S if retry_s is None else float(retry_s)
    if make_url(url).get_backend_name() == "sqlite":
        return build_engine(url)
    return sa.create_engine(
        url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=retry,
        pool_pre_ping=True,
        connect_args={"connect_timeout": max(2, math.ceil(retry))},
    )


class Lease:
    """One replica's hand on the lease row (SDD §11, §25.3): each call a short transaction of its
    own on ``engine``, its waits for a lock bounded by the retry period."""

    def __init__(
        self,
        engine: Engine,
        holder: str,
        address: str,
        lease_s: float,
        *,
        retry_s: float | None = None,
        name: str = ORCHESTRATOR_LEASE,
    ) -> None:
        self.engine = engine
        self.holder = holder
        self.address = address
        self.lease_s = float(lease_s)
        self.retry_s = RETRY_S if retry_s is None else float(retry_s)
        #: how long its own lapsed lease must have been expired before it takes it again (SDD §11)
        self.pause_s = PAUSE_PERIODS * self.retry_s
        self.name = name

    def _takeable(self) -> Any:
        """Whether a campaign takes the lease (SDD §11): once it expired - and once it has been
        expired for the pause where it names this replica, which only a deposition leaves it to
        campaign for (§8): a replica that stops campaigns no more, and a process starts as a new
        holder."""
        return sa.and_(
            _expired(),
            sa.or_(
                leases.c.holder != self.holder,
                leases.c.expires_at <= db_later(sa.literal(-self.pause_s, sa.Float())),
            ),
        )

    def _later(self) -> Any:
        return db_later(sa.literal(self.lease_s, sa.Float()))

    def read(self) -> LeaseRow | None:
        """The row as it is now, or None while nobody ever took the lease (:func:`read_lease`)."""
        return read_lease(self.engine, self.name)

    def campaign(self, record: Callable[[sa.Connection, Taken], None] | None = None) -> int | None:
        """One try for the lease: the epoch this replica took, else None (SDD §11). With no row it
        inserts one with epoch 1; an expired lease it locks FOR UPDATE - which waits for every write
        of the old epoch in flight - and takes in that transaction with the next epoch, where it is
        still expired; a valid one it never locks. Its own lapsed lease counts as expired only once
        it has been expired for the pause (§8). ``record`` runs in the transaction that took the
        lease - a server's leader.acquired (§8): when it fails, nothing is taken. A try whose wait
        outlasts the retry period takes nothing."""
        try:
            with self.engine.connect() as conn:
                with self._bounded(conn, write=False):
                    row = conn.execute(
                        sa.select(leases.c.epoch, self._takeable().label("takeable")).where(
                            leases.c.name == self.name
                        )
                    ).first()
                if row is None:
                    return self._take_first(conn, record)
                if not row.takeable:
                    return None
                return self._take_over(conn, record)
        except sa.exc.OperationalError as exc:
            if not _lock_wait_ended(exc):
                raise
            log.info("lease %s: a lock on its row outlasted this try", self.name)
            return None

    def _take_first(
        self, conn: sa.Connection, record: Callable[[sa.Connection, Taken], None] | None
    ) -> int | None:
        """The first holder's row, epoch 1, where there was none; None when another replica's
        insert came first."""
        with self._bounded(conn):
            conn.execute(self._first(conn.dialect.name))
            # an INSERT's rowcount is no driver's promise, and RHEL 9's SQLite (3.34.1) has no
            # RETURNING: the row this transaction reads back as its holder's, or none
            first = conn.execute(
                sa.select(leases.c.epoch).where(
                    leases.c.name == self.name, leases.c.holder == self.holder
                )
            ).scalar()
            if first is None:
                return None
            taken = Taken(int(first), None, None)
            if record is not None:
                record(conn, taken)
        return taken.epoch

    def _take_over(
        self, conn: sa.Connection, record: Callable[[sa.Connection, Taken], None] | None
    ) -> int | None:
        """The expired lease, locked and taken with the next epoch; None when it was renewed or
        taken since it was read."""
        with self._bounded(conn):
            locked = conn.execute(
                sa.select(leases.c.epoch, leases.c.holder)
                .where(leases.c.name == self.name, self._takeable())
                .with_for_update()
            ).first()
            if locked is None:
                return None  # renewed or taken since it was read
            # the row is this transaction's until it ends, so its next epoch is known without
            # RETURNING, which RHEL 9's SQLite (3.34.1) does not have
            taken = Taken(int(locked.epoch) + 1, str(locked.holder), int(locked.epoch))
            conn.execute(
                leases.update()
                .where(leases.c.name == self.name)
                .values(
                    holder=self.holder,
                    address=self.address,
                    epoch=taken.epoch,
                    acquired_at=db_now(),
                    renewed_at=db_now(),
                    expires_at=self._later(),
                    # the last leader's orchestrator is not this one's (SDD §12)
                    health=sa.null(),
                )
            )
            if record is not None:
                record(conn, taken)
        return taken.epoch

    def _first(self, dialect: str) -> Any:
        """The row of the first holder, epoch 1; of two inserting at once, one takes it and the
        other's inserts nothing."""
        insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
        return (
            insert(leases)
            .values(
                name=self.name,
                holder=self.holder,
                address=self.address,
                epoch=1,
                acquired_at=db_now(),
                renewed_at=db_now(),
                expires_at=self._later(),
            )
            .on_conflict_do_nothing(index_elements=[leases.c.name])
        )

    def renew(self, epoch: int, health: dict[str, Any] | None = None) -> bool:
        """Push the expiry a lease past the database's now, where the lease is this replica's at
        ``epoch``, and with ``health`` write the orchestrator's object into the row (SDD §12);
        False when it renewed nothing - the lease was taken (§11). It changes no key column, so it
        waits for no write. A renewal whose wait outlasts the retry period raises, as a database
        error does: the lease may still be this replica's (§11, plan task HA3)."""
        values: dict[str, Any] = {"renewed_at": db_now(), "expires_at": self._later()}
        if health is not None:
            values["health"] = health
        with self.engine.connect() as conn, self._bounded(conn):
            return conn.execute(self._own(epoch, **values)).rowcount == 1

    def write_health(self, epoch: int, health: dict[str, Any]) -> bool:
        """Write the orchestrator's object into this replica's lease at ``epoch`` and renew
        nothing: a leader whose tick loop is unhealthy lets its lease lapse, yet every replica
        reports its orchestrator (SDD §12). False when it wrote nothing - the lease was taken."""
        with self.engine.connect() as conn, self._bounded(conn):
            return conn.execute(self._own(epoch, health=health)).rowcount == 1

    def release(self, epoch: int, record: Callable[[sa.Connection], None] | None = None) -> bool:
        """Expire this replica's lease at ``epoch`` now, so another one takes it at its next try;
        False when it was no longer this replica's (SDD §11). ``record`` runs in the same
        transaction where the lease was released - a server's leader.released (§8): when it fails,
        nothing is released."""
        try:
            with self.engine.connect() as conn, self._bounded(conn):
                if conn.execute(self._own(epoch, expires_at=db_now())).rowcount != 1:
                    return False
                if record is not None:
                    record(conn)
                return True
        except sa.exc.OperationalError as exc:
            if not _lock_wait_ended(exc):
                raise
            log.info("lease %s: a lock on its row outlasted this replica's release", self.name)
            return False

    def _own(self, epoch: int, **values: Any) -> Any:
        """The update of this replica's lease at ``epoch``: no row once another one took it."""
        return (
            leases.update()
            .where(
                leases.c.name == self.name,
                leases.c.holder == self.holder,
                leases.c.epoch == epoch,
            )
            .values(**values)
        )

    @contextlib.contextmanager
    def _bounded(self, conn: sa.Connection, *, write: bool = True) -> Iterator[None]:
        """One transaction on ``conn`` whose statements and lock waits last at most the retry
        period: PostgreSQL's ``statement_timeout`` and ``lock_timeout`` (SDD §8); on SQLite a
        ``write`` holds the database's write lock from its start (``BEGIN IMMEDIATE``), waited for
        within its busy timeout, and a read takes none."""
        ms = max(1, round(self.retry_s * 1000))
        if conn.dialect.name != "sqlite":
            with conn.begin():
                conn.execute(
                    sa.select(
                        sa.func.set_config("lock_timeout", f"{ms}ms", True),
                        sa.func.set_config("statement_timeout", f"{ms}ms", True),
                    )
                )
                yield
            return
        if not write:
            with conn.begin():
                yield
            return
        raw = conn.connection.dbapi_connection
        assert raw is not None
        raw.execute(f"PRAGMA busy_timeout = {ms}")
        try:
            with conn.begin():
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                yield
        finally:
            raw.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")


__all__ = [
    "PAUSE_PERIODS",
    "RETRY_S",
    "Lease",
    "LeaseRow",
    "Taken",
    "lease_engine",
    "new_holder",
    "read_lease",
    "replica_address",
]
