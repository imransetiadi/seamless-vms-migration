"""The orchestrator's lease (SDD §11, §25.3; plan task HA2).

One row of ``leases`` elects the replica that runs the orchestrator. Every time the lease sets or
compares is the database's - ``now()`` on PostgreSQL, ``'now'`` on SQLite - and no statement sends
one, so the replicas' clocks may disagree by any amount. The epoch that each takeover raises fences
a server's writes (``Store(fenced=True)``, §25.4). The elector that renews the lease on a schedule
is §25.3's (plan task HA3); ``seamless serve`` takes it once at its start (§15).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement

from .store import ORCHESTRATOR_LEASE, SQLITE_BUSY_TIMEOUT_MS, leases

log = logging.getLogger(__name__)

#: how often a replica that does not lead tries the lease, and how long one try waits for a lock
#: held on its row (SDD §11, §25.3)
RETRY_S = 2.0

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
    return _SQLITE_CLOCK.format(f", printf('+%.3f seconds', {seconds})")


def _aware(value: datetime) -> datetime:
    """A time of the row as UTC: SQLite keeps no offset, PostgreSQL's timestamptz has one."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class LeaseRow:
    """The lease as one read found it; ``expired`` is the database's judgement at that read."""

    holder: str
    address: str
    epoch: int
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime
    expired: bool


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


def _lock_wait_ended(exc: sa.exc.OperationalError) -> bool:
    """Whether a statement gave up waiting for a lock: PostgreSQL's lock_timeout (SQLSTATE 55P03),
    SQLite's wait for another connection's write lock."""
    cause = exc.orig
    return getattr(cause, "sqlstate", None) == "55P03" or "database is locked" in str(cause)


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
        self.name = name

    def _expired(self) -> Any:
        """The lease is expired once the database's clock reaches its expiry (SDD §11)."""
        return leases.c.expires_at <= db_now()

    def _later(self) -> Any:
        return db_later(sa.literal(self.lease_s, sa.Float()))

    def read(self) -> LeaseRow | None:
        """The row as it is now, or None while nobody ever took the lease."""
        query = sa.select(
            leases.c.holder,
            leases.c.address,
            leases.c.epoch,
            leases.c.acquired_at,
            leases.c.renewed_at,
            leases.c.expires_at,
            self._expired().label("expired"),
        ).where(leases.c.name == self.name)
        with self.engine.connect() as conn:
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
        )

    def campaign(self) -> int | None:
        """One try for the lease: the epoch this replica took, else None (SDD §11). With no row it
        inserts one with epoch 1; an expired lease it locks FOR UPDATE - which waits for every write
        of the old epoch in flight - and takes in that transaction with the next epoch, where it is
        still expired; a valid one it never locks. A try whose lock wait outlasts the retry period
        takes nothing."""
        with self.engine.connect() as conn:
            with conn.begin():
                row = conn.execute(
                    sa.select(leases.c.epoch, self._expired().label("expired")).where(
                        leases.c.name == self.name
                    )
                ).first()
            if row is None:
                with conn.begin():
                    conn.execute(self._first(conn.dialect.name))
                    # an INSERT's rowcount is no driver's promise, and RHEL 9's SQLite (3.34.1) has
                    # no RETURNING: the row this transaction reads back as its holder's, or none
                    first = conn.execute(
                        sa.select(leases.c.epoch).where(
                            leases.c.name == self.name, leases.c.holder == self.holder
                        )
                    ).scalar()
                return None if first is None else int(first)
            if not row.expired:
                return None
            try:
                with self._bounded(conn):
                    locked = conn.execute(
                        sa.select(leases.c.epoch)
                        .where(leases.c.name == self.name, self._expired())
                        .with_for_update()
                    ).first()
                    if locked is None:
                        return None  # renewed or taken since it was read
                    # the row is this transaction's until it ends, so its next epoch is known
                    # without RETURNING, which RHEL 9's SQLite (3.34.1) does not have
                    taken = int(locked.epoch) + 1
                    conn.execute(
                        leases.update()
                        .where(leases.c.name == self.name)
                        .values(
                            holder=self.holder,
                            address=self.address,
                            epoch=taken,
                            acquired_at=db_now(),
                            renewed_at=db_now(),
                            expires_at=self._later(),
                        )
                    )
                    return taken
            except sa.exc.OperationalError as exc:
                if not _lock_wait_ended(exc):
                    raise
                log.info("lease %s: a lock on its row outlasted this try", self.name)
                return None

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

    def renew(self, epoch: int) -> bool:
        """Push the expiry a lease past the database's now, where the lease is this replica's at
        ``epoch``; False when it renewed nothing - the lease was taken (SDD §11). It changes no key
        column, so it waits for no write."""
        return self._own(epoch, renewed_at=db_now(), expires_at=self._later())

    def release(self, epoch: int) -> bool:
        """Expire this replica's lease at ``epoch`` now, so another one takes it at its next try;
        False when it was no longer this replica's (SDD §11)."""
        return self._own(epoch, expires_at=db_now())

    def _own(self, epoch: int, **values: Any) -> bool:
        statement = (
            leases.update()
            .where(
                leases.c.name == self.name,
                leases.c.holder == self.holder,
                leases.c.epoch == epoch,
            )
            .values(**values)
        )
        try:
            with self.engine.connect() as conn, self._bounded(conn):
                return conn.execute(statement).rowcount == 1
        except sa.exc.OperationalError as exc:
            if not _lock_wait_ended(exc):
                raise
            log.info("lease %s: a lock on its row outlasted this replica's update", self.name)
            return False

    @contextlib.contextmanager
    def _bounded(self, conn: sa.Connection) -> Iterator[None]:
        """One transaction on ``conn`` whose wait for a lock lasts at most the retry period:
        PostgreSQL's ``lock_timeout``, SQLite's busy timeout around ``BEGIN IMMEDIATE``."""
        ms = max(1, round(self.retry_s * 1000))
        if conn.dialect.name != "sqlite":
            with conn.begin():
                conn.execute(sa.select(sa.func.set_config("lock_timeout", f"{ms}ms", True)))
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


async def take(lease: Lease) -> int:
    """Try the lease every retry period until this replica takes it; the epoch it took (SDD §15):
    a server writes nothing before."""
    said = False
    while True:
        epoch = await asyncio.to_thread(lease.campaign)
        if epoch is not None:
            log.info("took the orchestrator lease with epoch %d as %s", epoch, lease.holder)
            return epoch
        if not said:
            held = await asyncio.to_thread(lease.read)
            log.warning(
                "another server holds the orchestrator lease (%s, epoch %s): waiting for it",
                held.holder if held else "nobody",
                held.epoch if held else None,
            )
            said = True
        await asyncio.sleep(lease.retry_s)


__all__ = ["RETRY_S", "Lease", "LeaseRow", "new_holder", "replica_address", "take"]
