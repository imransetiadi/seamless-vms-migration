"""Runs the import_from_hypervisor role with ansible-playbook.

The "hypervisor" is a local-connection inventory host; sudo, setsid,
qemu-img, qemu-nbd and ss are PATH shims. The fake qemu-nbd records
its arguments, writes its PID file and pretends to listen until SIGTERM.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import os
import re
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

QEMU_IMG = """#!%(python)s
import json, sys
args = sys.argv[1:]
with open(%(img_records)r, "a") as f:
    f.write(json.dumps(args) + "\\n")
path = args[-1]
with open(path, "rb") as f:
    data = f.read()
if "-f" in args:
    fmt = args[args.index("-f") + 1]
else:
    # like qemu, guess the format from the content: a qcow2 header reads as qcow2, whatever wrote it
    fmt = "qcow2" if data.startswith(b"QFI\\xfb") else "raw"
print("image: " + path)
print("file format: " + fmt)
# a disk file may hold the size line this qemu-img reports for it; otherwise it is 10 GiB
sizes = [line for line in data.decode("latin-1").splitlines() if line.startswith("virtual size:")]
print(sizes[0] if sizes else "virtual size: 10 GiB (10737418240 bytes)")
"""

# the first bytes of a qcow2 file: its magic and version 3
QCOW2_HEADER = b"QFI\xfb\x00\x00\x00\x03"

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

# iproute2's `ss -ltnp`, the listener check RHEL 9 ships (net-tools' netstat is not installed by
# default there; audit A-10)
SS = """#!%(python)s
import os
print("State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process")
for port in sorted(os.listdir(%(listening)r)):
    print('LISTEN 0      1      127.0.0.1:%%s      0.0.0.0:*         users:(("qemu-nbd",pid=1,fd=3))' %% port)
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
        self.img_records = os.path.join(root, "qemu-img.jsonl")
        self.log_dir = os.path.join(root, "var", "log", "os-migrate-nbd")
        for path in (self.bin, self.listening):
            os.makedirs(path)
        scripts = {"sudo": SUDO, "setsid": SETSID, "qemu-img": QEMU_IMG,
                   "qemu-nbd": QEMU_NBD, "ss": SS}
        for name, text in scripts.items():
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(text % {"python": sys.executable, "records": self.records,
                                "img_records": self.img_records, "listening": self.listening})
            os.chmod(path, 0o755)
        # A space in the instances path checks that paths are quoted.
        self.instances = os.path.join(root, "nova instances")
        self.disk_dir = os.path.join(self.instances, "srv-1")
        os.makedirs(self.disk_dir)
        # a qcow2 root disk and ephemeral disk, and the formats Nova recorded for them
        for name in ("disk", "disk.eph0"):
            with open(os.path.join(self.disk_dir, name), "wb") as f:
                f.write(QCOW2_HEADER)
        self.record_formats({"disk": "qcow2", "disk.eph0": "qcow2"})
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

    def record_formats(self, formats):
        """Write disk.info as Nova does: one line of JSON, each disk's path -> its format (a name
        is a file of this instance's directory, an absolute path is kept as it is)."""
        recorded = {os.path.join(self.disk_dir, name): fmt for name, fmt in formats.items()}
        with open(os.path.join(self.disk_dir, "disk.info"), "w") as f:
            f.write(json.dumps(recorded) + "\n")

    def started(self):
        if not os.path.exists(self.records):
            return []
        with open(self.records) as f:
            return [json.loads(line) for line in f]

    def qemu_img_calls(self):
        if not os.path.exists(self.img_records):
            return []
        with open(self.img_records) as f:
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


@pytest.mark.parametrize(
    ("line", "size"),
    [
        ("virtual size: 1.5 GiB (1610612736 bytes)", 2),
        ("virtual size: 512 MiB (536870912 bytes)", 1),
        ("virtual size: 10G (10737418240 bytes)", 10),  # qemu-img before 4.0
    ],
)
def test_disk_size_is_the_byte_count_rounded_up_to_whole_gib(hypervisor, line, size):
    """The destination volume is never smaller than the disk, whatever unit qemu-img prints."""
    with open(os.path.join(hypervisor.disk_dir, "disk"), "w") as f:
        f.write(line + "\n")
    rc, out = hypervisor.run()
    assert rc == 0, out
    assert [d["size"] for d in hypervisor.nbdkit_disks()] == [size, 10]


def test_a_disk_whose_size_cannot_be_read_fails_the_export(hypervisor):
    with open(os.path.join(hypervisor.disk_dir, "disk"), "w") as f:
        f.write("virtual size: unavailable\n")
    rc, out = hypervisor.run()
    assert rc != 0
    assert "cannot read the virtual size" in out
    assert hypervisor.nbdkit_disks() is None


def test_a_disk_qemu_img_sizes_at_zero_bytes_fails_the_export(hypervisor):
    """Seamless Migrate SDD 6.6 (upstream intake UP-01, after os-migrate 33aca993e4): an empty or
    unparsable image qemu-img sizes at 0 bytes is refused before its export - it was recorded with
    size 0 and the run failed later, at the destination volume's create."""
    with open(os.path.join(hypervisor.disk_dir, "disk"), "w") as f:
        f.write("virtual size: 0 B (0 bytes)\n")
    rc, out = hypervisor.run()
    assert rc != 0, out
    assert "cannot read a virtual size above zero" in out, out
    assert hypervisor.nbdkit_disks() is None
    assert hypervisor.started() == []


def _add_instance(hypervisor, instance_id, name, migration_params=None, disks=("disk",), first=False):
    """Another SHUTOFF instance on the same hypervisor, its qcow2 disks recorded in disk.info,
    listed last in workloads.yml - or first, as export_workloads writes a newer server."""
    disk_dir = os.path.join(hypervisor.instances, instance_id)
    os.makedirs(disk_dir)
    paths = [os.path.join(disk_dir, disk) for disk in disks]
    for path in paths:
        with open(path, "wb") as f:
            f.write(QCOW2_HEADER)
    with open(os.path.join(disk_dir, "disk.info"), "w") as f:
        f.write(json.dumps({path: "qcow2" for path in paths}) + "\n")
    with open(hypervisor.workloads) as f:
        data = yaml.safe_load(f)
    data["resources"].insert(0 if first else len(data["resources"]), {
        "type": "openstack.compute.Server",
        "params": {"name": name},
        "_info": {"id": instance_id, "status": "SHUTOFF", "hypervisor_hostname": "hv-test"},
        "_migration_params": {"use_nbdkit_direct": True, **(migration_params or {})},
    })
    with open(hypervisor.workloads, "w") as f:
        yaml.safe_dump(data, f)
    return paths[0] if len(paths) == 1 else paths


def _set_params(hypervisor, instance_id, **params):
    with open(hypervisor.workloads) as f:
        data = yaml.safe_load(f)
    for resource in data["resources"]:
        if resource["_info"]["id"] == instance_id:
            resource["_migration_params"].update(params)
    with open(hypervisor.workloads, "w") as f:
        yaml.safe_dump(data, f)


def _served(hypervisor):
    """The live exports: port -> the disk each serves."""
    return {
        int(next(a for a in e["argv"] if a.startswith("--port="))[len("--port="):]): e["argv"][-1]
        for e in hypervisor.started()
        if alive(e["pid"])
    }


def _recorded(hypervisor, instance_id):
    with open(hypervisor.workloads) as f:
        data = yaml.safe_load(f)
    for resource in data["resources"]:
        if resource["_info"]["id"] == instance_id:
            return resource["_migration_params"].get("nbdkit_disks")
    return None


def test_two_disks_of_a_run_sharing_a_port_on_one_hypervisor_are_refused(hypervisor):
    """Seamless Migrate SDD 6.6 (upstream intake UP-04a, after os-migrate f1d254cb8b): srv-1's and
    srv-2's first disks both took the default base port 10809 on hv-test. The second's start
    stopped the first's export through the PID file named by that port, and srv-1's recorded URI -
    naming no export - then served srv-2's disk, which the import copied into srv-1's volume
    without an error. The second is refused before its export starts; the first stays served."""
    second = _add_instance(hypervisor, "srv-2", "vm2")
    rc, out = hypervisor.run()
    assert rc != 0, out
    assert "port 10809" in out and "another disk of this run" in out, out
    started = hypervisor.started()
    assert second not in [entry["argv"][-1] for entry in started], out
    first = [e for e in started if e["argv"][-1] == os.path.join(hypervisor.disk_dir, "disk")]
    assert first and alive(first[0]["pid"]), out  # srv-1's export was never stopped


def test_workloads_on_one_hypervisor_with_their_own_base_ports_are_each_exported(hypervisor):
    """The way out the refusal names: a base port of its own per workload on one hypervisor."""
    second = _add_instance(hypervisor, "srv-2", "vm2", {"nbdkit_port": 10900})
    rc, out = hypervisor.run()
    assert rc == 0, out
    served = {
        int(next(a for a in e["argv"] if a.startswith("--port="))[len("--port="):]): e["argv"][-1]
        for e in hypervisor.started()
        if alive(e["pid"])
    }
    assert served == {
        10809: os.path.join(hypervisor.disk_dir, "disk"),
        10810: os.path.join(hypervisor.disk_dir, "disk.eph0"),
        10900: second,
    }, out


def test_a_rerun_never_stops_an_earlier_runs_export_of_another_disk(hypervisor):
    """Seamless Migrate SDD 6.6 (review RV44B-04): srv-1 exported and recorded by run 1; run 2
    enables srv-2 (the default base port), listed first. srv-2's disk took 10809, unknown to the
    run's table, and its start stopped srv-1's run-1 export through the port's PID file - srv-1's
    recorded URI then served srv-2's disk. A previous export is stopped only when it serves this
    same disk: srv-2 is refused, srv-1's export keeps serving what its record names."""
    rc, out = hypervisor.run()
    assert rc == 0, out
    root = os.path.join(hypervisor.disk_dir, "disk")
    first = [e for e in hypervisor.started() if e["argv"][-1] == root][0]

    second = _add_instance(hypervisor, "srv-2", "vm2", first=True)
    rc, out = hypervisor.run()
    assert rc != 0, out
    assert "serves another disk" in out and "srv-2" in out, out
    assert alive(first["pid"]), out
    assert _served(hypervisor).get(10809) == root, out
    assert second not in [e["argv"][-1] for e in hypervisor.started()], out
    assert _recorded(hypervisor, "srv-1")[0]["uri"] == "nbd+ssh://stack@hv-test:10809"


def test_a_workload_refused_for_a_shared_port_exports_none_of_its_disks(hypervisor):
    """Seamless Migrate SDD 6.6 (review RV44B-05): the refusal fired per disk, after the
    workload's earlier disks were exported - running, recorded nowhere, and the refusal's way out
    (a base port of its own) moved the workload off their ports, so no rerun stopped them. All of
    a workload's ports are judged before its first export: the refused workload exports nothing,
    and after its way out every live export is one a record names."""
    _set_params(hypervisor, "srv-1", nbdkit_port=10810)  # srv-1: 10810, 10811
    _add_instance(hypervisor, "srv-2", "vm2", disks=("disk", "disk.eph0"))  # 10809, 10810
    rc, out = hypervisor.run()
    assert rc != 0 and "port 10810" in out and "another disk of this run" in out, out
    srv2 = os.path.join(hypervisor.instances, "srv-2")
    assert not [e for e in hypervisor.started() if e["argv"][-1].startswith(srv2)], out

    _set_params(hypervisor, "srv-2", nbdkit_port=10900)  # the refusal's way out
    rc, out = hypervisor.run()
    assert rc == 0, out
    recorded = {
        d["port"]: d["disk_path"]
        for instance in ("srv-1", "srv-2")
        for d in _recorded(hypervisor, instance)
    }
    assert _served(hypervisor) == recorded, out


def test_a_workload_an_earlier_runs_export_refuses_exports_none_of_its_disks(hypervisor):
    """Seamless Migrate SDD 6.6 (reviews RV44B-04, RV45B-01): run 1 exported and recorded srv-2
    alone on its own base port 10810; run 2 enables srv-1 (the default base port: 10809, 10810).
    The earlier run's export of srv-2 behind qemu-nbd-10810.pid was found disk by disk: srv-1's
    root disk was exported on 10809 first, then its second disk was refused - the root disk's
    export ran on, recorded nowhere, and the refusal's way out (a base port of its own) moved srv-1
    off 10809, so no later run stopped it. All of a workload's ports are judged against an earlier
    run's exports before its first export too: srv-1 exports nothing, and after its way out every
    live export is one a record names."""
    _set_params(hypervisor, "srv-1", use_nbdkit_direct=False)
    second = _add_instance(hypervisor, "srv-2", "vm2", {"nbdkit_port": 10810})
    rc, out = hypervisor.run()
    assert rc == 0, out
    assert _served(hypervisor) == {10810: second}, out

    _set_params(hypervisor, "srv-1", use_nbdkit_direct=True)
    rc, out = hypervisor.run()
    assert rc != 0 and "port 10810" in out and "serves another disk" in out, out
    srv1 = os.path.join(hypervisor.instances, "srv-1")
    assert not [e for e in hypervisor.started() if e["argv"][-1].startswith(srv1)], out
    assert _recorded(hypervisor, "srv-1") is None

    _set_params(hypervisor, "srv-1", nbdkit_port=10900)  # the refusal's way out
    rc, out = hypervisor.run()
    assert rc == 0, out
    recorded = {
        d["port"]: d["disk_path"]
        for instance in ("srv-1", "srv-2")
        for d in _recorded(hypervisor, instance)
    }
    assert _served(hypervisor) == recorded, out


def _second_disk_sized_at_zero(hv):
    with open(os.path.join(hv.disk_dir, "disk.eph0"), "w") as f:  # what the qemu-img shim prints
        f.write("virtual size: 0 B (0 bytes)\n")
    return "cannot read a virtual size above zero"


def _second_disk_size_unreadable(hv):
    with open(os.path.join(hv.disk_dir, "disk.eph0"), "w") as f:
        f.write("virtual size: unavailable\n")
    return "cannot read the virtual size"


def _second_port_held_by_a_listener_no_pid_file_names(hv):
    open(os.path.join(hv.listening, "10810"), "w").close()  # an older qemu-nbd, say, without a PID file
    return "Port 10810 is in use by another process"


LATER_REFUSALS = {
    "a second disk sized at zero bytes": _second_disk_sized_at_zero,
    "a second disk whose size cannot be read": _second_disk_size_unreadable,
    "the second port held by a listener no PID file names": _second_port_held_by_a_listener_no_pid_file_names,
}


@pytest.mark.parametrize("refusal", list(LATER_REFUSALS))
def test_a_workload_a_later_disk_refuses_leaves_none_of_its_disks_exported(hypervisor, refusal):
    """Seamless Migrate SDD 6.6 (review RV46B-01): srv-1's two disks take 10809 and 10810; its second
    disk is refused after its root disk's export started - sized at zero bytes, a size qemu-img
    cannot give, its port held by a listener no PID file names. The root disk's export ran on,
    recorded nowhere. A failure of any disk stops the exports of the workload no record names
    before the run fails: nothing of srv-1 stays exported, and the run says why."""
    said = LATER_REFUSALS[refusal](hypervisor)
    rc, out = hypervisor.run()
    assert rc != 0 and said in out, out[-3000:]
    assert _recorded(hypervisor, "srv-1") is None
    srv1 = hypervisor.disk_dir + os.sep
    assert {port: disk for port, disk in _served(hypervisor).items() if disk.startswith(srv1)} == {}, out[-1500:]


def test_a_refused_workloads_exports_are_stopped_behind_a_quoted_log_dir(hypervisor):
    """Seamless Migrate SDD 6.6 (review RV46B-01): the stop that follows a later disk's refusal
    reads the PID files as process_disk.yml does - with os_migrate_nbdkit_log_dir holding a space
    and a quote too."""
    log_dir = os.path.join(hypervisor.root, "var", "log", "os migrate's nbd")
    _second_disk_sized_at_zero(hypervisor)
    rc, out = hypervisor.run(os_migrate_nbdkit_log_dir=log_dir)
    assert rc != 0 and "cannot read a virtual size above zero" in out, out[-3000:]
    srv1 = hypervisor.disk_dir + os.sep
    assert {port: disk for port, disk in _served(hypervisor).items() if disk.startswith(srv1)} == {}, out[-1500:]


def test_a_rerun_a_later_disk_refuses_keeps_the_exports_its_record_names(hypervisor):
    """Seamless Migrate SDD 6.6 (review RV46B-01): run 1 exported and recorded srv-1's two disks; a
    rerun is refused at the second disk, after the root disk's export was restarted on its recorded
    port. Only exports no record names are stopped: the record still names a live export of each of
    srv-1's disks."""
    rc, out = hypervisor.run()
    assert rc == 0, out
    recorded = {d["port"]: d["disk_path"] for d in _recorded(hypervisor, "srv-1")}
    assert _served(hypervisor) == recorded, out

    _second_disk_sized_at_zero(hypervisor)
    rc, out = hypervisor.run()
    assert rc != 0 and "cannot read a virtual size above zero" in out, out[-3000:]
    assert {d["port"]: d["disk_path"] for d in _recorded(hypervisor, "srv-1")} == recorded
    assert _served(hypervisor) == recorded, out[-1500:]


def test_the_format_comes_from_nova_disk_info_never_from_the_disk(hypervisor):
    """SDD §6.6: a raw disk's first block is the guest's. A qcow2 header the guest wrote there must
    not make qemu-nbd, run with sudo, follow a backing file of the guest's choosing."""
    disk, eph0 = (os.path.join(hypervisor.disk_dir, name) for name in ("disk", "disk.eph0"))
    with open(disk, "wb") as f:
        f.write(QCOW2_HEADER + b"\nbacking file: /etc/shadow\n")
    hypervisor.record_formats({"disk": "raw", "disk.eph0": "qcow2"})

    rc, out = hypervisor.run()

    assert rc == 0, out
    exported = {entry["argv"][-1]: entry["argv"] for entry in hypervisor.started()}
    assert "--format=raw" in exported[disk]
    assert "--format=qcow2" in exported[eph0]
    # qemu-img is told the format as well: nothing probes the disk
    assert sorted(hypervisor.qemu_img_calls()) == [["info", "-f", "qcow2", eph0], ["info", "-f", "raw", disk]]


@pytest.mark.parametrize(
    ("disk_info", "named"),
    [
        (None, "disk.info"),  # no disk.info at all
        ("not json\n", "disk.info"),
        ('["disk", "disk.eph0"]\n', "disk.info"),  # not a map of disk -> format
        ({"disk": "qcow2"}, "disk.eph0"),  # a disk it records no format for
        ({"disk": "qcow2", "disk.eph0": "qcow2 --object x"}, "disk.eph0"),  # not a plain name
        ({"disk": "qcow2", "/old/instances/srv-1/disk": "raw", "disk.eph0": "qcow2"}, "disk"),  # two formats
    ],
    ids=["missing", "not-json", "not-a-map", "unrecorded-disk", "not-a-plain-name", "two-formats"],
)
def test_a_disk_without_one_recorded_format_is_refused_before_any_export(hypervisor, disk_info, named):
    path = os.path.join(hypervisor.disk_dir, "disk.info")
    if disk_info is None:
        os.remove(path)
    elif isinstance(disk_info, dict):
        hypervisor.record_formats(disk_info)
    else:
        with open(path, "w") as f:
            f.write(disk_info)

    rc, out = hypervisor.run()

    assert rc != 0
    assert "refusing to let qemu guess" in out
    assert os.path.join(hypervisor.disk_dir, named) in out
    if named == "disk.info":
        # refused by the disk.info check itself, not by a later step tripping over what it let through
        assert re.search(r"fatal: \[localhost\]: FAILED! => .*cannot read the disk formats Nova recorded", out)
    assert hypervisor.started() == []
    assert hypervisor.nbdkit_disks() is None


def test_disk_info_entries_are_matched_by_file_name(hypervisor):
    """Nova keys disk.info by its own instances path; os_migrate_nbdkit_nova_instances_dir may
    spell the same directory differently (here with a trailing slash)."""
    hypervisor.record_formats({
        "/var/lib/nova/instances/srv-1/disk": "raw",
        "/var/lib/nova/instances/srv-1/disk.eph0": "qcow2",
    })  # fmt: skip

    rc, out = hypervisor.run(os_migrate_nbdkit_nova_instances_dir=hypervisor.instances + "/")

    assert rc == 0, out
    formats = [[a for a in entry["argv"] if a.startswith("--format=")] for entry in hypervisor.started()]
    assert formats == [["--format=raw"], ["--format=qcow2"]]


def test_the_config_drive_is_not_exported(hypervisor):
    """SDD §6.6: Nova gives the destination server a config drive of its own and records no format
    for the source's at spawn; carried over, it would be a data disk with the source's metadata."""
    with open(os.path.join(hypervisor.disk_dir, "disk.config"), "wb") as f:
        f.write(b"\0" * 64)  # an ISO 9660 image starts with its system area

    rc, out = hypervisor.run()

    assert rc == 0, out
    assert [entry["argv"][-1] for entry in hypervisor.started()] == [
        os.path.join(hypervisor.disk_dir, "disk"),
        os.path.join(hypervisor.disk_dir, "disk.eph0"),
    ]
    assert [d["device"] for d in hypervisor.nbdkit_disks()] == ["/dev/vda", "/dev/vdb"]


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
