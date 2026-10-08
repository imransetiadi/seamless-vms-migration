"""Runs the warm playbooks with ansible-playbook.

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


def save(path, state):
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
              "deleted_volume_ids": [], "state_deleted": False}
    if exists:
        result["changed"] = True
        state["destination_server_id"] = None
        if args["delete_dest_volumes"]:
            result["deleted_volume_ids"] = sorted(
                v["dest_id"] for v in state["dest_volumes"].values())
            state["dest_volumes"] = {}
            if state["pending_snapshot"] is None:
                os.remove(path)
                result["state_deleted"] = True
            else:
                save(path, state)
        else:
            save(path, state)
elif NAME == "server_action":
    result = {"changed": True}
elif NAME == "server_info":
    status = world.get("server_status", {}).get(args["server"], "ACTIVE")
    result = {"changed": False, "servers": [] if status == "MISSING" else [
        {"id": args["server"], "name": args["server"], "status": status}]}
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
        self.world = {}

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
    create = next(c["args"] for c in calls if c["module"] == "import_workload_create_instance")
    assert create["warm_state_dir"] == env.state_dir
    assert [m["uuid"] for m in create["block_device_mapping"]] == ["dvol-vm1"]
    assert env.state("srv-1")["destination_server_id"] == "dst-vm1"
    assert "server_action:stop" not in modules_for(calls, "vm2", "srv-2")
    assert "refusing to stop the source" in out

    # 4. Retrying the cutover skips vm1 (already created) and cuts vm2 over.
    env.world["existing"] = []
    rc, out, calls = env.run("import_workloads_cutover")
    assert rc == 0, out
    # the control plane's downtime clock keys on this task header (SDD §7.2, executors STOP_TASK)
    assert "TASK [os_migrate.os_migrate.import_workloads_warm : Stop the source server]" in out
    assert modules_for(calls, "vm1", "srv-1") == []
    assert "server_action:stop" in modules_for(calls, "vm2", "srv-2")
    assert env.state("srv-2")["destination_server_id"] == "dst-vm2"

    # 5. Rollback with deletion of the destination volumes; vm2 has a
    #    temporary snapshot left by an interrupted pass.
    state = env.state("srv-2")
    state["pending_snapshot"] = {"transfer_uuid": "uuid-left", "volume_map": {}}
    with open(os.path.join(env.state_dir, "srv-2.json"), "w") as f:
        json.dump(state, f)
    rc, out, calls = env.run(
        "rollback_workloads", os_migrate_rollback_delete_dest_volumes=True
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
    assert env.state("srv-1") is None and env.state("srv-2") is None


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
    rc, out, _ = env.run("import_workloads_cutover")
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


def test_cutover_rerun_is_a_noop_for_a_healthy_destination_server(env):
    rc, out, _ = env.run("import_workloads_cutover")
    assert rc == 0, out

    rc, out, calls = env.run("import_workloads_cutover")

    assert rc == 0, out
    assert "destination server dst-vm1 was already created" in out
    assert modules_for(calls, "vm1", "srv-1") == []
    assert [c["args"]["server"] for c in calls if c["module"] == "server_info"] == ["dst-vm1", "dst-vm2"]


def test_rollback_passes_the_destination_conversion_host(env):
    rc, out, _ = env.run("import_workloads_precopy")
    assert rc == 0, out

    rc, out, calls = env.run("rollback_workloads", os_migrate_rollback_delete_dest_volumes=True)

    assert rc == 0, out
    rollback = [c["args"] for c in calls if c["module"] == "import_workload_rollback"]
    assert {r["conversion_host"] for r in rollback} == {"os_migrate_conv_dst"}


def test_rollback_keeps_destination_volumes_by_default(env):
    rc, out, _ = env.run("import_workloads_precopy")
    assert rc == 0, out

    rc, out, calls = env.run("rollback_workloads")

    assert rc == 0, out
    rollback = [c["args"] for c in calls if c["module"] == "import_workload_rollback"]
    assert [r["delete_dest_volumes"] for r in rollback] == [False, False]
    assert env.state("srv-1")["dest_volumes"]  # kept for a later cutover
    assert not [c for c in calls if c["module"] == "os_conversion_host_info"]
