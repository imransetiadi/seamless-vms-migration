"""Secret files stay out of the image build context and out of git (SDD §17, Security.md C9-04).

``docker build -f seamless/Containerfile .`` reads ``seamless/Containerfile.dockerignore`` (a link
to ``seamless/.containerignore``) instead of the root ``.dockerignore``; Podman builds pass the file
with ``--ignorefile``. Both must keep out every kind of secret file the repository's tools and docs
create, because the Containerfile bind-mounts the whole context into a build step and the context
reaches local, remote and CI builders.
"""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

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
    "tests/func/auth_tenant.yml",  # functional tests (docs/src/devel/dev-env-setup.rst)
    "tests/func/auth_admin.yml",
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


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_every_secret_path_is_git_ignored():
    """A ``git add -A`` never stages a secret file (CLAUDE.md §7)."""
    tracked = [
        path
        for path in SECRET_PATHS
        if subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", path], check=False
        ).returncode
        != 0
    ]
    assert tracked == [], f"git would track: {tracked}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_finder_metadata_is_git_ignored():
    """macOS Finder writes ``.DS_Store`` into every folder it opens: ``git status`` must not offer
    them for a ``git add -A``."""
    tracked = [
        path
        for path in (".DS_Store", "dashboard/.DS_Store", "scripts/_skeleton_role_/.DS_Store")
        if subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", path], check=False
        ).returncode
        != 0
    ]
    assert tracked == [], f"git would track: {tracked}"


def test_auth_file_generation_never_prints_credentials():
    """``make generate-auth-files`` writes cloud credentials to a 0600 file, never to the log."""
    recipe = [
        line
        for line in (ROOT / "Makefile").read_text().splitlines()
        if "auth-from-clouds.sh" in line
    ]
    assert recipe, "the generate-auth-files recipe moved"
    for line in recipe:
        assert "tee" not in line, line
        assert "umask 077" in line and "> " in line, line


def _makefile_recipe_lines() -> list[str]:
    """The Makefile's recipe lines, backslash continuations joined, without the tab and ``@``."""
    lines: list[str] = []
    current: str | None = None
    for raw in (ROOT / "Makefile").read_text().splitlines():
        if current is None:
            if not raw.startswith("\t"):
                continue
            current = raw[1:]
        else:
            current += " " + raw.strip()
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        lines.append(current.lstrip("@"))
        current = None
    return lines


def _run_recipe_line(line: str, path: Path, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    """Run one recipe line the way make does (the Makefile's SHELL and .SHELLFLAGS) with only
    ``path`` on PATH."""
    makefile = (ROOT / "Makefile").read_text()
    shell = re.search(r"^SHELL := (.+)$", makefile, re.M)
    flags = re.search(r"^\.SHELLFLAGS := (.+)$", makefile, re.M)
    assert shell and flags, "the Makefile no longer sets SHELL and .SHELLFLAGS"
    return subprocess.run(
        [shell.group(1), *flags.group(1).split(), line],
        cwd=cwd,
        env={"PATH": str(path)},
        capture_output=True,
        text=True,
        check=False,
    )


def _makefile_target_recipe(target: str) -> str:
    """One target's recipe, backslash continuations joined (the lines under ``target:``)."""
    lines = (ROOT / "Makefile").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    recipe = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line[1:])
    return "\n".join(recipe).replace("\\\n", " ")


def _ci_step_run(name: str) -> str:
    """The run script of the CI step whose name starts with ``name``, continuations joined."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    step = next(step for step in steps if step.get("name", "").startswith(name))
    return step["run"].replace("\\\n", " ")


def test_seamless_check_runs_the_ci_checks_that_run_locally():
    """``make seamless-check`` is "everything CI runs that can run locally" (QASuite §13): the
    control-plane coverage gate (NFR-11), the playbook syntax checks and ansible-lint, with CI's
    threshold, playbooks and lint targets."""
    recipe = _makefile_target_recipe("seamless-check")
    gate = re.search(r"--cov-fail-under=(\d+)", _ci_step_run("Unit and integration tests"))
    assert gate and "--cov=seamless_migrate" in recipe
    assert f"--cov-fail-under={gate.group(1)}" in recipe
    playbooks = r"for pb in (.*?); do"
    ci_playbooks = re.search(playbooks, _ci_step_run("Playbook syntax checks"), re.S)
    make_playbooks = re.search(playbooks, recipe, re.S)
    assert ci_playbooks and make_playbooks and "--syntax-check" in recipe
    assert make_playbooks.group(1).split() == ci_playbooks.group(1).split()
    targets = r"ansible-lint\"? --offline ([^\n;]*)"
    ci_lint = re.search(targets, _ci_step_run("ansible-lint"))
    make_lint = re.search(targets, recipe)
    assert ci_lint and make_lint
    assert make_lint.group(1).split() == ci_lint.group(1).split()
    # the venv the target runs in has what the gate needs
    extras = tomllib.loads((ROOT / "seamless" / "pyproject.toml").read_text())["project"]
    assert any(req.startswith("pytest-cov") for req in extras["optional-dependencies"]["dev"])


def test_seamless_check_fails_when_any_playbook_syntax_check_fails(tmp_path):
    """A failing syntax check fails ``make seamless-check`` even when a later playbook passes: bash
    ignores errexit for a loop inside an && list, so the loop has to stop on the first failure."""
    line = next(line for line in _makefile_recipe_lines() if "--syntax-check" in line)
    bin_dir = tmp_path / "seamless" / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (tmp_path / ".cache/colltree/ansible_collections/os_migrate/os_migrate").mkdir(parents=True)
    fakes = {
        # the first playbook of the list has a syntax error, the others pass
        "ansible-playbook": '#!/bin/sh\ncase "$*" in *playbooks/import_workloads.yml*) '
        'echo "syntax error" >&2; exit 4;; esac\nexit 0\n',
        "ansible-lint": "#!/bin/sh\nexit 0\n",
    }
    for name, body in fakes.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    script = line.replace("$(CURDIR)", str(tmp_path)).replace("$$", "$")
    run = _run_recipe_line(script, Path("/usr/bin:/bin"), cwd=tmp_path)
    assert run.returncode != 0, run.stdout + run.stderr
    assert "syntax error" in run.stderr


@pytest.mark.parametrize("tool", ["gitleaks", "actionlint", "shellcheck"])
def test_seamless_check_fails_when_an_installed_scanner_fails(tool, tmp_path):
    """``make seamless-check`` skips a scanner that is not installed, but one that is installed and
    finds problems fails the check (QASuite §13): ``command -v tool && tool || echo skipped``
    reported such a failure as "not installed: skipped" and passed."""
    line = next(line for line in _makefile_recipe_lines() if f"command -v {tool}" in line)
    fake = tmp_path / "bin" / tool
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\necho '{tool}: 1 problem found'\nexit 1\n")
    fake.chmod(0o755)
    run = _run_recipe_line(line, fake.parent)
    assert run.returncode != 0, run.stdout + run.stderr
    assert f"{tool}: 1 problem found" in run.stdout
    assert "not installed" not in run.stdout


@pytest.mark.parametrize("tool", ["gitleaks", "actionlint", "shellcheck"])
def test_seamless_check_skips_a_scanner_that_is_not_installed(tool, tmp_path):
    line = next(line for line in _makefile_recipe_lines() if f"command -v {tool}" in line)
    (tmp_path / "bin").mkdir()
    run = _run_recipe_line(line, tmp_path / "bin")
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"{tool} not installed: skipped" in run.stdout


def test_strict_gitleaks_config_never_allowlists_a_secret_path():
    """``.gitleaks.toml`` drives CI, pre-commit and history scans: a force-added ``.env`` or token
    file must be reported, so none of its allowlisted paths may cover a secret file (Security.md
    S-17). Working-tree scans add the git-ignored local secrets through ``.gitleaks-tree.toml``."""
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    paths = [re.compile(p) for p in config.get("allowlist", {}).get("paths", [])]
    for block in config.get("allowlists", []):
        paths += [re.compile(p) for p in block.get("paths", [])]
    covered = [path for path in SECRET_PATHS if any(rx.search(path) for rx in paths)]
    assert covered == [], f".gitleaks.toml allowlists secret files: {covered}"


def test_tree_gitleaks_config_skips_only_the_git_ignored_secret_files():
    """``.gitleaks-tree.toml`` (working-tree scans) skips the local secret files a checkout holds
    and nothing broader: each of its patterns stands for one kind of git-ignored secret file."""
    config = tomllib.loads((ROOT / ".gitleaks-tree.toml").read_text(encoding="utf-8"))
    assert config["extend"]["path"] == ".gitleaks.toml"
    paths = [re.compile(p) for p in config["allowlist"]["paths"]]
    reported = [path for path in SECRET_PATHS if not any(rx.search(path) for rx in paths)]
    assert reported == [], f"working-tree scans would report local secret files: {reported}"
    samples = [*SECRET_PATHS, "seamless/tests/__pycache__/test_memory.cpython-313.pyc"]
    stray = [rx.pattern for rx in paths if not any(rx.search(path) for path in samples)]
    assert stray == [], f"patterns that match no kind of secret file: {stray}"
    for code in (
        "seamless/src/seamless_migrate/app.py",
        "deploy/compose/compose.yaml",
        "README.md",
    ):
        assert not any(rx.search(code) for rx in paths), code


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks is not installed")
def test_gitleaks_reports_a_force_added_env_file(tmp_path):
    """The scan CI and pre-commit run catches a committed compose ``.env`` (synthetic key)."""
    repo = tmp_path / "repo"
    (repo / "deploy" / "compose").mkdir(parents=True)
    (repo / ".gitleaks.toml").write_text((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    synthetic = "TYPESAFE_API_KEY=" + "tsk_" + "9fQ2xLmA7vR4tB8nZ1cK6dJ3wE5yH0pU"
    (repo / "deploy" / "compose" / ".env").write_text(synthetic + "\n")

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-f", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "leak")
    scan = subprocess.run(
        ["gitleaks", "git", "--no-banner", "--redact", "-c", ".gitleaks.toml", "."],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert scan.returncode == 1, scan.stdout + scan.stderr
