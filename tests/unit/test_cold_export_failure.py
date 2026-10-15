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
    else:  # tried too: this fake's Glance, a file store, removes it (a Ceph one keeps a cloned image)
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
    # what the classifier reads, and the names: none of it can be part of the random transfer id
    assert not re.search(r"(?i)timeout|vm1-|os-migrate-", _printed(exported)), _printed(exported)
    assert all("name" not in mapping for mapping in exported["volume_map"].values())
    assert cleaned is not None and cleaned["_exit_code"] in (None, 0), cleaned
    assert (set(src.volumes), src.snapshots, src.images) == (volumes, {}, {})
    for volume_id in volumes:
        assert _holders(src, volume_id) == ["srv-1"], volume_id


def _answer_lost(src, server_id, volume_id):
    """Nova carries out the attach of ``volume_id`` to ``server_id``, but its answer never reaches
    openstacksdk - a load balancer's 504 in front of nova-api, a connection reset after the
    request was processed: the call raises with the attachment made."""
    attach = src.attach_volume

    def attach_volume(server, volume, device=None, wait=True, timeout=None):
        attach(server, volume, device=device, wait=wait, timeout=timeout)
        if server.id == server_id and volume.id == volume_id:
            raise fake.SDKException("Remote end closed connection without response")

    src.attach_volume = attach_volume


def test_an_attach_whose_answer_was_lost_is_put_back_by_the_rescue():
    """Review RV9-01: Nova carried out the attach of a data volume to the source conversion host,
    but its answer was lost, so the export failed. The export marked a volume only once the call
    returned: the rescue skipped this one and could not attach it back to the source VM, where it
    was in-use, and the rollback started the source VM without it. The export now marks it before
    the call, once openstacksdk's own checks pass (Seamless Migrate SDD 6.6)."""
    src, data = _boot_from_volume_vm()
    _answer_lost(src, "src-conv", "vol-data1")
    host = SourceHost(src)

    exported, cleaned = _export_then_rescue(src, host, data)

    assert exported.get("failed") and "volume_map" in exported, exported
    assert exported["volume_map"]["/dev/vdb"].get("source_attached") is True
    assert cleaned is not None and cleaned["_exit_code"] in (None, 0), cleaned
    for volume_id in DISKS:
        assert _holders(src, volume_id) == ["srv-1"], volume_id
    assert set(src.volumes) == set(DISKS) and src.snapshots == {}


def _nova_api_refuses(src, pairs):
    """nova-api answers an attach of these (server, volume) pairs with a 503 while ``down`` holds -
    a restarting or overloaded API: the request never reaches Nova, nothing is attached. The SDK's
    own checks (available, not on that server) passed before it sent the request."""
    attach = src.attach_volume
    state = {"down": True}

    def attach_volume(server, volume, device=None, wait=True, timeout=None):
        if state["down"] and (server.id, volume.id) in pairs:
            src.record("attach_volume_503", server=server.id, volume=volume.id)
            raise fake.SDKException("HttpException: 503: Server Error for url: os-volume_attachments")
        return attach(server, volume, device=device, wait=wait, timeout=timeout)

    src.attach_volume = attach_volume
    return state


def test_a_rescue_never_takes_the_shared_volume_another_export_attached_after_its_own_failed():
    """Review RV10-01: two cold cutovers share the source conversion host, and their VMs a
    multi-attach volume. B takes the attach lock first and marks the volume before it asks Nova,
    and nova-api answers 503: nothing was attached. A's export, next to take the lock, attaches
    the volume. B's rescue saw B's mark and the volume on the conversion host, and took it from
    under A's export. B now keeps the mark only if Cinder, settled, lists the conversion host -
    decided while B still holds the lock (Seamless Migrate SDD 6.6)."""
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
    api = _nova_api_refuses(src, {("src-conv", "vol-shared"), ("srv-b", "vol-shared")})

    with mock.patch.object(volume_common.time, "sleep"):
        for export in (a, b):
            export._test_source_vm_shutdown()
            export._get_root_and_data_volumes()
            export._validate_volumes_match_data()
        for export in (a, b):
            export._detach_data_volumes_from_source()
        with pytest.raises(Exception, match="503"):
            b._attach_volumes_to_converter()
        b.reattach_detached_volumes()  # meets the same 503
        b.remove_unheld_root_copy()
        api["down"] = False
        a._attach_volumes_to_converter()
        with mock.patch.object(volume_common, "RemoteShell", host):
            cleanup = cleanup_module.OpenStackSourceHostCleanup(
                src, "src-conv", "/keys/k", "cloud-user", "srv-b", b.transfer_uuid, b.volume_map,
                timeout=3,
            )
        with pytest.raises(Exception, match="vol-shared is not available"):
            cleanup.close_exports()  # B cannot have it back while A's export holds it (RV8-02)

    assert not b.volume_map["/dev/vdb"].get("source_attached"), b.volume_map["/dev/vdb"]
    assert _holders(src, "vol-shared") == ["src-conv"], _holders(src, "vol-shared")
    assert not [c for c in src.calls_to("detach_volume") if c[1]["volume"] == "vol-shared"
                and c[1]["server"] == "src-conv"]


def _answer_lost_while_nova_attaches(src, server_id, volume_id, reads):
    """Nova accepted the attach of ``volume_id`` to ``server_id``, but its answer never reached
    openstacksdk, and Nova carries the attach out a little later: Cinder shows the volume
    `attaching`, and lists the attachment only once it is attached - here after ``reads`` reads
    of the volume."""
    attach, read = src.attach_volume, src.get_volume_by_id
    pending = {}

    def attach_volume(server, volume, device=None, wait=True, timeout=None):
        if server.id == server_id and volume.id == volume_id and "reads" not in pending:
            src.record("attach_volume", server=server.id, volume=volume.id)
            src.volumes[volume.id].status = "attaching"
            pending["reads"] = reads
            raise fake.SDKException("Remote end closed connection without response")
        return attach(server, volume, device=device, wait=wait, timeout=timeout)

    def get_volume_by_id(volume_id_read):
        volume = read(volume_id_read)
        if volume_id_read == volume_id and pending.get("reads"):
            pending["reads"] -= 1
            if not pending["reads"]:  # Nova finished the attach
                volume.status = "available"
                attach(src.servers[server_id], volume)
        return volume

    src.attach_volume, src.get_volume_by_id = attach_volume, get_volume_by_id


@pytest.mark.parametrize("reads", [3, 7], ids=["while the export waits", "while the cleanup waits"])
def test_an_attach_nova_finishes_after_its_answer_was_lost_is_still_put_back_by_the_rescue(reads):
    """Review RV10-02: the answer to the attach of a data volume was lost, and Nova carried the
    attach out a few seconds later. Meanwhile Cinder showed the volume `attaching` and listed no
    attachment, so the rescue skipped the volume, and the rollback started the source VM without
    it. The export waits, within its timeout, for Cinder to settle the volume and keeps the mark of
    an attach still under way; the cleanup waits the same way (Seamless Migrate SDD 6.6)."""
    src, data = _boot_from_volume_vm()
    _answer_lost_while_nova_attaches(src, "src-conv", "vol-data1", reads=reads)
    host = SourceHost(src)

    exported, cleaned = _export_then_rescue(src, host, data)

    assert exported.get("failed") and exported["volume_map"]["/dev/vdb"].get("source_attached")
    assert cleaned is not None and cleaned["_exit_code"] in (None, 0), cleaned
    for volume_id in DISKS:
        assert _holders(src, volume_id) == ["srv-1"], volume_id


def _served(host):
    """What each NBD export on the source conversion host serves: nbdkit's file= or qemu-nbd's
    last argument."""
    served = []
    for command in host.commands_at(CONV):
        if command[:2] == ["sudo", "nbdkit"]:
            served.append(command[-1][len("file="):])
        elif command[:2] == ["sudo", "qemu-nbd"]:
            served.append(command[-1])
    return served


def test_an_export_never_serves_a_disk_whose_path_another_volume_took():
    """Review RV12-03 (the RV11-04 residual's consequence): B's attach of the shared multi-attach
    volume fails with its answer lost while Nova is slow, so B keeps its mark; Nova then fails it,
    A's export attaches the volume, and B's rescue takes it from under A's export. C's export,
    next for the attach lock, attaches its disks, and the kernel gives the freed path to one of
    them. A's export named each disk by its path, so it served C's disk as the shared one with no
    error - the transfer would have copied another VM's disk. It now reads each disk's virtio
    serial before it serves it, and fails (Seamless Migrate SDD 6.6)."""
    src, data = _shared_volume_vms()
    src.add_server("srv-c", "vmc")
    src.servers["srv-c"].status = "SHUTOFF"
    src.add_volume("root-srv-c", "vmc-root", 10, bootable=True, server_id="srv-c", device="/dev/vda")
    src.add_volume("vol-c", "vmc-data", 10, server_id="srv-c", device="/dev/vdb")
    data["srv-c"] = fake.workload_data(server_id="srv-c", name="vmc", volumes=[
        fake.server_volume_data("root-srv-c", "vmc-root", 10, "/dev/vda", "srv-c", bootable=True),
        fake.server_volume_data("vol-c", "vmc-data", 10, "/dev/vdb", "srv-c"),
    ])
    host = SourceHost(src)
    with mock.patch.object(volume_common, "RemoteShell", host):
        a, b, c = (
            export_module.OpenStackSourceVolume(
                src, "src-conv", "/keys/k", "cloud-user", server_id,
                server_util.Server.from_data(data[server_id]), timeout=3,
            )
            for server_id in ("srv-a", "srv-b", "srv-c")
        )
    attach, slow = src.attach_volume, {"pending": True}

    def attach_volume(server, volume, device=None, wait=True, timeout=None):
        if slow["pending"] and server.id == "src-conv" and volume.id == "vol-shared":
            # B's: nova-api accepted it and reserved the volume, the answer never came back
            slow["pending"] = False
            src.record("attach_volume", server=server.id, volume=volume.id)
            src.volumes["vol-shared"].status = "reserved"
            raise fake.SDKException("Remote end closed connection without response")
        return attach(server, volume, device=device, wait=wait, timeout=timeout)

    src.attach_volume = attach_volume
    with mock.patch.object(volume_common.time, "sleep"):
        for export in (a, b, c):
            export._test_source_vm_shutdown()
            export._get_root_and_data_volumes()
            export._validate_volumes_match_data()
        for export in (a, b):
            export._detach_data_volumes_from_source()
        with pytest.raises(fake.SDKException):
            b._attach_volumes_to_converter()
        assert b.volume_map["/dev/vdb"].get("source_attached")  # kept: still under way (RV11-04)
        b.reattach_detached_volumes()
        b.remove_unheld_root_copy()
        src.volumes["vol-shared"].status = "available"  # nova-compute failed it, unreserved it
        a._attach_volumes_to_converter()  # A, next for the attach lock, attaches the shared volume
        shared_path = a.volume_map["/dev/vdb"]["source_dev"]
        with mock.patch.object(volume_common, "RemoteShell", host):
            cleanup = cleanup_module.OpenStackSourceHostCleanup(
                src, "src-conv", "/keys/k", "cloud-user", "srv-b", b.transfer_uuid, b.volume_map,
                timeout=3,
            )
        cleanup.close_exports()  # the residual: it takes the shared volume from A's export
        c._detach_data_volumes_from_source()
        c._attach_volumes_to_converter()  # C, next for the lock, attaches its own disks
        reused_by = {
            attachment["device"]: volume.id
            for volume in src.volumes.values()
            for attachment in volume.attachments
            if attachment["server_id"] == "src-conv"
        }.get(shared_path)
        assert reused_by not in (None, "vol-shared"), reused_by  # the kernel reused the path

        with pytest.raises(RuntimeError, match="not of volume vol-shared"):
            a._export_volumes_from_converter()

    assert shared_path not in _served(host), _served(host)


@pytest.mark.parametrize("tool", ["nbdkit", "qemu-nbd"])
def test_an_export_serves_each_disk_by_its_virtio_serial_link(tool):
    """Review RV12-03: each NBD export serves the disk's udev link of its virtio serial - the
    volume id Nova gives it, cut to 20 characters - which goes with the volume, never the device
    path the kernel gives the next disk attached (Seamless Migrate SDD 6.6)."""
    src, data = _boot_from_volume_vm()
    host = SourceHost(src)
    if tool == "qemu-nbd":
        host.val = lambda command: 1 if command[:2] == ["which", "nbdkit"] else None

    with mock.patch.object(volume_common.time, "sleep"):
        exported = fake.run_module(export_module, dict(COMMON, data=data), src, host)

    assert exported["_exit_code"] in (None, 0), exported
    wanted = sorted(
        "/dev/disk/by-id/virtio-" + mapping["source_id"][:20]
        for mapping in exported["volume_map"].values()
    )
    assert sorted(_served(host)) == wanted


class NoSerialHost(SourceHost):
    """A source conversion host whose disks show no virtio serial - virtio-scsi, say."""

    def __call__(self, address, ssh_user, key_path=None):
        shell = super().__call__(address, ssh_user, key_path)
        out = shell.cmd_out

        def cmd_out(command, **kwargs):
            if command[:1] == ["cat"] and command[1].startswith("/sys/block/"):
                shell.commands.append(list(command))
                return ""
            return out(command, **kwargs)

        shell.cmd_out = cmd_out
        return shell


def test_a_disk_that_shows_no_virtio_serial_is_still_exported_by_its_path():
    """Review RV12-03: a disk that shows no serial cannot be told by its identity, so it is still
    served by its path - the case Seamless Migrate SDD 6.6 names as remaining."""
    src, data = _boot_from_volume_vm()
    host = NoSerialHost(src)

    with mock.patch.object(volume_common.time, "sleep"):
        exported = fake.run_module(export_module, dict(COMMON, data=data), src, host)

    assert exported["_exit_code"] in (None, 0), exported
    assert sorted(_served(host)) == sorted(
        mapping["source_dev"] for mapping in exported["volume_map"].values()
    )


class SerialShell:
    """A conversion host as _identified_path sees it: ``serial`` answers the serial read, or it
    fails with exit status ``error``; ``link`` answers `test -b` on the by-id link."""

    def __init__(self, serial="", error=None, link=0):
        self.serial, self.error, self.link = serial, error, link

    def cmd_out(self, command, **kwargs):
        if self.error is not None:
            raise subprocess.CalledProcessError(self.error, command)
        return self.serial + "\n"

    def cmd_val(self, command, **kwargs):
        return self.link


@pytest.mark.parametrize(
    "shell, device, served",
    [
        (SerialShell("vol-data1"), "/dev/vdb", "/dev/disk/by-id/virtio-vol-data1"),
        (SerialShell("vol-data1", link=1), "/dev/vdb", "/dev/vdb"),  # no udev link: by its path
        (SerialShell(""), "/dev/vdb", "/dev/vdb"),  # an empty serial: by its path
        (SerialShell(error=1), "/dev/sdb", "/dev/sdb"),  # not virtio-blk, no serial file
        (SerialShell(error=1), "/dev/vdb", RuntimeError),  # a vdX without its serial file is gone
        (SerialShell(error=255), "/dev/vdb", subprocess.CalledProcessError),  # ssh failed
        (SerialShell("vol-data1", link=255), "/dev/vdb", subprocess.CalledProcessError),
        (SerialShell("vol-c"), "/dev/vdb", RuntimeError),  # another volume's disk
    ],
    ids=[
        "its link", "no link", "an empty serial", "an sdX disk", "a gone vdX disk",
        "ssh failed at the serial", "ssh failed at the link", "another volume",
    ],
)
def test_the_path_an_export_serves_for_a_disk(shell, device, served):
    """Review RV12-03, RV13-01, RV13-02: the disk's identity decides what the NBD export serves
    (Seamless Migrate SDD 6.6)."""
    export = object.__new__(export_module.OpenStackSourceVolume)
    export.shell, export.log = shell, mock.Mock()

    if isinstance(served, str):
        assert export._identified_path("vol-data1", device) == served
    else:
        with pytest.raises(served):
            export._identified_path("vol-data1", device)


BY_ID = "/dev/disk/by-id/virtio-"


class NbdkitHost(SourceHost):
    """The source conversion host, its NBD servers as their own sources have them. nbdkit's file
    plugin (plugins/file/file.c, the same in 1.24.0, 1.38.0 and master) stores
    ``nbdkit_realpath(file=)`` at its start - realpath(3), which resolves a link and fails on a
    path that does not exist - and opens that stored path for each NBD connection; qemu-nbd opens
    its image once and serves that descriptor. ``connect(port)`` is one new NBD connection: the
    volume it reads, or None. ``before`` holds (predicate, action) pairs, each action run once just
    before the first command its predicate matches (review RV13-01, RV13-02)."""

    def __init__(self, cloud):
        super().__init__(cloud)
        self.cloud = cloud
        self.exports = {}  # port: ("path", what nbdkit stored) or ("fd", the volume qemu-nbd opened)
        self.before = []

    def held(self):
        """{device: volume id} of the disks the conversion host has now."""
        server_id = self.cloud.addresses[CONV]
        return {
            attachment["device"]: volume.id
            for volume in self.cloud.volumes.values()
            for attachment in volume.attachments
            if attachment["server_id"] == server_id
        }

    def realpath(self, path):
        """realpath(3) on the conversion host now: a by-id link resolves to its disk."""
        held = self.held()
        if path.startswith(BY_ID):
            serial = path[len(BY_ID):]
            return next((dev for dev, volume_id in held.items() if volume_id[:20] == serial), None)
        return path if path in self.cloud.devices.get(self.cloud.addresses[CONV], []) else None

    def connect(self, port):
        kind, what = self.exports[port]
        held = self.held()
        if kind == "path":
            return held.get(what)
        return what if what in held.values() else None

    def _run_before(self, command):
        for entry in list(self.before):
            predicate, action = entry
            if predicate(command):
                self.before.remove(entry)
                action()

    def __call__(self, address, ssh_user, key_path=None):
        shell = super().__call__(address, ssh_user, key_path)
        host, out, val = self, shell.cmd_out, shell.cmd_val

        def cmd_out(command, **kwargs):
            host._run_before(command)
            if command[:2] in (["sudo", "nbdkit"], ["sudo", "qemu-nbd"]):
                nbdkit = command[1] == "nbdkit"
                resolved = host.realpath(command[-1][len("file="):] if nbdkit else command[-1])
                if resolved is None:  # realpath's or qemu-nbd's own "No such file or directory"
                    shell.commands.append(list(command))
                    raise subprocess.CalledProcessError(1, command)
                port = int(command[command.index("--port" if nbdkit else "-p") + 1])
                host.exports[port] = ("path", resolved) if nbdkit else ("fd", host.held()[resolved])
            return out(command, **kwargs)

        def cmd_val(command, **kwargs):
            host._run_before(command)
            return val(command, **kwargs)

        shell.cmd_out, shell.cmd_val = cmd_out, cmd_val
        return shell


def _shared_volume_residual():
    """Review RV11-04's residual on an NbdkitHost: B's attach of the shared multi-attach volume
    fails with its answer lost while Nova is slow, so B keeps its mark; Nova then fails it, and A's
    export, next for the attach lock, attaches the volume. Returns the host, A's export, B's rescue
    (its source cleanup, not yet run) and C's export (its disks off its VM, not yet attached). Call
    it with volume_common.time.sleep patched."""
    src, data = _shared_volume_vms()
    src.add_server("srv-c", "vmc")
    src.servers["srv-c"].status = "SHUTOFF"
    src.add_volume("root-srv-c", "vmc-root", 10, bootable=True, server_id="srv-c", device="/dev/vda")
    src.add_volume("vol-c", "vmc-data", 10, server_id="srv-c", device="/dev/vdb")
    data["srv-c"] = fake.workload_data(server_id="srv-c", name="vmc", volumes=[
        fake.server_volume_data("root-srv-c", "vmc-root", 10, "/dev/vda", "srv-c", bootable=True),
        fake.server_volume_data("vol-c", "vmc-data", 10, "/dev/vdb", "srv-c"),
    ])
    host = NbdkitHost(src)
    with mock.patch.object(volume_common, "RemoteShell", host):
        a, b, c = (
            export_module.OpenStackSourceVolume(
                src, "src-conv", "/keys/k", "cloud-user", server_id,
                server_util.Server.from_data(data[server_id]), timeout=3,
            )
            for server_id in ("srv-a", "srv-b", "srv-c")
        )
    attach, slow = src.attach_volume, {"pending": True}

    def attach_volume(server, volume, device=None, wait=True, timeout=None):
        if slow["pending"] and server.id == "src-conv" and volume.id == "vol-shared":
            slow["pending"] = False  # B's: nova-api reserved it, the answer never came back
            src.record("attach_volume", server=server.id, volume=volume.id)
            src.volumes["vol-shared"].status = "reserved"
            raise fake.SDKException("Remote end closed connection without response")
        return attach(server, volume, device=device, wait=wait, timeout=timeout)

    src.attach_volume = attach_volume
    for export in (a, b, c):
        export._test_source_vm_shutdown()
        export._get_root_and_data_volumes()
        export._validate_volumes_match_data()
    for export in (a, b, c):
        export._detach_data_volumes_from_source()
    with pytest.raises(fake.SDKException):
        b._attach_volumes_to_converter()
    b.reattach_detached_volumes()
    b.remove_unheld_root_copy()
    src.volumes["vol-shared"].status = "available"  # nova-compute failed B's, unreserved it
    a._attach_volumes_to_converter()
    with mock.patch.object(volume_common, "RemoteShell", host):
        rescue = cleanup_module.OpenStackSourceHostCleanup(
            src, "src-conv", "/keys/k", "cloud-user", "srv-b", b.transfer_uuid, b.volume_map,
            timeout=3,
        )
    return host, a, rescue, c


def test_an_export_never_serves_the_path_its_gone_disk_left():
    """Review RV13-02: a serial read that failed counted as "no serial" and served the device
    path - but a virtio-blk disk that left the host fails that read the same way. B's rescue took
    the shared volume before A's export reached it, and C's attach landed while A's export looked
    for its NBD tools: A's export served /dev/vdX, now C's disk, and succeeded. A vdX disk always
    shows a serial file, so one without it fails the export (Seamless Migrate SDD 6.6)."""
    with mock.patch.object(volume_common.time, "sleep"):
        host, a, rescue, c = _shared_volume_residual()
        shared_path = a.volume_map["/dev/vdb"]["source_dev"]
        rescue.close_exports()  # B's rescue takes the shared volume first: its disk is gone
        read = ["cat", "/sys/block/%s/serial" % shared_path.rsplit("/", 1)[-1]]
        seen = {"read": False}
        host.before.append((lambda command: command == read, lambda: seen.update(read=True)))
        host.before.append((
            lambda command: seen["read"] and command == ["which", "qemu-nbd"],
            c._attach_volumes_to_converter,  # Nova finishes C's attach meanwhile
        ))
        with pytest.raises(RuntimeError, match="gone: volume vol-shared left the host"):
            a._export_volumes_from_converter()

    assert seen["read"], "A's export never read the serial of the shared volume's disk"
