from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import os
import subprocess
from unittest import mock

import pytest

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

    boot, data = [call[1] for call in dst.calls_to("create_volume")]
    assert boot == {
        "name": "vm1-boot", "bootable": True, "size": 20, "wait": True,
        "image": None, "description": "boot copy",
    }
    assert data == {
        "name": "vm1-data", "bootable": False, "size": 10, "wait": True,
        "image": None, "availability_zone": "nova", "description": "data disk",
    }
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
