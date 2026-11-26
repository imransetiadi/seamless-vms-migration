#!/usr/bin/env python3
"""Report the direct dependencies that have newer releases, as Markdown (for a job summary).

Dependabot was removed (its pull requests kept conflicting with main); this report keeps the
visibility without opening anything. It reads what the repository declares - seamless/pyproject.toml
and requirements.txt for Python, the pinned ``uses:`` of .github/workflows - and what the tools
found behind:

  --pip FILE             ``pip list --outdated --format=json``
  --npm FILE             ``npm outdated --json`` (run in dashboard/)
  --actions-latest FILE  {"owner/repo": "vX.Y.Z"}, each action's latest release
  --images-latest FILE   {"name:tag": "sha256:..."}, each digest-pinned image tag's current digest
  --list-actions         print the pinned actions' owner/repo, one per line, and exit
  --list-images          print the digest-pinned images' name:tag, one per line, and exit

A Python release the declared range refuses (a reviewed cap, such as openstacksdk's, SDD §21.7) is
marked "outside": it needs a deliberate, reviewed bump, not a routine one. Bumps are made by hand
in a reviewed pull request (Security.md §12).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

NOTHING = "Nothing behind."
#: an image pinned by digest with its tag: [registry[:port]/]path:tag@sha256:...
IMAGE = re.compile(r"((?:[\w.-]+(?::\d+)?/)?[\w./-]+:[\w.-]+)@(sha256:[0-9a-f]{64})")
#: where the repository pins container images by digest (Security.md §12)
IMAGE_FILE = re.compile(r"(Containerfile|Dockerfile)[^/]*$|^deploy/.+\.ya?ml$")
SKIP_DIRS = {".git", "node_modules", ".venv", ".worktrees", "dist", "dist-mock"}
USES = re.compile(r"uses:\s*([\w.-]+/[\w.-]+)(?:/[^@\s]*)?@[0-9a-f]{40}\s*#\s*(v?[\w.-]+)")


def declared_python(root: Path) -> dict[str, list[SpecifierSet]]:
    """Each direct Python dependency's declared ranges, by canonical name."""
    lines: list[str] = []
    pyproject = root / "seamless" / "pyproject.toml"
    if pyproject.exists():
        project = tomllib.loads(pyproject.read_text()).get("project", {})
        lines += project.get("dependencies", [])
        for extra in project.get("optional-dependencies", {}).values():
            lines += extra
    requirements = root / "requirements.txt"
    if requirements.exists():
        lines += requirements.read_text().splitlines()
    declared: dict[str, list[SpecifierSet]] = {}
    for line in lines:
        line = line.split(" #", 1)[0].strip()
        # options (-r, -c, -e) and URL requirements (git+https://...) name no range to compare
        if not line or line.startswith(("-", "#")) or "://" in line:
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            continue
        declared.setdefault(canonicalize_name(requirement.name), []).append(requirement.specifier)
    return declared


def python_section(root: Path, outdated: list[dict[str, str]]) -> list[str]:
    declared = declared_python(root)
    rows = []
    for item in sorted(outdated, key=lambda i: canonicalize_name(i["name"])):
        name = canonicalize_name(item["name"])
        if name not in declared:
            continue  # transitive: it moves with what pulls it in
        latest = item["latest_version"]
        refusing = [s for s in declared[name] if not s.contains(latest, prereleases=True)]
        if refusing:
            note = f"**outside** `{refusing[0]}`: a reviewed cap, bump it deliberately"
        else:
            ranges = ", ".join(f"`{s}`" for s in declared[name] if str(s)) or "any"
            note = f"allowed by {ranges}"
        rows.append(f"| {name} | {item['version']} | {latest} | {note} |")
    head = ["## Python (direct dependencies)", ""]
    if not rows:
        return [*head, NOTHING, ""]
    table = ["| Package | Installed | Latest | Declared range |", "|---|---|---|---|"]
    return [*head, *table, *rows, ""]


def _behind(current: str, latest: str) -> bool:
    """Whether ``current`` is older than ``latest``; an unparsable pair counts when they differ."""
    try:
        return Version(current) < Version(latest)
    except InvalidVersion:
        return current != latest


def _change(current: str, latest: str) -> str:
    try:
        a, b = Version(current), Version(latest)
    except InvalidVersion:
        return "?"
    if b.major != a.major:
        return "major"
    if b.minor != a.minor:
        return "minor"
    return "patch"


def npm_section(outdated: dict[str, dict[str, str]]) -> list[str]:
    rows = []
    for name, info in sorted(outdated.items()):
        latest, current = info.get("latest"), info.get("current")
        if not latest:
            continue  # a git or local dependency: no registry release to compare
        if current is None:
            rows.append(f"| {name} | not installed | {latest} | - |")
        elif _behind(current, latest):
            rows.append(f"| {name} | {current} | {latest} | {_change(current, latest)} |")
    head = ["## Dashboard (npm)", ""]
    if not rows:
        return [*head, NOTHING, ""]
    return [*head, "| Package | Current | Latest | Change |", "|---|---|---|---|", *rows, ""]


def pinned_actions(root: Path) -> dict[str, set[str]]:
    """Each pinned action's owner/repo and every release its pins' comments name."""
    pins: dict[str, set[str]] = {}
    workflows = root / ".github" / "workflows"
    for workflow in sorted(workflows.glob("*.y*ml")) if workflows.exists() else []:
        for repo, version in USES.findall(workflow.read_text()):
            pins.setdefault(repo, set()).add(version)
    return pins


def _is_version(tag: str) -> bool:
    """A release tag naming a version (v4.40.0), not another artifact (codeql-bundle-v2.27.6)."""
    try:
        Version(tag)
    except InvalidVersion:
        return False
    return True


def actions_section(root: Path, latest: dict[str, str]) -> list[str]:
    rows = [
        f"| {repo} | {pinned} | {latest[repo]} |"
        for repo, versions in sorted(pinned_actions(root).items())
        if _is_version(latest.get(repo, ""))
        for pinned in sorted(versions)
        if _behind(pinned, latest[repo])
    ]
    head = ["## GitHub Actions (pinned by commit SHA)", ""]
    if not rows:
        return [*head, NOTHING, ""]
    return [*head, "| Action | Pinned | Latest release |", "|---|---|---|", *rows, ""]


def pinned_images(root: Path) -> dict[str, dict[str, list[str]]]:
    """Each digest-pinned image's name:tag, its pinned digests and the files that pin each."""
    pins: dict[str, dict[str, list[str]]] = {}
    files = []
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = sorted(d for d in subdirectories if d not in SKIP_DIRS)  # not entered
        for name in sorted(names):
            path = Path(directory, name)
            if IMAGE_FILE.search(path.relative_to(root).as_posix()):
                files.append(path)
    for path in files:
        relative = path.relative_to(root)
        for image, digest in IMAGE.findall(path.read_text(errors="replace")):
            where = pins.setdefault(image, {}).setdefault(digest, [])
            if str(relative) not in where:
                where.append(str(relative))
    return pins


def images_section(root: Path, latest: dict[str, str]) -> list[str]:
    rows = [
        f"| {image} | {pinned[:19]} | {latest[image][:19]} | {', '.join(files)} |"
        for image, digests in sorted(pinned_images(root).items())
        if latest.get(image, "").startswith("sha256:")
        for pinned, files in sorted(digests.items())
        if latest[image] != pinned
    ]
    head = ["## Container images (pinned by digest)", ""]
    if not rows:
        return [*head, NOTHING, ""]
    table = ["| Image | Pinned | Tag's current digest | Pinned in |", "|---|---|---|---|"]
    return [*head, *table, *rows, ""]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--pip", type=Path)
    parser.add_argument("--npm", type=Path)
    parser.add_argument("--actions-latest", type=Path)
    parser.add_argument("--images-latest", type=Path)
    parser.add_argument("--list-actions", action="store_true")
    parser.add_argument("--list-images", action="store_true")
    args = parser.parse_args(argv)
    if args.list_actions:
        print("\n".join(sorted(pinned_actions(args.root))))
        return 0
    if args.list_images:
        print("\n".join(sorted(pinned_images(args.root))))
        return 0
    out = ["# Outdated dependencies", ""]
    if args.pip:
        out += python_section(args.root, json.loads(args.pip.read_text() or "[]"))
    if args.npm:
        out += npm_section(json.loads(args.npm.read_text() or "{}"))
    if args.actions_latest:
        out += actions_section(args.root, json.loads(args.actions_latest.read_text() or "{}"))
    if args.images_latest:
        out += images_section(args.root, json.loads(args.images_latest.read_text() or "{}"))
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
