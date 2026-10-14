from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import os
import subprocess
import time
from unittest import mock

import pytest
from openstack import exceptions as sdk_exceptions

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
    server,
    volume_common,
)
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake

CONV = "10.1.0.10"


def cloud():
    dst = fake.FakeCloud("dst")
    dst.add_conversion_host("dst-conv", CONV)
    return dst


def workload():
    data = fake.workload_data(
        volumes=[
            fake.server_volume_data(
                "vol-data", "vm1-data", 10, "/dev/vdb", "srv-1", description="data disk"
            )
        ],
        boot_volume_params={"description": "boot copy"},
    )
    return server.Server.from_data(data)


def cold_volume_map():
    # As import_workload_export_volumes leaves it for a boot-from-volume VM:
    # the boot entry's source_id is the temporary copy of the boot volume.
    return {
        "/dev/vda": {"source_id": "tmp-copy", "name": "vm1-boot", "size": 20,
                     "bootable": True, "dest_id": None, "progress": 0.0},
        "/dev/vdb": {"source_id": "vol-data", "name": "vm1-data", "size": 10,
                     "bootable": False, "dest_id": None, "progress": 0.0},
    }


def test_cold_create_destination_volumes_params(tmp_path):
    dst = cloud()
    shells = fake.ShellFactory([dst])
    with mock.patch.object(volume_common, "RemoteShell", shells):
        transfer = volume_common.OpenstackVolumeTransfer(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1", timeout=77
        )
    transfer.volume_map = cold_volume_map()
    transfer.ser_server = workload()

    transfer._create_destination_volumes()

    # created without waiting - each is recorded before it is waited for - and the bootable flag
    # set once the volume is available (Seamless Migrate SDD 6.3)
    boot, data = [call[1] for call in dst.calls_to("create_volume")]
    assert boot == {
        "name": "vm1-boot", "bootable": None, "size": 20, "wait": False,
        "image": None, "description": "boot copy",
    }
    assert data == {
        "name": "vm1-data", "bootable": None, "size": 10, "wait": False,
        "image": None, "availability_zone": "nova", "description": "data disk",
    }
    flags = [call[1]["bootable"] for call in dst.calls_to("set_volume_bootable_status")]
    assert flags == [True, False]
    assert all(entry["dest_id"] for entry in transfer.volume_map.values())


def test_destination_volume_sdk_params():
    ser_server = workload()
    mapping = cold_volume_map()

    boot = volume_common.destination_volume_sdk_params(
        None, ser_server, "/dev/vda", mapping["/dev/vda"], 99
    )
    data = volume_common.destination_volume_sdk_params(
        None, ser_server, "/dev/vdb", mapping["/dev/vdb"], 99
    )
    plain = volume_common.destination_volume_sdk_params(
        None, None, "/dev/vdb", mapping["/dev/vdb"], 99
    )

    assert boot == {"name": "vm1-boot", "bootable": True, "size": 20, "wait": True,
                    "timeout": 99, "description": "boot copy"}
    assert data == {"name": "vm1-data", "bootable": False, "size": 10, "wait": True,
                    "timeout": 99, "availability_zone": "nova",
                    "description": "data disk"}
    assert plain == {"name": "vm1-data", "bootable": False, "size": 10, "wait": True,
                     "timeout": 99}


def test_shell_factory_is_injectable():
    dst = cloud()
    shells = fake.ShellFactory([dst])

    base = volume_common.OpenStackVolumeBase(
        dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1", shell_factory=shells
    )

    assert base.shell is shells.shells[0]
    assert base.shell.address == CONV
    assert base.shell.key_path == "/keys/k"


def test_update_progress_replaces_state_file_atomically(tmp_path):
    dst = cloud()
    state_file = str(tmp_path / "vm1.state")
    base = volume_common.OpenStackVolumeBase(
        dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1",
        state_file=state_file, shell_factory=fake.ShellFactory([dst]),
    )
    base.volume_map = {"/dev/vda": {"progress": 0.0}, "/dev/vdb": {"progress": 0.0}}

    with mock.patch.object(volume_common.os, "replace", wraps=os.replace) as replace:
        base._update_progress("/dev/vdb", 42.5)

    tmp_name, target = replace.call_args[0]
    assert target == state_file and os.path.dirname(tmp_name) == str(tmp_path)
    with open(state_file, encoding="utf-8") as f:
        assert json.load(f) == {"/dev/vda": 0.0, "/dev/vdb": 42.5}
    assert os.listdir(str(tmp_path)) == ["vm1.state"]


def _exporter(nbdkit_installed):
    src = fake.FakeCloud("src")
    src.add_conversion_host("src-conv", "10.0.0.10")

    def which(command):
        if command[0] == "which":
            return 0 if command[1] == "qemu-nbd" or nbdkit_installed else 1
        return None

    shells = fake.ShellFactory([src], val=which)
    export = volume_common.OpenstackVolumeExport(
        src, "src-conv", "/keys/k", "cloud-user", "uuid-1", shell_factory=shells
    )
    export.volume_map = {
        "/dev/vda": {"source_id": "vol-1", "source_dev": "/dev/vdb", "port": None}
    }
    return export, shells


def test_nbdkit_export_is_read_only():
    export, shells = _exporter(nbdkit_installed=True)

    export._export_volumes_from_converter()

    nbdkit = [c for c in shells.commands_at("10.0.0.10") if c[:2] == ["sudo", "nbdkit"]]
    assert len(nbdkit) == 1
    assert "--readonly" in nbdkit[0]
    # nbdkit options must come before the plugin name.
    assert nbdkit[0].index("--readonly") < nbdkit[0].index("file")
    assert "--ipaddr" in nbdkit[0] and "127.0.0.1" in nbdkit[0]


def test_qemu_nbd_export_is_read_only():
    export, shells = _exporter(nbdkit_installed=False)

    export._export_volumes_from_converter()

    qemu_nbd = [c for c in shells.commands_at("10.0.0.10") if c[:2] == ["sudo", "qemu-nbd"]]
    assert len(qemu_nbd) == 1
    assert "--read-only" in qemu_nbd[0]
    assert "127.0.0.1" in qemu_nbd[0]


class LockShell:
    """A conversion host whose lock files hold ``owners`` (lock path -> content)."""

    def __init__(self, owners):
        self.owners = dict(owners)
        self.commands = []

    def cmd_out(self, command, **kwargs):
        self.commands.append(list(command))
        if command[:2] == ["sudo", "cat"]:
            if command[2] not in self.owners:
                raise FileNotFoundError(command[2])
            return self.owners[command[2]]
        return ""

    def cmd_val(self, command, **kwargs):
        self.commands.append(list(command))
        if command[:3] == ["sudo", "rm", "-f"]:
            self.owners.pop(command[3], None)
        return 1 if command[:2] == ["sudo", "kill"] else 0

    def removed(self):
        return [c[3] for c in self.commands if c[:3] == ["sudo", "rm", "-f"]]


def lock_transfer(owners):
    dst = cloud()
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst])):
        transfer = volume_common.OpenstackVolumeTransfer(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1"
        )
    transfer.shell = LockShell(owners)
    return transfer


def dead_pid():
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


DST_LOCK = volume_common.ATTACH_LOCK_FILE_DESTINATION + ".lock"


def test_a_lock_names_its_owner_as_host_and_pid():
    """SDD §6.6: the lock file holds {migrator hostname}:{pid}."""
    import socket

    transfer = lock_transfer({})
    guarded = volume_common.use_lock(volume_common.ATTACH_LOCK_FILE_DESTINATION)(
        lambda self: "attached"
    )
    assert guarded(transfer) == "attached"
    flock = next(c for c in transfer.shell.commands if "flock" in c)
    assert "echo %s:%d " % (socket.gethostname(), os.getpid()) in flock[-1]


def test_a_dead_owner_of_this_host_loses_its_lock():
    """A module killed by a cancel or a step timeout never released its lock (audit COL-06)."""
    import socket

    transfer = lock_transfer({DST_LOCK: "%s:%d\n" % (socket.gethostname(), dead_pid())})
    assert transfer.check_and_cleanup_lockfiles() is True
    assert transfer.shell.removed() == [DST_LOCK]


def test_a_live_owner_another_hosts_lock_and_a_bare_pid_keep_their_locks():
    """Liveness is checked only where the pid lives: never on the conversion host."""
    import socket

    for owner in (
        "%s:%d" % (socket.gethostname(), os.getpid()),
        "another-migrator:%d" % dead_pid(),
        str(dead_pid()),
    ):
        transfer = lock_transfer({DST_LOCK: owner})
        assert not transfer.check_and_cleanup_lockfiles()
        assert transfer.shell.removed() == [], owner
        assert not any(c[:2] == ["sudo", "kill"] for c in transfer.shell.commands)


class FlockShell(LockShell):
    """A conversion host whose ``sudo flock`` tries end, in turn, with ``results`` - 255 is ssh's
    own status when it cannot reach the host, and such a try costs ``clock`` the 10 s connect
    timeout; after the last result every try takes the lock."""

    address = "10.1.0.10"

    def __init__(self, results, clock):
        super().__init__({})
        self.results = list(results)
        self.clock = clock

    def cmd_val(self, command, **kwargs):
        if command[:2] == ["sudo", "flock"]:
            self.commands.append(list(command))
            result = self.results.pop(0) if self.results else 0
            if result == 255:
                self.clock.now += 10  # ConnectTimeout=10
            return result
        return super().cmd_val(command, **kwargs)


class Clock:
    """volume_common's time module, its clock moved on by sleep at once."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


def _guarded_attach(results):
    clock = Clock()
    transfer = lock_transfer({})
    transfer.shell = FlockShell(results, clock)
    guarded = volume_common.use_lock(volume_common.ATTACH_LOCK_FILE_DESTINATION)(
        lambda self: "attached"
    )
    return transfer, clock, guarded


def test_a_lock_wait_gives_up_when_ssh_cannot_reach_the_conversion_host():
    """SDD 6.6: ssh exits 255 when it cannot reach the host. The lock wait gives up once that
    lasted the 15-minute window - it tried 1800 times, each up to the 10 s connect timeout, about
    5.5 h, while a cold cutover's source stays stopped - with a "Lock timeout" the control plane
    retries (R-07)."""
    window = volume_common.SSH_UNREACHABLE_S
    assert window == 900  # rides out a reboot (review RV8-01)
    transfer, clock, guarded = _guarded_attach([255] * 1800)

    with mock.patch.object(volume_common, "time", clock):
        with pytest.raises(
            RuntimeError,
            match=r"Lock timeout: conversion host 10\.1\.0\.10 unreachable over SSH for \d+ s, "
            r"waiting for " + volume_common.ATTACH_LOCK_FILE_DESTINATION,
        ):
            guarded(transfer)

    tries = [c for c in transfer.shell.commands if c[:2] == ["sudo", "flock"]]
    # each try costs the 10 s connect timeout and the 1 s sleep
    assert len(tries) <= window // 11 + 3, len(tries)
    assert window <= clock.now <= window + 25, clock.now


def test_a_lock_wait_rides_out_a_failed_ssh_now_and_then_and_a_busy_lock():
    """One failed connection, or a lock another migration holds, starts the window again: a wait
    that sees ssh fail now and then, between busy answers, for far longer than the window, still
    takes the lock."""
    transfer, clock, guarded = _guarded_attach([255, 16] * 100 + [255, 255, 0])

    with mock.patch.object(volume_common, "time", clock):
        assert guarded(transfer) == "attached"

    assert clock.now > volume_common.SSH_UNREACHABLE_S


def test_conversion_host_ssh_keeps_the_link_alive():
    """SDD §6.6: a silent partition ends the ssh in about a minute (audit COL-09), for module
    commands and for the data link a warm pass opens from the destination to the source."""
    from ansible_collections.os_migrate.os_migrate.plugins.module_utils import warm_migration

    preamble = volume_common.RemoteShell("10.0.0.10", "cloud-user", "/keys/k").ssh_preamble()
    pairs = list(zip(preamble, preamble[1:]))
    assert ("-o", "ServerAliveInterval=15") in pairs
    assert ("-o", "ServerAliveCountMax=4") in pairs
    assert ("-o", "ConnectTimeout=10") in pairs
    assert preamble[-1] == "cloud-user@10.0.0.10"

    words = warm_migration.blocksync_receive_command(
        "uuid-1", "/dev/vdb", "/dev/vdc", preamble, "cloud-user"
    )
    sender = words[words.index("--") + 1:]
    assert "ServerAliveInterval=15" in sender and "ServerAliveCountMax=4" in sender


class PortShell:
    """A destination conversion host with its shared port map (PORT_MAP_FILE): its first ``free``
    listen tests find a port free, every later one finds it taken."""

    def __init__(self, free):
        self.free = free
        self.ports = set()  # what PORT_MAP_FILE holds
        self.ssh_user = "cloud-user"
        self.commands = []

    def cmd_out(self, command, **kwargs):
        self.commands.append(list(command))
        if command == ["sudo", "cat", volume_common.PORT_MAP_FILE]:
            return json.dumps(sorted(self.ports))
        if command[:2] == ["sudo", "cat"]:
            raise subprocess.CalledProcessError(1, command)  # no lock file
        return ""

    def cmd_val(self, command, **kwargs):
        self.commands.append(list(command))
        if command[:4] == ["timeout", "1", "nc", "-l"]:
            self.free -= 1
            return 124 if self.free >= 0 else 1
        return 0

    def cmd_sub(self, command, **kwargs):
        self.commands.append(list(command))
        shell = self

        class PortMapWriter:
            def communicate(self, data=None, timeout=None):
                shell.ports = set(json.loads(data))
                return "", ""

        return PortMapWriter()


def test_a_transfer_failing_before_its_forwarding_starts_reports_why_and_frees_its_ports():
    """The teardown read the forwarding process's PID before it checked that one exists: a
    transfer that failed before the forwarding started (no free port, an SSH error) ended in
    AttributeError instead of its own error, and skipped the release - the ports it had claimed
    stayed claimed on the conversion host for every later transfer."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_transfer_volumes as transfer_module,
    )

    src, dst = fake.FakeCloud("src"), cloud()
    src.add_conversion_host("src-conv", "10.0.0.10")
    volume_map = {
        "/dev/vda": dict(cold_volume_map()["/dev/vda"], port=10809),
        "/dev/vdb": dict(cold_volume_map()["/dev/vdb"], port=10810),
    }
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = transfer_module.OpenStackDestinationVolume(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1", "10.0.0.10", volume_map,
            workload(),
        )
        # a free port for the first disk, none left for the second
        transfer.shell = PortShell(free=1)
        with pytest.raises(RuntimeError, match="No free ports on conversion host"):
            transfer.transfer_exports()

    assert transfer.shell.ports == set()  # the first disk's port is free again
    assert not dst.calls_to("create_volume")


def test_the_destination_detach_takes_the_volumes_it_is_given_or_the_volume_maps():
    """SDD 6.3 (review RV4-05): given volume ids, the destination detach - under the conversion
    host's attach lock - detaches those and leaves the volume map's volumes attached; given none,
    it detaches the volume map's, as before."""
    dst = cloud()
    shells = fake.ShellFactory([dst])
    with mock.patch.object(volume_common, "RemoteShell", shells):
        transfer = volume_common.OpenstackVolumeTransfer(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1", timeout=5
        )
    conversion_host = dst.servers["dst-conv"]
    for volume_id in ("vol-mine", "vol-earlier"):
        dst.add_volume(volume_id, volume_id, 1)
        dst.attach_volume(conversion_host, dst.volumes[volume_id])
    transfer.volume_map = {"/dev/vda": {"dest_id": "vol-mine"}}

    transfer._detach_volumes_from_destination_converter(["vol-earlier"])
    assert not dst.volumes["vol-earlier"].attachments
    assert dst.volumes["vol-mine"].attachments  # the volume map's stay attached
    flocks = [c for c in shells.commands_at(CONV) if c[:2] == ["sudo", "flock"]]
    assert volume_common.ATTACH_LOCK_FILE_DESTINATION in flocks[-1]

    transfer._detach_volumes_from_destination_converter()
    assert not dst.volumes["vol-mine"].attachments


def test_the_fake_conversion_host_refuses_a_lock_its_holder_takes_again():
    """use_lock does not nest - on a real conversion host a holder taking its lock again waits for
    itself until it times out - so the fake fails a test that nests it at once, and the lock is
    free again once the outer holder released it."""
    dst = cloud()
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst])):
        transfer = volume_common.OpenstackVolumeTransfer(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1"
        )
    lock = volume_common.use_lock(volume_common.ATTACH_LOCK_FILE_DESTINATION)
    inner = lock(lambda self: "inner")
    outer = lock(inner)
    with pytest.raises(AssertionError, match="does not nest"):
        outer(transfer)
    assert inner(transfer) == "inner"


def _cinder_wait_for_status(cloud):
    """block_storage.wait_for_status as openstacksdk 4.21 runs it, for a volume whose status no
    longer changes during the wait: the status asked for returns at once, one of `failures` raises
    ResourceFailure, anything else raises ResourceTimeout once `wait` seconds are up."""

    def wait_for_status(res, status="available", failures=None, interval=2, wait=None, **kwargs):
        assert wait is not None
        cloud.record("wait_for_status", id=res.id, status=status)
        volume = cloud.volumes[res.id]
        if volume.status == status:
            return volume
        if volume.status in (failures or ["error"]):
            raise sdk_exceptions.ResourceFailure("Volume:%s transitioned to failure state" % res.id)
        raise sdk_exceptions.ResourceTimeout(
            "Timeout waiting for Volume:%s to transition to %s (it is %s)"
            % (res.id, status, volume.status)
        )

    return wait_for_status


def test_a_cold_export_detaches_a_disk_another_server_shares_from_the_source_vm():
    """SDD 6.6 (review RV6-01): the cold export detaches each data disk from the stopped source VM;
    a multi-attach disk another server also holds stays `in-use`, so the detach is done once the
    source VM's attachment is gone - waiting for `available` timed out, and the transient retry
    then migrated the VM without that disk."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_export_volumes as export_module,
    )

    src = fake.FakeCloud("src")
    src.add_conversion_host("src-conv", "10.0.0.10")
    src.add_server("srv-1", "vm1", image={"id": "img-1"})  # image-booted, no boot_disk_copy
    src.servers["srv-1"].status = "SHUTOFF"
    src.add_server("peer-1", "vm1-peer")
    shared = src.add_volume("vol-shared", "quorum", 1, server_id="srv-1", device="/dev/vdb")
    shared.attachments.append({"server_id": "peer-1", "device": "/dev/vdb", "volume_id": "vol-shared"})
    src.servers["peer-1"].volumes.append({"id": "vol-shared"})
    src.block_storage.wait_for_status = _cinder_wait_for_status(src)
    data = fake.workload_data(
        volumes=[fake.server_volume_data("vol-shared", "quorum", 1, "/dev/vdb", "srv-1")],
        image_ref={"name": "img-1"},
        boot_disk_copy=False,
    )
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([src])):
        export = export_module.OpenStackSourceVolume(
            src, "src-conv", "/keys/k", "cloud-user", "srv-1", server.Server.from_data(data)
        )
    export._get_root_and_data_volumes()

    # a detach that waits for `available` fails here at once instead of polling for 30 minutes
    with mock.patch.object(volume_common.time, "sleep"):
        export._detach_data_volumes_from_source()

    assert {a["server_id"] for a in src.volumes["vol-shared"].attachments} == {"peer-1"}


def test_a_detach_waits_for_available_when_no_other_server_holds_the_volume():
    """SDD 6.6: with no other holder, the detach waits until Cinder shows the volume available -
    `detaching` first - as openstacksdk's own wait did, within the timeout."""
    dst = cloud()
    dst.add_volume("vol-1", "vol-1", 1)
    conversion_host = dst.servers["dst-conv"]
    dst.attach_volume(conversion_host, dst.volumes["vol-1"])
    nova_detach, cinder_get, reads = dst.detach_volume, dst.get_volume_by_id, []

    def detach(server, volume, **kwargs):
        nova_detach(server, volume, **kwargs)
        dst.volumes["vol-1"].status = "detaching"

    def get_volume_by_id(volume_id):
        reads.append(volume_id)
        if len(reads) == 3:
            dst.volumes["vol-1"].status = "available"
        return cinder_get(volume_id)

    dst.detach_volume, dst.get_volume_by_id = detach, get_volume_by_id
    with mock.patch.object(volume_common.time, "sleep") as sleep:
        volume_common.detach_volume_bounded(dst, conversion_host, dst.volumes["vol-1"], 7)

    assert dst.volumes["vol-1"].status == "available"
    assert sleep.call_count == 2


def test_a_detach_that_never_ends_fails_at_the_timeout():
    """SDD 6.6 (review 5): openstacksdk 4.x's detach_volume(wait=True) would wait forever for a
    detach that never ends; detach_volume_bounded asks Nova without waiting and gives up at the
    timeout, naming the volume and the server."""
    dst = cloud()
    dst.add_volume("vol-1", "vol-1", 1)
    conversion_host = dst.servers["dst-conv"]
    dst.attach_volume(conversion_host, dst.volumes["vol-1"])
    waits = []

    def nova_accepts_and_never_detaches(server, volume, **kwargs):
        waits.append(kwargs.get("wait"))

    dst.detach_volume = nova_accepts_and_never_detaches
    with mock.patch.object(volume_common.time, "sleep"), pytest.raises(
        RuntimeError, match="Timed out after 3 s .*vol-1.*dst-conv"
    ):
        volume_common.detach_volume_bounded(dst, conversion_host, dst.volumes["vol-1"], 3)
    assert waits == [False]


def _unbounded_detach_calls(root):
    """The detach_volume calls under the plugins directory root that are not inside
    detach_volume_bounded, as sorted "relative/path.py:line" entries."""
    import ast

    offenders = []
    for directory, dirs, files in os.walk(root):
        if directory == os.path.join(root, "modules") and "_vendor" in dirs:
            dirs.remove("_vendor")  # openstack.cloud's own checkout (make vendor-links), not ours
        for name in files:
            path = os.path.join(directory, name)
            if not name.endswith(".py") or os.path.islink(path):
                continue  # the vendored openstack.cloud modules are links (make vendor-links)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "detach_volume":
                    owner = parents.get(node)
                    while owner is not None and not isinstance(owner, ast.FunctionDef):
                        owner = parents.get(owner)
                    if owner is None or owner.name != "detach_volume_bounded":
                        offenders.append("%s:%d" % (os.path.relpath(path, root), node.lineno))
    return sorted(offenders)


def test_every_detach_goes_through_the_bounded_wait():
    """SDD 6.6 (review 5): no module calls openstacksdk's detach_volume itself - with wait=True it
    would wait without bound - but through detach_volume_bounded."""
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugins")
    assert _unbounded_detach_calls(root) == []


def test_the_detach_guard_leaves_the_vendored_openstack_cloud_checkout_out(tmp_path):
    """Review RV6-03: `make vendor-links`, which `make test-ansible-units` runs first, checks
    openstack.cloud out under plugins/modules/_vendor as real files, and its server_volume module
    calls conn.detach_volume itself. The guard leaves that upstream tree out, and still flags a
    module of this collection."""
    vendored = tmp_path / "modules" / "_vendor" / "openstack.cloud" / "plugins" / "modules"
    vendored.mkdir(parents=True)
    (vendored / "server_volume.py").write_text(
        "def run(self, server, volume, wait, timeout):\n"
        "    self.conn.detach_volume(server, volume, wait=wait, timeout=timeout)\n"
    )
    (tmp_path / "module_utils").mkdir()
    (tmp_path / "module_utils" / "ours.py").write_text(
        "def detach(conn, server, volume):\n"
        "    conn.detach_volume(server=server, volume=volume, wait=True)\n"
    )

    assert _unbounded_detach_calls(str(tmp_path)) == [os.path.join("module_utils", "ours.py") + ":2"]


def test_a_volume_cinder_no_longer_has_reads_as_none():
    """openstacksdk 4.x's get_volume_by_id raises NotFoundException for a missing volume - its
    docstring's "else None" notwithstanding: volume_or_none reads that as None, and lets any
    other error through (Seamless Migrate SDD 6.4, review RV4-06)."""
    dst = cloud()
    dst.add_volume("vol-1", "kept", 1)
    assert volume_common.volume_or_none(dst, "vol-1").id == "vol-1"
    assert volume_common.volume_or_none(dst, "vol-gone") is None

    class ServerError(Exception):
        status_code = 503

    dst.get_volume_by_id = mock.Mock(side_effect=ServerError("Service Unavailable"))
    with pytest.raises(ServerError):
        volume_common.volume_or_none(dst, "vol-1")
