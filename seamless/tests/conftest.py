"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest
import sqlalchemy as sa

from seamless_migrate.store import Store

PG_URL = os.environ.get("SEAMLESS_TEST_PG_URL")

STORE_BACKENDS = [
    "sqlite",
    pytest.param(
        "postgres",
        marks=pytest.mark.skipif(not PG_URL, reason="SEAMLESS_TEST_PG_URL is not set"),
    ),
]


def _make_store(backend: str, tmp_path) -> Store:
    if backend == "postgres":
        assert PG_URL
        store = Store(PG_URL)
        store.create_schema()
        store.truncate_for_tests()
        return store
    store = Store(f"sqlite:///{tmp_path / 'seamless.db'}")
    store.create_schema()
    return store


@pytest.fixture(params=STORE_BACKENDS)
def any_store(request, tmp_path) -> Iterator[Store]:
    """A fresh store on SQLite and (when configured) PostgreSQL."""
    store = _make_store(request.param, tmp_path)
    yield store
    store.dispose()


@pytest.fixture
def store(tmp_path) -> Iterator[Store]:
    """A fresh SQLite store (used by everything that is not a store test)."""
    store = _make_store("sqlite", tmp_path)
    yield store
    store.dispose()


@pytest.fixture
def refuse_events_insert() -> Iterator[Callable[[Store, str], list[str]]]:
    """``refuse(store, kind)``: the store's events table refuses the next row of ``kind`` when its
    INSERT runs - inside its transaction, after whatever ran there before it, as a store fails (a
    lost connection, a lock timeout) - never when the statement is built, so a write split over
    two transactions leaves its first half behind (review RV23-08). It returns the kinds refused."""
    listening = []

    def refuse(store: Store, kind: str) -> list[str]:
        refused: list[str] = []

        def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            if refused or not statement.lstrip().upper().startswith("INSERT INTO EVENTS"):
                return
            if context.compiled_parameters[0].get("kind") == kind:
                refused.append(kind)
                raise RuntimeError("the store was briefly unavailable")

        sa.event.listen(store.engine, "before_cursor_execute", before_cursor_execute)
        listening.append((store.engine, before_cursor_execute))
        return refused

    yield refuse
    for engine, listener in listening:
        sa.event.remove(engine, "before_cursor_execute", listener)
