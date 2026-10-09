"""Destination-side helpers of the warm path: recording the destination
server at cutover and rolling a workload back (SDD 6.5)."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
from unittest import mock

import pytest

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
    server,
    warm_migration,
)
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.warm_migration import (
    WarmRollback,
    WarmState,
)
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake

BDM = [
    {
        "boot_index": 0,
        "delete_on_termination": False,
        "destination_type": "volume",
        "device_name": "vda",
        "source_type": "volume",
        "uuid": "dvol-boot",
    }
]


def migrated(tmp_path, pending_snapshot=None, record=True):
    """A destination cloud holding the migrated server of source srv-1."""
    dst = fake.FakeCloud("dst")
    dst.add_conversion_host("dst-conv", "10.1.0.10")
    dst.add_server("dst-srv", "vm1")
    dst.add_server("other", "vm10")
    dst.add_volume("dvol-boot", "vm1-boot", 20, bootable=True, server_id="dst-srv",
                   device="/dev/vda")
    dst.add_volume("dvol-data", "vm1-data", 10, server_id="dst-srv", device="/dev/vdb")
    dst.add_volume("unrelated", "keep-me", 1)
    state_dir = str(tmp_path / "workload_warm")
    if record:
        WarmState(
            state_dir,
            "srv-1",
            server_name="vm1",
            transfer_uuid="uuid-3",
            dest_volumes={
                "/dev/vda": {"dest_id": "dvol-boot", "size": 20, "bootable": True,
                             "name": "vm1-boot"},
                "/dev/vdb": {"dest_id": "dvol-data", "size": 10, "bootable": False,
                             "name": "vm1-data"},
            },
            passes=[{"number": 1, "kind": "full"}],
            pending_snapshot=pending_snapshot,
            destination_server_id="dst-srv",
        ).save()
    return dst, state_dir


# --- recording the destination server ----------------------------------------


def test_server_create_reports_new_server_before_waiting():
    ser = server.Server.from_data(fake.workload_data())
    conn = mock.Mock()
    conn.compute.create_server.return_value = fake.Obj(id="dst-srv-1")
    events = []
    conn.compute.wait_for_server.side_effect = (
        lambda srv, **kwargs: events.append("wait") or srv
    )

    with mock.patch.object(
        server.Server, "sdk_params", return_value={"name": "vm1"}
    ), mock.patch.object(
        server.Server, "update_sdk_params_networks_simple"
    ), mock.patch.object(server.Server, "_create_floating_ips"):
        created = ser.create(
            conn, BDM, on_create=lambda srv: events.append(("created", srv.id))
        )
        ser.create(conn, BDM)

    assert created.id == "dst-srv-1"
    assert events == [("created", "dst-srv-1"), "wait", "wait"]
    assert conn.compute.create_server.call_args[1]["block_device_mapping"] == BDM


def test_record_destination_server_keeps_the_rest_of_the_state(tmp_path):
    dst, state_dir = migrated(tmp_path)
    state = WarmState.load(state_dir, "srv-1")
    state.destination_server_id = None
    state.save()

    warm_migration.record_destination_server(state_dir, "srv-1", "new-srv", "vm1")

    recorded = WarmState.load(state_dir, "srv-1")
    assert recorded.destination_server_id == "new-srv"
    assert recorded.dest_volumes == state.dest_volumes
    assert recorded.passes == state.passes

    warm_migration.record_destination_server(str(tmp_path / "fresh"), "srv-2", "s2", "vm2")
    fresh = WarmState.load(str(tmp_path / "fresh"), "srv-2")
    assert fresh.destination_server_id == "s2" and fresh.server_name == "vm2"


def _create_instance_args(state_dir):
    return {
        "cloud": "dst",
        "data": fake.workload_data(),
        "block_device_mapping": BDM,
        "warm_state_dir": state_dir,
    }


def test_create_instance_module_records_the_destination_server(tmp_path):
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_create_instance as create_module,
    )

    dst = fake.FakeCloud("dst")
    state_dir = str(tmp_path / "workload_warm")
    WarmState(state_dir, "srv-1", server_name="vm1").save()
    seen_on_disk = []

    def fake_create(self, conn, block_device_mapping, on_create=None):
        new = dst.add_server("dst-new", "vm1")
        on_create(new)
        seen_on_disk.append(WarmState.load(state_dir, "srv-1").destination_server_id)
        return new

    with mock.patch.object(server.Server, "create", fake_create), mock.patch.object(
        server.Server, "from_sdk", return_value=mock.Mock(data={"params": {"name": "vm1"}})
    ):
        result = fake.run_module(create_module, _create_instance_args(state_dir), dst)

    assert result["_exit_code"] in (None, 0), result
    assert result["changed"] is True and result["server_id"] == "dst-new"
    assert seen_on_disk == ["dst-new"]  # recorded before waiting for ACTIVE
    assert WarmState.load(state_dir, "srv-1").destination_server_id == "dst-new"


def test_create_instance_module_reuses_a_recorded_server(tmp_path):
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_create_instance as create_module,
    )

    dst, state_dir = migrated(tmp_path)

    with mock.patch.object(server.Server, "create") as create:
        result = fake.run_module(create_module, _create_instance_args(state_dir), dst)

    assert result["changed"] is False and result["server_id"] == "dst-srv"
    create.assert_not_called()


# --- rollback ------------------------------------------------------------------


def test_rollback_deletes_recorded_server_and_keeps_volumes(tmp_path):
    dst, state_dir = migrated(tmp_path)

    result = WarmRollback(dst, WarmState.load(state_dir, "srv-1"), timeout=5).run()

    assert result == {
        "changed": True,
        "deleted_server_id": "dst-srv",
        "deleted_volume_ids": [],
        "kept_volume_ids": [],
        "state_deleted": False,
    }
    assert "dst-srv" not in dst.servers and "other" in dst.servers
    assert {"dvol-boot", "dvol-data", "unrelated"} <= set(dst.volumes)
    state = WarmState.load(state_dir, "srv-1")
    assert state.destination_server_id is None
    assert sorted(state.dest_volumes) == ["/dev/vda", "/dev/vdb"]
    assert state.passes == [{"number": 1, "kind": "full"}]


def test_rollback_deletes_volumes_and_state_when_asked(tmp_path):
    dst, state_dir = migrated(tmp_path)
    # attached to the destination server after the cutover: not the migration's (SDD 6.5)
    dst.add_volume("dvol-extra", "attached-later", 5)
    dst.volumes["dvol-extra"].attachments.append(
        {"server_id": "dst-srv", "device": "/dev/vdc", "volume_id": "dvol-extra"}
    )

    result = WarmRollback(dst, WarmState.load(state_dir, "srv-1"), timeout=5).run(
        delete_volumes=True
    )

    assert result["deleted_server_id"] == "dst-srv"
    assert result["deleted_volume_ids"] == ["dvol-boot", "dvol-data"]
    assert result["kept_volume_ids"] == ["dvol-extra"]
    assert result["state_deleted"] is True
    assert set(dst.volumes) == {"unrelated", "dvol-extra"}
    assert not os.path.exists(WarmState.path_for(state_dir, "srv-1"))


def test_rollback_keeps_state_while_a_snapshot_is_pending(tmp_path):
    pending = {"transfer_uuid": "uuid-4", "volume_map": {"/dev/vda": {"tmp_volume_id": "t"}}}
    dst, state_dir = migrated(tmp_path, pending_snapshot=pending)

    result = WarmRollback(dst, WarmState.load(state_dir, "srv-1"), timeout=5).run(
        delete_volumes=True
    )

    assert result["state_deleted"] is False
    state = WarmState.load(state_dir, "srv-1")
    assert state.pending_snapshot == pending  # source temporaries still to clean up
    assert state.dest_volumes == {} and state.passes == []
    assert state.destination_server_id is None


def test_rollback_detaches_volumes_left_on_the_conversion_host(tmp_path):
    dst, state_dir = migrated(tmp_path)
    dst.volumes["dvol-data"].attachments.append(
        {"server_id": "dst-conv", "device": "/dev/vdb", "volume_id": "dvol-data"}
    )

    WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), timeout=5, conversion_host="dst-conv"
    ).run(delete_volumes=True)

    assert "dvol-data" not in dst.volumes
    assert ("detach_volume", {"server": "dst-conv", "volume": "dvol-data"}) in dst.calls


def test_rollback_keeps_volumes_held_by_another_server(tmp_path):
    """A shared volume attached to a server outside the migration is never
    detached from it or deleted (the role names only the conversion host)."""
    dst, state_dir = migrated(tmp_path)
    dst.volumes["dvol-data"].attachments.append(
        {"server_id": "other", "device": "/dev/vdb", "volume_id": "dvol-data"}
    )

    result = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), timeout=5, conversion_host="dst-conv"
    ).run(delete_volumes=True)

    assert result["deleted_volume_ids"] == ["dvol-boot"]
    assert result["kept_volume_ids"] == ["dvol-data"]
    assert "dvol-data" in dst.volumes
    assert not [c for c in dst.calls_to("detach_volume") if c[1]["volume"] == "dvol-data"]
    assert result["state_deleted"] is True  # the kept volume is no longer recorded
    assert not os.path.exists(WarmState.path_for(state_dir, "srv-1"))


def test_rollback_without_a_conversion_host_keeps_every_attached_volume(tmp_path):
    dst, state_dir = migrated(tmp_path)
    dst.volumes["dvol-data"].attachments.append(
        {"server_id": "dst-conv", "device": "/dev/vdb", "volume_id": "dvol-data"}
    )

    result = WarmRollback(dst, WarmState.load(state_dir, "srv-1"), timeout=5).run(
        delete_volumes=True
    )

    assert result["kept_volume_ids"] == ["dvol-data"]
    assert "dvol-data" in dst.volumes


def test_rollback_without_a_record_does_nothing(tmp_path):
    dst, state_dir = migrated(tmp_path, record=False)

    result = WarmRollback(dst, WarmState.load(state_dir, "srv-1"), timeout=5).run(
        delete_volumes=True
    )

    assert result["changed"] is False
    assert "dst-srv" in dst.servers
    assert not os.path.exists(state_dir)


def test_rollback_is_idempotent(tmp_path):
    dst, state_dir = migrated(tmp_path)
    WarmRollback(dst, WarmState.load(state_dir, "srv-1"), timeout=5).run()

    again = WarmRollback(dst, WarmState.load(state_dir, "srv-1"), timeout=5).run()

    assert again["changed"] is False
    assert not dst.calls_to("delete_server")[1:]


def test_rollback_can_match_the_destination_server_by_exact_name(tmp_path):
    dst, state_dir = migrated(tmp_path, record=False)
    state = WarmState.load(state_dir, "srv-1")

    result = WarmRollback(
        dst, state, server_name="vm1", match_by_name=True, timeout=5
    ).run()

    assert result["deleted_server_id"] == "dst-srv"
    assert "other" in dst.servers  # "vm10" is not "vm1"

    dst.add_server("twin-1", "vm1")
    dst.add_server("twin-2", "vm1")
    with pytest.raises(RuntimeError, match="vm1"):
        WarmRollback(dst, state, server_name="vm1", match_by_name=True, timeout=5).run()


def test_rollback_module(tmp_path):
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_rollback as rollback_module,
    )

    dst, state_dir = migrated(tmp_path)
    args = {
        "cloud": "dst",
        "data": fake.workload_data(),
        "state_dir": state_dir,
        "delete_dest_volumes": True,
    }

    result = fake.run_module(rollback_module, args, dst)

    assert result["_exit_code"] in (None, 0), result
    assert result["changed"] is True
    assert result["deleted_server_id"] == "dst-srv"
    assert result["deleted_volume_ids"] == ["dvol-boot", "dvol-data"]
    assert result["state_deleted"] is True

    again = fake.run_module(rollback_module, args, dst)
    assert again["changed"] is False
