"""The live RBAC matrix probes what SDD §12 says about a plan's policy fields (QASuite S-01).

An operator may send a policy field at the plan's current value; only a change needs the
approver. The route therefore reads the plan before it can refuse, and a plan that does not exist
answers 404 to an operator as to anyone: the operator's probe needs a real plan and a value that
plan does not hold. The approver's probe keeps a plan that does not exist, so the check never
changes the stack it runs against.
"""

import asyncio
import json
import secrets
import subprocess
import sys
import threading
import time

import httpx
import pytest
import uvicorn

from seamless_migrate.api import routes_plans
from seamless_migrate.api.app import create_app
from seamless_migrate.config import find_repo_root
from seamless_migrate.domain.enums import PlanStatus, Role
from seamless_migrate.domain.models import Plan
from seamless_migrate.store import Store
from tests.api_support import DESTINATION, SOURCE, api_settings

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
        # the policy field changes the plan's value; the guard names no provider (RV16-01)
        assert body.pop("source_provider_id") == "rbac-probe-none", body
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


def test_a_plan_answer_read_only_in_part_skips_no_patch_probe_silently(tmp_path):
    """Review RV16-04: the probes stopped quietly at the first field the plan lacked (a stack of an
    older version, a renamed field): one PATCH probe of three ran, and the script said neither SKIP
    nor FAIL. Each field the plan lacks is now named as skipped."""
    plan = {"id": "plan-1", "require_approval": True, "cutover_window": None}
    run, calls = _rbac_live(tmp_path, [plan])

    operator = [body for _, body, role in _patch_probes(calls) if role == "OPERATOR"]
    assert len(operator) == 2, operator
    assert "SKIP PATCH policy probe require_distinct_approver" in run.stdout, run.stdout


class LiveServer:
    """The app on 127.0.0.1, served by uvicorn in a thread of its own."""

    def __init__(self, app):
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        )
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve()), daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            assert time.monotonic() < deadline, "the server did not start"
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(30)


@pytest.fixture
def stack(tmp_path):
    """(base URL, tokens, store, plan id): a running server of this app whose first plan is a draft
    an approver made, with approval and a distinct approver required."""
    settings, tokens = api_settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'rbac.db'}")
    store.create_schema()
    with LiveServer(create_app(settings, store)) as base:
        admin = {"Authorization": f"Bearer {tokens[Role.admin]}"}
        for provider in (SOURCE, DESTINATION):
            assert (
                httpx.post(f"{base}/api/v1/providers", json=provider, headers=admin).status_code
                == 201
            )
        created = httpx.post(
            f"{base}/api/v1/plans",
            headers={"Authorization": f"Bearer {tokens[Role.approver]}"},
            json={
                "name": "Finance web tier",
                "source_provider_id": SOURCE["id"],
                "destination_provider_id": DESTINATION["id"],
                "vm_ids": ["vm-web-01"],
                "require_approval": True,
                "require_distinct_approver": True,
            },
        )
        assert created.status_code == 201, created.text
        yield base, tokens, store, created.json()["id"]
    store.dispose()


def _rbac_live_against(tmp_path, base, tokens):
    """rbac-live.sh with the real curl against ``base``."""
    work = tmp_path / "e2e-tmp"
    work.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(work),
        "BASE": f"{base}/api/v1",
        "VIEWER": tokens[Role.viewer],
        "OPERATOR": tokens[Role.operator],
        "APPROVER": tokens[Role.approver],
        "ADMIN": tokens[Role.admin],
    }
    return subprocess.run(
        ["bash", str(ROOT / "tests" / "e2e" / "rbac-live.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _policy(base, tokens, plan_id):
    plan = httpx.get(
        f"{base}/api/v1/plans/{plan_id}", headers={"Authorization": f"Bearer {tokens[Role.admin]}"}
    ).json()
    fields = ("require_approval", "require_distinct_approver", "auto_cutover", "cutover_window")
    return {field: plan[field] for field in fields} | {"status": plan["status"]}


def test_the_operators_patch_probe_never_changes_the_plan_it_probes(tmp_path, stack, monkeypatch):
    """Review RV16-01: when PATCH lets an operator change a policy field - the regression the
    probe exists to find - each probe was a write: the plan was left with approval off, no distinct
    approver and the probe's 2030 window, back in draft. Each probe now also names a source
    provider that does not exist, so a role check that let it through meets the provider check
    (400) before anything is written."""
    base, tokens, _, plan_id = stack
    before = _policy(base, tokens, plan_id)

    async def no_check(request, fields, principal):  # the regression the probe looks for
        return None

    monkeypatch.setattr(routes_plans, "_check_policy_fields", no_check)
    run = _rbac_live_against(tmp_path, base, tokens)

    assert f"FAIL operator PATCH /plans/{plan_id}" in run.stdout, run.stdout + run.stderr
    after = _policy(base, tokens, plan_id)
    assert after == before, f"the probe changed the plan it probes: {before} -> {after}"


def test_with_the_role_check_in_place_the_matrix_passes_and_changes_nothing(tmp_path, stack):
    base, tokens, _, plan_id = stack
    before = _policy(base, tokens, plan_id)

    run = _rbac_live_against(tmp_path, base, tokens)

    assert run.returncode == 0 and "RBAC matrix OK" in run.stdout, run.stdout + run.stderr
    assert "SKIP" not in run.stdout
    assert _policy(base, tokens, plan_id) == before


@pytest.mark.parametrize("status", [PlanStatus.running, PlanStatus.cancelled])
def test_a_plan_in_any_status_gives_the_operator_403_before_409(tmp_path, stack, status):
    """The route checks the policy fields before the plan's status, so a running or cancelled first
    plan still gives the operator 403 (QASuite S-01)."""
    base, tokens, store, plan_id = stack
    plan, version = store.get_versioned("plan", plan_id, Plan)
    store.put("plan", plan.model_copy(update={"status": status}), expected_version=version)

    run = _rbac_live_against(tmp_path, base, tokens)

    assert run.returncode == 0 and "RBAC matrix OK" in run.stdout, run.stdout + run.stderr
    assert _policy(base, tokens, plan_id)["status"] == status
