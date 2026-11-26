"""The CI workflows run GitHub Actions pinned by commit SHA (Security.md §9.2).

A tag (``@v4``) can be moved, or force-pushed by whoever compromises the action's repository, to
make CI run other code with the workflow's token; a full commit SHA cannot. The version stays in a
comment (``# v4.4.0``) so a reader, and whoever bumps the pin, knows the release.
"""

import re

import yaml

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


def test_every_checkout_leaves_no_token_in_the_repository():
    """actions/checkout keeps the job token in .git/config by default; the jobs then install
    packages whose build code runs in the workspace and could read it. No job uses git with
    credentials after checking out (the repository is public; gitleaks-action takes its token
    from env), so every checkout sets persist-credentials: false."""
    persisting = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        for name, job in yaml.safe_load(workflow.read_text())["jobs"].items():
            for step in job.get("steps", []):
                if not step.get("uses", "").startswith("actions/checkout@"):
                    continue
                if (step.get("with") or {}).get("persist-credentials") is not False:
                    persisting.append(f"{workflow.name}: {name}")
    assert persisting == []


def _collection_steps() -> dict[str, str]:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    return {step.get("name", ""): step.get("run", "") for step in ci["jobs"]["collection"]["steps"]}


def test_collection_unit_tests_run_in_parallel_with_the_pinned_xdist():
    """tests/unit ran serially for ~19 of the collection job's 20 minutes, the CI critical path;
    pytest-xdist spreads it over the runner's CPUs. The job installs the pin upstream's
    requirements-tests.txt already carries, so the two never drift."""
    steps = _collection_steps()
    pin = re.search(
        r"^pytest-xdist==\S+$",
        (ROOT / "requirements-tests.txt").read_text(),
        re.M,
    )
    assert pin, "requirements-tests.txt no longer pins pytest-xdist"
    install = next(run for name, run in steps.items() if name.startswith("Install ansible-core"))
    assert pin.group(0) in install
    unit = next(run for name, run in steps.items() if name.startswith("Collection unit tests"))
    assert re.search(r"pytest -n auto\b.* tests/unit", unit)
    # every test still runs: the step still fails on a skipped one
    assert "unexpected skipped tests" in unit


def test_control_plane_tests_run_in_parallel_with_the_coverage_gate():
    """The control-plane suite took ~700 s serially, the CI critical path once the collection and
    browser jobs were parallel; each xdist worker gets its own PostgreSQL database
    (tests/pg_support.py), and the coverage gate (NFR-11) covers every worker's lines."""
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = {s.get("name", ""): s.get("run", "") for s in ci["jobs"]["control-plane"]["steps"]}
    run = next(run for name, run in steps.items() if name.startswith("Unit and integration tests"))
    assert re.search(r"pytest -n auto\b.*--cov-fail-under=85", run)
    pyproject = (ROOT / "seamless" / "pyproject.toml").read_text()
    dev = pyproject.split("\ndev = [", 1)[1].split("]", 1)[0]
    assert "pytest-xdist" in dev


def _playwright_runs() -> list[str]:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    return [
        step.get("run", "")
        for job in ci["jobs"].values()
        for step in job.get("steps", [])
        if "test:e2e" in step.get("run", "") or "playwright test" in step.get("run", "")
    ]


def test_every_playwright_project_runs_in_ci_on_a_runner_of_its_own():
    """The browser smoke ran Chromium and WebKit in one job, the CI critical path; more workers on
    one runner made WebKit time out (#256). Each browser now runs on a runner of its own, both
    Playwright configs per browser, so no project is ever dropped."""
    runs = _playwright_runs()
    for config, command in (
        ("playwright.config.ts", "test:e2e"),
        ("playwright.api.config.ts", "--config playwright.api.config.ts"),
    ):
        projects = re.findall(r"name: '(\w+)'", (ROOT / "dashboard" / config).read_text())
        assert sorted(projects) == ["chromium", "webkit"], config
        for project in projects:
            matching = [
                run
                for run in runs
                if command in run
                and (config != "playwright.config.ts" or "playwright.api" not in run)
            ]
            assert any(f"--project={project}" in run for run in matching), (config, project)
