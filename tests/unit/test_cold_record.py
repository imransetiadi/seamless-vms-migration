"""The cold import's record of its workload, and the cold rollback that reads it (SDD 6.3, 7).

The cold transfer creates new destination volumes each time and a cold retry never reuses
them, so a cold rollback deletes exactly the volumes the cold import recorded - and keeps any
other volume attached to the destination server (the owner's choice, 2026-10-14).
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os

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
