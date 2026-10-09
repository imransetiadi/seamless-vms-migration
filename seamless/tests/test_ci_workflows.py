"""The CI workflows run GitHub Actions pinned by commit SHA (Security.md §9.2).

A tag (``@v4``) can be moved, or force-pushed by whoever compromises the action's repository, to
make CI run other code with the workflow's token; a full commit SHA cannot. The version stays in a
comment (``# v4.4.0``) so a reader and Dependabot's ``github-actions`` updates know the release.
"""

import re

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)(.*)$")
PINNED = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")
VERSION_COMMENT = re.compile(r"#\s*v\d+\.\d+\.\d+\b")


def _uses() -> list[tuple[str, int, str, str]]:
    found = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        for number, line in enumerate(workflow.read_text().splitlines(), 1):
            match = USES.match(line)
            if match and not match.group(1).startswith("./"):
                found.append((workflow.name, number, match.group(1), match.group(2)))
    return found


def test_every_action_is_pinned_to_a_commit_sha_with_its_version():
    uses = _uses()
    assert uses, "no 'uses:' found in .github/workflows"
    assert [f"{f}:{n} {ref}" for f, n, ref, _ in uses if not PINNED.fullmatch(ref)] == []
    assert [f"{f}:{n} {ref}" for f, n, ref, rest in uses if not VERSION_COMMENT.search(rest)] == []
