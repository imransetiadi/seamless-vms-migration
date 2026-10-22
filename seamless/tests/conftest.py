"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from collections.abc import AsyncIterator, Callable, Iterator

import pytest
import sqlalchemy as sa

from seamless_migrate.store import Store
from tests.leftover_tasks import cancel_leftovers

PG_URL = os.environ.get("SEAMLESS_TEST_PG_URL")

STORE_BACKENDS = [
    "sqlite",
    pytest.param(
        "postgres",
        marks=pytest.mark.skipif(not PG_URL, reason="SEAMLESS_TEST_PG_URL is not set"),
    ),
]


#: how long the tasks an async test leaves get to end once cancelled before the test fails naming
#: them (tests/leftover_tasks.py, QASuite §14)
LEFTOVER_GRACE_S = 30.0
LEFTOVERS = "_no_task_outlives_its_test"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Each async test gets LEFTOVERS as its first fixture: torn down last, right before its event
    loop closes, it cancels what the test left as the close does - and names a task that does not
    end instead of hanging the run."""
    for item in items:
        test = getattr(item, "obj", None)
        names = getattr(item, "fixturenames", None)
        if inspect.iscoroutinefunction(test) and names is not None and LEFTOVERS not in names:
            names.insert(0, LEFTOVERS)


@pytest.fixture
async def _no_task_outlives_its_test(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    yield
    this = asyncio.current_task()
    report = await cancel_leftovers(
        (task for task in asyncio.all_tasks() if task is not this), LEFTOVER_GRACE_S
    )
    if report:
        report = f"{request.node.nodeid}: {report}"
        # now, into the job's log: the loop's close still waits for those tasks afterwards
        print(report, file=sys.stderr, flush=True)
        pytest.fail(report, pytrace=False)


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
def refuse_write() -> Iterator[Callable[[Store, str, str], list[str]]]:
    """``refuse(store, write, kind)``: the store refuses the next ``write`` - "INSERT INTO events"
    or "UPDATE documents" - of a row of ``kind`` (an event's kind, a document's) when it RUNS:
    inside its transaction, after whatever ran there before it, as a store fails (a lost
    connection, a lock timeout), never when the statement is built. Refusing each half in turn
    fails a write split over two transactions, whichever half it writes first (reviews RV23-08,
    RV24-03). It returns what it refused."""
    listening = []

    def refuse(store: Store, write: str, kind: str) -> list[str]:
        refused: list[str] = []

        def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            if refused or not statement.lstrip().upper().startswith(write.upper()):
                return
            # the event's kind is its `kind` value, the document's its key's (`kind_1`)
            if kind in context.compiled_parameters[0].values():
                refused.append(f"{write} {kind}")
                raise RuntimeError("the store was briefly unavailable")

        sa.event.listen(store.engine, "before_cursor_execute", before_cursor_execute)
        listening.append((store.engine, before_cursor_execute))
        return refused

    yield refuse
    for engine, listener in listening:
        sa.event.remove(engine, "before_cursor_execute", listener)


@pytest.fixture
def land_first() -> Iterator[Callable[[Store, str, str, Callable[[], object]], list[object]]]:
    """``land(store, write, kind, other)``: just before the next ``write`` ("UPDATE documents",
    "INSERT INTO events") of a row of ``kind`` runs, another writer's ``other()`` commits on a
    connection of its own - a concurrent change landing between a caller's read and its write, as
    the CLI beside the server or a second replica makes one (review RV25-04). It returns what
    ``other`` returned."""
    listening = []

    def land(store: Store, write: str, kind: str, other: Callable[[], object]) -> list[object]:
        started: list[str] = []
        landed: list[object] = []

        def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            if started or not statement.lstrip().upper().startswith(write.upper()):
                return
            if kind in context.compiled_parameters[0].values():
                # once: the other writer's own statements run through this listener too
                started.append(statement)
                landed.append(other())

        sa.event.listen(store.engine, "before_cursor_execute", before_cursor_execute)
        listening.append((store.engine, before_cursor_execute))
        return landed

    yield land
    for engine, listener in listening:
        sa.event.remove(engine, "before_cursor_execute", listener)
