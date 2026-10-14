"""Files of os-migrate 1.0.5 that this project changed say so (NOTICE; Apache License 2.0,
section 4(b)): each carries "Modified by the Seamless Migrate project, 2026." unless NOTICE names
it. fixtures/os-migrate-1.0.5.sha256 holds the sha256 of every upstream file, so the check runs
offline and needs no upstream history in the repository."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()
MANIFEST = Path(__file__).parent / "fixtures" / "os-migrate-1.0.5.sha256"


def _upstream() -> dict[str, str]:
    """Each upstream file's path and its sha256 in os-migrate 1.0.5."""
    files = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            digest, path = line.split("  ", 1)
            files[path] = digest
    return files


def _named_in_notice(notice: str) -> set[str]:
    """The changed files NOTICE names because they cannot carry the comment."""
    return set(re.findall(r"^  (\S+)$", notice, re.M))


def _unmarked_changes(root: Path, upstream: dict[str, str], named: set[str]) -> list[str]:
    """The upstream files under root whose content differs from os-migrate 1.0.5 and that neither
    carry the notice nor are named in NOTICE; a file removed here is not one of them."""
    unmarked = []
    for path, digest in sorted(upstream.items()):
        file = root / path
        if not file.is_file():
            continue
        data = file.read_bytes()
        if hashlib.sha256(data).hexdigest() == digest or path in named:
            continue
        if b"Modified by the Seamless Migrate project, 2026." not in data:
            unmarked.append(path)
    return unmarked


def test_every_upstream_file_this_project_changed_says_so():
    upstream = _upstream()
    assert len(upstream) == 562
    named = _named_in_notice((ROOT / "NOTICE").read_text(encoding="utf-8"))
    assert "README.md" in named

    assert _unmarked_changes(ROOT, upstream, named) == []


def test_a_changed_upstream_file_without_the_notice_is_reported(tmp_path):
    def sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    upstream = {
        "same.yml": sha(b"upstream\n"),
        "changed.yml": sha(b"upstream\n"),
        "marked.yml": sha(b"upstream\n"),
        "README.md": sha(b"upstream\n"),
        "removed.yml": sha(b"upstream\n"),
    }
    (tmp_path / "same.yml").write_bytes(b"upstream\n")
    (tmp_path / "changed.yml").write_bytes(b"upstream, changed\n")
    (tmp_path / "marked.yml").write_bytes(
        b"# Modified by the Seamless Migrate project, 2026.\nupstream, changed\n"
    )
    (tmp_path / "README.md").write_bytes(b"upstream, changed\n")

    assert _unmarked_changes(tmp_path, upstream, {"README.md"}) == ["changed.yml"]
