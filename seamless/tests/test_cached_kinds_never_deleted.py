"""SDD §11: the document kinds the control plane caches by change stamp are never deleted.

``VersionedDocuments`` trusts ``(count, sum of versions)``; a delete, an insert and an update
between two reads could leave it unchanged. This guard fails when code starts deleting one of
those kinds.
"""

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "seamless_migrate"
CACHED = ("migration", "plan", "recommendation")
# a store delete naming a kind: db.delete("plan", …), store.delete('migration', …), .delete(kind=…),
# and one written with its events: delete_and_emit(store, bus, "advisor_policy", …),
# .delete_with_events("plan", …) (review RV51-11)
DELETE = re.compile(
    r"(?:\.delete(?:_with_events)?\(\s*(?:kind\s*=\s*)?|delete_and_emit\([^\"')]*)[\"'](\w+)[\"']"
)


def test_no_code_deletes_a_cached_document_kind():
    deleted = {
        (path.relative_to(SOURCE).as_posix(), kind)
        for path in SOURCE.rglob("*.py")
        for kind in DELETE.findall(path.read_text(encoding="utf-8"))
    }
    assert {kind for _, kind in deleted} >= {"provider", "advisor_policy"}  # the guard sees deletes
    assert [entry for entry in sorted(deleted) if entry[1] in CACHED] == []
