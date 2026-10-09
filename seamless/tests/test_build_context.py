"""The image build context never carries a secret (SDD §17, Security.md C9-04).

``docker build -f seamless/Containerfile .`` reads ``seamless/Containerfile.dockerignore`` (a link
to ``seamless/.containerignore``) instead of the root ``.dockerignore``; Podman builds pass the file
with ``--ignorefile``. Both must keep out every kind of secret file the repository's tools and docs
create, because the Containerfile bind-mounts the whole context into a build step and the context
reaches local, remote and CI builders.
"""

import re

import pytest

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()

#: one sample per kind of secret file the repository's docs and tools create
SECRET_PATHS = [
    "deploy/compose/.env",  # scripts/compose-init.sh: DB password, Jev token, TypeSafe key
    "deploy/compose/.env.bak",
    "deploy/compose/tokens.yaml",
    "deploy/compose/compose.local.yaml",  # README: VMware passwords for the local stack
    "deploy/openshift/secret.yaml",  # a filled-in copy of secret-example.yaml
    "clouds.yaml",
    "seamless/clouds.yaml",
    "secure.yaml",
    "tests/clouds.yml",  # make generate-auth-files input
    "tests/auth_tenant.yml",  # make generate-auth-files output: cloud passwords
    "tests/auth_admin.yml",
    "deploy/tls/server.key",
    "deploy/tls/server.pem",
    "seamless/secrets/vcenter-dc2/password",  # SEAMLESS_SECRETS_DIR in a checkout
    ".claude/settings.local.json",
    "seamless/data/seamless.db",  # the dev SQLite store: plans and events
]


def _regex(pattern: str) -> re.Pattern[str]:
    """Docker's ignore-pattern syntax (moby/patternmatcher): ``**`` spans directories, ``*`` and
    ``?`` stay within one path segment, ``[...]`` is a character class."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        elif pattern[i] == "[" and "]" in pattern[i:]:
            end = pattern.index("]", i)
            out, i = out + pattern[i : end + 1], end + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def excluded(path: str, lines: list[str]) -> bool:
    """True when ``path`` stays out of the build context: the last matching pattern decides, and a
    pattern that matches a parent directory excludes everything inside it."""
    parts = path.split("/")
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    result = False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        rx = _regex(line.lstrip("!").strip("/"))
        if any(rx.match(candidate) for candidate in candidates):
            result = not negate
    return result


def test_ignore_matcher_follows_docker_semantics():
    lines = ["**/*.key", "deploy/compose/.env", ".claude", "seamless/tests", "!seamless/tests/keep"]
    assert excluded("a/b/c.key", lines) and excluded("c.key", lines)
    assert excluded("deploy/compose/.env", lines) and not excluded("x/deploy/compose/.env", lines)
    assert excluded(".claude/settings.json", lines)
    assert excluded("seamless/tests/x.py", lines) and not excluded("seamless/tests/keep", lines)
    assert not excluded("seamless/src/app.py", lines)


@pytest.mark.parametrize("ignore_file", ["seamless/.containerignore", ".dockerignore"])
def test_build_context_excludes_every_secret_path(ignore_file):
    lines = (ROOT / ignore_file).read_text(encoding="utf-8").splitlines()
    leaked = [path for path in SECRET_PATHS if not excluded(path, lines)]
    assert leaked == [], f"{ignore_file} lets these into the build context: {leaked}"


def test_docker_reads_the_seamless_ignore_file():
    link = ROOT / "seamless" / "Containerfile.dockerignore"
    assert link.is_symlink()
    assert link.resolve() == (ROOT / "seamless" / ".containerignore").resolve()
