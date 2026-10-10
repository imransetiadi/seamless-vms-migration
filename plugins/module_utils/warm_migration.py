# Copyright: Seamless Migrate contributors
# Apache License 2.0 (see LICENSE)
"""Warm migration of OpenStack workloads (SDD 6).

A warm migration copies a server's disks while it keeps running: every pass
snapshots the source volumes into temporary volumes attached to the source
conversion host (``OpenstackWarmSnapshot``) and synchronises them into
persistent destination volumes attached to the destination conversion host
with blocksync (``OpenstackWarmSync``). Only the final pass runs after the
source is shut down, so downtime is proportional to the last delta.

Everything a pass needs to resume or clean up is kept in the warm state file
``{state_dir}/{source_server_id}.json`` (``WarmState``).
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import datetime
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import blocksync
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.volume_common import (
    serialized_volume_type,
    ATTACH_LOCK_FILE_DESTINATION,
    ATTACH_LOCK_FILE_SOURCE,
    DEFAULT_TIMEOUT,
    OpenStackVolumeBase,
    RemoteShell,
    destination_volume_sdk_params,
    use_lock,
)

BOOT_DEVICE = "/dev/vda"
PASS_KINDS = ("full", "delta", "final")
REQUESTED_PASS_KINDS = ("auto",) + PASS_KINDS
DEFAULT_PARALLEL_DISKS = 4
DEFAULT_TMP_PREFIX = "os-migrate-"
BLOCKSYNC_REMOTE_PATH = "/tmp/seamless-blocksync-{transfer_uuid}.py"

_SAFE_SERVER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_TRANSFER_UUID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_GIB = 1024 ** 3


def utc_now():
    """ISO-8601 UTC timestamp ending in ``Z`` (millisecond precision)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def _checked_server_id(server_id):
    if not isinstance(server_id, str) or not _SAFE_SERVER_ID.match(server_id):
        raise ValueError("unsafe server id for a warm state file: %r" % (server_id,))
    return server_id


def _checked_transfer_uuid(transfer_uuid):
    if not isinstance(transfer_uuid, str) or not _SAFE_TRANSFER_UUID.match(transfer_uuid):
        raise ValueError("invalid transfer uuid: %r" % (transfer_uuid,))
    return transfer_uuid


class WarmState:
    """The warm state file of one source server (SDD 6.3).

    Written atomically (temporary file + rename) with mode 0600.
    """

    def __init__(
        self,
        state_dir,
        server_id,
        server_name=None,
        transfer_uuid=None,
        dest_volumes=None,
        passes=None,
        pending_snapshot=None,
        destination_server_id=None,
    ):
        self.state_dir = state_dir
        self.server_id = _checked_server_id(server_id)
        self.server_name = server_name
        self.transfer_uuid = transfer_uuid
        self.dest_volumes = dest_volumes if dest_volumes is not None else {}
        self.passes = passes if passes is not None else []
        self.pending_snapshot = pending_snapshot
        self.destination_server_id = destination_server_id

    @staticmethod
    def path_for(state_dir, server_id):
        return os.path.join(state_dir, _checked_server_id(server_id) + ".json")

    @property
    def path(self):
        return self.path_for(self.state_dir, self.server_id)

    @classmethod
    def load(cls, state_dir, server_id):
        """Load the state of ``server_id``; a fresh state if there is none."""
        path = cls.path_for(state_dir, server_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return cls(state_dir, server_id)
        if data.get("server_id") not in (None, server_id):
            raise ValueError(
                "%s belongs to server %s, not %s" % (path, data.get("server_id"), server_id)
            )
        return cls(
            state_dir,
            server_id,
            server_name=data.get("server_name"),
            transfer_uuid=data.get("transfer_uuid"),
            dest_volumes=data.get("dest_volumes") or {},
            passes=data.get("passes") or [],
            pending_snapshot=data.get("pending_snapshot"),
            destination_server_id=data.get("destination_server_id"),
        )

    def exists(self):
        return os.path.exists(self.path)

    def to_dict(self):
        return {
            "server_id": self.server_id,
            "server_name": self.server_name,
            "transfer_uuid": self.transfer_uuid,
            "dest_volumes": self.dest_volumes,
            "passes": self.passes,
            "pending_snapshot": self.pending_snapshot,
            "destination_server_id": self.destination_server_id,
        }

    def save(self):
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=self.state_dir, prefix="." + self.server_id + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def delete(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def next_pass_kind(state, requested):
    """The kind of the next sync pass.

    ``auto`` is ``full`` until destination volumes exist, then ``delta``; a
    ``delta`` without destination volumes is a ``full`` pass; ``final`` (the
    pass after the source is stopped) and ``full`` are kept as requested.
    """
    if requested not in REQUESTED_PASS_KINDS:
        raise ValueError(
            "pass kind must be one of %s, got %r" % (", ".join(REQUESTED_PASS_KINDS), requested)
        )
    has_destination = bool(state.dest_volumes)
    if requested == "auto":
        return "delta" if has_destination else "full"
    if requested == "delta" and not has_destination:
        return "full"
    return requested


def build_warm_block_device_mapping(dest_volumes):
    """block_device_mapping_v2 for the destination server.

    ``/dev/vda`` boots (index 0), every other volume is attached (-1). No
    volume is deleted with the server: they hold the migrated data and are
    reused by a retried cutover or removed explicitly by a rollback.
    """
    mapping = []
    for dev in sorted(dest_volumes):
        mapping.append(
            {
                "boot_index": 0 if dev == BOOT_DEVICE else -1,
                "delete_on_termination": False,
                "destination_type": "volume",
                "device_name": dev.split("/")[-1],
                "source_type": "volume",
                "uuid": dest_volumes[dev]["dest_id"],
            }
        )
    return mapping


def parse_blocksync_progress(line):
    """``{"bytes_done", "bytes_total", "pct"}`` of a receiver progress line."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("event") != "progress":
        return None
    try:
        return {
            "bytes_done": int(event["bytes_done"]),
            "bytes_total": int(event["bytes_total"]),
            "pct": float(event["pct"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def parse_blocksync_summary(output):
    """The JSON summary printed by ``blocksync receive`` as its last line."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            summary = json.loads(line)
        except ValueError:
            return None
        return summary if isinstance(summary, dict) else None
    return None


def blocksync_script_path(transfer_uuid):
    return BLOCKSYNC_REMOTE_PATH.format(transfer_uuid=_checked_transfer_uuid(transfer_uuid))


def blocksync_source():
    """Source of blocksync.py, also when imported from an AnsiballZ zip."""
    loader = getattr(blocksync, "__loader__", None)
    if loader is not None and hasattr(loader, "get_source"):
        try:
            source = loader.get_source(blocksync.__name__)
        except (ImportError, OSError):
            source = None
        if source:
            return source
    with open(blocksync.__file__, "r", encoding="utf-8") as f:
        return f.read()


def blocksync_receive_command(
    transfer_uuid,
    dest_device,
    source_device,
    source_ssh,
    ssh_user,
    chunk_size=blocksync.DEFAULT_CHUNK_SIZE,
    workers=blocksync.DEFAULT_WORKERS,
    assume_zero=False,
    progress_interval=blocksync.DEFAULT_PROGRESS_INTERVAL,
    python="python3",
):
    """Shell words that run ``blocksync receive`` on the destination host.

    The receiver runs as root (it writes a block device) and spawns the
    sender through ``source_ssh`` (the SSH preamble from the destination to
    the source conversion host). That SSH runs as ``ssh_user`` because the
    conversion-host link key belongs to that user. Every word is quoted for
    the destination shell, and the sender's remote words are quoted once
    more for the source shell, so device paths are never interpreted.
    """
    script = blocksync_script_path(transfer_uuid)
    sender_remote = [
        "sudo", "-n", python, script, "send",
        "--device", source_device,
        "--chunk-size", str(int(chunk_size)),
        "--workers", str(int(workers)),
    ]
    sender = ["sudo", "-n", "-u", ssh_user, "--"] + list(source_ssh)
    sender += [shlex.quote(word) for word in sender_remote]
    receiver = [
        "sudo", "-n", python, script, "receive",
        "--device", dest_device,
        "--chunk-size", str(int(chunk_size)),
        "--workers", str(int(workers)),
        "--progress-interval", str(progress_interval),
    ]
    if assume_zero:
        receiver.append("--assume-zero")
    return [shlex.quote(word) for word in receiver + ["--"] + sender]


def _snapshot_entry(source_id, size, bootable, name, volume_type):
    return {
        "source_id": source_id,
        "tmp_volume_id": None,
        "snap_id": None,
        "image_id": None,
        "source_dev": None,
        "size": size,
        "bootable": bool(bootable),
        "name": name,
        "volume_type": volume_type,
    }


def snapshot_complete(pending):
    """True when every temporary volume exists and is attached."""
    return all(
        entry.get("tmp_volume_id") and entry.get("source_dev")
        for entry in (pending.get("volume_map") or {}).values()
    )


class _WarmVolumeBase(OpenStackVolumeBase):
    """Shared helpers of the snapshot and sync sides."""

    def __init__(self, *args, **kwargs):
        super(_WarmVolumeBase, self).__init__(*args, **kwargs)
        self._progress_lock = threading.Lock()

    def _wait_detached(self, volume_id, side):
        for _ in range(self.timeout):
            volume = self.conn.get_volume_by_id(volume_id)
            if volume is None or not self._volume_still_attached(volume, self._converter()):
                return
            time.sleep(1)
        raise RuntimeError(
            "Timed out waiting to detach volume %s from the %s conversion host!"
            % (volume_id, side)
        )

    def _detach_from_converter(self, volume_ids, side):
        converter = self._converter()
        for volume_id in volume_ids:
            volume = self._get_volume_maybe(volume_id)
            if volume is None or not self._volume_still_attached(volume, converter):
                continue
            self.log.info("Detaching volume %s from the %s conversion host", volume_id, side)
            self.conn.detach_volume(
                server=converter, volume=volume, wait=True, timeout=self.timeout
            )
            self._wait_detached(volume_id, side)

    def _forget_transfer(self, shell, transfer_uuid):
        """Best effort: stop blocksync processes and remove the script."""
        script = blocksync_script_path(transfer_uuid)
        # "[s]eamless" matches the processes but not this pkill command line.
        pattern = "[s]eamless-blocksync-" + transfer_uuid
        for command in (
            ["sudo", "pkill", "-f", shlex.quote(pattern)],
            ["sudo", "rm", "-f", script],
        ):
            try:
                shell.cmd_val(command)
            except (OSError, subprocess.SubprocessError) as err:
                self.log.warning("Command %s failed on %s: %s", command, shell.address, err)

    def _wait_volume_available(self, volume):
        return self.conn.block_storage.wait_for_status(
            volume, status="available", failures=["error"], wait=self.timeout
        )


class OpenstackWarmSnapshot(_WarmVolumeBase):
    """Point-in-time copies of a running server's disks (source cloud).

    ``create()`` force-snapshots every volume of the server (or snapshots
    the server to an image for image-booted servers with ``boot_disk_copy``),
    creates temporary volumes from them and attaches those to the source
    conversion host. The source server is never stopped or detached.
    ``cleanup()`` detaches and deletes everything ``create()`` made. Every
    resource id is written to the warm state before waiting on it, so an
    interrupted run can always be cleaned up.
    """

    def __init__(
        self,
        openstack_connection,
        conversion_host_id,
        ssh_key_path,
        ssh_user,
        transfer_uuid,
        ser_server,
        state,
        conversion_host_address=None,
        boot_volume_prefix=None,
        state_file=None,
        log_file=None,
        timeout=DEFAULT_TIMEOUT,
        shell_factory=None,
    ):
        super(OpenstackWarmSnapshot, self).__init__(
            openstack_connection,
            conversion_host_id,
            ssh_key_path,
            ssh_user,
            _checked_transfer_uuid(transfer_uuid),
            conversion_host_address=conversion_host_address,
            state_file=state_file,
            log_file=log_file,
            timeout=timeout,
            shell_factory=shell_factory,
        )
        self.ser_server = ser_server
        self.source_instance_id = ser_server.info()["id"]
        self.state = state
        self.boot_volume_prefix = (
            boot_volume_prefix if boot_volume_prefix is not None else DEFAULT_TMP_PREFIX
        )
        self.volume_map = {}
        self.changed = False

    def create(self):
        """Snapshot the server; returns the volume map (idempotent per uuid)."""
        pending = self.state.pending_snapshot
        if pending is not None:
            if pending.get("transfer_uuid") == self.transfer_uuid and snapshot_complete(pending):
                self.log.info("Reusing the snapshot of transfer %s", self.transfer_uuid)
                self.volume_map = pending["volume_map"]
                return self.volume_map
            self.log.info(
                "Removing the unfinished snapshot of transfer %s", pending.get("transfer_uuid")
            )
            self._cleanup_pending()
        self.changed = True
        self.volume_map = self._discover_volumes()
        self.state.server_name = self.ser_server.params().get("name")
        self.state.transfer_uuid = self.transfer_uuid
        self.state.pending_snapshot = {
            "transfer_uuid": self.transfer_uuid,
            "volume_map": self.volume_map,
        }
        self.state.save()
        for dev in sorted(self.volume_map):
            entry = self.volume_map[dev]
            if entry["source_id"] is None:
                self._copy_server_image(entry)
            else:
                self._copy_volume(entry)
        self._attach_tmp_volumes()
        self.state.save()
        return self.volume_map

    def cleanup(self):
        """Remove the pending snapshot's resources; False if there is none."""
        if self.state.pending_snapshot is None:
            return False
        self.changed = True
        self._cleanup_pending()
        return True

    def _description(self):
        return "Seamless Migrate warm transfer %s of server %s" % (
            self.transfer_uuid,
            self.source_instance_id,
        )

    def _discover_volumes(self):
        sourcevm = self._source_vm()
        if sourcevm is None:
            raise RuntimeError("Source server %s not found!" % self.source_instance_id)
        volume_map = {}
        for server_volume in sourcevm.volumes or []:
            volume = self.conn.get_volume_by_id(server_volume["id"])
            dev = self._get_attachment(volume, sourcevm)["device"]
            volume_map[dev] = _snapshot_entry(
                volume["id"],
                volume["size"],
                volume["bootable"],
                volume["name"],
                volume["volume_type"],
            )
        if BOOT_DEVICE not in volume_map:
            if not sourcevm.image:
                raise RuntimeError("No known boot device found for this instance!")
            if self.ser_server.migration_params().get("boot_disk_copy"):
                volume_map[BOOT_DEVICE] = _snapshot_entry(
                    None, None, True, self.boot_volume_prefix + sourcevm.name, None
                )
            else:
                self.log.info("Image-based instance without boot_disk_copy: data volumes only")
        return volume_map

    def _copy_volume(self, entry):
        name = self.boot_volume_prefix + entry["source_id"]
        snapshot = self.conn.create_volume_snapshot(
            volume_id=entry["source_id"],
            force=True,
            wait=False,
            name=name,
            description=self._description(),
        )
        entry["snap_id"] = snapshot.id
        self.state.save()
        self.conn.block_storage.wait_for_status(
            snapshot, status="available", failures=["error"], wait=self.timeout
        )
        volume = self.conn.create_volume(
            size=entry["size"],
            snapshot_id=snapshot.id,
            name=name,
            description=self._description(),
            wait=False,
        )
        entry["tmp_volume_id"] = volume.id
        self.state.save()
        self._wait_volume_available(volume)

    def _copy_server_image(self, entry):
        self.log.info("Image-based instance with boot_disk_copy: snapshotting the server")
        image = self.conn.compute.create_server_image(
            server=self.source_instance_id, name=entry["name"], wait=False
        )
        entry["image_id"] = image.id
        self.state.save()
        self.conn.wait_for_image(image, timeout=self.timeout)
        image = self.conn.get_image_by_id(image.id)
        if image is None or image.status != "active":
            raise RuntimeError("Could not create new image of image-based instance!")
        size = max((image.size or 0) // _GIB, image.min_disk or 0, 1)
        volume = self.conn.create_volume(
            size=size,
            image=image.id,
            name=entry["name"],
            description=self._description(),
            wait=False,
        )
        entry["size"] = size
        entry["tmp_volume_id"] = volume.id
        self.state.save()
        self._wait_volume_available(volume)

    @use_lock(ATTACH_LOCK_FILE_SOURCE)
    def _attach_tmp_volumes(self):
        def update_source(mapping, dev_path):
            mapping["source_dev"] = dev_path
            self.state.save()
            return mapping

        def volume_id(mapping):
            return mapping["tmp_volume_id"]

        self._attach_volumes(
            self.conn,
            "source",
            (self._converter, self.shell.cmd_out, update_source, volume_id),
        )

    @use_lock(ATTACH_LOCK_FILE_SOURCE)
    def _detach_tmp_volumes(self):
        self._detach_from_converter(
            [entry.get("tmp_volume_id") for _, entry in sorted(self.volume_map.items())],
            "source",
        )

    def _cleanup_pending(self):
        pending = self.state.pending_snapshot
        self.volume_map = pending.get("volume_map") or {}
        stale_uuid = pending.get("transfer_uuid")
        if stale_uuid:
            self._forget_transfer(self.shell, stale_uuid)
        self._detach_tmp_volumes()
        # Volumes first: a snapshot or image with dependent clones (RBD)
        # cannot be deleted.
        for dev in sorted(self.volume_map):
            entry = self.volume_map[dev]
            if entry.get("tmp_volume_id"):
                self.log.info("Deleting temporary volume %s", entry["tmp_volume_id"])
                self.conn.delete_volume(
                    name_or_id=entry["tmp_volume_id"], wait=True, timeout=self.timeout
                )
                entry["tmp_volume_id"] = None
                entry["source_dev"] = None
                self.state.save()
            if entry.get("snap_id"):
                self.log.info("Deleting temporary snapshot %s", entry["snap_id"])
                self.conn.delete_volume_snapshot(
                    name_or_id=entry["snap_id"], wait=True, timeout=self.timeout
                )
                entry["snap_id"] = None
                self.state.save()
            if entry.get("image_id"):
                self.log.info("Deleting temporary image %s", entry["image_id"])
                self.conn.delete_image(
                    name_or_id=entry["image_id"], wait=True, timeout=self.timeout
                )
                entry["image_id"] = None
                self.state.save()
        self.state.pending_snapshot = None
        self.state.save()


class OpenstackWarmSync(_WarmVolumeBase):
    """One blocksync pass from the pending snapshot to the destination.

    Destination volumes are created on the first pass (with the same
    parameters as the cold path) and reused afterwards; they are attached to
    the destination conversion host only for the duration of a pass.
    """

    def __init__(
        self,
        openstack_connection,
        conversion_host_id,
        ssh_key_path,
        ssh_user,
        transfer_uuid,
        ser_server,
        state,
        source_conversion_host_address,
        conversion_host_address=None,
        chunk_size=blocksync.DEFAULT_CHUNK_SIZE,
        workers=blocksync.DEFAULT_WORKERS,
        parallel_disks=DEFAULT_PARALLEL_DISKS,
        assume_zero=False,
        python_interpreter="python3",
        progress_interval=blocksync.DEFAULT_PROGRESS_INTERVAL,
        state_file=None,
        log_file=None,
        timeout=DEFAULT_TIMEOUT,
        shell_factory=None,
        preserve_volume_type=False,
    ):
        super(OpenstackWarmSync, self).__init__(
            openstack_connection,
            conversion_host_id,
            ssh_key_path,
            ssh_user,
            _checked_transfer_uuid(transfer_uuid),
            conversion_host_address=conversion_host_address,
            state_file=state_file,
            log_file=log_file,
            timeout=timeout,
            shell_factory=shell_factory,
            preserve_volume_type=preserve_volume_type,
        )
        self.ser_server = ser_server
        self.state = state
        self.source_conversion_host_address = source_conversion_host_address
        self.chunk_size = int(chunk_size)
        self.workers = int(workers)
        self.parallel_disks = max(1, int(parallel_disks))
        self.assume_zero = assume_zero
        self.python_interpreter = python_interpreter
        self.progress_interval = progress_interval
        self.volume_map = {}
        self._receivers = {}
        self._receivers_lock = threading.Lock()
        self._cancelled = threading.Event()

    # --- public ----------------------------------------------------------

    def sync(self, pass_kind="auto"):
        """Run one pass; returns the SyncPass dict recorded in the state."""
        kind = next_pass_kind(self.state, pass_kind)
        source_map = self._source_volume_map()
        number = len(self.state.passes) + 1
        started_at = utc_now()
        started = time.monotonic()
        created = self._ensure_dest_volumes(source_map)
        orphaned = sorted(set(self.state.dest_volumes) - set(source_map))
        if orphaned:
            self.log.warning(
                "Destination volumes recorded for %s have no source device in this "
                "pass (the source's disks changed); they stay recorded and unused "
                "until a rollback with delete_dest_volumes",
                ", ".join(orphaned),
            )
        # Never-written blocks may only be assumed zero when a later pass still
        # scans the whole volume: the final pass is the last one, so it reads
        # the destination in full and verifies every chunk.
        assume_zero = bool(self.assume_zero and created and kind != "final")
        if self.assume_zero and created and kind == "final":
            self.log.warning(
                "assume_zero ignored for the final pass: the new destination "
                "volumes of %s are read in full",
                ", ".join(sorted(created)),
            )
        self.volume_map = {}
        for dev in sorted(source_map):
            entry = source_map[dev]
            self.volume_map[dev] = {
                "source_id": entry["source_id"],
                "source_dev": entry["source_dev"],
                "dest_id": self.state.dest_volumes[dev]["dest_id"],
                "dest_dev": None,
                "name": self.state.dest_volumes[dev]["name"],
                "size": self.state.dest_volumes[dev]["size"],
                "bootable": self.state.dest_volumes[dev]["bootable"],
                "assume_zero": bool(assume_zero and dev in created),
                "progress": 0.0,
            }
        summaries = []
        error = None
        try:
            # A pass interrupted earlier may have left volumes attached.
            self._detach_volumes_from_destination_converter()
            self._attach_destination_volumes()
            self._install_blocksync()
            summaries = self._run_receivers()
        except BaseException as err:
            error = err
            raise
        finally:
            self._finish_pass(error)
        sync_pass = {
            "number": number,
            "kind": kind,
            "started_at": started_at,
            "ended_at": utc_now(),
            "bytes_scanned": sum(int(s["bytes_scanned"]) for s in summaries),
            "bytes_changed": sum(int(s["bytes_changed"]) for s in summaries),
            "bytes_transferred": sum(int(s["bytes_transferred"]) for s in summaries),
            "duration_s": round(time.monotonic() - started, 3),
        }
        self.state.passes.append(sync_pass)
        self.state.transfer_uuid = self.transfer_uuid
        self.state.save()
        return sync_pass

    def block_device_mapping(self):
        """BDM of the destination volumes of the server's current disks."""
        source_map = (self.state.pending_snapshot or {}).get("volume_map") or self.volume_map
        devices = [dev for dev in source_map if dev in self.state.dest_volumes]
        return build_warm_block_device_mapping(
            {dev: self.state.dest_volumes[dev] for dev in devices}
        )

    # --- volumes -----------------------------------------------------------

    def _source_volume_map(self):
        pending = self.state.pending_snapshot
        if pending is None:
            raise RuntimeError(
                "No pending snapshot for server %s: run import_workload_warm_snapshot "
                "with state=present first." % self.state.server_id
            )
        if pending.get("transfer_uuid") != self.transfer_uuid:
            raise RuntimeError(
                "The pending snapshot belongs to transfer %s, not %s."
                % (pending.get("transfer_uuid"), self.transfer_uuid)
            )
        if not snapshot_complete(pending):
            raise RuntimeError(
                "The pending snapshot of transfer %s is incomplete." % self.transfer_uuid
            )
        return pending["volume_map"]

    def _ensure_dest_volumes(self, source_map):
        created = set()
        for dev in sorted(source_map):
            source = source_map[dev]
            recorded = self.state.dest_volumes.get(dev)
            if recorded is not None:
                volume = self._get_volume_maybe(recorded.get("dest_id"))
                if volume is not None:
                    if source["size"] and source["size"] > recorded["size"]:
                        self._extend_dest_volume(dev, volume, source["size"])
                    continue
                self.log.warning(
                    "Destination volume %s of %s is gone, creating a new one",
                    recorded.get("dest_id"),
                    dev,
                )
            self._create_dest_volume(dev, source)
            created.add(dev)
        return created

    def _create_dest_volume(self, dev, source):
        # Same parameters as the cold path, whose boot volume_map entry
        # points at the temporary copy of the boot volume.
        cold_mapping = {
            "name": source["name"],
            "size": source["size"],
            "bootable": source["bootable"],
            "source_id": source["tmp_volume_id"] if dev == BOOT_DEVICE else source["source_id"],
            # the serialized type (mapped by the user in workloads.yml), never the raw
            # source-cloud type recorded by the snapshot
            "volume_type": serialized_volume_type(self.ser_server, volume_id=source["source_id"])
            if source.get("source_id")
            else None,
        }
        params = destination_volume_sdk_params(
            self.conn,
            self.ser_server,
            dev,
            cold_mapping,
            self.timeout,
            preserve_volume_type=self.preserve_volume_type,
        )
        params.pop("wait", None)
        params.pop("timeout", None)
        bootable = params.pop("bootable", None)
        # Record the id before waiting so an interrupted run never leaks or
        # duplicates a destination volume.
        volume = self.conn.create_volume(wait=False, **params)
        self.state.dest_volumes[dev] = {
            "dest_id": volume.id,
            "size": params["size"],
            "bootable": bool(bootable),
            "name": params.get("name"),
        }
        self.state.save()
        volume = self._wait_volume_available(volume)
        if bootable is not None:
            self.conn.block_storage.set_volume_bootable_status(volume, bootable)

    def _extend_dest_volume(self, dev, volume, size):
        self.log.info("Extending destination volume %s of %s to %s GB", volume.id, dev, size)
        self.conn.block_storage.extend_volume(volume, size)
        self._wait_volume_available(volume)
        self.state.dest_volumes[dev]["size"] = size
        self.state.save()

    @use_lock(ATTACH_LOCK_FILE_DESTINATION)
    def _attach_destination_volumes(self):
        def update_dest(mapping, dev_path):
            mapping["dest_dev"] = dev_path
            return mapping

        def volume_id(mapping):
            return mapping["dest_id"]

        self._attach_volumes(
            self.conn,
            "destination",
            (self._converter, self.shell.cmd_out, update_dest, volume_id),
        )

    # --- blocksync -----------------------------------------------------------

    def _source_shell(self):
        return self.shell_factory(
            self.source_conversion_host_address, self.ssh_user, self.ssh_key_path
        )

    def _install_blocksync(self):
        script = blocksync_script_path(self.transfer_uuid)
        fd, local_path = tempfile.mkstemp(prefix="seamless-blocksync-", suffix=".py")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(blocksync_source())
            for shell in (self.shell, self._source_shell()):
                rc = shell.scp_to(local_path, script)
                if rc != 0:
                    raise RuntimeError(
                        "Could not copy blocksync to %s (scp exit status %s)"
                        % (shell.address, rc)
                    )
        finally:
            os.unlink(local_path)

    def _run_receivers(self):
        devices = sorted(self.volume_map)
        if not devices:
            return []
        parallel = min(self.parallel_disks, len(devices))
        if parallel == 1:
            return [self._sync_device(dev) for dev in devices]
        results = {}
        errors = []
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(self._sync_device, dev): dev for dev in devices}
            for future in as_completed(futures):
                dev = futures[future]
                try:
                    results[dev] = future.result()
                except Exception as err:  # pylint: disable=broad-except
                    if not errors:
                        self._cancel_receivers()
                    errors.append("%s: %s" % (dev, err))
        if errors:
            raise RuntimeError("blocksync failed: " + "; ".join(sorted(errors)))
        return [results[dev] for dev in devices]

    def _sync_device(self, dev):
        if self._cancelled.is_set():
            raise RuntimeError("cancelled after another disk failed")
        mapping = self.volume_map[dev]
        source_ssh = RemoteShell(
            self.source_conversion_host_address, self.ssh_user
        ).ssh_preamble()
        command = blocksync_receive_command(
            self.transfer_uuid,
            mapping["dest_dev"],
            mapping["source_dev"],
            source_ssh,
            self.ssh_user,
            chunk_size=self.chunk_size,
            workers=self.workers,
            assume_zero=mapping["assume_zero"],
            progress_interval=self.progress_interval,
            python=self.python_interpreter,
        )
        self.log.info("Synchronising %s: %s", dev, " ".join(command))
        self._update_progress(dev, 0.0)
        process = self.shell.cmd_sub(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        with self._receivers_lock:
            self._receivers[dev] = process
        stdout = []
        reader = threading.Thread(target=lambda: stdout.extend(process.stdout))
        reader.daemon = True
        reader.start()
        try:
            for line in process.stderr:
                progress = parse_blocksync_progress(line)
                if progress is not None:
                    self._update_progress(dev, progress["pct"])
                elif line.strip():
                    self.log.info("blocksync %s: %s", dev, line.rstrip())
            returncode = process.wait()
            reader.join()
        finally:
            with self._receivers_lock:
                self._receivers.pop(dev, None)
        summary = parse_blocksync_summary("".join(stdout))
        if returncode != 0 or not summary or summary.get("ok") is not True:
            reason = (summary or {}).get("error") or "no summary was printed"
            raise RuntimeError(
                "blocksync of %s failed (exit status %s): %s" % (dev, returncode, reason)
            )
        self.log.info("blocksync %s summary: %s", dev, summary)
        self._update_progress(dev, 100.0)
        return summary

    def _cancel_receivers(self):
        self._cancelled.set()
        with self._receivers_lock:
            processes = list(self._receivers.values())
        for process in processes:
            try:
                process.terminate()
            except OSError:
                pass

    def _update_progress(self, dev_path, progress):
        with self._progress_lock:
            super(OpenstackWarmSync, self)._update_progress(dev_path, progress)

    def _finish_pass(self, error):
        """Stop receivers, remove the script and detach the volumes.

        Failures here are logged; they are raised only if the pass itself
        succeeded, so they never mask the original error.
        """
        steps = (
            self._cancel_receivers,
            lambda: self._forget_transfer(self.shell, self.transfer_uuid),
            lambda: self._forget_transfer(self._source_shell(), self.transfer_uuid),
            self._detach_volumes_from_destination_converter,
        )
        first_failure = None
        for step in steps:
            try:
                step()
            except Exception as err:  # pylint: disable=broad-except
                self.log.error("Cleanup after the sync pass failed: %s", err)
                if first_failure is None:
                    first_failure = err
        if error is None and first_failure is not None:
            raise first_failure


def record_destination_server(state_dir, server_id, destination_server_id, server_name=None):
    """Record the destination server created for ``server_id`` (cutover)."""
    state = WarmState.load(state_dir, server_id)
    state.destination_server_id = destination_server_id
    if server_name and not state.server_name:
        state.server_name = server_name
    state.save()
    return state


class WarmRollback:
    """Destination-side rollback of one workload (SDD 6.5).

    Deletes the destination server recorded in the warm state (or, with
    ``match_by_name``, the only destination server with exactly the
    workload's name, for workloads migrated without a warm state). With
    ``delete_volumes`` it also deletes the destination volumes - the
    recorded ones and every volume attached to that server - and the warm
    state itself; the state is kept while a source snapshot is pending so
    its temporary resources can still be cleaned up.
    """

    def __init__(
        self,
        conn,
        state,
        server_name=None,
        match_by_name=False,
        dst_filters=None,
        timeout=DEFAULT_TIMEOUT,
        conversion_host=None,
    ):
        self.conn = conn
        self.state = state
        self.server_name = server_name or state.server_name
        self.match_by_name = match_by_name
        self.dst_filters = dst_filters or {}
        self.timeout = timeout
        # Name or id of the destination conversion host: the only other
        # server a destination volume is ever detached from.
        self.conversion_host = conversion_host
        self.log = logging.getLogger("osp-osp")

    def run(self, delete_volumes=False):
        result = {
            "changed": False,
            "deleted_server_id": None,
            "deleted_volume_ids": [],
            "kept_volume_ids": [],
            "state_deleted": False,
        }
        recorded = self.state.exists()
        volume_ids = [entry["dest_id"] for _, entry in sorted(self.state.dest_volumes.items())]
        # With a warm state its volumes are the migration's; another volume attached to the
        # destination server (e.g. by an operator after the cutover) is kept (SDD 6.5). Without
        # one (cold) the destination server's attachments are the migration's.
        foreign = []
        destination = self._find_destination_server()
        if destination is not None:
            for attachment in self.conn.compute.volume_attachments(destination):
                if attachment.volume_id in volume_ids:
                    continue
                if recorded:
                    foreign.append(attachment.volume_id)
                else:
                    volume_ids.append(attachment.volume_id)
            self.log.info("Deleting destination server %s", destination.id)
            self.conn.compute.delete_server(destination)
            self.conn.compute.wait_for_delete(destination, wait=self.timeout)
            result["changed"] = True
            result["deleted_server_id"] = destination.id
        if recorded and self.state.destination_server_id is not None:
            self.state.destination_server_id = None
            self.state.save()
        if not delete_volumes:
            return result

        result["kept_volume_ids"].extend(foreign)
        for volume_id in volume_ids:
            deleted = self._delete_volume(volume_id)
            if deleted is None:
                result["kept_volume_ids"].append(volume_id)
                continue
            if deleted:
                result["changed"] = True
                result["deleted_volume_ids"].append(volume_id)
            for dev, entry in list(self.state.dest_volumes.items()):
                if entry["dest_id"] == volume_id:
                    del self.state.dest_volumes[dev]
            if recorded:
                self.state.save()
        if recorded:
            if self.state.pending_snapshot is None:
                self.state.delete()
                result["state_deleted"] = True
            else:
                self.log.warning(
                    "Keeping the warm state of %s: its source snapshot of transfer %s "
                    "still has to be cleaned up",
                    self.state.server_id,
                    self.state.pending_snapshot.get("transfer_uuid"),
                )
                self.state.dest_volumes = {}
                self.state.passes = []
                self.state.save()
            result["changed"] = True
        return result

    def _find_destination_server(self):
        if self.state.destination_server_id:
            return self.conn.compute.find_server(
                self.state.destination_server_id, ignore_missing=True
            )
        if not (self.match_by_name and self.server_name):
            return None
        # Nova filters names by regular expression; compare exactly as well.
        candidates = [
            candidate
            for candidate in self.conn.compute.servers(
                name="^%s$" % re.escape(self.server_name), **self.dst_filters
            )
            if candidate.name == self.server_name
        ]
        if len(candidates) > 1:
            raise RuntimeError(
                "Several destination servers are named %s (%s); refusing to guess"
                % (self.server_name, ", ".join(sorted(c.id for c in candidates)))
            )
        return candidates[0] if candidates else None

    def _is_conversion_host(self, holder):
        return self.conversion_host is not None and self.conversion_host in (
            holder.id,
            getattr(holder, "name", None),
        )

    def _delete_volume(self, volume_id):
        """True when deleted, False when already gone, None when kept because
        another server than the destination conversion host still uses it."""
        volume = self.conn.get_volume_by_id(volume_id)
        if volume is None:
            return False
        for _ in range(self.timeout):
            volume = self.conn.get_volume_by_id(volume_id)
            if volume is None:
                return False
            if not volume.attachments:
                break
            # Attachments left on the destination conversion host after an
            # interrupted pass are removed; those of the deleted destination
            # server go away on their own. Any other holder (a shared volume
            # attached to another server) owns data outside this migration:
            # the volume is kept.
            for attachment in list(volume.attachments):
                holder = self.conn.get_server_by_id(attachment["server_id"])
                if holder is None:
                    continue
                if not self._is_conversion_host(holder):
                    self.log.warning(
                        "Keeping destination volume %s: it is attached to server %s, "
                        "which is not the destination conversion host",
                        volume_id,
                        holder.id,
                    )
                    return None
                self.log.info("Detaching volume %s from %s", volume_id, holder.id)
                self.conn.detach_volume(
                    server=holder, volume=volume, wait=True, timeout=self.timeout
                )
            time.sleep(1)
        else:
            raise RuntimeError("Volume %s is still attached, cannot delete it" % volume_id)
        self.log.info("Deleting destination volume %s", volume_id)
        self.conn.delete_volume(name_or_id=volume_id, wait=True, timeout=self.timeout)
        return True
