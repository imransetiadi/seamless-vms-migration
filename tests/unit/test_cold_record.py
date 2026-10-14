"""The cold import's record of its workload, and the cold rollback that reads it (SDD 6.3, 7).

The cold transfer creates new destination volumes each time and a cold retry never reuses
them, so a cold rollback deletes exactly the volumes the cold import recorded - and keeps any
other volume attached to the destination server (the owner's choice, 2026-10-14).
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
from unittest import mock

import pytest

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import warm_migration
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


def _record(state_dir, volume_map, data=None):
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_record_volumes as record_module,
    )

    args = {"data": data or fake.workload_data(), "state_dir": state_dir, "volume_map": volume_map}
    return fake.run_module(record_module, args, None)


def test_the_cold_import_records_the_volumes_its_transfer_created(tmp_path):
    state_dir = str(tmp_path / "workload_warm")

    result = _record(state_dir, TRANSFERRED)

    assert result["_exit_code"] in (None, 0), result
    assert result["changed"] is True
    assert result["dest_volume_ids"] == ["dvol-boot", "dvol-data"]
    state = WarmState.load(state_dir, "srv-1")
    # as the warm path records its volumes (SDD 6.3): a warm sync reusing one reads all four
    assert state.dest_volumes == {
        "/dev/vda": {"dest_id": "dvol-boot", "size": 20, "bootable": True, "name": "vm1-boot"},
        "/dev/vdb": {"dest_id": "dvol-data", "size": 10, "bootable": False, "name": "vm1-data"},
    }
    assert state.server_name == "vm1"
    assert state.passes == [] and state.destination_server_id is None


def test_the_record_keeps_what_the_state_file_holds_and_a_rerun_changes_nothing(tmp_path):
    state_dir = str(tmp_path / "workload_warm")
    # import_workload_create_instance with warm_state_dir records the server (SDD 6.5)
    warm_migration.record_destination_server(state_dir, "srv-1", "dst-srv", "vm1")

    _record(state_dir, TRANSFERRED)
    again = _record(state_dir, TRANSFERRED)

    assert again["changed"] is False
    state = WarmState.load(state_dir, "srv-1")
    assert state.destination_server_id == "dst-srv"
    assert sorted(state.dest_volumes) == ["/dev/vda", "/dev/vdb"]


def test_a_disk_the_transfer_created_no_volume_for_is_not_recorded(tmp_path):
    state_dir = str(tmp_path / "workload_warm")

    result = _record(state_dir, {"/dev/vda": {"source_id": "svol-boot", "size": 20}})

    assert result["changed"] is False and result["dest_volume_ids"] == []
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
    assert result["state_deleted"] is True
    assert set(dst.volumes) == {"unrelated", "dvol-extra"}


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
    assert result["state_deleted"] is True
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
