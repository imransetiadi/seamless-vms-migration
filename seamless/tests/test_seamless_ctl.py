"""scripts/seamless-ctl.sh (SDD §17.2): one lifecycle - up, down, stop, start, restart, scale,
capacity, status, logs - for docker, podman, kubernetes and openshift. Run against stub tools that
record their arguments: it never runs a second orchestrator, never deletes data, and a drain's token
never reaches a command line."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()
SCRIPT = ROOT / "scripts" / "seamless-ctl.sh"
TOKEN = "smg_" + "t0ken" * 8  # a test token: never an argument of any tool

#: a tool that records its call - with the header files curl was handed - and answers by the rules
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
for n, rule in enumerate(json.loads(Path(os.environ["STUB_RULES"]).read_text())):
    if rule["cmd"] == name and all(part in joined for part in rule.get("has", [])):
        counter = Path(os.environ["STUB_LOG"] + f".{n}")
        seen = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(seen + 1))
        answers = rule.get("answers", [{}])
        answer = answers[min(seen, len(answers) - 1)]
        sys.stdout.write(answer.get("stdout", ""))
        sys.exit(answer.get("exit", 0))
"""
TOOLS = ("make", "kubectl", "oc", "curl", "docker", "podman")


#: the helper that reads deploy/compose/.env as Compose does (SDD §17.2)
DOTENV = SCRIPT.parent / "dotenv-value.sh"


def _run(tmp_path, *argv, rules=(), xtrace=False, env_file=None, token_mode=0o600):
    """Run a copy of the script in a scratch tree with the stub tools first on PATH; return the run
    and the tools' calls in order."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, tree / "scripts" / SCRIPT.name)
    shutil.copy(DOTENV, tree / "scripts" / DOTENV.name)
    if env_file is not None:
        (tree / "deploy" / "compose").mkdir(parents=True, exist_ok=True)
        (tree / "deploy" / "compose" / ".env").write_text(env_file)
        (tree / "deploy" / "compose" / ".env").chmod(0o600)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in TOOLS:
        (bin_dir / name).write_text(STUB)
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
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        STUB_LOG=str(log),
        STUB_RULES=str(tmp_path / "rules.json"),
        TMPDIR=str(tmp_path),
    )
    argv = [str(token_file) if a == "@TOKEN_FILE" else a for a in argv]
    cmd = ["bash", *(["-x"] if xtrace else []), str(tree / "scripts" / SCRIPT.name), *argv]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return proc, calls, tree


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def _args(calls, cmd):
    return [" ".join(c["args"]) for c in calls if c["cmd"] == cmd]


PLATFORMS = ["docker", "podman", "kubernetes", "openshift"]


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
        f"{ns} get pods -l app.kubernetes.io/name=seamless -o name",
        f"{ns} wait --for=delete pod -l app.kubernetes.io/name=seamless --timeout=600s",
        f"{ns} scale statefulset/seamless-postgres --replicas=0",
        f"{ns} get pods -l app.kubernetes.io/name=seamless-postgres -o name",
        f"{ns} wait --for=delete pod -l app.kubernetes.io/name=seamless-postgres --timeout=600s",
    ]
    assert not any("delete" in " ".join(c["args"]).replace("--for=delete", "") for c in calls)


@pytest.mark.parametrize(("platform", "tool", "overlay"), CLUSTERS)
def test_a_cluster_up_applies_the_overlay_and_scales_both_back_to_one(
    tmp_path, platform, tool, overlay
):
    """SDD §17.2: up applies the overlay, then scales PostgreSQL and the control plane to one
    replica each - an apply restores no replica count an earlier down set - and waits for both
    rollouts."""
    proc, calls, tree = _run(tmp_path, platform, "up", "--namespace", "migrations")
    assert proc.returncode == 0, proc.stderr
    ns = "-n migrations"
    assert _args(calls, tool) == [
        f"{ns} apply -k {tree / overlay}",
        f"{ns} scale statefulset/seamless-postgres --replicas=1",
        f"{ns} rollout status statefulset/seamless-postgres --timeout=600s",
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
        f"{ns} get statefulset/seamless-postgres -o jsonpath={{.spec.replicas}}",
        *(database if postgres == "0" else []),
        f"{ns} scale deployment/seamless --replicas=1",
        f"{ns} rollout status deployment/seamless --timeout=600s",
    ]


@pytest.mark.parametrize(("platform", "tool", "overlay"), CLUSTERS)
def test_a_cluster_stop_and_scale_zero_leave_postgresql_running(tmp_path, platform, tool, overlay):
    """SDD §17.2: stop - and scale 0 - end the control plane alone; PostgreSQL keeps its replica."""
    for argv in (["stop"], ["scale", "0"]):
        proc, calls, _ = _run(tmp_path / argv[-1], platform, *argv)
        assert proc.returncode == 0, proc.stderr
        assert _args(calls, tool) == [
            "-n seamless-migrate scale deployment/seamless --replicas=0",
            "-n seamless-migrate get pods -l app.kubernetes.io/name=seamless -o name",
        ]


@pytest.mark.parametrize(("platform", "tool", "overlay"), CLUSTERS)
def test_a_cluster_restart_rolls_the_deployment_and_waits(tmp_path, platform, tool, overlay):
    """SDD §17.2: restart is a rollout restart of the Deployment - Recreate ends the old pod before
    the new one starts, so one orchestrator at a time -, awaited."""
    proc, calls, _ = _run(tmp_path, platform, "restart")
    assert proc.returncode == 0, proc.stderr
    assert _args(calls, tool) == [
        "-n seamless-migrate rollout restart deployment/seamless",
        "-n seamless-migrate rollout status deployment/seamless --timeout=600s",
    ]


@pytest.mark.parametrize(("platform", "tool", "overlay"), CLUSTERS)
def test_cluster_capacity_patches_the_configmap_and_sets_the_limits_in_one_rollout(
    tmp_path, platform, tool, overlay
):
    """SDD §17.2: capacity scales the one control plane up or down - its concurrency settings in the
    ConfigMap, its CPU and memory limits - in a single rollout, awaited."""
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
    ns = "-n seamless-migrate"
    data = {"SEAMLESS_MAX_CONCURRENT_MIGRATIONS": "20", "SEAMLESS_MAX_CONCURRENT_CUTOVERS": "4"}
    assert _args(calls, tool) == [
        f"{ns} patch configmap/seamless-config --type merge -p {_compact({'data': data})}",
        f"{ns} set resources deployment/seamless -c seamless --limits=cpu=8,memory=8Gi",
        f"{ns} rollout status deployment/seamless --timeout=600s",
    ]
    # settings alone change no pod template: a rollout restart reads the ConfigMap again
    proc, calls, _ = _run(tmp_path / "settings", platform, "capacity", "--max-cutovers", "2")
    assert proc.returncode == 0, proc.stderr
    assert _args(calls, tool) == [
        f"{ns} patch configmap/seamless-config --type merge -p "
        + _compact({"data": {"SEAMLESS_MAX_CONCURRENT_CUTOVERS": "2"}}),
        f"{ns} rollout restart deployment/seamless",
        f"{ns} rollout status deployment/seamless --timeout=600s",
    ]


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
    assert "-n seamless-migrate rollout restart deployment/seamless" in _args(calls, tool)
    for argv in (["--max-migrations", "20", "--memory", "8Gi"], ["--max-migrations", "10"]):
        proc, _, _ = _run(tmp_path / argv[-1], platform, "capacity", *argv)
        assert proc.returncode == 0 and "250 MiB" not in proc.stderr, (argv, proc.stderr)


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
        ("KEY=1.2.3\t# pinned\n", "1.2.3"),
        ("KEY=a#b\n", "a#b"),
        ("KEY=1\nKEY=2\n", "2"),
        ("KEYS=1\nOTHER=2\n", ""),
        ("# KEY=1\n", ""),
        ("KEY=1.2.3\r\n", "1.2.3"),
        ("KEY=\n", ""),
    ],
    ids=[
        "plain",
        "an export prefix",
        "spaces around",
        "double quotes and a comment",
        "single quotes and a comment",
        "an inline comment",
        "a tab before the comment",
        "a hash inside a value",
        "the last definition",
        "another key",
        "a commented line",
        "CRLF",
        "empty",
    ],
)
def test_the_dotenv_helper_reads_a_key_as_compose_does(tmp_path, dotenv, value):
    """SDD §17.2 (reviews RV38B-07, RV38B-09): scripts/dotenv-value.sh reads deploy/compose/.env by
    Compose's dotenv rules - an export prefix, spaces around the key and the value, quotes, an
    inline comment after an unquoted value, the last definition winning."""
    path = tmp_path / ".env"
    path.write_text(dotenv)
    done = subprocess.run(
        ["sh", str(DOTENV), "KEY", str(path)], capture_output=True, text=True, check=True
    )
    assert done.stdout == (value + "\n" if value else "")


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
        "ask" if c["cmd"] == "curl" and "phase=cutover" in " ".join(c["args"]) else c["cmd"]
        for c in calls
        if c["cmd"] == "make" or "phase=cutover" in " ".join(c["args"])
    ]
    assert order == ["ask", "make"], (order, proc.stderr)


def test_a_drain_pauses_the_running_plans_and_waits_until_no_cutover_is_in_flight(tmp_path):
    """SDD §17.2, §13.1: --drain pauses every running plan, waits until no migration is in cutover,
    verifying or rolling_back, then stops; the token reaches curl only through a header file in a
    private directory, gone afterwards, and the paused plans are named for resume."""
    rules = _drain_rules([{"stdout": '[{"id": "m-1"}]'}, {"stdout": "[]"}])
    proc, calls, tree = _run(tmp_path, "docker", "stop", "--drain", "@TOKEN_FILE", rules=rules)
    assert proc.returncode == 0, proc.stderr
    curl = _args(calls, "curl")
    header = [a for c in calls if c["cmd"] == "curl" for a in c["args"] if a.startswith("@")]
    assert curl[0] == f"-fsS --max-time 30 -H {header[0]} {API}/plans?status=running"
    assert (
        f"-fsS --max-time 30 -X POST -H {header[0]} -o /dev/null {API}/plans/plan-aa/pause" in curl
    )
    assert (
        f"-fsS --max-time 30 -X POST -H {header[0]} -o /dev/null {API}/plans/plan-bb/pause" in curl
    )
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
    assert _args(calls, "kubectl") == [] and "nothing was stopped" in proc.stderr
    assert "plan-aa" in proc.stderr and "resume" in proc.stderr


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


@pytest.mark.parametrize("argv", [["dance"], ["up", "--bogus"], [], ["status", "--namespace"]])
def test_an_unknown_action_or_option_is_refused_with_the_usage(tmp_path, argv):
    """SDD §17.2: a mistyped action or option runs nothing and says how to call the script."""
    proc, calls, _ = _run(tmp_path, "kubernetes", *argv)
    assert proc.returncode == 2 and "usage:" in proc.stderr.lower() and calls == []
    proc, calls, _ = _run(tmp_path / "platform", "nomad", "up")
    assert proc.returncode == 2 and calls == []


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
    podman = dry("seamless-restart", "SEAMLESS_ENGINE=podman")
    assert "podman compose" in podman and "--wait" not in podman.split("seamless-restart")[0]
