"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

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
