"""The HA failover drill, ``tests/e2e/ha-failover.sh``, against the fake cluster (SDD §25.10,
QASuite LAB-HA01, LAB-HA02, plan task HA8). It finds the leading pod through ``GET /api/v1/health``
- the lease's holder is the pod's name and 8 hex digits of its process (SDD §12, §25.6) -, takes it
away during a cutover whose source is stopped, waits for another replica to take the lease with a
higher epoch and drive, and checks the plan's migrations as DEMO-06 does: one source stop and one
destination server per VM, and ``migration.phase`` orders the FSM allows (SDD §5.1, §25.5).

``tests/fake_kube.py`` stands in for kubectl and oc, and a fake curl answers as the control plane
would from the same state: the lease's holder leads while its pod runs - not deleted, its node up,
not cut off from PostgreSQL -; once it does not, the lease lapses after ``TAKEOVER_AFTER`` answers
of the health, and another running pod takes it with the next epoch (``TAKEOVER=never``: none
does). A drill that passed whatever they answered would prove nothing.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from seamless_migrate.config import find_repo_root
from tests import fake_kube

ROOT = find_repo_root()
DRILL = ROOT / "tests" / "e2e" / "ha-failover.sh"

#: generated here, never a literal: it must reach no argument, no output and no log line
TOKEN = "smg_" + secrets.token_hex(16)
PLAN = "plan-6b1e0c9d2a"
WEB, DB = "mig-00000000a1", "mig-00000000b2"
LEADER, FOLLOWER = "seamless-2", "seamless-1"
HOLDER = "seamless-2-9c41d7ae"

FAKE_CURL = r'''#!/usr/bin/env python3
"""curl as the drill calls it (-fsS -m N -H @FILE URL): the control plane's answers, read from the
fake cluster's state (FAKE_K8S_STATE); each call is logged with its arguments to STUB_LOG."""
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"cmd": "curl", "args": args}) + "\n")
headers, url, i = [], "", 0
while i < len(args):
    if args[i] in ("-H", "-m", "--max-time", "-o", "-w", "-X", "-d"):
        if args[i] == "-H":
            headers.append(args[i + 1])
        i += 2
        continue
    if args[i].startswith("http"):
        url = args[i]
    i += 1
state = Path(os.environ["FAKE_K8S_STATE"])
st = json.loads(state.read_text())
route, _, query = url.split("/api/v1", 1)[1].partition("?")
params = dict(pair.split("=", 1) for pair in query.split("&") if pair)


def answer(body):
    state.write_text(json.dumps(st))
    print(json.dumps(body))
    sys.exit(0)


def leads(name):
    """A replica leads while its pod runs: not deleted, its node up, PostgreSQL in reach."""
    pod = next((p for p in st["deployment"]["pods"] if p["name"] == name), None)
    return (
        pod is not None and pod["phase"] == "Running" and not pod.get("terminating")
        and pod.get("node") not in st.get("nodes_down", [])
        and name not in st.get("cut_off", [])
    )


if route == "/health":  # public, as the API serves it
    lease = st["lease"]
    if lease["holder"] and not leads(lease["holder"].rsplit("-", 1)[0]):
        lease["lapsed"] = lease.get("lapsed", 0) + 1  # no renewal: valid a while, then expired
        if lease["lapsed"] > int(os.environ["TAKEOVER_AFTER"]):
            lease["holder"] = None
    elif not lease["holder"] and os.environ.get("TAKEOVER") != "never":
        taker = next((p["name"] for p in st["deployment"]["pods"] if leads(p["name"])), None)
        if taker:  # a follower's try takes it with the next epoch; its orchestrator starts
            bump = 0 if os.environ.get("TAKEOVER") == "same-epoch" else 1
            lease.update(holder=taker + "-5e2f0c71", epoch=lease["epoch"] + bump, ticks=0)
            st["taken_over"] = True
    elif st.get("taken_over") and os.environ.get("TAKEOVER") != "stalled":
        lease["ticks"] += 1
    holder = lease["holder"]
    orchestrator = {"running": bool(holder), "last_tick_age_s": 1.0 if holder else None,
                    "ticks": lease.get("ticks", 40) if holder else 0, "healthy": bool(holder)}
    leader = {"leading": False, "holder": holder, "epoch": lease["epoch"] if holder else None,
              "since": "2026-11-01T09:00:00+00:00" if holder else None}
    answer({"status": "ok", "version": "0.5.0", "demo": False, "demo_speed": None,
            "auth_disabled": False, "db": "ok", "orchestrator": orchestrator, "leader": leader})
bearer = "Authorization: Bearer " + os.environ["FAKE_TOKEN"]
sent = [Path(h[1:]).read_text().strip() if h.startswith("@") else h for h in headers]
if bearer not in sent:
    print("curl: (22) The requested URL returned error: 401", file=sys.stderr)
    sys.exit(22)
api = st["api"]
after = st.get("taken_over")
if route == "/migrations":
    migrations = api["migrations_after"] if after else api["migrations"]
    answer([m for m in migrations if m["plan_id"] == params["plan_id"]])
if route == "/events":
    events = api["events"] + (api["events_after"] if after else [])
    events = [e for e in events if e["seq"] > int(params.get("since", 0))]
    for key in ("plan_id", "migration_id"):
        events = [e for e in events if key not in params or e.get(key) == params[key]]
    page = min(int(params.get("limit", 100)), int(os.environ.get("FAKE_PAGE", "1000")))
    answer(events[-page:] if params.get("tail") == "true" else events[:page])
print("curl: (22) The requested URL returned error: 404", file=sys.stderr)
sys.exit(22)
'''

#: POWER_OFF_CMD, CUT_DB_CMD and HEAL_DB_CMD: each records the environment the drill gives it and
#: does to the fake cluster what its namesake does to a real one
FAKE_COMMAND = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
env = {key: os.environ.get(key) for key in ("NODE", "POD", "POD_IP", "NAMESPACE")}
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"cmd": name, "env": env}) + "\n")
state = Path(os.environ["FAKE_K8S_STATE"])
st = json.loads(state.read_text())
if name == "power-off":
    st.setdefault("nodes_down", []).append(env["NODE"])
elif name == "cut-db":
    st.setdefault("cut_off", []).append(env["POD"])
elif name == "heal-db":
    st["cut_off"] = [pod for pod in st.get("cut_off", []) if pod != env["POD"]]
state.write_text(json.dumps(st))
"""

#: what the deposed replica logs (SDD §25.8): its deposition and the writes the fence refused
DEPOSED_LOG = "\n".join(
    [
        "INFO seamless_migrate.leader: seamless-2-9c41d7ae took the orchestrator lease with "
        "epoch 3 (expired)",
        "WARNING seamless_migrate.leader: could not renew the orchestrator lease of epoch 3: "
        "connection timeout",
        "WARNING seamless_migrate.leader: seamless-2-9c41d7ae was deposed from the orchestrator "
        "lease of epoch 3: its renew deadline passed: no renewal for 20 s; 1 playbook process "
        "group(s) killed",
        "WARNING seamless_migrate.store: the fence refused put migration: this process's epoch "
        "is 3, the lease's is 4",
        "",
    ]
)


def _phase(seq: int, mid: str, frm: str, to: str, **data: str) -> dict:
    change = {"from": frm, "to": to, "reason": "", **data}
    return {"seq": seq, "kind": "migration.phase", "plan_id": PLAN, "migration_id": mid,
            "data": change}  # fmt: skip


def _event(seq: int, mid: str | None, kind: str, **data: str) -> dict:
    return {"seq": seq, "kind": kind, "plan_id": PLAN, "migration_id": mid, "data": data}


def _migration(
    mid: str, name: str, phase: str, stopped: str | None, server: str | None, downtime: float | None
) -> dict:
    return {
        "id": mid,
        "plan_id": PLAN,
        "vm": {"source_id": "vm-" + mid[-2:], "name": name, "project": "web"},
        "strategy": "warm",
        "phase": phase,
        "estimate": {"strategy": "warm", "downtime_s": 280.0},
        "downtime_started_at": stopped,
        "downtime_ended_at": None,
        "actual_downtime_s": downtime,
        "destination_server_id": server,
    }


STOPPED_AT = "2026-11-01T10:00:00+00:00"
#: web-01's warm cutover, its source stopped when the drill sees it; db-01 keeps warm meanwhile
EVENTS = [
    _event(1, None, "plan.started"),
    _phase(2, WEB, "pending", "validating"),
    _phase(3, DB, "pending", "validating"),
    _phase(4, WEB, "validating", "ready"),
    _phase(5, DB, "validating", "ready"),
    _phase(6, WEB, "ready", "precopy"),
    _phase(7, DB, "ready", "precopy"),
    _phase(8, WEB, "precopy", "syncing"),
    _phase(9, WEB, "syncing", "awaiting_cutover"),
    _phase(10, DB, "precopy", "awaiting_cutover"),
    _phase(11, WEB, "awaiting_cutover", "cutover"),
    _event(12, WEB, "migration.downtime_started", at=STOPPED_AT),
]
#: what the new leader wrote once it resumed the cutover from its checkpoint
EVENTS_AFTER = [
    _phase(20, WEB, "cutover", "verifying", destination_server_id="srv-0001"),
    _event(21, WEB, "migration.downtime_ended", cause="verified"),
    _phase(22, WEB, "verifying", "completed"),
    _phase(23, DB, "awaiting_cutover", "syncing"),
    _phase(24, DB, "syncing", "awaiting_cutover"),
]


def _cluster() -> dict:
    """Two replicas on two nodes - seamless-1 on node-a, seamless-2 on node-b -, seamless-2 leading
    at epoch 3, and the plan's two migrations as the API answers them."""
    state = fake_kube.cluster(replicas=2, data_modes=("ReadWriteMany",))
    follower = state["deployment"]["pods"][0]
    follower.update(node="node-a", ip="192.0.2.11")
    state["deployment"]["pods"].append(dict(follower, name=LEADER, node="node-b", ip="192.0.2.12"))
    state["serial"] = 2  # the next pod the ReplicaSet starts is seamless-3
    state["lease"] = {"holder": HOLDER, "epoch": 3}
    state["logs"] = {LEADER: DEPOSED_LOG}
    state["version"] = (
        "Client Version: v1.31.2\nKustomize Version: v5.4.2\nServer Version: v1.31.1\n"
    )
    state["api"] = {
        "migrations": [
            _migration(WEB, "web-01", "cutover", STOPPED_AT, None, None),
            _migration(DB, "db-01", "awaiting_cutover", None, None, None),
        ],
        "migrations_after": [
            _migration(WEB, "web-01", "completed", STOPPED_AT, "srv-0001", 312.4),
            _migration(DB, "db-01", "awaiting_cutover", None, None, None),
        ],
        "events": EVENTS,
        "events_after": EVENTS_AFTER,
    }
    return state


class Lab:
    """The fake cluster, the fakes first on PATH, and the drill's environment."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.state_file = tmp_path / "cluster.json"
        fake_kube.install(self.bin, self.state_file, _cluster())
        for name, body in (
            ("curl", FAKE_CURL),
            ("power-off", FAKE_COMMAND),
            ("cut-db", FAKE_COMMAND),
            ("heal-db", FAKE_COMMAND),
        ):
            (self.bin / name).write_text(body, encoding="utf-8")
            (self.bin / name).chmod(0o755)
        self.log = tmp_path / "calls.log"
        self.env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "STUB_LOG": str(self.log),
            "FAKE_K8S_STATE": str(self.state_file),
            "FAKE_TOKEN": TOKEN,
            "FAKE_PAGE": "4",  # the events in pages of four: the drill reads every page
            "TAKEOVER_AFTER": "1",
            "TOKEN": TOKEN,
            "SEAMLESS_URL": "https://seamless.apps.example.com",
            "PLAN": PLAN,
            "NAMESPACE": "seamless-migrate",
            "CUTOVER_WAIT": "10",
            "TAKEOVER_WAIT": "20",
            "SETTLE_WAIT": "10",
            "POLL": "0",
            "PYTHON": sys.executable,
        }

    def change(self, edit) -> None:
        """Edit the fake cluster's state before the drill runs."""
        state = json.loads(self.state_file.read_text())
        edit(state)
        self.state_file.write_text(json.dumps(state))

    def run(self, *argv: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(DRILL), *argv],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=120,
        )

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def state(self) -> dict:
        return json.loads(self.state_file.read_text())


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return Lab(tmp_path)


def _kube_calls(calls: list[dict], tool: str) -> list[list[str]]:
    return [c["args"] for c in calls if c["cmd"] == tool]


def _assert_no_token(proc: subprocess.CompletedProcess[str], calls: list[dict]) -> None:
    assert TOKEN not in proc.stdout + proc.stderr
    assert not [c for c in calls if TOKEN in json.dumps(c)], "the token reached an argument"


@pytest.mark.parametrize(("platform", "tool"), [("kubernetes", "kubectl"), ("openshift", "oc")])
@pytest.mark.parametrize("mode", ["delete", "node", "db"])
def test_the_failover_drill_finds_the_leading_pod_through_the_health(lab, platform, tool, mode):
    """LAB-HA01, LAB-HA02 (SDD §25.10): the leading pod is the one /health's leader names -
    seamless-2 of two, the holder without the 8 hex digits of its process -, and the drill takes
    that pod away and no other: deleted (delete); its node cordoned, then powered off by the
    operator's command (node); cut off from PostgreSQL by the operator's command, then given it
    back once the takeover came (db). It waits for seamless-1 to take the lease with the next
    epoch and drive, prints the times for QASuite §13 with the versions, and checks the plan's
    migrations as DEMO-06 does."""
    env = {"node": {"POWER_OFF_CMD": "power-off"}, "db": {"CUT_DB_CMD": "cut-db"}}.get(mode, {})
    if mode == "db":
        env["HEAL_DB_CMD"] = "heal-db"
    proc = lab.run(platform, mode, **env)
    out = proc.stdout + proc.stderr
    calls = lab.calls()
    assert proc.returncode == 0, out
    assert f"LEADER    {LEADER} on node node-b" in proc.stdout, out
    health = [
        i for i, c in enumerate(calls) if c["cmd"] == "curl" and c["args"][-1].endswith("/health")
    ]
    kube = _kube_calls(calls, tool)
    assert kube and not _kube_calls(calls, "oc" if tool == "kubectl" else "kubectl"), calls
    deletes = [args for args in kube if "delete" in args]
    cordons = [args for args in kube if "cordon" in args]
    commands = {c["cmd"]: c["env"] for c in calls if "env" in c}
    taken = {
        "delete": [i for i, c in enumerate(calls) if c["cmd"] == tool and "delete" in c["args"]],
        "node": [i for i, c in enumerate(calls) if c["cmd"] == "power-off"],
        "db": [i for i, c in enumerate(calls) if c["cmd"] == "cut-db"],
    }[mode]
    assert len(taken) == 1 and health and health[0] < taken[0], calls  # found through the health
    where = {"NODE": "node-b", "POD": LEADER, "POD_IP": "192.0.2.12"}
    where["NAMESPACE"] = "seamless-migrate"
    if mode == "delete":
        assert deletes == [["-n", "seamless-migrate", "delete", "pod", LEADER, "--wait=false"]]
        assert cordons == [] and commands == {}
    elif mode == "node":
        adm = ["adm"] if tool == "oc" else []  # OpenShift's node commands: oc adm cordon
        assert cordons == [[*adm, "cordon", "node-b"]] and deletes == []
        assert commands == {"power-off": where}
        uncordon = " ".join([tool, *adm, "uncordon", "node-b"])
        assert f"power it on, then {uncordon}" in proc.stdout, out
    else:
        assert commands == {"cut-db": where, "heal-db": where} and deletes == cordons == []
        # given back once the takeover came, before the checks: the deposed replica serves on
        order = [c["cmd"] if c["cmd"] != "curl" else c["args"][-1].split("?")[0] for c in calls]
        took = max(i for i, c in enumerate(order) if c.endswith("/health"))
        assert order.index("cut-db") < took < order.index("heal-db"), order
        assert order.index("heal-db") < min(i for i, c in enumerate(order) if c.endswith("/events"))
        assert "HEALED    HEAL_DB_CMD gave pod seamless-2 its PostgreSQL back" in proc.stdout
        assert lab.state()["cut_off"] == []
        logged = [line for line in proc.stdout.splitlines() if line.startswith("LOG       ")]
        assert len(logged) == 2, out  # the deposition and the refused write, nothing else
        assert "was deposed from the orchestrator lease of epoch 3" in logged[0], out
        assert "the fence refused put migration" in logged[1], out
    assert f"TAKEOVER  {FOLLOWER} took the lease at epoch 4" in proc.stdout, out
    assert "RECOVERY  a leader drove the migrations again" in proc.stdout, out
    assert "VERSIONS  seamless 0.5.0; Server Version: v1.31.1" in proc.stdout, out
    assert "PASS  web-01 (mig-00000000a1): 1 source stop, 1 destination server," in proc.stdout
    assert "PASS  db-01 (mig-00000000b2): 0 source stops, 0 destination servers," in proc.stdout
    # the downtime of the VM whose cutover lost its leader, beside its estimate (SDD §25.5, §25.10)
    assert "DOWNTIME  web-01 (mig-00000000a1): 312 s, estimated 280 s" in proc.stdout, out
    assert "DOWNTIME  db-01" not in proc.stdout  # in no step at the take-away
    assert "FAIL" not in out
    _assert_no_token(proc, calls)


@pytest.mark.parametrize(
    ("takeover", "said"),
    [
        ("never", "no new leader within TAKEOVER_WAIT (3 s) of the take-away"),
        ("same-epoch", "the health's last answer named seamless-1-5e2f0c71 at epoch 3"),
        ("stalled", "its orchestrator did not drive within TAKEOVER_WAIT (3 s)"),
    ],
    ids=["no replica takes the lease", "a holder at the old epoch", "the new leader never drives"],
)
def test_a_drill_whose_takeover_never_comes_fails(lab, takeover, said):
    """SDD §25.5: a leader drives the migrations again about 35 s after the loss. A drill whose
    takeover does not come within its bound fails, naming the wait, and checks nothing it could
    only pass: no replica takes the lease; another holder at the old epoch, which no takeover
    makes - each one increases the epoch (SDD §25.3, §25.8) -; or a new leader that never
    drives."""
    proc = lab.run("kubernetes", "delete", TAKEOVER=takeover, TAKEOVER_WAIT="3")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    fails = [line for line in proc.stdout.splitlines() if line.startswith("FAIL  ")]
    assert len(fails) == 1 and said in fails[0], out
    assert "RECOVERY" not in out and "PASS" not in out
    assert [args for args in _kube_calls(lab.calls(), "kubectl") if "delete" in args]
    _assert_no_token(proc, lab.calls())


def test_a_drill_that_finds_two_source_stops_fails(lab):
    """The hazard the fence exists for (SDD §25.4, §25.5): two orchestrators on one migration stop
    its source twice. One cutover stops its source once - its downtime clock opened once, the first
    stop's time kept across the takeover -: a second migration.downtime_started in the last cutover
    fails the drill, naming the VM and both stops."""
    second = _event(19, WEB, "migration.downtime_started", at="2026-11-01T10:00:41+00:00")
    lab.change(lambda st: st["api"]["events_after"].insert(0, second))
    proc = lab.run("openshift", "delete")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert (
        "FAIL  web-01 (mig-00000000a1): 2 source stops in its last cutover "
        "(migration.downtime_started at seq 12, 19)" in proc.stdout
    ), out
    assert "PASS  web-01" not in proc.stdout
    assert "PASS  db-01 (mig-00000000b2)" in proc.stdout, out  # each VM judged on its own
    _assert_no_token(proc, lab.calls())


def test_a_drill_that_finds_two_destination_servers_fails(lab):
    """SDD §25.5, §7.2: the new leader finds a destination server the old one created by its record
    or its name. One recorded at the take-away, srv-0001, and another after the takeover, srv-0002,
    are two servers for one VM: the drill fails, naming both."""

    def two(st: dict) -> None:
        st["api"]["migrations"][0]["destination_server_id"] = "srv-0001"
        st["api"]["migrations_after"][0]["destination_server_id"] = "srv-0002"
        st["api"]["events_after"][0]["data"]["destination_server_id"] = "srv-0002"

    lab.change(two)
    proc = lab.run("kubernetes", "delete")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert (
        "FAIL  web-01 (mig-00000000a1): 2 destination servers in its last cutover "
        "(srv-0001, srv-0002)" in proc.stdout
    ), out


def test_a_drill_that_finds_a_phase_order_the_fsm_forbids_fails(lab):
    """DEMO-06 (QASuite §6.6, SDD §5.1): each migration.phase follows an order the FSM allows. The
    drill also reads each change's own start: a change from cutover after the move to verifying is
    a second orchestrator's write - verifying to failed alone would read as allowed -, and so is a
    move the FSM forbids, such as awaiting_cutover to completed. Each fails the drill."""

    def diverged(st: dict) -> None:
        st["api"]["events_after"][1:3] = [_phase(21, WEB, "cutover", "failed")]
        st["api"]["migrations_after"][0]["phase"] = "failed"
        st["api"]["events_after"].append(_phase(25, DB, "awaiting_cutover", "completed"))

    lab.change(diverged)
    proc = lab.run("kubernetes", "delete")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert (
        "FAIL  web-01 (mig-00000000a1): cutover -> failed at seq 21 follows the change to "
        "verifying at seq 20" in proc.stdout
    ), out
    assert (
        "FAIL  db-01 (mig-00000000b2): awaiting_cutover -> completed at seq 25, a change the FSM "
        "does not allow" in proc.stdout
    ), out


def test_the_drill_takes_nothing_away_without_an_explicit_mode_and_its_command(lab):
    """No mode is a default, and none takes a pod away before the drill knows it can: without a
    mode, without the operator's command a mode needs - powering a node off is the platform's -,
    with a single replica, or with a leader that is no control plane pod, it exits 2 having
    deleted, cordoned, powered off and cut off nothing."""
    cases = [
        ((), {}),
        (("kubernetes",), {}),
        (("kubernetes", "evict"), {}),
        (("kubernetes", "node"), {}),
        (("openshift", "db"), {}),
        (("kubernetes", "delete"), {"PLAN": ""}),
    ]
    for argv, env in cases:
        proc = lab.run(*argv, **env)
        assert proc.returncode == 2, (argv, proc.stdout + proc.stderr)
        assert lab.calls() == [], argv  # refused before any call
    assert "POWER_OFF_CMD" in lab.run("kubernetes", "node").stderr
    assert "CUT_DB_CMD" in lab.run("openshift", "db").stderr
    assert "none is a default" in lab.run().stderr

    def single(st: dict) -> None:
        st["deployment"]["pods"] = [p for p in st["deployment"]["pods"] if p["name"] == LEADER]

    lab.change(single)
    proc = lab.run("kubernetes", "delete")
    assert proc.returncode == 2 and "two replicas" in proc.stderr, proc.stdout + proc.stderr
    lab.change(lambda st: st["lease"].update(holder="seamless-postgres-0-0f0f0f0f"))
    proc = lab.run("kubernetes", "delete")
    assert proc.returncode == 2 and "no control plane pod" in proc.stderr, proc.stderr
    acted = [c for c in lab.calls() if c["cmd"] in ("power-off", "cut-db")]
    acted += [c for c in lab.calls() if {"delete", "cordon"} & set(c.get("args", []))]
    assert acted == []
    _assert_no_token(proc, lab.calls())


def test_the_fake_cluster_answers_the_drill_as_kubectl_and_oc_do(lab):
    """tests/fake_kube.py, as the drill reads and acts on one pod (plan task HA8): a pod's node and
    address by jsonpath, NotFound with kubectl's exit 1 for a pod not there, a deleted pod still
    listed while it terminates and another started at once, a node cordoned by kubectl cordon or oc
    adm cordon, a pod's log - nothing for the Deployment's, as seamless-ctl.sh's logs reads it -
    and the cluster's version."""

    def kube(tool: str, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(lab.bin / tool), *argv], env=lab.env, capture_output=True, text=True, timeout=30
        )

    ns = ("-n", "seamless-migrate")
    node = kube("kubectl", *ns, "get", "pod", LEADER, "-o", "jsonpath={.spec.nodeName}")
    address = kube("oc", *ns, "get", "pod", LEADER, "-o", "jsonpath={.status.podIP}")
    assert (node.returncode, node.stdout, address.stdout) == (0, "node-b", "192.0.2.12")
    absent = kube("kubectl", *ns, "get", "pod", "seamless-9", "-o", "jsonpath={.spec.nodeName}")
    assert absent.returncode == 1 and absent.stdout == ""
    assert absent.stderr == 'Error from server (NotFound): pods "seamless-9" not found\n'
    gone = kube("kubectl", *ns, "delete", "pod", LEADER, "--wait=false")
    assert (gone.returncode, gone.stdout) == (0, f'pod "{LEADER}" deleted\n')
    pods = {p["name"]: p for p in lab.state()["deployment"]["pods"]}
    assert pods[LEADER]["terminating"] and pods["seamless-3"]["phase"] == "Running"
    assert not pods[FOLLOWER].get("terminating")
    listed = kube(
        "kubectl", *ns, "get", "pods", "-l", "app.kubernetes.io/name=seamless", "-o", "name"
    )
    assert listed.stdout.split() == ["pod/seamless-1", "pod/seamless-2", "pod/seamless-3"]
    assert kube("kubectl", "cordon", "node-a").stdout == "node/node-a cordoned\n"
    assert kube("oc", "adm", "cordon", "node-b").stdout == "node/node-b cordoned\n"
    assert lab.state()["cordoned"] == ["node-a", "node-b"]
    assert kube("oc", *ns, "logs", LEADER, "-c", "seamless").stdout == DEPOSED_LOG
    followed = kube("kubectl", *ns, "logs", "deployment/seamless", "-f", "--tail=200")
    assert (followed.returncode, followed.stdout) == (0, "")
    assert kube("kubectl", "version").stdout.endswith("Server Version: v1.31.1\n")
