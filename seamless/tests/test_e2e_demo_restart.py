"""tests/e2e/demo-restart.sh - DEMO-04 and DEMO-06 (QASuite §6.6) - against stubs (docs-drift
DD3-03): the restart drill goes through this host's Make targets, which pick its engine and Docker
context (SDD §17.1), and a restart that does not happen fails DEMO-06 - it restarted through the
Colima context alone, its failure silenced, and printed "DEMO-06 OK" on any other host."""

import json
import os
import shutil
import subprocess
import sys

import pytest

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()
E2E = ROOT / "tests" / "e2e"

#: answers by URL: the stats, no migration and no running plan, a live health, two events in order
FAKE_CURL = """#!{python}
import json, os, sys
args = sys.argv[1:]
url = next((a for a in args if a.startswith("http")), "")
with open(os.environ["CALLS_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"cmd": "curl", "url": url}}) + "\\n")
if "/stats" in url:
    print(json.dumps({{"by_phase": {{"ready": 1}}}}))
elif "/events" in url:
    print(json.dumps([{{"seq": 1, "kind": "plan.created", "data": {{}}}},
                      {{"seq": 2, "kind": "plan.started", "data": {{}}}}]))
elif "/migrations" in url or "/plans" in url:
    print("[]")
elif "/health" in url:
    print('{{"status": "ok"}}')
"""

#: make and docker record their arguments; make exits MAKE_EXIT
FAKE_TOOL = """#!{python}
import json, os, sys
with open(os.environ["CALLS_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"cmd": os.path.basename(sys.argv[0]), "args": sys.argv[1:]}}) + "\\n")
sys.exit(int(os.environ.get("MAKE_EXIT", "0")) if sys.argv[0].endswith("make") else 0)
"""


def _drill(tmp_path, make_exit: int):
    tree = tmp_path / "tree"
    (tree / "tests" / "e2e").mkdir(parents=True)
    for name in ("demo-restart.sh", "token-headers.sh"):
        shutil.copy(E2E / name, tree / "tests" / "e2e" / name)
    venv = tree / "seamless" / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text(
        f'#!/bin/sh\nPYTHONPATH="{ROOT / "seamless" / "src"}" exec "{sys.executable}" "$@"\n'
    )
    (venv / "python").chmod(0o755)
    bin_dir, tmp = tmp_path / "bin", tmp_path / "tmp"
    bin_dir.mkdir()
    tmp.mkdir()
    (bin_dir / "curl").write_text(FAKE_CURL.format(python=sys.executable))
    for tool in ("make", "docker"):
        (bin_dir / tool).write_text(FAKE_TOOL.format(python=sys.executable))
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    os.symlink(sys.executable, bin_dir / "python3")
    log = tmp_path / "calls.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CALLS_LOG": str(log),
        "TMPDIR": str(tmp),
        "BASE": "http://127.0.0.1:9",
        "TOKEN": "smg_" + "a" * 32,
        "APPROVER_TOKEN": "smg_" + "b" * 32,
        "RESTART_AT": "0",
        "MAKE_EXIT": str(make_exit),
    }
    run = subprocess.run(
        ["bash", str(tree / "tests" / "e2e" / "demo-restart.sh")],
        env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return run, calls, tree


def test_the_restart_drill_restarts_through_this_hosts_make_targets(tmp_path):
    """DEMO-06 restarts the control plane by the Make targets - its container stopped, then
    started as it was: its mode kept, PostgreSQL running -, never a fixed Docker context."""
    run, calls, tree = _drill(tmp_path, make_exit=0)
    out = run.stdout + run.stderr
    assert run.returncode == 0, out
    assert "DEMO-06 OK" in run.stdout, out
    made = [c["args"] for c in calls if c["cmd"] == "make"]
    assert made == [
        ["-C", str(tree), "--no-print-directory", "seamless-stop"],
        ["-C", str(tree), "--no-print-directory", "seamless-start"],
    ], out
    assert not [c for c in calls if c["cmd"] == "docker"], out


@pytest.mark.parametrize("make_exit", [1, 2])
def test_a_restart_that_does_not_happen_fails_demo_06(tmp_path, make_exit):
    """A restart that fails - no engine, another context, a stack that is down - is DEMO-06
    failing: its output shown, never "DEMO-06 OK"."""
    run, _, _ = _drill(tmp_path, make_exit=make_exit)
    out = run.stdout + run.stderr
    assert run.returncode != 0, out
    assert "DEMO-06 OK" not in run.stdout, out
    assert "DEMO-06 FAILED" in run.stdout and "not restarted" in run.stdout, out
