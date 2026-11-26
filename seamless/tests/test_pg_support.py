"""Each pytest-xdist worker gets its own PostgreSQL database (tests/pg_support.py).

The store fixture truncates the database for every test: two workers sharing one would empty
each other's tables mid-test. CI runs the suite with ``-n auto``.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from tests.conftest import PG_URL
from tests.pg_support import ensure_database, worker_database_url

BASE = "postgresql+psycopg://seamless:ci-only@127.0.0.1:5432/seamless_test"


def test_a_worker_gets_a_database_of_its_own():
    assert worker_database_url(BASE, "gw3") == (
        "postgresql+psycopg://seamless:ci-only@127.0.0.1:5432/seamless_test_gw3"
    )


def test_without_a_worker_the_url_is_unchanged():
    assert worker_database_url(BASE, None) == BASE
    assert worker_database_url(BASE, "") == BASE


def test_the_query_and_the_credentials_survive():
    url = "postgresql+psycopg://u:p%40ss@db:5433/seamless_test?sslmode=require"
    assert worker_database_url(url, "gw0") == (
        "postgresql+psycopg://u:p%40ss@db:5433/seamless_test_gw0?sslmode=require"
    )


@pytest.mark.skipif(not PG_URL, reason="SEAMLESS_TEST_PG_URL is not set")
def test_ensure_database_creates_it_once():
    url = worker_database_url(PG_URL, "ensure_probe")
    ensure_database(url)
    ensure_database(url)  # a second call finds it
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(sa.text("select current_database()"))
                .scalar_one()
                .endswith("_ensure_probe")
            )
    finally:
        engine.dispose()
