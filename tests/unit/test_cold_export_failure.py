"""A cold export that fails puts back what it took (Seamless Migrate SDD 6.6, review RV6-01, layer
c): the export module fails with its volume map, so the role's rescue runs the source cleanup, and
the cleanup takes a partial map. The rollback that follows starts the source VM, which must then
have every disk it had."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
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

CONV = "10.0.0.10"
DISKS = {"vol-root": "/dev/vda", "vol-data1": "/dev/vdb", "vol-data2": "/dev/vdc"}


class SourceHost(fake.ShellFactory):
    """The source conversion host: its shells share the port map (PORT_MAP_FILE), every port it
    ever held is in ``handed_out``, and only the first ``ready_exports`` NBD exports (all with
    None) ever answer ``qemu-img info``."""

    def __init__(self, cloud, ready_exports=None):
        super().__init__([cloud])
        self.ports = set()
        self.handed_out = set()
        self.ready_exports = ready_exports
        self.answering = set()

    def __call__(self, address, ssh_user, key_path=None):
        shell = super().__call__(address, ssh_user, key_path)
        host, cmd_out = self, shell.cmd_out

        def out(command, **kwargs):
            if command == ["sudo", "cat", volume_common.PORT_MAP_FILE]:
                shell.commands.append(list(command))
                return json.dumps(sorted(host.ports))
            if command[:2] == ["qemu-img", "info"]:
                export = command[2]
                if host.ready_exports is not None and export not in host.answering:
                    if len(host.answering) >= host.ready_exports:
                        shell.commands.append(list(command))
                        raise subprocess.CalledProcessError(1, command)
                    host.answering.add(export)
            return cmd_out(command, **kwargs)

        def sub(command, **kwargs):
            shell.commands.append(list(command))

            class PortMapWriter:
                def communicate(self, data=None, timeout=None):
                    host.ports = set(json.loads(data))
                    host.handed_out |= host.ports
                    return "", ""

            return PortMapWriter()

        shell.cmd_out, shell.cmd_sub = out, sub
        return shell


def _boot_from_volume_vm():
    src = fake.FakeCloud("src")
    src.add_conversion_host("src-conv", CONV)
    src.add_server("srv-1", "vm1")
    src.servers["srv-1"].status = "SHUTOFF"
    for volume_id, device in DISKS.items():
        src.add_volume(volume_id, "vm1-" + volume_id, 10, bootable=device == "/dev/vda",
                       server_id="srv-1", device=device)
    data = fake.workload_data(volumes=[
        fake.server_volume_data(volume_id, "vm1-" + volume_id, 10, device, "srv-1",
                                bootable=device == "/dev/vda")
        for volume_id, device in DISKS.items()
    ])
    return src, data


COMMON = {"conversion_host": {"id": "src-conv"}, "ssh_key_path": "/keys/k",
          "ssh_user": "cloud-user", "timeout": 3}


def _export_then_rescue(src, host, data, meanwhile=None):
    """Run the export module, then - as the role's rescue does, only `when: 'volume_map' in
    exports` - the source cleanup with what the export reported; ``meanwhile`` runs between."""
    common = dict(COMMON, data=data)
    with mock.patch.object(volume_common.time, "sleep"):
        try:
            exported = fake.run_module(export_module, dict(common), src, host)
        except Exception as err:  # pylint: disable=broad-except
            # an exception out of the module: Ansible reports MODULE FAILURE, without a map
            exported = {"failed": True, "msg": str(err)}
        if meanwhile is not None:
            meanwhile()
        cleaned = None
        if "volume_map" in exported:
            cleaned = fake.run_module(cleanup_module, dict(
                common, transfer_uuid=exported["transfer_uuid"], volume_map=exported["volume_map"],
            ), src, host)
    return exported, cleaned


def _holders(src, volume_id):
    return sorted(a["server_id"] for a in src.volumes[volume_id].attachments)


def _fail_device_path_of(volume_id):
    wait = volume_common.OpenStackVolumeBase._wait_for_volume_dev_path

    def wait_for_device(self, conn, volume, vm, timeout):
        if volume.id == volume_id:
            # Nova attached it, but the conversion host never showed its device
            raise RuntimeError("Timed out waiting for volume device path!")
        return wait(self, conn, volume, vm, timeout)

    return wait_for_device


@pytest.mark.parametrize("failure", [
    "snapshot", "root copy", "converter attach", "device path", "nbd export",
])
def test_a_failed_cold_export_leaves_the_source_vm_as_it_was(failure):
    src, data = _boot_from_volume_vm()
    host = SourceHost(src, ready_exports=0 if failure == "nbd export" else None)
    injected = {
        "snapshot": ("create_volume_snapshot", lambda call: True),
        "root copy": ("create_volume", lambda call: call.get("snapshot_id") is not None),
        "converter attach": ("attach_volume",
                             lambda call: call["server"] == "src-conv" and call["volume"] == "vol-data1"),
    }
    if failure in injected:
        method, when = injected[failure]
        src.fail_on[method] = when
    patch = mock.patch.object(
        volume_common.OpenStackVolumeBase, "_wait_for_volume_dev_path",
        _fail_device_path_of("vol-data2") if failure == "device path"
        else volume_common.OpenStackVolumeBase._wait_for_volume_dev_path,
    )

    with patch:
        exported, cleaned = _export_then_rescue(src, host, data)

    assert exported.get("failed"), exported
    assert "volume_map" in exported and "transfer_uuid" in exported, exported
    assert cleaned is not None and cleaned["_exit_code"] in (None, 0), cleaned
    for volume_id in DISKS:  # every disk is the source VM's again, and only its
        assert _holders(src, volume_id) == ["srv-1"], (volume_id, src.volumes[volume_id].attachments)
    assert set(src.volumes) == set(DISKS)  # no copy of the root volume is left
    assert src.snapshots == {}
    assert host.ports == set()  # every port the export claimed is free again


def test_the_ports_of_a_failed_export_stay_claimed_until_the_rescue_frees_them():
    """The export records each port as soon as it claims it: after a failure the ports stay
    claimed - no other migration takes one while an NBD export may still hold it - and the
    rescue frees every one, also the port of the disk whose export never came up."""
    src, data = _boot_from_volume_vm()
    host = SourceHost(src, ready_exports=1)  # the root copy's export comes up, the next one never
    claimed = []

    exported, cleaned = _export_then_rescue(
        src, host, data, meanwhile=lambda: claimed.append(set(host.ports))
    )

    assert exported.get("failed") and cleaned["_exit_code"] in (None, 0), (exported, cleaned)
    assert claimed == [host.handed_out] and len(host.handed_out) == 2
    assert {m["port"] for m in exported["volume_map"].values()} - {None} == host.handed_out
    assert host.ports == set()


def test_a_successful_export_and_its_cleanup_still_map_and_put_back_every_disk():
    """The export records the snapshot before the copy now: a successful export still maps every
    disk - the root copy in place of the boot volume - and the cleanup after the transfer puts
    every disk back, the copy and its snapshot gone, its ports free."""
    src, data = _boot_from_volume_vm()
    host = SourceHost(src)

    with mock.patch.object(volume_common.time, "sleep"):
        exported = fake.run_module(export_module, dict(COMMON, data=data), src, host)
        volume_map = exported["volume_map"]
        assert exported["_exit_code"] in (None, 0), exported
        assert sorted(volume_map) == ["/dev/vda", "/dev/vdb", "/dev/vdc"]
        root = volume_map["/dev/vda"]
        assert root["snap_id"] in src.snapshots and root["source_id"] not in DISKS
        assert {m["port"] for m in volume_map.values()} == host.ports and None not in host.ports
        assert _holders(src, "vol-root") == ["srv-1"]
        assert _holders(src, "vol-data1") == _holders(src, root["source_id"]) == ["src-conv"]
        cleaned = fake.run_module(cleanup_module, dict(
            COMMON, data=data, transfer_uuid=exported["transfer_uuid"], volume_map=volume_map,
        ), src, host)

    assert cleaned["_exit_code"] in (None, 0), cleaned
    for volume_id in DISKS:
        assert _holders(src, volume_id) == ["srv-1"], volume_id
    assert set(src.volumes) == set(DISKS) and src.snapshots == {} and host.ports == set()


def _image_booted_vm():
    src = fake.FakeCloud("src")
    src.add_conversion_host("src-conv", CONV)
    src.add_server("srv-1", "vm1", image={"id": "img-base"})
    src.servers["srv-1"].status = "SHUTOFF"
    src.add_volume("vol-data1", "vm1-data1", 10, server_id="srv-1", device="/dev/vdb")
    data = fake.workload_data(
        volumes=[fake.server_volume_data("vol-data1", "vm1-data1", 10, "/dev/vdb", "srv-1")],
        image_ref={"name": "img-base"},
    )
    return src, data


def test_a_successful_image_copy_maps_the_copy_and_the_cleanup_removes_it():
    src, data = _image_booted_vm()
    host = SourceHost(src)

    with mock.patch.object(volume_common.time, "sleep"):
        exported = fake.run_module(export_module, dict(COMMON, data=data), src, host)
        root = exported["volume_map"]["/dev/vda"]
        copy = src.volumes[root["source_id"]]
        assert exported["_exit_code"] in (None, 0), exported
        assert root["image_id"] in src.images and copy.image_id == root["image_id"]
        assert (root["name"], root["size"], root["bootable"]) == (copy.name, copy.size, True)
        cleaned = fake.run_module(cleanup_module, dict(
            COMMON, data=data, transfer_uuid=exported["transfer_uuid"],
            volume_map=exported["volume_map"],
        ), src, host)

    assert cleaned["_exit_code"] in (None, 0), cleaned
    assert src.images == {} and set(src.volumes) == {"vol-data1"}
    assert _holders(src, "vol-data1") == ["srv-1"]


def test_a_failed_image_copy_leaves_no_image_behind():
    """An image-booted VM with boot_disk_copy: the export makes an image of the VM, then a volume
    from it. The image is recorded as soon as it exists, so the cleanup removes it."""
    src, data = _image_booted_vm()
    src.fail_on["create_volume"] = lambda call: call.get("image") is not None
    host = SourceHost(src)

    exported, cleaned = _export_then_rescue(src, host, data)

    assert exported.get("failed") and "volume_map" in exported, exported
    assert cleaned is not None and cleaned["_exit_code"] in (None, 0), cleaned
    assert src.images == {}
    assert _holders(src, "vol-data1") == ["srv-1"]


def _cleanup(src, volume_map):
    with mock.patch.object(volume_common, "RemoteShell", SourceHost(src)):
        return cleanup_module.OpenStackSourceHostCleanup(
            src, "src-conv", "/keys/k", "cloud-user", "srv-1", "uuid-1", volume_map, timeout=3,
        )


def _mapping(source_id, **fields):
    mapping = dict(source_dev=None, source_id=source_id, dest_dev=None, dest_id=None, snap_id=None,
                   image_id=None, name=source_id, size=10, port=None, url=None, progress=None,
                   bootable=False)
    mapping.update(fields)
    return mapping


def test_the_source_cleanup_never_deletes_the_source_vms_own_boot_volume():
    """Before the export made the root copy, the map's root entry names the source VM's own boot
    volume: the cleanup leaves it where it is. It used to send Cinder a delete for it, which
    Cinder refuses for an attached volume, so the whole cleanup failed."""
    src, _data = _boot_from_volume_vm()
    volume_map = {device: _mapping(volume_id) for volume_id, device in DISKS.items()}

    with mock.patch.object(volume_common.time, "sleep"):
        _cleanup(src, volume_map).close_exports()

    assert set(src.volumes) == set(DISKS)
    assert not [call for call in src.calls_to("delete_volume") if call[1]["name_or_id"] == "vol-root"]
    assert _holders(src, "vol-root") == ["srv-1"]


def test_the_source_cleanup_frees_only_the_ports_the_map_records():
    src, _data = _boot_from_volume_vm()
    volume_map = {"/dev/vdb": _mapping("vol-data1", port=50001), "/dev/vdc": _mapping("vol-data2")}

    assert _cleanup(src, volume_map).claimed_ports == [50001]
