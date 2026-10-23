"""Every Python requirement of the repository comes from a package index, never from a VCS URL or
a direct reference (Security.md SEC-09, C9-01): a tag can be moved after the review, and pip checks
no hash for one."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()

#: what pip reads as a VCS URL (``git+``, ``hg+``, ``svn+``, ``bzr+``) or a direct reference
#: (``name @ url``, a bare archive URL); an index option such as ``--index-url`` is none of them
_DIRECT = re.compile(r"\b(?:git|hg|svn|bzr)\+|@\s*\S+://|^(?:https?|file)://", re.I)


def _requirement_lines(path: Path) -> list[str]:
    """A requirements file's requirements, without its comments and blank lines."""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = "" if raw.lstrip().startswith("#") else raw.split(" #", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _pyproject_requirements(path: Path) -> list[str]:
    """A pyproject's dependencies and the dependencies of each of its extras."""
    project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
    requirements = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    return requirements


def _direct_references(requirements: list[str]) -> list[str]:
    return [requirement for requirement in requirements if _DIRECT.search(requirement)]


def test_no_python_requirement_installs_from_a_vcs_url_or_a_direct_reference():
    files = [*sorted(ROOT.glob("requirements*.txt")), *sorted(ROOT.glob("aee/requirements*.txt"))]
    assert {"requirements.txt", "aee/requirements.txt"} <= {str(f.relative_to(ROOT)) for f in files}
    found = {str(f.relative_to(ROOT)): _direct_references(_requirement_lines(f)) for f in files}
    pyproject = ROOT / "seamless" / "pyproject.toml"
    found["seamless/pyproject.toml"] = _direct_references(_pyproject_requirements(pyproject))

    assert {name: lines for name, lines in found.items() if lines} == {}


def test_the_check_names_each_vcs_url_and_direct_reference():
    requirements = [
        "openstacksdk @ git+https://example.org/openstacksdk.git@4.5.1",
        "git+https://example.org/tool.git#egg=tool",
        "-e hg+https://example.org/lib#egg=lib",
        "archive @ https://example.org/archive-1.0.tar.gz",
        "https://example.org/wheel-1.0-py3-none-any.whl",
        "openstacksdk>=4.21.0",
        'tomli>=2; python_version < "3.11"',
        "--index-url https://example.org/simple",
    ]

    assert _direct_references(requirements) == requirements[:5]


def test_a_comment_that_names_a_url_is_no_requirement(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "# the fork was openstacksdk @ git+https://example.org/openstacksdk.git@4.5.1\n"
        "\n"
        "openstacksdk>=4.21.0  # see https://example.org/bug\n",
        encoding="utf-8",
    )

    assert _requirement_lines(requirements) == ["openstacksdk>=4.21.0"]
