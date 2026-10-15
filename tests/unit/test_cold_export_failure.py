"""A cold export that fails puts back what it took (Seamless Migrate SDD 6.6, review RV6-01, layer
c): the export module fails with its volume map, so the role's rescue runs the source cleanup, and
the cleanup takes a partial map. The rollback that follows starts the source VM, which must then
have every disk it had."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import re
import subprocess
from unittest import mock

import pytest

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import server as server_util
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
        assert (root["name"], root["size"], root["bootable"], copy.bootable) == (
            copy.name, copy.size, True, True
        )
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


def _shared_volume_vms():
    """Two shut-off VMs, each booted from its own volume, sharing a multi-attach data volume."""
    src = fake.FakeCloud("src")
    src.add_conversion_host("src-conv", CONV)
    names = {"srv-a": "vma", "srv-b": "vmb"}
    for server_id, name in names.items():
        src.add_server(server_id, name)
        src.servers[server_id].status = "SHUTOFF"
        src.add_volume("root-" + server_id, name + "-root", 10, bootable=True,
                       server_id=server_id, device="/dev/vda")
    src.add_volume("vol-shared", "shared", 10, server_id="srv-a", device="/dev/vdb")
    src.volumes["vol-shared"].attachments.append(
        {"server_id": "srv-b", "device": "/dev/vdb", "volume_id": "vol-shared"}
    )
    src.servers["srv-b"].volumes.append({"id": "vol-shared"})
    data = {
        server_id: fake.workload_data(server_id=server_id, name=name, volumes=[
            fake.server_volume_data("root-" + server_id, name + "-root", 10, "/dev/vda", server_id,
                                    bootable=True),
            fake.server_volume_data("vol-shared", "shared", 10, "/dev/vdb", server_id),
        ])
        for server_id, name in names.items()
    }
    return src, data


def test_a_failed_exports_rescue_never_detaches_the_volume_another_export_holds():
    """Review RV8-02: two cold cutovers of one wave share the source conversion host, and their
    VMs a multi-attach volume. Both exports detached it from their VM; A's export attached it, B's
    attach was refused. Cinder's attachment names the server, not the migration: B's rescue, which
    detached every mapped volume the conversion host held, took it from under A's export. It now
    detaches only what B's transfer attached, and fails, saying it cannot give B the volume back
    while A's export holds it (the cold path cannot move a disk another server holds)."""
    src, data = _shared_volume_vms()
    host = SourceHost(src)
    with mock.patch.object(volume_common, "RemoteShell", host):
        a, b = (
            export_module.OpenStackSourceVolume(
                src, "src-conv", "/keys/k", "cloud-user", server_id,
                server_util.Server.from_data(data[server_id]), timeout=3,
            )
            for server_id in ("srv-a", "srv-b")
        )

    with mock.patch.object(volume_common.time, "sleep"):
        for export in (a, b):
            export._test_source_vm_shutdown()
            export._get_root_and_data_volumes()
            export._validate_volumes_match_data()
        for export in (a, b):
            export._detach_data_volumes_from_source()
        a._attach_volumes_to_converter()
        with pytest.raises(Exception, match="already attached"):
            b._attach_volumes_to_converter()
        # B's module fails with its map, and the role's rescue cleans up after B
        with mock.patch.object(volume_common, "RemoteShell", host):
            cleanup = cleanup_module.OpenStackSourceHostCleanup(
                src, "src-conv", "/keys/k", "cloud-user", "srv-b", b.transfer_uuid, b.volume_map,
                timeout=3,
            )
        with pytest.raises(Exception, match="vol-shared is not available"):
            cleanup.close_exports()

    held_by_a = a.volume_map["/dev/vdb"]
    assert held_by_a["source_id"] == "vol-shared" and held_by_a["source_dev"], held_by_a
    assert held_by_a.get("source_attached") is True
    assert not b.volume_map["/dev/vdb"].get("source_attached")
    assert "src-conv" in _holders(src, "vol-shared"), _holders(src, "vol-shared")


def _the_wait_fails(src, what):
    """Cinder or Glance made the resource, then the wait for it failed: the root snapshot or the
    root copy - of the boot volume, or of the image - went to error, the image was still saving
    when the timeout ran out. As in openstacksdk, a create with wait=True runs that wait itself and
    raises out of the create, the resource made; one without it returns the resource still in the
    making."""
    failing = set()
    wait_for_status, wait_for_image = src.block_storage.wait_for_status, src.wait_for_image

    def failing_status_wait(res, status="available", failures=None, interval=2, wait=None, **kwargs):
        if res.id not in failing:
            return wait_for_status(res, status, failures, interval, wait, **kwargs)
        assert wait is not None, "wait_for_status without a wait bound waits forever"
        src.record("wait_for_status", id=res.id, status=status)
        (src.volumes.get(res.id) or src.snapshots[res.id]).status = "error"
        raise fake.SDKException("%s transitioned to failure state error" % res.id)

    def failing_image_wait(image, timeout=3600):
        if image.id not in failing:
            return wait_for_image(image, timeout)
        src.record("wait_for_image", image=image.id, timeout=timeout)
        raise fake.SDKException("Timeout waiting for image to snapshot")

    src.block_storage.wait_for_status, src.wait_for_image = failing_status_wait, failing_image_wait
    if what == "root snapshot":
        create_snapshot = src.create_volume_snapshot

        def create_volume_snapshot(volume_id, force=False, wait=True, timeout=None, **kwargs):
            snapshot = create_snapshot(volume_id, force=force, wait=wait, timeout=timeout, **kwargs)
            snapshot.status = "creating"
            failing.add(snapshot.id)
            if wait:
                src.block_storage.wait_for_status(snapshot, wait=timeout)
            return snapshot

        src.create_volume_snapshot = create_volume_snapshot
    elif what in ("root copy", "image copy"):
        create_volume = src.create_volume
        made_from = "snapshot_id" if what == "root copy" else "image"

        def create_root_copy(size, wait=True, timeout=None, **kwargs):
            volume = create_volume(size, wait=wait, timeout=timeout, **kwargs)
            if kwargs.get(made_from) is not None:
                volume.status = "creating"
                failing.add(volume.id)
            if wait or kwargs.get("bootable") is not None:  # openstacksdk waits for bootable too
                src.block_storage.wait_for_status(volume, wait=timeout)
            return volume

        src.create_volume = create_root_copy
    else:
        create_image = src.compute.create_server_image

        def create_server_image(server, name, metadata=None, wait=False, timeout=120):
            image = create_image(server, name, metadata=metadata, wait=wait, timeout=timeout)
            image.status = "saving"
            failing.add(image.id)
            if wait:
                return src.wait_for_image(image, timeout=timeout)
            return image

        src.compute.create_server_image = create_server_image


@pytest.mark.parametrize("what", ["root snapshot", "root copy", "image", "image copy"])
def test_what_the_export_made_before_its_wait_failed_never_outlives_the_rescue(what):
    """Review RV8-03: openstacksdk's wait=True raises after Cinder or Glance made the resource - on
    an error status, or a slow backend's timeout - and the export recorded each only once that call
    returned: it failed with a map that did not name the resource, so the rescue left it behind,
    and a root copy left so kept its snapshot from being deleted, which failed the rescue itself.
    The export now creates each without that wait, records it, then waits within its timeout
    (Seamless Migrate SDD 6.6)."""
    src, data = _image_booted_vm() if what.startswith("image") else _boot_from_volume_vm()
    volumes = set(src.volumes)
    _the_wait_fails(src, what)
    host = SourceHost(src)

    exported, cleaned = _export_then_rescue(src, host, data)

    assert exported.get("failed") and "volume_map" in exported, exported
    assert cleaned is not None and cleaned["_exit_code"] in (None, 0), cleaned
    assert (set(src.volumes), src.snapshots, src.images) == (volumes, {}, {})
    for volume_id in volumes:
        assert _holders(src, volume_id) == ["srv-1"], volume_id
    assert [call[1]["timeout"] for call in src.calls_to("wait_for_image") if "timeout" in call[1]] == (
        [COMMON["timeout"]] if what == "image" else []
    )


@pytest.mark.parametrize("vm", ["volume-booted", "image-booted"])
def test_a_root_copy_the_cleanup_cannot_delete_never_keeps_a_data_volume_off_the_source_vm(vm):
    """The cleanup after the transfer puts the data volumes back before it removes the root disk's
    copy, its snapshot and the image, and tries every step: a copy Cinder would not delete used to
    stop it before the data volumes - the source VM stayed without them - and before the snapshot
    and the image. It now fails at the end, naming each removal it could not do (Seamless Migrate
    SDD 6.6, review RV8-03)."""
    src, data = _image_booted_vm() if vm == "image-booted" else _boot_from_volume_vm()
    data_volumes = sorted(v for v, volume in src.volumes.items() if not volume.bootable)
    host = SourceHost(src)

    with mock.patch.object(volume_common.time, "sleep"):
        exported = fake.run_module(export_module, dict(COMMON, data=data), src, host)
        assert exported["_exit_code"] in (None, 0), exported
        root = exported["volume_map"]["/dev/vda"]
        src.fail_on["delete_volume"] = lambda call: call["name_or_id"] == root["source_id"]
        cleanup_calls = len(src.calls)
        with pytest.raises(RuntimeError) as failure:
            fake.run_module(cleanup_module, dict(
                COMMON, data=data, transfer_uuid=exported["transfer_uuid"],
                volume_map=exported["volume_map"],
            ), src, host)

    for volume_id in data_volumes:
        assert _holders(src, volume_id) == ["srv-1"], volume_id
    done = [call[0] for call in src.calls[cleanup_calls:]
            if call[0] in ("attach_volume", "delete_volume", "delete_volume_snapshot", "delete_image")]
    assert done == sorted(done, key=lambda name: name != "attach_volume"), done  # data volumes first
    message = str(failure.value)
    assert root["source_id"] in message and root["source_id"] in src.volumes, message
    if root["snap_id"]:  # tried too: Cinder keeps the snapshot of a copy it still has
        assert root["snap_id"] in message and root["snap_id"] in src.snapshots, message
    else:  # tried too: Glance removes the image all the same
        assert root["image_id"] not in src.images and root["image_id"] not in message, message


def _printed(result):
    """A failed module's result as Ansible's default callback prints it on the task's line, below
    -vvv: without its module arguments (invocation) and its traceback (exception)."""
    return json.dumps({key: value for key, value in result.items()
                       if key not in ("invocation", "exception", "_exit_code", "_connected")})


@pytest.mark.parametrize("vm", ["volume-booted", "image-booted"])
def test_a_failed_exports_result_names_no_volume_and_no_vm(vm):
    """Review RV8-05: Ansible prints a failed module's result on the task's line, and Seamless
    Migrate classifies a failure by that output, "timeout" making it transient (its SDD 7.2). The
    export failed with its whole volume map, so a data volume named "db-timeout-archive" - or an
    image-booted VM's name, in its copy's - made a stuck detach, meant to be permanent, be retried.
    The failure's map leaves out the names, which the rescue never reads, and the rescue still puts
    every disk back (Seamless Migrate SDD 6.6)."""
    src, data = _image_booted_vm() if vm == "image-booted" else _boot_from_volume_vm()
    src.servers["srv-1"].name = "app-timeout"
    src.volumes["vol-data1"].name = "db-timeout-archive"
    volumes = set(src.volumes)
    # the detach never ends: Cinder keeps the source VM's attachment
    src.detach_volume = lambda server, volume, wait=True, timeout=None: None
    host = SourceHost(src)

    exported, cleaned = _export_then_rescue(src, host, data)

    assert exported.get("failed"), exported
    assert exported["msg"] == "Timed out after 3 s waiting to detach volume vol-data1 from server srv-1"
    assert not re.search(r"(?i)timeout|app-|db-|vm1-", _printed(exported)), _printed(exported)
    assert all("name" not in mapping for mapping in exported["volume_map"].values())
    assert cleaned is not None and cleaned["_exit_code"] in (None, 0), cleaned
    assert (set(src.volumes), src.snapshots, src.images) == (volumes, {}, {})
    for volume_id in volumes:
        assert _holders(src, volume_id) == ["srv-1"], volume_id
