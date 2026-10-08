"""In-memory fakes for the openstacksdk cloud layer and RemoteShell.

They implement only what volume_common and warm_migration use, record every
call and model the conversion hosts' block devices so that the lsblk
before/after logic of ``OpenStackVolumeBase._attach_volumes`` works.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import io
import itertools
import json
import re

GIB = 1024 ** 3


class Obj(dict):
    """A dict with attribute access, like openstacksdk resources."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


class FakeCloud:
    def __init__(self, name):
        self.name = name
        self.calls = []
        self.servers = {}
        self.volumes = {}
        self.snapshots = {}
        self.images = {}
        self.devices = {}
        self.addresses = {}
        self.fail_on = {}
        self._ids = itertools.count(1)
        self.block_storage = _FakeBlockStorage(self)
        self.compute = _FakeCompute(self)

    # --- model helpers -------------------------------------------------
    def new_id(self, kind):
        return "%s-%s-%d" % (self.name, kind, next(self._ids))

    def record(self, method, **kwargs):
        self.calls.append((method, kwargs))
        failure = self.fail_on.get(method)
        if failure is not None and failure(kwargs):
            raise RuntimeError("injected %s failure" % method)

    def calls_to(self, *methods):
        return [call for call in self.calls if call[0] in methods]

    def add_conversion_host(self, server_id, address):
        self.servers[server_id] = Obj(
            id=server_id,
            name=server_id,
            status="ACTIVE",
            access_ipv4=address,
            volumes=[],
            image={"id": "conversion-image"},
        )
        self.devices[server_id] = ["/dev/vda"]
        self.addresses[address] = server_id
        return self.servers[server_id]

    def add_server(self, server_id, name, image=""):
        self.servers[server_id] = Obj(
            id=server_id,
            name=name,
            status="ACTIVE",
            access_ipv4="",
            volumes=[],
            image=image,
        )
        return self.servers[server_id]

    def add_volume(
        self,
        volume_id,
        name,
        size,
        bootable=False,
        volume_type="ceph",
        server_id=None,
        device=None,
    ):
        volume = Obj(
            id=volume_id,
            name=name,
            size=size,
            bootable=bootable,
            volume_type=volume_type,
            attachments=[],
            status="available",
        )
        self.volumes[volume_id] = volume
        if server_id is not None:
            volume.attachments.append(
                {"server_id": server_id, "device": device, "volume_id": volume_id}
            )
            volume.status = "in-use"
            self.servers[server_id].volumes.append({"id": volume_id})
        return volume

    # --- cloud layer -----------------------------------------------------
    def get_server_by_id(self, server_id):
        return self.servers.get(server_id)

    def get_volume_by_id(self, volume_id):
        return self.volumes.get(volume_id)

    def get_image_by_id(self, image_id):
        return self.images.get(image_id)

    def create_volume_snapshot(self, volume_id, force=False, wait=True, timeout=None, **kwargs):
        self.record(
            "create_volume_snapshot", volume_id=volume_id, force=force, wait=wait, **kwargs
        )
        snapshot = Obj(
            id=self.new_id("snap"),
            volume_id=volume_id,
            size=self.volumes[volume_id].size,
            name=kwargs.get("name"),
            status="available",
        )
        self.snapshots[snapshot.id] = snapshot
        return snapshot

    def create_volume(self, size, wait=True, timeout=None, image=None, bootable=None, **kwargs):
        self.record(
            "create_volume", size=size, wait=wait, image=image, bootable=bootable, **kwargs
        )
        volume = Obj(
            id=self.new_id("vol"),
            size=size,
            bootable=bool(bootable),
            attachments=[],
            status="available",
            image_id=image,
            name=kwargs.get("name"),
            snapshot_id=kwargs.get("snapshot_id"),
            volume_type=kwargs.get("volume_type"),
            description=kwargs.get("description"),
            availability_zone=kwargs.get("availability_zone"),
        )
        self.volumes[volume.id] = volume
        return volume

    def attach_volume(self, server, volume, device=None, wait=True, timeout=None):
        self.record("attach_volume", server=server.id, volume=volume.id)
        volume = self.volumes[volume.id]
        devices = self.devices.setdefault(server.id, [])
        dev = next(
            "/dev/vd" + letter
            for letter in "abcdefghijklmnopqrstuvwxyz"
            if "/dev/vd" + letter not in devices
        )
        devices.append(dev)
        volume.attachments.append(
            {"server_id": server.id, "device": dev, "volume_id": volume.id}
        )
        volume.status = "in-use"

    def detach_volume(self, server, volume, wait=True, timeout=None):
        self.record("detach_volume", server=server.id, volume=volume.id)
        volume = self.volumes[volume.id]
        for attachment in list(volume.attachments):
            if attachment["server_id"] == server.id:
                volume.attachments.remove(attachment)
                devices = self.devices.get(server.id, [])
                if attachment["device"] in devices:
                    devices.remove(attachment["device"])
        if not volume.attachments:
            volume.status = "available"

    def delete_volume(self, name_or_id=None, wait=True, timeout=None, force=False):
        self.record("delete_volume", name_or_id=name_or_id)
        volume = self.volumes.get(name_or_id)
        if volume is None:
            return False
        if volume.attachments:
            raise RuntimeError("volume %s is still attached" % name_or_id)
        del self.volumes[name_or_id]
        return True

    def delete_volume_snapshot(self, name_or_id=None, wait=False, timeout=None):
        self.record("delete_volume_snapshot", name_or_id=name_or_id)
        if any(v.get("snapshot_id") == name_or_id for v in self.volumes.values()):
            # Like RBD: a snapshot with dependent clones cannot be deleted.
            raise RuntimeError("snapshot %s has dependent volumes" % name_or_id)
        return self.snapshots.pop(name_or_id, None) is not None

    def delete_image(self, name_or_id, wait=False, timeout=3600, delete_objects=True):
        self.record("delete_image", name_or_id=name_or_id)
        return self.images.pop(name_or_id, None) is not None

    def wait_for_image(self, image, timeout=3600):
        self.record("wait_for_image", image=image.id)
        return self.images[image.id]


class _FakeBlockStorage:
    def __init__(self, cloud):
        self.cloud = cloud

    def wait_for_status(self, res, status="available", failures=None, interval=2, wait=None, **kwargs):
        self.cloud.record("wait_for_status", id=res.id, status=status)
        return self.cloud.volumes.get(res.id) or self.cloud.snapshots.get(res.id) or res

    def set_volume_bootable_status(self, volume, bootable):
        self.cloud.record("set_volume_bootable_status", volume=volume.id, bootable=bootable)
        self.cloud.volumes[volume.id].bootable = bootable

    def extend_volume(self, volume, size):
        self.cloud.record("extend_volume", volume=volume.id, size=size)
        self.cloud.volumes[volume.id].size = size


class _FakeCompute:
    def __init__(self, cloud):
        self.cloud = cloud

    def get_server(self, server_id):
        return self.cloud.servers[server_id]

    def find_server(self, name_or_id, ignore_missing=True, **query):
        self.cloud.record("find_server", name_or_id=name_or_id)
        if name_or_id in self.cloud.servers:
            return self.cloud.servers[name_or_id]
        named = [s for s in self.cloud.servers.values() if s.name == name_or_id]
        if named:
            return named[0]
        if ignore_missing:
            return None
        raise RuntimeError("server %s not found" % name_or_id)

    def servers(self, details=True, **query):
        self.cloud.record("servers", **query)
        pattern = query.get("name")
        return [
            server
            for server in list(self.cloud.servers.values())
            if pattern is None or re.search(pattern, server.name)
        ]

    def volume_attachments(self, server, **query):
        server_id = getattr(server, "id", server)
        return [
            Obj(volume_id=volume.id, server_id=server_id, device=attachment["device"])
            for volume in self.cloud.volumes.values()
            for attachment in volume.attachments
            if attachment["server_id"] == server_id
        ]

    def delete_server(self, server, ignore_missing=True, force=False):
        server_id = getattr(server, "id", server)
        self.cloud.record("delete_server", server=server_id)
        self.cloud.servers.pop(server_id, None)
        # Like Nova: volumes of a deleted server are detached.
        for volume in self.cloud.volumes.values():
            volume.attachments = [
                a for a in volume.attachments if a["server_id"] != server_id
            ]
            if not volume.attachments:
                volume.status = "available"

    def wait_for_delete(self, res, interval=2, wait=120, callback=None):
        self.cloud.record("wait_for_delete", server=res.id)
        return res

    def wait_for_server(self, server, status="ACTIVE", failures=None, interval=2, wait=120):
        self.cloud.record("wait_for_server", server=server.id)
        return server

    def create_server_image(self, server, name, metadata=None, wait=False, timeout=120):
        self.cloud.record("create_server_image", server=server, name=name, wait=wait)
        image = Obj(
            id=self.cloud.new_id("img"),
            name=name,
            size=3 * GIB + 5,
            min_disk=20,
            status="active",
        )
        self.cloud.images[image.id] = image
        return image


class FakeProcess:
    """Stand-in for the Popen of a remote command (text mode)."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = None
        self._final_rc = returncode
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.pid = 4242
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self._final_rc
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def communicate(self, input=None, timeout=None):
        self.wait()
        return self.stdout.read(), self.stderr.read()


def receive_process(scanned=1000, changed=100, transferred=60, returncode=0, error=None):
    """A finished blocksync receive: progress on stderr, summary on stdout."""
    if error is not None:
        summary = {"ok": False, "error": error}
        stderr = "blocksync: error: %s\n" % error
    else:
        summary = {
            "ok": True,
            "chunks": 4,
            "chunks_changed": 1,
            "bytes_scanned": scanned,
            "bytes_changed": changed,
            "bytes_transferred": transferred,
            "duration_s": 0.25,
        }
        lines = [
            json.dumps(
                {"event": "progress", "bytes_done": done, "bytes_total": scanned,
                 "pct": round(100.0 * done / scanned, 2)}
            )
            for done in (0, scanned // 2, scanned)
        ]
        lines.insert(1, "blocksync: a log line from the sender")
        stderr = "\n".join(lines) + "\n"
    return FakeProcess(returncode, json.dumps(summary) + "\n", stderr)


class FakeShell:
    """RemoteShell replacement bound to a FakeCloud conversion host."""

    def __init__(self, cloud, address, ssh_user, key_path=None, receive=None, val=None):
        self.cloud = cloud
        self.address = address
        self.ssh_user = ssh_user
        self.key_path = key_path
        self.receive = receive
        self.val = val
        self.commands = []
        self.copied = []

    def ssh_preamble(self):
        return ["ssh", "-o", "BatchMode=yes", "%s@%s" % (self.ssh_user, self.address)]

    def test_ssh_connection(self):
        self.commands.append(["echo connected"])

    def cmd_out(self, command, **kwargs):
        self.commands.append(list(command))
        if command and command[0] == "lsblk":
            server_id = self.cloud.addresses[self.address]
            return "\n".join(self.cloud.devices[server_id])
        return ""

    def cmd_val(self, command, **kwargs):
        self.commands.append(list(command))
        if self.val is not None:
            rc = self.val(list(command))
            if rc is not None:
                return rc
        if command[:2] == ["timeout", "1"]:
            return 124  # "timeout 1 nc -l PORT" timed out: the port is free
        return 0

    def cmd_sub(self, command, **kwargs):
        self.commands.append(list(command))
        if self.receive is not None:
            return self.receive(list(command))
        return receive_process()

    def scp_to(self, source, destination):
        with open(source, "r", encoding="utf-8") as f:
            self.copied.append((destination, f.read()))
        return 0


class ShellFactory:
    """``shell_factory`` for OpenStackVolumeBase: one FakeShell per call."""

    def __init__(self, clouds, receive=None, val=None):
        self.clouds = clouds
        self.receive = receive
        self.val = val
        self.shells = []

    def __call__(self, address, ssh_user, key_path=None):
        cloud = next(c for c in self.clouds if address in c.addresses)
        shell = FakeShell(cloud, address, ssh_user, key_path, receive=self.receive, val=self.val)
        self.shells.append(shell)
        return shell

    def at(self, address):
        return [shell for shell in self.shells if shell.address == address]

    def commands_at(self, address):
        return [command for shell in self.at(address) for command in shell.commands]


def server_volume_data(
    volume_id, name, size, device, server_id, bootable=False, volume_type="ceph",
    description=None, availability_zone="nova",
):
    return {
        "type": "openstack.network.ServerVolume",
        "params": {
            "availability_zone": availability_zone,
            "description": description,
            "name": name,
            "volume_type": volume_type,
        },
        "_info": {
            "id": volume_id,
            "size": size,
            "is_bootable": bootable,
            "attachments": [
                {"device": device, "server_id": server_id, "volume_id": volume_id}
            ],
        },
        "_migration_params": {},
    }


def workload_data(
    server_id="srv-1", name="vm1", volumes=(), image_ref=None, boot_disk_copy=True,
    boot_volume_params=None,
):
    params = {"availability_zone": None, "name": None, "description": None,
              "volume_type": None}
    params.update(boot_volume_params or {})
    return {
        "type": "openstack.compute.Server",
        "params": {
            "name": name,
            "image_ref": image_ref,
            "volumes": list(volumes),
            "ports": [],
            "floating_ips": [],
            "security_group_refs": [],
            "flavor_ref": {"name": "m1.small", "project_name": None, "domain_name": None},
        },
        "_info": {"id": server_id, "status": "ACTIVE"},
        # As exported: Server.migration_param_defaults plus boot_disk_copy.
        "_migration_params": {
            "boot_disk_copy": boot_disk_copy,
            "floating_ip_mode": "auto",
            "boot_volume_params": params,
            "port_creation_mode": "nova",
            "data_copy": True,
            "boot_volume": {"uuid": None},
            "additional_volumes": [{"uuid": None}],
            "use_nbdkit_direct": False,
            "nbdkit_socket_uri": None,
            "nbdkit_export_name": None,
        },
    }


def run_module(module, args, conn, shell_factory=None):
    """Run an Ansible module's main() in-process; returns its JSON result.

    The cloud connection is ``conn`` and RemoteShell is ``shell_factory``;
    ``_exit_code`` and ``_connected`` are added to the result.
    """
    import contextlib
    import sys

    from unittest import mock

    from ansible.module_utils import basic
    from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
        os_auth,
        volume_common,
    )

    try:
        from ansible.module_utils.testing import patch_module_args
    except ImportError:  # ansible-core < 2.19

        def patch_module_args(module_args):
            payload = json.dumps({"ANSIBLE_MODULE_ARGS": module_args}).encode()
            return mock.patch.object(basic, "_ANSIBLE_ARGS", payload)

    connect = mock.Mock(return_value=conn)
    stdout = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch_module_args(args))
        stack.enter_context(mock.patch.object(os_auth, "get_connection", connect))
        if shell_factory is not None:
            stack.enter_context(
                mock.patch.object(volume_common, "RemoteShell", shell_factory)
            )
        stack.enter_context(mock.patch.object(sys, "stdout", stdout))
        try:
            module.main()
            code = None
        except SystemExit as exit_info:
            code = exit_info.code
    result = json.loads(stdout.getvalue())
    result["_exit_code"] = code
    result["_connected"] = connect.called
    return result
