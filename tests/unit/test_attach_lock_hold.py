"""A conversion host's attach lock is never held long enough to starve the other migrations that
queue for it (Seamless Migrate SDD 6.6, review RV11-01): after a failed attach call the export,
and the source cleanup before it checks a marked volume, wait for Cinder to settle the volume for
ATTACH_SETTLE_S at most - never the module's timeout, which a reservation Nova abandoned spent in
full, twice, while every other export on the host queued behind the lock. After a successful call
the wait for the device path ends at once when Nova rolled the attach back, and after
ATTACH_UNDER_WAY_S while Cinder still shows it under way (review RV12-01)."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import subprocess
from unittest import mock

import pytest

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


def _nova_compute_rolls_back_the_attach_of(cloud, server_id, volume_id=None):
    """nova-api accepts the attach of ``volume_id`` (any volume when None) to ``server_id`` - it
    reserves the volume, casts to nova-compute and answers 202 - then nova-compute fails to connect
    it (an os-brick or libvirt error) and rolls back: it deletes the Cinder attachment and the
    block device mapping, so the volume is `available` again, lists no attachment, and no device
    ever shows on the host."""
    attach = cloud.attach_volume

    def attach_volume(server, volume, device=None, wait=True, timeout=None):
        if server.id == server_id and volume_id in (None, volume.id):
            cloud.record("attach_volume", server=server.id, volume=volume.id)
            cloud.volumes[volume.id].status = "available"  # attachment_delete: available again
            return None  # the call itself succeeded
        return attach(server, volume, device=device, wait=wait, timeout=timeout)

    cloud.attach_volume = attach_volume


def _nova_compute_abandons_the_attach_of(cloud, server_id, volume_id):
    """nova-api accepts the attach and reserves the volume; nova-compute dies with the cast in hand,
    so nothing completes or deletes the reservation: the volume stays `reserved`, no attachment."""
    attach = cloud.attach_volume

    def attach_volume(server, volume, device=None, wait=True, timeout=None):
        if server.id == server_id and volume.id == volume_id:
            cloud.record("attach_volume", server=server.id, volume=volume.id)
            cloud.volumes[volume.id].status = "reserved"
            return None
        return attach(server, volume, device=device, wait=wait, timeout=timeout)

    cloud.attach_volume = attach_volume


def _nova_compute_attaches_slowly(cloud, clock, server_id, volume_id, seconds):
    """A healthy Nova under load: nova-api reserves the volume and answers, and nova-compute
    connects it ``seconds`` later on ``clock`` - Cinder shows it `attaching` until then."""
    attach, read = cloud.attach_volume, cloud.get_volume_by_id
    pending = []

    def attach_volume(server, volume, device=None, wait=True, timeout=None):
        if server.id == server_id and volume.id == volume_id:
            cloud.record("attach_volume", server=server.id, volume=volume.id)
            cloud.volumes[volume.id].status = "attaching"
            pending.append((clock.now + seconds, server))
            return None
        return attach(server, volume, device=device, wait=wait, timeout=timeout)

    def get_volume_by_id(wanted):
        if pending and clock.now >= pending[0][0]:
            _due, server = pending.pop()
            cloud.volumes[volume_id].status = "available"
            attach(server, cloud.volumes[volume_id])  # nova-compute connects it
        return read(wanted)

    cloud.attach_volume, cloud.get_volume_by_id = attach_volume, get_volume_by_id


def _accepted_export(nova, timeout):
    """The export of a volume-booted VM whose data volume's attach Nova accepts, then ``nova``."""
    src, data = _boot_from_volume_vm()
    clock = Clock()
    nova(src, clock)
    host = LockClock(src, clock)
    with mock.patch.object(volume_common, "time", clock):
        exported = _run(export_module, dict(COMMON, data=data, timeout=timeout), src, host)
    return exported, host


def _rolled_back(src, clock):
    _nova_compute_rolls_back_the_attach_of(src, "src-conv", "vol-data1")


def test_an_attach_nova_rolled_back_after_accepting_it_frees_the_attach_lock_at_once():
    """Review RV12-01: RV11-01 capped the settle wait after a failed attach call, but an attach
    whose call succeeded and that nova-compute then rolled back waited for its device path the
    module's whole timeout, under the attach lock: 1800 s for a volume Cinder showed `available`
    again from the first read. nova-api reserves the volume before it answers, so the wait now
    fails at once - and the export leaves no mark, as for an attach Nova refused: nothing was
    attached (Seamless Migrate SDD 6.6)."""
    exported, host = _accepted_export(_rolled_back, timeout=1800)

    assert exported.get("failed") and "did not attach volume vol-data1" in exported["msg"], exported
    assert not exported["volume_map"]["/dev/vdb"].get("source_attached"), exported["volume_map"]
    holds = [released - taken for taken, released in host.held]
    assert holds and all(hold < 60 for hold in holds), holds


def test_an_export_queued_behind_a_rolled_back_attach_still_gets_the_attach_lock():
    """Review RV12-01, what the hold did to the others: with os_migrate_timeout raised to an hour,
    an export of another VM queued for the attach lock behind a rolled-back attach gave up with
    "Unable to acquire lock ...", which reads as permanent."""
    exported, host = _accepted_export(_rolled_back, timeout=3600)
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


def test_an_attach_nova_abandoned_after_accepting_it_gives_up_after_ten_minutes():
    """Review RV12-01: a nova-compute that dies with the cast in hand leaves the volume `reserved`
    for good, and the wait for its device path held the attach lock the module's whole timeout. It
    now gives up after ATTACH_UNDER_WAY_S - and keeps the mark: the attach may still complete."""
    exported, host = _accepted_export(
        lambda src, clock: _nova_compute_abandons_the_attach_of(src, "src-conv", "vol-data1"),
        timeout=1800,
    )

    assert exported.get("failed") and "still shows it reserved" in exported["msg"], exported
    assert exported["volume_map"]["/dev/vdb"].get("source_attached")  # kept: still under way
    holds = [released - taken for taken, released in host.held]
    assert holds and all(hold <= volume_common.ATTACH_UNDER_WAY_S + 60 for hold in holds), holds


def test_a_slow_attach_nova_completes_within_minutes_still_succeeds():
    """Review RV12-01: the cap is for an attach Nova abandoned, never for a healthy Nova under load:
    one that connects the volume five minutes after it accepted the call still exports it."""
    exported, _host = _accepted_export(
        lambda src, clock: _nova_compute_attaches_slowly(src, clock, "src-conv", "vol-data1", 300),
        timeout=1800,
    )

    assert not exported.get("failed"), exported
    assert exported["volume_map"]["/dev/vdb"]["source_dev"], exported["volume_map"]


def test_a_transfer_whose_attach_nova_rolled_back_fails_at_once():
    """Review RV12-01, the destination side: the transfer attaches its new volumes to the
    destination conversion host through the same wait, under that host's attach lock."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_transfer_volumes as transfer_module,
    )
    from ansible_collections.os_migrate.os_migrate.tests.unit.test_volume_common import (
        CONV,
        GoneAtTheAttachLock,
        cloud,
        cold_volume_map,
        workload,
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
    transfer.shell = GoneAtTheAttachLock(clock, CONV, cloud=dst, starts=lambda command: False)
    _nova_compute_rolls_back_the_attach_of(dst, "dst-conv")

    with mock.patch.object(volume_common, "time", clock):
        with pytest.raises(RuntimeError, match="did not attach volume"):
            transfer.transfer_exports()

    assert clock.now < 60, clock.now
