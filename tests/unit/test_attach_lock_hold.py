"""A conversion host's attach lock is never held long enough to starve the other migrations that
queue for it (Seamless Migrate SDD 6.6, review RV11-01): after a failed attach call the export,
and the source cleanup before it checks a marked volume, wait for Cinder to settle the volume for
ATTACH_SETTLE_S at most - never the module's timeout, which a reservation Nova abandoned spent in
full, twice, while every other export on the host queued behind the lock."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import subprocess
from unittest import mock

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import volume_common
from ansible_collections.os_migrate.os_migrate.plugins.modules import (
    import_workload_export_volumes as export_module,
)
from ansible_collections.os_migrate.os_migrate.plugins.modules import (
    import_workload_src_cleanup as cleanup_module,
)
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake
from ansible_collections.os_migrate.os_migrate.tests.unit.test_cold_export_failure import (
    COMMON,
    SourceHost,
    _boot_from_volume_vm,
)
from ansible_collections.os_migrate.os_migrate.tests.unit.test_conversion_host_outage import (
    Clock,
    _run,
)

SOURCE_LOCK = volume_common.ATTACH_LOCK_FILE_SOURCE


class LockClock(SourceHost):
    """The source conversion host, noting on ``clock`` when a module takes its attach lock and
    when it gives it back."""

    def __init__(self, cloud, clock):
        super().__init__(cloud)
        self.clock = clock
        self.held = []  # [taken at, released at]

    def __call__(self, address, ssh_user, key_path=None):
        shell = super().__call__(address, ssh_user, key_path)
        host, val, out = self, shell.cmd_val, shell.cmd_out

        def cmd_val(command, **kwargs):
            result = val(command, **kwargs)
            if command[:2] == ["sudo", "flock"] and command[6] == SOURCE_LOCK and result == 0:
                host.held.append([host.clock.now, None])
            return result

        def cmd_out(command, **kwargs):
            result = out(command, **kwargs)
            if command == ["sudo", "rm", "-vf", SOURCE_LOCK + ".lock"]:
                host.held[-1][1] = host.clock.now
            return result

        shell.cmd_val, shell.cmd_out = cmd_val, cmd_out
        return shell


def _nova_api_fails_after_cinder_reserved(src, server_id, volume_id):
    """nova-api reserved the volume in Cinder for the attach, then failed before nova-compute got
    it - it restarted, or its cast failed: the client gets a 500, and nothing ever completes or
    deletes Cinder's reservation. The volume stays `reserved`, with no attachment listed."""
    attach = src.attach_volume

    def attach_volume(server, volume, device=None, wait=True, timeout=None):
        if server.id == server_id and volume.id == volume_id:
            src.record("attach_volume", server=server.id, volume=volume.id)
            src.volumes[volume.id].status = "reserved"
            raise fake.SDKException("HttpException: 500: Server Error for url: os-volume_attachments")
        return attach(server, volume, device=device, wait=wait, timeout=timeout)

    src.attach_volume = attach_volume


def _abandoned_export(timeout):
    src, data = _boot_from_volume_vm()
    _nova_api_fails_after_cinder_reserved(src, "src-conv", "vol-data1")
    clock = Clock()
    host = LockClock(src, clock)
    with mock.patch.object(volume_common, "time", clock):
        exported = _run(export_module, dict(COMMON, data=data, timeout=timeout), src, host)
        cleaned = _run(cleanup_module, dict(
            COMMON, data=data, timeout=timeout, transfer_uuid=exported["transfer_uuid"],
            volume_map=exported["volume_map"],
        ), src, host)
    return exported, cleaned, host


def test_a_reservation_nova_abandoned_holds_the_attach_lock_minutes_not_a_whole_timeout_twice():
    """Review RV11-01: a reservation Nova abandoned stays `reserved`; the export held the attach
    lock its whole timeout, kept the mark, and the rescue's cleanup held it another timeout, then
    failed on the volume as it did at once before: an hour with the source stopped. Each settle
    wait now ends after ATTACH_SETTLE_S, with the same outcome."""
    exported, cleaned, host = _abandoned_export(timeout=1800)

    assert exported.get("failed") and "500" in exported["msg"], exported
    assert exported["volume_map"]["/dev/vdb"].get("source_attached")  # kept: still under way
    assert "vol-data1" in cleaned["msg"] and "'reserved'" in cleaned["msg"], cleaned
    holds = [released - taken for taken, released in host.held]
    assert len(holds) == 2, host.held  # the export's attach, then the cleanup's detach
    assert all(hold < 300 for hold in holds), holds  # minutes, not the 1800 s timeout


class HeldLock:
    """The source conversion host while another module holds its attach lock until ``until`` on
    ``clock``: each ssh try for the lock costs ``rtt`` seconds and answers 17 ("Another conversion
    is holding the lock") until then; the lock file names a live owner on another migrator."""

    address = "10.0.0.10"

    def __init__(self, clock, until, rtt):
        self.clock, self.until, self.rtt = clock, until, rtt

    def cmd_out(self, command, **kwargs):
        if command == ["sudo", "cat", SOURCE_LOCK + ".lock"]:
            return "migrator-2:4242"
        if command[:2] == ["sudo", "cat"]:
            raise subprocess.CalledProcessError(1, command)  # no such lock file
        return ""

    def cmd_val(self, command, **kwargs):
        if command[:2] == ["sudo", "flock"]:
            self.clock.now += self.rtt
            return 17 if self.clock.now < self.until else 0
        return 0


def test_an_export_queued_behind_a_reservation_nova_abandoned_still_gets_the_attach_lock():
    """Review RV11-01, what the hold did to the others: a lock wait gives up after 1800 tries, each
    at least its 1 s sleep, while the settle wait held the lock for the module's timeout. With
    os_migrate_timeout raised to an hour, an export of another VM queued for the lock gave up with
    "Unable to acquire lock ...", which reads as permanent. The hold is now minutes."""
    exported, _cleaned, host = _abandoned_export(timeout=3600)
    assert exported.get("failed"), exported
    taken, released = host.held[0]

    other = fake.FakeCloud("src")
    other.add_conversion_host("src-conv", "10.0.0.10")
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([other])):
        queued = volume_common.OpenStackVolumeBase(other, "src-conv", "/keys/k", "cloud-user", "u2")
    queued.shell = HeldLock(Clock(), until=released - taken, rtt=0.2)
    attach = volume_common.use_lock(SOURCE_LOCK)(lambda self: "attached")
    with mock.patch.object(volume_common, "time", queued.shell.clock):
        outcome = attach(queued)

    assert outcome == "attached"
