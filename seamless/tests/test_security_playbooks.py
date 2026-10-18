"""Security.md's incident playbooks hold statements that do what they say, run as written on the
store's own schema (Security.md §10, review RV37B-08)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import sqlalchemy as sa

from seamless_migrate.config import find_repo_root
from seamless_migrate.security.auth import DashboardUser, hash_token
from seamless_migrate.store import Store, documents

SECURITY = find_repo_root() / "docs" / "Security.md"


def _playbook(letter: str) -> str:
    """The paragraph of incident playbook ``letter`` (``**F. ...``) up to the next playbook."""
    text = SECURITY.read_text(encoding="utf-8")
    start = text.index(f"**{letter}. ")
    following = re.search(r"\n\*\*[A-Z]\. ", text[start + 1 :])
    return text[start : start + 1 + following.start()] if following else text[start:]


def _statements(paragraph: str) -> list[str]:
    """The SQL statements the paragraph quotes, in its order."""
    return re.findall(r"`((?:SELECT|DELETE)\b[^`]*;)`", paragraph)


def test_playbook_f_removes_a_user_row_a_database_write_planted(tmp_path):
    """Security.md playbook F (review RV37B-08): a write can plant a `user` row under any key - the
    control plane authenticates any row whose digest matches. The playbook's statements, run as
    written, list every row with its key and remove them all, the planted one included, before
    every dashboard user is issued again."""
    statements = _statements(_playbook("F"))
    listing = [s for s in statements if s.startswith("SELECT")]
    deletion = [s for s in statements if s.startswith("DELETE")]
    assert len(listing) == 1 and len(deletion) == 1, statements
    store = Store(f"sqlite:///{tmp_path / 'seamless.db'}")
    store.create_schema()
    try:
        store.create_user(
            DashboardUser(id="ops", name="Ops", role="admin", sha256=hash_token("smg_api")), "admin"
        )
        now = datetime.now(UTC)
        planted = DashboardUser(id="ops", name="Ops", role="admin", sha256=hash_token("smg_plant"))
        with store.engine.begin() as conn:
            conn.execute(
                documents.insert().values(
                    kind="user",
                    id="zz-planted",
                    version=1,
                    data=planted.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
            )
        with store.engine.begin() as conn:
            listed = sorted(tuple(row) for row in conn.execute(sa.text(listing[0])))
            conn.execute(sa.text(deletion[0]))
        assert [row[0] for row in listed] == ["ops", "zz-planted"]
        assert store.list("user", DashboardUser) == []
    finally:
        store.dispose()
