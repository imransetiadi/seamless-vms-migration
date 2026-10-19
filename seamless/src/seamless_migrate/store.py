"""Document + event store on SQLAlchemy 2.0 Core (SDD §11).

PostgreSQL 16 is the production database; SQLite serves tests and quick local runs. All methods
are synchronous; async callers use :class:`AsyncStore`, which runs them in worker threads.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import logging
import re
import threading
from collections.abc import AsyncIterator, Collection, Iterable
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

from .domain.enums import Phase
from .domain.models import Event
from .security.auth import DashboardUser

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

KINDS = (
    "provider",
    "plan",
    "migration",
    "recommendation",
    "advisor_snapshot",
    "advisor_policy",
    "user",
)

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

#: Top-level JSON fields the list filters push into SQL (SDD §11); each gets an expression
#: index on ``(kind, field)`` so a plan's migrations or a phase are found without a full scan.
INDEXED_FIELDS = ("plan_id", "phase", "wave_id", "status", "role")
#: ids per IN (...) of ``get_many``: far below SQLite's and PostgreSQL's bound-parameter limits
_GET_MANY_CHUNK = 500
for _field in INDEXED_FIELDS:
    sa.Index(f"ix_documents_{_field}", documents.c.kind, documents.c.data[_field].as_string())

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
    # the kinds filter (GET /events?audit=true, SDD §11): audit events are sparse, and a scan
    # of the primary key backwards reads the whole table to find the newest of them
    sa.Index("ix_events_kind_seq", "kind", "seq"),
    sqlite_autoincrement=True,
)


class StoreError(Exception):
    """Base class for store errors."""


class ConflictError(StoreError):
    """Optimistic-concurrency conflict (stale version, or the document already exists)."""


class UnstorableValue(StoreError):
    """A write holding what PostgreSQL cannot store - a NUL, a lone surrogate -, refused before
    any statement on every backend rather than rewritten (SDD §11)."""


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


def _as_strings(wanted: Any) -> list[str] | None:
    """``wanted`` as a list of strings when every member is a string (or string enum)."""
    members = (
        list(wanted)
        if isinstance(wanted, Collection) and not isinstance(wanted, str | bytes)
        else [wanted]
    )
    out: list[str] = []
    for member in members:
        if isinstance(member, Enum):
            member = member.value
        if not isinstance(member, str):
            return None
        out.append(member)
    return out


def _sql_filter(name: str, wanted: Any) -> Any:
    """A WHERE clause on the JSON field ``name`` for string filters, else ``None``; a value holding
    a NUL or a lone surrogate matches no stored string and is never sent (SDD §11)."""
    values = _as_strings(wanted)
    if not values or not name.isidentifier():
        return None
    values = [v for v in values if not _unsendable(v)]
    if not values:
        return sa.false()
    column = documents.c.data[name].as_string()
    return column == values[0] if len(values) == 1 else column.in_(values)


def _matches(value: Any, wanted: Any) -> bool:
    if isinstance(wanted, Enum):
        wanted = wanted.value
    if isinstance(wanted, Collection) and not isinstance(wanted, str | bytes):
        return any(_matches(value, w) for w in wanted)
    return value == wanted


#: the end of ``events.seq``'s range, PostgreSQL's integer: no bound beyond it is sent (SDD §11)
MAX_SEQ = 2**31 - 1

#: what PostgreSQL cannot store in a string: a NUL, a lone surrogate (SDD §11)
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def _unstorable_text(text: str) -> str | None:
    """What of ``text`` PostgreSQL cannot store, in words, else None (SDD §11)."""
    found = _UNSTORABLE.search(text)
    if found is None:
        return None
    return "a NUL character" if found.group() == "\x00" else "a lone surrogate"


def _unsendable(value: str | None) -> bool:
    """Whether a lookup value holds what no stored string does (SDD §11)."""
    return value is not None and _unstorable_text(value) is not None


def _find_unstorable(value: Any) -> tuple[list[str], str] | None:
    """The path, innermost part first, and what - of the first string of ``value`` PostgreSQL
    cannot store; its parts are only built on the way back from one. A model is read as it holds
    its fields: its JSON dump writes a lone surrogate of a map's key as U+FFFD, a rewrite the store
    would never see."""
    if isinstance(value, str):
        what = _unstorable_text(value)
        return ([], f"holds {what}") if what else None
    if isinstance(value, BaseModel):
        value = value.__dict__
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and (what := _unstorable_text(key)):
                return [], f"a key holds {what}"
            if (found := _find_unstorable(item)) is not None:
                found[0].append(str(key))
                return found
    elif isinstance(value, list | tuple | set | frozenset):
        for index, item in enumerate(value):
            if (found := _find_unstorable(item)) is not None:
                found[0].append(str(index))
                return found
    return None


def unstorable(value: Any) -> tuple[str, str] | None:
    """Where ``value`` first holds a NUL or a lone surrogate, and what: ``("vm.nics.0.network",
    "holds a NUL character")``, ``("vm.tags", "a key holds a lone surrogate")``, else None - what
    the store refuses, and the API before any route runs (SDD §11, §12)."""
    found = _find_unstorable(value)
    return None if found is None else (".".join(reversed(found[0])), found[1])


def _refuse_unstorable(subject: str, value: Any) -> None:
    """An ``UnstorableValue`` naming ``subject`` and the field, never the value, when ``value``
    holds what PostgreSQL cannot store - on every backend, before any statement (SDD §11)."""
    found = unstorable(value)
    if found is not None:
        where, what = found
        field = f"{where}: " if where else ""
        raise UnstorableValue(f"{subject}: {field}{what}, which the store cannot keep")


def _document_data(kind: str, doc_id: str, model: BaseModel) -> dict[str, Any]:
    """``model``'s JSON data, refused when it or its id holds what PostgreSQL cannot store - never
    rewritten, so a stored value is the one a later step compares (SDD §11, reviews RV48-04,
    RV48-05, RV48-08)."""
    what = _unstorable_text(doc_id)
    if what is not None:
        raise UnstorableValue(f"{kind}: id: holds {what}, which the store cannot keep")
    _refuse_unstorable(f"{kind} {doc_id}", model)
    return model.model_dump(mode="json")


def _kept_actor(actor: str) -> str:
    """``actor`` as ``events.actor`` keeps it: whole when it fits, else its first characters,
    ``~`` and 12 hex digits of its SHA-256 - two actors never read as one (SDD §11, reviews ST-02,
    RV48-10)."""
    width = events.c.actor.type.length
    if len(actor) <= width:
        return actor
    digest = hashlib.sha256(actor.encode("utf-8")).hexdigest()[:12]
    return f"{actor[: width - len(digest) - 1]}~{digest}"


def _event_row(event: Event) -> dict[str, Any]:
    """The ``events`` row of ``event``, refused when it holds what PostgreSQL cannot store, its
    actor as the column keeps it; the event a write returns, and the bus publishes, is this row
    (SDD §11, reviews ST-02, RV48-09, RV48-10, RV48-14)."""
    named = _unstorable_text(event.kind) is None
    _refuse_unstorable(f"{event.kind} event" if named else "an event", event)
    return {
        "ts": event.ts,
        "kind": event.kind,
        "plan_id": event.plan_id,
        "migration_id": event.migration_id,
        "actor": _kept_actor(event.actor),
        "message": event.message,
        "data": event.model_dump(mode="json")["data"],
    }


def _index_names(conn: sa.Connection) -> set[str]:
    """The index names the store's tables have: on PostgreSQL those of the tables their names
    resolve to through search_path, as every statement of the store does - not the current
    schema's, which may be the role's own and hold no table (SDD §11); every one on SQLite."""
    if conn.dialect.name == "postgresql":
        query = sa.text(
            "SELECT i.relname FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid"
            " WHERE x.indrelid IN (to_regclass(:documents), to_regclass(:events))"
        )
        rows = conn.execute(query, {"documents": documents.name, "events": events.name})
    else:
        rows = conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type = 'index'"))
    return {row[0] for row in rows}


class Store:
    """Synchronous store; see the module docstring."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = build_engine(url)

    # -- lifecycle ---------------------------------------------------------------------------
    def create_schema(self) -> None:
        metadata.create_all(self.engine, checkfirst=True)
        # create_all skips the indexes of tables that already exist, and expression indexes
        # cannot be reflected: add the missing ones by name. A complete schema takes no DDL -
        # PostgreSQL lets only a table's owner run CREATE INDEX, even IF NOT EXISTS, and the
        # application role need not own the tables (SDD §11, Security.md §9.4)
        with self.engine.begin() as conn:
            existing = _index_names(conn)
            for index in (*documents.indexes, *events.indexes):
                if index.name not in existing:
                    conn.execute(sa.schema.CreateIndex(index, if_not_exists=True))

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
    def create_user(self, user: DashboardUser, actor: str) -> Event:
        """Create the identity and its audit event together; a duplicate never rotates a token."""
        event = Event(
            kind="user.created",
            actor=actor,
            message=f"User {user.name} created",
            data={"name": user.name, "role": str(user.role)},
        )
        data = _document_data("user", user.id, user)
        row = _event_row(event)
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    documents.insert().values(
                        kind="user",
                        id=user.id,
                        version=1,
                        data=data,
                        created_at=event.ts,
                        updated_at=event.ts,
                    )
                )
                result = conn.execute(events.insert().values(**row))
                seq = int(result.inserted_primary_key[0])
        except IntegrityError:
            raise ConflictError("a user with this name already exists") from None
        return Event(seq=seq, **row)

    def put(self, kind: str, model: BaseModel, expected_version: int | None = None) -> int:
        """Insert or update ``model``; return the new version.

        ``expected_version=None`` writes unconditionally, ``0`` requires the document to be new,
        ``n > 0`` requires the stored version to be ``n`` (else :class:`ConflictError`).
        """
        doc_id = str(model.id)  # type: ignore[attr-defined]
        data = _document_data(kind, doc_id, model)
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
        # a versioned write is one statement; only a refused one reads what it found (SDD §11)
        if expected_version == 0:
            try:
                with self.engine.begin() as conn:
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
            except IntegrityError:
                raise self._refused(kind, doc_id, expected_version) from None
            return 1
        if expected_version is not None:
            with self.engine.begin() as conn:
                result = conn.execute(
                    documents.update()
                    .where(key & (documents.c.version == expected_version))
                    .values(version=expected_version + 1, data=data, updated_at=now)
                )
            if result.rowcount != 1:
                raise self._refused(kind, doc_id, expected_version)
            return expected_version + 1
        # unconditional: the next version of whatever is stored (put() retries a race)
        try:
            with self.engine.begin() as conn:
                row = conn.execute(sa.select(documents.c.version).where(key)).first()
                if row is None:
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

    def put_with_event(
        self, kind: str, model: BaseModel, event: Event, expected_version: int
    ) -> tuple[int, Event]:
        """Update ``model`` at ``expected_version`` and append ``event`` in one transaction (SDD
        §11): both are written, or neither - a ``ConflictError`` as ``put``'s, or the store's own
        error. The source watch's flag and its alert (§8.1, review RV21-04)."""
        doc_id = str(model.id)  # type: ignore[attr-defined]
        data = _document_data(kind, doc_id, model)
        row = _event_row(event)
        key = (documents.c.kind == kind) & (documents.c.id == doc_id)
        with self.engine.begin() as conn:
            updated = conn.execute(
                documents.update()
                .where(key & (documents.c.version == expected_version))
                .values(version=expected_version + 1, data=data, updated_at=_now())
            )
            seq = (
                int(conn.execute(events.insert().values(**row)).inserted_primary_key[0])
                if updated.rowcount == 1
                else None
            )
        if seq is None:
            raise self._refused(kind, doc_id, expected_version)
        return expected_version + 1, Event(seq=seq, **row)

    def _refused(self, kind: str, doc_id: str, expected_version: int) -> ConflictError:
        """Why a versioned write found no row to write: the document is missing, or at another
        version (SDD §11)."""
        key = (documents.c.kind == kind) & (documents.c.id == doc_id)
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(documents.c.version).where(key)).first()
        if row is None:
            return ConflictError(f"{kind} {doc_id!r} does not exist")
        return ConflictError(
            f"{kind} {doc_id!r} is at version {int(row.version)}, expected {expected_version}"
        )

    def get_versioned(self, kind: str, id_: str, model_cls: type[M]) -> tuple[M, int]:
        if _unsendable(id_):
            raise NotFound(kind, id_)  # no stored id holds one (SDD §11)
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

    def change_stamp(self, kind: str) -> tuple[int, int]:
        """``(count, sum of versions)`` of the documents of ``kind``: changes whenever one is
        inserted, updated or deleted, at the cost of one index scan instead of loading and
        validating every document (``/stats`` and ``/metrics`` cache on it)."""
        query = sa.select(
            sa.func.count(documents.c.id), sa.func.coalesce(sa.func.sum(documents.c.version), 0)
        ).where(documents.c.kind == kind)
        with self.engine.connect() as conn:
            count, versions = conn.execute(query).one()
        return int(count), int(versions)

    def list(
        self,
        kind: str,
        model_cls: type[M],
        *,
        limit: int | None = None,
        offset: int = 0,
        **filters: Any,
    ) -> list[M]:
        """Documents of ``kind`` in creation order; ``filters`` compare top-level JSON fields.

        String filters are evaluated in SQL (see ``INDEXED_FIELDS``), the others in Python;
        a filter value that is a list/tuple/set matches any of its members. ``limit`` and
        ``offset`` page the result; they are applied in SQL when every filter could be pushed
        down, otherwise after the Python filtering.
        """
        query = (
            sa.select(documents.c.data)
            .where(documents.c.kind == kind)
            .order_by(documents.c.created_at, documents.c.id)
        )
        all_pushed = True
        for name, wanted in filters.items():
            clause = _sql_filter(name, wanted)
            if clause is None:
                all_pushed = False
            else:
                query = query.where(clause)
        page_in_sql = all_pushed and (limit is not None or offset)
        if page_in_sql:
            query = query.offset(max(0, int(offset)))
            if limit is not None:
                query = query.limit(max(0, int(limit)))
        with self.engine.connect() as conn:
            rows = conn.execute(query).all()
        out: list[M] = []
        for row in rows:
            data = row.data
            # the Python check stays authoritative (non-string values, dialect quirks)
            if all(_matches(data.get(name), wanted) for name, wanted in filters.items()):
                out.append(model_cls.model_validate(data))
        if not page_in_sql and (limit is not None or offset):
            start = max(0, int(offset))
            out = out[start:] if limit is None else out[start : start + max(0, int(limit))]
        return out

    def versions(self, kind: str) -> list[tuple[str, int]]:
        """``(id, version)`` of every document of ``kind`` in creation order (SDD §11): one index
        read, no document loaded — a cached list reloads only what changed."""
        query = (
            sa.select(documents.c.id, documents.c.version)
            .where(documents.c.kind == kind)
            .order_by(documents.c.created_at, documents.c.id)
        )
        with self.engine.connect() as conn:
            return [(row.id, int(row.version)) for row in conn.execute(query)]

    def get_many(
        self, kind: str, ids: Collection[str], model_cls: type[M]
    ) -> dict[str, tuple[int, M]]:
        """``{id: (version, document)}`` for the ``ids`` that exist (SDD §11)."""
        wanted = [id_ for id_ in dict.fromkeys(ids) if not _unsendable(id_)]
        out: dict[str, tuple[int, M]] = {}
        with self.engine.connect() as conn:
            for start in range(0, len(wanted), _GET_MANY_CHUNK):
                chunk = wanted[start : start + _GET_MANY_CHUNK]
                query = sa.select(documents.c.id, documents.c.version, documents.c.data).where(
                    documents.c.kind == kind, documents.c.id.in_(chunk)
                )
                for row in conn.execute(query):
                    out[row.id] = (int(row.version), model_cls.model_validate(row.data))
        return out

    def plan_phases(self, plan_ids: Collection[str]) -> dict[str, dict[Phase, int]]:
        """Each plan's migration phases with how many of its migrations are in each (SDD §8.1,
        §11): one ``SELECT ... GROUP BY`` over the indexed ``plan_id`` and ``phase`` expressions; no
        document is loaded or validated. A plan without migrations is absent from the result."""
        wanted = [plan_id for plan_id in plan_ids if not _unsendable(plan_id)]
        if not wanted:
            return {}
        plan_col = documents.c.data["plan_id"].as_string()
        phase_col = documents.c.data["phase"].as_string()
        query = (
            sa.select(plan_col, phase_col, sa.func.count())
            .where(documents.c.kind == "migration", plan_col.in_(wanted))
            .group_by(plan_col, phase_col)
        )
        out: dict[str, dict[Phase, int]] = {}
        with self.engine.connect() as conn:
            for plan_id, phase, count in conn.execute(query):
                out.setdefault(plan_id, {})[Phase(phase)] = int(count)
        return out

    def delete(self, kind: str, id_: str, expected_version: int | None = None) -> None:
        """Delete the document; with ``expected_version`` only at that version - a write landing
        after the caller read it is a ``ConflictError``, never deleted unseen (SDD §11, review
        ST-05). A document gone is ``NotFound``."""
        if _unsendable(id_):
            raise NotFound(kind, id_)  # no stored id holds one (SDD §11)
        key = (documents.c.kind == kind) & (documents.c.id == id_)
        if expected_version is not None:
            key = key & (documents.c.version == expected_version)
        with self.engine.begin() as conn:
            result = conn.execute(documents.delete().where(key))
        if result.rowcount == 0 and expected_version is not None:
            with self.engine.connect() as conn:
                row = conn.execute(
                    sa.select(documents.c.version).where(
                        (documents.c.kind == kind) & (documents.c.id == id_)
                    )
                ).first()
            if row is not None:
                raise ConflictError(
                    f"{kind} {id_!r} is at version {int(row.version)}, expected {expected_version}"
                )
        if result.rowcount == 0:
            raise NotFound(kind, id_)

    # -- events ------------------------------------------------------------------------------
    def append_event(self, event: Event) -> Event:
        """Append ``event``; return it as written, its seq set (SDD §11)."""
        row = _event_row(event)
        with self.engine.begin() as conn:
            result = conn.execute(events.insert().values(**row))
            seq = int(result.inserted_primary_key[0])
        return Event(seq=seq, **row)

    def events(
        self,
        since_seq: int = 0,
        plan_id: str | None = None,
        migration_id: str | None = None,
        limit: int = 500,
        kinds: Iterable[str] | None = None,
        tail: bool = False,
        before_seq: int | None = None,
    ) -> list[Event]:
        """Matching events after ``since_seq`` (and below ``before_seq``) in ascending ``seq``: the
        first ``limit`` of them, or with ``tail`` the newest ``limit`` — with ``before_seq``, the
        page before an event (SDD §12). A filter value holding a NUL or a lone surrogate matches no
        stored event, and no seq outside ``events.seq``'s range is sent: nothing after its end or
        before 1 (SDD §11, review RV48-12)."""
        if _unsendable(plan_id) or _unsendable(migration_id):
            return []
        if since_seq >= MAX_SEQ or (before_seq is not None and before_seq <= 1):
            return []
        if before_seq is not None and before_seq > MAX_SEQ:
            before_seq = None
        if kinds is not None:
            kinds = [kind for kind in kinds if not _unsendable(kind)]
            if not kinds:
                return []
        query = sa.select(events).where(events.c.seq > max(since_seq, 0))
        if before_seq is not None:
            query = query.where(events.c.seq < before_seq)
        if plan_id is not None:
            query = query.where(events.c.plan_id == plan_id)
        if migration_id is not None:
            query = query.where(events.c.migration_id == migration_id)
        if kinds is not None:
            query = query.where(events.c.kind.in_(list(kinds)))
        order = events.c.seq.desc() if tail else events.c.seq
        query = query.order_by(order).limit(max(0, int(limit)))
        with self.engine.connect() as conn:
            rows = conn.execute(query).mappings().all()
        if tail:
            rows = rows[::-1]
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

    def delete_events_before(self, before: datetime) -> int:
        """Delete events with ``ts < before`` (audit retention, Security.md §12); returns the count.

        Export them first (``seamless events export``): the sequence numbers are never reused,
        so SSE clients resuming with ``since`` keep working after a prune.
        """
        if before.tzinfo is None:
            before = before.replace(tzinfo=UTC)
        before = before.astimezone(UTC)  # SQLite stores UTC wall time without an offset
        with self.engine.begin() as conn:
            result = conn.execute(events.delete().where(events.c.ts < before))
        return int(result.rowcount or 0)

    def cross_process_lock(self, name: str) -> CrossProcessLock:
        """The database-wide mutex ``name`` (SDD §5.4); see :class:`CrossProcessLock`."""
        return CrossProcessLock(self, name)

    def max_seq(self) -> int:
        with self.engine.connect() as conn:
            value = conn.execute(sa.select(sa.func.max(events.c.seq))).scalar()
        return int(value or 0)


class CrossProcessLock:
    """One named mutex shared by every process on the database (SDD §5.4).

    PostgreSQL: a session advisory lock on a connection of its own; SQLite: an exclusive
    ``flock`` on ``{database}.{name}.lock``. Either is dropped when its process ends, so a crash
    never strands it; an in-memory database is reachable from one process only and needs none.
    ``try_acquire`` never blocks; the calls of one instance are serialized, so a release waits
    for an attempt still running in another thread.
    """

    def __init__(self, store: Store, name: str) -> None:
        self._store = store
        self._name = name
        self._guard = threading.Lock()
        self._held: Any = None  # the open file or connection while held
        self._closed = False  # a closed instance acquires nothing more

    def _key(self) -> int:
        digest = hashlib.sha256(f"seamless-migrate:{self._name}".encode()).digest()
        return int.from_bytes(digest[:8], "big", signed=True)

    def try_acquire(self) -> bool:
        with self._guard:
            if self._held is not None:
                return True
            if self._closed:
                # an attempt that ran on after its waiter was cancelled and closed it
                return False
            url = make_url(self._store.url)
            if url.get_backend_name() == "sqlite":
                database = url.database or ""
                if database in ("", ":memory:") or database.startswith("file::memory:"):
                    self._held = True
                    return True
                path = Path(database).expanduser()
                handle = open(f"{path}.{self._name}.lock", "a+b")  # noqa: SIM115 - held open
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    return False
                except BaseException:
                    handle.close()
                    raise
                self._held = handle
                return True
            conn = self._store.engine.connect()
            try:
                got = conn.exec_driver_sql(
                    "SELECT pg_try_advisory_lock(%(key)s)", {"key": self._key()}
                ).scalar()
                conn.commit()
            except BaseException:
                conn.invalidate()
                conn.close()
                raise
            if not got:
                conn.close()
                return False
            self._held = conn
            return True

    def close(self) -> None:
        """Release, and refuse every later attempt - one still running in another thread too."""
        with self._guard:
            self._closed = True
        self.release()

    def release(self) -> None:
        with self._guard:
            held, self._held = self._held, None
            if held is None or held is True:
                return
            if hasattr(held, "fileno"):
                held.close()  # closing the file drops the flock
                return
            try:
                held.exec_driver_sql("SELECT pg_advisory_unlock(%(key)s)", {"key": self._key()})
                held.commit()
            except Exception:  # noqa: BLE001 - a connection that cannot unlock is never pooled
                log.warning("cannot release lock %s; dropping its connection", self._name)
                held.invalidate()
            finally:
                held.close()


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

    async def change_stamp(self, kind: str) -> tuple[int, int]:
        return await asyncio.to_thread(self.sync.change_stamp, kind)

    async def list(
        self,
        kind: str,
        model_cls: type[M],
        *,
        limit: int | None = None,
        offset: int = 0,
        **filters: Any,
    ) -> list[M]:
        return await asyncio.to_thread(
            lambda: self.sync.list(kind, model_cls, limit=limit, offset=offset, **filters)
        )

    async def versions(self, kind: str) -> list[tuple[str, int]]:
        return await asyncio.to_thread(self.sync.versions, kind)

    async def get_many(
        self, kind: str, ids: Collection[str], model_cls: type[M]
    ) -> dict[str, tuple[int, M]]:
        return await asyncio.to_thread(self.sync.get_many, kind, ids, model_cls)

    async def plan_phases(self, plan_ids: Collection[str]) -> dict[str, dict[Phase, int]]:
        return await asyncio.to_thread(self.sync.plan_phases, plan_ids)

    async def delete(self, kind: str, id_: str, expected_version: int | None = None) -> None:
        await asyncio.to_thread(self.sync.delete, kind, id_, expected_version)

    async def append_event(self, event: Event) -> Event:
        return await asyncio.to_thread(self.sync.append_event, event)

    async def events(self, **kwargs: Any) -> list[Event]:
        return await asyncio.to_thread(lambda: self.sync.events(**kwargs))

    async def delete_events_before(self, before: datetime) -> int:
        return await asyncio.to_thread(self.sync.delete_events_before, before)

    @contextlib.asynccontextmanager
    async def cross_process_lock(self, name: str, poll_s: float = 0.1) -> AsyncIterator[None]:
        """Hold the database-wide mutex ``name``, polling while another process holds it; a
        waiter cancelled meanwhile holds nothing after (SDD §5.4)."""
        lock = self.sync.cross_process_lock(name)
        try:
            # another process holds it: nothing in this one can signal its release, so poll
            while True:
                if await asyncio.to_thread(lock.try_acquire):
                    break
                await asyncio.sleep(poll_s)
            yield
        finally:
            # also after a cancelled attempt: release waits for it and drops what it got
            await asyncio.shield(asyncio.to_thread(lock.close))

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
    "UnstorableValue",
    "build_engine",
]


class VersionedDocuments:
    """Every document of one kind in creation order, kept between reads (SDD §11): while the
    store's change stamp holds, the kept list (one aggregate query); when it moved, only the
    documents whose version changed or that are new are loaded, and deleted ones dropped. The
    documents are shared between callers: read them, never change them."""

    def __init__(self, db: AsyncStore, kind: str, model_cls: type[Any]) -> None:
        self.db, self.kind, self.model_cls = db, kind, model_cls
        self.stamp: tuple[int, int] | None = None
        self.rows: list[Any] = []
        self.by_id: dict[str, tuple[int, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self) -> list[Any]:
        stamp = await self.db.change_stamp(self.kind)
        if stamp == self.stamp:
            return self.rows
        async with self._lock:
            if stamp == self.stamp:
                return self.rows
            versions = await self.db.versions(self.kind)
            known = self.by_id
            stale = {id_ for id_, version in versions if known.get(id_, (None,))[0] != version}
            fresh = await self.db.get_many(self.kind, stale, self.model_cls) if stale else {}
            by_id: dict[str, tuple[int, Any]] = {}
            for id_, _ in versions:
                # a stale document deleted between the two reads is left out
                hit = fresh.get(id_) if id_ in stale else known.get(id_)
                if hit is not None:
                    by_id[id_] = hit
            self.rows = [by_id[id_][1] for id_, _ in versions if id_ in by_id]
            self.by_id = by_id
            self.stamp = stamp
            return self.rows
