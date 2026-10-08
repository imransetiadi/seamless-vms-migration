from __future__ import absolute_import, division, print_function

__metaclass__ = type

import copy
import json
import os
import shlex
import stat
from unittest import mock

import pytest

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
    blocksync,
    server,
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

    def snapshot(self, transfer_uuid="uuid-1"):
        return warm_migration.OpenstackWarmSnapshot(
            self.src,
            "src-conv",
            "/keys/conversion",
            "cloud-user",
            transfer_uuid,
            self.ser_server(),
            self.state(),
            shell_factory=self.shells,
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


# --- WarmState ------------------------------------------------------------


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


def test_sync_assume_zero_only_for_new_volumes(tmp_path):
    scenario = Scenario(tmp_path)

    scenario.run_pass("uuid-1", assume_zero=True)
    first = receive_commands(scenario)
    scenario.run_pass("uuid-2", assume_zero=True)
    second = receive_commands(scenario)[len(first):]

    assert all("--assume-zero" in argv[:argv.index("--")] for argv in first)
    assert not any("--assume-zero" in argv for argv in second)


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
