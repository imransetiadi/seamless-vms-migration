"""scripts/outdated-report.py and .github/workflows/outdated.yml: with Dependabot gone, a weekly
report of the direct dependencies that have newer releases - Python (seamless/pyproject.toml,
requirements.txt), the dashboard's npm packages and the pinned GitHub Actions - written to the
job summary. It opens no pull request and never fails the run; a release the declared range
does not admit (a reviewed cap such as openstacksdk's, SDD §21.7) is marked, not proposed."""

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()
SCRIPT = ROOT / "scripts" / "outdated-report.py"
WORKFLOW = ROOT / ".github" / "workflows" / "outdated.yml"


def _tree(tmp_path: Path) -> Path:
    """A scratch repository with the files the report reads."""
    (tmp_path / "seamless").mkdir(parents=True)
    (tmp_path / "seamless" / "pyproject.toml").write_text(
        "[project]\n"
        'name = "x"\n'
        'dependencies = ["fastapi>=0.110", "PyYAML>=6"]\n'
        "[project.optional-dependencies]\n"
        'openstack = ["openstacksdk>=4.21.0,!=4.23.0,<4.25.0"]\n'
    )
    (tmp_path / "requirements.txt").write_text(
        "# the collection's runtime\n"
        "openstacksdk>=4.22.0,!=4.23.0,<4.25.0\n"
        "-r other.txt\n"
        "git+https://example.invalid/fork.git#egg=thing\n"
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    checkout = "3d3c42e5aac5ba805825da76410c181273ba90b1"
    codeql = "1bd86f2fdb4b86187fa4059f16f31a3202acc9ea"
    (workflows / "ci.yml").write_text(
        "jobs:\n  a:\n    steps:\n"
        f"      - uses: actions/checkout@{checkout} # v7.0.1\n"
        f"      - uses: github/codeql-action/init@{codeql} # v4.39.4\n"
        f"      - uses: github/codeql-action/analyze@{codeql} # v4.39.4\n"
    )
    return tmp_path


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _json(tmp_path: Path, name: str, data: object) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


def test_the_report_lists_only_direct_python_dependencies_and_marks_a_release_the_range_refuses(
    tmp_path,
):
    root = _tree(tmp_path / "repo")
    pip = _json(
        tmp_path,
        "pip.json",
        [
            {"name": "openstacksdk", "version": "4.24.0", "latest_version": "4.25.0"},
            {"name": "fastapi", "version": "0.115.0", "latest_version": "0.116.1"},
            {"name": "pyyaml", "version": "6.0.3", "latest_version": "6.0.4"},
            {"name": "idna", "version": "3.7", "latest_version": "3.10"},  # transitive only
        ],
    )
    done = _run(root, "--pip", pip)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    rows = {line.split(" | ")[0].lstrip("| "): line for line in lines if line.startswith("| ")}
    assert rows["fastapi"].startswith("| fastapi | 0.115.0 | 0.116.1 |"), rows["fastapi"]
    assert "allowed" in rows["fastapi"]
    assert rows["pyyaml"].startswith("| pyyaml | 6.0.3 | 6.0.4 |") and "allowed" in rows["pyyaml"]
    sdk = rows["openstacksdk"]
    assert "outside" in sdk and "<4.25.0" in sdk, sdk
    assert "idna" not in done.stdout


def test_the_report_lists_the_dashboard_packages_npm_finds_behind(tmp_path):
    root = _tree(tmp_path / "repo")
    npm = _json(
        tmp_path,
        "npm.json",
        {
            "@testing-library/jest-dom": {"current": "7.0.1", "wanted": "7.0.1", "latest": "8.0.0"},
            "vite": {"current": "8.0.1", "wanted": "8.0.3", "latest": "8.0.3"},
        },
    )
    done = _run(root, "--npm", npm)
    assert done.returncode == 0, done.stderr
    assert "| @testing-library/jest-dom | 7.0.1 | 8.0.0 | major |" in done.stdout
    assert "| vite | 8.0.1 | 8.0.3 | patch |" in done.stdout


def test_the_report_names_each_pinned_action_once_and_its_newer_release(tmp_path):
    root = _tree(tmp_path / "repo")
    listed = _run(root, "--list-actions")
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.split() == ["actions/checkout", "github/codeql-action"]
    latest = _json(
        tmp_path, "actions.json", {"actions/checkout": "v7.0.1", "github/codeql-action": "v4.40.0"}
    )
    done = _run(root, "--actions-latest", latest)
    assert done.returncode == 0, done.stderr
    assert "| github/codeql-action | v4.39.4 | v4.40.0 |" in done.stdout
    assert "actions/checkout |" not in done.stdout  # up to date


def test_the_report_says_so_when_nothing_is_behind(tmp_path):
    root = _tree(tmp_path / "repo")
    done = _run(
        root,
        "--pip",
        _json(tmp_path, "pip.json", []),
        "--npm",
        _json(tmp_path, "npm.json", {}),
        "--actions-latest",
        _json(
            tmp_path, "a.json", {"actions/checkout": "v7.0.1", "github/codeql-action": "v4.39.4"}
        ),
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.count("Nothing behind.") == 3


def test_the_outdated_workflow_runs_weekly_only_reads_and_never_fails_the_run():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    triggers = workflow[True]  # YAML 1.1 reads the key "on" as true
    assert set(triggers) == {"schedule", "workflow_dispatch"}, "no push or pull_request trigger"
    assert workflow["permissions"] == {"contents": "read"}
    [job] = workflow["jobs"].values()
    assert job.get("timeout-minutes"), "a bounded job"
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "scripts/outdated-report.py" in runs
    assert "GITHUB_STEP_SUMMARY" in runs
    assert "pull-request" not in WORKFLOW.read_text().replace("pull_request", "")
    report_steps = [s for s in job["steps"] if "outdated" in s.get("run", "")]
    assert report_steps and all(s.get("continue-on-error") for s in report_steps)
    # it installs the newest packages, whose build code runs in the job: leave no token in .git
    [checkout] = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
    assert checkout.get("with", {}).get("persist-credentials") is False


def test_the_report_ignores_a_latest_release_that_is_not_the_actions_version(tmp_path):
    """github/codeql-action's newest release is a CLI bundle (codeql-bundle-v2.27.6), not a
    version of the action: no row, rather than a false "behind"."""
    root = _tree(tmp_path / "repo")
    latest = _json(
        tmp_path,
        "actions.json",
        {"actions/checkout": "v7.0.1", "github/codeql-action": "codeql-bundle-v2.27.6"},
    )
    done = _run(root, "--actions-latest", latest)
    assert done.returncode == 0, done.stderr
    assert "codeql-bundle" not in done.stdout
    assert "Nothing behind." in done.stdout


def test_the_report_checks_every_release_an_action_is_pinned_at(tmp_path):
    """Two workflows can pin one action at different releases: each pin behind is named."""
    root = _tree(tmp_path / "repo")
    old = "0123456789abcdef0123456789abcdef01234567"
    (root / ".github" / "workflows" / "other.yml").write_text(
        f"jobs:\n  b:\n    steps:\n      - uses: actions/checkout@{old} # v6.0.0\n"
    )
    latest = _json(
        tmp_path, "actions.json", {"actions/checkout": "v7.0.1", "github/codeql-action": "v4.39.4"}
    )
    done = _run(root, "--actions-latest", latest)
    assert done.returncode == 0, done.stderr
    assert "| actions/checkout | v6.0.0 | v7.0.1 |" in done.stdout


def test_the_report_names_only_what_is_older_than_the_latest_release(tmp_path):
    """A version ahead of the latest release (a prerelease taken on purpose) is not behind; a
    package npm lists without an installed version says so instead of guessing a change."""
    root = _tree(tmp_path / "repo")
    npm = _json(
        tmp_path,
        "npm.json",
        {
            "ahead": {"current": "2.0.0-rc.1", "wanted": "2.0.0-rc.1", "latest": "1.9.0"},
            "missing": {"wanted": "1.2.0", "latest": "1.2.0"},
        },
    )
    latest = _json(
        tmp_path, "actions.json", {"actions/checkout": "v7.0.0", "github/codeql-action": "v4.39.4"}
    )
    done = _run(root, "--npm", npm, "--actions-latest", latest)
    assert done.returncode == 0, done.stderr
    assert "| ahead |" not in done.stdout
    assert "| missing | not installed | 1.2.0 | - |" in done.stdout
    assert "actions/checkout |" not in done.stdout  # pinned v7.0.1 is ahead of v7.0.0


def _step(name_part: str) -> str:
    [job] = yaml.safe_load(WORKFLOW.read_text())["jobs"].values()
    [step] = [s for s in job["steps"] if name_part in s.get("name", "")]
    return step["run"]


def test_the_workflow_s_own_steps_build_valid_json_and_a_summary_when_a_step_failed(tmp_path):
    """The two shell steps run as GitHub runs them (bash -e): the actions step writes valid JSON
    even when a repository answers no version tag, and the summary step still reports when the
    pip and npm steps left nothing behind (they continue on error)."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    gh = stubs / "gh"
    gh.write_text(
        '#!/bin/sh\ncase "$2" in\n'
        "  repos/actions/checkout/*) echo v7.0.1 ;;\n"
        "  repos/github/codeql-action/*) echo v999.0.0 ;;\n"
        "  *) ;;\nesac\n"
    )
    gh.chmod(0o755)
    python = stubs / "python"
    python.symlink_to(sys.executable)
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    summary = tmp_path / "summary.md"
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_STEP_SUMMARY": str(summary),
        "HOME": str(tmp_path),
    }
    for name in ("Latest release of each pinned action", "Write the report to the job summary"):
        done = subprocess.run(
            ["bash", "-e", "-c", _step(name)], cwd=ROOT, env=env, capture_output=True, text=True
        )
        assert done.returncode == 0, (name, done.stderr)
    latest = json.loads((runner_temp / "actions.json").read_text())
    assert latest["actions/checkout"] == "v7.0.1"
    assert latest["github/codeql-action"] == "v999.0.0"
    assert set(latest) == {"actions/checkout", "github/codeql-action"}  # the others: no tag
    report = summary.read_text()
    assert "# Outdated dependencies" in report
    assert re.search(r"^\| github/codeql-action \| v[\d.]+ \| v999\.0\.0 \|$", report, re.M), report
    assert "## Python" not in report and "## Dashboard" not in report  # those steps left nothing
