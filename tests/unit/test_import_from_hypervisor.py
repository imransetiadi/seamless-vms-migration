"""Runs the import_from_hypervisor role with ansible-playbook.

The "hypervisor" is a local-connection inventory host; sudo, setsid,
qemu-img, qemu-nbd and netstat are PATH shims. The fake qemu-nbd records
its arguments, writes its PID file and pretends to listen until SIGTERM.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import os
import stat
import subprocess
import sys

import pytest
import yaml

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import const

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
BIN = os.path.dirname(sys.executable)
ANSIBLE_PLAYBOOK = os.path.join(BIN, "ansible-playbook")

pytestmark = pytest.mark.skipif(
    not os.path.exists(ANSIBLE_PLAYBOOK) or not os.path.exists("/bin/sh"),
    reason="needs ansible-playbook and a POSIX shell",
)

SUDO = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -u|-g) shift 2 ;;
    --) shift; break ;;
    -*) shift ;;
    *) break ;;
  esac
done
exec "$@"
"""

SETSID = """#!%(python)s
import os, sys
os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])
"""

QEMU_IMG = """#!/bin/sh
echo "image: $2"
echo "file format: qcow2"
echo "virtual size: 10 GiB (10737418240 bytes)"
"""

QEMU_NBD = """#!%(python)s
import json, os, signal, sys, time
opts = dict(a[2:].split("=", 1) for a in sys.argv[1:-1] if a.startswith("--") and "=" in a)
with open(%(records)r, "a") as f:
    f.write(json.dumps({"pid": os.getpid(), "argv": sys.argv[1:]}) + "\\n")
with open(opts["pid-file"], "w") as f:
    f.write("%%d\\n" %% os.getpid())
marker = os.path.join(%(listening)r, opts["port"])
open(marker, "w").close()

def stop(signum, frame):
    try:
        os.remove(marker)
    except OSError:
        pass
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)
while True:
    time.sleep(0.2)
"""

NETSTAT = """#!%(python)s
import os
for port in sorted(os.listdir(%(listening)r)):
    print("tcp        0      0 127.0.0.1:%%s        0.0.0.0:*    LISTEN   1/qemu-nbd" %% port)
"""


class Hypervisor:
    def __init__(self, root):
        # its own collection tree (os_migrate.os_migrate -> this checkout), so the tests run anywhere
        # the other unit tests do, without the podman-built .ansible/collections farm
        self.collections = os.path.join(root, "collections")
        namespace = os.path.join(self.collections, "ansible_collections", "os_migrate")
        os.makedirs(namespace)
        os.symlink(REPO, os.path.join(namespace, "os_migrate"))
        self.root = root
        self.bin = os.path.join(root, "bin")
        self.listening = os.path.join(root, "listening")
        self.records = os.path.join(root, "qemu-nbd.jsonl")
        self.log_dir = os.path.join(root, "var", "log", "os-migrate-nbd")
        for path in (self.bin, self.listening):
            os.makedirs(path)
        scripts = {"sudo": SUDO, "setsid": SETSID, "qemu-img": QEMU_IMG,
                   "qemu-nbd": QEMU_NBD, "netstat": NETSTAT}
        for name, text in scripts.items():
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(text % {"python": sys.executable, "records": self.records,
                                "listening": self.listening})
            os.chmod(path, 0o755)
        # A space in the instances path checks that paths are quoted.
        self.instances = os.path.join(root, "nova instances")
        self.disk_dir = os.path.join(self.instances, "srv-1")
        os.makedirs(self.disk_dir)
        for name in ("disk", "disk.eph0", "disk.info"):
            open(os.path.join(self.disk_dir, name), "w").close()
        self.data_dir = os.path.join(root, "data")
        os.makedirs(self.data_dir)
        self.workloads = os.path.join(self.data_dir, "workloads.yml")
        with open(self.workloads, "w") as f:
            yaml.safe_dump({
                "os_migrate_version": const.OS_MIGRATE_VERSION,
                "resources": [{
                    "type": "openstack.compute.Server",
                    "params": {"name": "vm1"},
                    "_info": {"id": "srv-1", "status": "SHUTOFF",
                              "hypervisor_hostname": "hv-test"},
                    "_migration_params": {"use_nbdkit_direct": True},
                }],
            }, f)
        self.inventory = os.path.join(root, "inventory.yml")
        host = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable}
        with open(self.inventory, "w") as f:
            yaml.safe_dump({
                "migrator": {"hosts": {"localhost": host}},
                "hypervisors": {"hosts": {"hv-test": host}},
            }, f)

    def run(self, **extra_vars):
        variables = {
            "os_migrate_data_dir": self.data_dir,
            "os_migrate_nbdkit_nova_instances_dir": self.instances,
            "os_migrate_nbdkit_log_dir": self.log_dir,
            "os_migrate_nbdkit_protocol": "ssh",
        }
        variables.update(extra_vars)
        vars_file = os.path.join(self.root, "vars.json")
        with open(vars_file, "w") as f:
            json.dump(variables, f)
        home = os.path.join(self.root, "home")
        env = dict(
            os.environ,
            PATH=self.bin + os.pathsep + BIN + os.pathsep + os.environ.get("PATH", ""),
            HOME=home,
            ANSIBLE_HOME=os.path.join(home, ".ansible"),
            ANSIBLE_LOCAL_TEMP=os.path.join(home, "tmp"),
            ANSIBLE_COLLECTIONS_PATH=self.collections,
            ANSIBLE_COLLECTIONS_SCAN_SYS_PATH="false",
            ANSIBLE_NOCOLOR="1",
            ANSIBLE_RETRY_FILES_ENABLED="false",
        )
        proc = subprocess.run(
            [ANSIBLE_PLAYBOOK, "-i", self.inventory,
             os.path.join(REPO, "playbooks", "import_from_hypervisor.yml"),
             "-e", "@" + vars_file],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, env=env, timeout=300, check=False,
        )
        return proc.returncode, proc.stdout

    def started(self):
        if not os.path.exists(self.records):
            return []
        with open(self.records) as f:
            return [json.loads(line) for line in f]

    def nbdkit_disks(self):
        with open(self.workloads) as f:
            data = yaml.safe_load(f)
        return data["resources"][0]["_migration_params"].get("nbdkit_disks")


def alive(pid):
    return subprocess.run(["ps", "-p", str(pid)], stdout=subprocess.DEVNULL,
                          check=False).returncode == 0


@pytest.fixture
def hypervisor(tmp_path):
    hv = Hypervisor(str(tmp_path))
    yield hv
    subprocess.run(["pkill", "-f", str(tmp_path)], check=False)


def test_exports_are_read_only_bound_and_logged_privately(hypervisor):
    rc, out = hypervisor.run()

    assert rc == 0, out
    started = hypervisor.started()
    assert [entry["argv"][-1] for entry in started] == [
        os.path.join(hypervisor.disk_dir, "disk"),
        os.path.join(hypervisor.disk_dir, "disk.eph0"),
    ]
    for entry, port in zip(started, (10809, 10810)):
        argv = entry["argv"]
        assert "--read-only" in argv
        assert "--shared=1" in argv
        assert "--bind=127.0.0.1" in argv
        assert "--format=qcow2" in argv
        assert "--port=%d" % port in argv
        pid_file = os.path.join(hypervisor.log_dir, "qemu-nbd-%d.pid" % port)
        assert "--pid-file=" + pid_file in argv
        with open(pid_file) as f:
            assert int(f.read()) == entry["pid"]
        assert os.path.exists(os.path.join(hypervisor.log_dir, "qemu-nbd-%d.log" % port))
    assert stat.S_IMODE(os.stat(hypervisor.log_dir).st_mode) == 0o700
    disks = hypervisor.nbdkit_disks()
    assert [d["uri"] for d in disks] == [
        "nbd+ssh://stack@hv-test:10809",
        "nbd+ssh://stack@hv-test:10810",
    ]
    assert [d["device"] for d in disks] == ["/dev/vda", "/dev/vdb"]
    assert [d["size"] for d in disks] == [10, 10]  # parsed from qemu-img info, stored as integers
    assert [d["port"] for d in disks] == [10809, 10810]
    assert [d["bootable"] for d in disks] == [True, False]


def test_tcp_protocol_refuses_a_loopback_export(hypervisor):
    rc, out = hypervisor.run(os_migrate_nbdkit_protocol="tcp")

    assert rc != 0
    assert "os_migrate_nbdkit_bind_address" in out
    assert hypervisor.started() == []


def test_tcp_protocol_listens_on_the_bind_address(hypervisor):
    rc, out = hypervisor.run(
        os_migrate_nbdkit_protocol="tcp",
        os_migrate_nbdkit_bind_address="192.0.2.10",
        os_migrate_nbdkit_readonly=False,
    )

    assert rc == 0, out
    argv = hypervisor.started()[0]["argv"]
    assert "--bind=192.0.2.10" in argv
    assert "--read-only" not in argv
    assert [d["uri"] for d in hypervisor.nbdkit_disks()] == [
        "nbd://192.0.2.10:10809",
        "nbd://192.0.2.10:10810",
    ]


def test_rerun_stops_only_the_export_named_in_the_pid_file(hypervisor):
    rc, out = hypervisor.run()
    assert rc == 0, out
    first, second = hypervisor.started()
    # Port 10810's export is gone and its PID was reused by another process.
    os.kill(second["pid"], 15)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", hypervisor.root]
    )
    with open(os.path.join(hypervisor.log_dir, "qemu-nbd-10810.pid"), "w") as f:
        f.write("%d\n" % unrelated.pid)

    rc, out = hypervisor.run()

    assert rc == 0, out
    assert not alive(first["pid"])  # the previous export of 10809 was stopped
    assert unrelated.poll() is None  # the reused PID was left alone
    third, fourth = hypervisor.started()[2:]
    assert alive(third["pid"]) and alive(fourth["pid"])
    unrelated.kill()
    unrelated.wait()
