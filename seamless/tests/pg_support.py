"""One PostgreSQL database per pytest-xdist worker.

The store fixture (tests/conftest.py) truncates its database before every test, so workers that
shared one would empty each other's tables mid-test. Under xdist, ``PYTEST_XDIST_WORKER`` names the
worker (gw0, gw1, ...); its database is the configured one with that suffix, created on first use
by the configured URL's role (CI's is the postgres image's POSTGRES_USER).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import make_url


def worker_database_url(url: str, worker: str | None) -> str:
    """``url`` with the worker's suffix on its database name; unchanged outside xdist."""
    if not worker:
        return url
    parsed = make_url(url)
    return parsed.set(database=f"{parsed.database}_{worker}").render_as_string(hide_password=False)


def ensure_database(url: str) -> None:
    """Create ``url``'s database unless it exists, connected to the server's ``postgres`` one."""
    parsed = make_url(url)
    name = parsed.database
    engine = sa.create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            found = conn.execute(
                sa.text("select 1 from pg_database where datname = :name"), {"name": name}
            ).first()
            if found is None:
                quoted = conn.dialect.identifier_preparer.quote_identifier(name)
                conn.execute(sa.text(f"create database {quoted}"))
    finally:
        engine.dispose()
