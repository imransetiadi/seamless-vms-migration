"""Secret files stay out of the image build context and out of git (SDD §17, Security.md C9-04).

``docker build -f seamless/Containerfile .`` reads ``seamless/Containerfile.dockerignore`` (a link
to ``seamless/.containerignore``) instead of the root ``.dockerignore``; Podman builds pass the file
with ``--ignorefile``. Both must keep out every kind of secret file the repository's tools and docs
create, because the Containerfile bind-mounts the whole context into a build step and the context
reaches local, remote and CI builders.
"""

import http.server
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest
import yaml

from seamless_migrate.ai.redaction import redact
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


# what GET /api/v1/health of the control plane answers (SDD §12), and another program's 404
SEAMLESS_HEALTH = (
    b'{"status":"ok","version":"0.1.0","demo":true,"db":"ok",'
    b'"orchestrator":{"running":true,"last_tick_age_s":1.0,"ticks":3,"healthy":true}}'
)
OTHER_PROGRAM = b'{"code":"not_found","status":404}'


def _serve(host: str, port: int, status: int, body: bytes) -> http.server.ThreadingHTTPServer:
    """A loopback HTTP server answering every GET with ``status`` and ``body``."""

    class Answer(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server's name)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    class Server(http.server.ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    server = Server((host, port), Answer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("on_ipv6", ["another program", "nothing", "seamless"])
def test_seamless_up_says_to_open_127_when_localhost_is_another_program(on_ipv6, tmp_path):
    """The stack publishes on 127.0.0.1 only, but a browser opens ``localhost`` on ::1 first: when
    another program answers on [::1]:<port>, ``make seamless-up``/``seamless-demo`` name the address
    of the dashboard (a user took the other program's 404 for a dead dashboard, 2026-10-10)."""
    servers = [_serve("127.0.0.1", 0, 200, SEAMLESS_HEALTH)]
    port = servers[0].server_address[1]
    try:
        if on_ipv6 != "nothing":
            try:
                other = on_ipv6 == "another program"
                servers.append(
                    _serve(
                        "::1",
                        port,
                        404 if other else 200,
                        OTHER_PROGRAM if other else SEAMLESS_HEALTH,
                    )
                )
            except OSError:
                pytest.skip("no IPv6 loopback on this machine")
        probe = tmp_path / "probe.mk"
        probe.write_text("seamless-localhost-probe: ; @$(SEAMLESS_LOCALHOST_NOTE)\n")
        result = subprocess.run(
            [
                "make",
                "-s",
                "--no-print-directory",
                "-f",
                "Makefile",
                "-f",
                str(probe),
                "seamless-localhost-probe",
                f"SEAMLESS_HOST_PORT={port}",
            ],
            cwd=ROOT,
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    assert result.returncode == 0, result.stderr
    if on_ipv6 == "another program":
        assert f"http://localhost:{port}/ reaches another program" in result.stdout
        assert f"open http://127.0.0.1:{port}/" in result.stdout
    else:
        assert result.stdout == ""


@pytest.mark.parametrize("target", ["seamless-up", "seamless-demo"])
def test_seamless_up_and_demo_check_localhost_once_healthy(target):
    recipe = _makefile_target_recipe(target)
    assert "$(SEAMLESS_LOCALHOST_NOTE)" in recipe
    assert recipe.index("$(SEAMLESS_WAIT_HEALTH)") < recipe.index("$(SEAMLESS_LOCALHOST_NOTE)")


def test_seamless_check_runs_the_ci_checks_that_run_locally():
    """``make seamless-check`` runs CI's checks that need only the venv and the dashboard's
    node_modules (SDD §17.1, QASuite §13): the control-plane coverage gate (NFR-11), the playbook
    syntax checks and ansible-lint, with CI's threshold, playbooks and lint targets."""
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


def test_seamless_check_says_which_ci_checks_it_leaves_out():
    """SDD §17.1: ``make seamless-check`` is not all of CI. Its comment, ``make seamless-help``
    and the README name what it leaves out - each a CI check the recipe does not run - and the
    README says how to run each, so a contributor who passed it knows what CI still runs."""
    recipe = _makefile_target_recipe("seamless-check")
    makefile = (ROOT / "Makefile").read_text()
    lines = makefile.splitlines()
    start = lines.index("seamless-check:")
    comment = []
    for line in reversed(lines[:start]):
        if not line.startswith("#"):
            break
        comment.insert(0, line)
    comment_text = "\n".join(comment)
    help_line = next(line for line in lines if line.strip().startswith('@echo "  seamless-check '))
    readme = (ROOT / "README.md").read_text()
    # the block a contributor reads under ``make seamless-check``, to the end of its code fence
    start_check = readme.index("make seamless-check")
    readme_block = readme[start_check : readme.index("```", start_check)]
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for where, text in (
        ("the Makefile comment", comment_text),
        ("make seamless-help", help_line),
        ("the README", readme),
    ):
        claims = [phrase for phrase in ("every CI check", "Everything CI runs") if phrase in text]
        assert not claims, f"{where} claims all of CI: {claims}"
    # each a CI check the recipe leaves out, with how to run it in the README
    for command in (
        "npm run test:e2e",
        "demo-cutover-refusal-browser.mjs",
        "demo-approval-browser.mjs",
        "demo-signin-browser.mjs",
        "SEAMLESS_TEST_PG_URL",
        "pip-audit",
        "npm audit",
        "trivy config",
    ):
        assert command in ci, command
        assert command not in recipe, command
        assert command in readme_block, command
    for left_out in (
        "Playwright",
        "browser journeys",
        "PostgreSQL",
        "audits",
        "trivy",
        "manifest",
        "image",
    ):
        assert left_out in comment_text, left_out
        assert left_out in help_line, left_out


def test_ci_and_seamless_check_run_the_demo07_script():
    """DEMO-07 (QASuite §6.6, §14.7) runs where the control plane is tested - CI's control-plane
    job and ``make seamless-check`` - since the script needs only the installed control plane. CI
    installs into the runner's Python, not a venv, so it names the ``seamless`` on its PATH."""
    script = "tests/e2e/demo-cutover-refusal.sh"
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = workflow["jobs"]["control-plane"]["steps"]
    step = next((s for s in steps if s.get("name", "").startswith("DEMO-07")), None)
    assert step is not None, [s.get("name") for s in steps]
    ci = _ci_step_run("DEMO-07")
    assert script in ci and "SEAMLESS=seamless" in ci
    # its run takes about 15 s: a hang fails the step, not the job's whole time budget
    assert 0 < step.get("timeout-minutes", 0) <= 5
    assert script in _makefile_target_recipe("seamless-check")


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


def _ids_carrying_a_secret(nodeids: list[str]) -> list[str]:
    """The tests among ``nodeids`` whose ids hold what the control plane redacts as a secret (SDD
    §13.4), by test name only: their ids are never repeated."""
    return sorted({nodeid.split("[")[0] for nodeid in nodeids if redact(nodeid) != nodeid})


def test_ci_runs_the_demo07_browser_journey_against_a_built_dashboard():
    """The dashboard-only DEMO-07 journey (QASuite §14.7) runs in CI's dashboard-e2e job, where
    Playwright's Chromium is installed, after the build its server serves and a control-plane
    install. CI installs into the runner's Python, not a venv, so it names the ``seamless`` on its
    PATH; a hang fails the step, not the job's time budget."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = workflow["jobs"]["dashboard-e2e"]["steps"]
    script = "demo-cutover-refusal-browser.mjs"
    at = next((i for i, step in enumerate(steps) if script in step.get("run", "")), None)
    assert at is not None, [step.get("name") or step.get("uses") for step in steps]
    journey = steps[at]
    assert "SEAMLESS=seamless" in journey["run"]
    assert 0 < journey.get("timeout-minutes", 0) <= 5
    before = " ".join(f"{step.get('uses', '')} {step.get('run', '')}" for step in steps[:at])
    assert "npm run build" in before and "actions/setup-python" in before
    assert re.search(r"pip install -e \.", before), before


def test_ci_runs_the_maker_checker_browser_journey_after_the_demo07_one():
    """The maker-checker journey (QASuite §14.7, SDD §5.4) runs in the same dashboard-e2e job,
    after the DEMO-07 journey's build and install, with the runner's ``seamless`` and a time
    bound of its own."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = workflow["jobs"]["dashboard-e2e"]["steps"]
    runs = [step.get("run", "") for step in steps]
    at = next((i for i, run in enumerate(runs) if "demo-approval-browser.mjs" in run), None)
    assert at is not None, [step.get("name") or step.get("uses") for step in steps]
    demo07 = next(i for i, run in enumerate(runs) if "demo-cutover-refusal-browser.mjs" in run)
    assert at > demo07 and "SEAMLESS=seamless" in runs[at]
    assert 0 < steps[at].get("timeout-minutes", 0) <= 5


def test_no_test_id_carries_a_secret():
    """A parametrized case's id comes from its arguments, and ids reach pytest's cache - which the
    working-tree scan of ``make seamless-check`` reads (S-17) - ``-v`` output and CI logs. So no id
    may hold a secret: such a case gets explicit ``ids``."""
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT / "seamless",
        capture_output=True,
        text=True,
        timeout=300,
    )
    ids = [line for line in run.stdout.splitlines() if "::" in line]
    assert ids, run.stdout[-1000:] + run.stderr[-1000:]
    leaking = _ids_carrying_a_secret(ids)
    assert leaking == [], f"test ids that carry a secret - give their cases ids=: {leaking}"


def test_the_id_check_names_the_tests_whose_ids_carry_a_pem_key_or_a_url_password():
    pem = "-----BEGIN RSA PRIVATE KEY-----" + "K" * 40 + "-----END RSA PRIVATE KEY-----"
    url = "postgresql://svc:" + "pw" + "@db.example/seamless"
    ids = [
        f"tests/t.py::test_pem[x {pem} y]",
        f"tests/t.py::test_url[{url}]",
        "tests/t.py::test_plain[the cut splits a PEM key]",
        "tests/t.py::test_unparametrized",
    ]
    assert _ids_carrying_a_secret(ids) == ["tests/t.py::test_pem", "tests/t.py::test_url"]


def test_the_containers_stop_grace_leaves_room_for_the_orchestrator_to_stop():
    """SDD §8: on SIGTERM the server waits up to 5 s for open requests, then stop() may take 15 s
    (a playbook gets SIGTERM, then SIGKILL after 10 s); the container's grace covers both — 30 s on
    Compose (its default 10 s would kill the stop), 60 s on OpenShift."""
    compose = yaml.safe_load((ROOT / "deploy/compose/compose.yaml").read_text(encoding="utf-8"))
    assert compose["services"]["seamless"]["stop_grace_period"] == "30s"
    deployment = yaml.safe_load(
        (ROOT / "deploy/openshift/deployment.yaml").read_text(encoding="utf-8")
    )
    assert deployment["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 60


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


def test_makefile_recipes_edit_files_in_place_portably():
    """SDD §17: the checkout runs `make vendor-links` on macOS too, whose BSD sed reads the script
    after a bare `-i` as the backup suffix and fails. `-i.bak` (then remove the backup) works with
    both seds, as scripts/vendor-openstack-cloud.sh does."""
    import re

    bare = [line for line in _makefile_recipe_lines() if re.search(r"\bsed -i(\s|$)", line)]
    assert not bare, bare
