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


def test_create_instance_module_finishes_a_recorded_server(tmp_path):
    """SDD §6.5: a re-run after the first attempt failed once Nova had created the server
    (a timeout while it booted, an error adding floating IPs) waits for that server and adds
    the floating IPs it lacks, instead of only reporting it."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_create_instance as create_module,
    )

    dst, state_dir = migrated(tmp_path)
    finished = []

    def record_fips(self, conn, sdk_srv):
        finished.append(sdk_srv.id)

    with mock.patch.object(server.Server, "create") as create, mock.patch.object(
        server.Server, "_create_floating_ips", record_fips
    ):
        result = fake.run_module(create_module, _create_instance_args(state_dir), dst)

    assert result["_exit_code"] in (None, 0), result
    assert result["server_id"] == "dst-srv"
    create.assert_not_called()
    assert finished == ["dst-srv"]
    assert ("wait_for_server", {"server": "dst-srv"}) in [
        (c[0], {"server": c[1].get("server")}) for c in dst.calls_to("wait_for_server")
    ]


def test_create_instance_module_finishes_a_recorded_server_an_operator_renamed(tmp_path):
    """SDD §6 (lead 8, decided): a recorded server an operator renamed is finished like any
    recorded server. A refusal would fail the cutover after the source stopped, and the automatic
    rollback (SDD §8, plan.verification.auto_rollback, on by default) would then delete that very
    server - the one the rename set aside - with the recorded volumes; a fresh server cannot be
    built from those volumes anyway, as the renamed server holds them."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_create_instance as create_module,
    )

    dst, state_dir = migrated(tmp_path)
    dst.servers["dst-srv"].name = "vm1-aside"
    finished = []

    def record_fips(self, conn, sdk_srv):
        finished.append(sdk_srv.id)

    with mock.patch.object(server.Server, "create") as create, mock.patch.object(
        server.Server, "_create_floating_ips", record_fips
    ):
        result = fake.run_module(create_module, _create_instance_args(state_dir), dst)

    assert result["_exit_code"] in (None, 0), result
    assert result["server_id"] == "dst-srv"
    create.assert_not_called()
    assert finished == ["dst-srv"]
    assert "dst-srv" in dst.servers


@pytest.mark.parametrize("recorded", ["gone", "none", "no state dir"])
def test_create_instance_module_finish_only_never_creates_a_server(tmp_path, recorded):
    """SDD §6.4 (audit A-07): the warm cutover's re-run finishes the server its warm state records,
    passing an empty block device mapping. When that server vanished between the role's lookup and
    this call, a create would build a server without the synchronised volumes - an image-booted VM
    boots from its image - and record it as the destination, after the source stopped. With
    finish_only the module never creates one - not even when warm_state_dir names no directory -:
    it fails, naming the rollback, and leaves the record as it is."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_create_instance as create_module,
    )

    dst, state_dir = migrated(tmp_path)
    del dst.servers["dst-srv"]  # gone since the role looked it up
    if recorded == "none":
        warm_migration.record_destination_server(state_dir, "srv-1", None)
    args = dict(_create_instance_args(state_dir), block_device_mapping=[], finish_only=True)
    if recorded == "no state dir":
        args["warm_state_dir"] = ""
    created = []

    def fake_create(self, conn, block_device_mapping, on_create=None):
        created.append(block_device_mapping)
        new = dst.add_server("dst-new", "vm1")
        if on_create is not None:  # none without a warm state
            on_create(new)
        return new

    with mock.patch.object(server.Server, "create", fake_create), mock.patch.object(
        server.Server, "from_sdk", return_value=mock.Mock(data={"params": {"name": "vm1"}})
    ):
        result = fake.run_module(create_module, args, dst)

    assert created == []  # never a server without the synchronised volumes
    assert result["_exit_code"] not in (None, 0) and result.get("failed") is True, result
    assert "rollback_workloads.yml" in result["msg"]
    if recorded == "gone":
        assert "dst-srv was not found" in result["msg"]
    expected = None if recorded == "none" else "dst-srv"
    assert WarmState.load(state_dir, "srv-1").destination_server_id == expected


def test_create_instance_module_replaces_a_recorded_server_that_is_gone(tmp_path):
    """SDD §6.4 (audit A-07): without finish_only - the first create, and the cold import, which
    records its server too - a recorded server that is gone (deleted by hand since) is replaced by
    the one the call creates from the block device mapping it got: a refusal would fail a cold
    retry after the source stopped and its volumes were transferred."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_create_instance as create_module,
    )

    dst, state_dir = migrated(tmp_path)
    del dst.servers["dst-srv"]
    created = []

    def fake_create(self, conn, block_device_mapping, on_create=None):
        created.append(block_device_mapping)
        new = dst.add_server("dst-new", "vm1")
        on_create(new)
        return new

    with mock.patch.object(server.Server, "create", fake_create), mock.patch.object(
        server.Server, "from_sdk", return_value=mock.Mock(data={"params": {"name": "vm1"}})
    ):
        result = fake.run_module(create_module, _create_instance_args(state_dir), dst)

    assert result["_exit_code"] in (None, 0), result
    assert result["changed"] is True and result["server_id"] == "dst-new"
    assert created == [BDM]
    assert WarmState.load(state_dir, "srv-1").destination_server_id == "dst-new"


# --- rollback ------------------------------------------------------------------


def test_rollback_deletes_recorded_server_and_keeps_volumes(tmp_path):
    dst, state_dir = migrated(tmp_path)

    result = WarmRollback(dst, WarmState.load(state_dir, "srv-1"), timeout=5).run()

    assert result == {
        "changed": True,
        "deleted_server_id": "dst-srv",
        "deleted_volume_ids": [],
        "kept_volume_ids": [],
        "deleted_port_ids": [],
        "state_cleared": False,
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
    assert result["state_cleared"] is True
    assert set(dst.volumes) == {"unrelated", "dvol-extra"}
    # emptied, not removed: the sign that the migration recorded the workload (SDD 6.3)
    state = WarmState.load(state_dir, "srv-1")
    assert state.exists() and (state.dest_volumes, state.passes) == ({}, [])


def test_rollback_keeps_state_while_a_snapshot_is_pending(tmp_path):
    pending = {"transfer_uuid": "uuid-4", "volume_map": {"/dev/vda": {"tmp_volume_id": "t"}}}
    dst, state_dir = migrated(tmp_path, pending_snapshot=pending)

    result = WarmRollback(dst, WarmState.load(state_dir, "srv-1"), timeout=5).run(
        delete_volumes=True
    )

    assert result["state_cleared"] is True
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
    assert result["state_cleared"] is True  # the kept volume is no longer recorded
    assert WarmState.load(state_dir, "srv-1").dest_volumes == {}


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


def test_rollback_deletes_the_ports_neutron_port_mode_created(tmp_path):
    """SDD 6.6: Nova keeps ports it did not create when their server is deleted. The rollback
    deletes those of the workload's source ports - unbound, or bound to the deleted server -
    and keeps every other port."""
    dst, state_dir = migrated(tmp_path)
    dst.add_port("p1", "fa:16:3e:00:00:01", description="os-migrate:sp-1", device_id="dst-srv")
    dst.add_port("p2", "fa:16:3e:00:00:02", description="os-migrate:sp-2")
    dst.add_port("p-other-vm", "fa:16:3e:00:00:09", description="os-migrate:sp-9")
    dst.add_port("p-in-use", "fa:16:3e:00:00:03", description="os-migrate:sp-2", device_id="other")
    dst.add_port("p-operator", "fa:16:3e:00:00:04", device_id="dst-srv")

    result = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), source_port_ids=["sp-1", "sp-2"], timeout=5
    ).run()

    assert result["deleted_server_id"] == "dst-srv"
    assert result["deleted_port_ids"] == ["p1", "p2"]
    assert set(dst.ports) == {"p-other-vm", "p-in-use", "p-operator"}


def test_rollback_deletes_the_ports_of_a_cutover_that_created_no_server(tmp_path):
    """A cutover can fail between creating the ports and creating the server: there is no
    server to delete, the ports go anyway."""
    dst, state_dir = migrated(tmp_path, record=False)
    dst.add_port("p1", "fa:16:3e:00:00:01", description="os-migrate:sp-1")

    result = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), source_port_ids=["sp-1"], timeout=5
    ).run()

    assert result["deleted_server_id"] is None
    assert result["deleted_port_ids"] == ["p1"] and result["changed"] is True
    assert "dst-srv" in dst.servers  # neither recorded nor matched by name: not this workload's
    assert not dst.ports


def test_rollback_checks_each_port_itself_before_deleting_it(tmp_path):
    """A cloud that ignored the description filter would list every port: only the
    workload's marked ports are deleted all the same."""
    dst, state_dir = migrated(tmp_path)
    dst.add_port("p1", "fa:16:3e:00:00:01", description="os-migrate:sp-1")
    dst.add_port("p-unmarked", "fa:16:3e:00:00:05")
    dst.add_port("p-other-vm", "fa:16:3e:00:00:09", description="os-migrate:sp-9")
    dst.network.ports = lambda **query: list(dst.ports.values())

    result = WarmRollback(
        dst, WarmState.load(state_dir, "srv-1"), source_port_ids=["sp-1"], timeout=5
    ).run()

    assert result["deleted_port_ids"] == ["p1"]
    assert set(dst.ports) == {"p-unmarked", "p-other-vm"}


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
    assert result["state_cleared"] is True

    again = fake.run_module(rollback_module, args, dst)
    assert again["changed"] is False


def test_rollback_module_deletes_no_port_when_told(tmp_path):
    """SDD 6.4, 6.6 (review RV-02): a cancel's cleanup deletes no port - it made none, and an unbound
    port keyed to the VM's source port may be another plan's migration of the VM."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_rollback as rollback_module,
    )

    dst, state_dir = migrated(tmp_path)
    dst.add_port("p-plan-b", "fa:16:3e:00:00:01", description="os-migrate:sp-1")  # unbound
    port = {
        "type": "openstack.network.ServerPort",
        "params": {"mac_address": "fa:16:3e:00:00:01"},
        "_info": {"id": "sp-1", "device_id": "srv-1", "device_owner": "compute:nova"},
    }
    args = {
        "cloud": "dst",
        "data": fake.workload_data(ports=[port]),
        "state_dir": state_dir,
        "delete_dest_volumes": True,
        "delete_ports": False,
    }

    result = fake.run_module(rollback_module, args, dst)

    assert result["_exit_code"] in (None, 0), result
    assert result["deleted_port_ids"] == []
    assert "p-plan-b" in dst.ports
    assert result["deleted_volume_ids"] == ["dvol-boot", "dvol-data"]  # its own data path still goes


def test_rollback_module_deletes_the_workloads_neutron_ports(tmp_path):
    """The module names the workload's source ports from its data (SDD 6.6)."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_rollback as rollback_module,
    )

    dst, state_dir = migrated(tmp_path)
    dst.add_port("p1", "fa:16:3e:00:00:01", description="os-migrate:sp-1", device_id="dst-srv")
    dst.add_port("p-other-vm", "fa:16:3e:00:00:09", description="os-migrate:sp-9")
    port = {
        "type": "openstack.network.ServerPort",
        "params": {"mac_address": "fa:16:3e:00:00:01"},
        "_info": {"id": "sp-1", "device_id": "srv-1", "device_owner": "compute:nova"},
    }
    args = {"cloud": "dst", "data": fake.workload_data(ports=[port]), "state_dir": state_dir}

    result = fake.run_module(rollback_module, args, dst)

    assert result["_exit_code"] in (None, 0), result
    assert result["deleted_port_ids"] == ["p1"]
    assert set(dst.ports) == {"p-other-vm"}
    again = fake.run_module(rollback_module, args, dst)
    assert again["changed"] is False and again["deleted_port_ids"] == []
