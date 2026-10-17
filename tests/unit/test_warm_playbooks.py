"""Runs the warm playbooks - and the cold import's record for a rollback - with ansible-playbook.

The collection is copied into a temporary collections path; only the modules
that talk to clouds or conversion hosts (and the conversion host lookup) are
replaced by stubs that record their arguments and keep a fake warm state, so
the role's real control flow, variables and rescue paths are exercised.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import os
import shutil
import subprocess
import sys
import time

import pytest
import yaml

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import const

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
BIN = os.path.dirname(sys.executable)
ANSIBLE_PLAYBOOK = os.path.join(BIN, "ansible-playbook")

pytestmark = pytest.mark.skipif(
    not os.path.exists(ANSIBLE_PLAYBOOK), reason="needs ansible-playbook next to python"
)

STUBBED = (
    "import_workload_prelim",
    "import_workload_dst_check",
    "import_workload_src_check",
    "import_workload_export_volumes",
    "import_workload_transfer_volumes",
    "import_workload_src_cleanup",
    "import_workload_dst_failure_cleanup",
    "import_workload_warm_snapshot",
    "import_workload_warm_sync",
    "import_workload_create_instance",
    "import_workload_rollback",
    "os_conversion_host_info",
    "server_action",
    "server_info",
)

STUB = r'''#!/usr/bin/python
# WANT_JSON
import json
import os
import sys

NAME = %(name)r
WORLD = %(world)r
CALLS = %(calls)r

with open(sys.argv[1]) as f:
    args = dict((k, v) for k, v in json.load(f).items() if not k.startswith("_ansible"))
with open(WORLD) as f:
    world = json.load(f)
with open(CALLS, "a") as f:
    f.write(json.dumps({"module": NAME, "args": args}) + "\n")

data = args.get("data") or {}
name = data.get("params", {}).get("name")
server_id = data.get("_info", {}).get("id")


def state_path(state_dir):
    return os.path.join(state_dir, server_id + ".json")


def load(path):
    if not os.path.exists(path):
        return {"server_id": server_id, "server_name": name, "transfer_uuid": None,
                "dest_volumes": {}, "passes": [], "pending_snapshot": None,
                "destination_server_id": None}
    with open(path) as f:
        return json.load(f)


def nova(server):
    """Nova as upstream answers while it powers ``server`` off: with world "powering_off", the
    server reads ACTIVE, task state powering-off, for that many server_info reads, then SHUTOFF.
    None for a server Nova does not stop; else (reads left, counter file, reads so far)."""
    left = world.get("powering_off", {}).get(server)
    if left is None:
        return None
    path = WORLD + ".reads." + server
    reads = 0
    if os.path.exists(path):
        with open(path) as f:
            reads = int(f.read())
    return max(left - reads, 0), path, reads


def save(path, state):
    # as WarmState.save does: the first record creates the state directory
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    with open(path + ".tmp", "w") as f:
        json.dump(state, f)
    os.rename(path + ".tmp", path)


result = {"changed": False}
if [NAME, name] in world.get("fail", []):
    result = {"failed": True, "msg": "injected %%s failure for %%s" %% (NAME, name)}
elif NAME == "import_workload_prelim":
    if name in world.get("existing", []):
        result = {"changed": False, "server_name": name,
                  "msg": "VM '%%s' already exists on destination, skipping." %% name}
    else:
        result = {"changed": True, "server_name": name,
                  "log_file": os.path.join(args["log_dir"], name + ".log"),
                  "state_file": os.path.join(args["log_dir"], name + ".state")}
elif NAME == "import_workload_dst_check" and args.get("network_as_warnings"):
    result = {"changed": False,
              "cutover_refusals": world.get("refusals", {}).get(name, [])}
elif NAME == "os_conversion_host_info":
    result = {"changed": False, "openstack_conversion_host": {
        "id": "src-conv", "address": "10.0.0.10", "status": "ACTIVE", "name": args["server"]}}
elif NAME == "import_workload_warm_snapshot":
    path = state_path(args["state_dir"])
    state = load(path)
    if args.get("state", "present") == "present":
        uuid = "uuid-%%s-%%d" %% (name, len(state["passes"]) + 1)
        state["pending_snapshot"] = {"transfer_uuid": uuid,
                                     "volume_map": {"/dev/vda": {"source_dev": "/dev/vdb"}}}
        save(path, state)
        result = {"changed": True, "transfer_uuid": uuid,
                  "volume_map": state["pending_snapshot"]["volume_map"]}
    elif state["pending_snapshot"] is not None:
        state["pending_snapshot"] = None
        save(path, state)
        result = {"changed": True}
elif NAME == "import_workload_warm_sync":
    path = state_path(args["state_dir"])
    state = load(path)
    kind = args["pass_kind"]
    if kind == "auto":
        kind = "delta" if state["dest_volumes"] else "full"
    state["dest_volumes"]["/dev/vda"] = {"dest_id": "dvol-" + name, "size": 1,
                                         "bootable": True, "name": name}
    sync_pass = {"number": len(state["passes"]) + 1, "kind": kind,
                 "started_at": "2026-10-08T10:00:00.000Z",
                 "ended_at": "2026-10-08T10:00:01.000Z", "bytes_scanned": 100,
                 "bytes_changed": 10, "bytes_transferred": 5, "duration_s": 1.0}
    state["passes"].append(sync_pass)
    save(path, state)
    result = {"changed": True, "transfer_uuid": args["transfer_uuid"], "sync_pass": sync_pass,
              "volume_map": {}, "block_device_mapping": [
                  {"boot_index": 0, "delete_on_termination": False,
                   "destination_type": "volume", "device_name": "vda",
                   "source_type": "volume", "uuid": "dvol-" + name}]}
elif NAME == "import_workload_create_instance":
    path = state_path(args["warm_state_dir"])
    state = load(path)
    state["destination_server_id"] = "dst-" + name
    save(path, state)
    result = {"changed": True, "server_id": "dst-" + name}
elif NAME == "import_workload_rollback":
    path = state_path(args["state_dir"])
    exists = os.path.exists(path)
    state = load(path)
    result = {"changed": False, "deleted_server_id": state["destination_server_id"],
              "deleted_volume_ids": [], "state_cleared": False}
    if exists:
        result["changed"] = True
        state["destination_server_id"] = None
        if args["delete_dest_volumes"]:
            result["deleted_volume_ids"] = sorted(
                v["dest_id"] for v in state["dest_volumes"].values())
            # as the module does: the record is emptied, the file stays (SDD 6.3)
            state["dest_volumes"], state["passes"] = {}, []
            result["state_cleared"] = True
        save(path, state)
elif NAME == "import_workload_export_volumes":
    result = {"changed": True, "transfer_uuid": "uuid-cold-" + name, "volume_map": {
        "/dev/vda": {"source_id": "svol-%%s-vda" %% name, "size": 1, "bootable": True,
                     "name": "os-migrate-" + name},
        "/dev/vdb": {"source_id": "svol-%%s-vdb" %% name, "size": 2, "bootable": False,
                     "name": name + "-data"}}}
elif NAME == "import_workload_transfer_volumes":
    volume_map = dict((dev, dict(entry, dest_id="dvol-%%s-%%s" %% (name, dev.split("/")[-1])))
                      for dev, entry in args["volume_map"].items())
    if args.get("warm_state_dir"):
        # as the module does: each volume recorded as soon as Cinder created it (SDD 6.3)
        path = state_path(args["warm_state_dir"])
        state = load(path)
        for dev, entry in volume_map.items():
            state["dest_volumes"][dev] = dict(
                (key, entry[key]) for key in ("dest_id", "size", "bootable", "name"))
        save(path, state)
    if [NAME, name] in world.get("fail_after", []):
        # cut short after creating its volumes: the module crashes and returns no volume_map
        print(json.dumps({"failed": True, "msg": "MODULE FAILURE: attach refused"}))
        sys.exit(1)
    result = {"changed": True, "volume_map": volume_map, "block_device_mapping": [
        {"boot_index": 0 if dev == "/dev/vda" else -1,
         "delete_on_termination": dev == "/dev/vda", "destination_type": "volume",
         "device_name": dev.split("/")[-1], "source_type": "volume",
         "uuid": volume_map[dev]["dest_id"]} for dev in sorted(volume_map)]}
elif NAME in ("import_workload_src_cleanup", "import_workload_dst_failure_cleanup"):
    result = {"changed": True}
elif NAME == "server_action":
    result = {"changed": True}
    power = nova(args["server"])
    if power is not None and args["action"] in ("start", "stop"):
        # openstack.cloud's server_action reads the status: ACTIVE while Nova powers it off
        if power[0] and args["action"] == "start":
            result = {"changed": False}  # "already ACTIVE": nothing started
        elif power[0]:
            result = {"failed": True, "msg": "ConflictException: 409: Client Error for url: "
                      "https://nova.example.invalid/v2.1/servers/%%s/action, Cannot 'stop' "
                      "instance while it is in task_state powering-off" %% args["server"]}
        elif args["action"] == "stop":
            result = {"changed": False}  # SHUTOFF already
    if result.get("changed"):
        with open(WORLD + ".nova", "a") as f:
            f.write("%%s %%s\n" %% (args["action"], args["server"]))
elif NAME == "server_info":
    status = world.get("server_status", {}).get(args["server"], "ACTIVE")
    task_state = None
    power = nova(args["server"])
    if power is not None:
        left, path, reads = power
        status, task_state = ("ACTIVE", "powering-off") if left else ("SHUTOFF", None)
        with open(path, "w") as f:
            f.write(str(reads + 1))
    result = {"changed": False, "servers": [] if status == "MISSING" else [
        {"id": args["server"], "name": args["server"], "status": status,
         "task_state": task_state, "access_ipv4": "10.9.9.9"}]}
print(json.dumps(result))
'''

CONV_HOST_DETAILS = """---
- name: Stub conversion host lookup
  ansible.builtin.set_fact:
    os_src_conversion_host_info:
      openstack_conversion_host: {id: src-conv, address: 10.0.0.10, status: ACTIVE, name: conv-src}
    os_dst_conversion_host_info:
      openstack_conversion_host: {id: dst-conv, address: 10.1.0.10, status: ACTIVE, name: conv-dst}
"""


def server_data(server_id, name):
    params = {key: None for key in (
        "availability_zone", "config_drive", "description", "disk_config", "key_name",
        "user_data", "scheduler_hints")}
    params.update(
        name=name, metadata={}, tags=[], ports=[], floating_ips=[], volumes=[],
        security_group_refs=[], image_ref=None,
        flavor_ref={"name": "m1.small", "project_name": None, "domain_name": None},
    )
    return {
        "type": "openstack.compute.Server",
        "params": params,
        "_info": {"id": server_id, "status": "ACTIVE"},
        "_migration_params": {
            "boot_disk_copy": True, "floating_ip_mode": "auto",
            "boot_volume_params": {"availability_zone": None, "name": None,
                                   "description": None, "volume_type": None},
            "port_creation_mode": "nova", "data_copy": True, "boot_volume": {"uuid": None},
            "additional_volumes": [{"uuid": None}], "use_nbdkit_direct": False,
            "nbdkit_socket_uri": None, "nbdkit_export_name": None,
        },
    }


class Env:
    def __init__(self, root):
        self.root = root
        self.collections = os.path.join(root, "collections")
        target = os.path.join(self.collections, "ansible_collections", "os_migrate", "os_migrate")
        for item in ("plugins", "roles", "playbooks", "meta"):
            shutil.copytree(
                os.path.join(REPO, item), os.path.join(target, item),
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        shutil.copy(os.path.join(REPO, "galaxy.yml"), target)
        self.world_file = os.path.join(root, "world.json")
        self.calls_file = os.path.join(root, "calls.jsonl")
        for name in STUBBED:
            with open(os.path.join(target, "plugins", "modules", name + ".py"), "w") as f:
                f.write(STUB % {"name": name, "world": self.world_file, "calls": self.calls_file})
        tasks = os.path.join(target, "roles", "conversion_host", "tasks")
        with open(os.path.join(tasks, "conv_host_details.yml"), "w") as f:
            f.write(CONV_HOST_DETAILS)
        with open(os.path.join(tasks, "conv_hosts_inventory.yml"), "w") as f:
            f.write("---\n- name: Stub conversion hosts inventory\n"
                    "  ansible.builtin.debug:\n    msg: stub\n")
        self.playbooks = os.path.join(target, "playbooks")
        self.data_dir = os.path.join(root, "data")
        os.makedirs(self.data_dir)
        with open(os.path.join(self.data_dir, "workloads.yml"), "w") as f:
            yaml.safe_dump({
                "os_migrate_version": const.OS_MIGRATE_VERSION,
                "resources": [server_data("srv-1", "vm1"), server_data("srv-2", "vm2")],
            }, f)
        self.state_dir = os.path.join(self.data_dir, "workload_warm")
        # the cold role reads the clouds file for its source volume API version (synthetic)
        with open(os.path.join(self.data_dir, "clouds.yaml"), "w") as f:
            yaml.safe_dump({"clouds": {
                "src": {"auth": {"auth_url": "https://src.example.invalid:5000/v3"}},
                "dst": {"auth": {"auth_url": "https://dst.example.invalid:5000/v3"}},
            }}, f)
        self.world = {}

    def nova_actions(self):
        """The starts and stops Nova took (refused ones and the module's no-ops are not)."""
        path = self.world_file + ".nova"
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().splitlines()

    def state(self, server_id):
        path = os.path.join(self.state_dir, server_id + ".json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def run(self, playbook, **extra_vars):
        with open(self.world_file, "w") as f:
            json.dump(self.world, f)
        if os.path.exists(self.calls_file):
            os.remove(self.calls_file)
        variables = {
            "os_migrate_data_dir": self.data_dir,
            "os_migrate_src_auth": {"auth_url": "https://src.example.invalid:5000/v3"},
            "os_migrate_dst_auth": {"auth_url": "https://dst.example.invalid:5000/v3"},
            "os_migrate_src_filter_current_project": False,
            "os_migrate_dst_filter_current_project": False,
            "os_migrate_conversion_keypair_private_path": os.path.join(self.root, "conv.key"),
            "os_migrate_warm_chunk_size": 1048576,
            "os_migrate_warm_workers": 2,
        }
        variables.update(extra_vars)
        vars_file = os.path.join(self.root, "vars.json")
        with open(vars_file, "w") as f:
            json.dump(variables, f)
        home = os.path.join(self.root, "home")
        env = dict(
            os.environ,
            PATH=BIN + os.pathsep + os.environ.get("PATH", ""),
            HOME=home,
            ANSIBLE_HOME=os.path.join(home, ".ansible"),
            ANSIBLE_LOCAL_TEMP=os.path.join(home, "tmp"),
            ANSIBLE_COLLECTIONS_PATH=self.collections,
            ANSIBLE_COLLECTIONS_SCAN_SYS_PATH="false",
            ANSIBLE_NOCOLOR="1",
            ANSIBLE_RETRY_FILES_ENABLED="false",
        )
        proc = subprocess.run(
            [ANSIBLE_PLAYBOOK, "-i", os.path.join(REPO, "inventory", "localhost.yml"),
             os.path.join(self.playbooks, playbook + ".yml"), "-e", "@" + vars_file],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, env=env, timeout=300, check=False,
        )
        calls = []
        if os.path.exists(self.calls_file):
            with open(self.calls_file) as f:
                calls = [json.loads(line) for line in f]
        return proc.returncode, proc.stdout, calls


def modules_for(calls, name, server_id):
    """Recorded modules of one workload, with snapshot state and action."""
    sequence = []
    for call in calls:
        args = call["args"]
        if args.get("data", {}).get("params", {}).get("name") == name:
            label = call["module"]
            if label == "import_workload_warm_snapshot":
                label += ":" + args.get("state", "present")
            sequence.append(label)
        elif call["module"] == "server_action" and args.get("server") == server_id:
            sequence.append("server_action:" + args["action"])
        elif call["module"] == "import_workload_src_check" and args.get("name") == server_id:
            sequence.append(call["module"])
    return sequence


@pytest.fixture
def env(tmp_path):
    return Env(str(tmp_path))


PRECOPY = [
    "import_workload_prelim",
    "import_workload_dst_check",
    "import_workload_warm_snapshot:present",
    "import_workload_warm_sync",
    "import_workload_warm_snapshot:absent",
]


def test_a_pass_records_what_the_cutover_will_refuse(env):
    """SDD 6.6, 7.2: each pre-copy pass writes, beside the workload's warm state, what its
    destination check found that the cutover will refuse - and a later pass replaces it."""

    def refusals(server_id):
        with open(os.path.join(env.state_dir, server_id + ".refusals.json")) as f:
            return json.load(f)

    problem = "Destination prerequisites not met: port p2 on network 'gone' does not resolve"
    env.world["refusals"] = {"vm1": [problem]}
    rc, out = env.run("import_workloads_precopy")[:2]
    assert rc == 0, out
    assert refusals("srv-1") == [problem]
    assert refusals("srv-2") == []
    env.world["refusals"] = {}  # fixed meanwhile
    rc, out = env.run("import_workloads_precopy")[:2]
    assert rc == 0, out
    assert refusals("srv-1") == []


def test_warm_playbooks_story(env):
    # 1. First pre-copy: a full pass per workload.
    rc, out, calls = env.run("import_workloads_precopy")
    assert rc == 0, out
    for name, server_id in (("vm1", "srv-1"), ("vm2", "srv-2")):
        assert modules_for(calls, name, server_id) == PRECOPY
    sync = next(c["args"] for c in calls if c["module"] == "import_workload_warm_sync")
    assert sync["pass_kind"] == "auto" and sync["transfer_uuid"] == "uuid-vm1-1"
    assert sync["chunk_size"] == 1048576 and sync["workers"] == 2
    assert sync["src_conversion_host_address"] == "10.0.0.10"
    assert sync["conversion_host"]["id"] == "dst-conv"
    assert sync["state_dir"] == env.state_dir
    assert env.state("srv-1")["passes"][0]["kind"] == "full"
    assert env.state("srv-1")["pending_snapshot"] is None
    # a pass makes no port or floating IP: their problems are warnings there (SDD 6.6)
    check = next(c["args"] for c in calls if c["module"] == "import_workload_dst_check")
    assert check["network_as_warnings"] is True

    # 2. Another pre-copy pass is a delta pass.
    rc, out, calls = env.run("import_workloads_precopy")
    assert rc == 0, out
    assert [p["kind"] for p in env.state("srv-1")["passes"]] == ["full", "delta"]

    # 3. Cutover: vm1 is cut over; vm2's name is taken in the destination by
    #    a server the warm state does not know, so its source is not stopped.
    env.world["existing"] = ["vm2"]
    rc, out, calls = env.run("import_workloads_cutover")
    assert rc != 0
    assert modules_for(calls, "vm1", "srv-1") == [
        "import_workload_prelim",
        "import_workload_dst_check",
        "server_action:stop",
        "import_workload_src_check",
        "import_workload_warm_snapshot:present",
        "import_workload_warm_sync",
        "import_workload_warm_snapshot:absent",
        "import_workload_create_instance",
    ]
    sync = next(c["args"] for c in calls if c["module"] == "import_workload_warm_sync")
    assert sync["pass_kind"] == "final"
    # the cutover's check refuses them before the source stops
    check = next(c["args"] for c in calls if c["module"] == "import_workload_dst_check")
    assert "network_as_warnings" not in check
    create = next(c["args"] for c in calls if c["module"] == "import_workload_create_instance")
    assert create["warm_state_dir"] == env.state_dir
    assert [m["uuid"] for m in create["block_device_mapping"]] == ["dvol-vm1"]
    assert env.state("srv-1")["destination_server_id"] == "dst-vm1"
    assert "server_action:stop" not in modules_for(calls, "vm2", "srv-2")
    assert "refusing to stop the source" in out

    # 4. Retrying the cutover only finishes vm1 (already created, SDD §6.5) and cuts vm2 over.
    env.world["existing"] = []
    rc, out, calls = env.run("import_workloads_cutover")
    assert rc == 0, out
    # the control plane's downtime clock keys on this task header (SDD §7.2, executors STOP_TASK)
    assert "TASK [os_migrate.os_migrate.import_workloads_warm : Stop the source server]" in out
    assert modules_for(calls, "vm1", "srv-1") == ["import_workload_create_instance"]
    assert "server_action:stop" in modules_for(calls, "vm2", "srv-2")
    assert env.state("srv-2")["destination_server_id"] == "dst-vm2"

    # 5. Rollback with deletion of the destination volumes; vm2 has a
    #    temporary snapshot left by an interrupted pass.
    state = env.state("srv-2")
    state["pending_snapshot"] = {"transfer_uuid": "uuid-left", "volume_map": {}}
    with open(os.path.join(env.state_dir, "srv-2.json"), "w") as f:
        json.dump(state, f)
    rc, out, calls = env.run(
        "rollback_workloads", os_migrate_rollback_delete_dest_volumes=True, os_migrate_rollback_all=True
    )
    assert rc == 0, out
    assert modules_for(calls, "vm1", "srv-1") == [
        "import_workload_rollback",
        "server_action:start",
    ]
    assert modules_for(calls, "vm2", "srv-2") == [
        "import_workload_warm_snapshot:absent",
        "import_workload_rollback",
        "server_action:start",
    ]
    rollback = next(c["args"] for c in calls if c["module"] == "import_workload_rollback")
    assert rollback["delete_dest_volumes"] is True
    assert rollback["match_by_name"] is False
    conversion = [c for c in calls if c["module"] == "os_conversion_host_info"]
    assert len(conversion) == 1 and conversion[0]["args"]["cloud"] == "src"
    for server_id in ("srv-1", "srv-2"):  # emptied, not removed (SDD 6.3)
        state = env.state(server_id)
        assert (state["dest_volumes"], state["passes"]) == ({}, []), server_id
        assert state["destination_server_id"] is None and state["pending_snapshot"] is None


def test_failed_precopy_removes_the_snapshot_and_reports(env):
    env.world["fail"] = [["import_workload_warm_sync", "vm1"]]

    rc, out, calls = env.run("import_workloads_precopy")

    assert rc != 0
    assert modules_for(calls, "vm1", "srv-1") == [
        "import_workload_prelim",
        "import_workload_dst_check",
        "import_workload_warm_snapshot:present",
        "import_workload_warm_sync",
        "import_workload_warm_snapshot:absent",
    ]
    assert env.state("srv-1")["pending_snapshot"] is None
    assert "Warm pre-copy of vm1 failed: injected import_workload_warm_sync failure" in out
    assert modules_for(calls, "vm2", "srv-2") == []  # the play stops on failure


def test_cutover_rerun_refuses_a_failed_destination_server(env):
    """A recorded destination server in ERROR is not 'already cut over'."""
    rc, out = env.run("import_workloads_cutover")[:2]
    assert rc == 0, out
    env.world["server_status"] = {"dst-vm1": "ERROR"}

    rc, out, calls = env.run("import_workloads_cutover")

    assert rc != 0
    assert "destination server dst-vm1 is in ERROR" in out
    assert "rollback_workloads.yml" in out
    # nothing stopped, synchronised or created again (the rescue's snapshot cleanup is a no-op)
    sequence = modules_for(calls, "vm1", "srv-1")
    assert not {"server_action:stop", "import_workload_warm_sync", "import_workload_create_instance"} & set(sequence)
    looked_up = [c["args"]["server"] for c in calls if c["module"] == "server_info"]
    assert looked_up == ["dst-vm1"]


def test_cutover_rerun_finishes_a_healthy_destination_server(env):
    """SDD §6.5: the re-run neither stops, syncs nor creates again; it finishes the recorded
    server (ACTIVE, missing floating IPs), which an earlier attempt may have left half done."""
    rc, out = env.run("import_workloads_cutover")[:2]
    assert rc == 0, out

    rc, out, calls = env.run("import_workloads_cutover")

    assert rc == 0, out
    assert "destination server dst-vm1 was already created" in out
    assert modules_for(calls, "vm1", "srv-1") == ["import_workload_create_instance"]
    finish = next(c for c in calls if c["module"] == "import_workload_create_instance"
                  and c["args"]["data"]["params"]["name"] == "vm1")
    assert finish["args"]["warm_state_dir"] and finish["args"]["block_device_mapping"] == []
    assert [c["args"]["server"] for c in calls if c["module"] == "server_info"] == ["dst-vm1", "dst-vm2"]


def test_rollback_passes_the_destination_conversion_host(env):
    rc, out = env.run("import_workloads_precopy")[:2]
    assert rc == 0, out

    rc, out, calls = env.run(
        "rollback_workloads", os_migrate_rollback_delete_dest_volumes=True, os_migrate_rollback_all=True
    )

    assert rc == 0, out
    rollback = [c["args"] for c in calls if c["module"] == "import_workload_rollback"]
    assert {r["conversion_host"] for r in rollback} == {"os_migrate_conv_dst"}


def test_rollback_keeps_destination_volumes_by_default(env):
    rc, out = env.run("import_workloads_precopy")[:2]
    assert rc == 0, out

    rc, out, calls = env.run("rollback_workloads", os_migrate_rollback_all=True)

    assert rc == 0, out
    rollback = [c["args"] for c in calls if c["module"] == "import_workload_rollback"]
    assert [r["delete_dest_volumes"] for r in rollback] == [False, False]
    assert env.state("srv-1")["dest_volumes"]  # kept for a later cutover
    assert not [c for c in calls if c["module"] == "os_conversion_host_info"]


def test_a_rollback_can_leave_the_source_as_it_is(env):
    """SDD 6.5, 7 (COLD-02): the cleanup before a cold cutover removes what a warm pre-copy left
    and leaves the source as it is - a retry may keep it stopped with an open clock; any other
    rollback starts it."""
    rc, out = env.run("import_workloads_precopy")[:2]
    assert rc == 0, out

    rc, out, calls = env.run(
        "rollback_workloads",
        os_migrate_workloads_filter=[{"regex": "^vm1$"}],
        os_migrate_rollback_delete_dest_volumes=True,
        os_migrate_rollback_start_source=False,
    )

    assert rc == 0, out
    assert modules_for(calls, "vm1", "srv-1") == ["import_workload_rollback"]
    # the volumes are gone, the record is emptied - the file stays (SDD 6.3)
    assert (env.state("srv-1")["dest_volumes"], env.state("srv-1")["passes"]) == ({}, [])
    assert "source server left as it was" in out

    rc, out, calls = env.run("rollback_workloads", os_migrate_workloads_filter=[{"regex": "^vm2$"}])
    assert rc == 0, out
    assert modules_for(calls, "vm2", "srv-2") == ["import_workload_rollback", "server_action:start"]


def test_a_rollback_starts_a_source_nova_still_powers_off(env):
    """SDD 6.5, 8 (review RV26-10): Nova still powers the source off - the server reads ACTIVE,
    task state powering-off - when the rollback comes (a cutover's stop cut off by the step
    timeout, or one whose wait met a 503, opens the clock and starts the rollback at once).
    server_action reads that ACTIVE as running and would start nothing, leaving the source to
    end SHUTOFF: the rollback waits until no stop runs, then starts it."""
    rc, out = env.run("import_workloads_precopy")[:2]
    assert rc == 0, out
    env.world["powering_off"] = {"srv-1": 2}

    rc, out, calls = env.run(
        "rollback_workloads",
        os_migrate_workloads_filter=[{"regex": "^vm1$"}],
        os_migrate_source_stop_poll_s=0,
    )

    assert rc == 0, out
    assert env.nova_actions() == ["start srv-1"], out


def test_a_stop_wait_whose_poll_outlasts_its_timeout_reads_the_server_again(env):
    """SDD 6.5 (review RV27-09): the wait reads the server every os_migrate_source_stop_poll_s
    seconds, at most os_migrate_timeout seconds - and once more at the latest after the timeout,
    also when the poll interval is longer. Retries of timeout // poll came out 0 there, and
    ansible-core then ran the task once without looking at `until`: the stop that still ran was
    neither waited for nor reported, and the rollback started nothing yet ended rc 0."""
    rc, out = env.run("import_workloads_precopy")[:2]
    assert rc == 0, out
    env.world["powering_off"] = {"srv-1": 2}  # Nova powers srv-1 off past the timeout
    started = time.monotonic()

    rc, out, calls = env.run(
        "rollback_workloads",
        os_migrate_workloads_filter=[{"regex": "^vm1$"}],
        os_migrate_source_stop_poll_s=60,
        os_migrate_timeout=2,
    )

    reads = [c for c in calls if c["module"] == "server_info"]
    assert (rc != 0, len(reads), env.nova_actions()) == (True, 2, []), (rc, len(reads), out[-1500:])
    assert time.monotonic() - started < 30, "the wait outlived os_migrate_timeout"


def test_a_warm_cutover_rerun_waits_for_a_stop_nova_still_runs(env):
    """SDD 6.5 (review RV26-10): a re-run of the cutover - the retry after a 503 during the
    stop's wait - finds Nova still powering the source off. A second stop would be refused
    (409, a permanent failure that rolls the cutover back): the cutover waits for the stop that
    runs, then goes on with the source SHUTOFF."""
    rc, out = env.run("import_workloads_precopy")[:2]
    assert rc == 0, out
    env.world["powering_off"] = {"srv-1": 2}

    rc, out, calls = env.run(
        "import_workloads_cutover",
        os_migrate_workloads_filter=[{"regex": "^vm1$"}],
        os_migrate_source_stop_poll_s=0,
    )

    assert rc == 0, out
    assert env.state("srv-1")["destination_server_id"] == "dst-vm1"
    assert env.nova_actions() == [], "a stop was sent while one ran"


def test_a_cold_import_waits_for_a_stop_nova_still_runs(env):
    """SDD 6.5 (review RV26-10): the cold import's stop - a retry after a 503 during an earlier
    attempt's wait - finds Nova still powering the source off: it waits for that stop instead of
    sending a second one Nova refuses."""
    env.world["powering_off"] = {"srv-1": 2}

    rc, out, calls = env.run("import_workloads", os_migrate_source_stop_poll_s=0, **COLD)

    assert rc == 0, out
    assert env.state("srv-1")["destination_server_id"] == "dst-vm1"
    assert env.nova_actions() == [], "a stop was sent while one ran"


def test_a_rollback_can_leave_the_ports_alone(env):
    """SDD 6.5 (review RV-02): a cancel's cleanup passes os_migrate_rollback_delete_ports false, which
    reaches the module as delete_ports; any other rollback deletes the workload's ports."""
    rc, out = env.run("import_workloads_precopy")[:2]
    assert rc == 0, out

    rc, out, calls = env.run(
        "rollback_workloads",
        os_migrate_workloads_filter=[{"regex": "^vm1$"}],
        os_migrate_rollback_delete_ports=False,
    )
    assert rc == 0, out
    rollback = [c["args"] for c in calls if c["module"] == "import_workload_rollback"]
    assert [r["delete_ports"] for r in rollback] == [False]

    rc, out, calls = env.run("rollback_workloads", os_migrate_workloads_filter=[{"regex": "^vm2$"}])
    assert rc == 0, out
    rollback = [c["args"] for c in calls if c["module"] == "import_workload_rollback"]
    assert [r["delete_ports"] for r in rollback] == [True]


def test_rollback_refuses_the_catch_all_filter_without_confirmation(env):
    """SDD 6.5: without a workload filter a rollback would revert every cut-over workload of the
    data dir; it refuses unless os_migrate_rollback_all is true. An exact filter rolls back one."""
    rc, out = env.run("import_workloads_precopy")[:2]
    assert rc == 0, out

    rc, out, calls = env.run("rollback_workloads")
    assert rc != 0
    assert "os_migrate_rollback_all" in out
    assert not [c for c in calls if c["module"] == "import_workload_rollback"]

    rc, out, calls = env.run("rollback_workloads", os_migrate_workloads_filter=[{"regex": "^vm1$"}])
    assert rc == 0, out
    assert len([c for c in calls if c["module"] == "import_workload_rollback"]) == 1


# --- the cold import's record (SDD 6.3, 6.5, 7) --------------------------------------------

# vm1's volumes as the transfer stub created them, recorded as the warm path records its own
RECORDED = {
    "/dev/vda": {"dest_id": "dvol-vm1-vda", "size": 1, "bootable": True, "name": "os-migrate-vm1"},
    "/dev/vdb": {"dest_id": "dvol-vm1-vdb", "size": 2, "bootable": False, "name": "vm1-data"},
}

COLD = {
    "os_migrate_workloads_filter": [{"regex": "^vm1$"}],
    "os_migrate_workload_stop_before_migration": True,
    "os_migrate_conversion_host_ssh_user": "cloud-user",
    "os_migrate_dst_filters": {},
}


def test_the_cold_import_records_its_volumes_and_server_for_a_rollback(env):
    """The owner's choice (2026-10-14): a cold rollback deletes exactly the volumes the cold
    import created - a cold retry never reuses them - so the import records them as soon as its
    transfer created them, and the server as soon as Nova accepts it."""
    rc, out, calls = env.run("import_workloads", **COLD)

    assert rc == 0, out
    # each writes its part of the record itself: no other task records the volumes (SDD 6.5)
    for module in ("import_workload_transfer_volumes", "import_workload_create_instance"):
        args = next(c["args"] for c in calls if c["module"] == module)
        assert args["warm_state_dir"] == env.state_dir, module
    state = env.state("srv-1")
    assert state["dest_volumes"] == RECORDED
    assert state["destination_server_id"] == "dst-vm1"
    assert state["passes"] == []
    assert env.state("srv-2") is None  # filtered out


def test_a_failed_cold_import_keeps_the_record_of_the_volumes_it_created(env):
    """A failure after the transfer: with the failure cleanup off nothing else knows the volumes
    the transfer created, and with it on the record of a volume already gone costs nothing."""
    env.world["fail"] = [["import_workload_create_instance", "vm1"]]
    for cleanup in (False, True):
        rc, out, calls = env.run(
            "import_workloads", os_migrate_workload_cleanup_on_failure=cleanup, **COLD
        )

        assert rc != 0, out
        assert env.state("srv-1")["dest_volumes"] == RECORDED
        cleaned = [c for c in calls if c["module"] == "import_workload_dst_failure_cleanup"]
        assert bool(cleaned) is cleanup


def test_a_cold_transfer_cut_short_leaves_the_record_of_the_volumes_it_created(env):
    """SDD 6.3 (review lead B): the transfer created the volumes, then failed before it returned
    its volume_map - the failure cleanup cannot see them, and a rollback deletes them by the
    record alone."""
    env.world["fail_after"] = [["import_workload_transfer_volumes", "vm1"]]

    rc, out, calls = env.run("import_workloads", **COLD)

    assert rc != 0, out
    assert env.state("srv-1")["dest_volumes"] == RECORDED
    assert not [c for c in calls if c["module"] == "import_workload_dst_failure_cleanup"]
    assert not [c for c in calls if c["module"] == "import_workload_create_instance"]


def test_a_cold_import_without_data_copy_records_the_server_and_no_volume(env):
    """data_copy false: the server boots from volumes the import did not create, so only the
    server is recorded - a rollback must never delete those volumes (SDD 6.3)."""
    with open(os.path.join(env.data_dir, "workloads.yml")) as f:
        workloads = yaml.safe_load(f)
    workloads["resources"][0]["_migration_params"]["data_copy"] = False
    with open(os.path.join(env.data_dir, "workloads.yml"), "w") as f:
        yaml.safe_dump(workloads, f)

    rc, out, calls = env.run("import_workloads", **COLD)

    assert rc == 0, out
    assert not [c for c in calls if c["module"] == "import_workload_transfer_volumes"]
    state = env.state("srv-1")
    assert state["destination_server_id"] == "dst-vm1"
    assert state["dest_volumes"] == {}


# --- each conversion host's own SSH user (SDD 6.5, audit EX-08) ----------------------------------

PER_SIDE = {
    "os_migrate_conversion_host_ssh_user": "centos",  # upstream's single user, overridden per side
    "os_migrate_src_conversion_host_ssh_user": "cloud-user",
    "os_migrate_dst_conversion_host_ssh_user": "ubuntu",
}


def ssh_users(calls):
    """Per conversion-host module: the (ssh_user, src_ssh_user) pairs its calls got."""
    users = {}
    for call in calls:
        if "ssh_user" in call["args"]:
            pair = (call["args"]["ssh_user"], call["args"].get("src_ssh_user"))
            users.setdefault(call["module"], set()).add(pair)
    return users


def test_each_conversion_host_is_reached_as_its_own_sides_user(env):
    """SDD 6.5 (audit EX-08): a RHEL source host (cloud-user) beside an Ubuntu destination host
    (ubuntu). The modules that work on the source host get the source's user; those that work on
    the destination host get the destination's, and the source's as src_ssh_user for their hop to
    the source host - in the cold import (its failure cleanup too) and in a warm pass."""
    env.world["fail"] = [["import_workload_create_instance", "vm1"]]
    rc, out, calls = env.run(
        "import_workloads", os_migrate_workload_cleanup_on_failure=True, **dict(COLD, **PER_SIDE)
    )
    assert rc != 0, out
    assert ssh_users(calls) == {
        "import_workload_export_volumes": {("cloud-user", None)},
        "import_workload_transfer_volumes": {("ubuntu", "cloud-user")},
        "import_workload_src_cleanup": {("cloud-user", None)},
        "import_workload_dst_failure_cleanup": {("ubuntu", None)},
    }
    env.world = {}
    rc, out, calls = env.run("import_workloads_precopy", **PER_SIDE)
    assert rc == 0, out
    assert ssh_users(calls) == {
        "import_workload_warm_snapshot": {("cloud-user", None)},
        "import_workload_warm_sync": {("ubuntu", "cloud-user")},
    }


def test_one_ssh_user_still_reaches_both_conversion_hosts(env):
    """SDD 6.5: upstream's single os_migrate_conversion_host_ssh_user, set alone, still serves
    both conversion hosts - and the hop between them (audit EX-08)."""
    rc, out, calls = env.run("import_workloads_precopy", os_migrate_conversion_host_ssh_user="centos")
    assert rc == 0, out
    assert ssh_users(calls) == {
        "import_workload_warm_snapshot": {("centos", None)},
        "import_workload_warm_sync": {("centos", "centos")},
    }


def test_the_conversion_hosts_inventory_gives_each_host_its_sides_user(env):
    """SDD 6.5 (audit EX-08): the deployed conversion hosts are added to the inventory with their
    sides' users - which the link key's setup logs in as - however the single variable is set."""
    real = os.path.join(REPO, "roles", "conversion_host", "tasks", "conv_hosts_inventory.yml")
    tasks = os.path.join(env.collections, "ansible_collections", "os_migrate", "os_migrate",
                         "roles", "conversion_host", "tasks")
    shutil.copy(real, tasks)
    with open(os.path.join(env.playbooks, "inventory_users.yml"), "w") as f:
        f.write(
            "---\n- hosts: localhost\n  gather_facts: false\n  tasks:\n"
            "    - ansible.builtin.include_role:\n"
            "        name: os_migrate.os_migrate.conversion_host\n"
            "        tasks_from: conv_hosts_inventory.yml\n"
            "    - ansible.builtin.debug:\n"
            "        msg: \"USERS {{ hostvars['os_migrate_conv_src'].ansible_ssh_user }} "
            "{{ hostvars['os_migrate_conv_dst'].ansible_ssh_user }}\"\n"
        )
    rc, out, calls = env.run(
        "inventory_users",
        os_migrate_src_conversion_host_name="conv-src",
        os_migrate_dst_conversion_host_name="conv-dst",
        **PER_SIDE,
    )
    assert rc == 0, out
    assert "USERS cloud-user ubuntu" in out
