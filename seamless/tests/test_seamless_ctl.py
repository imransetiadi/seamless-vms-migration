"""scripts/seamless-ctl.sh (SDD §17.2): one lifecycle - up, down, stop, start, restart, scale,
capacity, status, logs - for docker, podman, kubernetes and openshift. Run against stub tools that
record their arguments: it never runs a second orchestrator, never deletes data, and a drain's token
never reaches a command line."""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from seamless_migrate.config import find_repo_root
from tests import fake_kube

ROOT = find_repo_root()
SCRIPT = ROOT / "scripts" / "seamless-ctl.sh"
TOKEN = "smg_" + "t0ken" * 8  # a test token: never an argument of any tool

#: a tool that records its call - with the header files curl was handed - and answers by the rules;
#: unless a rule says otherwise, curl answers as an API that took the request: -w's %{http_code} the
#: status (200, or 000 for an answer that exits non-zero), GET /api/v1/me an operator's token (its
#: body, then the status on a line of its own when -w asks for it)
STUB = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
keys = ("SEAMLESS_ENGINE", "SEAMLESS_DEMO", "SEAMLESS_SERVICE")
record = {"cmd": name, "args": args, "env": {k: os.environ[k] for k in keys if k in os.environ}}
headers = [Path(args[i + 1][1:]).read_text()
           for i, a in enumerate(args[:-1]) if a == "-H" and args[i + 1].startswith("@")]
if headers:
    record["headers"] = headers
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(record) + "\n")
joined = " ".join(args)
status = name == "curl" and any("%{http_code}" in a for a in args)
for n, rule in enumerate(json.loads(Path(os.environ["STUB_RULES"]).read_text())):
    if rule["cmd"] == name and all(part in joined for part in rule.get("has", [])):
        counter = Path(os.environ["STUB_LOG"] + f".{n}")
        seen = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(seen + 1))
        answers = rule.get("answers", [{}])
        answer = answers[min(seen, len(answers) - 1)]
        code = ("000" if answer.get("exit", 0) else "200") if status else ""
        sys.stdout.write(answer.get("stdout", code))
        sys.exit(answer.get("exit", 0))
if name == "curl" and args[-1:] and args[-1].endswith("/api/v1/me"):
    sys.stdout.write(json.dumps({"name": "ops", "role": "operator"}) + ("\n200" if status else ""))
elif status:
    sys.stdout.write("200")
"""
TOOLS = ("make", "kubectl", "oc", "curl", "docker", "podman")
#: the Route's or Ingress's URL a cluster's drain and resume take (SDD §17.2)
CLUSTER_URL = "https://seamless.example.com"


#: the helper that reads deploy/compose/.env as Compose does (SDD §17.2)
DOTENV = SCRIPT.parent / "dotenv-value.sh"


def _run(
    tmp_path,
    *argv,
    rules=(),
    xtrace=False,
    env_file=None,
    token_mode=0o600,
    cluster=None,
    tools=None,
    cwd=None,
    environment=None,
    tokens_yaml=True,
    path=None,
):
    """Run a copy of the script in a scratch tree with the stub tools first on PATH; return the run
    and the tools' calls in order. With ``cluster`` (a fake_kube state), kubectl and oc keep it -
    read it back with ``_cluster_after(tmp_path)``; ``tools`` adds scripts by name to that PATH,
    ``cwd`` is where the script runs, and ``environment`` is exported to it. ``env_file`` writes
    deploy/compose/.env and, as make seamless-init does, a tokens.yaml beside it (synthetic, no
    token) unless ``tokens_yaml`` is false; ``path`` replaces the PATH after the stubs."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, tree / "scripts" / SCRIPT.name)
    shutil.copy(DOTENV, tree / "scripts" / DOTENV.name)
    if env_file is not None:
        (tree / "deploy" / "compose").mkdir(parents=True, exist_ok=True)
        (tree / "deploy" / "compose" / ".env").write_text(env_file)
        (tree / "deploy" / "compose" / ".env").chmod(0o600)
        if tokens_yaml:
            (tree / "deploy" / "compose" / "tokens.yaml").write_text("tokens: []\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in TOOLS:
        (bin_dir / name).write_text(STUB)
        (bin_dir / name).chmod(0o755)
    fake = {} if cluster is None else fake_kube.install(bin_dir, tmp_path / "cluster.json", cluster)
    for name, script in (tools or {}).items():
        (bin_dir / name).write_text(script)
        (bin_dir / name).chmod(0o755)
    token_file = tmp_path / "operator.token"
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(token_mode)
    log = tmp_path / "calls.log"
    (tmp_path / "rules.json").write_text(json.dumps(list(rules)))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SEAMLESS_", "KUBECONFIG", "DOCKER_"))
    }
    env.update(
        PATH=f"{bin_dir}:{path or os.environ['PATH']}",
        STUB_LOG=str(log),
        STUB_RULES=str(tmp_path / "rules.json"),
        TMPDIR=str(tmp_path),
        **fake,
        **(environment or {}),
    )
    argv = [str(token_file) if a == "@TOKEN_FILE" else a for a in argv]
    cmd = ["bash", *(["-x"] if xtrace else []), str(tree / "scripts" / SCRIPT.name), *argv]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120, cwd=cwd)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return proc, calls, tree


def _cluster_after(tmp_path) -> dict:
    """The fake cluster's state after a ``_run(..., cluster=...)``."""
    return json.loads((tmp_path / "cluster.json").read_text())


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def _args(calls, cmd):
    return [" ".join(c["args"]) for c in calls if c["cmd"] == cmd]


PLATFORMS = ["docker", "podman", "kubernetes", "openshift"]
#: the pods a stop waits for: the running ones, never one that failed (SDD §17.2, review RV38B-03)
ACTIVE = "--field-selector=status.phase!=Failed,status.phase!=Succeeded"
#: PostgreSQL's pods a start or an up waits for: every one but seamless-postgres-0 (review RV42A-01)
OTHER_DATABASES = ACTIVE + ",metadata.name!=seamless-postgres-0"


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("count", ["2", "3", "-1", "one", ""])
def test_scale_takes_zero_or_one_and_refuses_a_second_orchestrator(tmp_path, platform, count):
    """SDD §17.2, §17 (D2): the orchestrator is a singleton - a second replica would run two
    orchestrators against one database -, so scale refuses every count but 0 and 1 before it runs
    anything."""
    proc, calls, _ = _run(tmp_path, platform, "scale", *([count] if count else []))
    assert proc.returncode == 2, proc.stderr
    assert "0 or 1" in proc.stderr and calls == []
    if count in ("2", "3"):
        assert "single" in proc.stderr and "SDD §17" in proc.stderr


@pytest.mark.parametrize("engine", ["docker", "podman"])
@pytest.mark.parametrize(
    ("argv", "target", "extra"),
    [
        (["up"], "seamless-up", []),
        (["up", "--demo"], "seamless-demo", []),
        (["down"], "seamless-down", []),
        (["stop"], "seamless-stop", []),
        (["start"], "seamless-start", []),
        (["start", "--demo"], "seamless-start", ["SEAMLESS_DEMO=true"]),
        (["restart"], "seamless-restart", []),
        (["restart", "--demo"], "seamless-restart", ["SEAMLESS_DEMO=true"]),
        (["scale", "0"], "seamless-stop", []),
        (["scale", "1"], "seamless-start", []),
        (["logs"], "seamless-logs", ["SEAMLESS_SERVICE=seamless"]),
    ],
)
def test_the_single_host_stack_goes_through_its_make_targets(tmp_path, engine, argv, target, extra):
    """SDD §17.2: docker and podman drive the §17.1 stack through its Make targets, the engine
    named, so its engine and context logic stays in one place."""
    proc, calls, tree = _run(tmp_path, engine, *argv)
    assert proc.returncode == 0, proc.stderr
    assert _args(calls, "make") == [
        " ".join(
            ["-C", str(tree), "--no-print-directory", target, f"SEAMLESS_ENGINE={engine}", *extra]
        )
    ]
    assert not [c for c in calls if c["cmd"] in ("docker", "podman", "kubectl", "oc")]


def test_single_host_status_lists_the_containers_and_asks_the_health(tmp_path):
    """SDD §17.2: status is make seamless-ps and GET /api/v1/health on the loopback port."""
    proc, calls, tree = _run(
        tmp_path,
        "docker",
        "status",
        rules=[{"cmd": "curl", "answers": [{"stdout": '{"status":"ok"}'}]}],
    )
    assert proc.returncode == 0, proc.stderr
    assert _args(calls, "make") == [
        f"-C {tree} --no-print-directory seamless-ps SEAMLESS_ENGINE=docker"
    ]
    assert _args(calls, "curl") == ["-fsS --max-time 10 http://127.0.0.1:8080/api/v1/health"]
    assert '{"status":"ok"}' in proc.stdout


CLUSTERS = [("kubernetes", "kubectl", "deploy/kubernetes"), ("openshift", "oc", "deploy/openshift")]
PODS_THERE = [
    {"cmd": "kubectl", "has": ["get pods"], "answers": [{"stdout": "pod/x\n"}]},
    {"cmd": "oc", "has": ["get pods"], "answers": [{"stdout": "pod/x\n"}]},
]


@pytest.mark.parametrize(("platform", "tool", "overlay"), CLUSTERS)
def test_a_cluster_down_stops_the_control_plane_before_postgresql_and_deletes_nothing(
    tmp_path, platform, tool, overlay
):
    """SDD §17.2: down scales the control plane to 0 and waits for its pod to be gone - no
    orchestrator writes to a database being stopped -, then PostgreSQL; PVCs, Secrets and the
    ConfigMap stay."""
    proc, calls, _ = _run(tmp_path, platform, "down", rules=PODS_THERE)
    assert proc.returncode == 0, proc.stderr
    ns = "-n seamless-migrate"
    assert _args(calls, tool) == [
        f"{ns} scale deployment/seamless --replicas=0",
        f"{ns} get pods -l app.kubernetes.io/name=seamless {ACTIVE} -o name",
        f"{ns} wait --for=delete pod -l app.kubernetes.io/name=seamless {ACTIVE} --timeout=600s",
        f"{ns} scale statefulset/seamless-postgres --replicas=0",
        f"{ns} get pods -l app.kubernetes.io/name=seamless-postgres {ACTIVE} -o name",
        f"{ns} wait --for=delete pod -l app.kubernetes.io/name=seamless-postgres {ACTIVE} "
        "--timeout=600s",
    ]
    assert not any("delete" in " ".join(c["args"]).replace("--for=delete", "") for c in calls)


@pytest.mark.parametrize(("platform", "tool", "overlay"), CLUSTERS)
def test_a_cluster_up_applies_the_overlay_and_scales_both_back_to_one(
    tmp_path, platform, tool, overlay
):
    """SDD §17.2: up looks at PostgreSQL first - out of band scaled out, it is back to one before
    the apply (review RV43A-01) -, applies the overlay, then scales PostgreSQL and the control
    plane to one replica each - an apply restores no replica count an earlier down set - and
    waits for both rollouts."""
    rules = [{"cmd": tool, "has": ["get statefulset"], "answers": [{"stdout": "1"}]}]
    proc, calls, tree = _run(tmp_path, platform, "up", "--namespace", "migrations", rules=rules)
    assert proc.returncode == 0, proc.stderr
    ns = "-n migrations"
    assert _args(calls, tool) == [
        # a first up's NotFound reads as nothing, any other failure stops up (review RV44A-04)
        f"{ns} get statefulset/seamless-postgres --ignore-not-found -o jsonpath={{.spec.replicas}}",
        f"{ns} get pods -l app.kubernetes.io/name=seamless-postgres {OTHER_DATABASES} -o name",
        f"{ns} apply -k {tree / overlay}",
        f"{ns} scale statefulset/seamless-postgres --replicas=1",
        f"{ns} rollout status statefulset/seamless-postgres --timeout=600s",
        f"{ns} get pods -l app.kubernetes.io/name=seamless-postgres {OTHER_DATABASES} -o name",
        f"{ns} scale deployment/seamless --replicas=1",
        f"{ns} rollout status deployment/seamless --timeout=600s",
    ]
    other = "oc" if tool == "kubectl" else "kubectl"
    assert _args(calls, other) == []


@pytest.mark.parametrize(("platform", "tool", "overlay"), CLUSTERS)
@pytest.mark.parametrize("postgres", ["0", "1"])
def test_a_cluster_start_brings_postgresql_back_first_when_it_is_down(
    tmp_path, platform, tool, overlay, postgres
):
    """SDD §17.2: start brings PostgreSQL back to one replica when a down left it at 0, then the
    control plane, and waits for each rollout."""
    rules = [{"cmd": tool, "has": ["get statefulset"], "answers": [{"stdout": postgres}]}]
    proc, calls, _ = _run(tmp_path, platform, "start", rules=rules)
    assert proc.returncode == 0, proc.stderr
    ns = "-n seamless-migrate"
    database = [
        f"{ns} scale statefulset/seamless-postgres --replicas=1",
        f"{ns} rollout status statefulset/seamless-postgres --timeout=600s",
    ]
    assert _args(calls, tool) == [
        f"{ns} get statefulset/seamless-postgres --ignore-not-found -o jsonpath={{.spec.replicas}}",
        *(database if postgres == "0" else []),
        # the others judged by their pods, whatever the count says (review RV42A-01)
        f"{ns} get pods -l app.kubernetes.io/name=seamless-postgres {OTHER_DATABASES} -o name",
        f"{ns} scale deployment/seamless --replicas=1",
        f"{ns} rollout status deployment/seamless --timeout=600s",
    ]


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
@pytest.mark.parametrize("postgres", [2, 3])
def test_a_cluster_start_brings_postgresql_back_to_one_database_before_the_control_plane(
    tmp_path, platform, postgres
):
    """SDD §17.2 (review RV38B-15): each PostgreSQL replica is a database of its own (a PVC each)
    behind the one headless Service the control plane connects to. Scaled out of band, start
    brings it back to one, waits until the others are gone - rollout status does not wait for a
    scale-down - and only then starts the control plane, whose connections reach one database;
    it names the replicas it found and the PVCs it kept. It acted at 0 replicas only, and started
    the control plane against every database."""
    proc, _, _ = _run(
        tmp_path, platform, "start", cluster=fake_kube.cluster(replicas=0, postgres=postgres)
    )
    state = _cluster_after(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert state["statefulset"]["replicas"] == 1
    assert [p["name"] for p in state["statefulset"]["pods"]] == ["seamless-postgres-0"]
    [pod] = fake_kube.running(state)
    assert pod["databases"] == ["seamless-postgres-0"]
    kept = [f"data-seamless-postgres-{i}" for i in range(1, postgres)]
    assert f"PostgreSQL ran {postgres} replicas" in proc.stderr, proc.stderr
    assert all(pvc in proc.stderr for pvc in kept), proc.stderr


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
@pytest.mark.parametrize("action", ["start", "up"])
def test_postgresql_extra_pods_still_there_are_awaited_before_the_control_plane(
    tmp_path, platform, action
):
    """SDD §17.2 (reviews RV42A-01, RV42A-02): PostgreSQL's replica count already back at one -
    scaled down by hand, or a start run again after its wait gave up - with the extra pods still
    terminating, each a database of its own behind the one Service: start and up judge by the
    pods and wait them out before the control plane starts, naming them. start waited only for a
    count above one; up never waited."""
    state = fake_kube.cluster(replicas=0, postgres=3)
    state["statefulset"]["replicas"] = 1
    for pod in state["statefulset"]["pods"][1:]:
        pod["terminating"] = True
    proc, _, _ = _run(tmp_path, platform, action, cluster=state)
    after = _cluster_after(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert [p["name"] for p in after["statefulset"]["pods"]] == ["seamless-postgres-0"]
    [pod] = fake_kube.running(after)
    assert pod["databases"] == ["seamless-postgres-0"]
    assert "PostgreSQL ran 3 replicas" in proc.stderr, proc.stderr
    assert "data-seamless-postgres-1 data-seamless-postgres-2" in proc.stderr


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
def test_a_cluster_up_from_postgresql_scaled_out_waits_for_the_extra_databases(tmp_path, platform):
    """SDD §17.2 (review RV42A-02): up scales PostgreSQL back to one - rollout status does not wait
    for a scale-down - and starts the control plane only once the others are gone."""
    proc, _, _ = _run(tmp_path, platform, "up", cluster=fake_kube.cluster(replicas=0, postgres=3))
    after = _cluster_after(tmp_path)
    assert proc.returncode == 0, proc.stderr
    [pod] = fake_kube.running(after)
    assert pod["databases"] == ["seamless-postgres-0"]


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
def test_a_cluster_up_on_a_running_stack_brings_postgresql_back_to_one_before_its_apply(
    tmp_path, platform
):
    """SDD §17.2 (review RV43A-01): a running control plane, PostgreSQL scaled out by hand, up
    with a new image in the overlay - the documented upgrade: the apply rolls the control plane's
    pod at once, so PostgreSQL is back to one, its others gone, before the apply. The pod the
    apply starts finds one database."""
    state = fake_kube.cluster(replicas=1, postgres=3)
    state["apply_rolls"] = True
    proc, calls, _ = _run(tmp_path, platform, "up", cluster=state)
    after = _cluster_after(tmp_path)
    assert proc.returncode == 0, proc.stderr
    [pod] = fake_kube.running(after)
    assert pod["databases"] == ["seamless-postgres-0"], (pod, proc.stderr)
    tool = "oc" if platform == "openshift" else "kubectl"
    lines = _args(calls, tool)
    apply = next(i for i, line in enumerate(lines) if " apply -k " in line)
    assert "-n seamless-migrate scale statefulset/seamless-postgres --replicas=1" in lines[:apply]
    assert "PostgreSQL ran 3 replicas" in proc.stderr, proc.stderr


@pytest.mark.parametrize(("platform", "tool", "overlay"), CLUSTERS)
def test_a_cluster_stop_and_scale_zero_leave_postgresql_running(tmp_path, platform, tool, overlay):
    """SDD §17.2: stop - and scale 0 - end the control plane alone; PostgreSQL keeps its replica."""
    for argv in (["stop"], ["scale", "0"]):
        proc, calls, _ = _run(tmp_path / argv[-1], platform, *argv)
        assert proc.returncode == 0, proc.stderr
        assert _args(calls, tool) == [
            "-n seamless-migrate scale deployment/seamless --replicas=0",
            f"-n seamless-migrate get pods -l app.kubernetes.io/name=seamless {ACTIVE} -o name",
        ]


@pytest.mark.parametrize(("platform", "tool", "overlay"), CLUSTERS)
def test_a_cluster_restart_rolls_the_deployment_and_waits(tmp_path, platform, tool, overlay):
    """SDD §17.2: restart is a rollout restart of the Deployment - Recreate ends the old pod before
    the new one starts, so one orchestrator at a time -, awaited; it asks first whether the
    control plane is stopped (review RV38B-04)."""
    proc, calls, _ = _run(tmp_path, platform, "restart")
    assert proc.returncode == 0, proc.stderr
    assert _args(calls, tool) == [
        "-n seamless-migrate get deployment/seamless -o jsonpath={.spec.replicas}",
        "-n seamless-migrate rollout restart deployment/seamless",
        "-n seamless-migrate rollout status deployment/seamless --timeout=600s",
    ]


@pytest.mark.parametrize(("platform", "tool", "overlay"), CLUSTERS)
def test_cluster_capacity_patches_the_configmap_and_sets_the_limits_in_one_rollout(
    tmp_path, platform, tool, overlay
):
    """SDD §17.2: capacity scales the one control plane up or down - its concurrency settings in the
    ConfigMap, its CPU and memory limits - in a single rollout, awaited: the Deployment and the
    settings read first - a mistyped namespace or an expired login shows before anything changes
    (review RV43A-06) -, the limits checked by a server-side dry run, then the ConfigMap - the
    settings it changes read again, to be put back on a failure -, then one patch of the pod
    template - the limits and a restart annotation (reviews RV38B-01, RV38B-02, RV41A-06)."""
    proc, calls, _ = _run(
        tmp_path,
        platform,
        "capacity",
        "--max-migrations",
        "20",
        "--max-cutovers",
        "4",
        "--cpu",
        "8",
        "--memory",
        "8Gi",
    )
    assert proc.returncode == 0, proc.stderr
    ns = ["-n", "seamless-migrate"]
    data = {"SEAMLESS_MAX_CONCURRENT_MIGRATIONS": "20", "SEAMLESS_MAX_CONCURRENT_CUTOVERS": "4"}
    made = [c["args"] for c in calls if c["cmd"] == tool]
    patches = [
        args[args.index("-p") + 1] for args in made if args[2:4] == ["patch", "deployment/seamless"]
    ]
    read = [*ns, "get", "configmap/seamless-config", "-o"]
    # its writes dry-run on the server first - the settings' patch and the template's (RV45A-02)
    settings = [
        *ns,
        "patch",
        "configmap/seamless-config",
        "--type",
        "merge",
        "-p",
        _compact({"data": data}),
    ]
    dry = ["--dry-run=server", "-o", "name"]
    assert made == [
        [*ns, "get", "deployment/seamless", "-o", "name"],
        [*read, 'go-template={{index .data "SEAMLESS_MAX_CONCURRENT_MIGRATIONS"}}'],
        [*read, 'go-template={{index .data "SEAMLESS_MAX_CONCURRENT_CUTOVERS"}}'],
        [*settings, *dry],
        [*ns, "patch", "deployment/seamless", "-p", patches[0], *dry],
        [*read, 'go-template={{index .data "SEAMLESS_MAX_CONCURRENT_MIGRATIONS"}}'],
        [*read, 'go-template={{index .data "SEAMLESS_MAX_CONCURRENT_CUTOVERS"}}'],
        settings,
        [*ns, "patch", "deployment/seamless", "-p", patches[1]],
        [*ns, "rollout", "status", "deployment/seamless", "--timeout=600s"],
    ]
    assert patches[0] == patches[1]
    template = json.loads(patches[1])["spec"]["template"]
    limits = {"cpu": "8", "memory": "8Gi"}
    assert template["spec"] == {
        "containers": [{"name": "seamless", "resources": {"limits": limits}}]
    }
    [(annotation, at)] = template["metadata"]["annotations"].items()
    assert annotation == "kubectl.kubernetes.io/restartedAt"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", at)
    # settings alone: no limit, the restart annotation alone - the pod reads the ConfigMap; both
    # writes dry-run on the server first (RV45A-02)
    proc, calls, _ = _run(tmp_path / "settings", platform, "capacity", "--max-cutovers", "2")
    assert proc.returncode == 0, proc.stderr
    made = [c["args"] for c in calls if c["cmd"] == tool]
    assert [args[2:4] + (["dry"] if "--dry-run=server" in args else []) for args in made] == [
        ["get", "deployment/seamless"],
        ["get", "configmap/seamless-config"],
        ["patch", "configmap/seamless-config", "dry"],
        ["patch", "deployment/seamless", "dry"],
        ["get", "configmap/seamless-config"],
        ["patch", "configmap/seamless-config"],
        ["patch", "deployment/seamless"],
        ["rollout", "status"],
    ]
    assert list(json.loads(made[6][-1])["spec"]["template"]) == ["metadata"]


@pytest.mark.parametrize(("platform", "tool", "overlay"), CLUSTERS)
def test_cluster_capacity_warns_when_more_migrations_than_its_memory_limit_was_sized_for(
    tmp_path, platform, tool, overlay
):
    """SDD §17.2, Operations.md §4.6 (review 38b): each concurrent migration needs about 250 MiB,
    and the manifests' 4Gi limit is sized for 10 - like the single host, the script says so when
    more are asked for without --memory, and goes on; with --memory, or 10 at most, it says
    nothing."""
    proc, calls, _ = _run(tmp_path, platform, "capacity", "--max-migrations", "20")
    assert proc.returncode == 0, proc.stderr
    assert "about 250 MiB" in proc.stderr and "deploy/openshift/deployment.yaml" in proc.stderr
    assert "--memory" in proc.stderr
    assert any(
        args.startswith("-n seamless-migrate patch deployment/seamless")
        for args in _args(calls, tool)
    )
    for argv in (["--max-migrations", "20", "--memory", "8Gi"], ["--max-migrations", "10"]):
        proc, _, _ = _run(tmp_path / argv[-1], platform, "capacity", *argv)
        assert proc.returncode == 0 and "250 MiB" not in proc.stderr, (argv, proc.stderr)


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
def test_a_cluster_down_waits_for_the_running_pod_only_then_stops_postgresql(tmp_path, platform):
    """SDD §17.2 (review RV38B-03): a pod evicted days before (phase Failed) stays until the pod
    GC - the scale-down never deletes it -, so down waits for the running pod alone, then stops
    PostgreSQL; it waited for the evicted one until its timeout and never stopped PostgreSQL."""
    proc, _, _ = _run(tmp_path, platform, "down", cluster=fake_kube.cluster(evicted=True))
    state = _cluster_after(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert fake_kube.running(state) == [] and state["statefulset"]["replicas"] == 0
    assert fake_kube.running(state, "statefulset") == []


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
def test_a_cluster_restart_of_a_stopped_control_plane_starts_it(tmp_path, platform):
    """SDD §17.2 (review RV38B-04): after stop, restart starts the control plane - one pod - as the
    single host's restart creates and starts it; it rolled an empty Deployment and reported
    success with nothing running. A running one restarts as before: one new pod."""
    proc, _, _ = _run(tmp_path, platform, "restart", cluster=fake_kube.cluster(replicas=0))
    assert proc.returncode == 0, proc.stderr
    assert len(fake_kube.running(_cluster_after(tmp_path))) == 1
    proc, _, _ = _run(tmp_path / "running", platform, "restart", cluster=fake_kube.cluster())
    [pod] = fake_kube.running(_cluster_after(tmp_path / "running"))
    assert proc.returncode == 0 and pod["name"] != "seamless-1", proc.stderr


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
@pytest.mark.parametrize(
    "argv",
    [
        ["--max-migrations", "30", "--memory", "4Gi"],
        ["--max-migrations", "30", "--cpu", "4", "--memory", "4Gi"],
        ["--max-migrations", "30"],
        ["--max-migrations", "30", "--memory", "8Gi"],
    ],
    ids=["the same memory", "the same limits", "settings alone", "a new limit"],
)
def test_cluster_capacity_restarts_the_pod_once_with_the_new_settings(tmp_path, platform, argv):
    """SDD §17.2 (review RV38B-01): capacity restarts the control plane once, and its new pod
    reads the new settings - also when the limits stay as they are, where `set resources`
    patched nothing and no pod ever read them."""
    proc, _, _ = _run(tmp_path, platform, "capacity", *argv, cluster=fake_kube.cluster())
    state = _cluster_after(tmp_path)
    assert proc.returncode == 0, proc.stderr
    [pod] = fake_kube.running(state)
    assert pod["config"]["SEAMLESS_MAX_CONCURRENT_MIGRATIONS"] == "30"
    assert state["serial"] == 2  # one new pod: a single restart
    if "--memory" in argv:
        assert pod["limits"]["memory"] == argv[argv.index("--memory") + 1]


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
def test_a_capacity_limit_the_api_server_refuses_changes_nothing(tmp_path, platform):
    """SDD §17.2 (review RV38B-02): a limit below the manifests' requests (1Gi) is refused by the
    API server - checked by a server-side dry run before anything changes, so the ConfigMap keeps
    its values for the next pod start and the running pod keeps running."""
    proc, _, _ = _run(
        tmp_path,
        platform,
        "capacity",
        "--max-migrations",
        "20",
        "--memory",
        "512Mi",
        cluster=fake_kube.cluster(),
    )
    state = _cluster_after(tmp_path)
    assert proc.returncode != 0
    assert state["configmap"]["SEAMLESS_MAX_CONCURRENT_MIGRATIONS"] == "10"
    assert [p["name"] for p in fake_kube.running(state)] == ["seamless-1"]


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
@pytest.mark.parametrize(
    "argv",
    [["--max-migrations", "20"], ["--max-migrations", "20", "--memory", "8Gi"]],
    ids=["settings alone", "with a limit"],
)
def test_a_capacity_whose_template_patch_fails_puts_the_settings_back(tmp_path, platform, argv):
    """SDD §17.2 (review RV41A-06): the template patch failing after the ConfigMap's - the API
    server restarting - left the new concurrency in the ConfigMap, unsaid, for the next pod start
    to take with the old limits. The settings the ConfigMap held are put back, the running pod
    runs on, and the script exits 1 saying so."""
    state = fake_kube.cluster()
    state["unavailable"] = True
    proc, _, _ = _run(tmp_path, platform, "capacity", *argv, cluster=state)
    after = _cluster_after(tmp_path)
    assert proc.returncode == 1, proc.stderr
    assert "put back" in proc.stderr, proc.stderr
    assert after["configmap"]["SEAMLESS_MAX_CONCURRENT_MIGRATIONS"] == "10"
    assert [p["name"] for p in fake_kube.running(after)] == ["seamless-1"]


def test_a_failed_capacity_removes_a_setting_the_configmap_lacked(tmp_path):
    """SDD §17.2 (review RV41A-06): putting the settings back removes a key the ConfigMap did not
    hold before - the manifests' default applies again, as before the change."""
    state = fake_kube.cluster()
    state["unavailable"] = True
    del state["configmap"]["SEAMLESS_MAX_CONCURRENT_CUTOVERS"]
    proc, _, _ = _run(tmp_path, "kubernetes", "capacity", "--max-cutovers", "5", cluster=state)
    assert proc.returncode == 1, proc.stderr
    assert _cluster_after(tmp_path)["configmap"] == {"SEAMLESS_MAX_CONCURRENT_MIGRATIONS": "10"}


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
def test_a_capacity_limit_a_limitrange_refuses_is_rolled_back(tmp_path, platform):
    """SDD §17.2 (review RV41A-07): a LimitRange judges the pod, not the Deployment - the dry run
    passed, Recreate ended the running pod, the new pod was refused, and the control plane stayed
    down until someone patched the limit back. The failed rollout is undone - the previous
    template, the settings the ConfigMap held - so the control plane runs again as it was, and the
    script exits 1 naming the refusal."""
    state = fake_kube.cluster()
    state["limitrange"] = {"memory": "4Gi"}
    argv = ["capacity", "--max-migrations", "20", "--memory", "8Gi"]
    proc, calls, _ = _run(tmp_path, platform, *argv, cluster=state)
    after = _cluster_after(tmp_path)
    assert proc.returncode == 1, proc.stderr
    assert "LimitRange" in proc.stderr, proc.stderr
    [pod] = fake_kube.running(after)
    assert pod["limits"]["memory"] == "4Gi"
    assert pod["config"]["SEAMLESS_MAX_CONCURRENT_MIGRATIONS"] == "10"
    # the undo is awaited by its pod - the Progressing condition keeps the passed deadline after an
    # undo, so a rollout status on it failed at once - and no second undo is advised (RV42A-07)
    assert "put back as they were" in proc.stderr, proc.stderr
    assert "could not be undone" not in proc.stderr
    tool = "oc" if platform == "openshift" else "kubectl"
    assert f"{tool} -n seamless-migrate describe replicaset" in proc.stderr, proc.stderr


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
def test_a_refused_rollout_is_undone_also_when_the_settings_cannot_be_put_back(tmp_path, platform):
    """SDD §17.2 (review RV42A-03): the settings' put-back failing (an admission webhook timing
    out) died before the undo - the refused template stayed and no control plane ran. The undo
    runs all the same, and the message names what could not be put back."""
    state = fake_kube.cluster()
    state["limitrange"] = {"memory": "4Gi"}
    state["fail_configmap_patch"] = 2  # the put-back, after the capacity's own patch
    argv = ["capacity", "--max-migrations", "20", "--memory", "8Gi"]
    proc, _, _ = _run(tmp_path, platform, *argv, cluster=state)
    after = _cluster_after(tmp_path)
    assert proc.returncode == 1, proc.stderr
    [pod] = fake_kube.running(after)
    assert pod["limits"]["memory"] == "4Gi"  # the template rolled back, the control plane running
    assert "seamless-config could not be put back" in proc.stderr, proc.stderr
    assert "SEAMLESS_MAX_CONCURRENT_MIGRATIONS" in proc.stderr  # what to set back
    # the pod the undo started read the new settings: the restart they then need is named (RV43A-03)
    assert pod["config"]["SEAMLESS_MAX_CONCURRENT_MIGRATIONS"] == "20"
    assert f"then scripts/seamless-ctl.sh {platform} restart" in proc.stderr, proc.stderr


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
def test_a_done_undo_whose_pod_is_not_available_says_the_template_is_back(tmp_path, platform):
    """SDD §17.2 (review RV43A-02): a LimitRange added since the last rollout refuses the running
    template's pod too. The undo is done - the template before this capacity is back - but its pod
    is not Available within --timeout. That is said as such, with where to look; no revision is
    advised: after an undo the newest revision is the restored one, so "the revision before this
    capacity" would be the refused template - a second undo."""
    state = fake_kube.cluster()  # limits 4 CPU / 4Gi
    state["limitrange"] = {"memory": "2Gi"}  # added after the last rollout: the old pod ran
    proc, calls, _ = _run(tmp_path, platform, "capacity", "--max-migrations", "20", cluster=state)
    after = _cluster_after(tmp_path)
    tool = "oc" if platform == "openshift" else "kubectl"
    assert proc.returncode == 1, proc.stderr
    assert "-n seamless-migrate rollout undo deployment/seamless" in _args(calls, tool)
    assert after["deployment"]["annotations"] == {}  # the template before this capacity
    assert after["deployment"]["limits"] == {"cpu": "4", "memory": "4Gi"}
    assert "the template is back as it was" in proc.stderr, proc.stderr
    assert f"{tool} -n seamless-migrate describe replicaset" in proc.stderr, proc.stderr
    assert "was not rolled back" not in proc.stderr and "--to-revision" not in proc.stderr


def test_a_timeout_of_zero_is_refused(tmp_path):
    """SDD §17.2 (review RV43A-02): --timeout takes at least one second - kubectl wait reads 0 as a
    single check, rollout status as no bound at all, so 0 meant neither."""
    argv = ["capacity", "--max-migrations", "20", "--timeout", "0"]
    proc, calls, _ = _run(tmp_path, "kubernetes", *argv)
    assert proc.returncode == 2 and calls == [], proc.stderr
    assert "--timeout takes at least 1 second" in proc.stderr, proc.stderr


DRY_RUN_CLUSTER_ACTIONS = [
    ["start"],
    ["restart"],
    ["up"],
    ["down"],
    ["stop"],
    ["scale", "0"],
    ["scale", "1"],
    ["status", "--url", CLUSTER_URL],
    ["logs"],
    ["capacity", "--max-migrations", "20", "--memory", "8Gi"],
    ["stop", "--drain", "@TOKEN_FILE", "--url", CLUSTER_URL],
    ["capacity", "--max-migrations", "20", "--drain", "@TOKEN_FILE", "--url", CLUSTER_URL],
    ["resume", "--token-file", "@TOKEN_FILE", "--url", CLUSTER_URL, "plan-aa"],
]


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
@pytest.mark.parametrize(
    "argv", DRY_RUN_CLUSTER_ACTIONS, ids=[" ".join(a[:2]) for a in DRY_RUN_CLUSTER_ACTIONS]
)
def test_a_cluster_dry_run_calls_no_tool_and_changes_nothing(tmp_path, platform, argv):
    """SDD §17.2 (reviews RV38B-17, RV42A-08, RV43A-08): --dry-run prints a cluster action's
    commands and runs none - start's dry run, rewritten for PostgreSQL's extra pods, read the
    cluster or scaled it for real in mutants no test noticed, and a status calling kubectl directly
    passed every test. Every cluster action, a drain's and resume's API calls too: no tool runs,
    and a stopped stack with PostgreSQL scaled out stays as it was."""
    state = fake_kube.cluster(replicas=0, postgres=2)
    proc, calls, _ = _run(tmp_path, platform, *argv, "--dry-run", cluster=state)
    assert proc.returncode == 0, proc.stderr
    assert calls == []
    assert _cluster_after(tmp_path) == state
    assert [line for line in proc.stdout.splitlines() if line.startswith("+ ")], proc.stdout


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
def test_a_cluster_capacity_dry_run_changes_nothing(tmp_path, platform):
    """SDD §17.2 (reviews RV38B-17, RV41A-08): --dry-run prints every command of a cluster's
    capacity - the server-side dry run, the ConfigMap's patch, the template's, the rollout - and
    runs none: no kubectl or oc call, the ConfigMap and the pod as they were."""
    argv = ["capacity", "--max-migrations", "20", "--memory", "8Gi", "--dry-run"]
    proc, calls, _ = _run(tmp_path, platform, *argv, cluster=fake_kube.cluster())
    state = _cluster_after(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert calls == []
    assert state["configmap"]["SEAMLESS_MAX_CONCURRENT_MIGRATIONS"] == "10"
    assert state["serial"] == 1 and state["deployment"]["limits"]["memory"] == "4Gi"
    printed = [line for line in proc.stdout.splitlines() if line.startswith("+ ")]
    assert [
        next(w for w in ("--dry-run=server", "configmap", "rollout", "patch") if w in line)
        for line in printed
    ] == [
        "--dry-run=server",  # the settings' patch (RV45A-02)
        "--dry-run=server",  # the template's
        "configmap",
        "patch",
        "rollout",
    ], printed


def test_the_documented_dry_run_of_a_cluster_down_is_what_the_script_prints():
    """Operations.md §2 shows `kubernetes down --dry-run`'s output: it is what the script prints
    (review 38b, a check of its own made a test)."""
    ops = (find_repo_root() / "docs" / "Operations.md").read_text(encoding="utf-8")
    block = ops[ops.index("scripts/seamless-ctl.sh kubernetes down --dry-run") :]
    documented = [line for line in block.split("```", 1)[0].splitlines() if line.startswith("+ ")]
    out = subprocess.run(
        ["bash", str(SCRIPT), "kubernetes", "down", "--dry-run"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert out == documented


ENV = "".join(
    [
        "POSTGRES_PASSWORD=x\n",
        "SEAMLESS_MAX_CONCURRENT_MIGRATIONS=10\n",
        "# a comment\n",
        "SEAMLESS_HOST_PORT=8080\n",
    ]
)


def test_single_host_capacity_writes_the_env_file_and_restarts_the_control_plane(tmp_path):
    """SDD §17.2: on the single host the concurrency settings go into deploy/compose/.env - a line
    replaced, a missing one added, every other line and the file's 0600 mode kept -, then a
    restart."""
    proc, calls, tree = _run(
        tmp_path,
        "podman",
        "capacity",
        "--max-migrations",
        "12",
        "--max-cutovers",
        "5",
        env_file=ENV,
    )
    assert proc.returncode == 0, proc.stderr
    env = tree / "deploy" / "compose" / ".env"
    assert env.read_text() == (
        "POSTGRES_PASSWORD=x\nSEAMLESS_MAX_CONCURRENT_MIGRATIONS=12\n# a comment\n"
        "SEAMLESS_HOST_PORT=8080\nSEAMLESS_MAX_CONCURRENT_CUTOVERS=5\n"
    )
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert _args(calls, "make") == [
        f"-C {tree} --no-print-directory seamless-restart SEAMLESS_ENGINE=podman"
    ]
    assert "about 250 MiB" in proc.stderr  # 12 slots: past what the 3g limit was sized for


@pytest.mark.parametrize(
    "argv",
    [
        ["--max-migrations", "0"],
        ["--max-migrations", "-3"],
        ["--max-cutovers", "two"],
        [],
        ["--cpu", "4"],  # the single host's limits live in compose.yaml
    ],
)
def test_single_host_capacity_refuses_a_bad_value_before_changing_anything(tmp_path, argv):
    """SDD §15.1, §17.2: each concurrency setting is at least 1; nothing to change, or a CPU or
    memory limit on the single host (compose.yaml holds those), is refused before the file is
    touched."""
    proc, calls, tree = _run(tmp_path, "docker", "capacity", *argv, env_file=ENV)
    assert proc.returncode == 2, proc.stderr
    assert (tree / "deploy" / "compose" / ".env").read_text() == ENV and calls == []


def test_dry_run_prints_every_command_and_runs_none(tmp_path):
    """SDD §17.2: --dry-run shows what an action would run and runs nothing."""
    proc, calls, tree = _run(tmp_path, "kubernetes", "up", "--dry-run")
    assert proc.returncode == 0, proc.stderr and calls == []
    assert calls == []
    assert f"kubectl -n seamless-migrate apply -k {tree / 'deploy/kubernetes'}" in proc.stdout
    assert (
        "kubectl -n seamless-migrate rollout status deployment/seamless --timeout=600s"
        in proc.stdout
    )
    proc, calls, tree = _run(tmp_path / "docker", "docker", "restart", "--dry-run")
    assert proc.returncode == 0 and calls == []
    assert (
        f"make -C {tree} --no-print-directory seamless-restart SEAMLESS_ENGINE=docker"
        in proc.stdout
    )
    # a capacity rewrites no .env, a drain pauses no plan (review RV38B-17)
    proc, calls, tree = _run(
        tmp_path / "capacity",
        "docker",
        "capacity",
        "--max-migrations",
        "12",
        "--dry-run",
        env_file=ENV,
    )
    assert proc.returncode == 0 and calls == [], proc.stderr
    assert (tree / "deploy" / "compose" / ".env").read_text() == ENV
    assert "SEAMLESS_MAX_CONCURRENT_MIGRATIONS=12" in proc.stdout
    proc, calls, _ = _run(
        tmp_path / "drain",
        "docker",
        "stop",
        "--drain",
        "@TOKEN_FILE",
        "--dry-run",
        rules=_drain_rules([]),
    )
    assert proc.returncode == 0 and calls == [], proc.stderr
    assert "/plans?status=running" in proc.stdout and "then wait" in proc.stdout
    assert "/api/v1/me" in proc.stdout  # the token's role, asked first (review RV44A-02)


API = "http://127.0.0.1:8080/api/v1"
RUNNING = json.dumps(
    [{"id": "plan-aa", "status": "running"}, {"id": "plan-bb", "status": "running"}]
)


def _drain_rules(cutover_answers):
    return [
        {"cmd": "curl", "has": ["/plans?status=running"], "answers": [{"stdout": RUNNING}]},
        {"cmd": "curl", "has": ["phase=cutover"], "answers": cutover_answers},
        {"cmd": "curl", "has": ["phase=verifying"], "answers": [{"stdout": "[]"}]},
        {"cmd": "curl", "has": ["phase=rolling_back"], "answers": [{"stdout": "[]"}]},
    ]


@pytest.mark.parametrize(
    ("dotenv", "value"),
    [
        ("KEY=1.2.3\n", "1.2.3"),
        ("export KEY=1.2.3\n", "1.2.3"),
        ("  KEY = 1.2.3  \n", "1.2.3"),
        ('KEY="1.2.3" # pinned\n', "1.2.3"),
        ("KEY='1.2.3' # pinned\n", "1.2.3"),
        ("KEY=1.2.3 # pinned\n", "1.2.3"),
        ("KEY=1.2.3\t# pinned\n", "1.2.3\t# pinned"),
        ("KEY=a#b\n", "a#b"),
        ("KEY=1\nKEY=2\n", "2"),
        ("KEYS=1\nOTHER=2\n", ""),
        ("# KEY=1\n", ""),
        ("KEY=1.2.3\r\n", "1.2.3"),
        ("KEY=\n", ""),
        ("KEY: 1.2.3\n", "1.2.3"),
        ("KEY : 1.2.3\n", "1.2.3"),
        ("KEY: a=b\n", "a=b"),
        ("KEY=a:b\n", "a:b"),
        ("\ufeffKEY=1.2.3\n", "1.2.3"),
        ('KEY="1.2\\"3"\n', '1.2"3'),
        ("KEY='${PORT}'\n", "${PORT}"),
    ],
    ids=[
        "plain",
        "an export prefix",
        "spaces around",
        "double quotes and a comment",
        "single quotes and a comment",
        "an inline comment",
        "a tab before a hash is no comment",
        "a hash inside a value",
        "the last definition",
        "another key",
        "a commented line",
        "CRLF",
        "empty",
        "a YAML-style colon",
        "spaces around the colon",
        "an equals sign in a colon's value",
        "a colon in a value",
        "a byte-order mark",
        "an escaped quote",
        "single quotes keep a dollar",
    ],
)
@pytest.mark.parametrize("locale", ["C", "en_US.UTF-8"])
def test_the_dotenv_helper_reads_a_key_as_compose_does(tmp_path, dotenv, value, locale):
    """SDD §17.2 (reviews RV38B-07, RV38B-09, RV41A-01, RV41A-03): scripts/dotenv-value.sh reads
    deploy/compose/.env by compose-go's dotenv rules - a byte-order mark ignored, an export
    prefix, a key ended by '=' or ':', spaces around the key and the value, quotes, an inline
    comment after ' #' in an unquoted value (a tab before a '#' is part of the value), the last
    definition winning - in any locale (each case checked against compose-go v2.16.1's own
    parser)."""
    path = tmp_path / ".env"
    path.write_text(dotenv, encoding="utf-8")
    done = subprocess.run(
        ["sh", str(DOTENV), "KEY", str(path)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "LC_ALL": locale},
    )
    assert done.stdout == (value + "\n" if value else "")


@pytest.mark.parametrize(
    "dotenv",
    [
        "PORT=18081\nKEY=${PORT}\n",
        'KEY="${PORT}"\n',
        "KEY=$PORT\n",
        "KEY=a$$b\n",
        'KEY="unterminated\n',
    ],
    ids=[
        "a braced variable",
        "in double quotes",
        "a bare variable",
        "an escaped dollar",
        "an open quote",
    ],
)
def test_the_dotenv_helper_refuses_a_value_compose_would_interpolate(tmp_path, dotenv):
    """SDD §17.2 (review RV41A-02): Compose interpolates an unquoted or double-quoted value from the
    environment and the keys above, and refuses a quote never closed; the helper refuses either -
    a message naming the key, exit 3, nothing printed - so no caller guesses a port or an image."""
    path = tmp_path / ".env"
    path.write_text(dotenv)
    done = subprocess.run(["sh", str(DOTENV), "KEY", str(path)], capture_output=True, text=True)
    assert (done.returncode, done.stdout) == (3, ""), done
    assert "KEY" in done.stderr and str(path) in done.stderr


@pytest.mark.parametrize(
    ("dotenv", "status"),
    [
        ("SEAMLESS_DEMO=\n", 0),
        ("export SEAMLESS_DEMO=false\n", 0),
        (" SEAMLESS_DEMO = true\n", 0),
        ("SEAMLESS_DEMO: true\n", 0),
        ("SEAMLESS_DEMO=${X}\n", 0),
        ("SEAMLESS_DEMO_SPEED=2\n", 1),
        ("# SEAMLESS_DEMO=true\n", 1),
        (None, 1),
    ],
)
def test_the_dotenv_helper_tells_whether_a_key_is_set(tmp_path, dotenv, status):
    """SDD §17.2 (review RV41A-04): --defined exits 0 when the file sets the key - an empty value or
    one Compose would interpolate too -, 1 when it does not or there is no file."""
    path = tmp_path / ".env"
    if dotenv is not None:
        path.write_text(dotenv)
    done = subprocess.run(
        ["sh", str(DOTENV), "--defined", "SEAMLESS_DEMO", str(path)], capture_output=True, text=True
    )
    assert (done.returncode, done.stdout) == (status, ""), done


def test_status_and_drain_ask_the_port_the_stack_publishes(tmp_path):
    """SDD §17.2 (review RV38B-07): with SEAMLESS_HOST_PORT moved in deploy/compose/.env, Compose
    publishes that port - status and a drain ask it, so the operator's token never goes to
    whatever program holds 8080; the shell's port still wins."""
    port_env = "POSTGRES_PASSWORD=x\nexport SEAMLESS_HOST_PORT = 18081 # moved\n"
    _, status, _ = _run(
        tmp_path / "status",
        "docker",
        "status",
        env_file=port_env,
        rules=[{"cmd": "curl", "answers": [{"stdout": "{}"}]}],
    )
    rules = [
        {"cmd": "curl", "has": ["/plans?status=running"], "answers": [{"stdout": "[]"}]},
        {"cmd": "curl", "has": ["phase="], "answers": [{"stdout": "[]"}]},
    ]
    proc, drain, _ = _run(
        tmp_path / "drain",
        "docker",
        "stop",
        "--drain",
        "@TOKEN_FILE",
        env_file=port_env,
        rules=rules,
    )
    assert proc.returncode == 0, proc.stderr
    asked = [c["args"][-1] for c in status + drain if c["cmd"] == "curl"]
    assert asked and all(url.startswith("http://127.0.0.1:18081/") for url in asked), asked


@pytest.mark.parametrize(
    "port_env",
    ["POSTGRES_PASSWORD=x\nSEAMLESS_HOST_PORT: 18081\n", "\ufeffSEAMLESS_HOST_PORT=18081\nX=1\n"],
    ids=["a YAML-style colon", "a byte-order mark before the key"],
)
def test_a_drain_asks_the_port_compose_reads_in_every_spelling(tmp_path, port_env):
    """SDD §17.2 (review RV41A-01): compose-go ends a key at ':' too and drops a byte-order mark -
    Compose publishes 18081 -, so the drain sends the operator's token there, never to 8080."""
    rules = [
        {"cmd": "curl", "has": ["/plans?status=running"], "answers": [{"stdout": "[]"}]},
        {"cmd": "curl", "has": ["phase="], "answers": [{"stdout": "[]"}]},
    ]
    proc, calls, _ = _run(
        tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", env_file=port_env, rules=rules
    )
    assert proc.returncode == 0, proc.stderr
    asked = [c["args"][-1] for c in calls if c["cmd"] == "curl"]
    assert asked and all(url.startswith("http://127.0.0.1:18081/") for url in asked), asked


@pytest.mark.parametrize("argv", [["status"], ["stop", "--drain", "@TOKEN_FILE"]])
def test_a_port_compose_would_interpolate_asks_no_api(tmp_path, argv):
    """SDD §17.2 (review RV41A-02): with SEAMLESS_HOST_PORT=${PORT} the script cannot tell the port
    Compose publishes - it refuses, never guessing 8080 or asking a URL built from the raw text,
    and runs nothing; --url still names the API."""
    port_env = "PORT=18081\nSEAMLESS_HOST_PORT=${PORT}\n"
    proc, calls, _ = _run(tmp_path, "docker", *argv, env_file=port_env, rules=_drain_rules([]))
    assert proc.returncode == 2, proc.stderr  # a refusal: nothing ran (RV43A-07)
    assert "SEAMLESS_HOST_PORT" in proc.stderr and calls == [], (proc.stderr, calls)
    rules = [*_drain_rules([{"stdout": "[]"}]), {"cmd": "curl", "answers": [{"stdout": "{}"}]}]
    proc, calls, _ = _run(
        tmp_path / "url", "docker", *argv, "--url", "http://127.0.0.1:18081",
        env_file=port_env, rules=rules,
    )  # fmt: skip
    asked = [c["args"][-1] for c in calls if c["cmd"] == "curl"]
    assert asked and all(url.startswith("http://127.0.0.1:18081/") for url in asked), asked


def test_a_drain_waits_while_a_migration_verifies_or_rolls_back_too(tmp_path):
    """SDD §17.2 (review RV38B-16): a drain waits until no migration is in cutover, verifying or
    rolling_back - each of them asked again until it is clear, not cutover alone."""
    busy = [{"stdout": '[{"id": "m-1"}]'}, {"stdout": "[]"}]
    rules = [
        {"cmd": "curl", "has": ["/plans?status=running"], "answers": [{"stdout": RUNNING}]},
        {"cmd": "curl", "has": ["phase=cutover"], "answers": [{"stdout": "[]"}]},
        {"cmd": "curl", "has": ["phase=verifying"], "answers": busy},
        {"cmd": "curl", "has": ["phase=rolling_back"], "answers": busy},
    ]
    proc, calls, _ = _run(tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", rules=rules)
    assert proc.returncode == 0, proc.stderr
    asked = [
        phase
        for c in calls
        if c["cmd"] == "curl"
        for phase in ("cutover", "verifying", "rolling_back")
        if f"phase={phase}&" in c["args"][-1]
    ]
    assert asked == ["cutover", "verifying", "rolling_back"] * 2
    assert "waiting: a migration is in verifying rolling_back" in proc.stdout + proc.stderr
    assert [c for c in calls if c["cmd"] == "make"], "stopped once all three were clear"


@pytest.mark.parametrize("status", ["404", "409"])
def test_a_drain_whose_pause_fails_names_the_plans_it_already_paused(tmp_path, status):
    """SDD §17.2 (reviews RV38B-05, RV43A-04): --drain prints the plans it paused - also when it
    stops half-way: plan-bb's pause was refused (it completed meanwhile, 409) after plan-aa's
    succeeded, and plan-aa, paused for good, is named with the resume command, as a drain that
    times out names them; it died naming none, and a second drain lists running plans only. The
    API refused plan-bb's pause - 409, or 404 for a plan gone -, so it was not paused, and is not
    named: resume would start a plan that failed meanwhile again (review RV44A-02: the status, not
    curl -f's 22 for any error)."""
    rules = [
        {"cmd": "curl", "has": ["plan-bb/pause"], "answers": [{"stdout": status}]},
        *_drain_rules([{"stdout": "[]"}]),
    ]
    proc, calls, _ = _run(tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", rules=rules)
    paused = [c for c in calls if c["cmd"] == "curl" and c["args"][-1].endswith("plan-aa/pause")]
    assert proc.returncode == 1 and paused
    assert f"the API refused to pause plan plan-bb (HTTP {status})" in proc.stderr, proc.stderr
    assert "may be paused" not in proc.stderr, proc.stderr
    assert f"resume --token-file {tmp_path / 'operator.token'} plan-aa\n" in proc.stderr
    assert not [a for a in _args(calls, "make") if "seamless-stop" in a]  # nothing was stopped


@pytest.mark.parametrize(
    ("rules", "paused"),
    [
        (
            [
                {"cmd": "curl", "has": ["/plans?status=running"], "answers": [{"stdout": RUNNING}]},
                {"cmd": "curl", "has": ["phase=cutover"], "answers": [{"exit": 22}]},
            ],
            ["plan-aa", "plan-bb"],
        ),
        (
            [
                {
                    "cmd": "curl",
                    "has": ["/plans?status=running"],
                    "answers": [
                        {
                            "stdout": json.dumps(
                                [
                                    {"id": "plan-aa", "status": "running"},
                                    {"id": "../plan-bb", "status": "running"},
                                ]
                            )
                        }
                    ],
                },
            ],
            ["plan-aa"],
        ),
    ],
    ids=["the migrations list fails during the wait", "a later plan id is refused"],
)
def test_a_drain_that_fails_after_a_pause_names_the_plans_it_paused(tmp_path, rules, paused):
    """SDD §17.2 (reviews RV38B-05, RV41A-05): every way a drain fails after a pause stops
    nothing and names the plans it paused, with the resume command - it died naming none, the
    plans paused until someone noticed."""
    proc, calls, _ = _run(tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", rules=rules)
    assert proc.returncode == 1, proc.stderr
    assert "nothing was stopped" in proc.stderr, proc.stderr
    assert f"resume --token-file {tmp_path / 'operator.token'} {' '.join(paused)}" in proc.stderr
    assert not [a for a in _args(calls, "make") if "seamless-stop" in a]  # nothing was stopped


@pytest.mark.parametrize(
    "argv",
    [["capacity", "--max-migrations", "20"], ["restart"]],
    ids=["capacity", "restart"],
)
def test_a_drain_pauses_no_plan_for_a_restart_its_make_target_refuses(tmp_path, argv):
    """SDD §17.2 (review RV42A-04): the single host's restart - capacity's last step - waits on
    SEAMLESS_HOST_PORT, which its Make target refuses when Compose would interpolate it; --url
    gets past the script's own port check, so the drain paused every plan and the restart then
    stopped. The port is checked before the drain: nothing is paused or changed."""
    port_env = "POSTGRES_PASSWORD=x\nPORT=18081\nSEAMLESS_HOST_PORT=${PORT}\n"
    proc, calls, tree = _run(
        tmp_path, "docker", *argv, "--drain", "@TOKEN_FILE", "--url", "http://127.0.0.1:18081",
        env_file=port_env, rules=_drain_rules([{"stdout": "[]"}]),
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr  # a refusal: nothing ran (RV43A-07)
    assert "SEAMLESS_HOST_PORT" in proc.stderr and "nothing was changed" in proc.stderr, proc.stderr
    assert calls == []  # no plan listed or paused, no make
    assert (tree / "deploy" / "compose" / ".env").read_text() == port_env
    # the shell's own port is the one Make takes, as Compose does: nothing to refuse
    proc, calls, _ = _run(
        tmp_path / "shell", "docker", *argv, "--drain", "@TOKEN_FILE", env_file=port_env,
        rules=_drain_rules([{"stdout": "[]"}]), environment={"SEAMLESS_HOST_PORT": "18081"},
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert [c["cmd"] for c in calls][-1] == "make"


CAPACITY_OR_RESTART = pytest.mark.parametrize(
    "argv", [["capacity", "--max-migrations", "20"], ["restart"]], ids=["capacity", "restart"]
)


def _paused(calls) -> list[str]:
    return [c["args"][-1] for c in calls if c["cmd"] == "curl" and "/pause" in c["args"][-1]]


@CAPACITY_OR_RESTART
@pytest.mark.parametrize("url", [False, True], ids=["by the port", "with --url"])
def test_a_port_compose_would_refuse_is_refused_before_the_drain(tmp_path, argv, url):
    """SDD §17.2 (review RV43A-05): the restart's Compose publishes SEAMLESS_HOST_PORT - a value
    that is no number it refuses (0.0.0.0:8081 gives "too many colons"). That is refused before the
    drain, --url or not - the URL the drain uses changes nothing for Compose -, and never advises
    --url for restart and capacity; nothing is paused or changed, exit 2."""
    port_env = "POSTGRES_PASSWORD=x\nSEAMLESS_HOST_PORT=0.0.0.0:8081\n"
    proc, calls, tree = _run(
        tmp_path, "docker", *argv, "--drain", "@TOKEN_FILE",
        *(["--url", "http://127.0.0.1:8080"] if url else []),
        env_file=port_env, rules=_drain_rules([{"stdout": "[]"}]),
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    assert calls == [], proc.stderr  # no plan listed or paused, no make
    assert "SEAMLESS_HOST_PORT is '0.0.0.0:8081'" in proc.stderr, proc.stderr
    assert "or --url" not in proc.stderr, proc.stderr
    assert (tree / "deploy" / "compose" / ".env").read_text() == port_env


@CAPACITY_OR_RESTART
@pytest.mark.parametrize("missing", [".env", "tokens.yaml"])
def test_the_files_the_restart_needs_are_checked_before_the_drain(tmp_path, argv, missing):
    """SDD §17.2 (review RV43A-06): the restart's Make target refuses a missing deploy/compose/.env
    or tokens.yaml (seamless-check-env); both are checked before the drain - the drain paused every
    plan and capacity rewrote .env before make refused. Nothing is paused, exit 2."""
    env = "POSTGRES_PASSWORD=x\nSEAMLESS_HOST_PORT=8080\n"
    proc, calls, _ = _run(
        tmp_path, "docker", *argv, "--drain", "@TOKEN_FILE",
        env_file=None if missing == ".env" else env, tokens_yaml=missing != "tokens.yaml",
        rules=_drain_rules([{"stdout": "[]"}]),
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    assert not _paused(calls) and calls == [], (calls, proc.stderr)
    assert f"deploy/compose/{missing} is missing" in proc.stderr, proc.stderr


@CAPACITY_OR_RESTART
def test_make_missing_is_found_before_the_drain(tmp_path, monkeypatch, argv):
    """SDD §17.2 (review RV43A-06): make itself was asked for only after the drain."""
    keep = tmp_path / "keep"
    keep.mkdir()
    os.symlink(os.path.realpath(sys.executable), keep / "python3")
    for tool in ("sh", "bash", "awk", "find", "mktemp", "date", "sed", "grep", "mv", "rm", "cat",
                 "env", "dirname", "basename", "tr", "sleep", "chmod"):  # fmt: skip
        found = shutil.which(tool)
        assert found, tool
        os.symlink(found, keep / tool)
    monkeypatch.setattr(sys.modules[__name__], "TOOLS", tuple(t for t in TOOLS if t != "make"))
    env = "POSTGRES_PASSWORD=x\nSEAMLESS_HOST_PORT=8080\n"
    proc, calls, _ = _run(
        tmp_path, "docker", *argv, "--drain", "@TOKEN_FILE", env_file=env, path=str(keep),
        rules=_drain_rules([{"stdout": "[]"}]),
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    assert not _paused(calls), proc.stderr
    assert "make is needed" in proc.stderr, proc.stderr


@pytest.mark.parametrize(("platform", "tool"), [("kubernetes", "kubectl"), ("openshift", "oc")])
def test_a_cluster_capacity_reads_the_cluster_before_its_drain(tmp_path, platform, tool):
    """SDD §17.2 (review RV43A-06): a capacity without --cpu or --memory has no dry run, so it asked
    the cluster nothing before its drain - a mistyped namespace (the drain itself goes to --url) or
    an expired login showed only after every plan was paused. It reads the Deployment and the
    ConfigMap first: nothing is paused, exit 2."""
    rules = [
        {"cmd": "curl", "has": ["/plans?status=running"], "answers": [{"stdout": RUNNING}]},
        {"cmd": "curl", "has": ["phase="], "answers": [{"stdout": "[]"}]},
        {"cmd": tool, "answers": [{"exit": 1}]},  # Error from server (NotFound): namespaces ...
    ]
    proc, calls, _ = _run(
        tmp_path, platform, "capacity", "--max-migrations", "20", "--drain", "@TOKEN_FILE",
        "--url", CLUSTER_URL, "--namespace", "seamles-migrate", rules=rules,
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    assert not _paused(calls) and not [c for c in calls if c["cmd"] == "curl"], proc.stderr
    assert "cannot read deployment/seamless in namespace seamles-migrate" in proc.stderr


def test_a_failed_make_target_after_a_drain_exits_1_naming_it(tmp_path):
    """SDD §17.2 (review RV43A-07): exit 2 means a refusal, nothing ran. A restart that fails in
    its Make target after the drain paused every plan and capacity rewrote .env is a failed step:
    exit 1, naming the target, not make's own 2."""
    env = "POSTGRES_PASSWORD=x\nSEAMLESS_HOST_PORT=8080\n"
    restart = {"cmd": "make", "has": ["seamless-restart"], "answers": [{"exit": 2}]}
    rules = [*_drain_rules([{"stdout": "[]"}]), restart]
    proc, _, tree = _run(
        tmp_path, "docker", "capacity", "--max-migrations", "20", "--drain", "@TOKEN_FILE",
        env_file=env, rules=rules,
    )  # fmt: skip
    assert "SEAMLESS_MAX_CONCURRENT_MIGRATIONS=20" in (tree / "deploy/compose/.env").read_text()
    assert proc.returncode == 1, (proc.returncode, proc.stderr)
    assert "make seamless-restart failed" in proc.stderr, proc.stderr


def test_a_pause_that_got_no_answer_is_named_for_resume(tmp_path):
    """SDD §17.2 (review RV42A-06): a pause the API applied while its answer never came - curl's
    --max-time, a Route's timeout - leaves the plan paused; it is named for resume with the plans
    paused before it. It was named only once its answer came."""
    rules = [
        {"cmd": "curl", "has": ["/plans?status=running"], "answers": [{"stdout": RUNNING}]},
        {"cmd": "curl", "has": ["plan-bb/pause"], "answers": [{"exit": 28}]},
    ]
    proc, calls, _ = _run(tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", rules=rules)
    assert proc.returncode == 1, proc.stderr
    assert "cannot pause plan plan-bb" in proc.stderr and "may be paused" in proc.stderr
    assert f"resume --token-file {tmp_path / 'operator.token'} plan-aa plan-bb" in proc.stderr
    assert not [a for a in _args(calls, "make") if "seamless-stop" in a]  # nothing was stopped


def test_an_interrupted_drain_names_the_plans_it_paused(tmp_path):
    """SDD §17.2 (review RV41A-05): an interrupt (Ctrl-C) while the drain waits for a cutover stops
    nothing and names the paused plans with the resume command."""
    rules = _drain_rules([{"stdout": '[{"id": "m-1"}]'}])
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, tree / "scripts" / SCRIPT.name)
    shutil.copy(DOTENV, tree / "scripts" / DOTENV.name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in TOOLS:
        (bin_dir / name).write_text(STUB)
        (bin_dir / name).chmod(0o755)
    token_file = tmp_path / "operator.token"
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(0o600)
    (tmp_path / "rules.json").write_text(json.dumps(rules))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SEAMLESS_", "DOCKER_"))}
    env.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        STUB_LOG=str(tmp_path / "calls.log"),
        STUB_RULES=str(tmp_path / "rules.json"),
        TMPDIR=str(tmp_path),
    )
    argv = ["bash", str(tree / "scripts" / SCRIPT.name), "docker", "stop", "--drain"]
    proc = subprocess.Popen(
        [*argv, str(token_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
        # SIGINT as Ctrl-C leaves it: a shell that runs this suite as a background job starts its
        # children with SIGINT ignored, which bash can then neither trap nor reset - the drain
        # waited on, never interrupted
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )
    assert proc.stdout is not None
    for line in proc.stdout:  # the drain waits for the cutover in flight
        if line.startswith("waiting:"):
            break
    os.killpg(proc.pid, signal.SIGINT)  # what Ctrl-C sends: the script and its sleep
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 1, err
    assert "nothing was stopped" in err and "resume" in err and "plan-aa plan-bb" in err, err
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert not [c for c in calls if "seamless-stop" in c]  # its checks ran, the stop never


@pytest.mark.parametrize("timeout", ["08", "09"])
def test_a_drain_reads_a_leading_zero_wait_timeout_as_decimal_seconds(tmp_path, timeout):
    """SDD §17.2 (review RV38B-06): --wait-timeout takes seconds, 08 being eight. Bash reads a
    leading zero as octal in $(( )), where 08 and 09 are errors: the drain ended after its pauses,
    before its wait, and the script stopped the control plane without asking whether a cutover
    was in flight."""
    rules = _drain_rules([{"stdout": "[]"}])
    proc, calls, _ = _run(
        tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", "--wait-timeout", timeout, rules=rules
    )
    assert proc.returncode == 0, proc.stderr
    order = [
        "ask" if c["cmd"] == "curl" else "stop"
        for c in calls
        if "seamless-stop" in c["args"] or "phase=cutover" in " ".join(c["args"])
    ]
    assert order == ["ask", "stop"], (order, proc.stderr)


def test_a_drain_pauses_the_running_plans_and_waits_until_no_cutover_is_in_flight(tmp_path):
    """SDD §17.2, §13.1: --drain pauses every running plan, waits until no migration is in cutover,
    verifying or rolling_back, then stops; the token reaches curl only through a header file in a
    private directory, gone afterwards, and the paused plans are named for resume."""
    rules = _drain_rules([{"stdout": '[{"id": "m-1"}]'}, {"stdout": "[]"}])
    proc, calls, tree = _run(tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", rules=rules)
    assert proc.returncode == 0, proc.stderr
    curl = _args(calls, "curl")
    header = [a for c in calls if c["cmd"] == "curl" for a in c["args"] if a.startswith("@")]
    # the token's role first, its status read (reviews RV44A-02, RV45A-07), then the running plans
    assert curl[:2] == [
        f"-sS --max-time 30 -H {header[0]} -w \\n%{{http_code}} {API}/me",
        f"-fsS --max-time 30 -H {header[0]} {API}/plans?status=running",
    ]
    # each pause's HTTP status read, never curl -f's 22 for any error (review RV44A-02)
    pause = f"-sS --max-time 30 -X POST -H {header[0]} -o /dev/null -w %{{http_code}} {API}/plans"
    assert f"{pause}/plan-aa/pause" in curl and f"{pause}/plan-bb/pause" in curl
    assert sum("phase=cutover" in a for a in curl) == 2  # waited once
    assert [c["cmd"] for c in calls][-1] == "make" and _args(calls, "make")[-1].endswith(
        "seamless-stop SEAMLESS_ENGINE=docker"
    )
    assert all(
        c["headers"] == [f"Authorization: Bearer {TOKEN}\n"] for c in calls if c["cmd"] == "curl"
    )
    assert not any(TOKEN in " ".join(c["args"]) for c in calls)
    assert not Path(header[0][1:]).exists()  # the private directory went with the script
    assert "plan-aa" in proc.stdout and "plan-bb" in proc.stdout and "resume" in proc.stdout


def test_a_drain_that_times_out_stops_nothing(tmp_path):
    """SDD §17.2: a cutover still in flight when --wait-timeout ends stops nothing; the paused plans
    are named so they can be resumed."""
    rules = _drain_rules([{"stdout": '[{"id": "m-1"}]'}])
    proc, calls, _ = _run(
        tmp_path, "kubernetes", "down", "--drain", "@TOKEN_FILE", "--url", "https://seamless.example.com",
        "--wait-timeout", "0", rules=rules,
    )  # fmt: skip
    assert proc.returncode == 1
    # read, and its writes dry-run, before the drain (reviews RV44A-01, RV45A-02); nothing after it
    dry = "--replicas=0 --dry-run=server -o name"
    assert _args(calls, "kubectl") == [
        "-n seamless-migrate get deployment/seamless -o name",
        "-n seamless-migrate get statefulset/seamless-postgres -o name",
        f"-n seamless-migrate scale deployment/seamless {dry}",
        f"-n seamless-migrate scale statefulset/seamless-postgres {dry}",
    ]
    assert "nothing was stopped" in proc.stderr
    assert "plan-aa" in proc.stderr and "resume" in proc.stderr


def _changes(calls) -> list[int]:
    """The places of the calls that change the stack: make - never its read-only checks -, a
    cluster's patch, scale or rollout - never a server-side dry run."""
    return [
        i
        for i, c in enumerate(calls)
        if (c["cmd"] == "make" and "seamless-check-context" not in c["args"])
        or (
            c["cmd"] in ("kubectl", "oc")
            and {"patch", "scale", "rollout", "set"} & set(c["args"])
            and "--dry-run=server" not in c["args"]
        )
    ]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_capacity_takes_drain_and_waits_out_the_cutovers_before_its_restart(tmp_path, platform):
    """SDD §17.2 (review RV38B-22): capacity ends in a restart - make seamless-restart, a cluster's
    new pod template -, so it takes --drain as restart does: the running plans are paused and the
    cutover in flight ends before anything changes. It refused --drain. A cluster's limits are
    checked by a server-side dry run before the drain and again after it."""
    rules = _drain_rules([{"stdout": '[{"id": "m-1"}]'}, {"stdout": "[]"}])
    cluster = platform in ("kubernetes", "openshift")
    proc, calls, tree = _run(
        tmp_path, platform, "capacity", "--max-migrations", "12", "--drain", "@TOKEN_FILE",
        *(["--url", CLUSTER_URL, "--memory", "8Gi"] if cluster else []),
        rules=rules, env_file=None if cluster else ENV,
        cluster=fake_kube.cluster() if cluster else None,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    waits = [i for i, c in enumerate(calls) if c["cmd"] == "curl" and "phase=" in c["args"][-1]]
    assert len(waits) == 6  # cutover, verifying, rolling_back - asked twice: it waited once
    assert waits[-1] < _changes(calls)[0], [c["cmd"] for c in calls]
    if cluster:
        dry = [i for i, c in enumerate(calls) if "--dry-run=server" in c["args"]]
        assert dry[0] < waits[0] and waits[-1] < dry[-1] < _changes(calls)[0], dry
        [pod] = fake_kube.running(_cluster_after(tmp_path))
        assert pod["config"]["SEAMLESS_MAX_CONCURRENT_MIGRATIONS"] == "12"
        assert pod["limits"]["memory"] == "8Gi"
    else:
        env = (tree / "deploy" / "compose" / ".env").read_text()
        assert "SEAMLESS_MAX_CONCURRENT_MIGRATIONS=12" in env
        assert _args(calls, "make")[-1].endswith("seamless-restart SEAMLESS_ENGINE=" + platform)
    assert "plan-aa" in proc.stdout and "resume" in proc.stdout


@pytest.mark.parametrize("platform", PLATFORMS)
def test_a_capacity_drain_that_times_out_changes_nothing(tmp_path, platform):
    """SDD §17.2: a cutover still in flight when --wait-timeout ends stops and changes nothing -
    no .env rewritten, no ConfigMap or pod template patched - and the paused plans are named."""
    rules = _drain_rules([{"stdout": '[{"id": "m-1"}]'}])
    cluster = platform in ("kubernetes", "openshift")
    proc, calls, tree = _run(
        tmp_path, platform, "capacity", "--max-migrations", "12", "--drain", "@TOKEN_FILE",
        "--wait-timeout", "0", *(["--url", CLUSTER_URL] if cluster else []),
        rules=rules, env_file=None if cluster else ENV,
        cluster=fake_kube.cluster() if cluster else None,
    )  # fmt: skip
    assert proc.returncode == 1, proc.stderr
    assert "nothing was stopped" in proc.stderr and "plan-aa" in proc.stderr
    assert _changes(calls) == []
    if cluster:
        state = _cluster_after(tmp_path)
        assert state["configmap"]["SEAMLESS_MAX_CONCURRENT_MIGRATIONS"] == "10"
        assert state["serial"] == 1
    else:
        assert (tree / "deploy" / "compose" / ".env").read_text() == ENV


#: the API server's refusal of a limit below the request (fake_kube.py)
BELOW_REQUEST = "must be less than or equal to"


@pytest.mark.parametrize(
    ("platform", "argv", "says"),
    [
        ("docker", [], "deploy/compose/.env is missing"),
        ("kubernetes", ["--memory", "512Mi", "--url", CLUSTER_URL], BELOW_REQUEST),
        ("openshift", ["--memory", "512Mi", "--url", CLUSTER_URL], BELOW_REQUEST),
    ],
    ids=["no .env", "kubernetes: a refused limit", "openshift: a refused limit"],
)
def test_a_capacity_that_would_be_refused_pauses_no_plan(tmp_path, platform, argv, says):
    """SDD §17.2 (review RV38B-22): a capacity's checks - deploy/compose/.env on the single host,
    the server-side dry run of a cluster's limits - run before its drain: a capacity that is
    refused leaves every plan running, with nothing to resume."""
    proc, calls, _ = _run(
        tmp_path, platform, "capacity", "--max-migrations", "12", *argv, "--drain", "@TOKEN_FILE",
        rules=_drain_rules([{"stdout": "[]"}]),
        cluster=fake_kube.cluster() if platform != "docker" else None,
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr  # a refusal: nothing ran (RV43A-07)
    assert says in proc.stderr, proc.stderr
    assert [c for c in calls if c["cmd"] == "curl"] == []  # no plan listed, none paused
    assert _changes(calls) == []


@pytest.mark.parametrize(("mode", "used"), [(0o644, False), (0o600, True)], ids=["0644", "0600"])
def test_a_symlinked_token_file_is_judged_by_the_file_it_points_to(tmp_path, mode, used):
    """SDD §17.2 (review RV38B-08): the token file must be one only its owner may read - the file a
    symlink points to, never the link's own mode: macOS gives a link the umask's mode (0700 under
    077), which let a link to a world-readable token through, and Linux 0777, which refused a link
    to a 0600 one."""
    target = tmp_path / "shared" / "operator.token"
    target.parent.mkdir()
    target.write_text(TOKEN + "\n")
    target.chmod(mode)
    link = tmp_path / "operator-link.token"
    old = os.umask(0o077)
    try:
        os.symlink(target, link)
    finally:
        os.umask(old)
    rules = [
        {"cmd": "curl", "has": ["/plans?status=running"], "answers": [{"stdout": "[]"}]},
        {"cmd": "curl", "has": ["phase="], "answers": [{"stdout": "[]"}]},
    ]
    proc, calls, _ = _run(tmp_path, "docker", "stop", "--drain", str(link), rules=rules)
    sent = [c for c in calls if c["cmd"] == "curl"]
    if used:
        assert proc.returncode == 0 and sent, proc.stderr
    else:
        assert proc.returncode == 2 and not sent, proc.stderr
        assert "may be read by others" in proc.stderr


def test_a_token_file_named_like_an_option_is_judged_by_its_mode(tmp_path):
    """SDD §17.2, §13.1 (review RV41A-11): a token file named `-tok` made find fail - its error
    hidden, its empty output read as "only its owner may read it" - and a 0644 token was sent; it
    is judged as ./-tok, and refused."""
    (tmp_path / "-tok").write_text(TOKEN + "\n")
    (tmp_path / "-tok").chmod(0o644)
    proc, calls, _ = _run(tmp_path / "run", "docker", "stop", "--drain", "-tok", cwd=tmp_path)
    assert proc.returncode == 2 and "may be read by others" in proc.stderr, proc.stderr
    assert calls == []


def test_a_token_file_find_cannot_judge_is_refused(tmp_path):
    """SDD §17.2, §13.1 (review RV41A-11): the mode check fails closed - when find cannot tell who
    may read the token file, the file is refused, never taken as private."""
    failing_find = "#!/bin/sh\necho 'find: cannot judge' >&2\nexit 1\n"
    proc, calls, _ = _run(
        tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", tools={"find": failing_find}
    )
    assert proc.returncode == 2 and "cannot tell who may read" in proc.stderr, proc.stderr
    assert calls == []


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604])
def test_a_token_file_others_may_read_is_refused(tmp_path, mode):
    """SDD §17.2, §13.1: the operator's token file must be readable by its owner alone."""
    proc, calls, _ = _run(tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", token_mode=mode)
    assert proc.returncode == 2 and "chmod 600" in proc.stderr and calls == []


def test_a_drain_refuses_xtrace_which_would_print_the_token(tmp_path):
    """SDD §13.1: under xtrace (bash -x) the shell prints every expanded word, the token among
    them."""
    proc, calls, _ = _run(tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", xtrace=True)
    assert proc.returncode == 2 and "xtrace" in proc.stderr and calls == []
    assert TOKEN not in proc.stderr


def test_a_cluster_drain_needs_the_api_url(tmp_path):
    """SDD §17.2: on a cluster the API is the Route's or Ingress's URL, which --url names."""
    proc, calls, _ = _run(tmp_path, "openshift", "stop", "--drain", "@TOKEN_FILE")
    assert proc.returncode == 2 and "--url" in proc.stderr and calls == []


def test_resume_starts_the_plans_a_drain_paused(tmp_path):
    """SDD §17.2: resume starts each named plan again (POST /plans/{id}/start)."""
    proc, calls, _ = _run(
        tmp_path, "podman", "resume", "--token-file", "@TOKEN_FILE", "plan-aa", "plan-bb"
    )
    assert proc.returncode == 0, proc.stderr
    header = [a for c in calls for a in c["args"] if a.startswith("@")][0]
    assert _args(calls, "curl") == [
        f"-fsS --max-time 30 -X POST -H {header} -o /dev/null {API}/plans/plan-aa/start",
        f"-fsS --max-time 30 -X POST -H {header} -o /dev/null {API}/plans/plan-bb/start",
    ]
    proc, calls, _ = _run(
        tmp_path / "bad", "podman", "resume", "--token-file", "@TOKEN_FILE", "../x"
    )
    assert proc.returncode == 2 and calls == []


def test_resume_starts_every_plan_and_names_the_ones_start_refuses(tmp_path):
    """SDD §17.2 (review RV38B-14): resume tries every plan it is given - one Start refuses (409:
    cancelled meanwhile, or a migration blocked) is named, the others start all the same - and
    exits 1. It stopped at the first refusal: the plans after it stayed paused, unnamed."""
    rules = [{"cmd": "curl", "has": ["plan-bb/start"], "answers": [{"exit": 22}]}]
    plans = ["plan-aa", "plan-bb", "plan-cc"]
    proc, calls, _ = _run(
        tmp_path, "podman", "resume", "--token-file", "@TOKEN_FILE", *plans, rules=rules
    )
    tried = [c["args"][-1].rsplit("/", 2)[-2] for c in calls if c["cmd"] == "curl"]
    assert tried == plans, (tried, proc.stderr)
    assert proc.returncode == 1
    assert "started: plan-aa" in proc.stdout and "started: plan-cc" in proc.stdout
    assert "started: plan-bb" not in proc.stdout
    assert "cannot start plan plan-bb" in proc.stderr, proc.stderr
    assert "plan-aa" not in proc.stderr and "plan-cc" not in proc.stderr


@pytest.mark.parametrize("argv", [["dance"], ["up", "--bogus"], [], ["status", "--namespace"]])
def test_an_unknown_action_or_option_is_refused_with_the_usage(tmp_path, argv):
    """SDD §17.2: a mistyped action or option runs nothing and says how to call the script."""
    proc, calls, _ = _run(tmp_path, "kubernetes", *argv)
    assert proc.returncode == 2 and "usage:" in proc.stderr.lower() and calls == []
    proc, calls, _ = _run(tmp_path / "platform", "nomad", "up")
    assert proc.returncode == 2 and calls == []


#: a docker whose existing control-plane container ran with SEAMLESS_DEMO=$CONTAINER_DEMO; it logs
#: each call with the SEAMLESS_DEMO in its environment (None: not set)
DOCKER_STUB = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "SEAMLESS_DEMO": os.environ.get("SEAMLESS_DEMO")}) + "\n")
if "version" in args and "--short" in args:
    print("2.29.0")
elif "ps" in args and "-q" in args:
    print("c0ffee")
elif args[-1:] == ["c0ffee"] and "inspect" in args:
    print("PATH=/usr/bin\nSEAMLESS_DEMO=" + os.environ["CONTAINER_DEMO"])
"""


def _restart(tmp_path, env_file: str, container_demo: str, **environment: str):
    """Run the real `make seamless-restart` of a copy of the Makefile against DOCKER_STUB; return
    the SEAMLESS_DEMO its `compose up` ran with (None: not in its environment, so Compose reads
    .env)."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "deploy" / "compose").mkdir(parents=True)
    shutil.copy(ROOT / "Makefile", tree / "Makefile")
    shutil.copy(DOTENV, tree / "scripts" / DOTENV.name)
    for name in ("compose.yaml", "compose.linux-host.yaml"):
        shutil.copy(ROOT / "deploy" / "compose" / name, tree / "deploy" / "compose" / name)
    (tree / "deploy" / "compose" / ".env").write_text(env_file, encoding="utf-8")
    (tree / "deploy" / "compose" / "tokens.yaml").write_text("tokens: []\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER_STUB)
    (bin_dir / "curl").write_text("#!/bin/sh\nexit 0\n")
    for tool in ("docker", "curl"):
        (bin_dir / tool).chmod(0o755)
    log = tmp_path / "docker.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SEAMLESS_", "DOCKER_"))}
    env.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        STUB_LOG=str(log),
        CONTAINER_DEMO=container_demo,
        **environment,
    )
    argv = ["make", "-C", str(tree), "--no-print-directory", "seamless-restart"]
    proc = subprocess.run(
        [*argv, "SEAMLESS_HOST_OS=Linux", "SEAMLESS_DOCKER_CONTEXT=default"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    up = [c for c in calls if "up" in c["args"] and "--force-recreate" in c["args"]]
    assert proc.returncode == 0 and len(up) == 1, (proc.stdout, proc.stderr, calls)
    return up[0]["SEAMLESS_DEMO"]


@pytest.mark.parametrize(
    ("env_file", "container", "environment", "handed"),
    [
        ("POSTGRES_PASSWORD=x\n", "true", {}, "true"),
        ("POSTGRES_PASSWORD=x\n", "false", {}, "false"),
        ("POSTGRES_PASSWORD=x\nSEAMLESS_DEMO=false\n", "true", {}, None),
        ("POSTGRES_PASSWORD=x\nexport SEAMLESS_DEMO=false\n", "true", {}, None),
        ("POSTGRES_PASSWORD=x\nSEAMLESS_DEMO: false\n", "true", {}, None),
        ("POSTGRES_PASSWORD=x\nSEAMLESS_DEMO=\n", "true", {}, None),
        ("POSTGRES_PASSWORD=x\n", "true", {"SEAMLESS_DEMO": "false"}, "false"),
    ],
    ids=[
        "a demo container comes back a demo",
        "a real one comes back real",
        "a .env line wins over the container",
        "an export prefix too",
        "a YAML-style colon too",
        "an empty value too",
        "the environment wins",
    ],
)
def test_a_restart_hands_compose_the_mode_its_sources_give(
    tmp_path, env_file, container, environment, handed
):
    """SDD §17.2 (reviews RV38B-18, RV41A-04, RV41A-08): make seamless-restart keeps the mode -
    SEAMLESS_DEMO from the environment, else .env's, which Compose reads itself (whether .env sets
    it is read by Compose's rules: an export prefix or a colon too), else the one the existing
    container ran with, handed to compose. A grep missed `export SEAMLESS_DEMO=false` and forced
    the container's demo mode over it."""
    assert _restart(tmp_path, env_file, container, **environment) == handed


def test_the_make_targets_stop_start_and_restart_the_control_plane_alone():
    """SDD §17.1, §17.2: seamless-stop stops the control plane's container; seamless-start starts
    it as it was, or creates it; seamless-restart recreates it from the current files, keeping its
    mode."""
    env = {**os.environ, "SEAMLESS_HOST_OS": "Linux", "SEAMLESS_DOCKER_CONTEXT": "default"}

    def dry(target, *extra):
        out = subprocess.run(
            ["make", "-n", "--no-print-directory", "-C", str(ROOT), target, *extra],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout
        return out

    stop = dry("seamless-stop")
    assert "compose -p seamless" in stop and " stop seamless" in stop
    start = dry("seamless-start")
    assert " start seamless" in start and "up -d --wait --wait-timeout 300 seamless" in start
    restart = dry("seamless-restart")
    assert "up -d --build --force-recreate --no-deps --wait --wait-timeout 300 seamless" in restart
    assert "SEAMLESS_DEMO=" in restart and ".Config.Env" in restart  # the mode the container ran in
    # and the compose command it runs is handed that mode, not only the line it echoes (RV38B-18)
    ran = next(
        line.strip()
        for line in restart.splitlines()
        if "up -d --build --force-recreate" in line and not line.strip().startswith("echo")
    )
    assert ran.startswith("env ${mode:+SEAMLESS_DEMO=$mode} "), ran
    podman = dry("seamless-restart", "SEAMLESS_ENGINE=podman")
    assert "podman compose" in podman and "--wait" not in podman.split("seamless-restart")[0]


# --- review 44a: every drain checks first, a pause's answer read by its status -------------------
CLUSTER_DRAINS = [["restart"], ["stop"], ["down"], ["scale", "0"]]


@pytest.mark.parametrize(("platform", "tool"), [("kubernetes", "kubectl"), ("openshift", "oc")])
@pytest.mark.parametrize("action", CLUSTER_DRAINS, ids=[" ".join(a) for a in CLUSTER_DRAINS])
def test_every_cluster_drain_reads_the_cluster_before_it_pauses_a_plan(
    tmp_path, platform, tool, action
):
    """SDD §17.2 (review RV44A-01): a cluster's restart, stop, down or scale 0 with --drain paused
    every running plan - the drain goes to --url - before its first kubectl or oc call, so an
    expired login or a mistyped --namespace showed only then, every plan paused. The Deployment is
    read first: every call failing as an expired login does, nothing is listed or paused, exit 2."""
    rules = [
        {"cmd": "curl", "has": ["/plans?status=running"], "answers": [{"stdout": RUNNING}]},
        {"cmd": "curl", "has": ["phase="], "answers": [{"stdout": "[]"}]},
        {"cmd": tool, "answers": [{"exit": 1}]},  # error: You must be logged in to the server
    ]
    proc, calls, _ = _run(
        tmp_path, platform, *action, "--drain", "@TOKEN_FILE", "--url", CLUSTER_URL,
        "--namespace", "seamles-migrate", rules=rules,
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    assert _args(calls, tool) == ["-n seamles-migrate get deployment/seamless -o name"]
    assert not [c for c in calls if c["cmd"] == "curl"], calls  # no plan listed or paused
    assert "cannot read deployment/seamless in namespace seamles-migrate" in proc.stderr


@pytest.mark.parametrize(("platform", "tool"), [("kubernetes", "kubectl"), ("openshift", "oc")])
def test_a_cluster_down_drain_reads_postgresql_too_before_it_pauses_a_plan(
    tmp_path, platform, tool
):
    """SDD §17.2 (review RV44A-01): down scales PostgreSQL's StatefulSet as well: one the namespace
    cannot be read for refuses the down before its drain - nothing listed or paused, exit 2."""
    rules = [
        *_drain_rules([{"stdout": "[]"}]),
        {"cmd": tool, "has": ["get statefulset"], "answers": [{"exit": 1}]},
    ]
    proc, calls, _ = _run(
        tmp_path, platform, "down", "--drain", "@TOKEN_FILE", "--url", CLUSTER_URL, rules=rules
    )
    assert proc.returncode == 2, proc.stderr
    assert not [c for c in calls if c["cmd"] == "curl"], calls
    assert "cannot read statefulset/seamless-postgres in namespace seamless-migrate" in proc.stderr


HOST_DRAINS = [
    ["stop"],
    ["scale", "0"],
    ["down"],
    ["restart"],
    ["capacity", "--max-migrations", "20"],
]


@pytest.mark.parametrize("engine", ["docker", "podman"])
@pytest.mark.parametrize("argv", HOST_DRAINS, ids=[" ".join(a[:2]) for a in HOST_DRAINS])
def test_every_single_host_drain_runs_the_make_targets_checks_before_it(tmp_path, argv, engine):
    """SDD §17.2 (review RV44A-01): the Make target a drain precedes first runs its read-only
    prerequisites - seamless-check-context (the Docker context, or Podman) and, but for down's,
    seamless-check-env (.env and tokens.yaml). A shell without the SEAMLESS_DOCKER_CONTEXT the stack
    was started with reached the API on 127.0.0.1 all the same: the drain paused every plan, then
    the target refused. They run before the drain: refused there - make's 2 -, nothing is listed
    or paused, exit 2; passed, the drain and the target follow."""
    env = "POSTGRES_PASSWORD=x\nSEAMLESS_HOST_PORT=8080\n"
    checks = "seamless-check-context" + ("" if argv == ["down"] else " seamless-check-env")
    rules = [
        *_drain_rules([{"stdout": "[]"}]),
        {"cmd": "make", "has": [checks], "answers": [{"exit": 2}]},
    ]
    proc, calls, tree = _run(
        tmp_path, engine, *argv, "--drain", "@TOKEN_FILE", env_file=env, rules=rules
    )
    assert proc.returncode == 2, proc.stderr
    assert _args(calls, "make") == [
        f"-C {tree} --no-print-directory {checks} SEAMLESS_ENGINE={engine}"
    ]
    assert [c["cmd"] for c in calls] == ["make"], calls  # no plan listed or paused
    assert f"make {checks} refused (exit 2): nothing was changed" in proc.stderr, proc.stderr
    assert (tree / "deploy" / "compose" / ".env").read_text() == env
    proc, calls, _ = _run(
        tmp_path / "passed", engine, *argv, "--drain", "@TOKEN_FILE", env_file=env,
        rules=_drain_rules([{"stdout": "[]"}]),
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    order = [c["cmd"] if c["cmd"] == "make" else "curl" for c in calls]
    assert order[0] == "make" and order[-1] == "make" and "curl" in order, order
    assert checks in _args(calls, "make")[0]


@CAPACITY_OR_RESTART
@pytest.mark.parametrize("port", ["80800", "65536", "0"])
def test_a_port_compose_cannot_publish_is_refused_before_the_drain(tmp_path, argv, port):
    """SDD §17.2 (review RV44A-06): Compose publishes a host port from 1 to 65535 - "Invalid
    hostPort: 80800" -, and 0 picks a random one the restart's health wait never asks. Any plain
    number passed the check before the drain: with --url - the old port while .env names a
    mistyped new one - every plan was paused, then the restart's Compose refused. Nothing is
    paused, exit 2."""
    env = f"POSTGRES_PASSWORD=x\nSEAMLESS_HOST_PORT={port}\n"
    rules = [*_drain_rules([{"stdout": "[]"}]), {"cmd": "make", "answers": [{"exit": 2}]}]
    proc, calls, _ = _run(
        tmp_path, "docker", *argv, "--drain", "@TOKEN_FILE", "--url", "http://127.0.0.1:8080",
        env_file=env, rules=rules,
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    assert calls == [], (calls, proc.stderr)
    assert f"SEAMLESS_HOST_PORT is '{port}'" in proc.stderr and "1 to 65535" in proc.stderr
    assert "integer expression" not in proc.stderr, proc.stderr  # judged, not a test's own error


@pytest.mark.parametrize("port", ["80800", "0"])
def test_a_drain_refuses_a_port_out_of_range_where_it_asks_the_api(tmp_path, port):
    """SDD §17.2 (review RV44A-06): a drain without --url asks the API at the port the stack
    publishes: one out of 1-65535 is refused there too, naming --url - curl failed on it, exit 1."""
    env = f"POSTGRES_PASSWORD=x\nSEAMLESS_HOST_PORT={port}\n"
    proc, calls, _ = _run(
        tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", env_file=env, rules=_drain_rules([])
    )
    assert proc.returncode == 2 and calls == [], proc.stderr
    assert f"SEAMLESS_HOST_PORT is '{port}'" in proc.stderr and "or --url" in proc.stderr


@pytest.mark.parametrize("status", ["500", "502", "504"])
def test_a_pause_answered_with_a_server_error_is_named_for_resume(tmp_path, status):
    """SDD §17.2 (review RV44A-02): the API commits a pause before it answers - a failure after the
    commit answers 500 for a plan paused -, and a Route answers 502 when the pod dies mid-request,
    504 on its own timeout. curl -f's 22 - any status from 400 - read as "the API refused, not
    paused" dropped such a plan from resume. The status is read: a 5xx may have paused it, and it
    is named for resume with the plans paused before it."""
    rules = [
        {"cmd": "curl", "has": ["plan-bb/pause"], "answers": [{"stdout": status}]},
        *_drain_rules([{"stdout": "[]"}]),
    ]
    proc, calls, _ = _run(tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", rules=rules)
    assert proc.returncode == 1, proc.stderr
    assert f"answered HTTP {status} to the pause of plan plan-bb" in proc.stderr, proc.stderr
    assert "may be paused" in proc.stderr
    assert f"resume --token-file {tmp_path / 'operator.token'} plan-aa plan-bb" in proc.stderr
    assert not [a for a in _args(calls, "make") if "seamless-stop" in a]  # nothing was stopped


class _PausingApi(BaseHTTPRequestHandler):
    """An operator's /me and the two running plans of RUNNING; a pause is answered with the status
    ``server.answers`` gives its plan (200 by default), applied first unless that is a 409."""

    def log_message(self, *args):  # quiet
        pass

    def _reply(self, code: int, body: str) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/api/v1/me":
            self._reply(200, json.dumps({"name": "ops", "role": "operator"}))
        elif self.path == "/api/v1/plans?status=running":
            self._reply(200, RUNNING)
        else:
            self._reply(200, "[]")

    def do_POST(self):  # noqa: N802
        plan = self.path.split("/")[-2]
        code = self.server.answers.get(plan, 200)
        if code != 409:
            self.server.paused.append(plan)
        self._reply(code, "{}")


@pytest.mark.parametrize(("status", "named"), [(409, ["plan-aa"]), (504, ["plan-aa", "plan-bb"])])
def test_the_real_curl_names_for_resume_what_a_pause_may_have_paused(tmp_path, status, named):
    """SDD §17.2 (review RV44A-02), with the real curl against a local API: a pause answered 409 -
    the plan no longer running - paused nothing and is not named; one a gateway answers 504 may
    have been applied behind it, and is named for resume with the plans paused before it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PausingApi)
    server.paused, server.answers = [], {"plan-bb": status}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real_curl = shutil.which("curl")
    try:
        proc, calls, _ = _run(
            tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE",
            "--url", f"http://127.0.0.1:{server.server_address[1]}",
            tools={"curl": f'#!/bin/sh\nexec "{real_curl}" "$@"\n'},
        )  # fmt: skip
    finally:
        server.shutdown()
        server.server_close()
    assert proc.returncode == 1, proc.stderr
    resume = re.search(r"resume --token-file \S+ --url \S+ (.*)$", proc.stderr, re.M)
    assert resume and resume.group(1).split() == named, proc.stderr
    assert server.paused == named
    assert not [a for a in _args(calls, "make") if "seamless-stop" in a]  # nothing was stopped


@pytest.mark.parametrize("role", ["viewer", "nobody"])
def test_a_drain_refuses_a_token_below_the_operator_role(tmp_path, role):
    """SDD §17.2 (review RV44A-02): GET /plans takes a viewer's token, a pause an operator's: a
    viewer's listed the running plans, then the first pause was refused (403) - and explained as a
    plan no longer running. The token's role (GET /api/v1/me) is asked before any plan is listed:
    below an operator's, nothing is listed or paused, exit 2."""
    me = {
        "cmd": "curl",
        "has": ["/api/v1/me"],
        "answers": [{"stdout": json.dumps({"role": role}) + "\n200"}],
    }
    rules = [me, *_drain_rules([{"stdout": "[]"}])]
    proc, calls, _ = _run(tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", rules=rules)
    assert proc.returncode == 2, proc.stderr
    asked = [c["args"][-1] for c in calls if c["cmd"] == "curl"]
    assert asked == [f"{API}/me"], asked
    assert f"the token's role is '{role}'" in proc.stderr and "operator" in proc.stderr
    assert not [a for a in _args(calls, "make") if "seamless-stop" in a]


#: kubectl/oc in front of the fake: PostgreSQL's StatefulSet read fails as a transient error does
FLAKY_STATEFULSET_READ = r"""#!/bin/sh
case " $* " in *" get statefulset/seamless-postgres "*)
  echo "Unable to connect to the server: net/http: TLS handshake timeout" >&2
  exit 1 ;;
esac
exec "$REAL_FAKE" "$@"
"""
#: kubectl/oc in front of the fake: rollout undo fails as an API server that is unavailable fails it
FAILING_UNDO = r"""#!/bin/sh
case " $* " in *" rollout undo "*)
  echo "error: the server is currently unable to handle the request" >&2
  exit 1 ;;
esac
exec "$REAL_FAKE" "$@"
"""


def _wrapped(tmp_path, tool: str, wrapper: str) -> dict:
    """``tools`` and ``environment`` for ``_run``: ``wrapper`` as ``tool``, the fake behind it."""
    real = tmp_path / "fake" / tool
    real.parent.mkdir()
    real.write_text(fake_kube.SCRIPT)
    real.chmod(0o755)
    return {"tools": {tool: wrapper}, "environment": {"REAL_FAKE": str(real)}}


@pytest.mark.parametrize(("platform", "tool"), [("kubernetes", "kubectl"), ("openshift", "oc")])
def test_a_failed_statefulset_read_stops_up_before_its_apply(tmp_path, platform, tool):
    """SDD §17.2 (review RV44A-04): up took every failure of PostgreSQL's StatefulSet read for a
    first up - nothing to bring back to one -, so a transient API error let the apply roll the
    control plane onto every database, and up exited 0. Only NotFound is a first up; any other
    failure is refused before the apply, nothing changed, exit 2."""
    state = fake_kube.cluster(replicas=1, postgres=3)
    state["apply_rolls"] = True
    proc, calls, _ = _run(
        tmp_path, platform, "up", cluster=state, **_wrapped(tmp_path, tool, FLAKY_STATEFULSET_READ)
    )
    assert proc.returncode == 2, proc.stderr
    assert not [c for c in calls if "apply" in c["args"]], calls
    assert _cluster_after(tmp_path) == state
    assert "cannot read statefulset/seamless-postgres in namespace seamless-migrate" in proc.stderr


@pytest.mark.parametrize(("platform", "tool"), [("kubernetes", "kubectl"), ("openshift", "oc")])
def test_a_first_up_reads_no_statefulset_and_applies_the_overlay(tmp_path, platform, tool):
    """SDD §17.2 (review RV44A-04): in a namespace the manifests were never applied to - a first up
    - PostgreSQL's StatefulSet read finds nothing: up goes on to the apply, which creates it, and
    the control plane starts against one database. No test gave that read kubectl's NotFound."""
    proc, calls, _ = _run(tmp_path, platform, "up", cluster=fake_kube.empty())
    after = _cluster_after(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert [c["args"][2] for c in calls][:2] == ["get", "apply"], _args(calls, tool)
    [pod] = fake_kube.running(after)
    assert pod["databases"] == ["seamless-postgres-0"]


@pytest.mark.parametrize(("platform", "tool"), [("kubernetes", "kubectl"), ("openshift", "oc")])
@pytest.mark.parametrize(
    ("answer", "says", "not_said"),
    [
        ({"stdout": ""}, "no StatefulSet seamless-postgres in namespace seamles-migrate: run up",
         "cannot read"),
        ({"exit": 1}, "cannot read statefulset/seamless-postgres in namespace seamles-migrate",
         "run up first"),
    ],
    ids=["not found", "a failed read"],
)  # fmt: skip
def test_a_start_without_postgresql_is_refused_before_anything_changed(
    tmp_path, platform, tool, answer, says, not_said
):
    """SDD §17.2 (review RV44A-08): start's first step reads PostgreSQL's StatefulSet; a namespace
    without it ("run up first") and a read that failed both exited 1, a failed step, though
    nothing had changed - and the failed read was said to be a missing StatefulSet. Both are
    refused, exit 2, each said as what it is."""
    rules = [{"cmd": tool, "has": ["get statefulset"], "answers": [answer]}]
    proc, calls, _ = _run(
        tmp_path, platform, "start", "--namespace", "seamles-migrate", rules=rules
    )
    assert proc.returncode == 2, proc.stderr
    assert _args(calls, tool) == [
        "-n seamles-migrate get statefulset/seamless-postgres --ignore-not-found "
        "-o jsonpath={.spec.replicas}"
    ]
    assert says in proc.stderr and not_said not in proc.stderr, proc.stderr


@pytest.mark.parametrize(("platform", "tool"), [("kubernetes", "kubectl"), ("openshift", "oc")])
def test_a_failed_put_back_after_a_failed_undo_puts_the_settings_back_before_the_rollback(
    tmp_path, platform, tool
):
    """SDD §17.2 (review RV44A-07): the settings' put-back and the undo both failing - an API
    server that is unavailable fails both -, the message named a restart for "the pod the undo
    started" - no undo started a pod - before the rollback by hand, so the refused template was
    restarted first. The settings are put back first, then the rollback, whose pod reads them; no
    restart is named."""
    state = fake_kube.cluster()
    state["limitrange"] = {"memory": "4Gi"}
    state["fail_configmap_patch"] = 2  # the put-back
    proc, _, _ = _run(
        tmp_path, platform, "capacity", "--max-migrations", "20", "--memory", "8Gi",
        cluster=state, **_wrapped(tmp_path, tool, FAILING_UNDO),
    )  # fmt: skip
    assert proc.returncode == 1, proc.stderr
    assert "the pod the undo started" not in proc.stderr, proc.stderr
    assert f"seamless-ctl.sh {platform} restart" not in proc.stderr, proc.stderr
    put_back = proc.stderr.index(
        'set {"SEAMLESS_MAX_CONCURRENT_MIGRATIONS":"10"} in it yourself first'
    )
    assert put_back < proc.stderr.index("--to-revision"), proc.stderr
    assert "the pod it starts reads the settings put back" in proc.stderr, proc.stderr


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
def test_a_failed_put_back_after_an_undo_whose_pod_is_not_available_restarts_once_it_is(
    tmp_path, platform
):
    """SDD §17.2 (review RV44A-07): the undo done but its pod not available - a LimitRange added
    since the last rollout refuses the restored template's pod too -, a restart named at once
    fails as that pod does. It is named for once the control plane's pod is available."""
    state = fake_kube.cluster()
    state["limitrange"] = {"memory": "2Gi"}
    state["fail_configmap_patch"] = 2  # the put-back
    proc, _, _ = _run(tmp_path, platform, "capacity", "--max-migrations", "20", cluster=state)
    assert proc.returncode == 1, proc.stderr
    assert "the template is back as it was" in proc.stderr, proc.stderr
    assert (
        f"then, once the control plane's pod is available, scripts/seamless-ctl.sh {platform} "
        "restart" in proc.stderr
    ), proc.stderr


@pytest.mark.parametrize("platform", ["docker", "kubernetes"])
def test_a_resume_dry_run_names_no_plan_as_started(tmp_path, platform):
    """SDD §17.2 (review RV44A-09): --dry-run prints every command and runs none - resume's dry run
    printed "started: plan-aa" for a POST it never sent."""
    url = ["--url", CLUSTER_URL] if platform == "kubernetes" else []
    proc, calls, _ = _run(
        tmp_path, platform, "resume", "--token-file", "@TOKEN_FILE", *url, "plan-aa", "--dry-run"
    )
    assert proc.returncode == 0 and calls == [], proc.stderr
    assert "/plans/plan-aa/start" in proc.stdout
    assert "started:" not in proc.stdout, proc.stdout


def _troubleshooting_row(message: str) -> str:
    """The row of docs/Operations.md §7 that explains ``message``."""
    ops = (ROOT / "docs" / "Operations.md").read_text(encoding="utf-8")
    return next(line for line in ops.splitlines() if line.startswith(f"| `{message}"))


@pytest.mark.parametrize("cause", ["a broken link", "a directory the user cannot enter"])
def test_operations_md_explains_a_token_file_it_cannot_read_by_causes_that_give_it(tmp_path, cause):
    """docs/Operations.md §7 (review RV44A-11) explained "cannot tell who may read the token
    file" by an unreadable directory or a broken link, which stop at the readability check before
    it - "cannot read the token file", a message no row explained. Each cause its row names gives
    that row's message."""
    assert cause in _troubleshooting_row("cannot read the token file"), cause
    if cause != "a broken link" and os.geteuid() == 0:
        pytest.skip("root enters any directory")
    token = tmp_path / "token"
    if cause == "a broken link":
        token.symlink_to(tmp_path / "missing")
    else:
        token = tmp_path / "locked" / "token"
        token.parent.mkdir()
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        token.parent.chmod(0o000)
    try:
        proc, calls, _ = _run(tmp_path, "docker", "stop", "--drain", str(token), "--dry-run")
    finally:
        token.parent.chmod(0o700)
    assert proc.returncode == 2 and calls == [], proc.stderr
    assert "cannot read the token file" in proc.stderr, proc.stderr


def test_operations_md_explains_who_may_read_the_token_file_by_find_failing(tmp_path):
    """docs/Operations.md §7 (review RV44A-11): "cannot tell who may read the token file" comes
    from a find that itself fails while judging the file's mode - its row says so."""
    assert "`find` itself failed" in _troubleshooting_row("cannot tell who may read the token file")
    proc, calls, _ = _run(
        tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", "--dry-run",
        tools={"find": "#!/bin/sh\necho 'find: I/O error' >&2\nexit 1\n"},
    )  # fmt: skip
    assert proc.returncode == 2 and calls == [], proc.stderr
    assert "cannot tell who may read the token file" in proc.stderr, proc.stderr


# --- review 45a: the port Compose loads, a cluster's first writes, any 4xx, the token's answers --
HOST_STOPS = {"stop": ["stop"], "down": ["down"], "scale 0": ["scale", "0"]}


@pytest.mark.parametrize("engine", ["docker", "podman"])
@pytest.mark.parametrize("action", list(HOST_STOPS))
@pytest.mark.parametrize("port", ["80800", "8080x"])
def test_a_port_compose_cannot_load_is_refused_before_a_stop_or_down_drain(
    tmp_path, engine, action, port
):
    """SDD §17.2 (review RV45A-01): every Compose command of the single host loads the whole
    project, its published port "127.0.0.1:${SEAMLESS_HOST_PORT:-8080}:8080" included ("Invalid
    hostPort: 80800") - stop, down and scale 0 too. Their drains checked no port: with --url (the
    stack still on its old port, .env naming a mistyped new one) every plan was paused, then the
    Make target's Compose refused the project. The port is checked before every drain: nothing is
    paused, exit 2."""
    env = f"POSTGRES_PASSWORD=x\nSEAMLESS_HOST_PORT={port}\n"
    rules = [
        *_drain_rules([{"stdout": "[]"}]),
        {"cmd": "make", "has": ["seamless-"], "answers": [{"exit": 0}]},
    ]
    proc, calls, _ = _run(
        tmp_path, engine, *HOST_STOPS[action], "--drain", "@TOKEN_FILE",
        "--url", "http://127.0.0.1:8080", env_file=env, rules=rules,
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    assert calls == [], calls  # no make, no plan listed or paused
    assert f"SEAMLESS_HOST_PORT is '{port}'" in proc.stderr and "1 to 65535" in proc.stderr


def test_a_down_drain_without_an_env_file_takes_composes_default_port(tmp_path):
    """SDD §17.2 (review RV45A-01): down needs no .env (seamless-down loads the project without
    it), so its port is Compose's default 8080: nothing to refuse."""
    proc, calls, _ = _run(
        tmp_path, "docker", "down", "--drain", "@TOKEN_FILE", "--url", "http://127.0.0.1:8080",
        rules=_drain_rules([{"stdout": "[]"}]),
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert _args(calls, "make")[-1].endswith("seamless-down SEAMLESS_ENGINE=docker")


#: kubectl/oc in front of the fake: the login may read the namespace (the view role), not change it
FORBIDDEN_WRITES = r"""#!/bin/sh
case " $* " in *" scale "*|*" rollout restart "*|*" patch "*)
  printf '%s %s\n' 'Error from server (Forbidden): deployments.apps "seamless" is forbidden:' \
    'User "oncall" cannot patch resource "deployments/scale" in API group "apps"' >&2
  exit 1 ;;
esac
exec "$REAL_FAKE" "$@"
"""
CLUSTER_WRITES = [
    ["stop"],
    ["scale", "0"],
    ["down"],
    ["restart"],
    ["capacity", "--max-migrations", "20"],
]


@pytest.mark.parametrize(("platform", "tool"), [("kubernetes", "kubectl"), ("openshift", "oc")])
@pytest.mark.parametrize("action", CLUSTER_WRITES, ids=[" ".join(a[:2]) for a in CLUSTER_WRITES])
def test_a_cluster_drain_refuses_a_login_that_may_read_but_not_write(
    tmp_path, platform, tool, action
):
    """SDD §17.2 (review RV45A-02): the check before a cluster drain was a read of the Deployment,
    while the action starts with a write - a scale, a rollout restart, capacity's ConfigMap and
    template patches. A login bound to the view role reads it and is refused the write: every
    plan was paused, then the write was Forbidden. The writes are dry-run on the server before
    the drain: nothing is paused or changed, exit 2."""
    state = fake_kube.cluster()
    proc, calls, _ = _run(
        tmp_path, platform, *action, "--drain", "@TOKEN_FILE", "--url", CLUSTER_URL,
        rules=_drain_rules([{"stdout": "[]"}]), cluster=state,
        **_wrapped(tmp_path, tool, FORBIDDEN_WRITES),
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    assert _cluster_after(tmp_path) == state
    assert not [c for c in calls if c["cmd"] == "curl"], calls  # no plan listed or paused
    assert "server-side dry run" in proc.stderr and "nothing was changed" in proc.stderr, (
        proc.stderr
    )


@pytest.mark.parametrize("platform", ["kubernetes", "openshift"])
@pytest.mark.parametrize("action", CLUSTER_WRITES, ids=[" ".join(a[:2]) for a in CLUSTER_WRITES])
def test_a_cluster_drains_dry_runs_change_nothing_and_its_action_follows(
    tmp_path, platform, action
):
    """SDD §17.2 (review RV45A-02): the server-side dry runs before the drain change nothing - the
    fake applies none -, and the action runs after the drain as before."""
    state = fake_kube.cluster(replicas=1, postgres=1)
    proc, calls, _ = _run(
        tmp_path, platform, *action, "--drain", "@TOKEN_FILE", "--url", CLUSTER_URL,
        rules=_drain_rules([{"stdout": "[]"}]), cluster=state,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    tool = "oc" if platform == "openshift" else "kubectl"
    first_curl = next(i for i, c in enumerate(calls) if c["cmd"] == "curl")
    dry = [i for i, c in enumerate(calls) if c["cmd"] == tool and "--dry-run=server" in c["args"]]
    assert dry and dry[0] < first_curl, _args(calls, tool)


@pytest.mark.parametrize("role", ["operator", "approver", "admin"])
def test_a_drain_takes_an_operators_token_and_any_above_it(tmp_path, role):
    """SDD §17.2 (review RV45A-03): a drain needs an operator's token or higher - an approver's and
    an admin's drain, and so does every token when authentication is off, where GET /api/v1/me
    answers admin. The role test fed only an operator's and below: a check that refused an
    approver's or an admin's token passed it."""
    me = {
        "cmd": "curl",
        "has": ["/api/v1/me"],
        "answers": [{"stdout": json.dumps({"role": role}) + "\n200"}],
    }
    proc, calls, _ = _run(
        tmp_path,
        "docker",
        "stop",
        "--drain",
        "@TOKEN_FILE",
        rules=[me, *_drain_rules([{"stdout": "[]"}])],
    )
    assert proc.returncode == 0, proc.stderr
    assert _paused(calls) and _args(calls, "make")[-1].endswith(
        "seamless-stop SEAMLESS_ENGINE=docker"
    )


@pytest.mark.parametrize(
    ("answer", "says"),
    [
        ({"stdout": "\n401"}, "does not take this token (HTTP 401)"),
        ({"stdout": "\n403"}, "does not take this token (HTTP 403)"),
        ({"stdout": "\n502"}, "HTTP 502"),
        ({"stdout": "\n000", "exit": 7}, "HTTP 000"),
    ],
    ids=["401", "403", "502", "no answer"],
)
def test_a_drain_the_api_cannot_serve_is_refused_with_2_before_any_plan_is_listed(
    tmp_path, answer, says
):
    """SDD §17.2 (review RV45A-07): a token the API does not take (GET /api/v1/me answers 401 or
    403), or an API that cannot answer, died with 1 - a failed step's status - though nothing was
    listed, paused or stopped. Both are refused: 2, saying which."""
    me = {"cmd": "curl", "has": ["/api/v1/me"], "answers": [answer]}
    proc, calls, _ = _run(
        tmp_path,
        "docker",
        "stop",
        "--drain",
        "@TOKEN_FILE",
        rules=[me, *_drain_rules([{"stdout": "[]"}])],
    )
    assert proc.returncode == 2, proc.stderr
    assert [c["args"][-1].rsplit("/", 1)[-1] for c in calls if c["cmd"] == "curl"] == ["me"]
    assert not [a for a in _args(calls, "make") if "seamless-stop" in a]
    assert says in proc.stderr, proc.stderr


def test_a_drain_whose_listing_fails_is_refused_with_2(tmp_path):
    """SDD §17.2 (review RV45A-07): a listing of the running plans that fails - before any plan is
    paused - stops the drain as a refusal, 2, as the token's question does."""
    rules = [{"cmd": "curl", "has": ["/plans?status=running"], "answers": [{"exit": 22}]}]
    proc, calls, _ = _run(tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", rules=rules)
    assert proc.returncode == 2, proc.stderr
    assert "cannot list the running plans" in proc.stderr and not _paused(calls)


@pytest.mark.parametrize("status", ["401", "403", "404", "409", "429"])
def test_a_pause_any_4xx_answers_paused_nothing_and_is_not_named(tmp_path, status):
    """SDD §17.2 (review RV45A-06): the API answers a 4xx before it applies anything - 401 or 403
    from the role check ahead of the route, 404 or 409 from the route, 429 from the lockout -, so
    the plan was not paused and is not named for resume (resume would start it again were it to
    fail meanwhile, RV43A-04); a 409 may also answer a plan still running that another writer keeps
    changing, so no cause is claimed."""
    rules = [
        {"cmd": "curl", "has": ["plan-bb/pause"], "answers": [{"stdout": status}]},
        *_drain_rules([{"stdout": "[]"}]),
    ]
    proc, calls, _ = _run(tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", rules=rules)
    assert proc.returncode == 1, proc.stderr
    assert (
        f"the API refused to pause plan plan-bb (HTTP {status}): it was not paused" in proc.stderr
    )
    assert "no longer running" not in proc.stderr and "may be paused" not in proc.stderr
    resume = re.search(r"resume --token-file \S+ (.*)$", proc.stderr, re.M)
    assert resume and resume.group(1).split() == ["plan-aa"], proc.stderr


def test_a_statefulset_recreates_the_ordinal_still_terminating_never_the_next(tmp_path):
    """fake_kube (review RV45A-09): a StatefulSet puts each pod at the slot of its ordinal in
    [0, replicas) and waits while the pod of a slot terminates (kubernetes v1.33.0
    stateful_set_control.go); the fake took the lowest name free among every pod listed and named
    seamless-postgres-1 beside a terminating -0, so a start after a down whose wait gave up
    reported a second database and failed. It recreates seamless-postgres-0 once the old one is
    gone, and the start runs as on a real cluster."""
    state = fake_kube.cluster()
    state["deployment"]["replicas"], state["deployment"]["pods"] = 0, []
    state["statefulset"]["replicas"] = 0
    state["statefulset"]["pods"][0]["terminating"] = True  # its grace period not over yet
    proc, _, _ = _run(tmp_path, "kubernetes", "start", cluster=state)
    after = _cluster_after(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert [p["name"] for p in after["statefulset"]["pods"]] == ["seamless-postgres-0"]
    [pod] = fake_kube.running(after)
    assert pod["databases"] == ["seamless-postgres-0"]


#: each message of the checks before a drain, as the script writes it, and the start of its row in
#: Operations.md §7 (reviews RV44A-11, RV45A-08)
TROUBLESHOOTING = {
    "cannot read statefulset/seamless-postgres in namespace $NS": (
        "cannot read statefulset/seamless-postgres in namespace ..."
    ),
    "does not take this token (HTTP $code)": "the API at ... does not take this token (HTTP 401)",
    "whose token this is (HTTP ${code:-000})": "cannot ask the API at ... whose token this is",
    "cannot read the API's answer to GET /api/v1/me": (
        "cannot read the API's answer to GET /api/v1/me"
    ),
    "cannot list the running plans at $API": "cannot list the running plans at ...",
    "make $checks refused (exit $?)": "make seamless-check-context ... refused ...",
    "may not $what in namespace $NS (a server-side dry run refused it)": (
        "kubectl may not ... (a server-side dry run refused it)"
    ),
    "the API server refuses those limits (a server-side dry run)": (
        "the API server refuses those limits (a server-side dry run) ..."
    ),
    "the API refused to pause plan $id (HTTP $code): it was not paused": (
        "the API refused to pause plan ... (HTTP 4xx): it was not paused"
    ),
}


@pytest.mark.parametrize("message", list(TROUBLESHOOTING))
def test_every_refusal_of_a_drains_checks_has_a_troubleshooting_row(message):
    """docs/Operations.md §7 (reviews RV44A-11, RV45A-08): each message the checks before a drain
    give - the cluster's reads and dry runs, the Make targets' checks, the token's and the API's
    answers, a pause the API refused - has a row that explains it; a fix commit said it added rows
    for the new messages, and four had none."""
    assert message in SCRIPT.read_text(), message  # the script still says it
    ops = (ROOT / "docs" / "Operations.md").read_text(encoding="utf-8")
    section = ops.split("\n## 7.", 1)[1].split("\n## 8.", 1)[0]
    row = f"| `{TROUBLESHOOTING[message]}"
    assert any(line.startswith(row) for line in section.splitlines()), row
