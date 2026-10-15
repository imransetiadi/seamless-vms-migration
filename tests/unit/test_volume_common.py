from __future__ import absolute_import, division, print_function

__metaclass__ = type

import io
import json
import logging
import os
import re
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
    conv = src.add_conversion_host("src-conv", "10.0.0.10")
    # the volume the map names is on the conversion host, at /dev/vdb: the export checks the
    # disk's identity before it serves it (Seamless Migrate SDD 6.6, review RV13-02)
    src.attach_volume(conv, src.add_volume("vol-1", "vm1-root", 10))

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

    def test_ssh_connection(self):
        """As RemoteShell's: one command over ssh, whose failure raises."""
        self.cmd_out(["echo connected"])

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


class UnsetPortMap(PortShell):
    """A destination conversion host the ssh check reaches whose port map then cannot be set up -
    sudo fails, its /var/run is full: from that check on, the release's first command exits 1."""

    def __init__(self, free):
        super().__init__(free)
        self.checked = False

    def test_ssh_connection(self):
        super().test_ssh_connection()
        self.checked = True

    def cmd_out(self, command, **kwargs):
        if self.checked and command[:3] == ["sudo", "bash", "-c"]:
            raise subprocess.CalledProcessError(1, command)
        return super().cmd_out(command, **kwargs)


class OddCheck(PortShell):
    """A destination conversion host whose ssh check raises what no ssh failure does - here the
    decode of an answer that is no UTF-8."""

    def test_ssh_connection(self):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


@pytest.mark.parametrize(
    "shell, logged",
    [
        (UnsetPortMap, "Could not release ports [49152]"),
        (OddCheck, "Ports [49152] stay claimed on the conversion host it cannot reach"),
    ],
    ids=["the release fails", "the ssh check fails another way"],
)
def test_a_failed_transfer_whose_port_release_fails_reports_its_own_error(caplog, shell, logged):
    """SDD 6.6 (review RV11-02): a failed transfer's release that fails is logged and never
    replaces the transfer's error - raised in the teardown, its "Unable to initialize port map
    file!" hid why the transfer failed; nor does an ssh check that fails another way."""
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
        transfer.shell = shell(free=1)
        with caplog.at_level(logging.WARNING, logger="osp-osp"):
            with pytest.raises(RuntimeError, match="No free ports on conversion host"):
                transfer.transfer_exports()

    assert transfer.shell.ports == {49152}  # still claimed
    assert logged in caplog.text


class SilentHost(PortShell):
    """A destination conversion host that stops answering once the transfer waits for its
    exports: every `qemu-img info` over ssh fails as ssh does - exit status 255 - each try
    costing the 10 s connect timeout."""

    def __init__(self, clock):
        super().__init__(free=2)
        self.clock = clock
        self.address = CONV

    def cmd_out(self, command, **kwargs):
        if command[:2] == ["qemu-img", "info"]:
            self.clock.now += 10
            raise subprocess.CalledProcessError(255, ["ssh", self.address] + list(command))
        return super().cmd_out(command, **kwargs)


@pytest.mark.parametrize("waits_for", ["the forwarded exports", "an nbdkit socket"])
def test_a_transfers_wait_for_its_exports_gives_up_after_the_ssh_window(waits_for):
    """Review RV9-04: the transfer's waits for the forwarded NBD exports and for an nbdkit socket
    retried every failed `qemu-img info`, ssh's 255 too, `timeout` times - about 5.5 h with the
    default while the destination conversion host could not be reached. They now give up once
    ssh has failed for the window, as a lock wait does (SDD 6.6)."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_transfer_volumes as transfer_module,
    )

    src, dst = fake.FakeCloud("src"), cloud()
    src.add_conversion_host("src-conv", "10.0.0.10")
    volume_map = {"/dev/vda": dict(cold_volume_map()["/dev/vda"], port=10809)}
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = transfer_module.OpenStackDestinationVolume(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1", "10.0.0.10", volume_map,
            workload(), timeout=1800,
        )
    clock = Clock()
    transfer.shell = SilentHost(clock)

    with mock.patch.object(volume_common, "time", clock):
        with pytest.raises(RuntimeError, match="SSH timeout: conversion host %s unreachable" % CONV):
            if waits_for == "an nbdkit socket":
                transfer._setup_nbdkit_direct_urls("nbd+unix:///?socket=/run/nbd.sock", "disk1")
            else:
                transfer._create_forwarding_process()

    assert clock.now <= volume_common.SSH_UNREACHABLE_S + 60


class GoneHost(PortShell):
    """The destination conversion host, down for good from the first `qemu-img info` on: every
    ssh to it fails as ssh does - exit status 255 - each try the 10 s connect timeout."""

    def __init__(self, clock):
        super().__init__(free=2)
        self.clock, self.address, self.down_since = clock, CONV, None

    def _down(self, command):
        if command[:2] == ["qemu-img", "info"] and self.down_since is None:
            self.down_since = self.clock.now
        if self.down_since is not None:
            self.clock.now += 10
            return True
        return False

    def cmd_out(self, command, **kwargs):
        if self._down(command):
            raise subprocess.CalledProcessError(255, ["ssh", self.address] + list(command))
        return super().cmd_out(command, **kwargs)

    def cmd_val(self, command, **kwargs):
        return 255 if self._down(command) else super().cmd_val(command, **kwargs)

    def cmd_sub(self, command, **kwargs):
        if "-N" in command:  # the SSH forward
            return fake.FakeProcess()
        return super().cmd_sub(command, **kwargs)


def test_a_transfer_whose_host_went_down_gives_up_after_one_ssh_window():
    """Review RV10-04: the transfer's wait for its forwarded exports gives up once ssh to the
    destination conversion host has failed for the window - and then released its ports under the
    port lock, a second wait on the same dead host: 31 minutes in all with the source stopped, and
    its "Lock timeout" replaced the forwarded wait's "SSH timeout". A transfer that gave up so now
    releases no port, and its error stands (SDD 6.6)."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_transfer_volumes as transfer_module,
    )

    src, dst = fake.FakeCloud("src"), cloud()
    src.add_conversion_host("src-conv", "10.0.0.10")
    volume_map = {"/dev/vda": dict(cold_volume_map()["/dev/vda"], port=10809)}
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = transfer_module.OpenStackDestinationVolume(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1", "10.0.0.10", volume_map,
            workload(), timeout=1800,
        )
    clock = Clock()
    transfer.shell = GoneHost(clock)

    with mock.patch.object(volume_common, "time", clock):
        with pytest.raises(RuntimeError, match="waiting for the forwarded NBD exports"):
            transfer.transfer_exports()

    assert clock.now - transfer.shell.down_since <= volume_common.SSH_UNREACHABLE_S + 60


def _takes_the_attach_lock(command):
    return command[:2] == ["sudo", "flock"] and command[6] == volume_common.ATTACH_LOCK_FILE_DESTINATION


class GoneAtTheAttachLock(PortShell):
    """The destination conversion host, down for good from the first command ``starts`` matches -
    by default the transfer's first try for its attach lock - on: every ssh to it then fails as ssh
    does, exit status 255, each try the 10 s connect timeout. Until then it answers: its ports are
    free, the forwarded exports answer, lsblk lists the devices of the fake cloud."""

    def __init__(self, clock, address, cloud=None, starts=_takes_the_attach_lock):
        super().__init__(free=2)
        self.clock, self.address, self.down_since = clock, address, None
        self.cloud, self.starts = cloud, starts

    def _down(self, command):
        if self.down_since is None and self.starts(command):
            self.down_since = self.clock.now
        if self.down_since is None:
            return False
        self.clock.now += 10
        return True

    def cmd_out(self, command, **kwargs):
        if self._down(command):
            raise subprocess.CalledProcessError(255, ["ssh", self.address] + list(command))
        if command and command[0] == "lsblk":
            return "\n".join(self.cloud.devices[self.cloud.addresses[self.address]])
        return super().cmd_out(command, **kwargs)

    def cmd_val(self, command, **kwargs):
        return 255 if self._down(command) else super().cmd_val(command, **kwargs)

    def cmd_sub(self, command, **kwargs):
        if "-N" in command:  # the SSH forward
            return fake.FakeProcess()
        return super().cmd_sub(command, **kwargs)


def test_a_transfer_whose_host_went_down_between_two_waits_fails_without_a_port_lock_window():
    """Review RV11-02: the destination conversion host goes down between two waits - here at the
    lsblk of the attach, as well during the copy, whose ssh then exits 255. The step fails at once
    on ssh's 255, and its release then waited a whole window for the port lock of the host it
    cannot reach, the source stopped, its "Lock timeout ... v2v-migration-lock" replacing the
    error. A failed transfer now releases its ports only if one ssh check reaches the host."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_transfer_volumes as transfer_module,
    )

    src, dst = fake.FakeCloud("src"), cloud()
    src.add_conversion_host("src-conv", "10.0.0.10")
    volume_map = {"/dev/vda": dict(cold_volume_map()["/dev/vda"], port=10809)}
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = transfer_module.OpenStackDestinationVolume(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1", "10.0.0.10", volume_map,
            workload(), timeout=1800,
        )
    clock = Clock()
    transfer.shell = GoneAtTheAttachLock(
        clock, CONV, cloud=dst, starts=lambda command: command[:1] == ["lsblk"]
    )

    with mock.patch.object(volume_common, "time", clock):
        with pytest.raises(Exception) as failure:
            transfer.transfer_exports()

    assert "lsblk" in str(failure.value), str(failure.value)  # the transfer's own error
    assert clock.now - transfer.shell.down_since <= 60, str(failure.value)


class RefusingHost(PortShell):
    """A conversion host ssh fails to reach at once from the first port probe on: the migrator's
    route to it went away or its sshd stopped while the host stays up ("Connection refused"), so
    ssh exits 255 within milliseconds - 20 ms here - not after the 10 s connect timeout."""

    def __init__(self, clock, address):
        super().__init__(free=0)
        self.clock, self.address, self.down = clock, address, False

    def cmd_val(self, command, **kwargs):
        self.down = self.down or command[:3] == ["timeout", "1", "nc"]
        if self.down:
            self.clock.now += 0.02
            return 255
        return super().cmd_val(command, **kwargs)

    def cmd_out(self, command, **kwargs):
        if self.down:
            self.clock.now += 0.02
            raise subprocess.CalledProcessError(255, ["ssh", self.address] + list(command))
        return super().cmd_out(command, **kwargs)


def test_the_port_probe_gives_up_after_the_ssh_window_when_ssh_fails_at_once():
    """Review RV11-03: the probe's give-up is timed - ssh failing for 900 s - but its loop had no
    pause and ended with the ports: with each failed ssh back in milliseconds it walked all 16383
    ephemeral ports in about 5.5 minutes and ended with "No free ports on conversion host!", which
    reads as permanent. It now tries the same port again once a second while ssh fails."""
    dst = cloud()
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst])):
        transfer = volume_common.OpenstackVolumeTransfer(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1"
        )
    clock = Clock()
    transfer.shell = RefusingHost(clock, CONV)

    with mock.patch.object(volume_common, "time", clock):
        with pytest.raises(RuntimeError, match="SSH timeout: conversion host %s unreachable" % CONV):
            transfer._find_free_port()

    assert transfer.shell.ports == set()  # no port it could not probe was claimed


class PortMapGone(PortShell):
    """A conversion host whose port map cannot be read: the lock was taken, then ssh fails (exit
    status 255) or `cat` fails (exit status 1) on the read of PORT_MAP_FILE."""

    def __init__(self, status):
        super().__init__(free=1)
        self.status, self.address = status, CONV

    def cmd_out(self, command, **kwargs):
        if command == ["sudo", "cat", volume_common.PORT_MAP_FILE]:
            raise subprocess.CalledProcessError(self.status, ["ssh", self.address] + list(command))
        return super().cmd_out(command, **kwargs)


@pytest.mark.parametrize(
    "status,message",
    [
        (volume_common.SSH_FAILED, "SSH timeout: conversion host %s unreachable over SSH, reading the port map" % CONV),
        (1, "Unable to read the port map"),
    ],
)
def test_a_port_map_read_that_fails_gives_up_with_its_own_error(status, message):
    """Audit A-17: the read of the port map under the port lock fell through to an unbound
    variable when the command failed, so a transient ssh failure between taking the lock and the
    read surfaced as a Python error instead of the "SSH timeout" give-up the control plane retries
    (SDD 6.6); any other failure of the read names the port map. Neither claims a port."""
    dst = cloud()
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst])):
        transfer = volume_common.OpenstackVolumeTransfer(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1"
        )
    transfer.shell = PortMapGone(status)

    with pytest.raises(RuntimeError, match=message):
        transfer._find_free_port()

    assert transfer.claimed_ports == []
    assert not any(c[:4] == ["timeout", "1", "nc", "-l"] for c in transfer.shell.commands)


class PortLockHeldElsewhere:
    """The destination conversion host, reachable, whose port lock a live module of another
    migrator holds (``check_process`` keeps another host's owner): every try for the port lock
    answers 17 ("Another conversion is holding the lock") and costs ``rtt`` seconds of ``clock``."""

    address = CONV
    ssh_user = "cloud-user"

    def __init__(self, clock, rtt=0.2):
        self.clock, self.rtt = clock, rtt
        self.lock_tries = 0

    def test_ssh_connection(self):
        """Reachable: the check answers."""

    def cmd_out(self, command, **kwargs):
        if command == ["sudo", "cat", volume_common.PORT_LOCK_FILE + ".lock"]:
            return "migrator-2:4242"
        if command[:2] == ["sudo", "cat"]:
            raise subprocess.CalledProcessError(1, command)  # no such lock file
        return ""

    def cmd_val(self, command, **kwargs):
        if command[:2] == ["sudo", "flock"] and command[6] == volume_common.PORT_LOCK_FILE:
            self.lock_tries += 1
            self.clock.now += self.rtt
            return 17
        return 0

    def cmd_sub(self, command, **kwargs):
        return fake.FakeProcess()


def test_a_transfer_whose_port_lock_wait_gave_up_waits_no_second_window_to_release_no_port():
    """Review RV12-02: a failed transfer releases its ports once one ssh check answers (RV11-02).
    One whose first wait for the port lock gave up - another migrator's live module holds it -
    claimed no port, yet its release waited a second whole window for the same lock, about 36
    minutes more with a cold cutover's source stopped, to remove nothing (Seamless Migrate SDD
    6.6)."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_transfer_volumes as transfer_module,
    )

    src, dst = fake.FakeCloud("src"), cloud()
    src.add_conversion_host("src-conv", "10.0.0.10")
    volume_map = {"/dev/vda": dict(cold_volume_map()["/dev/vda"], port=10809)}
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = transfer_module.OpenStackDestinationVolume(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1", "10.0.0.10", volume_map,
            workload(), timeout=1800,
        )
    clock = Clock()
    transfer.shell = host = PortLockHeldElsewhere(clock)

    with mock.patch.object(volume_common, "time", clock):
        with pytest.raises(RuntimeError, match="Unable to acquire lock " + volume_common.PORT_LOCK_FILE):
            transfer.transfer_exports()

    assert transfer.claimed_ports == []
    assert host.lock_tries == volume_common.DEFAULT_TIMEOUT, (host.lock_tries, clock.now)


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
    dst.detach_reads = 0  # Cinder's timing is this test's own: the attachment goes at once
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


class ConvertShell:
    """A destination conversion host for the transfer's copy: its disks show the virtio serials of
    the volumes in ``held`` ({device: volume id}) and their by-id links; qemu-img, virt-sparsify and
    nbdcopy end at once."""

    address = CONV

    def __init__(self, held):
        self.held, self.commands = held, []

    def cmd_out(self, command, **kwargs):
        self.commands.append(list(command))
        if command[:1] == ["cat"] and command[1].startswith("/sys/block/"):
            device = "/dev/" + command[1].split("/")[3]
            if device not in self.held:
                raise subprocess.CalledProcessError(1, command)
            return self.held[device][:20]
        return ""

    def cmd_val(self, command, **kwargs):
        self.commands.append(list(command))
        if command[:2] == ["test", "-b"]:
            serial = command[2][len("/dev/disk/by-id/virtio-"):]
            return 0 if any(volume_id[:20] == serial for volume_id in self.held.values()) else 1
        return 0

    def cmd_sub(self, command, **kwargs):
        self.commands.append(list(command))
        return DoneProcess()


class DoneProcess:
    """A process that has ended well, its output already read."""

    returncode = 0

    def __init__(self):
        read, write = os.pipe()
        os.close(write)
        self.stdout = os.fdopen(read, "rb")
        self.stderr = io.StringIO()

    def poll(self):
        return 0

    def wait(self):
        return 0

    def communicate(self, data=None, timeout=None):
        return "", ""


def _copying_transfer(tmp_path, held):
    transfer = object.__new__(volume_common.OpenstackVolumeTransfer)
    transfer.log, transfer.log_file, transfer.state_file = mock.Mock(), str(tmp_path / "log"), None
    transfer.transfer_uuid = "uuid-1"
    transfer.qemu_progress_re = re.compile(r"\((\d+\.\d+)/100%\)")
    transfer.volume_map = {
        "/dev/vda": {"dest_id": "dvol-boot-0123456789-tail", "dest_dev": "/dev/vdb",
                     "url": "nbd://localhost:10809/uuid-1", "progress": 0.0},
    }
    transfer.shell = ConvertShell(held)
    return transfer


@pytest.mark.parametrize("copy", ["qemu-img", "nbdcopy"])
def test_the_transfer_writes_each_destination_volume_through_its_by_id_link(tmp_path, copy):
    """Seamless Migrate SDD 6.6 (the RV12-03 hazard on the destination): qemu-img and nbdcopy wrote
    each destination volume through the /dev/vdX recorded at attach - a path another volume took
    before their open was written into. They are now given the volume's by-id link, once its serial
    was checked, which resolves at their open to the volume itself."""
    transfer = _copying_transfer(tmp_path, {"/dev/vdb": "dvol-boot-0123456789-tail"})

    if copy == "qemu-img":
        transfer._convert_destination_volumes()
        writes = [c[-1] for c in transfer.shell.commands if c[:3] == ["sudo", "qemu-img", "convert"]]
    else:
        transfer._convert_destination_volumes_nbdcopy()
        writes = [c[3] for c in transfer.shell.commands if c[:2] == ["sudo", "nbdcopy"]]

    assert writes and set(writes) == {"/dev/disk/by-id/virtio-dvol-boot-0123456789"}, writes


@pytest.mark.parametrize("copy", ["qemu-img", "nbdcopy"])
def test_the_transfer_never_writes_a_destination_path_another_volume_took(tmp_path, copy):
    """Seamless Migrate SDD 6.6: the destination volume left the conversion host, and the kernel gave
    its /dev/vdX to another volume's disk - the copy fails before anything is written."""
    transfer = _copying_transfer(tmp_path, {"/dev/vdb": "other-volume-0123456789"})

    with pytest.raises(RuntimeError, match="not of volume dvol-boot"):
        if copy == "qemu-img":
            transfer._convert_destination_volumes()
        else:
            transfer._convert_destination_volumes_nbdcopy()

    assert not [c for c in transfer.shell.commands if c[:3] in (["sudo", "qemu-img", "convert"],)]
    assert not [c for c in transfer.shell.commands if c[:2] == ["sudo", "nbdcopy"]]


class IdentityShell:
    """A destination conversion host whose /dev/vdc fails its identity check: ``gone`` - the
    volume left the host, so the serial read's ssh command fails with cat's exit status 1 - or
    ``another volume`` - the disk there is another volume's."""

    address = CONV

    def __init__(self, failure):
        self.failure = failure

    def cmd_out(self, command, **kwargs):
        if self.failure == "gone":
            raise subprocess.CalledProcessError(
                1, ["ssh", "-o", "ConnectTimeout=10", "cloud-user@" + CONV] + list(command)
            )
        return "other-volume-0123456"

    def cmd_val(self, command, **kwargs):
        return 0


TRANSFER_ARGS = {
    "data": fake.workload_data(),
    "conversion_host": {"id": "dst-conv"},
    "ssh_key_path": "/keys/k",
    "ssh_user": "cloud-user",
    "transfer_uuid": "uuid-1",
    "src_conversion_host_address": "10.0.0.10",
    "volume_map": {"/dev/vda": {"source_dev": "/dev/vdb", "url": "nbd://localhost:10809/uuid-1"}},
}


def _transfer_module_failing_with(transfer_exports):
    """The transfer module's run, its transfer failing as ``transfer_exports`` does."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_transfer_volumes as transfer_module,
    )

    with mock.patch.object(transfer_module, "OpenStackDestinationVolume") as destination:
        destination.return_value.transfer_exports.side_effect = transfer_exports
        return fake.run_module(transfer_module, dict(TRANSFER_ARGS), fake.FakeCloud("dst"))


@pytest.mark.parametrize(
    "failure, message",
    [
        ("gone", "/dev/vdc on the conversion host is gone: volume dvol-1 left the host"),
        ("another volume", "/dev/vdc on the conversion host is the disk of volume "
                           "other-volume-0123456, not of volume dvol-1: the volume left the host"),
    ],
)
def test_a_destination_disk_that_fails_its_identity_check_fails_the_transfer_by_its_own_message(
    failure, message
):
    """Review RV15-01: the transfer module let the identity check's error escape, and Ansible
    printed it with its cause - the serial read's ssh command line, ConnectTimeout and all - which
    the control plane retries: two more whole cold copies with the source stopped, for a failure
    SDD 6.6 calls permanent. The module now fails by the error's own message, as the export does;
    the cause stays in the traceback for whoever reads the log."""
    def transfer_exports():
        check = object.__new__(volume_common.OpenstackVolumeTransfer)
        check.log, check.shell = mock.Mock(), IdentityShell(failure)
        check._identified_path("dvol-1", "/dev/vdc")

    result = _transfer_module_failing_with(transfer_exports)

    assert result["_exit_code"] == 1 and result.get("failed"), result
    assert result["msg"] == message
    # what Ansible shows at the default verbosity, which the control plane classifies: neither the
    # traceback nor the module's arguments (its own `timeout` among them)
    hidden = ("exception", "invocation", "_exit_code", "_connected")
    shown = {key: value for key, value in result.items() if key not in hidden}
    assert "timeout" not in json.dumps(shown).lower(), shown
    if failure == "gone" and "exception" in result:
        # the cause stays for whoever reads the log: ansible-core 2.18, the production pin, returns
        # the traceback; 2.19 and later keep it for their own traceback display
        assert "ConnectTimeout" in result["exception"]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("SSH timeout: conversion host %s unreachable over SSH, checking the by-id "
                     "link of /dev/vdc" % CONV),
        subprocess.CalledProcessError(255, ["ssh", "-o", "ConnectTimeout=10", "cloud-user@" + CONV]),
        RuntimeError("Failed to convert volume! qemu-img: error while writing"),
    ],
    ids=["an SSH give-up", "ssh failing", "a failed copy"],
)
def test_the_transfers_other_errors_still_escape_its_module(error):
    """Review RV15-01: only an identity failure is caught. Every other error of the transfer
    leaves the module as before - its classification is the owner's question 8 - so an ssh
    give-up, or ssh's own failure, stays retried."""
    with pytest.raises(type(error)) as raised:
        _transfer_module_failing_with(error)

    assert raised.value is error


class LeavingDestination:
    """The destination conversion host of a cold transfer, its /dev on a real directory: the disk
    of the destination volume (a regular file standing for the block device /dev/vdb) and udev's
    by-id link to it. With ``leave`` "at the check" the volume leaves the host right after the
    identity check's `test -b` of its link - another actor detaches it - and udev removes the link;
    with "during the copy" it leaves once nbdcopy wrote into it; with None it stays (review RV15-02,
    RV16-02). The copy tools open their destination as their sources do: nbdcopy as libnbd's
    open_local - open(O_WRONLY), and when that fails open(O_WRONLY | O_TRUNC | O_CREAT | O_EXCL,
    0644) - and qemu-img's host_device with no O_CREAT. As on a real host `test -b` holds for the
    disk alone and `test -f` for a regular file, both following links. ``failing`` names the command
    whose ssh fails after the copy (exit 255) or, for "rm", whose sudo fails (exit 1); with
    ``dev_full`` /dev is RHEL 9's 4 MiB devtmpfs, so nbdcopy's write into a file it made fails and it
    exits 1, leaving the file, as libnbd's file_synch_write does; ``nbdcopy_exit`` makes it fail
    anyway (review RV16-02, RV16-03, RV16-06). ``leave="before the open"`` takes the disk at the
    identity check while udev keeps its by-id link: nbdcopy's open fails ENOENT and its create
    (O_CREAT | O_EXCL) EEXIST on the dangling link, so it exits 1 having written nothing, and udev
    removes the link by the check; ``failing="rm ssh"`` has ssh fail at the removal (review
    RV17-04)."""

    address = CONV

    def __init__(self, root, volume_id, leave="at the check", failing=None, dev_full=False,
                 nbdcopy_exit=0):
        self.root = root
        self.serial = volume_id[:20]
        self.disk = root / "dev" / "vdb"
        self.link = root / "dev" / "disk" / "by-id" / ("virtio-" + self.serial)
        self.link.parent.mkdir(parents=True)
        self.disk.write_bytes(bytes(4096))
        self.link.symlink_to("../../vdb")
        self.leave, self.failing, self.dev_full, self.nbdcopy_exit = leave, failing, dev_full, nbdcopy_exit
        self.left = False
        self.copied = False
        self.commands = []

    def _local(self, path):
        return self.root / path.lstrip("/")

    def _leaves(self):
        self.left = True
        if self.leave != "before the open":  # udev has not yet removed the link
            self.link.unlink()
        self.disk.unlink()

    def cmd_out(self, command, **kwargs):
        self.commands.append(list(command))
        if command == ["cat", "/sys/block/vdb/serial"]:
            if self.left:
                raise subprocess.CalledProcessError(1, command)
            return self.serial
        return ""  # qemu-img create of the overlay

    def cmd_val(self, command, **kwargs):
        self.commands.append(list(command))
        if command[:2] == ["test", "-b"]:
            if self.copied and self.failing == "test -b":
                return 255
            path = self._local(command[2])
            found = self.disk.exists() and os.path.realpath(path) == os.path.realpath(self.disk)
            if not self.left and self.leave in ("at the check", "before the open"):
                self._leaves()
            return 0 if found else 1
        if command[:2] == ["test", "-f"]:
            if self.failing == "test -f":
                return 255
            path = self._local(command[2])
            regular = path.is_file() and os.path.realpath(path) != os.path.realpath(self.disk)
            return 0 if regular else 1
        if command[:3] == ["sudo", "rm", "-f"]:
            if self.failing == "rm":
                return 1
            if self.failing == "rm ssh":
                return 255
            path = self._local(command[3])
            if path.exists() or path.is_symlink():
                path.unlink()
            return 0
        return 0  # rm -f of the overlay

    def cmd_sub(self, command, **kwargs):
        self.commands.append(list(command))
        if command[:2] == ["sudo", "nbdcopy"]:
            path = self._local(command[3])
            made = False
            try:
                fd = os.open(str(path), os.O_WRONLY)
            except OSError:
                try:
                    fd = os.open(str(path), os.O_WRONLY | os.O_TRUNC | os.O_CREAT | os.O_EXCL, 0o644)
                except OSError as err:  # the dangling link: nothing opened, nothing written
                    self.copied = True
                    self.link.unlink()  # udev catches up
                    return FailedProcess(1, "nbdcopy: %s: %s\n" % (command[3], err.strerror))
                made = True
            os.write(fd, b"the source volume's data")
            os.close(fd)
            self.copied = True
            if self.leave == "during the copy" and not self.left:
                self._leaves()
            if made and self.dev_full:
                return FailedProcess(1, "%s: No space left on device\n" % command[3])
            if self.nbdcopy_exit:
                return FailedProcess(self.nbdcopy_exit, "nbdcopy: write: Input/output error\n")
            return DoneProcess()
        if command[:3] == ["sudo", "qemu-img", "convert"]:
            try:
                os.close(os.open(str(self._local(command[-1])), os.O_RDWR))
            except OSError as err:
                return FailedProcess(1, "qemu-img: Could not open '%s': %s" % (command[-1], err))
            return DoneProcess()
        return DoneProcess()  # virt-sparsify


class FailedProcess:
    """A process that has ended with ``returncode``, ``output`` on its stdout."""

    def __init__(self, returncode, output):
        self.returncode = returncode
        read, write = os.pipe()
        os.write(write, output.encode())
        os.close(write)
        self.stdout = os.fdopen(read, "r")
        self.stderr = self.stdout

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode

    def communicate(self, data=None, timeout=None):
        return self.stdout.read(), ""


LEAVING_ID = "3b7a57d7-8210-47f9-b592-a6627ae52d13"


def _leaving_transfer(tmp_path, **host_args):
    host = LeavingDestination(tmp_path / "dst-conv", LEAVING_ID, **host_args)
    transfer = _copying_transfer(tmp_path, {})
    transfer.volume_map["/dev/vda"]["dest_id"] = LEAVING_ID
    transfer.shell = host
    return transfer, host


def test_nbdcopy_never_reports_a_destination_volume_that_left_the_host_as_written(tmp_path):
    """Review RV15-02: nbdcopy opens its destination again with O_CREAT when the first open fails,
    so a volume that left the host between the identity check and that open had its disk copied
    into a regular file nbdcopy created at the link's path, and the transfer reported success with
    the volume never written - on RHEL 8, whose devtmpfs takes half the RAM. The transfer now checks
    that the path is still a disk, removes the regular file, which would block udev's link, and
    fails as permanent (Seamless Migrate SDD 6.6)."""
    transfer, host = _leaving_transfer(tmp_path)

    with pytest.raises(volume_common.DiskIdentityError, match=LEAVING_ID):
        transfer._convert_destination_volumes_nbdcopy()

    assert not host.link.exists() and not host.link.is_symlink(), "the regular file stayed"
    assert transfer.volume_map["/dev/vda"]["progress"] != 100.0


def test_qemu_img_fails_when_the_destination_volume_left_the_host(tmp_path):
    """Control (review RV15-02): qemu-img's host_device opens its target with no O_CREAT, so the
    same sequence fails both converts - the sparsify attempt's and the whole-device fallback's -
    given the same checked link."""
    transfer, host = _leaving_transfer(tmp_path)

    with pytest.raises(RuntimeError, match="Could not open"):
        transfer._convert_destination_volumes()

    converts = [c for c in host.commands if c[:3] == ["sudo", "qemu-img", "convert"]]
    assert len(converts) == 2 and {c[-1] for c in converts} == {str("/dev/disk/by-id/virtio-" + LEAVING_ID[:20])}


def _regular_file_stayed(host):
    return host.link.is_file() and not host.link.is_symlink()


def test_nbdcopy_failing_in_the_file_it_made_leaves_no_file_and_names_the_cause(tmp_path):
    """Review RV16-02: on RHEL 9 /dev is a 4 MiB devtmpfs, so nbdcopy's copy into the regular file it
    made where the volume's link was runs out of room and nbdcopy exits 1. The transfer raised
    "Failed to copy volume with nbdcopy! Return code: 1" before its check ran: the file stayed in
    /dev/disk/by-id, where udev 252 will not make the link, and the error named nothing. The check
    now runs whatever nbdcopy's exit (Seamless Migrate SDD 6.6)."""
    transfer, host = _leaving_transfer(tmp_path, dev_full=True)

    with pytest.raises(volume_common.DiskIdentityError, match=LEAVING_ID):
        transfer._convert_destination_volumes_nbdcopy()

    assert not _regular_file_stayed(host), "the regular file stayed"


@pytest.mark.parametrize(
    "failing, said",
    [("test -f", "could not be checked"), ("rm", "could not be removed (sudo rm -f exited 1)")],
    ids=["ssh fails at test -f", "sudo rm fails"],
)
def test_the_check_after_nbdcopy_never_says_it_removed_a_file_it_left(tmp_path, failing, said):
    """Review RV16-03: the check ignored the results of its `test -f` and its `sudo rm -f`, and said
    "now removed" all the same, while nbdcopy's copy of the disk stayed in /dev. It now says what
    became of the file, and where to remove it by hand (Seamless Migrate SDD 6.6)."""
    transfer, host = _leaving_transfer(tmp_path, failing=failing)

    with pytest.raises(volume_common.DiskIdentityError) as raised:
        transfer._convert_destination_volumes_nbdcopy()

    message = str(raised.value)
    assert _regular_file_stayed(host)
    assert "now removed" not in message and said in message, message
    assert "/dev/disk/by-id/virtio-" + LEAVING_ID[:20] in message and CONV in message, message


@pytest.mark.parametrize("nbdcopy_exit", [0, 255], ids=["a whole copy", "ssh broke the copy"])
def test_ssh_failing_at_the_check_after_nbdcopy_gives_up_as_ssh_timeout(tmp_path, nbdcopy_exit):
    """Review RV16-06: ssh failing (255) at the check after nbdcopy is no proof the volume left: the
    check gives up as "SSH timeout: ...", which the control plane retries, and removes nothing - a
    whole, good copy is never failed as permanent for an ssh blip, nor a copy ssh broke (Seamless
    Migrate SDD 6.6)."""
    transfer, host = _leaving_transfer(
        tmp_path, leave=None, failing="test -b", nbdcopy_exit=nbdcopy_exit
    )

    with pytest.raises(RuntimeError) as raised:
        transfer._convert_destination_volumes_nbdcopy()

    assert not isinstance(raised.value, volume_common.DiskIdentityError), raised.value
    assert str(raised.value).startswith("SSH timeout:"), raised.value
    assert not [c for c in host.commands if c[:3] == ["sudo", "rm", "-f"]]


#: what Seamless Migrate's control plane retries (its classify_failure, executors/ansible.py)
TRANSIENT = re.compile(r"(?i)timeout|http 503|connection reset")


@pytest.mark.parametrize("leave", [None, "at the check"], ids=["the volume stays", "it left before the copy"])
def test_a_failed_copy_stays_permanent_when_ssh_fails_at_the_check_after_it(tmp_path, leave):
    """Review RV17-01: after nbdcopy failed on its own (exit 1) every answer the check could give is
    permanent, yet an ssh failure at its `test -b` made the transfer "SSH timeout: ...", retried -
    whole new copies, a cold source stopped meanwhile - with nbdcopy's failure named nowhere. The
    transfer keeps nbdcopy's failure and says the path could not be checked (Seamless Migrate SDD
    6.6)."""
    transfer = _leaving_transfer(tmp_path, leave=leave, failing="test -b", nbdcopy_exit=1)[0]

    with pytest.raises(RuntimeError) as raised:
        transfer._convert_destination_volumes_nbdcopy()

    message = str(raised.value)
    assert not TRANSIENT.search(message), message
    assert message.startswith("Failed to copy volume with nbdcopy! Return code: 1"), message
    assert "could not be checked (ssh failed)" in message, message
    assert "/dev/disk/by-id/virtio-" + LEAVING_ID[:20] in message and CONV in message, message


def test_a_failed_copy_onto_a_disk_still_there_keeps_nbdcopys_own_error(tmp_path):
    """Review RV16-02: the check runs after a failed copy too, and a path that is still the volume's
    disk leaves nbdcopy's own failure to stand (Seamless Migrate SDD 6.6)."""
    transfer, host = _leaving_transfer(tmp_path, leave=None, nbdcopy_exit=1)

    with pytest.raises(RuntimeError, match="Failed to copy volume with nbdcopy! Return code: 1") as raised:
        transfer._convert_destination_volumes_nbdcopy()

    assert not isinstance(raised.value, volume_common.DiskIdentityError)
    copy = next(i for i, c in enumerate(host.commands) if c[:2] == ["sudo", "nbdcopy"])
    assert ["test", "-b", "/dev/disk/by-id/virtio-" + LEAVING_ID[:20]] in host.commands[copy:]


def test_a_volume_that_left_after_nbdcopy_finished_is_named_as_such(tmp_path):
    """Reviews RV16-03, RV17-04: with no regular file at the path and nbdcopy's exit 0, every write
    succeeded and the volume left after the copy - the error says so, claims no file, and never
    that the volume was not written whole (Seamless Migrate SDD 6.6)."""
    transfer, host = _leaving_transfer(tmp_path, leave="during the copy")

    with pytest.raises(volume_common.DiskIdentityError) as raised:
        transfer._convert_destination_volumes_nbdcopy()

    message = str(raised.value)
    assert "left the host after nbdcopy finished" in message, message
    assert "regular file" not in message and "not written whole" not in message, message
    assert not [c for c in host.commands if c[:3] == ["sudo", "rm", "-f"]]


def test_a_volume_nbdcopy_never_opened_is_named_by_nbdcopys_exit(tmp_path):
    """Review RV17-04: the volume left at the identity check while udev kept its by-id link;
    nbdcopy opened nothing, wrote nothing and exited 1. The error names nbdcopy's exit and never a
    copy that did not happen (Seamless Migrate SDD 6.6)."""
    transfer, host = _leaving_transfer(tmp_path, leave="before the open")

    with pytest.raises(volume_common.DiskIdentityError) as raised:
        transfer._convert_destination_volumes_nbdcopy()

    message = str(raised.value)
    assert "nbdcopy exited 1" in message and "during the copy" not in message, message
    assert not TRANSIENT.search(message), message


def test_an_ssh_failure_at_the_removal_is_not_reported_as_rms_own_exit(tmp_path):
    """Review RV17-04: ssh's 255 at `sudo rm -f` says nothing of rm's own exit - the connection may
    have dropped after rm ran: the error says ssh failed at the removal and asks to check the path,
    never "sudo rm -f exited 255" (Seamless Migrate SDD 6.6)."""
    transfer = _leaving_transfer(tmp_path, failing="rm ssh")[0]

    with pytest.raises(volume_common.DiskIdentityError) as raised:
        transfer._convert_destination_volumes_nbdcopy()

    message = str(raised.value)
    assert "exited 255" not in message, message
    assert "ssh failed at the removal" in message, message
    assert "/dev/disk/by-id/virtio-" + LEAVING_ID[:20] in message and CONV in message, message
