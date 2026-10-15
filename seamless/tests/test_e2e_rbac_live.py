"""The live RBAC matrix probes what SDD §12 says about a plan's policy fields (QASuite S-01).

An operator may send a policy field at the plan's current value; only a change needs the
approver. The route therefore reads the plan before it can refuse, and a plan that does not exist
answers 404 to an operator as to anyone: the operator's probe needs a real plan and a value that
plan does not hold. The approver's probe keeps a plan that does not exist, so the check never
changes the stack it runs against.
"""

import json
import secrets
import subprocess
import sys

import pytest

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()
ROLES = ("VIEWER", "OPERATOR", "APPROVER", "ADMIN")

#: a curl that logs each request - method, path, body, the role whose token it carried and whether
#: it asked for the status code only - and
#: answers GET /plans with the plans in $PLANS, every request that asks for its status with 200
FAKE_CURL = """#!{python}
import json, os, sys
args = sys.argv[1:]
roles = {{os.environ[r]: r for r in {roles!r}}}
method, body, role, url = "GET", "", "-", args[-1]
for flag, value in zip(args, args[1:]):
    if flag == "-X":
        method = value
    elif flag == "-d":
        body = value
    elif flag == "-H" and value.startswith("@"):
        token = open(value[1:], encoding="utf-8").read().strip().rsplit(" ", 1)[-1]
        role = roles.get(token, "?")
with open(os.environ["CALLS_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps([method, url.split("/api/v1", 1)[-1], body, role, "-w" in args]) + "\\n")
if "-w" in args:
    print("200", end="")
elif method == "GET" and url.endswith("/plans"):
    print(os.environ["PLANS"])
else:
    print("{{}}")
"""

PLAN = {
    "id": "plan-1",
    "require_approval": True,
    "require_distinct_approver": True,
    "auto_cutover": False,
    "cutover_window": None,
}
#: a plan holding the other values: the probes must still change each of them
PLAN_SET = {
    "id": "plan-2",
    "require_approval": False,
    "require_distinct_approver": False,
    "auto_cutover": True,
    "cutover_window": {"start": "2030-01-01T01:00:00Z", "end": "2030-01-01T05:00:00Z"},
}


def _rbac_live(tmp_path, plans):
    """Run rbac-live.sh against the fake curl; return the run and the requests it made."""
    tokens = {role: "smg_" + secrets.token_urlsafe(24) for role in ROLES}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(
        FAKE_CURL.format(python=sys.executable, roles=ROLES), encoding="utf-8"
    )
    (bin_dir / "curl").chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CALLS_LOG": str(log),
        "TMPDIR": str(tmp_path),
        "BASE": "http://127.0.0.1:9/api/v1",
        "PLANS": json.dumps(plans),
        **tokens,
    }
    run = subprocess.run(
        ["bash", str(ROOT / "tests" / "e2e" / "rbac-live.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return run, calls


def _patch_probes(calls):
    """(path, changed fields, role) of each PATCH that sends a policy field."""
    policy = {"require_approval", "require_distinct_approver", "auto_cutover", "cutover_window"}
    probes = []
    for method, path, body, role, _ in calls:
        if method == "PATCH" and body and policy & set(json.loads(body)):
            probes.append((path, json.loads(body), role))
    return probes


@pytest.mark.parametrize("plan", [PLAN, PLAN_SET], ids=["defaults", "the other values"])
def test_the_operators_policy_probe_changes_a_real_plans_value_and_the_approvers_touches_none(
    tmp_path, plan
):
    """SDD §12: the operator's PATCH probes go to a plan the stack has, each with a value that
    plan does not hold, so the 403 is the role's; the approver's go to a plan that does not
    exist, so nothing is ever changed."""
    run, calls = _rbac_live(tmp_path, [plan])

    probes = _patch_probes(calls)
    operator = [(path, body) for path, body, role in probes if role == "OPERATOR"]
    approver = [(path, body) for path, body, role in probes if role == "APPROVER"]
    assert len(operator) == 3 and len(approver) == 3, run.stdout + run.stderr
    for path, body in operator:
        assert path == f"/plans/{plan['id']}", path
        for field, value in body.items():
            assert value != plan[field], (field, value)
    assert all(path == "/plans/nope" for path, _ in approver), approver
    # the plan was read once, with the viewer's token: no role higher than needed
    reads = [c[3] for c in calls if c[0] == "GET" and c[1] == "/plans" and not c[4]]
    assert reads == ["VIEWER"], reads


@pytest.mark.parametrize("plans", [[], "not json"], ids=["no plan", "an unreadable answer"])
def test_without_a_plan_to_probe_the_patch_probes_are_skipped_not_failed(tmp_path, plans):
    """A stack with no plan (or a plan list the script cannot read) leaves the operator's PATCH
    probe nothing to compare against: the script says SKIP, probes no PATCH, and fails nothing
    for it."""
    run, calls = _rbac_live(tmp_path, plans)

    assert _patch_probes(calls) == [], _patch_probes(calls)
    assert "SKIP" in run.stdout, run.stdout
    # the policy probes' own lines carry their JSON body; the stub's 200s fail the other checks
    failed = [line for line in run.stdout.splitlines() if line.startswith("FAIL") and "{" in line]
    assert not [line for line in failed if "PATCH" in line], failed
