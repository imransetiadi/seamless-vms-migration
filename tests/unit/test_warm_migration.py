from __future__ import absolute_import, division, print_function

__metaclass__ = type

import copy
import json
import logging
import os
import shlex
import stat
from unittest import mock

import pytest

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
    blocksync,
    server,
    volume_common,
    warm_migration,
)
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.warm_migration import (
    WarmState,
)
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake

SRC_CONV = "10.0.0.10"
DST_CONV = "10.1.0.10"
STATE_KEYS = {
    "server_id",
    "server_name",
    "transfer_uuid",
    "dest_volumes",
    "passes",
    "pending_snapshot",
    "destination_server_id",
}
PASS_KEYS = {
    "number",
    "kind",
    "started_at",
    "ended_at",
    "bytes_scanned",
    "bytes_changed",
    "bytes_transferred",
    "duration_s",
}
SNAPSHOT_ENTRY_KEYS = {
    "source_id",
    "tmp_volume_id",
    "snap_id",
    "image_id",
    "source_dev",
    "size",
    "bootable",
    "name",
    "volume_type",
}
CREATE_CALLS = ("create_volume_snapshot", "create_volume", "create_server_image", "attach_volume")


class Scenario:
    """A boot-from-volume server with one data volume, two clouds, two hosts."""

    def __init__(self, tmp_path, receive=None, image_booted=False, boot_disk_copy=True):
        self.src = fake.FakeCloud("src")
        self.dst = fake.FakeCloud("dst")
        self.src.add_conversion_host("src-conv", SRC_CONV)
        self.dst.add_conversion_host("dst-conv", DST_CONV)
        volumes = []
        if image_booted:
            self.src.add_server("srv-1", "vm1", image={"id": "img-base"})
        else:
            self.src.add_server("srv-1", "vm1")
            self.src.add_volume(
                "vol-boot", "vm1-boot", 20, bootable=True, volume_type="ssd",
                server_id="srv-1", device="/dev/vda",
            )
            volumes.append(
                fake.server_volume_data(
                    "vol-boot", "vm1-boot", 20, "/dev/vda", "srv-1", bootable=True,
                    volume_type="ssd", description="boot disk",
                )
            )
        self.src.add_volume(
            "vol-data", "vm1-data", 10, server_id="srv-1", device="/dev/vdb"
        )
        volumes.append(
            fake.server_volume_data(
                "vol-data", "vm1-data", 10, "/dev/vdb", "srv-1", description="data disk"
            )
        )
        self.data = fake.workload_data(
            volumes=volumes,
            image_ref={"name": "base"} if image_booted else None,
            boot_disk_copy=boot_disk_copy,
        )
        self.state_dir = str(tmp_path / "workload_warm")
        self.state_file = str(tmp_path / "vm1.state")
        self.shells = fake.ShellFactory([self.src, self.dst], receive=receive)

    def ser_server(self):
        return server.Server.from_data(copy.deepcopy(self.data))

    def state(self):
        return WarmState.load(self.state_dir, "srv-1")

    def snapshot(self, transfer_uuid="uuid-1", **kwargs):
        return warm_migration.OpenstackWarmSnapshot(
            self.src,
            "src-conv",
            "/keys/conversion",
            "cloud-user",
            transfer_uuid,
            self.ser_server(),
            self.state(),
            shell_factory=self.shells,
            **kwargs
        )

    def sync(self, transfer_uuid="uuid-1", **kwargs):
        return warm_migration.OpenstackWarmSync(
            self.dst,
            "dst-conv",
            "/keys/conversion",
            "cloud-user",
            transfer_uuid,
            self.ser_server(),
            self.state(),
            SRC_CONV,
            state_file=self.state_file,
            shell_factory=self.shells,
            **kwargs
        )

    def run_pass(self, transfer_uuid, pass_kind="auto", **kwargs):
        self.snapshot(transfer_uuid).create()
        sync_pass = self.sync(transfer_uuid, **kwargs).sync(pass_kind)
        self.snapshot(transfer_uuid).cleanup()
        return sync_pass


def receive_commands(scenario):
    return [
        shlex.split(" ".join(command))
        for command in scenario.shells.commands_at(DST_CONV)
        if "receive" in " ".join(command)
    ]


def test_a_warm_pass_reaches_each_conversion_host_as_its_own_user(tmp_path):
    """Seamless Migrate SDD 6.5 (audit EX-08): the destination host is reached as its own user
    (ubuntu), which runs the link key; the hop to the source host - the copy of blocksync, the
    sender the receiver starts - logs in as the source host's user (cloud-user)."""
    scenario = Scenario(tmp_path)
    scenario.snapshot().create()
    scenario.shells.shells = []
    warm_migration.OpenstackWarmSync(
        scenario.dst,
        "dst-conv",
        "/keys/conversion",
        "ubuntu",
        "uuid-1",
        scenario.ser_server(),
        scenario.state(),
        SRC_CONV,
        state_file=scenario.state_file,
        shell_factory=scenario.shells,
        src_ssh_user="cloud-user",
    ).sync()
    users = {(shell.address, shell.ssh_user) for shell in scenario.shells.shells}
    assert users == {(DST_CONV, "ubuntu"), (SRC_CONV, "cloud-user")}
    for receive in receive_commands(scenario):
        words = " ".join(receive)
        assert "-u ubuntu --" in words and "cloud-user@" + SRC_CONV in words, words
        assert "ubuntu@" + SRC_CONV not in words, words


# --- WarmState ------------------------------------------------------------


def test_the_warm_state_is_synced_before_and_after_its_rename(tmp_path):
    """SDD 6.3: a rollback reads in the warm state what to delete, so its rename must outlive a
    crash - the temporary file is synced before the rename and the directory after it, as the
    control plane's workload record is (review RV4-07)."""
    state = WarmState.load(str(tmp_path / "workload_warm"), "srv-1")
    events = []
    fsync, replace = os.fsync, os.replace

    def synced(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return fsync(fd)

    def renamed(source, target):
        events.append("rename")
        return replace(source, target)

    with mock.patch.object(warm_migration.os, "fsync", synced), mock.patch.object(
        warm_migration.os, "replace", renamed
    ):
        state.save()

    assert events == ["file", "rename", "directory"]


def test_a_state_directory_that_cannot_be_synced_still_gets_the_state(tmp_path):
    """Some network or FUSE mounts refuse to sync a directory: the state is saved all the same."""
    state_dir = str(tmp_path / "workload_warm")
    state = WarmState.load(state_dir, "srv-1")
    state.transfer_uuid = "uuid-1"
    fsync = os.fsync

    def refusing(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(22, "Invalid argument")
        return fsync(fd)

    with mock.patch.object(warm_migration.os, "fsync", refusing):
        state.save()

    assert WarmState.load(state_dir, "srv-1").transfer_uuid == "uuid-1"


def test_warm_state_roundtrip_atomic(tmp_path):
    state_dir = str(tmp_path / "workload_warm")
    state = WarmState.load(state_dir, "srv-1")
    assert state.to_dict() == {
        "server_id": "srv-1",
        "server_name": None,
        "transfer_uuid": None,
        "dest_volumes": {},
        "passes": [],
        "pending_snapshot": None,
        "destination_server_id": None,
    }

    state.server_name = "vm1"
    state.transfer_uuid = "uuid-1"
    state.dest_volumes["/dev/vda"] = {
        "dest_id": "d-1", "size": 20, "bootable": True, "name": "vm1-boot"
    }
    state.passes.append({"number": 1, "kind": "full"})
    state.save()

    path = os.path.join(state_dir, "srv-1.json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with open(path, encoding="utf-8") as f:
        assert set(json.load(f)) == STATE_KEYS
    assert WarmState.load(state_dir, "srv-1").to_dict() == state.to_dict()

    # Written to a temporary file in the same directory, then renamed.
    with mock.patch.object(warm_migration.os, "replace", wraps=os.replace) as replace:
        state.save()
    tmp_name, target = replace.call_args[0]
    assert target == path
    assert os.path.dirname(tmp_name) == state_dir and tmp_name != path
    assert os.listdir(state_dir) == ["srv-1.json"]

    # A failed write leaves the previous file untouched and no temp file.
    with open(path, encoding="utf-8") as f:
        before = f.read()
    state.passes.append({"not-json": object()})
    with pytest.raises(TypeError):
        state.save()
    with open(path, encoding="utf-8") as f:
        assert f.read() == before
    assert os.listdir(state_dir) == ["srv-1.json"]


@pytest.mark.parametrize("bad_id", ["../srv", "a/b", "", ".hidden"])
def test_warm_state_rejects_unsafe_server_ids(tmp_path, bad_id):
    with pytest.raises(ValueError):
        WarmState.load(str(tmp_path), bad_id)


def test_warm_state_delete(tmp_path):
    state = WarmState.load(str(tmp_path), "srv-1")
    state.save()
    state.delete()
    state.delete()
    assert os.listdir(str(tmp_path)) == []


# --- pure helpers -----------------------------------------------------------


def test_next_pass_kind_auto(tmp_path):
    state = WarmState.load(str(tmp_path), "srv-1")
    assert warm_migration.next_pass_kind(state, "auto") == "full"
    assert warm_migration.next_pass_kind(state, "delta") == "full"
    assert warm_migration.next_pass_kind(state, "final") == "final"

    state.dest_volumes["/dev/vda"] = {"dest_id": "d-1", "size": 1, "bootable": True, "name": "x"}
    assert warm_migration.next_pass_kind(state, "auto") == "delta"
    assert warm_migration.next_pass_kind(state, "delta") == "delta"
    assert warm_migration.next_pass_kind(state, "full") == "full"
    assert warm_migration.next_pass_kind(state, "final") == "final"
    with pytest.raises(ValueError):
        warm_migration.next_pass_kind(state, "bogus")


def test_build_bdm_marks_boot_and_keeps_volumes():
    bdm = warm_migration.build_warm_block_device_mapping(
        {
            "/dev/vdb": {"dest_id": "d-data", "size": 10, "bootable": False, "name": "data"},
            "/dev/vda": {"dest_id": "d-boot", "size": 20, "bootable": True, "name": "boot"},
        }
    )
    assert bdm == [
        {
            "boot_index": 0,
            "delete_on_termination": False,
            "destination_type": "volume",
            "device_name": "vda",
            "source_type": "volume",
            "uuid": "d-boot",
        },
        {
            "boot_index": -1,
            "delete_on_termination": False,
            "destination_type": "volume",
            "device_name": "vdb",
            "source_type": "volume",
            "uuid": "d-data",
        },
    ]
    # Image-booted servers without boot_disk_copy only map data volumes.
    data_only = warm_migration.build_warm_block_device_mapping(
        {"/dev/vdb": {"dest_id": "d-data", "size": 10, "bootable": False, "name": "data"}}
    )
    assert [entry["boot_index"] for entry in data_only] == [-1]


def test_parse_progress_line():
    line = '{"event":"progress","bytes_done":5,"bytes_total":10,"pct":50.0}\n'
    assert warm_migration.parse_blocksync_progress(line) == {
        "bytes_done": 5,
        "bytes_total": 10,
        "pct": 50.0,
    }
    for other in (
        "",
        "blocksync: warning: something",
        "{not json",
        '{"event":"send_progress","bytes_done":1,"bytes_total":2}',
        '{"event":"progress","bytes_done":"x","bytes_total":10,"pct":1}',
        "[1, 2]",
    ):
        assert warm_migration.parse_blocksync_progress(other) is None


def test_parse_summary_takes_last_json_line():
    output = "noise\n" + json.dumps({"ok": True, "chunks": 1}) + "\n\n"
    assert warm_migration.parse_blocksync_summary(output) == {"ok": True, "chunks": 1}
    assert warm_migration.parse_blocksync_summary("") is None
    assert warm_migration.parse_blocksync_summary("garbage\n") is None


def test_receive_command_quotes_paths():
    preamble = [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
        "cloud-user@10.0.0.10",
    ]
    command = warm_migration.blocksync_receive_command(
        "1234-abcd",
        "/dev/disk/by-id/virtio-dst $(reboot)",
        "/dev/vd b;rm -rf /",
        preamble,
        "cloud-user",
        chunk_size=1048576,
        workers=2,
        assume_zero=True,
    )

    # What the destination conversion host's shell executes:
    argv = shlex.split(" ".join(command))
    script = "/tmp/seamless-blocksync-1234-abcd.py"
    assert argv[:5] == ["sudo", "-n", "python3", script, "receive"]
    assert argv[argv.index("--device") + 1] == "/dev/disk/by-id/virtio-dst $(reboot)"
    assert argv[argv.index("--chunk-size") + 1] == "1048576"
    assert argv[argv.index("--workers") + 1] == "2"
    assert "--assume-zero" in argv[:argv.index("--")]
    sender = argv[argv.index("--") + 1:]
    # The sender's ssh runs as the SSH user, whose key reaches the source.
    assert sender[:5] == ["sudo", "-n", "-u", "cloud-user", "--"]
    assert sender[5:5 + len(preamble)] == preamble
    # ... and what the source conversion host's shell executes:
    remote = shlex.split(" ".join(sender[5 + len(preamble):]))
    assert remote == [
        "sudo", "-n", "python3", script, "send",
        "--device", "/dev/vd b;rm -rf /",
        "--chunk-size", "1048576",
        "--workers", "2",
    ]

    with pytest.raises(ValueError):
        warm_migration.blocksync_receive_command(
            "../../etc/x", "/dev/vdb", "/dev/vdc", preamble, "cloud-user"
        )


def test_blocksync_source_is_the_module_source():
    source = warm_migration.blocksync_source()
    with open(blocksync.__file__, encoding="utf-8") as f:
        assert source == f.read()


# --- OpenstackWarmSnapshot ------------------------------------------------


def test_snapshot_create_is_idempotent_per_transfer_uuid(tmp_path):
    scenario = Scenario(tmp_path)

    volume_map = scenario.snapshot("uuid-1").create()

    assert sorted(volume_map) == ["/dev/vda", "/dev/vdb"]
    for dev, entry in volume_map.items():
        assert set(entry) == SNAPSHOT_ENTRY_KEYS
        tmp_volume = scenario.src.volumes[entry["tmp_volume_id"]]
        assert tmp_volume.snapshot_id == entry["snap_id"]
        assert tmp_volume.attachments[0]["server_id"] == "src-conv"
        assert entry["source_dev"] == tmp_volume.attachments[0]["device"]
    assert volume_map["/dev/vda"]["source_id"] == "vol-boot"
    assert volume_map["/dev/vda"]["bootable"] is True
    assert volume_map["/dev/vda"]["volume_type"] == "ssd"
    assert volume_map["/dev/vdb"]["size"] == 10
    # The running server keeps its volumes; snapshots are forced.
    assert all(call[1]["force"] for call in scenario.src.calls_to("create_volume_snapshot"))
    assert not scenario.src.calls_to("detach_volume")

    state = scenario.state()
    assert state.server_name == "vm1"
    assert state.transfer_uuid == "uuid-1"
    assert state.pending_snapshot == {"transfer_uuid": "uuid-1", "volume_map": volume_map}

    before = len(scenario.src.calls)
    again = scenario.snapshot("uuid-1")
    assert again.create() == volume_map
    assert again.changed is False
    assert [c for c in scenario.src.calls[before:] if c[0] in CREATE_CALLS] == []


def test_snapshot_with_new_uuid_replaces_stale_snapshot(tmp_path):
    scenario = Scenario(tmp_path)
    old = scenario.snapshot("uuid-1").create()

    new = scenario.snapshot("uuid-2").create()

    for entry in old.values():
        assert entry["tmp_volume_id"] not in scenario.src.volumes
        assert entry["snap_id"] not in scenario.src.snapshots
    for entry in new.values():
        assert entry["tmp_volume_id"] in scenario.src.volumes
    assert scenario.state().pending_snapshot["transfer_uuid"] == "uuid-2"


def test_snapshot_cleanup_deletes_tmp_volumes_and_snapshots(tmp_path):
    scenario = Scenario(tmp_path)
    volume_map = scenario.snapshot("uuid-1").create()

    cleaner = scenario.snapshot("uuid-1")
    assert cleaner.cleanup() is True

    for entry in volume_map.values():
        assert entry["tmp_volume_id"] not in scenario.src.volumes
        assert entry["snap_id"] not in scenario.src.snapshots
    # Source volumes are untouched and still attached to the source server.
    assert scenario.src.volumes["vol-boot"].attachments[0]["server_id"] == "srv-1"
    assert scenario.src.volumes["vol-data"].attachments[0]["server_id"] == "srv-1"
    assert scenario.src.devices["src-conv"] == ["/dev/vda"]
    assert scenario.state().pending_snapshot is None

    before = len(scenario.src.calls)
    assert scenario.snapshot("uuid-1").cleanup() is False
    assert not [c for c in scenario.src.calls[before:] if c[0].startswith("delete")]


def test_interrupted_snapshot_is_cleaned_up_on_retry(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.src.fail_on["attach_volume"] = lambda kwargs: len(
        scenario.src.calls_to("attach_volume")
    ) == 2
    with pytest.raises(RuntimeError):
        scenario.snapshot("uuid-1").create()

    partial = scenario.state().pending_snapshot
    tmp_ids = [entry["tmp_volume_id"] for entry in partial["volume_map"].values()]
    snap_ids = [entry["snap_id"] for entry in partial["volume_map"].values()]
    assert all(tmp_ids) and all(snap_ids)  # recorded before the failure

    del scenario.src.fail_on["attach_volume"]
    volume_map = scenario.snapshot("uuid-1").create()

    for volume_id in tmp_ids:
        assert volume_id not in scenario.src.volumes
    for snap_id in snap_ids:
        assert snap_id not in scenario.src.snapshots
    assert all(entry["source_dev"] for entry in volume_map.values())


def test_snapshot_image_booted_server_with_boot_disk_copy(tmp_path):
    scenario = Scenario(tmp_path, image_booted=True, boot_disk_copy=True)

    volume_map = scenario.snapshot("uuid-1").create()

    boot = volume_map["/dev/vda"]
    assert boot["source_id"] is None
    assert boot["image_id"] in scenario.src.images
    assert boot["bootable"] is True
    assert boot["size"] == 20  # max(image size in GiB, min_disk)
    assert scenario.src.volumes[boot["tmp_volume_id"]].image_id == boot["image_id"]
    assert volume_map["/dev/vdb"]["source_id"] == "vol-data"

    scenario.snapshot("uuid-1").cleanup()
    assert boot["image_id"] not in scenario.src.images


#: a server snapshot as Glance reports it, and the volume Cinder needs for it (audit A-06)
IMAGE_SIZES = [
    (dict(size=3 * fake.GIB + 5, min_disk=20), 20),  # Nova's min_disk: the instance's root disk
    (dict(size=int(2.5 * fake.GIB), min_disk=0), 3),  # a raw file of 2.5 GiB needs 3, not 2
    (dict(size=fake.GIB, min_disk=0, virtual_size=10 * fake.GIB), 10),  # a qcow2 file holds a larger disk
    (dict(size=None, min_disk=None), 1),
    # Glance before Victoria records no virtual size: a qcow2 file's own header gives it
    # (review RV29-09); a raw file is its disk; a header that cannot be read leaves the file's size
    (dict(size=fake.GIB, min_disk=0, disk_format="qcow2", data=fake.qcow2_header(10 * fake.GIB)), 10),
    (dict(size=int(2.5 * fake.GIB), min_disk=0, disk_format="raw", data=b"\x00" * 512), 3),
    (dict(size=fake.GIB, min_disk=0, disk_format="qcow2", data=b""), 1),
]


@pytest.mark.parametrize(("image", "size"), IMAGE_SIZES)
def test_a_boot_disk_copy_volume_is_as_large_as_cinder_needs_for_the_image(tmp_path, image, size):
    """Seamless Migrate SDD 6.1 (audit A-06): the volume a boot_disk_copy pass creates from the
    server's snapshot holds the image - its file rounded up to whole GiB, its virtual disk and
    its min_disk, at least 1 GiB; Cinder refuses a smaller one on every pass."""
    scenario = Scenario(tmp_path, image_booted=True, boot_disk_copy=True)
    scenario.src.server_image = image

    volume_map = scenario.snapshot("uuid-1").create()

    boot = volume_map["/dev/vda"]
    assert boot["size"] == size
    assert scenario.src.volumes[boot["tmp_volume_id"]].size == size


def test_snapshot_image_booted_server_without_boot_disk_copy(tmp_path):
    scenario = Scenario(tmp_path, image_booted=True, boot_disk_copy=False)

    volume_map = scenario.snapshot("uuid-1").create()

    assert sorted(volume_map) == ["/dev/vdb"]
    assert not scenario.src.calls_to("create_server_image")


# --- OpenstackWarmSync ------------------------------------------------------


def test_sync_creates_dest_volumes_only_on_first_pass(tmp_path):
    scenario = Scenario(tmp_path)

    first = scenario.run_pass("uuid-1")

    assert first["kind"] == "full"
    created = scenario.dst.calls_to("create_volume")
    assert [(c[1]["name"], c[1]["size"]) for c in created] == [
        ("vm1-boot", 20),
        ("vm1-data", 10),
    ]
    dest = scenario.state().dest_volumes
    assert dest["/dev/vda"]["bootable"] is True
    assert dest["/dev/vdb"] == {
        "dest_id": dest["/dev/vdb"]["dest_id"],
        "size": 10,
        "bootable": False,
        "name": "vm1-data",
    }
    assert scenario.dst.volumes[dest["/dev/vda"]["dest_id"]].bootable is True

    second = scenario.run_pass("uuid-2")

    assert second["kind"] == "delta"
    assert len(scenario.dst.calls_to("create_volume")) == 2
    assert scenario.state().dest_volumes == dest
    for entry in dest.values():
        assert scenario.dst.volumes[entry["dest_id"]].attachments == []
    assert scenario.dst.devices["dst-conv"] == ["/dev/vda"]


def test_a_destination_volume_whose_record_cannot_be_written_is_deleted(tmp_path):
    """Seamless Migrate SDD 6.3 (audit A-05): a pass records each destination volume as soon as
    Cinder accepted it; when that record cannot be written (a full disk), no record names the
    volume and it would outlive every rollback - the pass deletes it, once Cinder settled it,
    before it fails, as the cold transfer does."""
    import errno

    scenario = Scenario(tmp_path)
    dst = scenario.dst
    save = WarmState.save
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

    def full_disk(state, *args, **kwargs):
        if state.dest_volumes:  # the record of a destination volume
            raise OSError(errno.ENOSPC, "No space left on device")
        return save(state, *args, **kwargs)

    dst.create_volume, dst.delete_volume = creating, refuses_creating
    dst.block_storage.wait_for_status = settles
    with mock.patch.object(WarmState, "save", full_disk), pytest.raises(OSError, match="No space"):
        scenario.run_pass("uuid-1")

    created = scenario.dst.calls_to("create_volume")
    assert [c[1]["name"] for c in created] == ["vm1-boot"]  # the pass stopped at its record
    assert not [v for v in scenario.dst.volumes.values() if v.name == "vm1-boot"]
    assert scenario.state().dest_volumes == {}


def test_a_volume_extended_without_its_record_is_recorded_not_extended_again(tmp_path):
    """Seamless Migrate SDD 6.3 (review RV29-07): a pass extends a destination volume to the
    source's grown size, then records it. When that record cannot be written (a full disk), Cinder
    holds the volume at the new size while the state file keeps the old one; the next pass reads
    the size Cinder reports and records it - an extend to the size the volume has would be
    refused by Cinder ("New size for extend must be greater than current size") on every pass."""
    import errno

    scenario = Scenario(tmp_path)
    scenario.run_pass("uuid-1")
    data_id = scenario.state().dest_volumes["/dev/vdb"]["dest_id"]
    scenario.src.volumes["vol-data"].size = 15  # grown between passes
    dst = scenario.dst
    extend = dst.block_storage.extend_volume

    def cinder_extend(volume, size):
        current = dst.volumes[volume.id].size
        if int(size) - current <= 0:
            raise RuntimeError(
                "BadRequestException: 400: Invalid input received: New size for extend must be "
                "greater than current size. (current: %s, extended: %s)." % (current, size)
            )
        return extend(volume, size)

    dst.block_storage.extend_volume = cinder_extend
    save = WarmState.save

    def full_disk(state, *args, **kwargs):
        if (state.dest_volumes.get("/dev/vdb") or {}).get("size") == 15:
            raise OSError(errno.ENOSPC, "No space left on device")
        return save(state, *args, **kwargs)

    with mock.patch.object(WarmState, "save", full_disk), pytest.raises(OSError, match="space"):
        scenario.run_pass("uuid-2")
    assert dst.volumes[data_id].size == 15  # extended at Cinder ...
    assert scenario.state().dest_volumes["/dev/vdb"]["size"] == 10  # ... recorded at 10

    scenario.run_pass("uuid-3")  # the disk has room again

    assert scenario.state().dest_volumes["/dev/vdb"]["size"] == 15
    assert len(scenario.state().passes) == 2


def test_sync_destination_volumes_match_cold_path_params(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.data["_migration_params"]["boot_volume_params"]["description"] = "boot copy"

    scenario.run_pass("uuid-1")

    boot, data = scenario.dst.calls_to("create_volume")
    # Boot volume: the cold path takes boot_volume_params (not the source
    # volume's params) because its source_id is the temporary copy.
    assert boot[1]["description"] == "boot copy"
    assert "availability_zone" not in boot[1]
    # Data volume: the exported, user-editable ServerVolume params.
    assert data[1]["description"] == "data disk"
    assert data[1]["availability_zone"] == "nova"
    # volume_type is never passed (os-migrate 1.0.5 behaviour).
    assert "volume_type" not in boot[1] and "volume_type" not in data[1]


def test_sync_preserves_volume_type_when_asked(tmp_path):
    scenario = Scenario(tmp_path)
    # the user (or the control plane's apply_mappings) rewrote the serialized
    # types to destination types; the source cloud still reports ssd / ceph
    scenario.data["params"]["volumes"][0]["params"]["volume_type"] = "rhoso-ssd"
    scenario.data["params"]["volumes"][1]["params"]["volume_type"] = "rhoso-hdd"
    scenario.run_pass("uuid-1", preserve_volume_type=True)

    boot, data = scenario.dst.calls_to("create_volume")
    # Both destination volumes get the serialized (mapped) type, never the raw
    # source-cloud type recorded by the snapshot and never the destination default.
    assert boot[1]["volume_type"] == "rhoso-ssd"
    assert data[1]["volume_type"] == "rhoso-hdd"
    # boot_volume_params.volume_type (also mapped) wins for the boot copy when set
    typed = Scenario(tmp_path / "typed")
    typed.data["_migration_params"]["boot_volume_params"]["volume_type"] = "rhoso-boot"
    typed.run_pass("uuid-1", preserve_volume_type=True)
    calls = typed.dst.calls_to("create_volume")
    assert len(calls) == 2
    assert calls[0][1]["volume_type"] == "rhoso-boot"  # the boot volume


def test_cold_boot_copy_preserves_serialized_volume_type(tmp_path):
    """The cold path's /dev/vda mapping points at the temporary copy: the type comes from
    the serialized volume attached at /dev/vda."""
    from ansible_collections.os_migrate.os_migrate.plugins.module_utils import volume_common

    scenario = Scenario(tmp_path)
    scenario.data["params"]["volumes"][0]["params"]["volume_type"] = "rhoso-ssd"
    boot_mapping = {"name": "os-migrate-vm1", "bootable": True, "size": 20, "source_id": "tmp-1"}
    kept = volume_common.destination_volume_sdk_params(
        None, scenario.ser_server(), "/dev/vda", boot_mapping, 10, preserve_volume_type=True
    )
    assert kept["volume_type"] == "rhoso-ssd"
    default = volume_common.destination_volume_sdk_params(
        None, scenario.ser_server(), "/dev/vda", boot_mapping, 10
    )
    assert "volume_type" not in default


def test_destination_volume_sdk_params_volume_type_switch():
    from ansible_collections.os_migrate.os_migrate.plugins.module_utils import volume_common

    mapping = {"name": "n", "bootable": False, "size": 5, "source_id": "x", "volume_type": "hdd"}
    default = volume_common.destination_volume_sdk_params(None, None, "/dev/vdb", mapping, 10)
    assert "volume_type" not in default
    kept = volume_common.destination_volume_sdk_params(
        None, None, "/dev/vdb", mapping, 10, preserve_volume_type=True
    )
    assert kept["volume_type"] == "hdd"
    untyped = dict(mapping, volume_type=None)
    assert "volume_type" not in volume_common.destination_volume_sdk_params(
        None, None, "/dev/vdb", untyped, 10, preserve_volume_type=True
    )


def test_sync_records_pass_in_state(tmp_path):
    scenario = Scenario(
        tmp_path,
        receive=lambda command: fake.receive_process(scanned=1000, changed=100, transferred=60),
    )

    first = scenario.run_pass("uuid-1")

    assert set(first) == PASS_KEYS
    assert first["number"] == 1
    assert first["bytes_scanned"] == 2000
    assert first["bytes_changed"] == 200
    assert first["bytes_transferred"] == 120
    assert first["started_at"].endswith("Z") and first["ended_at"].endswith("Z")
    assert first["started_at"] <= first["ended_at"]
    assert isinstance(first["duration_s"], float)
    state = scenario.state()
    assert state.passes == [first]
    assert state.transfer_uuid == "uuid-1"
    with open(scenario.state_file, encoding="utf-8") as f:
        assert json.load(f) == {"/dev/vda": 100.0, "/dev/vdb": 100.0}

    final = scenario.run_pass("uuid-2", pass_kind="final")

    assert final["number"] == 2 and final["kind"] == "final"
    assert scenario.state().passes == [first, final]


def test_sync_runs_blocksync_between_attached_devices(tmp_path):
    scenario = Scenario(tmp_path)
    source_map = scenario.snapshot("uuid-1").create()

    sync = scenario.sync("uuid-1", chunk_size=1048576, workers=3)
    sync.sync("auto")

    script = "/tmp/seamless-blocksync-uuid-1.py"
    source_text = warm_migration.blocksync_source()
    for address in (SRC_CONV, DST_CONV):
        copied = [c for shell in scenario.shells.at(address) for c in shell.copied]
        assert copied == [(script, source_text)]
        assert ["sudo", "rm", "-f", script] in scenario.shells.commands_at(address)
    commands = receive_commands(scenario)
    assert len(commands) == 2
    used = {}
    for argv in commands:
        dest_dev = argv[argv.index("--device") + 1]
        sender = argv[argv.index("--") + 1:]
        remote = shlex.split(" ".join(sender[sender.index("cloud-user@" + SRC_CONV) + 1:]))
        used[remote[remote.index("--device") + 1]] = dest_dev
        assert argv[argv.index("--chunk-size") + 1] == "1048576"
        assert remote[remote.index("--workers") + 1] == "3"
    assert set(used) == {entry["source_dev"] for entry in source_map.values()}
    # Destination devices were the ones attached to the destination host.
    assert all(dev.startswith("/dev/vd") and dev != "/dev/vda" for dev in used.values())


def test_sync_has_each_blocksync_end_check_its_volume(tmp_path):
    """SDD 6.2: the kernel gives a freed /dev/vdX path to the next disk attached, so the warm sync
    has blocksync check the device it opens: the sender expects the temporary volume attached at
    its source_dev, the receiver the destination volume attached at its dest_dev."""
    dst_at = {}  # what the destination conversion host holds while blocksync runs

    def receive(command):
        dst_at.update(
            (attachment["device"], volume.id)
            for volume in scenario.dst.volumes.values()
            for attachment in volume.attachments
            if attachment["server_id"] == "dst-conv"
        )
        return fake.receive_process()

    scenario = Scenario(tmp_path, receive=receive)
    source_map = scenario.snapshot("uuid-1").create()

    scenario.sync("uuid-1").sync("auto")

    tmp_at = {entry["source_dev"]: entry["tmp_volume_id"] for entry in source_map.values()}
    commands = receive_commands(scenario)
    assert len(commands) == 2
    for argv in commands:
        receiver = argv[:argv.index("--")]
        sender = argv[argv.index("--") + 1:]
        remote = shlex.split(" ".join(sender[sender.index("cloud-user@" + SRC_CONV) + 1:]))
        dest_dev = receiver[receiver.index("--device") + 1]
        source_dev = remote[remote.index("--device") + 1]
        assert receiver[receiver.index("--expect-serial") + 1] == dst_at[dest_dev]
        assert remote[remote.index("--expect-serial") + 1] == tmp_at[source_dev]


def test_sync_assume_zero_only_for_new_volumes(tmp_path):
    scenario = Scenario(tmp_path)

    scenario.run_pass("uuid-1", assume_zero=True)
    first = receive_commands(scenario)
    scenario.run_pass("uuid-2", assume_zero=True)
    second = receive_commands(scenario)[len(first):]

    assert all("--assume-zero" in argv[:argv.index("--")] for argv in first)
    assert not any("--assume-zero" in argv for argv in second)


def test_sync_extends_the_destination_volume_when_the_source_grew(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.run_pass("uuid-1")
    data_id = scenario.state().dest_volumes["/dev/vdb"]["dest_id"]
    scenario.src.volumes["vol-data"].size = 15  # grown between passes

    scenario.run_pass("uuid-2")

    state = scenario.state()
    assert state.dest_volumes["/dev/vdb"]["dest_id"] == data_id  # reused, not replaced
    assert state.dest_volumes["/dev/vdb"]["size"] == 15
    assert scenario.dst.volumes[data_id].size == 15
    assert ("extend_volume", {"volume": data_id, "size": 15}) in scenario.dst.calls


@pytest.mark.parametrize("online", [False, True], ids=["refused-in-use", "extended-online"])
def test_a_pass_detaches_what_an_interrupted_pass_left_attached_before_it_extends(tmp_path, online):
    """Seamless Migrate SDD 6.4 (audit A-04): a pass interrupted earlier can leave a destination
    volume attached to the destination conversion host. When its source volume grew, the next
    pass extends it - which Cinder refuses for an `in-use` volume unless its microversion and
    backend allow, and after which the pass waits for `available`, which an attached volume never
    reads. The pass detaches what the interrupted one left first, so it neither fails nor wedges
    every later pass until an operator detaches the volume by hand."""
    scenario = Scenario(tmp_path)
    scenario.run_pass("uuid-1")
    dst = scenario.dst
    data_id = scenario.state().dest_volumes["/dev/vdb"]["dest_id"]
    # the pass interrupted here: the volume stayed attached to the conversion host
    dst.attach_volume(dst.servers["dst-conv"], dst.volumes[data_id])
    scenario.src.volumes["vol-data"].size = 15  # grown between passes
    extend = dst.block_storage.extend_volume

    def cinder_extend(volume, size):
        status = dst.volumes[volume.id].status
        if status != "available" and not online:
            raise RuntimeError(
                "BadRequestException: 400: Invalid volume: Volume %s status must be "
                "'{'status': 'available'}' to extend, currently %s." % (volume.id, status)
            )
        return extend(volume, size)

    dst.block_storage.extend_volume = cinder_extend

    scenario.run_pass("uuid-2")

    state = scenario.state()
    assert state.dest_volumes["/dev/vdb"] == {
        "dest_id": data_id, "size": 15, "bootable": False, "name": "vm1-data",
    }
    assert dst.volumes[data_id].size == 15
    assert dst.volumes[data_id].attachments == []
    assert [p["number"] for p in state.passes] == [1, 2]


def test_sync_recreates_a_destination_volume_that_is_gone(tmp_path, caplog):
    scenario = Scenario(tmp_path)
    scenario.run_pass("uuid-1")
    old_id = scenario.state().dest_volumes["/dev/vdb"]["dest_id"]
    del scenario.dst.volumes[old_id]  # deleted behind the migration's back

    with caplog.at_level(logging.WARNING, logger="osp-osp"):
        scenario.run_pass("uuid-2")

    new_id = scenario.state().dest_volumes["/dev/vdb"]["dest_id"]
    assert new_id != old_id and new_id in scenario.dst.volumes
    assert "is gone, creating a new one" in caplog.text


def test_sync_marks_a_reused_boot_volume_bootable_again(tmp_path):
    """A first pass interrupted between the create and the bootable flag left the boot
    volume non-bootable: the next pass sets the flag, or Nova would refuse to boot from it
    after the source was stopped (SDD §6.4)."""
    scenario = Scenario(tmp_path)
    block_storage = scenario.dst.block_storage
    real_wait = block_storage.wait_for_status
    waits = {"n": 0}

    def first_wait_times_out(res, **kwargs):
        waits["n"] += 1
        if waits["n"] == 1:
            raise RuntimeError("Timeout waiting for the volume to become available")
        return real_wait(res, **kwargs)

    block_storage.wait_for_status = first_wait_times_out
    scenario.snapshot("uuid-1").create()
    with pytest.raises(RuntimeError, match="Timeout"):
        scenario.sync("uuid-1").sync("auto")
    scenario.snapshot("uuid-1").cleanup()
    boot_id = scenario.state().dest_volumes["/dev/vda"]["dest_id"]
    assert scenario.dst.volumes[boot_id].bootable is False

    scenario.run_pass("uuid-2")

    assert scenario.dst.volumes[boot_id].bootable is True
    data_id = scenario.state().dest_volumes["/dev/vdb"]["dest_id"]
    assert scenario.dst.volumes[data_id].bootable is False


def test_sync_waits_for_a_reused_volume_still_being_created(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.run_pass("uuid-1")
    data_id = scenario.state().dest_volumes["/dev/vdb"]["dest_id"]
    scenario.dst.volumes[data_id].status = "creating"
    scenario.dst.calls.clear()

    scenario.run_pass("uuid-2")

    waits = [c[1] for c in scenario.dst.calls_to("wait_for_status")]
    assert {"id": data_id, "status": "available"} in waits


def test_sync_replaces_a_reused_volume_in_error(tmp_path, caplog):
    """A volume Cinder failed to create never becomes usable: it is deleted and created
    again, like one that is gone (SDD §6.4)."""
    scenario = Scenario(tmp_path)
    scenario.run_pass("uuid-1")
    old_id = scenario.state().dest_volumes["/dev/vdb"]["dest_id"]
    scenario.dst.volumes[old_id].status = "error"

    with caplog.at_level(logging.WARNING, logger="osp-osp"):
        scenario.run_pass("uuid-2")

    new_id = scenario.state().dest_volumes["/dev/vdb"]["dest_id"]
    assert new_id != old_id and new_id in scenario.dst.volumes
    assert old_id not in scenario.dst.volumes
    assert ("delete_volume", {"name_or_id": old_id}) in scenario.dst.calls
    assert "is in error" in caplog.text


def test_sync_scans_in_full_after_a_failed_first_pass_even_with_assume_zero(tmp_path):
    """The volumes created by a failed pass are partially written: the next
    pass must not assume they read as zeros."""
    scenario = Scenario(
        tmp_path,
        receive=lambda command: fake.receive_process(returncode=2, error="link dropped"),
    )
    scenario.snapshot("uuid-1").create()
    with pytest.raises(RuntimeError, match="link dropped"):
        scenario.sync("uuid-1", assume_zero=True).sync("auto")
    scenario.snapshot("uuid-1").cleanup()
    assert sorted(scenario.state().dest_volumes) == ["/dev/vda", "/dev/vdb"]
    scenario.shells = fake.ShellFactory([scenario.src, scenario.dst])  # the link is back

    scenario.run_pass("uuid-2", assume_zero=True)

    second = receive_commands(scenario)
    assert second and not any("--assume-zero" in argv for argv in second)


def test_sync_final_pass_never_assumes_zero(tmp_path, caplog):
    """A cutover without a preceding pre-copy creates the destination volumes
    in the final pass; that pass must read them in full (no later pass
    would catch a backend that does not return zeros)."""
    scenario = Scenario(tmp_path)

    with caplog.at_level(logging.WARNING, logger="osp-osp"):
        sync_pass = scenario.run_pass("uuid-1", pass_kind="final", assume_zero=True)

    assert sync_pass["kind"] == "final"
    assert not any("--assume-zero" in argv for argv in receive_commands(scenario))
    assert "assume_zero ignored for the final pass" in caplog.text


def test_sync_warns_about_recorded_volumes_without_a_source_device(tmp_path, caplog):
    scenario = Scenario(tmp_path)
    scenario.run_pass("uuid-1")
    state = scenario.state()
    state.dest_volumes["/dev/vdz"] = dict(state.dest_volumes["/dev/vdb"], name="stale")
    state.save()

    with caplog.at_level(logging.WARNING, logger="osp-osp"):
        scenario.run_pass("uuid-2")

    assert "/dev/vdz" in caplog.text and "no source device" in caplog.text
    assert "/dev/vdz" in scenario.state().dest_volumes  # kept for the rollback


def test_sync_failure_detaches_and_raises(tmp_path):
    scenario = Scenario(
        tmp_path,
        receive=lambda command: fake.receive_process(
            returncode=3, error="manifest digest mismatch"
        ),
    )
    scenario.snapshot("uuid-1").create()

    with pytest.raises(RuntimeError, match="manifest digest mismatch"):
        scenario.sync("uuid-1", parallel_disks=1).sync("auto")

    state = scenario.state()
    assert state.passes == []
    assert sorted(state.dest_volumes) == ["/dev/vda", "/dev/vdb"]  # kept for reuse
    for entry in state.dest_volumes.values():
        assert scenario.dst.volumes[entry["dest_id"]].attachments == []
    script = "/tmp/seamless-blocksync-uuid-1.py"
    for address in (SRC_CONV, DST_CONV):
        assert ["sudo", "rm", "-f", script] in scenario.shells.commands_at(address)


def test_parallel_sync_reports_the_failed_disk(tmp_path):
    def receive(command):
        argv = shlex.split(" ".join(command))
        sender = " ".join(argv[argv.index("--") + 1:])
        if "vol-data" in sender or source_devs["/dev/vdb"] in sender:
            return fake.receive_process(returncode=2, error="I/O error: device vanished")
        return fake.receive_process()

    scenario = Scenario(tmp_path, receive=receive)
    source_devs = {
        dev: entry["source_dev"] for dev, entry in scenario.snapshot("uuid-1").create().items()
    }

    with pytest.raises(RuntimeError) as excinfo:
        scenario.sync("uuid-1", parallel_disks=4).sync("auto")

    assert "/dev/vdb" in str(excinfo.value)
    assert "device vanished" in str(excinfo.value)
    assert "/dev/vda:" not in str(excinfo.value)
    assert scenario.state().passes == []


def test_sync_streams_progress_into_the_state_file(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.snapshot("uuid-1").create()
    seen = []
    original = warm_migration.OpenStackVolumeBase._update_progress

    def record(self, dev, pct):
        seen.append((dev, pct))
        return original(self, dev, pct)

    with mock.patch.object(warm_migration.OpenStackVolumeBase, "_update_progress", record):
        scenario.sync("uuid-1", parallel_disks=1).sync("auto")

    for dev in ("/dev/vda", "/dev/vdb"):
        values = [pct for seen_dev, pct in seen if seen_dev == dev]
        assert values[0] == 0.0 and 50.0 in values and values[-1] == 100.0


def test_sync_requires_a_matching_pending_snapshot(tmp_path):
    scenario = Scenario(tmp_path)
    with pytest.raises(RuntimeError, match="snapshot"):
        scenario.sync("uuid-1").sync("auto")

    scenario.snapshot("uuid-1").create()
    with pytest.raises(RuntimeError, match="uuid-1"):
        scenario.sync("uuid-9").sync("auto")


def test_sync_returns_block_device_mapping_for_current_volumes(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.snapshot("uuid-1").create()
    sync = scenario.sync("uuid-1")
    sync.sync("auto")

    bdm = sync.block_device_mapping()

    dest = scenario.state().dest_volumes
    assert [(e["device_name"], e["boot_index"], e["uuid"]) for e in bdm] == [
        ("vda", 0, dest["/dev/vda"]["dest_id"]),
        ("vdb", -1, dest["/dev/vdb"]["dest_id"]),
    ]
    assert all(e["delete_on_termination"] is False for e in bdm)


# --- Ansible modules ----------------------------------------------------------


def _module_args(scenario, cloud, conversion_host, address, **extra):
    args = {
        "cloud": cloud,
        "data": copy.deepcopy(scenario.data),
        "conversion_host": {"id": conversion_host, "address": address},
        "ssh_key_path": "/keys/conversion",
        "ssh_user": "cloud-user",
        "state_dir": scenario.state_dir,
    }
    args.update(extra)
    return args


def _run_module(module, args, conn, shells, capsys):
    return fake.run_module(module, args, conn, shells)


def test_snapshot_module_present_and_absent(tmp_path, capsys):
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_warm_snapshot as snapshot_module,
    )

    scenario = Scenario(tmp_path)
    args = _module_args(scenario, "src", "src-conv", SRC_CONV, transfer_uuid="uuid-7")

    created = _run_module(snapshot_module, args, scenario.src, scenario.shells, capsys)
    assert created["_exit_code"] == 0, created
    assert created["changed"] is True
    assert created["transfer_uuid"] == "uuid-7"
    assert sorted(created["volume_map"]) == ["/dev/vda", "/dev/vdb"]

    again = _run_module(snapshot_module, args, scenario.src, scenario.shells, capsys)
    assert again["changed"] is False
    assert again["volume_map"] == created["volume_map"]

    absent = dict(args, state="absent")
    absent.pop("transfer_uuid")
    removed = _run_module(snapshot_module, absent, scenario.src, scenario.shells, capsys)
    assert removed["changed"] is True and removed["transfer_uuid"] == "uuid-7"
    assert scenario.state().pending_snapshot is None

    noop = _run_module(snapshot_module, absent, scenario.src, scenario.shells, capsys)
    assert noop["changed"] is False
    assert noop["_connected"] is False  # nothing to clean: no cloud or SSH access


def test_sync_module_returns_pass_volume_map_and_bdm(tmp_path, capsys):
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_warm_sync as sync_module,
    )

    scenario = Scenario(tmp_path)
    scenario.snapshot("uuid-1").create()
    args = _module_args(
        scenario, "dst", "dst-conv", DST_CONV,
        src_conversion_host_address=SRC_CONV, chunk_size=1048576, workers=2,
    )

    result = _run_module(sync_module, args, scenario.dst, scenario.shells, capsys)

    assert result["_exit_code"] == 0, result
    assert result["changed"] is True
    assert result["transfer_uuid"] == "uuid-1"
    assert result["sync_pass"]["number"] == 1 and result["sync_pass"]["kind"] == "full"
    assert sorted(result["volume_map"]) == ["/dev/vda", "/dev/vdb"]
    assert [entry["boot_index"] for entry in result["block_device_mapping"]] == [0, -1]
    assert scenario.state().passes == [result["sync_pass"]]


def test_sync_module_fails_without_snapshot_or_with_bad_chunk_size(tmp_path, capsys):
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_warm_sync as sync_module,
    )

    scenario = Scenario(tmp_path)
    args = _module_args(
        scenario, "dst", "dst-conv", DST_CONV, src_conversion_host_address=SRC_CONV
    )

    missing = _run_module(sync_module, args, scenario.dst, scenario.shells, capsys)
    assert missing["failed"] is True and "No pending snapshot" in missing["msg"]

    bad = _run_module(
        sync_module, dict(args, chunk_size=1000), scenario.dst, scenario.shells, capsys
    )
    assert bad["failed"] is True and "chunk_size" in bad["msg"]


FAKE_SSH = """#!/bin/sh
# Like OpenSSH: drop the options and the destination, then let the
# "remote" shell run the remaining words joined by spaces.
while [ $# -gt 0 ]; do
  case "$1" in
    -o|-i|-p|-l) shift 2 ;;
    -*) shift ;;
    *) shift; break ;;
  esac
done
exec /bin/sh -c "$*"
"""

FAKE_SUDO = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -u) shift 2 ;;
    --) shift; break ;;
    -*) shift ;;
    *) break ;;
  esac
done
exec "$@"
"""


@pytest.mark.skipif(not os.path.exists("/bin/sh"), reason="needs a POSIX shell")
def test_receive_command_runs_end_to_end_through_two_shells(tmp_path, monkeypatch):
    import subprocess
    import sys

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("ssh", FAKE_SSH), ("sudo", FAKE_SUDO)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(
        warm_migration,
        "BLOCKSYNC_REMOTE_PATH",
        str(tmp_path / "seamless-blocksync-{transfer_uuid}.py"),
    )
    script = warm_migration.blocksync_script_path("uuid-e2e")
    with open(script, "w", encoding="utf-8") as f:
        f.write(warm_migration.blocksync_source())

    chunk = 65536
    data = os.urandom(5 * chunk + 17)
    src = tmp_path / "src dev $HOME;echo pwned 'q'"
    dst = tmp_path / "dst dev `id`&&x"
    src.write_bytes(data)
    dst.write_bytes(bytes(len(data)))

    command = warm_migration.blocksync_receive_command(
        "uuid-e2e",
        str(dst),
        str(src),
        ["ssh", "-o", "BatchMode=yes", "cloud-user@10.0.0.10"],
        "cloud-user",
        chunk_size=chunk,
        workers=2,
        progress_interval=0,
        python=sys.executable,
    )
    # The migrator runs it like RemoteShell.cmd_sub: ssh <preamble> <words>.
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "cloud-user@10.1.0.10"] + command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    summary = warm_migration.parse_blocksync_summary(proc.stdout)
    assert summary["ok"] is True and summary["chunks"] == 6
    assert dst.read_bytes() == data
    assert "pwned" not in proc.stdout + proc.stderr
    progress = [warm_migration.parse_blocksync_progress(line) for line in proc.stderr.splitlines()]
    assert [p for p in progress if p][-1]["pct"] == 100.0


def test_a_pinned_warm_pass_pins_each_conversion_host_and_keeps_the_links_options(tmp_path):
    """Seamless Migrate SDD 6.6 (plan task ST3): given a known_hosts file, the warm snapshot pins
    the source host under its alias by its server, the warm sync the destination host, and the
    sync's copy of blocksync the source host by its address alone - no server id is known there.
    The link the receiver opens from the destination host keeps today's options until ST6."""
    known_hosts = str(tmp_path / "known_hosts")
    scenario = Scenario(tmp_path)
    scenario.snapshot(ssh_known_hosts=known_hosts).create()
    scenario.sync(ssh_known_hosts=known_hosts).sync()

    pins = [
        (shell.address, shell.pin.get("side"), shell.pin.get("server"))
        for shell in scenario.shells.shells
    ]
    assert set(pins) == {
        (SRC_CONV, "source", None),
        (SRC_CONV, "source", "src-conv"),
        (DST_CONV, "destination", "dst-conv"),
    }
    assert {shell.pin.get("known_hosts") for shell in scenario.shells.shells} == {known_hosts}
    receives = receive_commands(scenario)
    assert receives
    for receive in receives:
        sender = receive[receive.index("--") + 1:]
        assert "StrictHostKeyChecking=no" in sender, sender
        assert not any("HostKeyAlias" in word or "UserKnownHostsFile" in word for word in sender)


def test_a_link_the_source_host_refuses_fails_the_warm_sync_at_once(tmp_path):
    """Seamless Migrate SDD 6.6, 26.5 (plan task ST3): with a known_hosts file, the warm sync reads
    its receiver's output once the receiver exits: the sender's "Permission denied" for the source
    host is a link the source host refused - LinkRefused, by the link's own message, for every
    disk at once. A "Permission denied" for another host is no refusal of the link, and without
    the file the sync fails as before."""
    said = []

    def receive(command):
        return fake.FakeProcess(3, "", "".join(said) + "blocksync: error: the sender ended\n")

    scenario = Scenario(tmp_path, receive=receive)
    scenario.snapshot().create()
    known_hosts = str(tmp_path / "known_hosts")

    said[:] = ["cloud-user@%s: Permission denied (publickey).\n" % SRC_CONV]
    with pytest.raises(volume_common.LinkRefused) as refused:
        scenario.sync(ssh_known_hosts=known_hosts).sync()
    assert str(refused.value) == (
        "link refused by the source conversion host (os_migrate_conv_src at %s): "
        "Permission denied (publickey)" % SRC_CONV
    )
    with pytest.raises(RuntimeError, match="blocksync failed") as unpinned:
        scenario.sync().sync()
    assert not isinstance(unpinned.value, volume_common.SshRefused)

    said[:] = ["cloud-user@%s: Permission denied (publickey).\n" % DST_CONV]
    with pytest.raises(RuntimeError, match="blocksync failed") as other_host:
        scenario.sync(ssh_known_hosts=known_hosts).sync()
    assert not isinstance(other_host.value, volume_common.SshRefused)
