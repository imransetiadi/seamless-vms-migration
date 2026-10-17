"""The cold import's record of its workload, and the cold rollback that reads it (SDD 6.3, 7).

The cold transfer creates new destination volumes each time and a cold retry never reuses
them, so a cold rollback deletes exactly the volumes the cold import recorded - and keeps any
other volume attached to the destination server (the owner's choice, 2026-10-14).
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
import subprocess
import time
from unittest import mock

import pytest

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
    server,
    volume_common,
    warm_migration,
)
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.warm_migration import (
    WarmRollback,
    WarmState,
)
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake
from ansible_collections.os_migrate.os_migrate.tests.unit.test_warm_destination import migrated

# what import_workload_transfer_volumes returns as volume_map: the source side of each disk
# and the destination volume it created
TRANSFERRED = {
    "/dev/vda": {"source_id": "svol-boot", "dest_id": "dvol-boot", "size": 20, "name": "vm1-boot",
                 "bootable": True, "dest_dev": "/dev/vdc"},
    "/dev/vdb": {"source_id": "svol-data", "dest_id": "dvol-data", "size": 10, "name": "vm1-data",
                 "bootable": False, "dest_dev": "/dev/vdd"},
}


def _record(state_dir, volume_map):
    """What the cold transfer records of vm1's volumes (SDD 6.3)."""
    return warm_migration.record_destination_volumes(state_dir, "srv-1", volume_map, "vm1")


def _cold_volume_map():
    # as import_workload_export_volumes leaves it, before the transfer created anything: each disk
    # exported over NBD on the source conversion host
    return {
        "/dev/vda": {"source_id": "svol-boot", "name": "vm1-boot", "size": 20, "bootable": True,
                     "port": 10809, "dest_id": None, "progress": 0.0},
        "/dev/vdb": {"source_id": "svol-data", "name": "vm1-data", "size": 10, "bootable": False,
                     "port": 10810, "dest_id": None, "progress": 0.0},
    }


def _clouds():
    src, dst = fake.FakeCloud("src"), fake.FakeCloud("dst")
    src.add_conversion_host("src-conv", "10.0.0.10")
    dst.add_conversion_host("dst-conv", "10.1.0.10")
    return src, dst


def _transfer(dst, state_dir):
    """vm1's cold transfer as import_workload_transfer_volumes runs it, with warm_state_dir."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_transfer_volumes as transfer_module,
    )

    return transfer_module.OpenStackDestinationVolume(
        dst, "dst-conv", "/keys/k", "cloud-user", "uuid-1", "10.0.0.10", _cold_volume_map(),
        server.Server.from_data(fake.workload_data()), warm_state_dir=state_dir,
    )


def test_the_cold_transfer_records_each_volume_as_soon_as_cinder_created_it(tmp_path):
    """SDD 6.3, 6.5 (review lead B): a volume is recorded before the next one is created - a
    transfer that fails at its second volume leaves the first one recorded, as the warm path
    records its volumes (a warm sync reusing one reads all four)."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    create = dst.create_volume

    def cinder_create(**kwargs):
        if kwargs["name"] == "vm1-data":
            raise RuntimeError("VolumeLimitExceeded: maximum number of volumes allowed exceeded")
        return create(**kwargs)

    dst.create_volume = cinder_create
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, state_dir)
    with pytest.raises(RuntimeError, match="VolumeLimitExceeded"):
        transfer._create_destination_volumes()

    assert len(dst.volumes) == 1
    boot = next(iter(dst.volumes))
    state = WarmState.load(state_dir, "srv-1")
    assert state.dest_volumes == {
        "/dev/vda": {"dest_id": boot, "size": 20, "bootable": True, "name": "vm1-boot"},
    }
    assert state.server_name == "vm1"
    assert state.passes == [] and state.destination_server_id is None


def test_a_cold_transfer_that_fails_after_creating_its_volumes_leaves_their_record(tmp_path):
    """The transfer creates every volume, then attaches, converts and detaches them: a failure
    there crashes the module, which returns no volume_map - the record names them all (SDD 6.3)."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, state_dir)
        with mock.patch.object(
            transfer, "_attach_destination_volumes", side_effect=RuntimeError("attach refused")
        ), pytest.raises(RuntimeError, match="attach refused"):
            transfer.transfer_exports()

    state = WarmState.load(state_dir, "srv-1")
    assert sorted(state.dest_volumes) == ["/dev/vda", "/dev/vdb"]
    assert sorted(v["dest_id"] for v in state.dest_volumes.values()) == sorted(dst.volumes)


def test_the_transfer_module_takes_warm_state_dir_and_hands_it_to_its_transfer(tmp_path):
    """The role passes warm_state_dir (SDD 6.5): the module accepts it and its transfer records."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_transfer_volumes as transfer_module,
    )

    state_dir = str(tmp_path / "workload_warm")
    args = {
        "data": fake.workload_data(), "conversion_host": {"id": "dst-conv"},
        "ssh_key_path": "/keys/k", "ssh_user": "cloud-user", "transfer_uuid": "uuid-1",
        "src_conversion_host_address": "10.0.0.10", "volume_map": _cold_volume_map(),
        "warm_state_dir": state_dir,
    }
    with mock.patch.object(transfer_module, "OpenStackDestinationVolume") as transfer:
        transfer.return_value.volume_map = TRANSFERRED
        result = fake.run_module(transfer_module, args, fake.FakeCloud("dst"))

    assert result["_exit_code"] in (None, 0), result
    assert transfer.call_args.kwargs["warm_state_dir"] == state_dir
    transfer.return_value.transfer_exports.assert_called_once_with()


def test_a_transfer_without_warm_state_dir_records_nothing(tmp_path):
    """Upstream behaviour: without warm_state_dir no state file is written."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, None)
    transfer._create_destination_volumes()

    assert len(dst.volumes) == 2
    assert not os.path.exists(state_dir)


def test_the_record_keeps_what_the_state_file_holds_and_a_rerun_changes_nothing(tmp_path):
    state_dir = str(tmp_path / "workload_warm")
    # import_workload_create_instance with warm_state_dir records the server (SDD 6.5)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-srv", "vm1")

    _record(state_dir, TRANSFERRED)
    changed, volume_ids = _record(state_dir, TRANSFERRED)

    assert changed is False and volume_ids == ["dvol-boot", "dvol-data"]
    state = WarmState.load(state_dir, "srv-1")
    assert state.destination_server_id == "dst-srv"
    assert state.dest_volumes == {
        "/dev/vda": {"dest_id": "dvol-boot", "size": 20, "bootable": True, "name": "vm1-boot"},
        "/dev/vdb": {"dest_id": "dvol-data", "size": 10, "bootable": False, "name": "vm1-data"},
    }


def test_a_disk_the_transfer_created_no_volume_for_is_not_recorded(tmp_path):
    state_dir = str(tmp_path / "workload_warm")

    assert _record(state_dir, {"/dev/vda": {"source_id": "svol-boot", "size": 20}}) == (False, [])
    assert not os.path.exists(WarmState.path_for(state_dir, "srv-1"))


def test_a_cold_rollback_deletes_exactly_the_recorded_volumes(tmp_path):
    dst, state_dir = migrated(tmp_path, record=False)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-srv", "vm1")
    _record(state_dir, TRANSFERRED)
    # attached to the migrated server at RHOSO after the cutover: not the migration's
    dst.add_volume("dvol-extra", "attached-later", 5)
    dst.volumes["dvol-extra"].attachments.append(
        {"server_id": "dst-srv", "device": "/dev/vdc", "volume_id": "dvol-extra"}
    )

    result = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", match_by_name=True, timeout=5
    ).run(delete_volumes=True)

    assert result["deleted_server_id"] == "dst-srv"
    assert result["deleted_volume_ids"] == ["dvol-boot", "dvol-data"]
    assert result["kept_volume_ids"] == ["dvol-extra"]
    assert result["state_cleared"] is True
    assert set(dst.volumes) == {"unrelated", "dvol-extra"}


def test_a_rollback_empties_the_record_and_keeps_the_file(tmp_path):
    """SDD 6.3, 6.4 (review RV3-03): a rollback that deletes the recorded volumes empties the
    record - no volume, pass or server - and keeps the file, the sign that the migration recorded
    its workload; a second rollback finds nothing more to do."""
    dst, state_dir = migrated(tmp_path, record=False)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-srv", "vm1")
    _record(state_dir, TRANSFERRED)

    def rollback():
        return WarmRollback(
            dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", match_by_name=True,
            timeout=5,
        ).run(delete_volumes=True)

    result = rollback()

    assert result["changed"] is True and result["state_cleared"] is True
    state = WarmState.load(state_dir, "srv-1")
    assert state.exists() and state.server_name == "vm1"
    assert (state.dest_volumes, state.passes, state.destination_server_id) == ({}, [], None)
    again = rollback()
    assert again["changed"] is False and again["state_cleared"] is True


def test_after_a_cleanup_no_rollback_takes_a_same_named_server_for_the_migrations(tmp_path):
    """SDD 6.4, 7 (review RV3-03): the cleanup before a cold re-run deletes the recorded volumes
    and empties the record instead of removing it, so the cold rollback after the stop - with
    match_by_name - still finds the record and never takes another plan's server named vm1 (and
    the boot volume Nova would delete with it) for the migration's."""
    dst = fake.FakeCloud("dst")
    dst.add_conversion_host("dst-conv", "10.1.0.10")
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True)
    dst.add_volume("dvol-data", "vm1-data", 10)
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)  # a transfer that failed before any server
    dst.add_server("plan-b-vm1", "vm1")
    dst.add_volume(
        "plan-b-boot", "vm1-boot", 20, bootable=True, server_id="plan-b-vm1", device="/dev/vda"
    )

    cleanup = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", timeout=5,
        conversion_host="dst-conv",
    ).run(delete_volumes=True)
    rollback = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", match_by_name=True, timeout=5,
        conversion_host="dst-conv",
    ).run(delete_volumes=False)

    assert cleanup["deleted_volume_ids"] == ["dvol-boot", "dvol-data"]
    assert rollback["deleted_server_id"] is None
    assert "plan-b-vm1" in dst.servers and "plan-b-boot" in dst.volumes


# Cinder's answer to a DELETE of a volume that is already being deleted (openstacksdk: a volume in
# `deleting` takes only a forced delete)
CINDER_400 = (
    "BadRequestException: 400: Client Error for url: .../volumes/%s, Invalid volume: Volume status "
    "must be available or error or error_restoring or error_extending or error_managing and must not "
    "be migrating, attached, belong to a group, have snapshots, awaiting a transfer, or be "
    "disassociated from snapshots after volume transfer."
)


def nova_deletes_on_termination(dst, server_id, volume_ids, reads_until_gone=3):
    """Nova deletes a delete_on_termination volume together with its server: Cinder shows it
    `deleting` for a while, and refuses a second DELETE meanwhile."""
    deleting = {}
    delete_server, get_volume_by_id, delete_volume = (
        dst.compute.delete_server,
        dst.get_volume_by_id,
        dst.delete_volume,
    )

    def nova_delete_server(server, ignore_missing=True, force=False):
        delete_server(server, ignore_missing=ignore_missing, force=force)
        if getattr(server, "id", server) == server_id:
            for volume_id in volume_ids:
                volume = dst.volumes.get(volume_id)
                if volume is not None:
                    volume.status = "deleting"
                    deleting[volume_id] = reads_until_gone

    def cinder_get_volume_by_id(volume_id):
        if volume_id in deleting and deleting[volume_id] is not None:
            deleting[volume_id] -= 1
            if deleting[volume_id] < 0:
                del deleting[volume_id]
                dst.volumes.pop(volume_id, None)
        return get_volume_by_id(volume_id)

    def cinder_delete_volume(name_or_id=None, wait=True, timeout=None, force=False):
        volume = dst.volumes.get(name_or_id)
        if volume is not None and volume.status == "deleting":
            dst.record("delete_volume", name_or_id=name_or_id)
            raise RuntimeError(CINDER_400 % name_or_id)
        return delete_volume(name_or_id=name_or_id, wait=wait, timeout=timeout, force=force)

    dst.compute.delete_server = nova_delete_server
    dst.get_volume_by_id = cinder_get_volume_by_id
    dst.delete_volume = cinder_delete_volume
    return deleting


def test_a_cold_rollback_waits_for_the_boot_volume_nova_deletes_with_the_server(tmp_path):
    """SDD 6.4 (review RV-03): the boot volume the cold import attached with delete_on_termination
    goes with the server; the rollback waits for it and never deletes it a second time - Cinder
    would refuse, and the rollback would stop before it starts the source."""
    dst, state_dir = migrated(tmp_path, record=False)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-srv", "vm1")
    _record(state_dir, TRANSFERRED)
    nova_deletes_on_termination(dst, "dst-srv", ["dvol-boot"])

    with mock.patch.object(warm_migration.time, "sleep"):
        result = WarmRollback(
            dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", match_by_name=True, timeout=5
        ).run(delete_volumes=True)

    assert result["deleted_server_id"] == "dst-srv"
    assert result["deleted_volume_ids"] == ["dvol-data"]  # the boot volume went with the server
    assert set(dst.volumes) == {"unrelated"}
    assert result["state_cleared"] is True
    assert not [c for c in dst.calls_to("delete_volume") if c[1]["name_or_id"] == "dvol-boot"]


def test_a_volume_that_stays_deleting_fails_the_rollback_with_its_reason(tmp_path):
    dst, state_dir = migrated(tmp_path, record=False)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-srv", "vm1")
    _record(state_dir, TRANSFERRED)
    deleting = nova_deletes_on_termination(dst, "dst-srv", ["dvol-boot"])
    dst.compute.delete_server(dst.servers["dst-srv"])
    deleting["dvol-boot"] = None  # stuck: never gone

    with mock.patch.object(warm_migration.time, "sleep"), pytest.raises(
        RuntimeError, match="dvol-boot is still being deleted"
    ):
        WarmRollback(dst, WarmState.load(state_dir, "srv-1"), timeout=5).run(delete_volumes=True)


def test_a_volume_nova_begins_deleting_during_the_rollbacks_own_delete_is_waited_for(tmp_path):
    """The race: available when read, `deleting` by the time the rollback's DELETE arrives - Cinder
    refuses it, and the volume is going anyway (SDD 6.4)."""
    dst, state_dir = migrated(tmp_path, record=False)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-srv", "vm1")
    _record(state_dir, TRANSFERRED)
    deleting = nova_deletes_on_termination(dst, "nobody", [])  # Cinder's behaviour only
    cinder_delete = dst.delete_volume

    def racing_delete(name_or_id=None, **kwargs):
        if name_or_id == "dvol-data" and name_or_id not in deleting:
            dst.volumes[name_or_id].status = "deleting"
            deleting[name_or_id] = 2
        return cinder_delete(name_or_id=name_or_id, **kwargs)

    dst.delete_volume = racing_delete

    with mock.patch.object(warm_migration.time, "sleep"):
        result = WarmRollback(dst, WarmState.load(state_dir, "srv-1"), timeout=5).run(
            delete_volumes=True
        )

    assert result["deleted_volume_ids"] == ["dvol-boot"]
    assert set(dst.volumes) == {"unrelated"}


def test_a_cold_record_without_a_server_never_matches_one_by_name(tmp_path):
    """SDD 6.4, 7 (review RV-05): the cold import recorded its volumes, then Nova never accepted a
    server - the name serves only a workload migrated before anything was recorded, so a
    same-named server is another one, and is kept; the recorded volumes go."""
    dst = fake.FakeCloud("dst")
    dst.add_conversion_host("dst-conv", "10.1.0.10")
    dst.add_server("someone-elses", "vm1")
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True)
    dst.add_volume("dvol-data", "vm1-data", 10)
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)

    result = WarmRollback(
        dst,
        WarmState.load(state_dir, "srv-1"),
        server_name="vm1",
        match_by_name=True,
        timeout=5,
        conversion_host="dst-conv",
    ).run(delete_volumes=True)

    assert result["deleted_server_id"] is None, result
    assert "someone-elses" in dst.servers
    assert result["deleted_volume_ids"] == ["dvol-boot", "dvol-data"]


def _unrecorded_server(dst, server_id="dst-srv-unrecorded", name="vm1"):
    """Nova accepted the create, but its answer never arrived: the server, built from the recorded
    volumes, exists while the record names no server."""
    dst.add_server(server_id, name)
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True, server_id=server_id, device="/dev/vda")
    dst.add_volume("dvol-data", "vm1-data", 10, server_id=server_id, device="/dev/vdb")


@pytest.mark.parametrize("match_by_name", [True, False])
def test_a_server_built_from_the_recorded_volumes_is_the_migrations_unrecorded(
    tmp_path, match_by_name
):
    """SDD 6.4, 7 (review RV3-06): a server named like the workload that holds the recorded
    volumes was built from them - the migration's own, though Nova's answer to its create was lost
    - so the rollback deletes it, in a cold rollback (match_by_name) as in a cleanup (none), rather
    than leave it running beside the restarted source."""
    dst = fake.FakeCloud("dst")
    dst.add_conversion_host("dst-conv", "10.1.0.10")
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)
    _unrecorded_server(dst)

    result = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", match_by_name=match_by_name,
        timeout=5, conversion_host="dst-conv",
    ).run(delete_volumes=True)

    assert result["deleted_server_id"] == "dst-srv-unrecorded"
    assert "dst-srv-unrecorded" not in dst.servers
    assert not {"dvol-boot", "dvol-data"} & set(dst.volumes)


@pytest.mark.parametrize("match_by_name", [True, False])
def test_a_rollback_whose_recorded_server_is_gone_finds_the_one_built_from_the_recorded_volumes(
    tmp_path, match_by_name
):
    """SDD 6.4, 7 (review RV4-04): the operator deleted the recorded server and retried; the
    re-run's create lost Nova's answer, so the record still names the deleted server - the rollback
    finds the re-run's server by the recorded volumes it holds and deletes it, rather than leave it
    running beside the restarted source."""
    dst = fake.FakeCloud("dst")
    dst.add_conversion_host("dst-conv", "10.1.0.10")
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-s1-deleted", "vm1")
    _unrecorded_server(dst)

    result = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", match_by_name=match_by_name,
        timeout=5, conversion_host="dst-conv",
    ).run(delete_volumes=True)

    assert result["deleted_server_id"] == "dst-srv-unrecorded"
    assert "dst-srv-unrecorded" not in dst.servers
    assert not {"dvol-boot", "dvol-data"} & set(dst.volumes)
    assert WarmState.load(state_dir, "srv-1").destination_server_id is None


def test_a_rollback_whose_recorded_server_is_gone_never_matches_one_by_name(tmp_path):
    """SDD 6.4, 7 (review RV4-04): with the recorded server gone, a same-named server that holds
    none of the recorded volumes is another one - kept even by a cold rollback (match_by_name) -
    and the recorded volumes go."""
    dst = fake.FakeCloud("dst")
    dst.add_conversion_host("dst-conv", "10.1.0.10")
    dst.add_server("someone-elses", "vm1")
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True)
    dst.add_volume("dvol-data", "vm1-data", 10)
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-s1-deleted", "vm1")

    result = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", match_by_name=True,
        timeout=5, conversion_host="dst-conv",
    ).run(delete_volumes=True)

    assert result["deleted_server_id"] is None, result
    assert "someone-elses" in dst.servers
    assert result["deleted_volume_ids"] == ["dvol-boot", "dvol-data"]


def test_a_differently_named_server_holding_a_recorded_volume_is_kept(tmp_path):
    """SDD 6.4: only a server named like the workload is taken for the migration's; one with
    another name that holds a recorded volume (attached by someone since) is not, and the volume it
    holds is kept."""
    dst = fake.FakeCloud("dst")
    dst.add_conversion_host("dst-conv", "10.1.0.10")
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)
    _unrecorded_server(dst, server_id="someone-elses", name="not-vm1")

    result = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", match_by_name=True,
        timeout=5, conversion_host="dst-conv",
    ).run(delete_volumes=True)

    assert result["deleted_server_id"] is None
    assert "someone-elses" in dst.servers
    assert sorted(result["kept_volume_ids"]) == ["dvol-boot", "dvol-data"]


def test_two_servers_built_from_the_recorded_volumes_are_an_error(tmp_path):
    """SDD 6.4: the rollback never guesses between servers that hold the recorded volumes."""
    dst = fake.FakeCloud("dst")
    dst.add_conversion_host("dst-conv", "10.1.0.10")
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)
    dst.add_server("twin-a", "vm1")
    dst.add_server("twin-b", "vm1")
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True, server_id="twin-a", device="/dev/vda")
    dst.add_volume("dvol-data", "vm1-data", 10, server_id="twin-b", device="/dev/vdb")

    with pytest.raises(RuntimeError, match="refusing to guess"):
        WarmRollback(
            dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", timeout=5,
            conversion_host="dst-conv",
        ).run(delete_volumes=True)
    assert {"twin-a", "twin-b"} <= set(dst.servers)


def test_the_conversion_host_is_never_taken_for_the_migrations_server(tmp_path):
    """SDD 6.4: a transfer cut short leaves a recorded volume attached to the destination
    conversion host; even named like the workload, the conversion host is never taken for the
    migration's server - the volume is detached from it and deleted."""
    dst = fake.FakeCloud("dst")
    dst.add_conversion_host("vm1", "10.1.0.10")  # a conversion host that happens to be named vm1
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True, server_id="vm1", device="/dev/vdb")
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, {"/dev/vda": TRANSFERRED["/dev/vda"]})

    result = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", match_by_name=True,
        timeout=5, conversion_host="vm1",
    ).run(delete_volumes=True)

    assert result["deleted_server_id"] is None
    assert "vm1" in dst.servers
    assert result["deleted_volume_ids"] == ["dvol-boot"]


try:
    from openstack.exceptions import ResourceFailure
except ImportError:  # openstacksdk is optional for the unit tests

    class ResourceFailure(Exception):
        pass


def _cinder_puts_in_error(dst, name):
    """Cinder accepts the volume called ``name`` and then puts it in `error` (no valid backend):
    the SDK's wait fails - inside create_volume(wait=True) as in an explicit wait_for_status."""
    create, wait = dst.create_volume, dst.block_storage.wait_for_status

    def fail(volume):
        volume.status = "error"
        raise ResourceFailure("Volume %s transitioned to failure state error" % volume.id)

    def sdk_create_volume(size, wait=True, timeout=None, image=None, bootable=None, **kwargs):
        volume = create(size, wait=False, timeout=timeout, image=image, bootable=bootable, **kwargs)
        if wait and kwargs.get("name") == name:
            fail(volume)
        return volume

    def sdk_wait_for_status(res, status="available", failures=None, **kwargs):
        if dst.volumes[res.id].name == name:
            fail(dst.volumes[res.id])
        return wait(res, status=status, failures=failures, **kwargs)

    dst.create_volume = sdk_create_volume
    dst.block_storage.wait_for_status = sdk_wait_for_status


def test_a_volume_that_ends_in_error_is_recorded_before_its_wait_fails(tmp_path):
    """SDD 6.3 (review RV3-01): each volume is recorded as soon as Cinder accepted it, before the
    transfer waits for it - one Cinder puts in error is recorded too, and the rollback removes it
    with its quota."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    _cinder_puts_in_error(dst, "vm1-data")
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, state_dir)
    with pytest.raises(ResourceFailure):
        transfer._create_destination_volumes()

    recorded = WarmState.load(state_dir, "srv-1").dest_volumes
    assert sorted(entry["dest_id"] for entry in recorded.values()) == sorted(dst.volumes)
    WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), timeout=5, conversion_host="dst-conv"
    ).run(delete_volumes=True)
    assert not dst.volumes


def test_the_boot_volume_is_marked_bootable_once_available(tmp_path):
    """SDD 6.3: the transfer waits for each volume after recording it, then sets the boot
    volume's bootable flag - Cinder sets it only on an available volume."""
    src, dst = _clouds()
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, str(tmp_path / "workload_warm"))
    transfer._create_destination_volumes()

    names = [
        c[0] for c in dst.calls
        if c[0] in ("create_volume", "wait_for_status", "set_volume_bootable_status")
    ]
    assert names[:3] == ["create_volume", "wait_for_status", "set_volume_bootable_status"]
    boot = next(v for v in dst.volumes.values() if v.name == "vm1-boot")
    assert boot.bootable is True
    assert all(c[1].get("wait") is False for c in dst.calls_to("create_volume"))


def test_a_volume_whose_record_cannot_be_written_is_deleted(tmp_path):
    """SDD 6.3 (review RV3-01): a volume no record names would outlive every rollback - when its
    record cannot be written, the transfer deletes it (once Cinder settled it) before it fails."""
    import errno

    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    # as Cinder: a volume is `creating` until it is waited for, and cannot be deleted before
    create, wait, delete = dst.create_volume, dst.block_storage.wait_for_status, dst.delete_volume

    def creating(*args, **kwargs):
        volume = create(*args, **kwargs)
        volume.status = "creating"
        return volume

    def settles(res, **kwargs):
        dst.volumes[res.id].status = "available"
        return wait(res, **kwargs)

    def refuses_creating(name_or_id=None, **kwargs):
        if dst.volumes[name_or_id].status == "creating":
            raise RuntimeError("Invalid volume: Volume status must be available or error")
        return delete(name_or_id=name_or_id, **kwargs)

    dst.create_volume, dst.delete_volume = creating, refuses_creating
    dst.block_storage.wait_for_status = settles
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, state_dir)
    with mock.patch.object(
        WarmState, "save", side_effect=OSError(errno.ENOSPC, "No space left on device")
    ), pytest.raises(OSError, match="No space left"):
        transfer._create_destination_volumes()

    assert not dst.volumes


def _earlier_attempt(state_dir, dst, data_holder=None):
    """Attempt 1 recorded its volumes and its server S1; then S1 went - Nova deleted its boot
    volume with it and detached the data volume (or, renamed, S1 still holds the data volume)."""
    _record(state_dir, TRANSFERRED)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-s1", "vm1")
    if data_holder:
        dst.add_server(data_holder, "vm1-kept")
    dst.add_volume("dvol-data", "vm1-data", 10, server_id=data_holder, device="/dev/vdb")


def test_a_rerun_deletes_the_earlier_attempts_volume_no_server_holds(tmp_path):
    """SDD 6.3 (review RV3-02): the cold path never reuses a volume - before a re-run records its
    new volume for a device, the earlier attempt's volume no server holds is deleted, so it never
    drops out of the record unremoved."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    _earlier_attempt(state_dir, dst)
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, state_dir)
    transfer._create_destination_volumes()

    assert "dvol-data" not in dst.volumes
    recorded = WarmState.load(state_dir, "srv-1").dest_volumes
    assert sorted(entry["dest_id"] for entry in recorded.values()) == sorted(dst.volumes)


def test_a_rerun_keeps_the_earlier_attempts_volume_a_server_holds(tmp_path):
    """SDD 6.3: an earlier volume a server holds - S1 renamed rather than deleted - is that
    server's: the re-run keeps it, and the record drops it."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    _earlier_attempt(state_dir, dst, data_holder="dst-s1-renamed")
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, state_dir)
    transfer._create_destination_volumes()

    assert "dvol-data" in dst.volumes
    recorded = WarmState.load(state_dir, "srv-1").dest_volumes
    assert "dvol-data" not in [entry["dest_id"] for entry in recorded.values()]


def test_an_earlier_volume_the_rerun_cannot_delete_fails_the_transfer(tmp_path):
    """SDD 6.3 (review RV5-01): an earlier volume the re-run cannot delete fails the transfer with
    the record unchanged, before it creates any volume."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    _earlier_attempt(state_dir, dst)
    delete = dst.delete_volume

    def cinder_delete(name_or_id=None, **kwargs):
        if name_or_id == "dvol-data":
            raise RuntimeError("Invalid volume: Volume status must be available or error")
        return delete(name_or_id=name_or_id, **kwargs)

    dst.delete_volume = cinder_delete
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, state_dir)
    with pytest.raises(RuntimeError, match="dvol-data"):
        transfer._create_destination_volumes()

    recorded = WarmState.load(state_dir, "srv-1").dest_volumes
    assert recorded["/dev/vdb"]["dest_id"] == "dvol-data"  # unchanged
    # it failed before it created any volume: every volume left in the destination is recorded
    assert not dst.calls_to("create_volume")
    assert set(dst.volumes) <= {entry["dest_id"] for entry in recorded.values()}


def test_a_warm_rollback_with_a_recorded_volume_gone_still_runs(tmp_path):
    """Review RV4-03: a recorded volume deleted out of band (an orphan the sync warned about) is
    gone - openstacksdk raises NotFound for it - and the rollback looking for the migration's
    server goes on without it, rather than fail before the source starts."""
    dst = fake.FakeCloud("dst")
    dst.add_conversion_host("dst-conv", "10.1.0.10")
    state_dir = str(tmp_path / "workload_warm")
    state = WarmState(state_dir, "srv-1", server_name="vm1", transfer_uuid="t-2",
                      passes=[{"number": 1, "kind": "full"}, {"number": 2, "kind": "final"}])
    state.dest_volumes = {
        "/dev/vda": {"dest_id": "dvol-boot", "size": 20, "bootable": True, "name": "vm1-boot"},
        "/dev/vdc": {"dest_id": "dvol-orphan", "size": 5, "bootable": False, "name": "vm1-old"},
    }
    state.save()
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True)

    result = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", timeout=5,
        conversion_host="dst-conv",
    ).run(delete_volumes=False)

    assert result["deleted_server_id"] is None
    assert "dvol-boot" in dst.volumes


def _nova_deleted_the_recorded_server(tmp_path, reads_until_gone):
    """Attempt 1 recorded its volumes and its server S1; the operator deleted S1, and Nova deletes
    its boot volume with it - Cinder shows it `deleting` - and leaves the data volume detached."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-s1", "vm1")
    dst.add_server("dst-s1", "vm1")
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True, server_id="dst-s1", device="/dev/vda")
    dst.add_volume("dvol-data", "vm1-data", 10, server_id="dst-s1", device="/dev/vdb")
    deleting = nova_deletes_on_termination(dst, "dst-s1", ["dvol-boot"], reads_until_gone)
    dst.compute.delete_server(dst.servers["dst-s1"])
    assert dst.volumes["dvol-boot"].status == "deleting"
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, state_dir)
    return dst, state_dir, transfer, deleting


def test_a_rerun_waits_until_the_earlier_boot_volume_nova_deletes_is_gone(tmp_path):
    """SDD 6.3 (review RV4-02, RV5-02): Cinder refuses a second delete of the boot volume Nova is
    deleting with the operator-deleted server - the re-run sends none and waits until Cinder no
    longer has it, deletes the earlier data volume no server holds, and only then creates a volume:
    an earlier volume stays recorded until it is gone."""
    dst, state_dir, transfer, deleting = _nova_deleted_the_recorded_server(tmp_path, 50)
    create, earlier_left = dst.create_volume, []

    def cinder_create(**kwargs):
        earlier_left.append(sorted({"dvol-boot", "dvol-data"} & set(dst.volumes)))
        return create(**kwargs)

    dst.create_volume = cinder_create
    with mock.patch.object(time, "sleep"):
        transfer._create_destination_volumes()

    assert earlier_left == [[], []]  # both gone before the first new volume
    recorded = WarmState.load(state_dir, "srv-1").dest_volumes
    assert not {"dvol-boot", "dvol-data"} & {entry["dest_id"] for entry in recorded.values()}
    assert not [c for c in dst.calls_to("delete_volume") if c[1]["name_or_id"] == "dvol-boot"]


def test_a_rerun_waits_for_an_earlier_volume_nova_began_deleting_while_it_deleted_it(tmp_path):
    """SDD 6.3 (review 6 lead): Nova, deleting the operator-deleted server S1, removes its boot
    volume's attachment and then deletes the volume. The re-run read it `available` in between,
    and Nova's delete landed before its own: Cinder refused that one - the volume no longer
    `available` - and the transfer failed, although the volume was going all the same. The
    re-run now waits until it is gone, as for a volume it found `deleting`."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-s1", "vm1")
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True)  # detached by Nova, not yet going
    dst.add_volume("dvol-data", "vm1-data", 10)
    reads_left = {}
    delete, get = dst.delete_volume, dst.get_volume_by_id

    def nova_deletes_first(name_or_id=None, **kwargs):
        if name_or_id == "dvol-boot":
            dst.record("delete_volume", name_or_id=name_or_id)
            dst.volumes["dvol-boot"].status = "deleting"
            reads_left["dvol-boot"] = 2
            raise RuntimeError("Invalid volume: Volume status must be available or error (HTTP 400)")
        return delete(name_or_id=name_or_id, **kwargs)

    def cinder_get(volume_id):
        if volume_id in reads_left:
            reads_left[volume_id] -= 1
            if reads_left[volume_id] < 0:
                del reads_left[volume_id]
                dst.volumes.pop(volume_id, None)
        return get(volume_id)

    dst.delete_volume, dst.get_volume_by_id = nova_deletes_first, cinder_get
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, state_dir)
    with mock.patch.object(time, "sleep"):
        transfer._create_destination_volumes()

    assert not {"dvol-boot", "dvol-data"} & set(dst.volumes)
    recorded = WarmState.load(state_dir, "srv-1").dest_volumes
    assert not {"dvol-boot", "dvol-data"} & {entry["dest_id"] for entry in recorded.values()}


def test_a_rerun_whose_host_went_down_while_it_removed_an_earlier_volume_gives_up_in_one_window(
    tmp_path,
):
    """Review RV11-02: a cold re-run detaches, under the destination conversion host's attach lock,
    the earlier volumes a transfer cut short left there. When that lock wait gives up, the re-run
    wraps the give-up in its own error ("Could not delete volume ..."), and the transfer released
    its ports all the same: a second 15-minute wait for the port lock on the same dead host, the
    source stopped, whose "Lock timeout ... v2v-migration-lock" replaced the error."""
    from ansible_collections.os_migrate.os_migrate.tests.unit.test_volume_common import (
        Clock,
        GoneAtTheAttachLock,
    )

    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True)
    dst.add_volume("dvol-data", "vm1-data", 10)
    for volume_id in ("dvol-boot", "dvol-data"):  # a transfer cut short left them there
        dst.attach_volume(dst.servers["dst-conv"], dst.volumes[volume_id])
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, state_dir)
    clock = Clock()
    transfer.shell = GoneAtTheAttachLock(clock, "10.1.0.10")

    with mock.patch.object(volume_common, "time", clock):
        with pytest.raises(RuntimeError) as failure:
            transfer.transfer_exports()

    message = str(failure.value)
    assert "v2v-migration-lock" not in message, message
    assert clock.now - transfer.shell.down_since <= volume_common.SSH_UNREACHABLE_S + 60, message


def test_an_earlier_volume_cinder_gives_up_deleting_fails_the_rerun_with_its_record(tmp_path):
    """SDD 6.3 (review RV5-02): Cinder can give up deleting the boot volume Nova deletes - an RBD
    image still watched is busy, and Cinder puts it back to `available` - so the re-run fails before
    it creates any volume, the earlier volume still recorded, rather than drop a copy of the disk
    from the record."""
    dst, state_dir, transfer, deleting = _nova_deleted_the_recorded_server(tmp_path, 50)

    def cinder_gives_up(seconds):
        deleting.pop("dvol-boot", None)
        dst.volumes["dvol-boot"].status = "available"

    with mock.patch.object(time, "sleep", side_effect=cinder_gives_up), pytest.raises(
        RuntimeError, match="dvol-boot .*available"
    ):
        transfer._create_destination_volumes()

    assert WarmState.load(state_dir, "srv-1").dest_volumes["/dev/vda"]["dest_id"] == "dvol-boot"
    assert "dvol-boot" in dst.volumes
    assert not dst.calls_to("create_volume")


def test_an_earlier_volume_still_being_deleted_at_the_timeout_fails_the_rerun_with_its_record(
    tmp_path,
):
    """SDD 6.3 (review RV5-02): the re-run waits for an earlier volume Cinder is deleting only
    within the transfer's timeout; still there then, it fails before it creates any volume, the
    earlier volume still recorded."""
    dst, state_dir, transfer, deleting = _nova_deleted_the_recorded_server(tmp_path, None)

    with mock.patch.object(time, "sleep"), pytest.raises(
        RuntimeError, match="dvol-boot .*still being deleted"
    ):
        transfer._create_destination_volumes()

    assert WarmState.load(state_dir, "srv-1").dest_volumes["/dev/vda"]["dest_id"] == "dvol-boot"
    assert not dst.calls_to("create_volume")


class _Killed(BaseException):
    """The orchestrator's stop or a step timeout kills the playbook's process group: the module
    dies without running an except or finally clause."""


def test_a_rerun_removes_the_earlier_volumes_before_it_creates_any(tmp_path):
    """SDD 6.3 (review RV5-01): the re-run waits for the conversion host's attach lock - another
    migration attaching holds it - to detach the earlier volumes a transfer cut short left there.
    Killed meanwhile, it has created no volume yet: every volume left in the destination is
    recorded, so no new volume waits unrecorded."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-s1", "vm1")  # stale
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True)
    dst.add_volume("dvol-data", "vm1-data", 10)
    for volume_id in ("dvol-boot", "dvol-data"):
        dst.attach_volume(dst.servers["dst-conv"], dst.volumes[volume_id])
    lock_waits = []

    def conversion_host(command):
        if command[:2] == ["sudo", "flock"] and command[6] == volume_common.ATTACH_LOCK_FILE_DESTINATION:
            lock_waits.append(command)
            if len(lock_waits) > 3:
                raise _Killed()
            return 16  # "Another conversion has the lock."
        return None

    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src], val=conversion_host)):
        transfer = _transfer(dst, state_dir)
    with mock.patch.object(time, "sleep"), pytest.raises(_Killed):
        transfer._create_destination_volumes()

    assert not dst.calls_to("create_volume")
    recorded = {e["dest_id"] for e in WarmState.load(state_dir, "srv-1").dest_volumes.values()}
    assert set(dst.volumes) <= recorded


def test_a_rerun_removes_the_earlier_volumes_left_on_the_conversion_host(tmp_path):
    """SDD 6.3 (review RV4-05): a transfer cut short left its volumes attached to the destination
    conversion host - the migration's own, no other server's: the re-run detaches them from it and
    deletes them, so none drops out of the record unremoved."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-s1", "vm1")  # stale
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True)
    dst.add_volume("dvol-data", "vm1-data", 10)
    conversion_host = dst.servers["dst-conv"]
    for volume_id in ("dvol-boot", "dvol-data"):
        dst.attach_volume(conversion_host, dst.volumes[volume_id])
    shells = fake.ShellFactory([dst, src])
    with mock.patch.object(volume_common, "RemoteShell", shells):
        transfer = _transfer(dst, state_dir)
    nova_detach, locked = dst.detach_volume, []

    def detach_volume(server, volume, **kwargs):
        locked.append(_holds_lock(shells, volume_common.ATTACH_LOCK_FILE_DESTINATION))
        return nova_detach(server, volume, **kwargs)

    dst.detach_volume = detach_volume
    transfer._create_destination_volumes()

    recorded = {e["dest_id"] for e in WarmState.load(state_dir, "srv-1").dest_volumes.values()}
    assert recorded == set(dst.volumes)  # nothing in the destination the record does not name
    assert not {"dvol-boot", "dvol-data"} & set(dst.volumes)
    # under the conversion host's attach lock, as every detach: another migration's attach
    # finds its device by what lsblk lists before and after it
    assert locked == [True, True]


def test_an_earlier_volume_the_conversion_host_and_a_server_hold_is_kept(tmp_path):
    """SDD 6.3 (review RV4-05): only a volume the conversion host alone holds is the migration's -
    one a server holds as well (a multi-attach volume) stays that server's: never detached from
    the conversion host, never deleted, and the record drops it."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    _earlier_attempt(state_dir, dst, data_holder="dst-s1-renamed")
    # a multi-attach volume: the conversion host holds it too (openstacksdk's attach_volume would
    # refuse to attach it, in-use; a transfer cut short left it there)
    dst.volumes["dvol-data"].attachments.append(
        {"server_id": "dst-conv", "device": "/dev/vdb", "volume_id": "dvol-data"}
    )
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, state_dir)
    transfer._create_destination_volumes()

    assert "dvol-data" in dst.volumes
    assert not [c for c in dst.calls_to("detach_volume") if c[1]["volume"] == "dvol-data"]
    recorded = WarmState.load(state_dir, "srv-1").dest_volumes
    assert "dvol-data" not in [entry["dest_id"] for entry in recorded.values()]


def test_an_earlier_volume_the_rerun_cannot_detach_fails_the_transfer(tmp_path):
    """SDD 6.3 (review RV4-05): an earlier volume left on the conversion host that the re-run
    cannot detach fails the transfer with the record unchanged, as one it cannot delete does."""
    src, dst = _clouds()
    state_dir = str(tmp_path / "workload_warm")
    _earlier_attempt(state_dir, dst)
    dst.attach_volume(dst.servers["dst-conv"], dst.volumes["dvol-data"])

    def nova_detach(server, volume, **kwargs):
        raise RuntimeError("Nova refused the detach")

    dst.detach_volume = nova_detach
    with mock.patch.object(volume_common, "RemoteShell", fake.ShellFactory([dst, src])):
        transfer = _transfer(dst, state_dir)
    with pytest.raises(RuntimeError, match="Could not delete volume dvol-data .*: Nova refused"):
        transfer._create_destination_volumes()

    recorded = WarmState.load(state_dir, "srv-1").dest_volumes
    assert recorded["/dev/vdb"]["dest_id"] == "dvol-data"  # unchanged
    # it failed before it created any volume: every volume left in the destination is recorded
    assert not dst.calls_to("create_volume")
    assert set(dst.volumes) <= {entry["dest_id"] for entry in recorded.values()}


def _holds_lock(shells, lock_file):
    """Whether the transfer holds ``lock_file`` on the conversion host now: its last flock of it
    came after its last removal of the lock file."""
    commands = [c for shell in shells.shells for c in shell.commands]
    taken = [i for i, c in enumerate(commands) if c[:2] == ["sudo", "flock"] and lock_file in c]
    freed = [i for i, c in enumerate(commands) if c[:3] == ["sudo", "rm", "-vf"] and lock_file + ".lock" in c]
    return bool(taken) and (not freed or taken[-1] > freed[-1])


# --- the rescue's destination cleanup after a failed transfer (SDD 6.6, audit A-08) -------------


def _cleanup_args(volume_map=None, state_dir=None):
    args = {
        "data": fake.workload_data(), "conversion_host": {"id": "dst-conv"},
        "ssh_key_path": "/keys/k", "ssh_user": "cloud-user", "transfer_uuid": "uuid-1",
    }
    if volume_map is not None:
        args["volume_map"] = volume_map
    if state_dir is not None:
        args["warm_state_dir"] = state_dir
    return args


def _left_by_a_failed_transfer(tmp_path):
    """vm1's failed transfer: its boot volume still on the destination conversion host, its data
    volume detached, both recorded as Cinder created them; an unrelated volume beside them."""
    src, dst = _clouds()
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True, server_id="dst-conv", device="/dev/vdb")
    dst.add_volume("dvol-data", "vm1-data", 10)
    dst.add_volume("unrelated", "keep-me", 1)
    state_dir = str(tmp_path / "workload_warm")
    _record(state_dir, TRANSFERRED)
    return src, dst, state_dir


@pytest.mark.parametrize("given", ["the transfer's map", "the record"])
def test_the_destination_cleanup_removes_a_failed_transfers_volumes(tmp_path, given):
    """SDD 6.6 (audit A-08): a transfer that failed raises - its result carries no volume map, and
    its errors reach the control plane's classifier as before - so the rescue's destination
    cleanup takes the workload's record instead: the volumes the transfer recorded as Cinder
    created them. Given the map, or the record, it detaches them from the destination conversion
    host and deletes them; nothing else."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_dst_failure_cleanup as cleanup_module,
    )

    src, dst, state_dir = _left_by_a_failed_transfer(tmp_path)
    args = (
        _cleanup_args(volume_map=TRANSFERRED)
        if given == "the transfer's map"
        else _cleanup_args(state_dir=state_dir)
    )
    result = fake.run_module(cleanup_module, args, dst, shell_factory=fake.ShellFactory([dst, src]))

    assert result["_exit_code"] in (None, 0), result
    assert sorted(dst.volumes) == ["unrelated"]


def test_the_destination_cleanup_of_a_conversion_host_it_cannot_reach_still_deletes_what_it_can(
    tmp_path,
):
    """SDD 6.6 (audit A-08): the conversion host may be what failed the transfer. The cleanup's
    one SSH check at its start no longer fails it: the volume that host holds stays for the
    rollback, which deletes it by the record - no attach lock is taken on a host the check could
    not reach - and a volume no server holds is deleted through the API."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_dst_failure_cleanup as cleanup_module,
    )

    src, dst, state_dir = _left_by_a_failed_transfer(tmp_path)
    shells = fake.ShellFactory([dst, src])

    def unreachable(shell):
        shell.commands.append(["echo connected"])
        raise subprocess.CalledProcessError(255, ["ssh", "-o", "ConnectTimeout=10", "cloud-user@x"])

    with mock.patch.object(fake.FakeShell, "test_ssh_connection", unreachable):
        result = fake.run_module(
            cleanup_module, _cleanup_args(state_dir=state_dir), dst, shell_factory=shells
        )

    assert result["_exit_code"] in (None, 0), result
    assert sorted(dst.volumes) == ["dvol-boot", "unrelated"]
    assert not [c for c in shells.commands_at("10.1.0.10") if c[:2] == ["sudo", "flock"]]


@pytest.mark.parametrize("trouble", ["ssh lost after the check", "a detach nova never ends"])
def test_the_destination_cleanup_still_deletes_what_no_server_holds_when_a_detach_fails(
    tmp_path, monkeypatch, trouble
):
    """SDD 6.6 (review RV32B-05): the conversion host may answer the cleanup's one SSH check and
    fail again during the detach - the attach-lock wait gives up ("Lock timeout"), or Nova never
    ends a detach. The cleanup is best effort: the volume the host still holds stays for the
    rollback, named in the result, and the volume no server holds is deleted all the same."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_dst_failure_cleanup as cleanup_module,
    )

    src, dst, state_dir = _left_by_a_failed_transfer(tmp_path)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    val = None
    if trouble == "ssh lost after the check":
        monkeypatch.setattr(volume_common, "SSH_UNREACHABLE_S", 0)

        def val(command):  # every ssh after the one check fails (ssh's own exit status)
            return volume_common.SSH_FAILED if command[:2] == ["sudo", "flock"] else None

    else:
        dst.detach_reads = 10 ** 6  # Nova accepted the detach; the guest never lets go
    args = dict(_cleanup_args(state_dir=state_dir), timeout=3)
    shells = fake.ShellFactory([dst, src], val=val)
    result = fake.run_module(cleanup_module, args, dst, shell_factory=shells)

    assert result["_exit_code"] in (None, 0), result
    assert sorted(dst.volumes) == ["dvol-boot", "unrelated"]
    assert result["volumes_left"] == ["dvol-boot"]
    if trouble == "ssh lost after the check":
        # the host is lost: one lock wait - its window opens on the first failed try and gives
        # up on the second - never one per volume
        locks = [c for c in shells.commands_at("10.1.0.10") if c[:2] == ["sudo", "flock"]]
        assert len(locks) == 2, locks


def test_the_destination_cleanup_never_fails_on_a_deletion_cinder_does_not_finish(tmp_path):
    """SDD 6.6 (reviews RV32B-02, RV32B-05): a deletion Cinder does not finish within the cleanup's
    timeout - openstacksdk's "Timeout waiting for Volume ... to delete" - leaves that volume for
    the rollback: the cleanup goes on with the others and never fails, so none of its words
    reach the output the control plane classifies a failed transfer by."""
    import openstack.exceptions

    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_dst_failure_cleanup as cleanup_module,
    )

    src, dst, state_dir = _left_by_a_failed_transfer(tmp_path)
    delete = dst.delete_volume

    def slow(name_or_id=None, wait=True, timeout=None, force=False):
        if name_or_id == "dvol-data":
            raise openstack.exceptions.ResourceTimeout(
                "Timeout waiting for Volume:dvol-data to delete"
            )
        return delete(name_or_id=name_or_id, wait=wait, timeout=timeout, force=force)

    dst.delete_volume = slow
    result = fake.run_module(
        cleanup_module, _cleanup_args(state_dir=state_dir), dst,
        shell_factory=fake.ShellFactory([dst, src]),
    )

    assert result["_exit_code"] in (None, 0), result
    assert not result.get("failed") and not result.get("warnings"), result
    assert "Timeout" not in str(result.get("msg", ""))
    assert result["volumes_left"] == ["dvol-data"]
    assert sorted(dst.volumes) == ["dvol-data", "unrelated"]  # dvol-boot detached and deleted


@pytest.mark.parametrize("gone", ["the conversion host", "the record"])
def test_the_destination_cleanup_that_cannot_start_gives_up_without_failing(tmp_path, gone):
    """SDD 6.6 (review RV32B-02): best effort to the end - a cleanup that cannot even start, its
    conversion host's server gone or the workload's record not its own, says so in its result and
    exits; the rescue goes on, and the rollback deletes by the record what is left."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_dst_failure_cleanup as cleanup_module,
    )

    src, dst, state_dir = _left_by_a_failed_transfer(tmp_path)
    if gone == "the conversion host":
        del dst.servers["dst-conv"]
        expected = ["dvol-boot", "dvol-data"]
    else:
        warm_migration.record_destination_volumes(state_dir, "srv-other", TRANSFERRED, "vm9")
        os.replace(
            warm_migration.WarmState.path_for(state_dir, "srv-other"),
            warm_migration.WarmState.path_for(state_dir, "srv-1"),
        )
        expected = []
    result = fake.run_module(
        cleanup_module, _cleanup_args(state_dir=state_dir), dst,
        shell_factory=fake.ShellFactory([dst, src]),
    )

    assert result["_exit_code"] in (None, 0), result
    assert sorted(result["volumes_left"]) == expected
    assert "gave up" in result["msg"] or "cannot read the record" in result["msg"], result


def test_the_destination_cleanup_with_neither_map_nor_record_removes_nothing(tmp_path):
    """SDD 6.6 (audit A-08): no map and no record of the workload: nothing is the transfer's."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_dst_failure_cleanup as cleanup_module,
    )

    src, dst = _clouds()
    dst.add_volume("unrelated", "keep-me", 1)
    result = fake.run_module(
        cleanup_module,
        _cleanup_args(state_dir=str(tmp_path / "workload_warm")),
        dst,
        shell_factory=fake.ShellFactory([dst, src]),
    )

    assert result["_exit_code"] in (None, 0), result
    assert sorted(dst.volumes) == ["unrelated"]
