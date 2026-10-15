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
