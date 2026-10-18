"""docs/QASuite.md names only tests that exist (CLAUDE.md §1, §2.3): a renamed or deleted test that
a requirement row still cites would claim coverage nobody runs."""

import re

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()
QASUITE = ROOT / "docs" / "QASuite.md"


def _python_tests() -> tuple[set[str], set[str]]:
    """Every test function of the control plane's and the collection's suites, and their modules."""
    names: set[str] = set()
    modules: set[str] = set()
    for base in ("seamless/tests", "tests"):
        for path in (ROOT / base).rglob("*.py"):
            modules.add(path.stem)
            text = path.read_text(encoding="utf-8")
            names |= set(re.findall(r"^\s*(?:async\s+)?def\s+(test_\w+)", text, re.MULTILINE))
    return names, modules


def _dashboard_tests() -> list[str]:
    """Every dashboard test file (Vitest, Playwright), by its path from the repository root."""
    return [
        path.relative_to(ROOT).as_posix()
        for base in ("dashboard/src", "dashboard/e2e")
        for path in (ROOT / base).rglob("*")
        if re.search(r"\.(test|spec)\.tsx?$", path.name)
    ]


def test_every_test_qasuite_names_exists():
    """CLAUDE.md §1 (docs-drift DD2-48): each backticked test name is a test function or module of
    the suites - a name ending in `_` is a convention's prefix, test_finding_<code> - and each
    dashboard test file it cites exists, named alone or by the end of its path."""
    text = QASUITE.read_text(encoding="utf-8")
    names, modules = _python_tests()
    cited = set(re.findall(r"`(test_\w+)", text))
    assert len(cited) > 800  # the parse found the matrix
    missing = sorted(
        name
        for name in cited
        if name not in names
        and name not in modules
        and not (name.endswith("_") and any(test.startswith(name) for test in names))
    )
    assert missing == []
    files = _dashboard_tests()
    cited_files = set(re.findall(r"`([\w./-]*\.(?:test|spec)\.tsx?)`", text))
    assert len(cited_files) > 50
    absent = sorted(
        cited
        for cited in cited_files
        if not any(path == cited or path.endswith("/" + cited) for path in files)
    )
    assert absent == []


#: the dummy secret files QASuite S-28 adds to a scratch copy for a collection build: named, never
#: present in the repository
S28_DUMMIES = {"tests/auth_tenant.yml", "tests/clouds.yml"}


def test_every_tests_path_qasuite_cites_exists_from_the_repository_root():
    """CLAUDE.md §1 (review RV37B-19): a path QASuite cites under `tests/` is the repository root's
    tests tree - the collection's and the e2e scripts -, so it exists there; the control plane's
    tests are cited as `seamless/tests/...`. S-28's dummy secret files are the exception."""
    text = QASUITE.read_text(encoding="utf-8")
    cited = set(re.findall(r"`(tests/[A-Za-z0-9_.\-/]+\.[a-z]+)`", text))
    assert cited, "the parse found no tests/ path"
    assert sorted(p for p in cited - S28_DUMMIES if not (ROOT / p).exists()) == []
