# Modified by the Seamless Migrate project, 2026.
# pylint: disable=consider-using-with
from __future__ import absolute_import, division, print_function

__metaclass__ = type
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.server_volume import (
    ServerVolume,
)
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import exc
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import time
import fcntl
import errno
import functools

# Default timeout for OpenStack operations
DEFAULT_TIMEOUT = 1800

# Lock to serialize volume attachments. This helps prevent device path
# mismatches between the OpenStack SDK and /dev in the VM.
ATTACH_LOCK_FILE_SOURCE = "/var/lock/v2v-source-volume-lock"
ATTACH_LOCK_FILE_DESTINATION = "/var/lock/v2v-destination-volume-lock"

# File containing ports used by all the nbdkit processes running on the source
# conversion host. There is a check to see if the port is available, but this
# should speed things up.
PORT_MAP_FILE = "/var/run/v2v-migration-ports"
PORT_LOCK_FILE = "/var/lock/v2v-migration-lock"  # Lock for the port map

try:
    # will be fixed in another PR
    # pylint: disable-next=unused-import
    from subprocess import DEVNULL
except ImportError:
    DEVNULL = open(os.devnull, "r+", encoding="utf8")


def _is_not_found(err):
    """openstacksdk's NotFoundException (a 404), whatever its version names it."""
    return getattr(err, "status_code", None) == 404 or any(
        cls.__name__ == "NotFoundException" for cls in type(err).__mro__
    )


def volume_or_none(conn, volume_id):
    """
    The volume ``volume_id``, or None when Cinder no longer has it:
    openstacksdk 4.x's get_volume_by_id raises NotFoundException for a
    missing volume, its docstring's "else None" notwithstanding (Seamless
    Migrate SDD 6.4, review RV4-06).
    """
    try:
        return conn.get_volume_by_id(volume_id)
    except Exception as err:  # pylint: disable=broad-except
        if _is_not_found(err):
            return None
        raise


def detach_volume_bounded(conn, server, volume, timeout):
    """
    Detach ``volume`` from ``server`` and wait, within ``timeout`` seconds,
    until the volume no longer lists that server's attachment and - unless
    another server still holds it, as it does a shared multi-attach disk, which
    stays `in-use` - until Cinder shows it available. openstacksdk 4.x's
    detach_volume(wait=True) ignores its timeout and waits for `available`,
    which a shared disk never reaches (Seamless Migrate SDD 6.6, review 5,
    RV6-01).
    """
    conn.detach_volume(server=server, volume=volume, wait=False)
    for second in range(timeout):
        current = volume_or_none(conn, volume["id"])
        if current is None:
            return
        holders = set(a["server_id"] for a in getattr(current, "attachments", None) or [])
        status = getattr(current, "status", None)
        if status == "error":
            raise RuntimeError(
                "Volume %s went to error while detaching from server %s"
                % (volume["id"], server["id"])
            )
        if server["id"] not in holders and (holders or status == "available"):
            return
        time.sleep(1)
    raise RuntimeError(
        "Timed out after %s s waiting to detach volume %s from server %s"
        % (timeout, volume["id"], server["id"])
    )


def _lock_host():
    """This host's name as a lock records it (SDD §6.6)."""
    host = socket.gethostname()
    return host if re.match(r"^[A-Za-z0-9._-]+$", host or "") else "unknown"


# Cinder's statuses while Nova attaches a volume: it lists the attachment only once attached
ATTACHING = ("reserved", "attaching")
# how long a settle wait for them holds the attach lock: ample for Nova to finish an attach, and
# far below what a lock wait queued behind it allows (Seamless Migrate SDD 6.6, review RV11-01)
ATTACH_SETTLE_S = 120
# how long an attach Nova accepted may stay under way while the wait for its device path holds the
# attach lock: ample for a slow Nova, and it bounds a reservation nova-compute abandoned (Seamless
# Migrate SDD 6.6, review RV12-01)
ATTACH_UNDER_WAY_S = 600

# ssh's own exit status when it cannot reach or log in to the host
SSH_FAILED = 255
# a lock wait gives up once ssh to the conversion host failed this long without a break: long
# enough to ride out a reboot or a network blip (Seamless Migrate SDD 6.6, review RV8-01)
SSH_UNREACHABLE_S = 900
# the prefixes of a give-up, each the "timeout" the control plane retries as transient: an
# SshOutage takes no other, and the control plane's tests read this tuple (Seamless Migrate SDD
# 6.6, review RV11-05)
GIVE_UP_PREFIXES = ("Lock timeout", "SSH timeout")


class ConversionHostUnreachable(RuntimeError):
    """A wait gave up: ssh to the conversion host failed for SSH_UNREACHABLE_S
    without a break (Seamless Migrate SDD 6.6)."""


class DiskIdentityError(RuntimeError):
    """A disk failed its identity check: it is another volume's, or the volume
    left the conversion host. Permanent: a module reports it by its own message,
    which the control plane classifies, never by its cause (Seamless Migrate
    SDD 6.6, review RV15-01)."""


def _checked_give_up_prefix(prefix):
    """``prefix``, one of GIVE_UP_PREFIXES, which the control plane retries."""
    if prefix not in GIVE_UP_PREFIXES:
        raise ValueError(
            "A give-up prefix is one of %s, not %r" % (", ".join(GIVE_UP_PREFIXES), prefix)
        )
    return prefix


class SshOutage:
    """
    How long ssh to a conversion host has failed without a break, its own exit
    status 255: a wait that meets it gives up once that reaches
    SSH_UNREACHABLE_S, with "{prefix}: conversion host {address} unreachable
    over SSH for {n} s, waiting for {what}", which the control plane retries
    (Seamless Migrate SDD 6.6, review RV8-01, RV9-02, RV9-04); the prefix is
    one of GIVE_UP_PREFIXES, checked whenever it is set and again when the
    give-up is built (review RV11-05, RV12-07, RV15-03).
    """

    #: the give-up: its prefix, the host, the seconds ssh failed and what the wait was for
    GIVE_UP = "%s: conversion host %s unreachable over SSH for %d s, waiting for %s"

    def __init__(self, address, waiting_for, prefix="SSH timeout"):
        self.prefix = prefix
        self.address = address
        self.waiting_for = waiting_for
        self.since = None

    @property
    def prefix(self):
        """The give-up's prefix: one of GIVE_UP_PREFIXES."""
        return self._prefix

    @prefix.setter
    def prefix(self, prefix):
        self._prefix = _checked_give_up_prefix(prefix)

    @property
    def failing(self):
        """True while ssh has failed since the last try that reached the host."""
        return self.since is not None

    def seen(self, unreachable):
        """One try: raise once ssh has failed for the window without a break."""
        if not unreachable:
            self.since = None
            return
        now = time.monotonic()
        if self.since is None:
            self.since = now
        elif now - self.since >= SSH_UNREACHABLE_S:
            # checked again as the template is filled: a prefix written past the setter -
            # _prefix itself, a subclass's own property - never reaches the give-up (review RV15-03)
            prefix = _checked_give_up_prefix(self.prefix)
            raise ConversionHostUnreachable(
                self.GIVE_UP % (prefix, self.address, now - self.since, self.waiting_for)
            )


def use_lock(lock_file):
    """Boilerplate for functions that need to take a lock."""

    def _decorate_lock(function):
        def wait_for_lock(self, *args, **kwargs):
            if self.check_and_cleanup_lockfiles():
                self.log.info("Cleaned up unused lockfiles before start")
            outage = SshOutage(
                getattr(self.shell, "address", "?"), lock_file, prefix="Lock timeout"
            )
            for second in range(DEFAULT_TIMEOUT):
                try:
                    self.log.info("Waiting for lock %s...", lock_file)
                    lock = lock_file + ".lock"
                    # the owner, so a later module can tell a dead one (SDD §6.6)
                    owner = "%s:%d" % (_lock_host(), os.getpid())
                    cmd = [
                        "sudo",
                        "flock",
                        "--timeout",
                        "1",
                        "--conflict-exit-code",
                        "16",
                        lock_file,
                        "-c",
                        '"( test ! -e '
                        + lock
                        + " || exit 17 ) "
                        + "&& echo "
                        + owner
                        + " | sudo "
                        + " tee "
                        + lock
                        + '"',
                    ]
                    result = self.shell.cmd_val(cmd)
                    # the conversion host cannot be reached: give up after 15 minutes
                    # of that rather than try 1800 times; the control plane retries
                    # a "timeout" (Seamless Migrate SDD 6.6, QASuite R-07)
                    outage.seen(result == SSH_FAILED)
                    if result == 16:
                        self.log.info("Another conversion has the lock.")
                    elif result == 17:
                        self.log.info("Another conversion is holding the lock.")
                    elif result == 0:
                        break
                    time.sleep(1)
                except subprocess.CalledProcessError as err:
                    time.sleep(1)
                    self.log.info("Error waiting for lock: %s", str(err))
            else:
                raise RuntimeError("Unable to acquire lock " + lock_file)
            try:
                return function(self, *args, **kwargs)
            finally:
                try:
                    lock = lock_file + ".lock"
                    result = self.shell.cmd_out(["sudo", "rm", "-vf", lock])
                    self.log.debug("Released lock: %s", result)
                except subprocess.CalledProcessError as err:
                    self.log.error("Error releasing lock: %s", str(err))

        return wait_for_lock

    return _decorate_lock


def destination_volume_sdk_params(
    conn, ser_server, path, mapping, timeout=DEFAULT_TIMEOUT, preserve_volume_type=False
):
    """Creation parameters for the destination copy of one source volume.

    Shared by the cold path (_create_destination_volumes) and the warm path
    so both create identical destination volumes. A mapping whose
    ``source_id`` is one of the workload's exported volumes takes that
    volume's user-editable params; otherwise the boot device takes the
    non-null ``boot_volume_params`` migration params.

    By default ``volume_type`` is never passed and the destination's default
    volume type is used (os-migrate 1.0.5 behaviour). With
    ``preserve_volume_type`` the serialized ``volume_type`` (or the mapping's
    own ``volume_type``) is kept, so a type rewritten to a destination type
    by the caller is honoured.
    """
    sdk_params = {
        "name": mapping["name"],
        "bootable": mapping["bootable"],
        "size": mapping["size"],
        "wait": True,
        "timeout": timeout,
    }
    if ser_server is not None:
        volumes = map(ServerVolume.from_data, ser_server.params()["volumes"])
        src_id_volumes = {vol.info()["id"]: vol for vol in volumes}
        source_id = mapping.get("source_id")
        if source_id in src_id_volumes:
            sdk_params.update(src_id_volumes[source_id].sdk_params(conn))
        elif path == "/dev/vda":
            # This code path is exercised when the boot disk is a copy:
            # boot-from-volume servers export a temporary copy of the boot
            # volume, image-booted servers with `boot_disk_copy: true` a
            # volume created from a server snapshot. `None` values in
            # boot_volume_params mean we do not override that parameter.
            boot_volume_params_defined = dict(
                filter(
                    lambda item: item[1] is not None,
                    ser_server.migration_params()["boot_volume_params"].items(),
                )
            )
            sdk_params.update(boot_volume_params_defined)
    if preserve_volume_type:
        if sdk_params.get("volume_type") is None and ser_server is not None:
            # boot copies: the serialized (already mapped) type of the volume at ``path``
            sdk_params["volume_type"] = serialized_volume_type(ser_server, device=path)
        if sdk_params.get("volume_type") is None and mapping.get("volume_type"):
            sdk_params["volume_type"] = mapping["volume_type"]
        if sdk_params.get("volume_type") is None:
            sdk_params.pop("volume_type", None)
    else:
        sdk_params.pop("volume_type", None)
    return sdk_params


def serialized_volume_type(ser_server, device=None, volume_id=None):
    """``volume_type`` of the serialized workload volume attached at ``device`` or with
    ``volume_id`` (the value the user may have mapped in workloads.yml); ``None`` if unknown.
    """
    for data in ser_server.params().get("volumes") or []:
        vol = ServerVolume.from_data(data)
        info = vol.info()
        if volume_id is not None and info.get("id") == volume_id:
            return vol.params().get("volume_type")
        if device is not None and any(
            a.get("device") == device for a in info.get("attachments") or []
        ):
            return vol.params().get("volume_type")
    return None


class OpenStackVolumeBase:
    def __init__(
        self,
        openstack_connection,
        conversion_host_id,
        ssh_key_path,
        ssh_user,
        transfer_uuid,
        conversion_host_address=None,
        state_file=None,
        log_file=None,
        timeout=DEFAULT_TIMEOUT,
        shell_factory=None,
        preserve_volume_type=False,
    ):
        # Required common parameters:
        # openstack_connection: OpenStack connection object
        # conversion_host_id: ID of conversion host instance
        # ssh_key_path: Path to SSH key authorized on conversion host
        # ssh_user: Username to create the SSH connection
        # transfer_uuid: UUID to mark processes on conversion hosts
        self.conn = openstack_connection
        self.conversion_host_id = conversion_host_id
        self.ssh_key_path = ssh_key_path
        self.ssh_user = ssh_user
        self.transfer_uuid = transfer_uuid

        # Optional parameters:
        # conversion_host_address: Optional address used to override 'access_ipv4'
        # state_file: File to hold current disk transfer state
        # log_file: Debug log path for volume migration
        # preserve_volume_type: keep the serialized volume_type of destination
        #                       volumes instead of the destination default
        self.conversion_host_address = conversion_host_address
        self.state_file = state_file
        self.log_file = log_file
        self.timeout = timeout
        self.preserve_volume_type = bool(preserve_volume_type)
        # Configure logging
        self.log = logging.getLogger("osp-osp")
        log_format = logging.Formatter(
            "%(asctime)s:%(levelname)s: " + "%(message)s (%(module)s:%(lineno)d)"
        )
        if log_file:
            log_handler = logging.FileHandler(log_file)
        else:
            log_handler = logging.NullHandler()
        log_handler.setFormatter(log_format)
        self.log.addHandler(log_handler)
        self.log.setLevel(logging.DEBUG)

        if self._converter() is None:
            raise RuntimeError(f"Cannot find instance {self.conversion_host_id}")

        # shell_factory(address, ssh_user, key_path) builds the RemoteShell;
        # injectable so the volume logic can be tested without SSH.
        self.shell_factory = shell_factory or RemoteShell
        self.shell = self.shell_factory(
            self._converter_address(), ssh_user, ssh_key_path
        )
        self._check_ssh()

        # Ports chosen for NBD export
        self.claimed_ports = []

    def _check_ssh(self):
        """Quick SSH check of the conversion host: a module fails at once."""
        self.shell.test_ssh_connection()

    def _source_vm(self):
        """
        Changes to the VM returned by get_server_by_id are not necessarily
        reflected in existing objects, so just get a new one every time.
        """
        return self.conn.get_server_by_id(self.source_instance_id)

    def _test_source_vm_shutdown(self):
        """Make sure the source VM is shutdown, and fail if it isn't."""
        server = self.conn.compute.get_server(self._source_vm().id)
        if server.status != "SHUTOFF":
            raise RuntimeError("Source VM is not shut down!")

    def delete_migrated_volumes(self):
        """Detach destination volumes from converter and delete them."""
        self._detach_volumes_from_destination_converter()
        self._delete_volumes()

    def _volume_still_attached(self, volume, vm):
        """Check if a volume is still attached to a VM."""
        for attachment in volume.attachments:
            if attachment["server_id"] == vm.id:
                return True
        return False

    def _get_volume_maybe(self, id_maybe):
        """Get volume by id, or None if id_maybe is None or if volume doesn't exist."""
        if not id_maybe:
            return None
        return volume_or_none(self.conn, id_maybe)

    @use_lock(ATTACH_LOCK_FILE_DESTINATION)
    def _detach_volumes_from_destination_converter(self, volume_ids=None):
        """
        Detach volumes from conversion host: the volume map's destination
        volumes, or the volumes ``volume_ids`` names (Seamless Migrate SDD 6.3).
        """
        self.log.info("Detaching volumes from the destination conversion host.")
        if volume_ids is None:
            volume_ids = [mapping["dest_id"] for mapping in self.volume_map.values()]
        converter = self._converter()
        for volume_id in volume_ids:
            volume = self._get_volume_maybe(volume_id)
            if not volume:
                continue
            if not self._volume_still_attached(volume, converter):
                self.log.info(
                    "Volume %s is not attached to conversion host, skipping detach.",
                    volume["id"],
                )
                continue

            self.log.info("Detaching volume %s.", volume["id"])
            detach_volume_bounded(self.conn, converter, volume, self.timeout)
            for second in range(self.timeout):
                converter = self._converter()
                volume = volume_or_none(self.conn, volume_id)
                if volume is None or not self._volume_still_attached(volume, converter):
                    break
                time.sleep(1)
            else:
                raise RuntimeError(
                    "Timed out waiting to detach volumes from "
                    "destination conversion host!"
                )

    def _converter_close_exports(self):
        """
        SSH to source conversion host, close the NBD export process and free
        the transfer's ports.
        """
        self._stop_exports()
        self._release_ports()

    def _stop_exports(self):
        """
        SSH to source conversion host and close the NBD export process.
        """
        self.log.info("Stopping exports from source conversion host...")
        try:
            pattern = "'" + self.transfer_uuid + "'"
            pids = self.shell.cmd_out(["pgrep", "-f", pattern]).split("\n")
            if len(pids) > 0:
                self.log.debug("Stopping NBD export PIDs (%s)", str(pids))
                try:
                    self.shell.cmd_out(["sudo", "pkill", "-f", pattern])
                except subprocess.CalledProcessError as err:
                    self.log.debug("Error stopping exports! %s", str(err))
        except subprocess.CalledProcessError as err:
            self.log.debug("Unable to get remote NBD PID! %s", str(err))

    def _delete_volumes(self):
        """Delete destination volumes."""
        self.log.info("Deleting migrated volumes from destination.")
        for path, mapping in self.volume_map.items():
            volume = self._get_volume_maybe(mapping["dest_id"])
            if not volume:
                continue
            if volume.attachments:
                self.log.warning(
                    "Volume %s is still has attachments, skipping delete.", volume["id"]
                )
                continue

            self.log.info("Deleting volume %s.", volume["id"])
            self.conn.delete_volume(volume["id"], timeout=self.timeout, wait=True)

    def check_process(self, owner):
        """Whether the owner of a lock (``host:pid``) may still hold it (SDD §6.6).

        A pid is checked only on the host it belongs to: the conversion host's
        process table knows nothing of the migrator's pids, so the owner of
        another host, or a lock in the earlier bare-pid form, counts as alive.
        """
        host, sep, pid = (owner or "").strip().rpartition(":")
        if not sep or host != _lock_host() or not pid.isdigit():
            return True
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def check_and_cleanup_lockfiles(self):
        """
        Removes lockfiles found in specific directories.
        If a lockfile is not being used by a process,
        it is either deleted or not being used.
        """
        # Remove specific lockfiles
        try:
            # Remove specific lockfiles
            for lockfile in [
                ATTACH_LOCK_FILE_SOURCE,
                ATTACH_LOCK_FILE_DESTINATION,
                PORT_LOCK_FILE,
            ]:
                pid = None
                try:
                    # Use self.shell.cmd_out to run the following command on the conversion host
                    pid = self.shell.cmd_out(["sudo", "cat", f"{lockfile}.lock"])
                except Exception as err:
                    self.log.debug("Lockfile doesn't exist %s", err)

                if pid:
                    if self.check_process(pid):
                        continue

                    # The lockfile is not being used by a process, so we can remove it
                    self.shell.cmd_val(["sudo", "rm", "-f", lockfile + ".lock"])
                    return True
        except FileNotFoundError:
            return False

    def _converter(self):
        """Refresh server object to pick up any changes."""
        return self.conn.get_server_by_id(self.conversion_host_id)

    def _converter_address(self):
        """Get IP address of conversion host."""
        if self.conversion_host_address:
            return self.conversion_host_address
        else:
            return self._converter().access_ipv4

    def _update_progress(self, dev_path, progress):
        self.log.info("Transfer progress for %s: %s%%", dev_path, str(progress))
        if self.state_file is None:
            return
        self.volume_map[dev_path]["progress"] = progress
        all_progress = {}
        for path, mapping in self.volume_map.items():
            all_progress[path] = mapping["progress"]
        # Readers (e.g. the control plane) poll this file: replace it
        # atomically so they never see a partially written document.
        state_dir = os.path.dirname(os.path.abspath(self.state_file))
        fd, tmp_path = tempfile.mkstemp(
            dir=state_dir, prefix=".progress-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf8") as state:
                json.dump(all_progress, state)
            os.replace(tmp_path, self.state_file)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _attach_volumes(self, conn, name, funcs, mark=None):
        """
        Attach all volumes in the volume map to the specified conversion host.
        Check the list of disks before and after attaching to be absolutely
        sure the right source data gets copied to the right destination disk.
        This is here because _attach_destination_volumes and
        _attach_volumes_to_converter looked almost identical.
        """
        self.log.info("Attaching volumes to %s wrapper", name)
        self.log.info("Volumes: %s", self.volume_map)
        host_func, ssh_func, update_func, volume_id_func = funcs
        self.log.info(
            "FUNCS: %s, %s, %s, %s", host_func, ssh_func, update_func, volume_id_func
        )
        for path, mapping in sorted(self.volume_map.items()):
            volume_id = volume_id_func(mapping)
            volume = conn.get_volume_by_id(volume_id)
            self.log.info("Attaching %s to %s conversion host", volume_id, name)

            disks_before = ssh_func(
                [
                    "lsblk",
                    "--noheadings",
                    "--list",
                    "--paths",
                    "--nodeps",
                    "--output NAME",
                ]
            )
            disks_before = set(disks_before.split())
            self.log.debug("Initial disk list: %s", disks_before)

            host = host_func()
            if (
                mark
                and volume["status"] == "available"
                and not self._volume_still_attached(volume, host)
            ):
                # openstacksdk's own checks pass, so it asks Nova to attach it:
                # this transfer's from here on, before its device shows, even if
                # the answer is lost - a cleanup detaches what is marked and Cinder
                # lists on this host, never a volume another migration's export
                # attached, which the SDK refuses (Seamless Migrate SDD 6.6,
                # review RV8-02, RV9-01)
                mapping[mark] = True
            try:
                conn.attach_volume(
                    volume=volume, wait=False, server=host, timeout=self.timeout
                )
            except Exception:
                if mark and mapping.get(mark):
                    # Nova may have carried it out all the same, or be at it: the mark
                    # stays only if Cinder, settled, lists this host or still shows the
                    # attach under way - decided while this export holds the attach
                    # lock, so no other export attached it meanwhile (Seamless Migrate
                    # SDD 6.6, review RV10-01, RV10-02)
                    settled = self._settled_volume(conn, volume["id"])
                    if not self._attach_may_be_ours(settled, host):
                        mapping.pop(mark, None)
                raise
            self.log.info("Waiting for volume to appear in %s wrapper", name)
            try:
                self._wait_for_volume_dev_path(conn, volume, host_func(), self.timeout)
            except Exception:
                if mark and mapping.get(mark):
                    # an attach Nova rolled back leaves no mark either; one still under
                    # way keeps it (Seamless Migrate SDD 6.6, review RV12-01)
                    now = volume_or_none(conn, volume["id"])
                    if not self._attach_may_be_ours(now, host):
                        mapping.pop(mark, None)
                raise

            disks_after = ssh_func(
                [
                    "lsblk",
                    "--noheadings",
                    "--list",
                    "--paths",
                    "--nodeps",
                    "--output NAME",
                ]
            )
            disks_after = set(disks_after.split())
            self.log.debug("Updated disk list: %s", disks_after)

            new_disks = disks_after - disks_before
            volume = conn.get_volume_by_id(volume_id)
            attachment = self._get_attachment(volume, host_func())
            dev_path = attachment["device"]
            if len(new_disks) == 1:
                if dev_path in new_disks:
                    self.log.debug(
                        "Successfully attached new disk %s, and %s "
                        "conversion host path matches OpenStack.",
                        dev_path,
                        name,
                    )
                else:
                    dev_path = new_disks.pop()
                    self.log.debug(
                        "Successfully attached new disk %s, but %s "
                        "conversion host path does not match the  "
                        "result from OpenStack. Using internal "
                        "device path %s.",
                        attachment["device"],
                        name,
                        dev_path,
                    )
            else:
                raise RuntimeError(
                    "Got unexpected disk list after attaching "
                    f"volume to {name} conversion host instance. "
                    "Failing migration procedure to avoid "
                    "assigning volumes incorrectly. New "
                    f"disks(s) inside VM: {new_disks}, disk provided by "
                    f"OpenStack: {dev_path}"
                )
            self.volume_map[path] = update_func(mapping, dev_path)

    def _settled_volume(self, conn, volume_id):
        """
        The volume ``volume_id`` once Cinder settled it - not `reserved` or
        `attaching`, where Nova is still at work and Cinder lists no attachment
        yet - within ATTACH_SETTLE_S (or the module's timeout, if shorter), else
        as it is then; None when Cinder no longer has it. It runs under the
        attach lock: a reservation Nova abandoned never settles (Seamless
        Migrate SDD 6.6, review RV10-01, RV10-02, RV11-01).
        """
        limit = min(self.timeout, ATTACH_SETTLE_S)
        # each poll is a Cinder GET as well as its 1 s sleep: the wait ends at its seconds,
        # measured, not only at as many polls (review RV13-03)
        deadline = time.monotonic() + limit
        for second in range(limit):
            volume = volume_or_none(conn, volume_id)
            if volume is None or getattr(volume, "status", None) not in ATTACHING:
                return volume
            if time.monotonic() >= deadline:
                return volume
            time.sleep(1)
        return volume_or_none(conn, volume_id)

    def _get_attachment(self, volume, vm):
        """
        Get the attachment object from the volume with the matching server ID.
        Convenience method for use only when the attachment is already certain.
        """
        for attachment in volume.attachments:
            if attachment["server_id"] == vm.id:
                return attachment
        raise RuntimeError("Volume is not attached to the specified instance!")

    def _attach_may_be_ours(self, volume, host):
        """
        Whether an attach of ``volume`` to ``host`` whose outcome this module
        did not see may be its own: Cinder lists the host, or still shows the
        attach under way (Seamless Migrate SDD 6.6, review RV10-01, RV10-02).
        """
        return volume is not None and (
            self._volume_still_attached(volume, host)
            or getattr(volume, "status", None) in ATTACHING
        )

    def _wait_for_volume_dev_path(self, conn, volume, vm, timeout):
        """
        After an attach call that succeeded, wait until Cinder lists ``vm``'s
        attachment of the volume with its device path. nova-api reserves the
        volume before it answers, so one `available` again, or in `error`,
        with no attachment means nova-compute rolled the attach back: that
        fails at once. One still `reserved` or `attaching` fails after
        ATTACH_UNDER_WAY_S, and only a listed attachment whose device has not
        shown waits ``timeout``: the wait holds the attach lock (Seamless
        Migrate SDD 6.6, review RV12-01).
        """
        volume_id = volume.id
        # each poll is a Cinder GET as well as its 1 s sleep: the cap is seconds, measured, as well
        # as polls (review RV13-03)
        under_way, under_way_since = 0, None
        for second in range(timeout):
            volume = conn.get_volume_by_id(volume_id)
            if volume.attachments:
                attachment = self._get_attachment(volume, vm)
                if attachment["device"].startswith("/dev/"):
                    return
            else:
                status = getattr(volume, "status", None)
                if status in ("available", "error"):
                    raise RuntimeError(
                        "Nova did not attach volume %s: Cinder shows it %s, attached nowhere"
                        % (volume_id, status)
                    )
                now = time.monotonic()
                if under_way_since is None:
                    under_way_since = now
                under_way += 1
                waited = max(now - under_way_since, under_way)
                if waited >= ATTACH_UNDER_WAY_S:
                    raise RuntimeError(
                        "Nova did not finish attaching volume %s: Cinder still shows it %s "
                        "after %d s" % (volume_id, status, waited)
                    )
            time.sleep(1)
        raise RuntimeError("Timed out waiting for volume device path!")

    def __read_used_ports(self):
        """
        Should only be called from functions locking the port list file, e.g.
        _find_free_port and _release_ports. Returns a set containing the ports
        currently used by all the migrations running on this conversion host.
        """
        try:
            cmd = [
                "sudo",
                "bash",
                "-c",
                f'"test -e {PORT_MAP_FILE} || echo [] > {PORT_MAP_FILE}"',
            ]
            result = self.shell.cmd_out(cmd)
            if result:
                self.log.debug("Port write result: %s", result)
        except subprocess.CalledProcessError as err:
            raise RuntimeError("Unable to initialize port map file!") from err

        try:  # Try to read in the set of used ports
            cmd = ["sudo", "cat", PORT_MAP_FILE]
            result = self.shell.cmd_out(cmd)
            used_ports = set(json.loads(result))
        except ValueError:
            self.log.info(
                "Unable to read port map from %s, re-initializing it...",
                PORT_MAP_FILE,
            )
            used_ports = set()
        except subprocess.CalledProcessError as err:
            # ssh's own failure is transient: a give-up the control plane retries, as the
            # waits of SDD 6.6 end; any other failure of the read is the port map's (audit A-17)
            if err.returncode == SSH_FAILED:
                raise RuntimeError(
                    "SSH timeout: conversion host %s unreachable over SSH, reading the port map"
                    % getattr(self.shell, "address", "?")
                ) from err
            raise RuntimeError("Unable to read the port map %s" % PORT_MAP_FILE) from err

        self.log.info("Currently used ports: %s", str(list(used_ports)))
        return used_ports

    def __write_used_ports(self, used_ports):
        """
        Should only be called from functions locking the port list file, e.g.
        _find_free_port and _release_ports. Writes out the given port list to
        the port list file on the current conversion host.
        """
        try:  # Write out port map to destination conversion host
            cmd = ["-T", "sudo", "bash", "-c", '"cat > ' + PORT_MAP_FILE + '"']
            input_json = json.dumps(list(used_ports))
            sub = self.shell.cmd_sub(
                cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                universal_newlines=True,
            )
            out, err = sub.communicate(input_json)
            if out:
                self.log.debug("Wrote port file, stdout: %s", out)
            if err:
                self.log.debug("Wrote port file, stderr: %s", err)
        except subprocess.CalledProcessError as err:
            self.log.debug(
                "Unable to write port map to conversion host! Error was: %s",
                str(err),
            )

    def _identified_path(self, volume_id, device):
        """
        The path a tool that opens the disk of volume ``volume_id``, attached
        at ``device``, is given, once that disk's identity is checked: the
        export's NBD server, the transfer's qemu-img or nbdcopy. The kernel
        gives a freed device path to the next disk attached, so a volume taken
        off the host would have the tool read, or write, another disk without
        an error. The disk's virtio serial - the volume id Nova gives it, cut to
        20 characters - must not name another volume, and a virtio-blk disk
        (vdX), which always shows a serial file, must still have one; a disk
        with no serial file (not virtio-blk, sdX) is given by its path. The
        tool is given udev's link of the serial, which resolves at its open to
        the volume itself; nbdkit resolves it once, at its start, so a swap
        after that is a known residual (Seamless Migrate SDD 6.6, review
        RV12-03, RV13-01, RV13-02, RV14-02).
        """
        name = device.rsplit("/", 1)[-1]
        try:
            serial = self.shell.cmd_out(["cat", "/sys/block/%s/serial" % name]).strip()
        except subprocess.CalledProcessError as err:
            if err.returncode == SSH_FAILED:
                raise
            if name.startswith("vd"):
                raise DiskIdentityError(
                    "%s on the conversion host is gone: volume %s left the host"
                    % (device, volume_id)
                ) from err
            serial = ""  # not virtio-blk: no serial file to check
        if not serial:
            self.log.warning("%s shows no virtio serial: using it by its path", device)
            return device
        if not volume_id.startswith(serial):
            raise DiskIdentityError(
                "%s on the conversion host is the disk of volume %s, not of volume %s: "
                "the volume left the host" % (device, serial, volume_id)
            )
        link = "/dev/disk/by-id/virtio-" + serial
        found = self.shell.cmd_val(["test", "-b", link])
        if found == SSH_FAILED:
            # a give-up the control plane retries, as the serial read's own error is (RV14-02)
            raise RuntimeError(
                "SSH timeout: conversion host %s unreachable over SSH, checking the by-id link of %s"
                % (getattr(self.shell, "address", "?"), device)
            )
        if found != 0:
            self.log.warning("%s has no %s link: using it by its path", device, link)
            return device
        return link

    @use_lock(PORT_LOCK_FILE)
    def _find_free_port(self):
        """
        Reserve ports on the current conversion host. Lock a file containing
        the used ports, select some ports from the range that is unused, and
        check that the port is available on the conversion host. Add this to
        the locked file and unlock it for the next conversion.
        """
        used_ports = self.__read_used_ports()

        # Choose ports from the available possibilities, and try to bind
        ephemeral_ports = set(range(49152, 65535))
        available_ports = ephemeral_ports - used_ports
        # ssh's 255 is no port in use: give up after the window (SDD 6.6, review RV10-03)
        outage = SshOutage(getattr(self.shell, "address", "?"), "a free port")

        try:
            port = available_ports.pop()
            while not self._test_port_available(port, outage):
                if outage.failing:
                    # ssh failed, not the port: the same port again once a second, so the
                    # window ends the wait, never the port range (SDD 6.6, review RV11-03)
                    time.sleep(1)
                    continue
                self.log.info("Port %d not available, trying another.", port)
                used_ports.add(port)  # Mark used to avoid trying again
                port = available_ports.pop()
        except KeyError as err:
            raise RuntimeError("No free ports on conversion host!") from err
        used_ports.add(port)
        self.__write_used_ports(used_ports)
        self.log.info("Allocated port %d, all used: %s", port, used_ports)

        self.claimed_ports.append(port)
        return port

    @use_lock(PORT_LOCK_FILE)
    def _release_ports(self):
        used_ports = self.__read_used_ports()

        for port in self.claimed_ports:
            try:
                used_ports.remove(port)
            except KeyError:
                self.log.debug("Port already released? %d", port)

        self.log.info("Cleaning used ports: %s", used_ports)
        self.__write_used_ports(used_ports)

    def _test_port_available(self, port, outage=None):
        """
        See if a port is open on the source conversion host by trying to listen
        on it; ``outage`` notes each try that ssh itself failed.
        """
        result = self.shell.cmd_val(["timeout", "1", "nc", "-l", str(port)])
        if outage is not None:
            outage.seen(result == SSH_FAILED)
        # The 'timeout' command returns 124 when the command times out, meaning
        # nc was successful and the port is free.
        return result == 124


class RemoteShell:  # pylint: disable=ansible-bad-function
    """Run SSH/SCP commands on a remote conversion host via subprocess."""
    def __init__(self, address, ssh_user, key_path=None):
        self.address = address
        self.ssh_user = ssh_user
        self.key_path = key_path

    def _default_options(self):
        options = [
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
            # a silent partition ends the ssh in about a minute, not the
            # kernel's ~2 h TCP keepalive, while the source may be stopped (SDD §6.6)
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
        ]
        if self.key_path:
            options.extend(["-i", self.key_path])
        return options

    def ssh_preamble(self):
        """Common options to SSH into a conversion host."""
        preamble = ["ssh"]
        preamble.extend(self._default_options())
        preamble.extend([self.ssh_user + "@" + self.address])
        return preamble

    def cmd_out(self, command, **kwargs):
        """Run command on the target conversion host and get the output."""
        args = self.ssh_preamble()
        args.extend(command)
        return subprocess.check_output(args, **kwargs).decode("utf-8").strip()

    def cmd_val(self, command, **kwargs):
        """Run command on the target conversion host and get return code."""
        args = self.ssh_preamble()
        args.extend(command)
        return subprocess.call(args, **kwargs)

    def cmd_sub(self, command, **kwargs):
        """Start a long-running command on the target conversion host."""
        args = self.ssh_preamble()
        args.extend(command)
        return subprocess.Popen(args, **kwargs)

    def scp_to(self, source, destination):
        """Copy a file to the target conversion host."""
        command = ["scp"]
        command.extend(self._default_options())
        remote_path = self.ssh_user + "@" + self.address + ":" + destination
        command.extend([source, remote_path])
        return subprocess.call(command)

    def scp_from(self, source, destination, recursive=False):
        """Copy a file from the source conversion host."""
        command = ["scp"]
        command.extend(self._default_options())
        if recursive:
            command.extend(["-r"])
        remote_path = self.ssh_user + "@" + self.address + ":" + source
        command.extend([remote_path, destination])
        return subprocess.call(command)

    def test_ssh_connection(self):
        """Quick SSH connectivity check."""
        out = self.cmd_out(["echo connected"])
        if out != "connected":
            raise RuntimeError(self.address + ": SSH test unsuccessful!")


class OpenstackVolumeExport(OpenStackVolumeBase):
    """Export volumes from an OpenStack instance over NBD."""

    def prepare_exports(self):
        """
        Prepare the volumes to be exported. This method needs to be implemented in the
        class that exports the VM or the list of detached volumes
        """
        raise NotImplementedError("Please Implement this method")

    def reattach_detached_volumes(self):
        """
        After a failed export: attach each data volume of the map that no
        server holds - the export detached it from the source VM, and never
        attached it to the conversion host - back to the source VM. Only the
        OpenStack API is used: the conversion host may be what failed, and the
        source cleanup checks SSH first, so without this the rollback that
        follows would start the source VM without them (Seamless Migrate SDD
        6.6, review RV8-01). Best effort: what cannot be attached is left to the
        source cleanup.
        """
        for path, mapping in sorted(self.volume_map.items()):
            if path == "/dev/vda" or not mapping.get("source_id"):
                continue  # the root disk or its copy is the source cleanup's
            try:
                volume = volume_or_none(self.conn, mapping["source_id"])
                if volume is None or getattr(volume, "attachments", None):
                    continue
                self.log.info("Attaching %s back to the source VM", volume["id"])
                self.conn.attach_volume(
                    volume=volume, server=self._source_vm(), wait=True, timeout=self.timeout
                )
            except Exception as err:  # pylint: disable=broad-except
                self.log.warning(
                    "Could not attach volume %s back to the source VM: %s",
                    mapping["source_id"],
                    err,
                )

    def remove_unheld_root_copy(self):
        """
        After a failed export, once the data volumes are back: delete the root
        disk's copy when no server holds it, then the snapshot or the image it
        was made from - through the OpenStack API alone, so an outage the
        source cleanup cannot outlast leaves none of them behind (Seamless
        Migrate SDD 6.6, review RV9-03). A copy the conversion host holds is the
        cleanup's, and the source VM's own boot volume - what the root entry
        names before a copy exists - is never deleted. Best effort: what cannot
        be deleted is left to the source cleanup.
        """
        root = self.volume_map.get("/dev/vda")
        if not root:
            return
        try:
            volume = self._get_volume_maybe(root["source_id"])
            if volume is not None:
                holders = {a["server_id"] for a in getattr(volume, "attachments", None) or []}
                if holders - {self.source_instance_id}:
                    return  # the copy, on the conversion host: the cleanup's
                if not holders:
                    self.log.info("Removing copy %s of the root volume", volume.id)
                    self.conn.delete_volume(
                        name_or_id=volume.id, wait=True, timeout=self.timeout
                    )
            if root["snap_id"]:
                self.log.info("Deleting temporary root device snapshot")
                self.conn.delete_volume_snapshot(
                    timeout=self.timeout, wait=True, name_or_id=root["snap_id"]
                )
            if root["image_id"]:
                self.log.info("Deleting temporary root device image")
                self.conn.delete_image(
                    timeout=self.timeout, wait=True, name_or_id=root["image_id"]
                )
        except Exception as err:  # pylint: disable=broad-except
            self.log.warning("Could not remove the copy of the root volume: %s", err)

    def _get_root_and_data_volumes(self):
        """
        Volume mapping step one: get the IDs and sizes of all volumes.
        Key off the original device path to eventually preserve this
        order on the destination. This method needs to be implemented in the
        class that exports the VM or the list of detached volumes
        """
        raise NotImplementedError("Please Implement this method")

    def _validate_volumes_match_data(self):
        """
        Check that the volumes as exported into the workload metadata YAML
        still match what is actually attached on the source VM, raise
        an error if not.
        """
        scanned_volume_ids = set(
            map(lambda vol: vol["source_id"], self.volume_map.values())
        )
        data_volume_ids = set(
            map(
                lambda vol: vol.get("_info", {}).get("id"),
                self.ser_server.params()["volumes"],
            )
        )
        if data_volume_ids != scanned_volume_ids:
            message = (
                f"The scanned set of volumes on instance '{self.source_instance_id}' is not the same "
                f"as in the exported data. Scanned: {scanned_volume_ids}. In data: {data_volume_ids}."
            )
            raise exc.InconsistentState(message)

    def _detach_data_volumes_from_source(self):
        """
        Detach data volumes from source VM, and pretend to "detach" the boot
        volume by creating a new volume from a snapshot of the VM. If the VM is
        booted directly from an image, take a VM snapshot and create the new
        volume from that snapshot.
        Volume map step two: replace boot disk ID with this new volume's ID,
        and record snapshot/image ID for later deletion.
        """
        sourcevm = self._source_vm()
        if "/dev/vda" in self.volume_map:
            mapping = self.volume_map["/dev/vda"]
            volume_id = mapping["source_id"]

            # Create a snapshot of the root volume. The snapshot, the copy and
            # the image below are each created without openstacksdk's own wait,
            # which raises with the resource made, and recorded at once, then
            # waited for: an export failing at any point leaves the cleanup
            # what it made to remove (Seamless Migrate SDD 6.6, review RV8-03)
            self.log.info("Boot-from-volume instance, creating boot volume snapshot")
            root_snapshot = self.conn.create_volume_snapshot(
                force=True,
                wait=False,
                volume_id=volume_id,
                name=f"{self.boot_volume_prefix}{volume_id}",
            )
            self.volume_map["/dev/vda"]["snap_id"] = root_snapshot.id
            root_snapshot = self.conn.block_storage.wait_for_status(
                root_snapshot, status="available", failures=["error"], wait=self.timeout
            )

            # Create a new volume from the snapshot
            self.log.info("Creating new volume from boot volume snapshot")
            root_volume_copy = self.conn.create_volume(
                wait=False,
                name=f"{self.boot_volume_prefix}{volume_id}",
                snapshot_id=root_snapshot.id,
                size=root_snapshot.size,
            )

            # Update the volume map with the new volume ID
            self.volume_map["/dev/vda"]["source_id"] = root_volume_copy.id
            self.conn.block_storage.wait_for_status(
                root_volume_copy, status="available", failures=["error"], wait=self.timeout
            )
        elif sourcevm.image and self.ser_server.migration_params()["boot_disk_copy"]:
            self.log.info(
                "Image-based instance, boot_disk_copy enabled: creating snapshot"
            )
            image = self.conn.compute.create_server_image(
                name=f"{self.boot_volume_prefix}{sourcevm.name}",
                server=sourcevm.id,
                wait=False,
            )
            # Recorded at once, the volume to come: an export failing before it
            # exists leaves the cleanup an image to remove (Seamless Migrate SDD 6.6)
            self.volume_map["/dev/vda"] = dict(
                source_dev=None,
                source_id=None,
                dest_dev=None,
                dest_id=None,
                snap_id=None,
                image_id=image.id,
                name=image.name,
                size=None,
                port=None,
                url=None,
                progress=None,
                bootable=True,
            )
            self.conn.wait_for_image(image, timeout=self.timeout)
            image = self.conn.get_image_by_id(image.id)  # refresh
            if image is None or image.status != "active":
                raise RuntimeError(
                    "Could not create new image of image-based instance!"
                )
            # bootable is set once the volume is available: openstacksdk waits
            # in the create whenever it is given
            volume = self.conn.create_volume(
                image=image.id,
                wait=False,
                name=image.name,
                size=max(image.size // (1024**3), image.min_disk),
            )
            self.volume_map["/dev/vda"]["source_id"] = volume.id
            volume = self.conn.block_storage.wait_for_status(
                volume, status="available", failures=["error"], wait=self.timeout
            )
            self.conn.block_storage.set_volume_bootable_status(volume, True)
            self.volume_map["/dev/vda"].update(
                name=volume["name"],
                size=volume["size"],
                bootable=True,
            )
            self._update_progress("/dev/vda", 0.0)
        elif (
            sourcevm.image and not self.ser_server.migration_params()["boot_disk_copy"]
        ):
            self.log.info(
                "Image-based instance, boot_disk_copy disabled: skipping boot volume"
            )
        else:
            raise RuntimeError("No known boot device found for this instance!")

        for path, mapping in self.volume_map.items():
            if path != "/dev/vda":  # Detach non-root volumes
                volume_id = mapping["source_id"]
                volume = self.conn.get_volume_by_id(volume_id)
                self.log.info("Detaching %s from %s", volume["id"], sourcevm.id)
                detach_volume_bounded(self.conn, sourcevm, volume, self.timeout)

    # Lock this part to have a better chance of the OpenStack device path
    # matching the device path seen inside the conversion host.
    @use_lock(ATTACH_LOCK_FILE_SOURCE)
    def _attach_volumes_to_converter(self):
        """
        Attach all the source volumes to the conversion host. Volume mapping
        step 3: fill in the volume's device path on the source conversion host.
        """

        def update_source(volume_mapping, dev_path):
            volume_mapping["source_dev"] = dev_path
            return volume_mapping

        def volume_id(volume_mapping):
            return volume_mapping["source_id"]

        self._attach_volumes(
            self.conn,
            "source",
            (self._converter, self.shell.cmd_out, update_source, volume_id),
            mark="source_attached",
        )

    def _export_volumes_from_converter(self):
        """
        SSH to source conversion host and start an NBD export. Volume mapping
        step 4: fill in the URL to the volume's matching NBD export. Each
        disk's identity, where it shows one, is checked before its export
        starts (_identified_path).
        """
        self.log.info("Exporting volumes from source conversion host...")
        for path, mapping in self.volume_map.items():
            port = self._find_free_port()
            # Recorded at once: an export failing before this disk's export is
            # ready leaves the source cleanup its port to free (Seamless Migrate
            # SDD 6.6, review RV6-01)
            mapping["port"] = port
            volume_id = mapping["source_id"]
            disk = self._identified_path(volume_id, mapping["source_dev"])
            self.log.info("Exporting %s from volume %s", disk, volume_id)

            # Fall back to qemu-nbd if nbdkit is not present
            qemu_nbd_present = self.shell.cmd_val(["which", "qemu-nbd"]) == 0
            nbdkit_present = self.shell.cmd_val(["which", "nbdkit"]) == 0
            if nbdkit_present:
                dump_plugin = ["nbdkit", "--dump-plugin", "file"]
                file_plugin_present = self.shell.cmd_val(dump_plugin) == 0
                if not file_plugin_present:
                    self.log.info("Found nbdkit, but without file plugin.")
            else:
                file_plugin_present = False

            if nbdkit_present and file_plugin_present:
                cmd = [
                    "sudo",
                    "nbdkit",
                    # The export is only read; never let a client write
                    # to the source volume.
                    "--readonly",
                    "--exportname",
                    self.transfer_uuid,
                    "--ipaddr",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "file",
                    "file=" + disk,
                ]
                self.log.info("Using nbdkit for export command: %s", cmd)
            elif qemu_nbd_present:
                cmd = [
                    "sudo",
                    "qemu-nbd",
                    "-p",
                    str(port),
                    "-b",
                    "127.0.0.1",
                    "--fork",
                    "--verbose",
                    "--read-only",
                    "--persistent",
                    "-x",
                    self.transfer_uuid,
                    disk,
                ]
                self.log.info("Using qemu-nbd for export command: %s", cmd)
            else:
                raise RuntimeError("No supported NBD export tool available!")

            self.log.info("Exporting %s over NBD, port %s", disk, str(port))
            result = self.shell.cmd_out(cmd)
            if result:
                self.log.debug("Result from NBD exporter: %s", result)

            # Check qemu-img info on this disk to make sure it is ready
            self.log.info("Waiting for valid qemu-img info on all exports...")
            outage = SshOutage(
                getattr(self.shell, "address", "?"), "the NBD export of " + disk
            )
            for second in range(self.timeout):
                try:
                    cmd = [
                        "qemu-img",
                        "info",
                        "nbd://localhost:" + str(port) + "/" + self.transfer_uuid,
                    ]
                    image_info = self.shell.cmd_out(cmd)
                    self.log.info("qemu-img info for %s: %s", disk, image_info)
                except subprocess.CalledProcessError as error:
                    outage.seen(error.returncode == SSH_FAILED)
                    self.log.info("Got exception: %s", error)
                    self.log.info("Trying again.")
                    time.sleep(1)
                else:
                    self.log.info("All volume exports ready.")
                    break
            else:
                raise RuntimeError("Timed out starting nbdkit exports!")

            self.log.info("Volume map so far: %s", self.volume_map)


class OpenstackVolumeTransfer(OpenStackVolumeBase):

    def transfer_exports(self):
        """
        Method to transfer volumes. This method needs to be implemented in the
        class that transfers the VM or the list of detached volumes
        """
        raise NotImplementedError("Please Implement this method")

    def _setup_nbdkit_direct_urls(self, nbdkit_socket_uri, nbdkit_export_name):
        """
        Set up volume URLs to point directly to an external nbdkit socket.
        This bypasses the source conversion host entirely.
        """
        self.log.info("Setting up direct nbdkit socket URLs...")
        self.log.info("NBDkit socket URI: %s", nbdkit_socket_uri)
        self.log.info("NBDkit export name: %s", nbdkit_export_name)

        for path, mapping in self.volume_map.items():
            # Use the provided nbdkit socket URI directly
            if nbdkit_export_name:
                url = f"{nbdkit_socket_uri}/{nbdkit_export_name}"
            else:
                url = nbdkit_socket_uri
            self.volume_map[path]["url"] = url
            self.log.info("Volume %s will use NBD URL: %s", path, url)

        # Verify connectivity to nbdkit socket
        self.log.info("Waiting for valid qemu-img info on nbdkit socket...")
        pending_disks = set(self.volume_map.keys())
        outage = SshOutage(getattr(self.shell, "address", "?"), "the nbdkit socket")
        for second in range(self.timeout):
            try:
                for disk in pending_disks.copy():
                    mapping = self.volume_map[disk]
                    url = mapping["url"]
                    cmd = ["qemu-img", "info", url]
                    image_info = self.shell.cmd_out(cmd)
                    self.log.info("qemu-img info for %s: %s", disk, image_info)
                    pending_disks.remove(disk)
            except subprocess.CalledProcessError as error:
                outage.seen(error.returncode == SSH_FAILED)
                self.log.info("Got exception: %s", error)
                self.log.info("Trying again.")
                time.sleep(1)
            else:
                self.log.info("All volume exports ready.")
                break
        else:
            raise RuntimeError("Timed out connecting to nbdkit socket!")

    def _create_forwarding_process(self):
        """
        Find free ports on the destination conversion host and set up SSH
        forwarding to the NBD ports listening on the source conversion host.
        """
        # It is expected that key authorization has already been set up from
        # the destination conversion host to the source conversion host!
        source_shell = RemoteShell(
            self.source_conversion_host_address, ssh_user=self.shell.ssh_user
        )
        forward_ports = ["-N", "-T"]
        for path, mapping in self.volume_map.items():
            port = self._find_free_port()
            forward = f"{port}:localhost:{mapping['port']}"
            forward_ports.extend(["-L", forward])
            url = "nbd://localhost:" + str(port) + "/" + self.transfer_uuid
            self.volume_map[path]["url"] = url
        command = source_shell.ssh_preamble()
        command.extend(forward_ports)
        self.log.debug("SSH forwarding command: %s", " ".join(command))
        self.forwarding_process = self.shell.cmd_sub(command)
        self.forwarding_process_command = " ".join(command)

        # Check qemu-img info on all the disks to make sure everything is ready
        self.log.info("Waiting for valid qemu-img info on all exports...")
        pending_disks = set(self.volume_map.keys())
        outage = SshOutage(getattr(self.shell, "address", "?"), "the forwarded NBD exports")
        for second in range(self.timeout):
            try:
                for disk in pending_disks.copy():
                    mapping = self.volume_map[disk]
                    url = mapping["url"]
                    cmd = ["qemu-img", "info", url]
                    image_info = self.shell.cmd_out(cmd)
                    self.log.info("qemu-img info for %s: %s", disk, image_info)
                    pending_disks.remove(disk)
            except subprocess.CalledProcessError as error:
                outage.seen(error.returncode == SSH_FAILED)
                self.log.info("Got exception: %s", error)
                self.log.info("Trying again.")
                time.sleep(1)
            else:
                self.log.info("All volume exports ready.")
                break
        else:
            raise RuntimeError("Timed out starting nbdkit exports!")

    def _stop_forwarding_process(self):
        self.log.info("Stopping export forwarding on source conversion host...")
        # a transfer that failed before the forwarding started has none: its own error stands,
        # and the ports it claimed are still released after this
        if self.forwarding_process:
            self.log.debug("(PID was %s)", self.forwarding_process.pid)
            self.forwarding_process.terminate()

        if self.forwarding_process_command:
            self.log.info("Stopping forwarding from source conversion host...")
            try:
                pattern = 'pgrep -f "' + self.forwarding_process_command + '"'
                pids = self.shell.cmd_out([pattern]).split("\n")
                for pid in pids:  # There should really only be one of these
                    try:
                        out = self.shell.cmd_out(["sudo", "kill", pid])
                        self.log.debug("Stopped forwarding PID (%s). %s", pid, out)
                    except subprocess.CalledProcessError as err:
                        self.log.debug("Unable to stop PID %s! %s", pid, str(err))
            except subprocess.CalledProcessError as err:
                self.log.debug("Unable to get forwarding PID! %s", str(err))

    def _create_destination_volumes(self):
        """
        Volume mapping step 5: create new volumes on the destination OpenStack,
        and fill in dest_id with the new volumes. Each is handed to
        _destination_volume_created as soon as Cinder accepted it, before it is
        waited for - one Cinder then puts in error is recorded too - and its
        bootable flag is set once it is available (Seamless Migrate SDD 6.3).
        """
        self.log.info("Creating volumes on destination cloud")
        self._before_destination_volumes_created()
        # Detached-volume transfers have no workload (no ser_server).
        ser_server = getattr(self, "ser_server", None)
        for path, mapping in self.volume_map.items():
            sdk_params = destination_volume_sdk_params(
                self.conn,
                ser_server,
                path,
                mapping,
                self.timeout,
                preserve_volume_type=self.preserve_volume_type,
            )
            sdk_params.pop("wait", None)
            timeout = sdk_params.pop("timeout", self.timeout)
            bootable = sdk_params.pop("bootable", None)
            new_volume = self.conn.create_volume(wait=False, **sdk_params)
            self.volume_map[path]["dest_id"] = new_volume.id
            try:
                self._destination_volume_created(path)
            except Exception:
                self._delete_unrecorded_volume(new_volume, timeout)
                raise
            new_volume = self.conn.block_storage.wait_for_status(
                new_volume, status="available", failures=["error"], wait=timeout
            )
            if bootable is not None:
                self.conn.block_storage.set_volume_bootable_status(new_volume, bootable)

    def _delete_unrecorded_volume(self, volume, timeout):
        """
        Delete a volume whose record could not be written - it would outlive
        every rollback - once Cinder settled it: a volume still being created
        cannot be deleted. A failure here is logged; the record's error stands.
        """
        try:
            self.conn.block_storage.wait_for_status(
                volume, status="available", failures=["error"], wait=timeout
            )
        except Exception as err:  # an errored volume is deleted all the same
            self.log.warning("Volume %s did not become available: %s", volume.id, err)
        try:
            self.conn.delete_volume(name_or_id=volume.id, wait=False)
        except Exception as err:
            self.log.error("Could not delete the unrecorded volume %s: %s", volume.id, err)

    def _before_destination_volumes_created(self):
        """
        Called before _create_destination_volumes creates any volume: a
        subclass may remove what an earlier attempt left, so that no new
        volume waits unrecorded meanwhile (Seamless Migrate SDD 6.3, review
        RV5-01). Does nothing here.
        """

    def _destination_volume_created(self, path):
        """
        Called as soon as Cinder created the destination volume of ``path``,
        before its data is copied: a subclass may record it for a rollback
        (Seamless Migrate SDD 6.3). Does nothing here.
        """

    @use_lock(ATTACH_LOCK_FILE_DESTINATION)
    def _attach_destination_volumes(self):
        """
        Volume mapping step 6: attach the new destination volumes to the
        destination conversion host. Fill in the destination device name.
        """

        def update_dest(volume_mapping, dev_path):
            volume_mapping["dest_dev"] = dev_path
            return volume_mapping

        def volume_id(volume_mapping):
            return volume_mapping["dest_id"]

        self._attach_volumes(
            self.conn,
            "destination",
            (self._converter, self.shell.cmd_out, update_dest, volume_id),
        )

    def _convert_destination_volumes_nbdcopy(self):
        """
        Use nbdcopy to transfer data from nbdkit socket to destination volumes.
        This is used in nbdkit direct mode.
        """
        self.log.info("Converting volumes with nbdcopy...")
        for path, mapping in self.volume_map.items():
            self.log.info("Copying source VM's %s: %s", path, str(mapping))
            url = mapping["url"]
            # the volume itself, never a path another volume took (Seamless Migrate SDD 6.6)
            dest_dev = self._identified_path(mapping["dest_id"], mapping["dest_dev"])

            self.log.info("Using nbdcopy from %s to %s", url, dest_dev)
            cmd = [
                "sudo",
                "nbdcopy",
                url,
                dest_dev,
                "--progress",
            ]

            self.log.info("Starting nbdcopy: %s", " ".join(cmd))
            # nbdcopy outputs progress to stderr
            nbd_sub = self.shell.cmd_sub(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=DEVNULL,
                universal_newlines=True,
            )

            progress = 0.0
            while nbd_sub.poll() is None:
                try:
                    rc = nbd_sub.poll()
                    line = nbd_sub.stderr.readline()
                    if line:
                        self.log.info("nbdcopy: %s", line.strip())
                        if "%" in line:
                            try:
                                pct = (
                                    line.split("%")[0]
                                    .split("[")[-1]
                                    .strip()
                                )
                                progress = float(pct)
                                self.log.info("Progress: %f%%", progress)
                                self._update_progress(path, progress)
                            except (ValueError, IndexError):
                                pass
                    if rc is not None:
                        raise RuntimeError(f"nbdcopy exited unexpectedly with return code {rc}!")
                except Exception as err:
                    self.log.debug("Error reading nbdcopy output: %s", str(err))
                    time.sleep(1)
            # Get final output
            stdout, stderr = nbd_sub.communicate()
            if stdout:
                self.log.info("nbdcopy stdout: %s", stdout)
            if stderr:
                self.log.info("nbdcopy stderr: %s", stderr)

            self.log.info("nbdcopy return code: %d", nbd_sub.returncode)
            if nbd_sub.returncode != 0:
                raise RuntimeError(f"Failed to copy volume with nbdcopy! Return code: {nbd_sub.returncode}")
            self._check_still_a_disk(mapping["dest_id"], dest_dev)

            # Mark as 100% complete
            self._update_progress(path, 100.0)
            self.log.info("Successfully copied %s", path)

    def _check_still_a_disk(self, volume_id, path):
        """
        nbdcopy opens its destination again with O_CREAT when the first open
        fails (libnbd's open_local): a volume that left the host between its
        identity check and that open has its disk copied into a regular file
        nbdcopy created at ``path``, and nbdcopy exits 0. The path must still be
        a block device; a regular file there is removed, as it would keep udev
        from making the link again, and the volume was not written (Seamless
        Migrate SDD 6.6, review RV15-02).
        """
        found = self.shell.cmd_val(["test", "-b", path])
        if found == 0:
            return
        if found == SSH_FAILED:
            raise RuntimeError(
                "SSH timeout: conversion host %s unreachable over SSH, checking that %s is still a disk"
                % (getattr(self.shell, "address", "?"), path)
            )
        if self.shell.cmd_val(["test", "-f", path]) == 0:
            self.shell.cmd_val(["sudo", "rm", "-f", path])
        raise DiskIdentityError(
            "%s on the conversion host is no longer a disk: volume %s left the host before "
            "nbdcopy opened it, and nbdcopy copied into a regular file in its place, now "
            "removed; the volume was not written" % (path, volume_id)
        )

    def _convert_destination_volumes(self):
        """
        Finally run the commands to copy the exported source volumes to the
        local destination volumes. Attempt to sparsify the volumes to minimize
        the amount of data sent over the network.
        """
        self.log.info("Converting volumes...")
        for path, mapping in self.volume_map.items():
            self.log.info("Converting source VM's %s: %s", path, str(mapping))
            devname = os.path.basename(mapping["dest_dev"])
            overlay = "/tmp/" + devname + "-" + self.transfer_uuid + ".qcow2"
            # the volume itself, never a path another volume took (Seamless Migrate SDD 6.6)
            target = self._identified_path(mapping["dest_id"], mapping["dest_dev"])

            def _log_convert(source_disk, source_format, mapping):
                """Write qemu-img convert progress to the wrapper log."""
                self.log.info("Copying volume data...")
                cmd = [
                    "sudo",
                    "qemu-img",
                    "convert",
                    "-p",
                    "-f",
                    source_format,
                    "-O",
                    "host_device",
                    source_disk,
                    target,
                ]
                # Non-blocking output processing stolen from pre_copy.py
                img_sub = self.shell.cmd_sub(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=DEVNULL,
                    universal_newlines=True,
                    bufsize=1,
                )
                flags = fcntl.fcntl(img_sub.stdout, fcntl.F_GETFL)
                flags |= os.O_NONBLOCK
                fcntl.fcntl(img_sub.stdout, fcntl.F_SETFL, flags)
                buf = b""
                while img_sub.poll() is None:
                    try:
                        buf += os.read(img_sub.stdout.fileno(), 1)
                    except (IOError, OSError) as err:
                        if err.errno != errno.EAGAIN:
                            raise
                        time.sleep(1)
                        continue
                    if buf:
                        try:
                            matches = self.qemu_progress_re.search(buf.decode())
                            if matches is not None:
                                progress = float(matches.group(1))
                                self._update_progress(path, progress)
                                buf = b""
                        except ValueError:
                            self.log.debug("No match yet. %s", str(buf))
                    else:
                        time.sleep(1)
                self.log.info("Conversion return code: %d", img_sub.returncode)
                if img_sub.returncode != 0:
                    try:
                        out = img_sub.stdout.read()
                    except (IOError, OSError) as err:
                        self.log.debug("Error reading stderr? %s", str(err))
                    raise RuntimeError("Failed to convert volume! " + out)
                # Just in case qemu-img returned before readline got to 100%
                self._update_progress(path, 100.0)

            try:
                self.log.info("Attempting initial sparsify...")
                environment = os.environ.copy()
                environment["LIBGUESTFS_BACKEND"] = "direct"

                cmd = [
                    "sudo",
                    "qemu-img",
                    "create",
                    "-f",
                    "qcow2",
                    "-b",
                    mapping["url"],
                    "-F",
                    "raw",
                    overlay,
                ]
                out = self.shell.cmd_out(cmd)
                self.log.info("Overlay output: %s", out)

                cmd = [
                    "sudo",
                    "--preserve-env=LIBGUESTFS_BACKEND",
                    "virt-sparsify",
                    "--in-place",
                    overlay,
                ]
                with open(self.log_file, "a", encoding="utf8") as log_fd:
                    img_sub = self.shell.cmd_sub(
                        cmd,
                        stdout=log_fd,
                        stderr=subprocess.STDOUT,
                        stdin=DEVNULL,
                        env=environment,
                    )
                    returncode = img_sub.wait()
                    self.log.info("Sparsify return code: %d", returncode)
                    if returncode != 0:
                        raise RuntimeError("Failed to convert volume!")

                _log_convert(overlay, "qcow2", mapping)
            except (OSError, RuntimeError, subprocess.CalledProcessError):
                self.log.info("Sparsify failed, converting whole device...")
                self.shell.cmd_val(["sudo", "rm", "-f", overlay])
                _log_convert(mapping["url"], "raw", mapping)

    @use_lock(ATTACH_LOCK_FILE_DESTINATION)
    def _detach_destination_volumes(self):
        """Disconnect new volumes from destination conversion host."""
        self.log.info("Detaching volumes from destination wrapper.")
        for path, mapping in self.volume_map.items():
            volume_id = mapping["dest_id"]
            volume = self.conn.get_volume_by_id(volume_id)
            detach_volume_bounded(self.conn, self._converter(), volume, self.timeout)


class OpenstackVolumeClean(OpenStackVolumeBase):

    def _check_ssh(self):
        """
        Ride out a conversion host that cannot be reached, as a lock wait does:
        after a reboot mid-export or mid-transfer it holds volumes only this
        cleanup gives back to the source VM. The other modules' checks fail at
        once (Seamless Migrate SDD 6.6, review RV9-02).
        """
        outage = SshOutage(getattr(self.shell, "address", "?"), "the source cleanup")
        for second in range(DEFAULT_TIMEOUT):
            try:
                return self.shell.test_ssh_connection()
            except subprocess.CalledProcessError as err:
                if err.returncode != SSH_FAILED:
                    raise
                outage.seen(True)
                self.log.info("Conversion host unreachable, waiting: %s", err)
                time.sleep(1)
        return self.shell.test_ssh_connection()

    def close_exports(self):
        """
        Put the source VM's volumes back where they were: stop the NBD
        exports, give the volumes back - the attach lock and the API are all
        that needs - and only then free the ports the map records. A port lock
        another migrator holds so never keeps the volumes off the source VM; a
        release that fails is logged, never raised over the handback, and a map
        that records no port takes no port lock (Seamless Migrate SDD 6.6,
        review RV13-04).
        """
        self._stop_exports()
        try:
            self._detach_volumes_from_source_converter()
            self._attach_data_volumes_to_source()
        finally:
            self._release_claimed_ports()

    def _release_claimed_ports(self):
        if not self.claimed_ports:
            return
        try:
            # as a failed transfer's release: a host it cannot reach cannot update its port
            # map, and a wait for its port lock would hold the stopped source VM another
            # window (Seamless Migrate SDD 6.6, review RV14-01)
            self.shell.test_ssh_connection()
        except Exception as err:  # pylint: disable=broad-except
            self.log.warning(
                "Ports %s stay claimed on the conversion host it cannot reach: %s",
                self.claimed_ports,
                err,
            )
            return
        try:
            self._release_ports()
        except Exception as err:  # pylint: disable=broad-except
            self.log.warning("Could not release ports %s: %s", self.claimed_ports, err)

    @use_lock(ATTACH_LOCK_FILE_SOURCE)
    def _detach_volumes_from_source_converter(self):
        """
        Detach volumes from conversion host: each mapped volume this transfer
        attached there - its device recorded, or marked source_attached as soon
        as Nova accepted the attach, so one a failed export left before its
        device showed is detached too - that Cinder still lists there. A volume
        another migration's export attached, a shared multi-attach volume, is
        never: Cinder's attachment names the server, not the migration
        (Seamless Migrate SDD 6.6, review RV6-01, RV8-02).
        """
        self.log.info("Detaching volumes from the source conversion host.")
        converter = self._converter()
        for path, mapping in self.volume_map.items():
            if not (mapping.get("source_attached") or mapping.get("source_dev")):
                continue  # this transfer never attached it
            # an attach Nova is still at shows on this host only once done (review RV10-02)
            volume = (
                self._settled_volume(self.conn, mapping["source_id"])
                if mapping["source_id"]
                else None
            )
            if volume is None:
                continue
            self.log.info("Inspecting volume %s", volume.id)
            if not self._volume_still_attached(volume, converter):
                self.log.info(
                    "Volume is not attached to conversion host, skipping detach."
                )
                continue
            detach_volume_bounded(self.conn, converter, volume, self.timeout)
            for second in range(self.timeout):
                converter = self._converter()
                volume = self.conn.get_volume_by_id(mapping["source_id"])
                if not self._volume_still_attached(volume, converter):
                    break
                time.sleep(1)
            else:
                raise RuntimeError(
                    "Timed out waiting to detach volumes from "
                    "source conversion host!"
                )

    def _attach_data_volumes_to_source(self):
        """
        Reattach the data volumes, then clean up the copy of the root volume,
        its snapshot and image. The map may be one a failed export left: the
        root entry names the source VM's own boot volume until the copy exists,
        and that volume is never deleted (Seamless Migrate SDD 6.6, review
        RV6-01). Every step is tried, the data volumes first: one that fails -
        a copy Cinder will not delete - never keeps a data volume off the
        source VM, and the cleanup fails at the end naming each step it could
        not do (review RV8-03).
        """
        self.log.info("Re-attaching volumes to source VM...")
        steps = [
            (
                "attach volume %s back to the source VM" % mapping["source_id"],
                functools.partial(self._attach_volume_to_source, mapping["source_id"]),
            )
            for path, mapping in sorted(self.volume_map.items())
            if path != "/dev/vda"
        ]
        root = self.volume_map.get("/dev/vda")
        if root is not None:
            steps.append((
                "remove the copy %s of the root volume" % root["source_id"],
                functools.partial(self._remove_root_volume_copy, root["source_id"]),
            ))
            if root["snap_id"]:
                steps.append((
                    "delete the root volume snapshot %s" % root["snap_id"],
                    functools.partial(self._delete_root_snapshot, root["snap_id"]),
                ))
            if root["image_id"]:
                steps.append((
                    "delete the root image %s" % root["image_id"],
                    functools.partial(self._delete_root_image, root["image_id"]),
                ))
        failures = []
        for what, step in steps:
            try:
                step()
            except Exception as err:  # pylint: disable=broad-except
                self.log.error("Could not %s: %s", what, err)
                failures.append("could not %s: %s" % (what, err))
        if failures:
            raise RuntimeError("Source cleanup incomplete: " + "; ".join(failures))

    def _attach_volume_to_source(self, volume_id):
        """Attach a data volume back to the source VM, unless it holds it."""
        volume = self.conn.get_volume_by_id(volume_id)
        sourcevm = self._source_vm()
        if self._volume_still_attached(volume, sourcevm):
            self.log.info("Volume %s is already attached to source VM", volume.id)
            return
        self.log.info("Attaching %s back to source VM", volume.id)
        self.conn.attach_volume(
            volume=volume, server=sourcevm, wait=True, timeout=self.timeout
        )

    def _remove_root_volume_copy(self, volume_id):
        """Delete the temporary copy of the source root disk, never the source VM's own."""
        root = self._get_volume_maybe(volume_id)
        if root is None:
            self.log.info("No copy of the root volume to remove")
        elif self._volume_still_attached(root, self._source_vm()):
            self.log.info(
                "Volume %s is the source VM's own boot volume, keeping it", root.id
            )
        else:
            self.log.info("Removing copy of root volume")
            self.conn.delete_volume(name_or_id=root.id, wait=True, timeout=self.timeout)

    def _delete_root_snapshot(self, snapshot_id):
        self.log.info("Deleting temporary root device snapshot")
        self.conn.delete_volume_snapshot(
            timeout=self.timeout, wait=True, name_or_id=snapshot_id
        )

    def _delete_root_image(self, image_id):
        """Remove the root image copy, of an image-launched instance."""
        self.log.info("Deleting temporary root device image")
        self.conn.delete_image(timeout=self.timeout, wait=True, name_or_id=image_id)
