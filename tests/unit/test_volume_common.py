from __future__ import absolute_import, division, print_function

__metaclass__ = type

import io
import json
import logging
import os
import re
import subprocess
import time
import traceback
from unittest import mock

import pytest
from openstack import exceptions as sdk_exceptions

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
    server,
    volume_common,
)
from ansible_collections.os_migrate.os_migrate.plugins.modules import (
    import_workload_export_volumes as export_module,
)
from ansible_collections.os_migrate.os_migrate.plugins.modules import (
    import_workload_transfer_volumes as transfer_module,
)
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake
from ansible_collections.os_migrate.os_migrate.tests.unit import local_sshd

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


def _exporter(nbdkit_installed, val=None):
    src = fake.FakeCloud("src")
    conv = src.add_conversion_host("src-conv", "10.0.0.10")
    # the volume the map names is on the conversion host, at /dev/vdb: the export checks the
    # disk's identity before it serves it (Seamless Migrate SDD 6.6, review RV13-02)
    src.attach_volume(conv, src.add_volume("vol-1", "vm1-root", 10))

    def which(command):
        if command[0] == "which":
            return 0 if command[1] == "qemu-nbd" or nbdkit_installed else 1
        return None

    shells = fake.ShellFactory([src], val=val or which)
    export = volume_common.OpenstackVolumeExport(
        src, "src-conv", "/keys/k", "cloud-user", "uuid-1", shell_factory=shells
    )
    export.volume_map = {
        "/dev/vda": {"source_id": "vol-1", "source_dev": "/dev/vdb", "port": None}
    }
    return export, shells


PROBES = {
    "which qemu-nbd": ["which", "qemu-nbd"],
    "which nbdkit": ["which", "nbdkit"],
    "the file plugin": ["nbdkit", "--dump-plugin", "file"],
}


@pytest.mark.parametrize("status", [255, -9], ids=["ssh failed", "ssh killed"])
@pytest.mark.parametrize("probe", list(PROBES))
def test_ssh_failing_at_an_export_tool_probe_is_an_ssh_timeout_never_a_missing_tool(probe, status):
    """Seamless Migrate SDD 6.6 (review 45b, lead 4): the cold export probes for its NBD server -
    which qemu-nbd, which nbdkit, nbdkit's file plugin. Ssh's own failure at a probe (255, or a
    negative status: ssh killed on the migrator) read as the tool missing: the export picked the
    other tool on a guess, and with ssh failing at all three it failed as "No supported NBD export
    tool available!", permanent. It gives up as an ssh timeout, retried."""
    def probes(command):
        if command == PROBES[probe]:
            return status
        return 0 if command in PROBES.values() else None

    export, shells = _exporter(nbdkit_installed=True, val=probes)
    with pytest.raises(RuntimeError, match=r"SSH timeout: conversion host 10\.0\.0\.10 unreachable over SSH"):
        export._export_volumes_from_converter()
    started = [c for c in shells.commands_at("10.0.0.10") if c[:2] in (["sudo", "nbdkit"], ["sudo", "qemu-nbd"])]
    assert started == []  # no tool picked on a guess


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


@pytest.mark.parametrize(
    ("command", "status", "message"),
    [
        (["sudo", "nbdkit", "--readonly", "file", "file=/dev/vdb"], 1, "nbdkit"),
        (['pgrep -f "ssh -o ConnectTimeout=10 -L 10809:localhost:10809 h"'], 1, "pgrep"),
        (["sudo", "nbdkit", "--readonly", "file", "file=/dev/vdb"], 255, None),
        (["sudo", "nbdkit", "--readonly", "file", "file=/dev/vdb"], -9, None),
        (["sudo", "nbdkit", "--readonly", "file", "file=/dev/vdb"], -15, None),
    ],
    ids=[
        "nbdkit failing",
        "a pattern that holds an ssh command",
        "ssh failing",
        "ssh killed by SIGKILL",
        "ssh killed by SIGTERM",
    ],
)
def test_a_remote_command_failing_by_itself_names_its_program_not_sshs_options(
    command, status, message
):
    """Seamless Migrate SDD 6.6 (reviews RV42B-02, RV43B-05): a command on the conversion host that
    fails by itself - a positive exit status - names its program and exit status - ssh's command
    line, ConnectTimeout among its options, read as a "timeout" the control plane retried with the
    source VM stopped -, while ssh's own failure (255), and ssh killed by a signal on this host (a
    negative status: no remote command failed), is raised as subprocess raises it, its command
    line kept."""
    shell = volume_common.RemoteShell("10.0.0.10", "cloud-user", "/keys/k")

    def ssh_exits(args, **kwargs):
        raise subprocess.CalledProcessError(status, args, output=b"partial")

    with mock.patch.object(volume_common.subprocess, "check_output", ssh_exits):
        with pytest.raises(subprocess.CalledProcessError) as caught:
            shell.cmd_out(command)
    err = caught.value
    assert (err.returncode, err.output) == (status, b"partial")
    if message is None:
        assert not isinstance(err, volume_common.RemoteCommandFailed)
        assert "ConnectTimeout=10" in str(err)
    else:
        assert isinstance(err, volume_common.RemoteCommandFailed) and err.cmd == command
        assert str(err) == f"{message} on the conversion host exited with status {status}"
        assert err.__suppress_context__  # the ssh command line goes with no traceback either


@pytest.mark.parametrize("status", [1, 255, -9], ids=["failing by itself", "ssh failing", "ssh killed"])
def test_a_remote_command_failing_by_itself_is_logged_in_full(caplog, status):
    """Seamless Migrate SDD 6.6 (review RV43B-06): its message names only the program, so the
    module's log keeps the full command that failed by itself - the pattern, the URL, the lock
    path the callers' own lines no longer carry -; ssh's own failure keeps it in its message."""
    shell = volume_common.RemoteShell("10.0.0.10", "cloud-user", "/keys/k")
    command = ["pgrep", "-f", "nbdkit --exportname transfer-1"]

    def ssh_exits(args, **kwargs):
        raise subprocess.CalledProcessError(status, args)

    caplog.set_level(logging.DEBUG, logger="osp-osp")
    with mock.patch.object(volume_common.subprocess, "check_output", ssh_exits):
        with pytest.raises(subprocess.CalledProcessError):
            shell.cmd_out(command)
    logged = [r.getMessage() for r in caplog.records if r.name == "osp-osp"]
    if status > 0 and status != volume_common.SSH_FAILED:
        assert logged == [
            "pgrep -f nbdkit --exportname transfer-1 on the conversion host 10.0.0.10 exited "
            "with status 1"
        ]
    else:
        assert logged == []


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


class ForwardingHost(PortShell):
    """A destination conversion host whose forwarded exports answer at once."""

    def cmd_sub(self, command, **kwargs):
        if "-L" in command:
            self.commands.append(list(command))
            return mock.Mock()
        return super().cmd_sub(command, **kwargs)


def test_the_transfers_forward_logs_in_to_the_source_host_as_its_own_user():
    """Seamless Migrate SDD 6.5 (audit EX-08): the destination host (ubuntu) forwards the source
    host's NBD exports over SSH, logging in to the source host as the source's user (cloud-user),
    not as its own."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_transfer_volumes as transfer_module,
    )

    src, dst = fake.FakeCloud("src"), cloud()
    src.add_conversion_host("src-conv", "10.0.0.10")
    volume_map = {"/dev/vda": dict(cold_volume_map()["/dev/vda"], port=10809)}
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = transfer_module.OpenStackDestinationVolume(
            dst, "dst-conv", "/keys/k", "ubuntu", "uuid-1", "10.0.0.10", volume_map,
            workload(), timeout=1800, src_ssh_user="cloud-user",
        )
    transfer.shell = ForwardingHost(free=2)
    transfer.shell.ssh_user = "ubuntu"  # the destination host, reached as its own user
    transfer._create_forwarding_process()
    [forward] = [c for c in transfer.shell.commands if "-L" in c]
    assert "cloud-user@10.0.0.10" in forward and "ubuntu@10.0.0.10" not in forward


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
        (-9, "SSH timeout: conversion host %s unreachable over SSH, reading the port map" % CONV),
        (1, "Unable to read the port map"),
    ],
    ids=["ssh failing", "ssh killed by a signal", "cat failing"],
)
def test_a_port_map_read_that_fails_gives_up_with_its_own_error(status, message):
    """Audit A-17: the read of the port map under the port lock fell through to an unbound
    variable when the command failed, so a transient ssh failure between taking the lock and the
    read surfaced as a Python error instead of the "SSH timeout" give-up the control plane retries
    (SDD 6.6); any other failure of the read names the port map. Neither claims a port. ssh killed
    by a signal on the migrator (a negative status) is ssh's failure too (review RV44B-03)."""
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


@pytest.mark.parametrize("status", [volume_common.SSH_FAILED, -9], ids=["ssh failing", "ssh killed"])
def test_ssh_failing_at_the_port_map_init_gives_up_as_an_ssh_timeout(status):
    """Seamless Migrate SDD 6.6 (review RV44B-03): the port map's initialisation under the port
    lock turned any failure of its ssh into "Unable to initialize port map file!" - permanent -
    where the read right after it gives ssh's own failure up as "SSH timeout", which the control
    plane retries."""

    class InitFails(PortMapGone):
        def cmd_out(self, command, **kwargs):
            if command[:3] == ["sudo", "bash", "-c"] and "test -e" in command[3]:
                raise subprocess.CalledProcessError(self.status, ["ssh", self.address] + list(command))
            return super().cmd_out(command, **kwargs)

    dst = cloud()
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst])):
        transfer = volume_common.OpenstackVolumeTransfer(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1"
        )
    transfer.shell = InitFails(status)
    with pytest.raises(RuntimeError, match="SSH timeout: conversion host %s unreachable over SSH" % CONV):
        transfer._find_free_port()
    assert transfer.claimed_ports == []


def test_ssh_killed_at_the_post_copy_disk_check_is_no_volume_that_left():
    """Seamless Migrate SDD 6.6 (review RV44B-03): after a whole copy (nbdcopy exit 0) ssh failing
    at the check that the path is still a disk gives up as "SSH timeout"; subprocess.call answers
    a killed ssh with a negative status, which the check read as the volume gone - permanent, on
    the cold path with the source stopped."""

    class KilledAtTheCheck:
        address = CONV

        def cmd_val(self, command, **kwargs):
            if command[:2] == ["test", "-b"]:
                return -9  # ssh killed by SIGKILL on the migrator
            return 1  # test -f: no regular file there

    check = object.__new__(volume_common.OpenstackVolumeTransfer)
    check.log, check.shell = mock.Mock(), KilledAtTheCheck()
    with pytest.raises(RuntimeError) as caught:
        check._check_still_a_disk("dvol-1", "/dev/vdc", 0)
    assert not isinstance(caught.value, volume_common.DiskIdentityError), str(caught.value)
    assert "SSH timeout: conversion host %s unreachable over SSH" % CONV in str(caught.value)


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


def test_ssh_killed_at_the_serial_read_is_no_disk_that_left_the_host():
    """Seamless Migrate SDD 6.6 (review RV44B-03): ssh failing at the identity check is no failure
    of the disk and is retried as transient - and so is ssh killed by a signal on the migrator: the
    real RemoteShell.cmd_out re-raises the negative status as ssh's own failure, its command line
    kept; the check read it as cat failing - the disk left the host, permanent."""

    def killed(args, **kwargs):
        raise subprocess.CalledProcessError(-9, args)

    def transfer_exports():
        check = object.__new__(volume_common.OpenstackVolumeTransfer)
        check.log = mock.Mock()
        check.shell = volume_common.RemoteShell(CONV, "cloud-user", "/keys/k")
        with mock.patch.object(volume_common.subprocess, "check_output", killed):
            check._identified_path("dvol-1", "/dev/vdc")

    # ssh's own failure leaves the module as before, never as an identity failure's message: the
    # escaped error keeps ssh's command line, ConnectTimeout and all, which the control plane retries
    with pytest.raises(subprocess.CalledProcessError) as raised:
        _transfer_module_failing_with(transfer_exports)
    assert not isinstance(raised.value, volume_common.RemoteCommandFailed)
    assert raised.value.returncode == -9 and "ConnectTimeout=10" in str(raised.value)


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


# --- ssh killed on the migrator at every site that tells ssh's failure from the command's -----------
# (Seamless Migrate SDD 6.6, reviews RV44B-03, RV45B-03): each test pins one site, which its 255-only
# form (before 0577c573) fails.
KILLED = -9  # subprocess's status for ssh killed by SIGKILL on the migrator


def _bare(cls, shell):
    obj = object.__new__(cls)
    obj.log, obj.shell = mock.Mock(), shell
    return obj


def test_a_lock_wait_gives_up_as_a_lock_timeout_when_ssh_is_killed_at_every_try():
    """use_lock's outage counter: a killed ssh at every try is the outage window's, given up as
    "Lock timeout" (retried), never 1800 tries ending in "Unable to acquire lock" (permanent)."""
    transfer, clock, guarded = _guarded_attach([KILLED] * 1800)
    with mock.patch.object(volume_common, "time", clock):
        with pytest.raises(RuntimeError, match=r"Lock timeout: conversion host 10\.1\.0\.10 unreachable"):
            guarded(transfer)


def test_ssh_killed_at_the_by_id_link_check_gives_up_as_an_ssh_timeout():
    """_identified_path: the by-id link check with ssh killed gives up as "SSH timeout" - never
    "has no link: using it by its path", the disk then served by a path another volume can take."""

    class Host:
        address = CONV

        def cmd_out(self, command, **kwargs):
            return "dvol-1"  # the disk's serial: the volume's id

        def cmd_val(self, command, **kwargs):
            return KILLED  # test -b /dev/disk/by-id/virtio-dvol-1

    check = _bare(volume_common.OpenstackVolumeTransfer, Host())
    with pytest.raises(RuntimeError, match="SSH timeout: conversion host %s unreachable over SSH, checking the by-id link" % CONV):
        check._identified_path("dvol-1", "/dev/vdc")


def test_the_port_probe_counts_a_killed_ssh_as_the_outage():
    """_test_port_available: a killed ssh is no port in use - the probe's outage window sees it."""

    class Host(PortShell):
        def cmd_val(self, command, **kwargs):
            return KILLED

    probe = _bare(volume_common.OpenstackVolumeTransfer, Host(free=1))
    outage = volume_common.SshOutage(CONV, "a free port")
    assert probe._test_port_available(49152, outage) is False
    assert outage.failing


class KilledSilentHost(SilentHost):
    """SilentHost whose ssh is killed (-9) at every `qemu-img info` instead of failing (255)."""

    def cmd_out(self, command, **kwargs):
        if command[:2] == ["qemu-img", "info"]:
            self.clock.now += 10
            raise subprocess.CalledProcessError(KILLED, ["ssh", self.address] + list(command))
        return PortShell.cmd_out(self, command, **kwargs)


@pytest.mark.parametrize("waits_for", ["the forwarded exports", "an nbdkit socket"])
def test_a_transfers_wait_gives_up_after_the_window_when_ssh_is_killed(waits_for):
    """The transfer's waits for the forwarded exports and for an nbdkit socket count a killed ssh
    as the outage and give up as "SSH timeout" after the window, never after 1800 tries as
    "Timed out ..." (no "timeout" the classifier reads as transient)."""
    src, dst = fake.FakeCloud("src"), cloud()
    src.add_conversion_host("src-conv", "10.0.0.10")
    volume_map = {"/dev/vda": dict(cold_volume_map()["/dev/vda"], port=10809)}
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = transfer_module.OpenStackDestinationVolume(
            dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1", "10.0.0.10", volume_map,
            workload(), timeout=1800,
        )  # fmt: skip
    clock = Clock()
    transfer.shell = KilledSilentHost(clock)
    with mock.patch.object(volume_common, "time", clock):
        with pytest.raises(RuntimeError, match="SSH timeout: conversion host %s unreachable" % CONV):
            if waits_for == "an nbdkit socket":
                transfer._setup_nbdkit_direct_urls("nbd+unix:///?socket=/run/nbd.sock", "disk1")
            else:
                transfer._create_forwarding_process()


def test_the_exports_wait_gives_up_after_the_window_when_ssh_is_killed():
    """The export's wait for its NBD exports counts a killed ssh as the outage: "SSH timeout"
    after the window, never "Timed out starting nbdkit exports!" after 1800 tries."""
    from ansible_collections.os_migrate.os_migrate.tests.unit.test_conversion_host_outage import (
        COMMON,
        RebootingHost,
        _boot_from_volume_vm,
        _run,
    )

    class KilledRebootingHost(RebootingHost):
        """RebootingHost whose ssh is killed (-9) while the host is down, instead of failing (255)."""

        def __call__(self, address, ssh_user, key_path=None):
            shell = super().__call__(address, ssh_user, key_path)
            out, val = shell.cmd_out, shell.cmd_val

            def cmd_out(command, **kwargs):
                try:
                    return out(command, **kwargs)
                except subprocess.CalledProcessError as err:
                    if err.returncode == 255:
                        raise subprocess.CalledProcessError(KILLED, err.cmd) from None
                    raise

            def cmd_val(command, **kwargs):
                status = val(command, **kwargs)
                return KILLED if status == 255 else status

            shell.cmd_out, shell.cmd_val = cmd_out, cmd_val
            return shell

    src, data = _boot_from_volume_vm()
    clock = Clock()
    host = KilledRebootingHost(src, clock, None, 10 ** 9, starts=lambda command: command[:2] == ["qemu-img", "info"])
    host.armed = True
    with mock.patch.object(volume_common, "time", clock):
        exported = _run(export_module, dict(COMMON, data=data, timeout=1800), src, host)
    assert exported.get("failed"), exported
    assert "SSH timeout: conversion host 10.0.0.10 unreachable over SSH" in exported["msg"], exported


class CheckHost:
    """The conversion host as _check_still_a_disk meets it: each command's status in turn."""

    address = CONV

    def __init__(self, **status):
        self.status = status

    def cmd_val(self, command, **kwargs):
        return self.status[{"-b": "b", "-f": "f"}.get(command[1], "rm")]


def test_ssh_killed_at_the_check_after_a_copy_ssh_killed_gives_up_as_an_ssh_timeout():
    """nbdcopy's ssh killed (-9) and ssh killed again at `test -b`: ssh broke both - "SSH
    timeout", retried, never nbdcopy's own failure "Return code: -9", permanent."""
    check = _bare(volume_common.OpenstackVolumeTransfer, CheckHost(b=KILLED))
    with pytest.raises(RuntimeError) as caught:
        check._check_still_a_disk("dvol-1", "/dev/vdc", KILLED)
    assert str(caught.value).startswith("SSH timeout:"), str(caught.value)


def test_ssh_killed_at_the_removal_of_a_file_left_is_said_as_such():
    """A regular file where the volume was, ssh killed at `sudo rm -f`: "ssh failed at the
    removal", never "sudo rm -f exited -9"."""
    check = _bare(volume_common.OpenstackVolumeTransfer, CheckHost(b=1, f=0, rm=KILLED))
    with pytest.raises(volume_common.DiskIdentityError) as caught:
        check._check_still_a_disk("dvol-1", "/dev/vdc", 0)
    assert "ssh failed at the removal" in str(caught.value), str(caught.value)


def test_ssh_killed_at_the_file_check_is_said_as_could_not_be_checked():
    """Not a disk, ssh killed at `test -f`: whether a file was left "could not be checked (ssh
    failed)", never "left the host after nbdcopy finished"."""
    check = _bare(volume_common.OpenstackVolumeTransfer, CheckHost(b=1, f=KILLED))
    with pytest.raises(volume_common.DiskIdentityError) as caught:
        check._check_still_a_disk("dvol-1", "/dev/vdc", 0)
    assert "could not be checked (ssh failed)" in str(caught.value), str(caught.value)


def test_the_source_cleanup_rides_out_a_killed_ssh():
    """OpenstackVolumeClean._check_ssh: ssh killed at the check is the outage it rides out, never
    a failure raised at once."""

    class Host:
        address = CONV
        tries = 0

        def test_ssh_connection(self):
            self.tries += 1
            if self.tries < 3:
                raise subprocess.CalledProcessError(KILLED, ["ssh", CONV, "echo connected"])

    clean = _bare(volume_common.OpenstackVolumeClean, Host())
    with mock.patch.object(volume_common, "time", Clock()):
        clean._check_ssh()
    assert clean.shell.tries == 3


# --- pinned host keys (Seamless Migrate SDD 6.6, 26.4, 26.5; plan task ST3) ----------------------

SRC_ALIAS = "os_migrate_conv_src"
DST_ALIAS = "os_migrate_conv_dst"

#: upstream's options for a shell given the key /keys/k, which a shell without a known_hosts file
#: keeps exactly
UPSTREAM_OPTIONS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
    "-i", "/keys/k",
]

needs_sshd = pytest.mark.skipif(
    local_sshd.SSHD is None, reason="needs an sshd binary (openssh-server): SDD 26.9's real ssh"
)


def ssh_options(argv):
    """The -o options of an ssh or scp command line: each name and the values it was given."""
    options = {}
    for flag, value in zip(argv, argv[1:]):
        if flag == "-o":
            name, _sep, setting = value.partition("=")
            options.setdefault(name, []).append(setting)
    return options


class CommandLines:
    """subprocess's check_output, call and Popen as RemoteShell runs them: each command line and
    the keywords it was given, recorded, nothing run."""

    def __init__(self, monkeypatch):
        self.argvs = []
        self.kwargs = []
        for name, answer in (("check_output", b"done"), ("call", 0), ("Popen", None)):
            monkeypatch.setattr(volume_common.subprocess, name, self._recorder(answer))

    def _recorder(self, answer):
        def run(args, **kwargs):
            self.argvs.append(list(args))
            self.kwargs.append(kwargs)
            return answer

        return run

    def clear(self):
        del self.argvs[:]
        del self.kwargs[:]


def every_command(shell):
    """One of each: a command's output, its status, a long process, a copy each way."""
    shell.cmd_out(["echo", "connected"])
    shell.cmd_val(["true"])
    shell.cmd_sub(["sleep", "1"])
    shell.scp_to("/tmp/a", "/tmp/b")
    shell.scp_from("/tmp/b", "/tmp/a", recursive=True)


def test_a_pinned_shell_passes_strict_host_key_options_to_ssh_and_scp(tmp_path, monkeypatch):
    """Seamless Migrate SDD 6.6, 26.4 (plan task ST3): given a known_hosts file, every command and
    copy of a conversion host's RemoteShell - ssh and scp alike - checks the host's key strictly,
    under its side's alias, against that file alone, with the algorithms of the keys the file pins
    under that alias (an RSA key's as SHA-2), beside the options it always had - and without
    upstream's StrictHostKeyChecking=no."""
    keys = {
        name: local_sshd.keygen(str(tmp_path / name), key_type)
        for name, key_type in (("src-ed25519", "ed25519"), ("src-rsa", "rsa"), ("dst", "ecdsa"))
    }
    known_hosts = local_sshd.write_known_hosts(
        str(tmp_path / "known_hosts"),
        [(SRC_ALIAS, keys["src-ed25519"]), (DST_ALIAS, keys["dst"]), (SRC_ALIAS, keys["src-rsa"])],
    )
    lines = CommandLines(monkeypatch)
    for side, alias, algorithms in (
        ("source", SRC_ALIAS, "ssh-ed25519,rsa-sha2-512,rsa-sha2-256"),
        ("destination", DST_ALIAS, "ecdsa-sha2-nistp256"),
    ):
        lines.clear()
        every_command(
            volume_common.RemoteShell(
                "10.0.0.10", "cloud-user", "/keys/k", known_hosts=known_hosts, side=side,
                server="srv-1",
            )
        )
        assert [argv[0] for argv in lines.argvs] == ["ssh", "ssh", "ssh", "scp", "scp"]
        for argv in lines.argvs:
            assert ssh_options(argv) == {
                "BatchMode": ["yes"],
                "StrictHostKeyChecking": ["yes"],
                "UserKnownHostsFile": [known_hosts],
                "GlobalKnownHostsFile": ["/dev/null"],
                "HostKeyAlias": [alias],
                "CheckHostIP": ["no"],
                "UpdateHostKeys": ["no"],
                "VerifyHostKeyDNS": ["no"],
                "HashKnownHosts": ["yes"],
                "HostKeyAlgorithms": [algorithms],
                "IdentitiesOnly": ["yes"],
                "ConnectTimeout": ["10"],
                "ServerAliveInterval": ["15"],
                "ServerAliveCountMax": ["4"],
            }, argv
            assert argv[argv.index("-i") + 1] == "/keys/k"
        assert lines.argvs[0][-3:] == ["cloud-user@10.0.0.10", "echo", "connected"]
        assert lines.argvs[3][-2:] == ["/tmp/a", "cloud-user@10.0.0.10:/tmp/b"]
        assert lines.argvs[4][-3:] == ["-r", "cloud-user@10.0.0.10:/tmp/b", "/tmp/a"]


def test_a_shell_without_a_known_hosts_file_keeps_upstreams_options(monkeypatch):
    """Seamless Migrate SDD 6.6, 26.4 (plan task ST3): without a known_hosts file - none, or an
    empty name - a RemoteShell runs ssh and scp exactly as upstream does, StrictHostKeyChecking=no
    among its options and their stderr left alone; a module without ssh_known_hosts builds its
    shell with upstream's three arguments."""
    lines = CommandLines(monkeypatch)
    target = "cloud-user@10.0.0.10"
    for shell in (
        volume_common.RemoteShell("10.0.0.10", "cloud-user", "/keys/k"),
        volume_common.RemoteShell(
            "10.0.0.10", "cloud-user", "/keys/k", known_hosts="", side="source", server="srv-1"
        ),
    ):
        lines.clear()
        every_command(shell)
        assert lines.argvs == [
            ["ssh"] + UPSTREAM_OPTIONS + [target, "echo", "connected"],
            ["ssh"] + UPSTREAM_OPTIONS + [target, "true"],
            ["ssh"] + UPSTREAM_OPTIONS + [target, "sleep", "1"],
            ["scp"] + UPSTREAM_OPTIONS + ["/tmp/a", target + ":/tmp/b"],
            ["scp"] + UPSTREAM_OPTIONS + ["-r", target + ":/tmp/b", "/tmp/a"],
        ]
        assert lines.kwargs == [{}] * 5

    src = fake.FakeCloud("src")
    src.add_conversion_host("src-conv", "10.0.0.10")
    built = []

    def shells(*args, **kwargs):
        built.append((args, kwargs))
        return fake.ShellFactory([src])(*args, **kwargs)

    volume_common.OpenstackVolumeClean(
        src, "src-conv", "/keys/k", "cloud-user", "uuid-1", shell_factory=shells
    )
    assert built == [(("10.0.0.10", "cloud-user", "/keys/k"), {})]


@pytest.fixture
def sshd():
    server = local_sshd.LocalSshd()
    try:
        yield server
    finally:
        server.stop()


def shells_of(server):
    """RemoteShell reaching ``server``'s port. A conversion host listens on 22, which the shell
    never names: the test adds ssh's Port option after the shell's own."""

    class Shell(volume_common.RemoteShell):
        def _default_options(self):
            return super(Shell, self)._default_options() + ["-o", "Port=%d" % server.port]

    return Shell


def conversion_host_at_127_0_0_1():
    src = fake.FakeCloud("src")
    src.add_conversion_host("src-conv", "127.0.0.1")
    return src


@needs_sshd
def test_a_pinned_key_connects_to_a_real_sshd(sshd, tmp_path):
    """Seamless Migrate SDD 6.6, 26.9 (plan task ST3): a real sshd whose key the file pins, hashed,
    under the shell's alias - beside another alias's entry and an RSA key of the same alias, which
    only widen HostKeyAlgorithms - takes the shell's commands and copies and a module's SSH check,
    and ssh writes nothing beside or into the file."""
    other = local_sshd.keygen(str(tmp_path / "other"), "rsa")
    known_hosts = local_sshd.write_known_hosts(
        str(tmp_path / "known_hosts"),
        [(DST_ALIAS, other), (SRC_ALIAS, sshd.host_keys["ed25519"]), (SRC_ALIAS, other)],
    )
    with open(known_hosts, "rb") as f:
        pinned = f.read()
    shells = shells_of(sshd)
    shell = shells(
        "127.0.0.1", sshd.user, sshd.client_key, known_hosts=known_hosts, side="source",
        server="src-conv",
    )

    assert shell.cmd_out(["echo", "connected"]) == "connected"
    assert shell.cmd_val(["true"]) == 0
    assert shell.cmd_val(["false"]) == 1  # the remote command's own status: no refusal
    (tmp_path / "payload").write_text("blocksync\n")
    assert shell.scp_to(str(tmp_path / "payload"), str(tmp_path / "copied")) == 0
    assert shell.scp_from(str(tmp_path / "copied"), str(tmp_path / "back")) == 0
    assert (tmp_path / "back").read_text() == "blocksync\n"

    clean = volume_common.OpenstackVolumeClean(
        conversion_host_at_127_0_0_1(), "src-conv", sshd.client_key, sshd.user, "uuid-1",
        shell_factory=shells, ssh_known_hosts=known_hosts,
    )
    assert (clean.shell.alias, clean.shell.server) == (SRC_ALIAS, "src-conv")
    with open(known_hosts, "rb") as f:
        assert f.read() == pinned
    assert sorted(os.listdir(str(tmp_path))) == [
        "back", "copied", "known_hosts", "other", "other.pub", "payload"
    ]


@needs_sshd
@pytest.mark.parametrize(
    ("pinned_type", "offered"),
    [
        ("ed25519", "the ED25519 key {fingerprint}"),
        ("ecdsa", "no key of a pinned type (it offered ssh-ed25519)"),
    ],
    ids=["another key of its type", "a key of another type"],
)
def test_another_key_is_refused_at_once_without_an_outage_wait(
    sshd, tmp_path, monkeypatch, pinned_type, offered
):
    """Seamless Migrate SDD 6.6, 26.5 (plan task ST3): a real sshd that presents another key than
    the one pinned - of the pinned key's type, or of a type the file pins none of, which
    HostKeyAlgorithms makes ssh refuse - is refused at once and for good. The source cleanup's SSH
    check, which rides out an unreachable host for 15 minutes, raises HostKeyRefused without one
    wait, and so do a command whose status a lock wait reads and a copy: never ssh's 255, which a
    wait takes for an outage."""
    other = local_sshd.keygen(str(tmp_path / "other"), pinned_type)
    known_hosts = local_sshd.write_known_hosts(str(tmp_path / "known_hosts"), [(SRC_ALIAS, other)])
    shells = shells_of(sshd)
    waits = []
    monkeypatch.setattr(volume_common.time, "sleep", waits.append)

    with pytest.raises(volume_common.HostKeyRefused) as refused:
        volume_common.OpenstackVolumeClean(
            conversion_host_at_127_0_0_1(), "src-conv", sshd.client_key, sshd.user, "uuid-1",
            shell_factory=shells, ssh_known_hosts=known_hosts,
        )
    assert waits == []
    presented = offered.format(fingerprint=local_sshd.fingerprint(sshd.host_key_path()))
    assert " presented %s, not its pinned key " % presented in str(refused.value)

    shell = shells("127.0.0.1", sshd.user, sshd.client_key, known_hosts=known_hosts, side="source")
    with pytest.raises(volume_common.HostKeyRefused):
        shell.cmd_val(["true"])
    with pytest.raises(volume_common.HostKeyRefused):
        shell.scp_to(known_hosts, str(tmp_path / "copied"))
    assert not (tmp_path / "copied").exists()
    assert waits == []


@needs_sshd
def test_an_unknown_alias_is_refused_at_once(sshd, tmp_path, monkeypatch):
    """Seamless Migrate SDD 6.6, 26.4 (plan task ST3): the alias decides, not the address. A file
    that pins the sshd's very key, but under the destination's alias alone, has the source side
    refused at once - its source cleanup waits nothing - and pinning no key for it, while the
    destination side connects to the same sshd."""
    known_hosts = local_sshd.write_known_hosts(
        str(tmp_path / "known_hosts"), [(DST_ALIAS, sshd.host_keys["ed25519"])]
    )
    shells = shells_of(sshd)
    waits = []
    monkeypatch.setattr(volume_common.time, "sleep", waits.append)

    with pytest.raises(volume_common.HostKeyRefused) as refused:
        volume_common.OpenstackVolumeClean(
            conversion_host_at_127_0_0_1(), "src-conv", sshd.client_key, sshd.user, "uuid-1",
            shell_factory=shells, ssh_known_hosts=known_hosts,
        )
    assert waits == []
    assert str(refused.value) == (
        "host key refused: the source conversion host (os_migrate_conv_src, server src-conv at "
        "127.0.0.1) presented an ED25519 key, and none is pinned for it"
    )

    destination = shells(
        "127.0.0.1", sshd.user, sshd.client_key, known_hosts=known_hosts, side="destination",
        server="dst-conv",
    )
    assert destination.cmd_out(["echo", "connected"]) == "connected"


@needs_sshd
def test_a_refusal_names_the_fingerprints_without_the_command_line_or_timeout(sshd, tmp_path):
    """Seamless Migrate SDD 6.6, 26.5 (plan task ST3, review RV42B-02): a refusal names the side,
    the alias, the server, every pinned key and the key ssh saw - never ssh's command line, whose
    ConnectTimeout the control plane would read as a transient "timeout", nor the error that
    holds it."""
    pinned = []
    for key_type in ("ed25519", "ecdsa"):
        local_sshd.keygen(str(tmp_path / key_type), key_type)
        pinned.append(tmp_path / (key_type + ".pub"))
    known_hosts = local_sshd.write_known_hosts(
        str(tmp_path / "known_hosts"), [(SRC_ALIAS, path.read_text()) for path in pinned]
    )
    shell = shells_of(sshd)(
        "127.0.0.1", sshd.user, sshd.client_key, known_hosts=known_hosts, side="source",
        server="src-conv",
    )

    with pytest.raises(volume_common.HostKeyRefused) as refused:
        shell.cmd_out(["echo", "connected"])
    message = str(refused.value)
    assert message == (
        "host key refused: the source conversion host (os_migrate_conv_src, server src-conv at "
        "127.0.0.1) presented the ED25519 key %s, not its pinned keys ED25519 %s, ECDSA %s"
        % (
            local_sshd.fingerprint(sshd.host_key_path()),
            local_sshd.fingerprint(str(pinned[0])),
            local_sshd.fingerprint(str(pinned[1])),
        )
    )
    assert "timeout" not in message.lower()
    for word in ("ssh ", "-o", "BatchMode", known_hosts, sshd.client_key):
        assert word not in message
    err = refused.value
    assert err.__cause__ is None and err.__suppress_context__
    told = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    assert "ConnectTimeout" not in told and "returned non-zero exit status" not in told


class RefusingHosts(fake.ShellFactory):
    """Conversion hosts that refuse a pinned shell's SSH check as RemoteShell does; an unpinned
    one connects."""

    def __call__(self, address, ssh_user, key_path=None, **pin):
        shell = super().__call__(address, ssh_user, key_path, **pin)
        if pin:
            def refuse():
                raise volume_common.HostKeyRefused(
                    "host key refused: the %s conversion host (%s, server %s at %s) presented an "
                    "ED25519 key, and none is pinned for it"
                    % (pin["side"], volume_common.HOST_KEY_ALIASES[pin["side"]], pin["server"],
                       address)
                )

            shell.test_ssh_connection = refuse
        return shell


def test_every_module_pins_its_sides_conversion_host_and_fails_with_the_refusal(tmp_path):
    """Seamless Migrate SDD 6.4, 6.6 (plan task ST3): each module that reaches a conversion host
    takes ssh_known_hosts and pins its own side's host - the source host for the exports, the
    source cleanups and the warm snapshot, the destination host for the transfers and the
    destination failure cleanup - by the server it was given. A refusal fails the cold path's
    modules with it as their message, the warm snapshot with its own message around it, and the
    destination failure cleanup, best effort, not at all; upstream's modules raise it."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_volumes_export,
        import_volumes_src_cleanup,
        import_volumes_transfer,
        import_workload_dst_failure_cleanup,
        import_workload_src_cleanup,
        import_workload_warm_snapshot,
    )

    src, dst = fake.FakeCloud("src"), cloud()
    src.add_conversion_host("src-conv", "10.0.0.10")
    known_hosts = str(tmp_path / "known_hosts")
    common = {"ssh_key_path": "/keys/k", "ssh_user": "cloud-user", "ssh_known_hosts": known_hosts}
    on_src = dict(common, conversion_host={"id": "src-conv"})
    on_dst = dict(common, conversion_host={"id": "dst-conv"})
    a_map = {"/dev/vdb": dict(cold_volume_map()["/dev/vdb"], port=10810)}
    runs = [
        (export_module, src, "source", dict(on_src, data=fake.workload_data()), "message"),
        (import_workload_src_cleanup, src, "source",
         dict(on_src, data=fake.workload_data(), transfer_uuid="uuid-1", volume_map=a_map),
         "message"),
        (transfer_module, dst, "destination",
         dict(on_dst, data=fake.workload_data(), transfer_uuid="uuid-1",
              src_conversion_host_address="10.0.0.10", volume_map=a_map), "message"),
        (import_workload_dst_failure_cleanup, dst, "destination",
         dict(on_dst, data=fake.workload_data(), transfer_uuid="uuid-1", volume_map={}), None),
        (import_workload_warm_snapshot, src, "source",
         dict(on_src, data=fake.workload_data(), state_dir=str(tmp_path / "warm")),
         "Warm snapshot (present) of server srv-1 failed: "),
        (import_volumes_export, src, "source",
         dict(on_src, data=[], log_dir=str(tmp_path)), "raised"),
        (import_volumes_src_cleanup, src, "source",
         dict(on_src, transfer_uuid="uuid-1", volume_map=a_map), "raised"),
        (import_volumes_transfer, dst, "destination",
         dict(on_dst, transfer_uuid="uuid-1", src_conversion_host_address="10.0.0.10",
              volume_map=a_map), "raised"),
    ]
    for module, conn, side, args, failing in runs:
        hosts = RefusingHosts([src, dst])
        server = args["conversion_host"]["id"]
        refusal = (
            "host key refused: the %s conversion host (%s, server %s at %s) presented an ED25519 "
            "key, and none is pinned for it"
            % (side, volume_common.HOST_KEY_ALIASES[side], server, conn.servers[server].access_ipv4)
        )
        if failing == "raised":
            with pytest.raises(volume_common.HostKeyRefused, match=re.escape(refusal)):
                fake.run_module(module, args, conn, shell_factory=hosts)
        else:
            result = fake.run_module(module, args, conn, shell_factory=hosts)
            if failing is None:
                assert not result.get("failed"), (module.__name__, result)
            elif failing == "message":
                assert result.get("failed") and result["msg"] == refusal, (module.__name__, result)
            else:
                assert result.get("failed") and result["msg"] == failing + refusal, result
        assert [shell.pin for shell in hosts.shells] == [
            {"known_hosts": known_hosts, "side": side, "server": server}
        ], module.__name__


class RefusedLink(PortShell):
    """A destination conversion host whose forward to the source host prints ``said`` - as ssh
    writes it, into the file the transfer gives it for stderr; no forwarded export answers."""

    def __init__(self, said):
        super().__init__(free=2)
        self.said = said
        self.address = CONV
        self.forward = None

    def cmd_out(self, command, **kwargs):
        if command[:2] == ["qemu-img", "info"]:
            self.commands.append(list(command))
            raise volume_common.RemoteCommandFailed(1, list(command))
        return super().cmd_out(command, **kwargs)

    def cmd_sub(self, command, **kwargs):
        if command[0] != "ssh":
            return super().cmd_sub(command, **kwargs)
        self.commands.append(list(command))
        self.forward = list(command)
        stderr = kwargs.get("stderr")
        if stderr is not None:
            stderr.write(self.said.encode("utf-8"))
            stderr.flush()
        return fake.FakeProcess(255 if "Permission" in self.said else 0)


@pytest.mark.parametrize(
    ("said", "reason"),
    [
        ("cloud-user@10.0.0.10: Permission denied (publickey).\n", "Permission denied (publickey)"),
        (
            "channel 2: open failed: administratively prohibited: open failed\n",
            "a forward administratively prohibited",
        ),
    ],
    ids=["the login", "a forward"],
)
def test_a_link_the_source_host_refuses_fails_the_cold_transfer_at_once(
    tmp_path, monkeypatch, said, reason
):
    """Seamless Migrate SDD 6.6, 26.5 (plan task ST3): with a known_hosts file, the cold transfer
    reads its forward's output each time a forwarded export does not answer. A link the source
    host refuses - its login, or a forward - fails it at once with LinkRefused, where it waited
    out its timeout and failed as a transient "Timed out ..."; the link's own options stay
    today's (plan task ST6 pins it), and without the file the transfer waits as before."""
    waits = []
    monkeypatch.setattr(volume_common.time, "sleep", waits.append)
    volume_map = {"/dev/vdb": dict(cold_volume_map()["/dev/vdb"], port=10810)}

    def transfer(**pinned):
        with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([cloud()])):
            moved = transfer_module.OpenStackDestinationVolume(
                cloud(), "dst-conv", "/keys/k", "cloud-user", "uuid-1", "10.0.0.10",
                dict(volume_map), workload(), timeout=5, **pinned
            )
        moved.shell = RefusedLink(said)
        return moved

    pinned = transfer(ssh_known_hosts=str(tmp_path / "known_hosts"))
    with pytest.raises(volume_common.LinkRefused) as refused:
        pinned._create_forwarding_process()
    assert str(refused.value) == (
        "link refused by the source conversion host (os_migrate_conv_src at 10.0.0.10): " + reason
    )
    assert waits == [] and refused.value.__suppress_context__
    assert "StrictHostKeyChecking=no" in pinned.shell.forward
    assert not any(word.startswith("HostKeyAlias") for word in pinned.shell.forward)

    unpinned = transfer()
    with pytest.raises(RuntimeError, match="Timed out starting nbdkit exports"):
        unpinned._create_forwarding_process()
    assert len(waits) == 5
