"""Document + event store on SQLAlchemy 2.0 Core (SDD §11).

PostgreSQL 16 is the production database; SQLite serves tests and quick local runs. All methods
are synchronous; async callers use :class:`AsyncStore`, which runs them in worker threads.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection, Iterable
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from .domain.models import Event

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

KINDS = ("provider", "plan", "migration")

JSONType = sa.JSON().with_variant(JSONB(), "postgresql")

metadata = sa.MetaData()

documents = sa.Table(
    "documents",
    metadata,
    sa.Column("kind", sa.String(32), primary_key=True),
    sa.Column("id", sa.String(128), primary_key=True),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("data", JSONType, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

events = sa.Table(
    "events",
    metadata,
    sa.Column("seq", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    sa.Column("kind", sa.String(64), nullable=False),
    sa.Column("plan_id", sa.String(64), nullable=True, index=True),
    sa.Column("migration_id", sa.String(64), nullable=True, index=True),
    sa.Column("actor", sa.String(128), nullable=False),
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("data", JSONType, nullable=False),
    sqlite_autoincrement=True,
)


class StoreError(Exception):
    """Base class for store errors."""


class ConflictError(StoreError):
    """Optimistic-concurrency conflict (stale version, or the document already exists)."""


class NotFound(StoreError):
    def __init__(self, kind: str, id_: str) -> None:
        super().__init__(f"{kind} {id_!r} not found")
        self.kind = kind
        self.id = id_


def _now() -> datetime:
    return datetime.now(UTC)


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


def build_engine(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        database = parsed.database or ""
        in_memory = database in ("", ":memory:") or database.startswith("file::memory:")
        if in_memory:
            return sa.create_engine(
                url, connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
        Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)
        engine = sa.create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
        sa.event.listen(engine, "connect", _sqlite_pragmas)
        return engine
    return sa.create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def _matches(value: Any, wanted: Any) -> bool:
    if isinstance(wanted, Enum):
        wanted = wanted.value
    if isinstance(wanted, Collection) and not isinstance(wanted, str | bytes):
        return any(_matches(value, w) for w in wanted)
    return value == wanted


class Store:
    """Synchronous store; see the module docstring."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = build_engine(url)

    # -- lifecycle ---------------------------------------------------------------------------
    def create_schema(self) -> None:
        metadata.create_all(self.engine, checkfirst=True)

    def dispose(self) -> None:
        self.engine.dispose()

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            return True
        except Exception:  # readiness probe must never raise
            log.debug("store ping failed", exc_info=True)
            return False

    def truncate_for_tests(self) -> None:
        """Delete every row (test helper; never used by the application)."""
        with self.engine.begin() as conn:
            conn.execute(documents.delete())
            conn.execute(events.delete())

    # -- documents ---------------------------------------------------------------------------
    def put(self, kind: str, model: BaseModel, expected_version: int | None = None) -> int:
        """Insert or update ``model``; return the new version.

        ``expected_version=None`` writes unconditionally, ``0`` requires the document to be new,
        ``n > 0`` requires the stored version to be ``n`` (else :class:`ConflictError`).
        """
        doc_id = str(model.id)  # type: ignore[attr-defined]
        data = model.model_dump(mode="json")
        attempts = 5 if expected_version is None else 1
        for _ in range(attempts):
            try:
                return self._put_once(kind, doc_id, data, expected_version)
            except ConflictError:
                if expected_version is not None:
                    raise
        raise ConflictError(f"{kind} {doc_id!r} is being updated concurrently")

    def _put_once(
        self, kind: str, doc_id: str, data: dict[str, Any], expected_version: int | None
    ) -> int:
        now = _now()
        key = (documents.c.kind == kind) & (documents.c.id == doc_id)
        try:
            with self.engine.begin() as conn:
                row = conn.execute(sa.select(documents.c.version).where(key)).first()
                if row is None:
                    if expected_version not in (None, 0):
                        raise ConflictError(f"{kind} {doc_id!r} does not exist")
                    conn.execute(
                        documents.insert().values(
                            kind=kind,
                            id=doc_id,
                            version=1,
                            data=data,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    return 1
                current = int(row.version)
                if expected_version is not None and current != expected_version:
                    raise ConflictError(
                        f"{kind} {doc_id!r} is at version {current}, expected {expected_version}"
                    )
                result = conn.execute(
                    documents.update()
                    .where(key & (documents.c.version == current))
                    .values(version=current + 1, data=data, updated_at=now)
                )
                if result.rowcount != 1:
                    raise ConflictError(f"{kind} {doc_id!r} changed concurrently")
                return current + 1
        except IntegrityError as exc:
            raise ConflictError(f"{kind} {doc_id!r} already exists") from exc

    def get_versioned(self, kind: str, id_: str, model_cls: type[M]) -> tuple[M, int]:
        with self.engine.connect() as conn:
            row = conn.execute(
                sa.select(documents.c.data, documents.c.version).where(
                    (documents.c.kind == kind) & (documents.c.id == id_)
                )
            ).first()
        if row is None:
            raise NotFound(kind, id_)
        return model_cls.model_validate(row.data), int(row.version)

    def get(self, kind: str, id_: str, model_cls: type[M]) -> M:
        return self.get_versioned(kind, id_, model_cls)[0]

    def list(self, kind: str, model_cls: type[M], **filters: Any) -> list[M]:
        """Documents of ``kind``; ``filters`` compare top-level JSON fields (in Python).

        A filter value that is a list/tuple/set matches any of its members.
        """
        with self.engine.connect() as conn:
            rows = conn.execute(
                sa.select(documents.c.data)
                .where(documents.c.kind == kind)
                .order_by(documents.c.created_at, documents.c.id)
            ).all()
        out: list[M] = []
        for row in rows:
            data = row.data
            if all(_matches(data.get(name), wanted) for name, wanted in filters.items()):
                out.append(model_cls.model_validate(data))
        return out

    def delete(self, kind: str, id_: str) -> None:
        with self.engine.begin() as conn:
            result = conn.execute(
                documents.delete().where((documents.c.kind == kind) & (documents.c.id == id_))
            )
        if result.rowcount == 0:
            raise NotFound(kind, id_)

    # -- events ------------------------------------------------------------------------------
    def append_event(self, event: Event) -> Event:
        payload = event.model_dump(mode="json")
        with self.engine.begin() as conn:
            result = conn.execute(
                events.insert().values(
                    ts=event.ts,
                    kind=event.kind,
                    plan_id=event.plan_id,
                    migration_id=event.migration_id,
                    actor=event.actor,
                    message=event.message,
                    data=payload["data"],
                )
            )
            seq = int(result.inserted_primary_key[0])
        return event.model_copy(update={"seq": seq})

    def events(
        self,
        since_seq: int = 0,
        plan_id: str | None = None,
        migration_id: str | None = None,
        limit: int = 500,
        kinds: Iterable[str] | None = None,
    ) -> list[Event]:
        query = sa.select(events).where(events.c.seq > since_seq)
        if plan_id is not None:
            query = query.where(events.c.plan_id == plan_id)
        if migration_id is not None:
            query = query.where(events.c.migration_id == migration_id)
        if kinds is not None:
            query = query.where(events.c.kind.in_(list(kinds)))
        query = query.order_by(events.c.seq).limit(max(0, int(limit)))
        with self.engine.connect() as conn:
            rows = conn.execute(query).mappings().all()
        return [
            Event(
                seq=row["seq"],
                ts=row["ts"],
                kind=row["kind"],
                plan_id=row["plan_id"],
                migration_id=row["migration_id"],
                actor=row["actor"],
                message=row["message"],
                data=row["data"] or {},
            )
            for row in rows
        ]

    def max_seq(self) -> int:
        with self.engine.connect() as conn:
            value = conn.execute(sa.select(sa.func.max(events.c.seq))).scalar()
        return int(value or 0)


class AsyncStore:
    """Async facade: every call runs the synchronous :class:`Store` method in a thread."""

    def __init__(self, store: Store) -> None:
        self.sync = store

    async def put(self, kind: str, model: BaseModel, expected_version: int | None = None) -> int:
        return await asyncio.to_thread(self.sync.put, kind, model, expected_version)

    async def get(self, kind: str, id_: str, model_cls: type[M]) -> M:
        return await asyncio.to_thread(self.sync.get, kind, id_, model_cls)

    async def get_versioned(self, kind: str, id_: str, model_cls: type[M]) -> tuple[M, int]:
        return await asyncio.to_thread(self.sync.get_versioned, kind, id_, model_cls)

    async def list(self, kind: str, model_cls: type[M], **filters: Any) -> list[M]:
        return await asyncio.to_thread(lambda: self.sync.list(kind, model_cls, **filters))

    async def delete(self, kind: str, id_: str) -> None:
        await asyncio.to_thread(self.sync.delete, kind, id_)

    async def append_event(self, event: Event) -> Event:
        return await asyncio.to_thread(self.sync.append_event, event)

    async def events(self, **kwargs: Any) -> list[Event]:
        return await asyncio.to_thread(lambda: self.sync.events(**kwargs))

    async def max_seq(self) -> int:
        return await asyncio.to_thread(self.sync.max_seq)

    async def ping(self) -> bool:
        return await asyncio.to_thread(self.sync.ping)


__all__ = [
    "AsyncStore",
    "ConflictError",
    "KINDS",
    "NotFound",
    "Store",
    "StoreError",
    "build_engine",
]
