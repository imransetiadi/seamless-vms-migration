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

    (boot,) = dst.volumes
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
    rollback = lambda: WarmRollback(  # noqa: E731
        dst, WarmState.load(state_dir, "srv-1"), server_name="vm1", match_by_name=True, timeout=5
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

    names = [c[0] for c in dst.calls if c[0] in ("create_volume", "wait_for_status",
                                                  "set_volume_bootable_status")]
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
    """SDD 6.3: an earlier volume the re-run cannot delete fails the transfer with the record
    unchanged - and the new volume, recorded nowhere, is removed."""
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
    # every volume left in the destination is recorded: this run's data volume was removed
    assert sorted(entry["dest_id"] for entry in recorded.values()) == sorted(dst.volumes)


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
