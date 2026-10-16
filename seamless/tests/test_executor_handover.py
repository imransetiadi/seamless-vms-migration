import ast
import asyncio
import gc
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Strategy
from seamless_migrate.domain.models import HandoverConfig, Mappings, VMRef, utcnow
from seamless_migrate.executors import handover as handover_module
from seamless_migrate.executors.base import PermanentStepError, StepName, TransientStepError
from seamless_migrate.executors.handover import (
    JOURNAL_FILE,
    HandoverExecutor,
    _cinder_bootable,
    _mark_boot,
    port_description,
)
from tests.cloud_support import InProcessCloud
from tests.executor_support import make_ctx
from tests.factories import make_disk, make_migration, make_plan, make_provider, make_vm

SRC = make_provider(cloud="src")
DST = make_provider(
    id="dst-rhoso",
    name="RHOSO 18.0",
    kind=ProviderKind.rhoso,
    role=ProviderRole.destination,
    cloud="dst",
)


class Crash(Exception):
    pass


class FakeResponse:
    """openstacksdk's raw Proxy calls return the response even for 4xx (raise_exc=False)."""

    def __init__(self, body, status=202):
        self._body = body
        self.status_code = status

    @property
    def text(self):
        return json.dumps(self._body)

    def json(self):
        return self._body


def refused(message):
    return FakeResponse({"badRequest": {"code": 400, "message": message}}, 400)


def _mv(version):
    major, minor = str(version).split(".")
    return int(major), int(minor)


class HTTPError(Exception):
    """What keystoneauth raises for a 4xx answer."""


class NotFoundException(HTTPError):
    """openstacksdk's answer to a missing resource (``openstack.exceptions.NotFoundException``)."""

    status_code = 404


class FakeCompute:
    """Nova as verified upstream: PUT os-volume_attachments changes delete_on_termination only
    from microversion 2.85; deleting a server deletes the volumes attached with
    delete_on_termination=true and detaches the others."""

    def __init__(self, cloud):
        self.c = cloud
        self.max_microversion = "2.88"  # Wallaby (RHOSP 17.1)

    def get_endpoint_data(self):
        return NS(max_microversion=self.max_microversion)

    def _attachment_url(self, url):
        _, servers, sid, kind, vid = url.split("/")
        assert (servers, kind) == ("servers", "os-volume_attachments"), url
        return self.c.servers[sid], vid

    def put(self, url, json=None, microversion=None, **kw):
        server, vid = self._attachment_url(url)
        body = json["volumeAttachment"]
        if "delete_on_termination" in body and (
            microversion is None or _mv(microversion) < (2, 85)
        ):
            return refused("delete_on_termination needs microversion 2.85")
        if body["volumeId"] != vid:
            return refused("that would be a swap")
        if vid not in server.dot:
            return FakeResponse({"itemNotFound": {"message": "volume not attached"}}, 404)
        self.c.record("keep" if body.get("delete_on_termination") is False else "dot", vid)
        server.dot[vid] = body["delete_on_termination"]
        return FakeResponse({"volumeAttachment": {"volumeId": vid}}, 202)

    def get(self, url, microversion=None, **kw):
        server, vid = self._attachment_url(url)
        body = {"volumeId": vid}
        if microversion and _mv(microversion) >= (2, 79):
            body["delete_on_termination"] = server.dot[vid]
        return FakeResponse({"volumeAttachment": body}, 200)

    def get_server(self, server_id):
        self.c.record("get_server", server_id)
        if server_id not in self.c.servers:
            raise NotFoundException(f"No Server found for {server_id}")
        return self.c.servers[server_id]

    def stop_server(self, server):
        self.c.record("stop_server", server.id)
        server.status = "SHUTOFF"

    def start_server(self, server):
        self.c.record("start_server", server.id)

    def wait_for_server(self, server, status="ACTIVE", failures=None, interval=2, wait=120):
        if self.c.crash_wait == "server":
            self.c.crash_wait = None
            raise Crash("wait_for_server")
        return server

    def volume_attachments(self, server):
        return [
            NS(volume_id=v, device=d, delete_on_termination=server.dot.get(v))
            for v, d in server.attachments
        ]

    def delete_volume_attachment(self, server, volume, ignore_missing=True):
        self.c.record("detach", volume)

    def delete_server(self, server, ignore_missing=True):
        sid = getattr(server, "id", server)
        self.c.record("delete_server", sid)
        server = self.c.servers.pop(sid, None)
        for port in [p for p in self.c.ports.values() if p.device_id == sid]:
            if port.created_by_nova:
                self.c.ports.pop(port.id)
            else:
                port.device_id = ""
        for vid, _device in getattr(server, "attachments", []) or []:
            volume = self.c.volumes.get(vid)
            if volume is None:
                continue  # unmanaged: Nova cannot delete what Cinder no longer knows
            if server.dot.get(vid):
                self.c.volumes.pop(vid)
                self.c.deleted_by_nova.append(vid)
            else:
                volume.status = "available"

    def wait_for_delete(self, res, interval=2, wait=120):
        return res

    def get_flavor(self, flavor_id):
        """A flavor by id, as a cloud answering below compute 2.47 makes one look it up."""
        return NS(id=flavor_id, **self.c.flavor_details.get(flavor_id, {}))

    def find_flavor(self, name, ignore_missing=True):
        # by default every flavor exists
        if self.c.flavor_names is not None and name not in self.c.flavor_names:
            if ignore_missing:
                return None
            raise NotFoundException(f"No Flavor found for {name}")
        return NS(id=f"flavor-{name}", name=name)

    def create_server(self, **params):
        self.c.record("create_server", params["name"])
        self.c.created.append(params)
        bdm = params.get("block_device_mapping") or []
        server = NS(
            id=f"{self.c.name}-new-{len(self.c.created)}",
            name=params["name"],
            status="ACTIVE",
            attachments=[(b["uuid"], None) for b in bdm],
            dot={b["uuid"]: b["delete_on_termination"] for b in bdm},
        )
        for b in bdm:
            if b["uuid"] in self.c.volumes:
                self.c.volumes[b["uuid"]].status = "in-use"
        for net in params.get("networks") or []:
            if "port" in net:
                self.c.ports[net["port"]].device_id = server.id
        self.c.servers[server.id] = server
        return server


class FakeBlockStorage:
    def __init__(self, cloud):
        self.c = cloud

    def get_volume(self, volume_id):
        if volume_id not in self.c.volumes:
            raise NotFoundException(f"HTTP 404: volume {volume_id} not found")
        return self.c.volumes[volume_id]

    def snapshots(self, details=True, **query):
        return [
            NS(id=f"snap-{i}", volume_id=query.get("volume_id"))
            for i in range(getattr(self.c.volumes.get(query.get("volume_id")), "snapshots", 0))
        ]

    def wait_for_status(self, res, status="available", failures=None, interval=2, wait=120):
        if self.c.crash_wait == "status":
            self.c.crash_wait = None
            raise Crash("wait_for_status")
        if getattr(res, "status", status) != status:
            raise HTTPError(f"timeout waiting for {res.id} to be {status} (is {res.status})")
        return res

    def wait_for_delete(self, res, interval=2, wait=120):
        if self.c.crash_wait == "delete":
            self.c.crash_wait = None
            raise Crash("wait_for_delete")
        return res

    def types(self):
        return [NS(id=f"type-{name}", name=name) for name in self.c.type_names]

    def backend_pools(self):
        if self.c.pools is None:
            raise RuntimeError("HTTP 403: Policy doesn't allow scheduler_extension:scheduler_stats")
        return [NS(name=name, capabilities=caps) for name, caps in self.c.pools]

    def post(self, url, json=None, **kw):
        if url.endswith("/action") and "os-set_image_metadata" in json:
            vid = url.split("/")[2]
            if self.c.refuse_metadata:
                return refused("Invalid image metadata")
            self.c.record("set_image_metadata", vid)
            self.c.image_metadata[vid] = dict(json["os-set_image_metadata"]["metadata"])
            return FakeResponse({"metadata": json["os-set_image_metadata"]["metadata"]}, 200)
        if url.endswith("/action") and "os-unmanage" in json:
            vid = url.split("/")[2]
            volume = self.get_volume(vid)
            # cinder.volume.api.API.delete(unmanage_only=True), verified on wallaby-eol and master
            if getattr(volume, "encryption_key_id", None):
                return refused("Unmanaging encrypted volumes is not supported.")
            if (
                volume.status not in ("available", "error", "error_restoring", "error_extending")
                or getattr(volume, "snapshots", 0)
                or getattr(volume, "group_id", None)
            ):
                return refused(
                    "Invalid volume: Volume status must be available or error and must not be "
                    "migrating, attached, belong to a group, have snapshots"
                )
            self.c.record("unmanage", vid)
            self.c.volumes.pop(vid)
            return FakeResponse({}, 202)
        if url == "/manageable_volumes":
            vol = json["volume"]
            self.c.record("manage", vol["ref"]["source-name"])
            new_id = f"{self.c.name}-vol-{len(self.c.managed) + 1}"
            self.c.managed.append(vol)
            self.c.volumes[new_id] = NS(
                id=new_id,
                name=vol["name"],
                size=0,
                volume_type=vol.get("volume_type"),
                is_bootable=vol.get("bootable"),
                host=vol["host"],
                status="available",
                encryption_key_id=None,
                snapshots=0,
                group_id=None,
            )
            return FakeResponse({"volume": {"id": new_id}}, 202)
        raise AssertionError(url)


class FakeNetwork:
    """Neutron: deleting a server deletes the ports Nova created for it and only unbinds the ports
    a user created and passed in; creating a port with a MAC and fixed IPs keeps them."""

    def __init__(self, cloud):
        self.c = cloud

    def ports(
        self, device_id=None, description=None, network_id=None, fixed_ips=None, mac_address=None
    ):
        held = [v.split("=", 1)[1] for v in fixed_ips or [] if v.startswith("ip_address=")]
        return [
            p
            for p in self.c.ports.values()
            if (device_id is None or p.device_id == device_id)
            and (description is None or getattr(p, "description", "") == description)
            and (network_id is None or getattr(p, "network_id", None) == network_id)
            and (mac_address is None or getattr(p, "mac_address", None) == mac_address)
            and all(a in [ip.get("ip_address") for ip in p.fixed_ips or []] for a in held)
        ]

    def get_port(self, port_id):
        if port_id not in self.c.ports:
            raise NotFoundException(f"HTTP 404: port {port_id} not found")
        return self.c.ports[port_id]

    def create_port(self, **attrs):
        self.c.record("create_port", attrs.get("mac_address"))
        self.c.port_attrs.append(attrs)
        port = NS(
            id=f"{self.c.name}-port-{len(self.c.port_attrs)}",
            network_id=attrs["network_id"],
            mac_address=attrs.get("mac_address") or "fa:16:3e:ff:ff:ff",
            fixed_ips=list(attrs.get("fixed_ips") or []),
            device_id="",
            created_by_nova=False,
            description=attrs.get("description", ""),
            security_group_ids=list(attrs.get("security_group_ids") or []),
            is_port_security_enabled=attrs.get("is_port_security_enabled", True),
            # Neutron's: the token's project unless an admin names another
            project_id=attrs.get("project_id") or self.c.current_project_id,
        )
        self.c.ports[port.id] = port
        return port

    def delete_port(self, port, ignore_missing=True):
        port_id = getattr(port, "id", port)
        self.c.record("delete_port", port_id)
        self.c.ports.pop(port_id, None)

    def get_security_group(self, group_id):
        return NS(id=group_id, name=self.c.security_groups[group_id])

    def find_security_group(self, name, ignore_missing=True, project_id=None):
        self.c.group_lookups.append((name, project_id))
        return NS(id=f"{self.c.name}-sg-{name}", name=name)

    def security_groups(self, name=None, project_id=None):
        """Neutron's security group list filtered by name and owner; by default every name is one
        group of the credential's own project."""
        listed = self.c.group_list
        if listed is None:
            listed = [
                NS(id=f"{self.c.name}-sg-{name}", name=name, project_id=self.c.current_project_id)
            ]
        return [
            g
            for g in listed
            if (name is None or g.name == name)
            and (project_id is None or g.project_id == project_id)
        ]

    def get_network(self, network_id):
        return NS(id=network_id, name="app-net")

    def networks(self, name=None, project_id=None):
        """Neutron's network list filtered by name and owner; by default every name is one
        network of the credential's own project."""
        listed = self.c.network_list
        if listed is None:
            listed = [
                NS(id=f"{self.c.name}-net-{name}", name=name, project_id=self.c.current_project_id)
            ]
        return [
            n
            for n in listed
            if (name is None or n.name == name)
            and (project_id is None or n.project_id == project_id)
        ]

    def subnets(self, network_id=None):
        """By default a network's subnets hold every address; a test lists its own."""
        if self.c.subnet_list is None:
            return [
                NS(
                    id="any-v4",
                    network_id=network_id,
                    cidr="0.0.0.0/0",
                    ipv6_address_mode=None,
                    ipv6_ra_mode=None,
                ),
                NS(
                    id="any-v6",
                    network_id=network_id,
                    cidr="::/0",
                    ipv6_address_mode=None,
                    ipv6_ra_mode=None,
                ),
            ]
        return [
            sn for sn in self.c.subnet_list if network_id is None or sn.network_id == network_id
        ]


CEPH = {"vendor_name": "Open Source", "storage_protocol": "ceph"}
ONTAP_NFS = {"vendor_name": "NetApp", "storage_protocol": "nfs"}
ONTAP_ISCSI = {"vendor_name": "NetApp", "storage_protocol": "iSCSI"}


class FakeCloud:
    def __init__(self, name, calls, servers=None, volumes=None, crash_on=None, pools=()):
        self.name = name
        self.pools = list(pools) if pools is not None else None
        self.calls = calls
        self.servers = servers or {}
        self.volumes = volumes or {}
        self.crash_on = crash_on
        # "delete" or "status": crash once inside that block-storage wait; "server": inside Nova's
        self.crash_wait = None
        self.created = []
        self.managed = []
        self.ports = {}
        self.port_attrs = []
        self.security_groups = {}
        self.group_lookups = []
        self.group_list = None
        # Nova's flavors (None: every name), and Cinder's types: the source's and the mapped one
        self.flavor_names = None
        # the flavors get_flavor reports by id: ephemeral (GiB), swap (MiB)
        self.flavor_details = {}
        self.type_names = ("ceph-ssd", "ceph-hdd", "rbd-hdd")
        self.current_project_id = f"{name}-project"
        # keystoneauth: the role names of the token (the handover's admin credential)
        self.roles = ["admin", "member", "reader"]
        self.session = NS(auth=NS(get_access=lambda session: NS(role_names=list(self.roles))))
        self.network_list = None
        self.subnet_list = None
        self.image_metadata = {}
        self.deleted_by_nova = []
        self.refuse_metadata = False
        self.compute = FakeCompute(self)
        self.block_storage = FakeBlockStorage(self)
        self.network = FakeNetwork(self)

    def record(self, op, arg):
        if op == "get_server":
            return
        if self.crash_on == op:
            self.crash_on = None
            raise Crash(op)
        self.calls.append((self.name, op, arg))


def source_cloud(calls, crash_on=None):
    server = NS(
        id="srv-1",
        name="web-01",
        status="ACTIVE",
        flavor={"original_name": "m1.small"},
        key_name="kp",
        metadata={"app": "shop"},
        security_groups=[{"name": "default"}],
        availability_zone="nova",
        attachments=[("vol-root", "/dev/vda"), ("vol-data", "/dev/vdb")],
        dot={"vol-root": True, "vol-data": False},
    )
    volumes = {
        "vol-root": NS(
            id="vol-root",
            name="web-01-root",
            size=20,
            volume_type="ceph-ssd",
            is_bootable=True,
            host="overcloud@tripleo_ceph#ssd",
            status="in-use",
            encryption_key_id=None,
            snapshots=0,
            group_id=None,
        ),
        "vol-data": NS(
            id="vol-data",
            name="web-01-data",
            size=100,
            volume_type="ceph-hdd",
            is_bootable=False,
            host="overcloud@tripleo_ceph#hdd",
            status="in-use",
            encryption_key_id=None,
            snapshots=0,
            group_id=None,
        ),
    }
    pools = [("overcloud@tripleo_ceph#ssd", CEPH), ("overcloud@tripleo_ceph#hdd", CEPH)]
    cloud = FakeCloud("src", calls, {"srv-1": server}, volumes, crash_on=crash_on, pools=pools)
    cloud.ports["port-1"] = NS(
        id="port-1",
        network_id="net-1",
        mac_address="fa:16:3e:00:00:01",
        fixed_ips=[
            {"subnet_id": "sub-v4", "ip_address": "10.0.0.5"},
            {"subnet_id": "sub-v6", "ip_address": "fd00::5"},
        ],
        device_id="srv-1",
        created_by_nova=True,
        security_group_ids=["sg-1"],
        is_port_security_enabled=True,
    )
    cloud.security_groups["sg-1"] = "web"
    return cloud


def setup(tmp_path, crash_on=None, dst_crash_on=None):
    calls = []
    dst_pools = [("hostgroup@ceph-ssd#ssd", CEPH), ("hostgroup@ceph-hdd#hdd", CEPH)]
    clouds = {
        "src": source_cloud(calls, crash_on),
        "dst": FakeCloud("dst", calls, pools=dst_pools, crash_on=dst_crash_on),
    }
    settings = Settings(data_dir=tmp_path / "data")
    executor = HandoverExecutor(settings, conn_factory=lambda p: clouds[p.cloud], poll_s=0)
    plan = make_plan(
        id="plan-0000beef",
        mappings=Mappings(
            flavors={"m1.small": "rhoso.small"},
            networks={"app-net": "rhoso-app"},
            volume_types={"ceph-hdd": "rbd-hdd"},
        ),
        handover=HandoverConfig(
            enabled=True,
            backend_map={
                "ceph-ssd": "hostgroup@ceph-ssd#ssd",
                "ceph-hdd": "hostgroup@ceph-hdd#hdd",
            },
        ),
    )
    vm = make_vm(
        source_id="srv-1",
        disks=[
            make_disk(id="vol-root", size_gb=20, volume_type="ceph-ssd", device="/dev/vda"),
            make_disk(
                id="vol-data",
                size_gb=100,
                bootable=False,
                volume_type="ceph-hdd",
                device="/dev/vdb",
            ),
        ],
    )
    mig = make_migration(
        id="mig-00000000hh", plan_id=plan.id, vm=vm, strategy=Strategy.storage_handover
    )
    return executor, calls, clouds, plan, mig, settings


def ops(calls):
    return [(cloud, op, arg) for cloud, op, arg in calls]


def shared_pool(clouds):
    """The two clouds share one Ceph pool, as SDD §7.3 has them: a manage finds the object by its
    source-name and the driver renames it ``volume-<new id>`` (step 6), an unmanage keeps it, and
    Nova refuses a server on a volume its Cinder does not know or another server holds (audit
    EX-17: the plain fakes manage any name)."""
    images = {f"volume-{vid}" for vid in clouds["src"].volumes}
    for cloud in clouds.values():
        post = cloud.block_storage.post

        def manage(url, json=None, _post=post, **kw):
            if url == "/manageable_volumes":
                name = json["volume"]["ref"]["source-name"]
                if name not in images:
                    return refused(f"Manage existing volume failed: image {name} not found")
                response = _post(url, json=json, **kw)
                images.discard(name)
                images.add(f"volume-{response.json()['volume']['id']}")
                return response
            return _post(url, json=json, **kw)

        cloud.block_storage.post = manage
        create = cloud.compute.create_server

        def create_server(_create=create, _cloud=cloud, **params):
            for b in params.get("block_device_mapping") or []:
                if b["uuid"] not in _cloud.volumes:
                    raise HTTPError(f"HTTP 400: Block Device Mapping is Invalid: {b['uuid']}")
                if _cloud.volumes[b["uuid"]].status != "available":
                    raise HTTPError(f"HTTP 400: Invalid volume: {b['uuid']} must be available")
            return _create(**params)

        cloud.compute.create_server = create_server
    return images


def held_manage(cloud, source_name):
    """A slow cinder-api: it accepts the manage of ``source_name``, but its answer waits until the
    test sets the second event (the first is set once the call waits)."""
    held, release = threading.Event(), threading.Event()
    post = cloud.block_storage.post

    def answer(url, json=None, **kw):
        response = post(url, json=json, **kw)
        if url == "/manageable_volumes" and json["volume"]["ref"]["source-name"] == source_name:
            held.set()
            release.wait(30)
        return response

    cloud.block_storage.post = answer
    return held, release


async def test_handover_cutover_order_and_bdm(tmp_path):
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    assert executor.supports(Strategy.storage_handover) and not executor.supports(Strategy.warm)
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    result = await executor.run(StepName.CUTOVER, ctx)
    # SDD §7.3: keep the volumes (delete_on_termination=false, microversion 2.85), delete the
    # server, and only then unmanage: Cinder refuses to unmanage an attached volume
    assert ops(calls) == [
        ("src", "stop_server", "srv-1"),
        ("src", "keep", "vol-data"),
        ("src", "keep", "vol-root"),
        ("src", "delete_server", "srv-1"),
        ("src", "unmanage", "vol-data"),
        ("src", "unmanage", "vol-root"),
        ("dst", "manage", "volume-vol-data"),
        ("dst", "manage", "volume-vol-root"),
        ("dst", "create_server", "web-01"),
    ]
    assert clouds["src"].deleted_by_nova == [], "the boot volume survived its server's deletion"
    assert rec.downtime_marks == 1
    assert result.destination_server_id == "dst-new-1"
    data_manage, root_manage = clouds["dst"].managed
    assert data_manage["host"] == "hostgroup@ceph-hdd#hdd"
    assert data_manage["volume_type"] == "rbd-hdd" and root_manage["volume_type"] == "ceph-ssd"
    assert root_manage["bootable"] is True
    params = clouds["dst"].created[0]
    assert params["flavor_id"] == "flavor-rhoso.small"
    assert params["networks"] == [{"uuid": "dst-net-rhoso-app", "fixed_ip": "10.0.0.5"}]
    bdm = params["block_device_mapping"]
    assert [(b["uuid"], b["boot_index"]) for b in bdm] == [("dst-vol-2", 0), ("dst-vol-1", -1)]
    assert all(b["delete_on_termination"] is False for b in bdm)
    saved = json.loads((executor.run_dir(ctx) / "source-server.json").read_text())
    assert saved["name"] == "web-01" and [a["volume_id"] for a in saved["attachments"]] == [
        "vol-data",
        "vol-root",
    ]
    # the original delete_on_termination is journaled for the rollback
    assert [a["delete_on_termination"] for a in saved["attachments"]] == [False, True]


async def test_handover_boots_from_the_root_device_nova_reports(tmp_path):
    """A legacy guest on the IDE bus boots from /dev/hda, Nova's root_device_name: the recreated
    server boots from that volume (SDD §7.3 step 2), as the inventory decides it."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    server = clouds["src"].servers["srv-1"]
    server.root_device_name = "/dev/hda"
    server.attachments = [("vol-root", "/dev/hda"), ("vol-data", "/dev/hdb")]
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    await executor.run(StepName.CUTOVER, ctx)
    saved = json.loads((executor.run_dir(ctx) / "source-server.json").read_text())
    assert [(a["volume_id"], a["boot"]) for a in saved["attachments"]] == [
        ("vol-data", False),
        ("vol-root", True),
    ]
    bdm = clouds["dst"].created[0]["block_device_mapping"]
    assert [(b["uuid"], b["boot_index"]) for b in bdm] == [("dst-vol-2", 0), ("dst-vol-1", -1)]


async def test_handover_falls_back_to_the_cinder_bootable_volume(tmp_path):
    """Nova shows no root device name and no volume is at /dev/vda: the volume Cinder marks
    bootable is the boot volume, as in the inventory (SDD §7.3 step 2)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].servers["srv-1"].attachments = [
        ("vol-root", "/dev/vdc"),
        ("vol-data", "/dev/vdb"),
    ]
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    await executor.run(StepName.CUTOVER, ctx)
    bdm = clouds["dst"].created[0]["block_device_mapping"]
    assert [(b["uuid"], b["boot_index"]) for b in bdm] == [("dst-vol-2", 0), ("dst-vol-1", -1)]


async def test_handover_falls_back_to_the_first_disk_of_another_bus(tmp_path):
    """No root device name, no Cinder bootable flag (a volume written by hand): a virtio-scsi
    guest's first disk, /dev/sda, is the boot volume (SDD §7.3 step 2)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].servers["srv-1"].attachments = [
        ("vol-root", "/dev/sda"),
        ("vol-data", "/dev/sdb"),
    ]
    clouds["src"].volumes["vol-root"].is_bootable = False
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    await executor.run(StepName.CUTOVER, ctx)
    bdm = clouds["dst"].created[0]["block_device_mapping"]
    assert [(b["uuid"], b["boot_index"]) for b in bdm] == [("dst-vol-2", 0), ("dst-vol-1", -1)]


@pytest.mark.parametrize(
    ("root", "devices", "flags", "boot"),
    [
        ("/dev/hda", ["/dev/hdb", "/dev/hda"], [False, False], "/dev/hda"),  # Nova's root device
        ("/dev/vda", ["/dev/vdc", "/dev/vdb"], ["true", "true"], "/dev/vdb"),  # lowest bootable
        ("/dev/vda", ["/dev/vdb", "/dev/vdc"], ["false", "TRUE"], "/dev/vdc"),  # flags as text
        ("/dev/vda", ["/dev/hdb", "/dev/xvda", "/dev/hda"], [False] * 3, "/dev/hda"),  # bus order
        ("/dev/vda", ["/dev/vdb", "/dev/vdc"], [False, None], None),  # nothing to boot from
    ],
)
def test_mark_boot_follows_the_rules_of_sdd_7_3(root, devices, flags, boot):
    attachments = [
        {"device": d, "bootable": _cinder_bootable(NS(is_bootable=f))}
        for d, f in zip(devices, flags, strict=True)
    ]
    _mark_boot(attachments, root)
    assert [a["device"] for a in attachments if a["boot"]] == ([boot] if boot else [])


async def test_handover_refuses_before_stop_when_no_volume_is_the_boot_volume(tmp_path):
    """Without a boot volume the destination server cannot be created, and neither can the source
    again on rollback: refuse while the VM still runs (SDD §7.3 step 0)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].servers["srv-1"].attachments = [
        ("vol-root", "/dev/vdc"),
        ("vol-data", "/dev/vdb"),
    ]
    clouds["src"].volumes["vol-root"].is_bootable = False
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="which volume the server boots from"):
        await executor.run(StepName.CUTOVER, ctx)
    assert calls == [] and rec.downtime_marks == 0


async def test_handover_journal_resume_skips_done_steps(tmp_path):
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="delete_server")
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="delete the source server"):
        await executor.run(StepName.CUTOVER, ctx)
    journal = json.loads((executor.run_dir(ctx) / JOURNAL_FILE).read_text())
    assert {"stop_source", "save_definition", "keep:vol-data", "keep:vol-root"} <= set(
        journal["done"]
    )
    assert "delete_source" not in journal["done"]
    first = list(calls)
    calls.clear()

    # control plane restarts; a fresh executor resumes from the journal
    executor2 = HandoverExecutor(settings, conn_factory=lambda p: clouds[p.cloud], poll_s=0)
    ctx2, rec2 = make_ctx(plan, mig, SRC, DST, settings)
    result = await executor2.run(StepName.CUTOVER, ctx2)
    assert ops(calls) == [
        ("src", "delete_server", "srv-1"),
        ("src", "unmanage", "vol-data"),
        ("src", "unmanage", "vol-root"),
        ("dst", "manage", "volume-vol-data"),
        ("dst", "manage", "volume-vol-root"),
        ("dst", "create_server", "web-01"),
    ]
    assert ("src", "stop_server", "srv-1") in first
    assert rec2.downtime_marks == 1, "downtime start is re-asserted (idempotent upstream)"
    assert result.destination_server_id
    # a third run is a no-op that returns the same server
    calls.clear()
    again = await executor2.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert calls == [] and again.destination_server_id == result.destination_server_id


async def test_a_cancelled_handover_step_ends_only_after_its_call_returned(tmp_path):
    """SDD §7.3: a handover call runs in a worker thread, which no cancel stops. A cancelled step -
    cancelled twice, as a step timeout and then a rollback request do - ends only once its call
    returned and journaled the manage it sent, so the rollback that follows unmanages that volume
    at RHOSO and manages the renamed object back (audit EX-01)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    shared_pool(clouds)
    held, release = held_manage(clouds["dst"], "volume-vol-root")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    step = asyncio.create_task(executor.run(StepName.CUTOVER, ctx))
    try:
        assert await asyncio.to_thread(held.wait, 10)
        step.cancel()
        await asyncio.sleep(0.2)
        step.cancel()
        await asyncio.sleep(0.2)
        assert not step.done(), "the step ended while its manage still ran"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await step
    journal = json.loads((executor.run_dir(ctx) / JOURNAL_FILE).read_text())
    assert journal["done"]["sent:manage_dst:vol-root"]["volume_id"] == "dst-vol-2"
    calls.clear()
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert ops(calls) == [
        ("dst", "unmanage", "dst-vol-2"),
        ("dst", "unmanage", "dst-vol-1"),
        ("src", "manage", "volume-dst-vol-2"),
        ("src", "manage", "volume-dst-vol-1"),
        ("src", "create_port", "fa:16:3e:00:00:01"),
        ("src", "create_server", "web-01"),
    ]
    assert result.details["vm"]["source_id"] == "src-new-1"


async def test_a_handover_step_cancelled_while_it_journals_ends_after_the_write(
    tmp_path, monkeypatch
):
    """SDD §7.3: a journal write is a call in flight too. A step cancelled while it journals the
    manage that completed ends only once the write landed (audit EX-01)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    held, release = threading.Event(), threading.Event()
    write = handover_module._write_json

    def slow_write(path, data):
        if "manage_dst:vol-root" in data.get("done", {}) and not held.is_set():
            held.set()
            release.wait(30)
        write(path, data)

    monkeypatch.setattr(handover_module, "_write_json", slow_write)
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    step = asyncio.create_task(executor.run(StepName.CUTOVER, ctx))
    try:
        assert await asyncio.to_thread(held.wait, 10)
        step.cancel()
        await asyncio.sleep(0.2)
        assert not step.done(), "the step ended while its journal write still ran"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await step
    journal = json.loads((executor.run_dir(ctx) / JOURNAL_FILE).read_text())
    assert journal["done"]["manage_dst:vol-root"]["dest_id"] == "dst-vol-2"


async def test_a_cancelled_handover_calls_own_failure_never_reaches_the_log_unredacted(tmp_path):
    """SDD §13: the error of a call that a cancelled step waited for is the step's to drop, not
    asyncio's to log raw as "Task exception was never retrieved" (audit EX-01)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    held, release = threading.Event(), threading.Event()
    post = clouds["dst"].block_storage.post

    def failing(url, json=None, **kw):
        if url == "/manageable_volumes":
            held.set()
            release.wait(30)
            raise HTTPError("HTTP 503 Service Unavailable")
        return post(url, json=json, **kw)

    clouds["dst"].block_storage.post = failing
    loop = asyncio.get_running_loop()
    reported = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context["message"]))
    try:
        step = asyncio.create_task(
            executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
        )
        assert await asyncio.to_thread(held.wait, 10)
        step.cancel()
        await asyncio.sleep(0.1)
        release.set()
        try:
            await step
        except asyncio.CancelledError:
            pass
        del step
        for _ in range(3):  # the loop lets go of the finished step before it can be collected
            await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        release.set()
        loop.set_exception_handler(None)
    assert reported == []


def test_every_handover_call_in_a_thread_outlives_a_cancel():
    """SDD §7.3: the handover hands no call to a thread that a cancel would leave running
    unawaited - each goes through _in_thread, so a new one cannot bring back audit EX-01."""
    tree = ast.parse(Path(handover_module.__file__).read_text())
    helper = next(n for n in tree.body if getattr(n, "name", None) == "_in_thread")
    inside = {id(n) for n in ast.walk(helper)}
    direct = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and n.attr in {"to_thread", "run_in_executor"}
        and id(n) not in inside
    ]
    assert direct == []


class Credentialed:
    """A cloud reached with one credential: once it is revoked, every call is refused, as Keystone
    refuses to authenticate a rotated password again."""

    def __init__(self, cloud, secret, revoked):
        self.cloud, self.secret, self.revoked, self.closed = cloud, secret, revoked, False

    def close(self):
        self.closed = True

    def __getattr__(self, name):
        target = getattr(self.cloud, name)
        if name not in ("compute", "block_storage", "network"):
            return target
        session = self

        class Service:
            def __getattr__(self, attr):
                call = getattr(target, attr)

                def guarded(*args, **kwargs):
                    if session.secret in session.revoked:
                        raise HTTPError("HTTP 401 Unauthorized: authentication required")
                    return call(*args, **kwargs)

                return guarded

        return Service()


async def test_a_handover_reconnects_once_its_providers_credentials_were_rotated(tmp_path):
    """SDD §10, §13.3: credentials are rotated while a plan runs (PUT .../credentials, the old
    password revoked). The handover's next step connects with the new ones, as the provider
    registry does, and closes the old connection - so the rollback of a cutover that failed after
    it deleted the source still rebuilds it. A change of state alone reconnects nothing (audit
    EX-14)."""
    _, calls, clouds, plan, mig, settings = setup(tmp_path, dst_crash_on="create_server")
    revoked: set[str] = set()
    built: list[Credentialed] = []

    def connect(provider):
        built.append(Credentialed(clouds[provider.cloud], provider.credentials_secret, revoked))
        return built[-1]

    executor = HandoverExecutor(settings, conn_factory=connect, poll_s=0)
    old_src, old_dst = (
        SRC.model_copy(update={"credentials_secret": "v1"}),
        DST.model_copy(update={"credentials_secret": "v1"}),
    )
    with pytest.raises(PermanentStepError, match="create the server"):
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, old_src, old_dst, settings)[0])
    assert ("src", "delete_server", "srv-1") in calls and len(built) == 2
    checked = {"status": "ok", "last_checked_at": utcnow()}
    await executor._conn(old_src.model_copy(update=checked))
    assert len(built) == 2, "a provider's state is not how it is reached"
    revoked.add("v1")
    rotated = {"credentials_secret": "v2", "credentials_updated_at": utcnow()}
    src, dst = old_src.model_copy(update=rotated), old_dst.model_copy(update=rotated)
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, src, dst, settings)[0])
    assert result.details["vm"]["source_id"] == "src-new-1"
    assert [c.secret for c in built] == ["v1", "v1", "v2", "v2"]
    assert [c.closed for c in built] == [True, True, False, False]


async def test_two_handover_steps_reaching_a_provider_at_once_share_one_connection(tmp_path):
    """Two migrations' steps reach the same provider at once: one connection is built and both use
    it - none is built twice and left open (audit EX-14)."""
    built = []

    def connect(provider):
        time.sleep(0.1)  # keystoneauth signs in
        built.append(provider.id)
        return NS(close=lambda: None)

    executor = HandoverExecutor(Settings(data_dir=tmp_path), conn_factory=connect, poll_s=0)
    first, second = await asyncio.gather(executor._conn(SRC), executor._conn(SRC))
    assert first is second and built == [SRC.id]


async def test_handover_rollback_reverses_order(tmp_path):
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    cut = await executor.run(StepName.CUTOVER, ctx)
    calls.clear()
    mig.destination_server_id = cut.destination_server_id
    rb_ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    result = await executor.run(StepName.ROLLBACK, rb_ctx)
    assert ops(calls) == [
        ("dst", "delete_server", "dst-new-1"),
        ("dst", "unmanage", "dst-vol-2"),  # boot volume was managed last -> unmanaged first
        ("dst", "unmanage", "dst-vol-1"),
        ("src", "manage", "volume-dst-vol-2"),
        ("src", "manage", "volume-dst-vol-1"),
        ("src", "create_port", "fa:16:3e:00:00:01"),  # Nova deleted the port it created
        ("src", "create_server", "web-01"),
    ]
    src_manage = clouds["src"].managed
    assert [m["host"] for m in src_manage] == [
        "overcloud@tripleo_ceph#ssd",
        "overcloud@tripleo_ceph#hdd",
    ]
    params = clouds["src"].created[0]
    assert [
        (b["boot_index"], b["delete_on_termination"]) for b in params["block_device_mapping"]
    ] == [(0, True), (-1, False)]
    # the recreated source VM has new ids; the executor reports them to the orchestrator
    assert result.details["source_running"] is True
    new_vm = result.details["vm"]
    assert new_vm["source_id"] == "src-new-1"
    assert {d["id"] for d in new_vm["disks"]} == {"src-vol-1", "src-vol-2"}
    assert not (executor.run_dir(rb_ctx) / JOURNAL_FILE).exists(), "journal archived"


async def test_handover_rollback_with_empty_journal_is_a_noop(tmp_path):
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="stop_server")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="stop source server"):
        await executor.run(StepName.CUTOVER, ctx)
    calls.clear()
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert calls == [] and result.details == {
        "source_running": True,
        "note": "nothing to roll back",
    }


async def test_handover_rollback_starts_a_source_whose_stop_was_sent(tmp_path):
    """SDD §7.3 step 1, review RT-10: Nova accepted the stop, then the wait for SHUTOFF failed;
    the stop is journaled as sent, so the rollback starts the source again instead of reading an
    empty journal as nothing to roll back."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].crash_wait = "server"  # the wait for SHUTOFF dies after the stop was sent
    with pytest.raises(PermanentStepError, match="stop source server"):
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert clouds["src"].servers["srv-1"].status == "SHUTOFF"
    calls.clear()
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert ops(calls) == [("src", "start_server", "srv-1")]
    assert result.details == {"source_running": True}
    # a retry instead of the rollback: the stop step runs again (it is not done until the wait
    # succeeds), sends no second stop to the stopped source, and the handover completes
    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "retry")
    clouds["src"].crash_wait = "server"
    with pytest.raises(PermanentStepError, match="stop source server"):
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    retried = await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert retried.destination_server_id
    assert [op for op in ops(calls) if op[1] == "stop_server"] == [("src", "stop_server", "srv-1")]
    # a source already stopped before the handover: no stop was sent, nothing to start again
    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "stopped")
    clouds["src"].servers["srv-1"].status = "SHUTOFF"
    clouds["src"].crash_wait = "server"
    with pytest.raises(PermanentStepError, match="stop source server"):
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    calls.clear()
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert ops(calls) == [] and result.details["note"] == "nothing to roll back"


async def test_a_handover_stop_never_confirmed_opens_the_clock_at_its_request(tmp_path):
    """SDD §7.3 step 1: Nova accepted the stop, then the wait for SHUTOFF failed. The source may be
    stopped, so the clock opens at the time the stop was journaled as sent - the automatic
    rollback (§8) and the cancel refusal (§5.1) need it - and the step still fails (audit
    EX-02). A stop call that itself fails sent nothing and opens no clock."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].crash_wait = "server"
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="stop source server"):
        await executor.run(StepName.CUTOVER, ctx)
    journal = json.loads((executor.run_dir(ctx) / JOURNAL_FILE).read_text())
    assert rec.downtime_at == [datetime.fromisoformat(journal["done"]["sent:stop_source"]["at"])]
    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "sent", crash_on="stop_server")
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="stop source server"):
        await executor.run(StepName.CUTOVER, ctx)
    assert rec.downtime_at == []


async def test_a_handover_cancelled_while_its_stop_waits_opens_the_clock_at_its_request(tmp_path):
    """SDD §7.3 step 1: a step cancelled while the stop it sent waits for SHUTOFF (the step timeout,
    a rollback request) opens the clock at the stop's request before it ends - also when a second
    cancel comes while it does (audit EX-02)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    held, release = threading.Event(), threading.Event()
    wait = clouds["src"].compute.wait_for_server

    def slow_wait(server, status="ACTIVE", **kw):
        if status == "SHUTOFF":
            held.set()
            release.wait(30)
        return wait(server, status=status, **kw)

    clouds["src"].compute.wait_for_server = slow_wait
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    record = rec.mark_downtime_start

    async def slow_mark(at=None):
        await asyncio.sleep(0.3)  # the orchestrator waits for the migration's lock, then saves
        await record(at)

    ctx.mark_downtime_start = slow_mark
    step = asyncio.create_task(executor.run(StepName.CUTOVER, ctx))
    try:
        assert await asyncio.to_thread(held.wait, 10)
        step.cancel()
    finally:
        release.set()
    await asyncio.sleep(0.1)
    step.cancel()  # while the clock is being opened
    with pytest.raises(asyncio.CancelledError):
        await step
    journal = json.loads((executor.run_dir(ctx) / JOURNAL_FILE).read_text())
    assert rec.downtime_at == [datetime.fromisoformat(journal["done"]["sent:stop_source"]["at"])]


async def test_a_handover_cancelled_while_it_opens_the_clock_still_opens_it(tmp_path):
    """SDD §7.3 step 1: the stop was confirmed and the clock is being opened when the step is
    cancelled - the clock still opens before the step ends (audit EX-02)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    entered = asyncio.Event()
    record = rec.mark_downtime_start

    async def slow_mark(at=None):
        entered.set()
        await asyncio.sleep(0.3)  # the orchestrator waits for the migration's lock, then saves
        await record(at)

    ctx.mark_downtime_start = slow_mark
    step = asyncio.create_task(executor.run(StepName.CUTOVER, ctx))
    await asyncio.wait_for(entered.wait(), 10)
    step.cancel()
    with pytest.raises(asyncio.CancelledError):
        await step
    assert rec.downtime_marks == 1


async def test_handover_rollback_before_the_source_is_deleted_restores_and_starts(tmp_path):
    """Crash before the server was deleted: the volumes are still attached and managed; the
    rollback sets the journaled delete_on_termination back and starts the source."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="delete_server")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="delete the source server"):
        await executor.run(StepName.CUTOVER, ctx)
    assert clouds["src"].servers["srv-1"].dot == {"vol-root": False, "vol-data": False}
    calls.clear()
    rb_ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    result = await executor.run(StepName.ROLLBACK, rb_ctx)
    assert ops(calls) == [("src", "dot", "vol-root"), ("src", "start_server", "srv-1")]
    assert clouds["src"].servers["srv-1"].dot == {"vol-root": True, "vol-data": False}
    assert result.details == {"source_running": True}, "same ids: nothing to report"
    assert not (executor.run_dir(rb_ctx) / JOURNAL_FILE).exists()


async def test_handover_rollback_after_the_source_is_deleted_recreates_it(tmp_path):
    """Crash at the first unmanage: the server is gone, both volumes still managed at the source;
    the rollback recreates the server on the same volumes with their delete_on_termination."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="unmanage")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="unmanage"):
        await executor.run(StepName.CUTOVER, ctx)
    assert "srv-1" not in clouds["src"].servers and clouds["src"].deleted_by_nova == []
    calls.clear()
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert ops(calls) == [
        ("src", "create_port", "fa:16:3e:00:00:01"),  # Nova deleted the port it created
        ("src", "create_server", "web-01"),
    ]
    bdm = clouds["src"].created[0]["block_device_mapping"]
    assert [(b["uuid"], b["boot_index"], b["delete_on_termination"]) for b in bdm] == [
        ("vol-root", 0, True),
        ("vol-data", -1, False),
    ]
    assert result.details["vm"]["source_id"] == "src-new-1"


async def test_handover_journals_every_port_with_its_mac_and_addresses(tmp_path):
    """SDD §7.3 step 2: every port with its id, MAC and every fixed IP (IPv4 and IPv6)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    await executor.run(StepName.CUTOVER, ctx)
    saved = json.loads((executor.run_dir(ctx) / "source-server.json").read_text())
    [port] = saved["networks"]
    assert (port["port_id"], port["mac_address"], port["network"]) == (
        "port-1",
        "fa:16:3e:00:00:01",
        "app-net",
    )
    assert [ip["ip_address"] for ip in port["fixed_ips"]] == ["10.0.0.5", "fd00::5"]


async def test_handover_rollback_recreates_nova_ports_with_their_mac_and_addresses(tmp_path):
    """The source server is gone and Nova deleted the port it had created: the rollback recreates
    the port with its MAC and both addresses and boots the source on it (SDD §7.3 rollback)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="unmanage")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="unmanage"):
        await executor.run(StepName.CUTOVER, ctx)
    assert "port-1" not in clouds["src"].ports
    await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    [port] = clouds["src"].ports.values()
    assert port.mac_address == "fa:16:3e:00:00:01"
    assert [ip["ip_address"] for ip in port.fixed_ips] == ["10.0.0.5", "fd00::5"]
    assert clouds["src"].created[0]["networks"] == [{"port": port.id}]
    assert port.device_id == "src-new-1"


async def test_handover_rollback_reuses_a_port_the_user_created(tmp_path):
    """A port created by a user survives the server's deletion, unbound: the rollback boots the
    source on that port again instead of creating another."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="unmanage")
    clouds["src"].ports["port-1"].created_by_nova = False
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="unmanage"):
        await executor.run(StepName.CUTOVER, ctx)
    calls.clear()
    await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert ("src", "create_port", "fa:16:3e:00:00:01") not in ops(calls)
    assert clouds["src"].created[0]["networks"] == [{"port": "port-1"}]
    assert clouds["src"].ports["port-1"].device_id == "src-new-1"


async def test_handover_rollback_resume_does_not_recreate_a_port_twice(tmp_path):
    """The rollback crashed after recreating the port: a resume boots on that port instead of
    asking Neutron for a second one with the same MAC, which it would refuse."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="unmanage")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="unmanage"):
        await executor.run(StepName.CUTOVER, ctx)
    clouds["src"].crash_on = "create_server"
    with pytest.raises(PermanentStepError, match="recreate the source server"):
        await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert [op for op in ops(calls) if op[1] == "create_port"] == [
        ("src", "create_port", "fa:16:3e:00:00:01")
    ]
    [port] = clouds["src"].ports.values()
    assert clouds["src"].created[-1]["networks"] == [{"port": port.id}]


async def test_handover_keeps_a_port_without_addresses(tmp_path):
    """A port with no fixed IP is journaled too, and the destination gets a NIC on the mapped
    network without asking for an address."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].ports["port-1"].fixed_ips = []
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    await executor.run(StepName.CUTOVER, ctx)
    saved = json.loads((executor.run_dir(ctx) / "source-server.json").read_text())
    assert [(n["port_id"], n["fixed_ips"]) for n in saved["networks"]] == [("port-1", [])]
    assert clouds["dst"].created[0]["networks"] == [{"uuid": "dst-net-rhoso-app"}]


def neutron(plan):
    """The plan keeping each port's MAC, every fixed IP and its security groups (SDD §4.2)."""
    return plan.model_copy(update={"port_creation_mode": "neutron"})


async def test_handover_in_neutron_mode_creates_each_port_with_its_mac_addresses_and_groups(
    tmp_path,
):
    """SDD §7.3 steps 2 and 8: the definition records the port's security groups by name and its
    port-security flag; the destination port is created first, through Neutron, with the MAC,
    both addresses, the groups found in the destination project and the §6.6 description, and
    Nova boots the server on it."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    ctx, _ = make_ctx(neutron(plan), mig, SRC, DST, settings)
    result = await executor.run(StepName.CUTOVER, ctx)
    saved = json.loads((executor.run_dir(ctx) / "source-server.json").read_text())
    [port] = saved["networks"]
    assert (port["security_groups"], port["port_security_enabled"]) == (["web"], True)
    assert clouds["dst"].port_attrs == [
        {
            "network_id": "dst-net-rhoso-app",
            "mac_address": "fa:16:3e:00:00:01",
            "fixed_ips": [{"ip_address": "10.0.0.5"}, {"ip_address": "fd00::5"}],
            "security_group_ids": ["dst-sg-web"],
            "is_port_security_enabled": True,
            "description": "os-migrate:port-1",
        }
    ]
    assert clouds["dst"].group_lookups == [("web", "dst-project")]
    [dst_port] = clouds["dst"].ports.values()
    assert clouds["dst"].created[0]["networks"] == [{"port": dst_port.id}]
    assert dst_port.device_id == result.destination_server_id
    steps = [op for op in ops(calls) if op[0] == "dst"]
    assert steps[-2:] == [
        ("dst", "create_port", "fa:16:3e:00:00:01"),
        ("dst", "create_server", "web-01"),
    ]


async def test_handover_in_neutron_mode_leaves_out_the_addresses_neutron_derives(tmp_path):
    """SDD §7.3 step 8, §6.6 (review NC-01, NC-09): Neutron 2023.1 allocates a SLAAC subnet's
    EUI-64 address of the kept MAC on every port it creates, and a sent one is allocated twice;
    step 8 never sends it, and a resume still knows the port Neutron gave such an address."""
    eui = "fd00::f816:3eff:fe00:1"
    net = "dst-net-rhoso-app"
    slaac_network = [subnet(net, "10.0.0.0/24"), subnet(net, "fd00::/64", "slaac")]
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].ports["port-1"].fixed_ips = [
        {"subnet_id": "sub-v4", "ip_address": "10.0.0.5"},
        {"subnet_id": "sub-v6", "ip_address": eui},
    ]
    clouds["dst"].subnet_list = slaac_network
    await executor.run(StepName.CUTOVER, make_ctx(neutron(plan), mig, SRC, DST, settings)[0])
    [attrs] = clouds["dst"].port_attrs
    assert attrs["fixed_ips"] == [{"ip_address": "10.0.0.5"}]

    # a port created before a crash, to which Neutron added the EUI-64 address of a SLAAC
    # subnet the source port had none on: still the one step 8 boots on
    other, calls2, clouds2, plan2, mig2, settings2 = setup(tmp_path / "resume")
    clouds2["src"].ports["port-1"].fixed_ips = [{"subnet_id": "sub-v4", "ip_address": "10.0.0.5"}]
    clouds2["dst"].subnet_list = slaac_network
    clouds2["dst"].ports["earlier"] = NS(
        id="earlier",
        network_id=net,
        mac_address="fa:16:3e:00:00:01",
        fixed_ips=[{"ip_address": "10.0.0.5"}, {"ip_address": eui}],
        device_id="",
        created_by_nova=False,
        description="os-migrate:port-1",
        project_id="dst-project",
    )
    await other.run(StepName.CUTOVER, make_ctx(neutron(plan2), mig2, SRC, DST, settings2)[0])
    assert clouds2["dst"].port_attrs == []
    assert clouds2["dst"].created[0]["networks"] == [{"port": "earlier"}]


async def test_handover_rollback_leaves_out_the_slaac_address_of_a_source_port(tmp_path):
    """SDD §7.3 rollback: the source port is recreated with its MAC and fixed IPs but the one on a
    SLAAC subnet, which the source's Neutron derives from the MAC itself - sent, it is refused or
    allocated twice, with the source server already deleted."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="unmanage")
    clouds["src"].ports["port-1"].fixed_ips = [
        {"subnet_id": "sub-v4", "ip_address": "10.0.0.5"},
        {"subnet_id": "sub-v6", "ip_address": "fd00::f816:3eff:fe00:1"},
    ]
    clouds["src"].subnet_list = [
        NS(
            id="sub-v4",
            network_id="net-1",
            cidr="10.0.0.0/24",
            ipv6_address_mode=None,
            ipv6_ra_mode=None,
        ),
        NS(
            id="sub-v6",
            network_id="net-1",
            cidr="fd00::/64",
            ipv6_address_mode="slaac",
            ipv6_ra_mode="slaac",
        ),
    ]
    with pytest.raises(PermanentStepError, match="unmanage"):
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    [attrs] = clouds["src"].port_attrs
    assert attrs["fixed_ips"] == [{"subnet_id": "sub-v4", "ip_address": "10.0.0.5"}]
    assert attrs["mac_address"] == "fa:16:3e:00:00:01"


async def test_handover_in_neutron_mode_resumes_on_the_port_it_created(tmp_path):
    """A crash after the port and before the server: the resume boots on the journaled port, and
    a port created but not yet journaled is found by its description, unbound."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, dst_crash_on="create_server")
    ctx, _ = make_ctx(neutron(plan), mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="create the server in RHOSO 18.0"):
        await executor.run(StepName.CUTOVER, ctx)
    await executor.run(StepName.CUTOVER, make_ctx(neutron(plan), mig, SRC, DST, settings)[0])
    assert [op for op in ops(calls) if op[1] == "create_port"] == [
        ("dst", "create_port", "fa:16:3e:00:00:01")
    ]
    [dst_port] = clouds["dst"].ports.values()
    assert clouds["dst"].created[-1]["networks"] == [{"port": dst_port.id}]

    other, calls2, clouds2, plan2, mig2, settings2 = setup(tmp_path / "unjournaled")
    clouds2["dst"].ports["earlier"] = NS(
        id="earlier",
        network_id="dst-net-rhoso-app",
        mac_address="fa:16:3e:00:00:01",
        fixed_ips=[{"ip_address": "10.0.0.5"}, {"ip_address": "fd00::5"}],
        device_id="",
        created_by_nova=False,
        description="os-migrate:port-1",
        project_id="dst-project",
    )
    await other.run(StepName.CUTOVER, make_ctx(neutron(plan2), mig2, SRC, DST, settings2)[0])
    assert clouds2["dst"].port_attrs == []
    assert clouds2["dst"].created[0]["networks"] == [{"port": "earlier"}]


async def test_handover_in_neutron_mode_never_boots_on_a_port_that_is_not_its_own(tmp_path):
    """SDD §7.3 step 8: the admin credential sees every project's ports, and an earlier cold or
    warm migration of the same VM may have left one with the description: only an unbound port
    on the target network, in the credential's project, with the MAC and exactly the addresses
    is used."""
    leftovers = {
        "other-net": {"network_id": "dst-net-elsewhere"},
        "other-mac": {"mac_address": "fa:16:3e:99:99:99"},
        "other-ips": {"fixed_ips": [{"ip_address": "10.0.0.77"}]},
        "other-project": {"project_id": "someone-else"},
        "bound": {"device_id": "another-server"},
    }
    for name, change in leftovers.items():
        executor, calls, clouds, plan, mig, settings = setup(tmp_path / name)
        port = {
            "id": name,
            "network_id": "dst-net-rhoso-app",
            "mac_address": "fa:16:3e:00:00:01",
            "fixed_ips": [{"ip_address": "10.0.0.5"}, {"ip_address": "fd00::5"}],
            "device_id": "",
            "created_by_nova": False,
            "description": "os-migrate:port-1",
            "project_id": "dst-project",
        }
        clouds["dst"].ports[name] = NS(**{**port, **change})
        ctx = make_ctx(neutron(plan), mig, SRC, DST, settings)[0]
        if name == "other-net":
            # on another network it holds nothing of ours: step 8 creates the port
            await executor.run(StepName.CUTOVER, ctx)
            [created] = clouds["dst"].port_attrs
            assert created["network_id"] == "dst-net-rhoso-app", name
            assert clouds["dst"].created[0]["networks"] != [{"port": name}], name
            continue
        # it holds an address or the MAC on the target network: Neutron would refuse step 8's
        # port, so step 0 refuses before anything stops - and nothing boots on it
        with pytest.raises(PermanentStepError, match=f"is held by port {name}"):
            await executor.run(StepName.CUTOVER, ctx)
        assert [op for op in ops(calls) if op[1] == "stop_server"] == [], name
        assert clouds["dst"].created == [], name


async def test_handover_rollback_recreates_source_ports_with_their_security_groups(tmp_path):
    """SDD §7.3 step 2 and rollback: every port's security group ids and port-security flag are
    journaled, and a recreated source port gets them back - Nova applies a server's groups only
    to ports it creates, so it would land in the project's default group."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="unmanage")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="unmanage"):
        await executor.run(StepName.CUTOVER, ctx)
    saved = json.loads((executor.run_dir(ctx) / "source-server.json").read_text())
    assert (
        saved["networks"][0]["security_group_ids"],
        saved["networks"][0]["port_security_enabled"],
    ) == (
        ["sg-1"],
        True,
    )
    await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    [attrs] = clouds["src"].port_attrs
    assert (attrs["security_group_ids"], attrs["is_port_security_enabled"]) == (["sg-1"], True)


async def test_handover_rollback_of_a_definition_without_port_groups_recreates_the_port(tmp_path):
    """A run journaled before §7.3 step 2 recorded the port's groups: its rollback recreates the
    port with its MAC and addresses and leaves the groups to Neutron, as before."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="unmanage")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="unmanage"):
        await executor.run(StepName.CUTOVER, ctx)
    definition = executor.run_dir(ctx) / "source-server.json"
    legacy = json.loads(definition.read_text())
    for n in legacy["networks"]:
        n.pop("security_group_ids")
        n.pop("port_security_enabled")
    definition.write_text(json.dumps(legacy))
    await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    [attrs] = clouds["src"].port_attrs
    assert attrs == {
        "network_id": "net-1",
        "mac_address": "fa:16:3e:00:00:01",
        "fixed_ips": [
            {"subnet_id": "sub-v4", "ip_address": "10.0.0.5"},
            {"subnet_id": "sub-v6", "ip_address": "fd00::5"},
        ],
    }


async def test_handover_rollback_in_neutron_mode_deletes_the_destination_ports(tmp_path):
    """SDD §7.3 rollback: Nova keeps the ports it did not create; the rollback deletes them after
    the destination server - also when the cutover failed before the server existed."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    ctx, _ = make_ctx(neutron(plan), mig, SRC, DST, settings)
    cut = await executor.run(StepName.CUTOVER, ctx)
    [dst_port] = clouds["dst"].ports
    calls.clear()
    mig.destination_server_id = cut.destination_server_id
    await executor.run(StepName.ROLLBACK, make_ctx(neutron(plan), mig, SRC, DST, settings)[0])
    assert ops(calls)[:3] == [
        ("dst", "delete_server", "dst-new-1"),
        ("dst", "delete_port", dst_port),
        ("dst", "unmanage", "dst-vol-2"),
    ]
    assert clouds["dst"].ports == {}

    early, _, clouds2, plan2, mig2, settings2 = setup(
        tmp_path / "early", dst_crash_on="create_server"
    )
    with pytest.raises(PermanentStepError):
        await early.run(StepName.CUTOVER, make_ctx(neutron(plan2), mig2, SRC, DST, settings2)[0])
    assert len(clouds2["dst"].ports) == 1
    await early.run(StepName.ROLLBACK, make_ctx(neutron(plan2), mig2, SRC, DST, settings2)[0])
    assert clouds2["dst"].ports == {}


async def test_handover_in_neutron_mode_keeps_port_security_off_and_a_port_without_address(
    tmp_path,
):
    """A second port with port security off, no group and no address: created as it was, with
    an explicit empty address list (Neutron allocates none) and port security off."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].ports["port-2"] = NS(
        id="port-2",
        network_id="net-1",
        mac_address="fa:16:3e:00:00:02",
        fixed_ips=[],
        device_id="srv-1",
        created_by_nova=True,
        security_group_ids=[],
        is_port_security_enabled=False,
    )
    await executor.run(StepName.CUTOVER, make_ctx(neutron(plan), mig, SRC, DST, settings)[0])
    second = clouds["dst"].port_attrs[1]
    assert (second["mac_address"], second["fixed_ips"]) == ("fa:16:3e:00:00:02", [])
    assert (second["security_group_ids"], second["is_port_security_enabled"]) == ([], False)
    assert [n["port"] for n in clouds["dst"].created[0]["networks"]] == list(clouds["dst"].ports)


async def test_handover_rollback_deletes_only_its_own_ports_even_if_the_filter_is_ignored(
    tmp_path,
):
    """A Neutron that ignored the description filter would list every port: the rollback still
    deletes only the port step 8 created for this migration."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, dst_crash_on="create_server")
    clouds["dst"].ports["foreign"] = NS(
        id="foreign",
        network_id="x",
        mac_address="fa:16:3e:99:99:99",
        fixed_ips=[],
        device_id="",
        created_by_nova=False,
        description="",
    )
    clouds["dst"].ports["other-vm"] = NS(
        id="other-vm",
        network_id="x",
        mac_address="fa:16:3e:99:99:98",
        fixed_ips=[],
        device_id="",
        created_by_nova=False,
        description="os-migrate:port-9",
    )
    with pytest.raises(PermanentStepError):
        await executor.run(StepName.CUTOVER, make_ctx(neutron(plan), mig, SRC, DST, settings)[0])
    unfiltered = clouds["dst"].network.ports
    clouds["dst"].network.ports = lambda device_id=None, description=None: unfiltered()
    await executor.run(StepName.ROLLBACK, make_ctx(neutron(plan), mig, SRC, DST, settings)[0])
    assert set(clouds["dst"].ports) == {"foreign", "other-vm"}


def overridden(plan, entry, **kw):
    """The plan with a NIC override for the handover VM's port (SDD §4.2)."""
    return plan.model_copy(update={"nic_overrides": {"srv-1": {"fa:16:3e:00:00:01": entry}}, **kw})


async def test_handover_step_8_sends_an_overridden_nic_where_its_entry_says(tmp_path):
    """SDD §7.3 step 8: the port's entry decides its network and addresses - Nova gets its one
    address in "nova" mode, Neutron every address in "neutron" mode; a network-only entry keeps
    the port's own addresses."""
    from seamless_migrate.domain.models import NicOverride

    entry = NicOverride(network="rhoso-dmz", fixed_ips=["192.168.10.5"])
    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "nova")
    await executor.run(
        StepName.CUTOVER, make_ctx(overridden(plan, entry), mig, SRC, DST, settings)[0]
    )
    assert clouds["dst"].created[0]["networks"] == [
        {"uuid": "dst-net-rhoso-dmz", "fixed_ip": "192.168.10.5"}
    ]

    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "neutron")
    ctx = make_ctx(overridden(plan, entry, port_creation_mode="neutron"), mig, SRC, DST, settings)[
        0
    ]
    await executor.run(StepName.CUTOVER, ctx)
    [attrs] = clouds["dst"].port_attrs
    assert (attrs["network_id"], attrs["fixed_ips"]) == (
        "dst-net-rhoso-dmz",
        [{"ip_address": "192.168.10.5"}],
    )

    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "network-only")
    only_net = overridden(plan, NicOverride(network="rhoso-dmz"), port_creation_mode="neutron")
    await executor.run(StepName.CUTOVER, make_ctx(only_net, mig, SRC, DST, settings)[0])
    [attrs] = clouds["dst"].port_attrs
    assert (attrs["network_id"], attrs["fixed_ips"]) == (
        "dst-net-rhoso-dmz",
        [{"ip_address": "10.0.0.5"}, {"ip_address": "fd00::5"}],
    )


async def test_handover_in_neutron_mode_resumes_on_the_port_its_nic_override_gives(tmp_path):
    """SDD §7.3 step 8: a resume compares an earlier port with what step 8 would create - the
    override's network and addresses, in any order, the MAC in any case; one on the mapped network
    is not its port. A stale earlier port with the NIC's own addresses holds the MAC on the
    override's network: step 0 refuses before anything stops (Neutron would refuse the MAC)."""
    from seamless_migrate.domain.models import NicOverride

    entry = NicOverride(network="rhoso-dmz", fixed_ips=["192.168.10.5", "fd00:10::5"])
    leftover = {
        "mac_address": "fa:16:3e:00:00:01",
        "device_id": "",
        "created_by_nova": False,
        "description": "os-migrate:port-1",
        "project_id": "dst-project",
    }
    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "reuse")
    clouds["dst"].ports["mapped-network"] = NS(
        id="mapped-network",
        network_id="dst-net-rhoso-app",
        fixed_ips=[{"ip_address": "192.168.10.5"}, {"ip_address": "fd00:10::5"}],
        **leftover,
    )
    clouds["dst"].ports["its-own"] = NS(
        id="its-own",
        network_id="dst-net-rhoso-dmz",
        fixed_ips=[{"ip_address": "fd00:10::5"}, {"ip_address": "192.168.10.5"}],
        **{**leftover, "mac_address": "FA:16:3E:00:00:01"},
    )
    ctx = make_ctx(overridden(plan, entry, port_creation_mode="neutron"), mig, SRC, DST, settings)
    await executor.run(StepName.CUTOVER, ctx[0])
    assert clouds["dst"].port_attrs == []
    assert clouds["dst"].created[0]["networks"] == [{"port": "its-own"}]

    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "stale")
    clouds["dst"].ports["own-addresses"] = NS(
        id="own-addresses",
        network_id="dst-net-rhoso-dmz",
        fixed_ips=[{"ip_address": "10.0.0.5"}, {"ip_address": "fd00::5"}],
        **leftover,
    )
    ctx = make_ctx(overridden(plan, entry, port_creation_mode="neutron"), mig, SRC, DST, settings)
    with pytest.raises(
        PermanentStepError, match="MAC fa:16:3e:00:00:01 is held by port own-addresses"
    ):
        await executor.run(StepName.CUTOVER, ctx[0])
    assert [op for op in ops(calls) if op[1] == "stop_server"] == []
    assert clouds["dst"].port_attrs == []


def test_destination_nic_of_a_definition_journaled_with_its_first_address_only():
    """A definition from before every address was journaled keeps its first address."""
    plan = make_plan(mappings=Mappings(networks={"app-net": "rhoso-app"}))
    port = {"network": "app-net", "fixed_ip": "10.0.0.5", "mac_address": "fa:16:3e:00:00:01"}
    assert HandoverExecutor._destination_nic(plan, "srv-1", port) == ("rhoso-app", ["10.0.0.5"])
    bare = {"network": "app-net", "fixed_ip": None}
    assert HandoverExecutor._destination_nic(plan, "srv-1", bare) == ("rhoso-app", [])


async def test_handover_in_nova_mode_records_no_port_groups(tmp_path):
    """The default mode is unchanged: no security-group lookups, Nova creates the port."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    await executor.run(StepName.CUTOVER, ctx)
    saved = json.loads((executor.run_dir(ctx) / "source-server.json").read_text())
    assert "security_groups" not in saved["networks"][0]
    assert clouds["dst"].port_attrs == [] and clouds["dst"].group_lookups == []


async def test_handover_resume_after_a_crash_while_unmanaging_waits_instead_of_failing(tmp_path):
    """The control plane died while Cinder was unmanaging the first volume: the resume knows the
    unmanage was sent and does not trip over the volume Cinder no longer knows (SDD §7.3)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].crash_wait = "delete"
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError):
        await executor.run(StepName.CUTOVER, ctx)
    assert "vol-data" not in clouds["src"].volumes  # Cinder did unmanage it
    result = await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert result.destination_server_id == "dst-new-1"
    assert [op for op in ops(calls) if op[1] == "unmanage"] == [
        ("src", "unmanage", "vol-data"),
        ("src", "unmanage", "vol-root"),
    ]


async def test_handover_resume_after_a_crash_while_managing_waits_for_the_same_volume(tmp_path):
    """The control plane died while RHOSO was managing the first volume: the resume waits for that
    volume instead of managing the renamed object again (SDD §7.3)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["dst"].crash_wait = "status"
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError):
        await executor.run(StepName.CUTOVER, ctx)
    await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert [op for op in ops(calls) if op[1] == "manage"] == [
        ("dst", "manage", "volume-vol-data"),
        ("dst", "manage", "volume-vol-root"),
    ]
    bdm = clouds["dst"].created[0]["block_device_mapping"]
    assert [(b["uuid"], b["boot_index"]) for b in bdm] == [("dst-vol-2", 0), ("dst-vol-1", -1)]


async def test_handover_resume_after_a_crash_while_creating_waits_for_the_same_server(tmp_path):
    """The control plane died while Nova built the destination server: the resume waits for that
    server instead of creating a second one on the same volumes (SDD §7.3)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["dst"].crash_wait = "server"
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="create the server in RHOSO 18.0"):
        await executor.run(StepName.CUTOVER, ctx)
    result = await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert result.destination_server_id == "dst-new-1"
    assert [op for op in ops(calls) if op[1] == "create_server"] == [
        ("dst", "create_server", "web-01")
    ]
    assert sorted(clouds["dst"].servers) == ["dst-new-1"]


async def test_handover_resume_creates_the_server_again_when_the_one_sent_is_gone(tmp_path):
    """A server Nova accepted but lost (deleted while nobody waited) is created again."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["dst"].crash_wait = "server"
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="create the server in RHOSO 18.0"):
        await executor.run(StepName.CUTOVER, ctx)
    clouds["dst"].compute.delete_server("dst-new-1")
    result = await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert result.destination_server_id == "dst-new-2"
    assert sorted(clouds["dst"].servers) == ["dst-new-2"]


async def test_handover_rollback_after_a_crash_while_creating_deletes_that_server(tmp_path):
    """The destination server a crashed create left is known to the journal: the rollback deletes
    it before it unmanages the volumes it holds (SDD §7.3)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["dst"].crash_wait = "server"
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="create the server in RHOSO 18.0"):
        await executor.run(StepName.CUTOVER, ctx)
    calls.clear()
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert ops(calls)[0] == ("dst", "delete_server", "dst-new-1")
    assert clouds["dst"].servers == {}
    assert ("dst", "unmanage", "dst-vol-1") in ops(calls)
    assert result.details["source_running"] is True


async def test_handover_rollback_resume_after_a_crash_while_recreating_the_source_waits_for_it(
    tmp_path,
):
    """The rollback crashed while Nova built the recreated source server: its resume waits for
    that server instead of creating a second one (SDD §7.3)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, dst_crash_on="manage")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="manage vol-data in RHOSO 18.0"):
        await executor.run(StepName.CUTOVER, ctx)
    clouds["src"].crash_wait = "server"
    with pytest.raises(PermanentStepError, match="recreate the source server"):
        await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert result.details["vm"]["source_id"] == "src-new-1"
    assert [op for op in ops(calls) if op[1] == "create_server"] == [
        ("src", "create_server", "web-01")
    ]
    assert sorted(clouds["src"].servers) == ["src-new-1"]


async def test_handover_rollback_after_a_crash_while_managing_unmanages_that_volume(tmp_path):
    """The RHOSO volume a crashed manage created is known to the journal: the rollback unmanages
    it and manages the source back under its name (SDD §7.3)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["dst"].crash_wait = "status"
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError):
        await executor.run(StepName.CUTOVER, ctx)
    calls.clear()
    await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert ("dst", "unmanage", "dst-vol-1") in ops(calls)
    assert ("src", "manage", "volume-dst-vol-1") in ops(calls)
    assert "dst-vol-1" not in clouds["dst"].volumes


async def test_handover_rollback_after_a_crash_while_unmanaging_finishes_it_and_manages_back(
    tmp_path,
):
    """A crash while Cinder unmanaged the first source volume, then a rollback: the volume Cinder
    forgot is managed back with the other one and the source server boots on both (SDD §7.3)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].crash_wait = "delete"
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError):
        await executor.run(StepName.CUTOVER, ctx)
    calls.clear()
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert ("src", "manage", "volume-vol-data") in ops(calls)
    assert ("src", "unmanage", "vol-data") not in ops(calls)  # Cinder already did
    bdm = clouds["src"].created[0]["block_device_mapping"]
    assert len(bdm) == 2 and result.details["source_running"] is True


async def test_handover_rollback_resume_after_a_crash_while_managing_back(tmp_path):
    """The rollback itself crashed while the source managed a volume back: its resume waits for
    that volume instead of managing the object again (SDD §7.3)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, dst_crash_on="manage")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="manage vol-data in RHOSO 18.0"):
        await executor.run(StepName.CUTOVER, ctx)
    clouds["src"].crash_wait = "status"
    with pytest.raises(PermanentStepError):
        await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    calls.clear()
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert [op for op in ops(calls) if op[1] == "manage"] == [("src", "manage", "volume-vol-data")]
    assert result.details["source_running"] is True


async def test_handover_rollback_after_unmanage_manages_back_and_recreates(tmp_path):
    """Crash at the RHOSO manage: both volumes are unmanaged at the source; the rollback manages
    them back, recreates the server and reports the new ids."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, dst_crash_on="manage")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="manage vol-data in RHOSO 18.0"):
        await executor.run(StepName.CUTOVER, ctx)
    calls.clear()
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert ops(calls) == [
        ("src", "manage", "volume-vol-root"),
        ("src", "manage", "volume-vol-data"),
        ("src", "create_port", "fa:16:3e:00:00:01"),  # Nova deleted the port it created
        ("src", "create_server", "web-01"),
    ]
    new_vm = result.details["vm"]
    assert new_vm["source_id"] == "src-new-1"
    assert {d["id"] for d in new_vm["disks"]} == {"src-vol-1", "src-vol-2"}
    # a second rollback while the migration still names the replaced server (a stop or a failed
    # save after the first, SDD §7.3) changes nothing and reports the same VM again (audit EX-04)
    calls.clear()
    again = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert calls == [] and again.details["vm"] == new_vm
    # once the migration follows the recreated VM, there is nothing to roll back
    followed = mig.model_copy(update={"vm": VMRef.model_validate(new_vm)})
    done = await executor.run(StepName.ROLLBACK, make_ctx(plan, followed, SRC, DST, settings)[0])
    assert calls == [] and done.details["note"] == "nothing to roll back"


async def test_a_rollback_resumed_after_two_cycles_reports_the_newest_recreated_vm(tmp_path):
    """SDD §7.3: a migration rolled back twice - each cycle recreating its source - reads, when its
    second rollback's outcome was not recorded, the newest archived journal, never an older
    cycle's (audit EX-04)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    rdir = executor.run_dir(ctx)
    rdir.mkdir(parents=True)
    (rdir / "source-server.json").write_text(
        json.dumps({"server_id": "srv-1", "attachments": [{"volume_id": "vol-root"}]})
    )
    for stamp, server, volume in (
        ("20261017T040000", "src-old", "src-vol-old"),
        ("20261017T050000", "src-newest", "src-vol-newest"),
    ):
        done = {"rb:create_source": {"server_id": server}}
        done["rb:manage_src:vol-root"] = {"volume_id": volume}
        (rdir / f"handover-journal.rolled-back-{stamp}.json").write_text(
            json.dumps({"done": done, "order": list(done)})
        )
    result = await executor.run(StepName.ROLLBACK, ctx)
    assert result.details["vm"]["source_id"] == "src-newest" and calls == []
    assert "src-vol-newest" in {d["id"] for d in result.details["vm"]["disks"]}


async def test_a_second_rollback_after_one_that_kept_the_source_has_nothing_to_roll_back(
    tmp_path,
):
    """SDD §7.3: a rollback before the source server was deleted keeps the server and its id, so
    a second one - its journal archived - has nothing to roll back and reports no VM (audit
    EX-04)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="delete_server")
    with pytest.raises(PermanentStepError, match="delete the source server"):
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    first = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert "vm" not in first.details
    calls.clear()
    again = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert calls == [] and again.details == {
        "source_running": True,
        "note": "nothing to roll back",
    }


@pytest.mark.parametrize(
    ("trait", "reason"),
    [
        ({"encryption_key_id": "key-1"}, "encrypted"),
        ({"snapshots": 2}, "2 snapshot"),
        ({"group_id": "grp-1"}, "group"),
        ({"consistency_group_id": "cg-1"}, "group"),
    ],
)
async def test_handover_refuses_before_stop_when_cinder_cannot_unmanage_a_volume(
    tmp_path, trait, reason
):
    """Cinder's unmanage rules (volume/api.py delete(unmanage_only=True)) are checked while the
    VM runs: such a volume would otherwise fail after the stop."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    for key, value in trait.items():
        setattr(clouds["src"].volumes["vol-data"], key, value)
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match=reason):
        await executor.run(StepName.CUTOVER, ctx)
    assert calls == [] and rec.downtime_marks == 0


@pytest.mark.parametrize(
    ("server", "flavor_details", "named"),
    [
        ({"flavor": {"original_name": "m1.big", "ephemeral": 50, "swap": 0}}, {}, "an ephemeral"),
        ({"flavor": {"original_name": "m1.swap", "ephemeral": 0, "swap": 1024}}, {}, "a swap"),
        ({"image": {"id": "img-1"}}, {}, "a root disk from an image"),
        ({"flavor": {"id": "flv-7"}}, {"flv-7": {"ephemeral": 20, "swap": ""}}, "an ephemeral"),
    ],
)
async def test_handover_refuses_before_stop_a_server_with_a_disk_on_its_hypervisor(
    tmp_path, server, flavor_details, named
):
    """SDD §7.3 step 0, §4.2: a server resized since its validation onto a flavor with an
    ephemeral or swap disk - read from the flavor Nova embeds, or looked up by id where it does not
    - or booted from an image. Step 4 would delete that disk with the server; it is refused while
    the VM still runs (audit EX-15)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    for key, value in server.items():
        setattr(clouds["src"].servers["srv-1"], key, value)
    clouds["src"].flavor_details = flavor_details
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
        await executor.run(StepName.CUTOVER, ctx)
    assert named in str(refused.value) and "on its hypervisor" in str(refused.value)
    assert calls == [] and rec.downtime_marks == 0


async def test_handover_refuses_before_stop_a_volume_another_server_holds_too(tmp_path):
    """SDD §7.3 step 0: a multi-attach volume another server also holds stays in-use once step 4
    deletes the source server, so step 5 could never unmanage it - refused while the VM runs. One
    the source alone holds passes (audit EX-09)."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    volume = clouds["src"].volumes["vol-data"]
    volume.multiattach = True
    volume.attachments = [{"server_id": "srv-1"}, {"server_id": "srv-db-2"}]
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
        await executor.run(StepName.CUTOVER, ctx)
    assert "volume vol-data is attached to another server too (srv-db-2)" in str(refused.value)
    assert calls == [] and rec.downtime_marks == 0
    # an attachment without a server holds it as well
    volume.attachments = [{"server_id": "srv-1"}, {"server_id": None}]
    with pytest.raises(PermanentStepError, match="an attachment without a server"):
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    # the source alone holds it: the handover goes ahead
    volume.attachments = [{"server_id": "srv-1"}]
    result = await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert result.destination_server_id == "dst-new-1"


async def test_handover_refuses_before_stop_without_compute_microversion_2_85(tmp_path):
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].compute.max_microversion = "2.79"
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="2.85"):
        await executor.run(StepName.CUTOVER, ctx)
    assert calls == [] and rec.downtime_marks == 0


# -- NetApp ONTAP (SDD §7.3.1) -------------------------------------------------------------------
NFS_SRC = "overcloud@ontap_nfs#192.0.2.5:/cinder_vol"
NFS_DST = "hostgroup@ontap_nfs#10.20.0.5:/cinder_vol"


def netapp_setup(tmp_path, family="nfs", crash_on=None, dst_pools=None):
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on=crash_on)
    if family == "nfs":
        src_pool, caps, backend = NFS_SRC, ONTAP_NFS, "hostgroup@ontap_nfs"
        default_dst = [
            ("hostgroup@ontap_nfs#10.20.0.5:/cinder_gold", ONTAP_NFS),
            (NFS_DST, ONTAP_NFS),
        ]
    else:
        src_pool, caps, backend = (
            "overcloud@ontap_iscsi#flex_a",
            ONTAP_ISCSI,
            "hostgroup@ontap_iscsi",
        )
        default_dst = [
            ("hostgroup@ontap_iscsi#flex_a", ONTAP_ISCSI),
            ("hostgroup@ontap_iscsi#flex_b", ONTAP_ISCSI),
        ]
    for volume in clouds["src"].volumes.values():
        volume.host = src_pool
    clouds["src"].pools = [(src_pool, caps), ("overcloud@tripleo_ceph#ssd", CEPH)]
    clouds["dst"].pools = default_dst if dst_pools is None else dst_pools
    plan = plan.model_copy(
        update={
            "handover": HandoverConfig(
                enabled=True, backend_map={"ceph-ssd": backend, "ceph-hdd": backend}
            )
        }
    )
    return executor, calls, clouds, plan, mig, settings


async def test_handover_netapp_nfs_manages_by_share_path(tmp_path):
    executor, calls, clouds, plan, mig, settings = netapp_setup(tmp_path, "nfs")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    await executor.run(StepName.CUTOVER, ctx)
    managed = [(op, arg) for cloud, op, arg in calls if cloud == "dst" and op == "manage"]
    # the destination's own LIF address, the export the source used
    assert managed == [
        ("manage", "10.20.0.5:/cinder_vol/volume-vol-data"),
        ("manage", "10.20.0.5:/cinder_vol/volume-vol-root"),
    ]
    assert {m["host"] for m in clouds["dst"].managed} == {NFS_DST}
    saved = json.loads((executor.run_dir(ctx) / "source-server.json").read_text())
    assert saved["storage"]["vol-data"] == {
        "family": "netapp_nfs",
        "src_pool": "192.0.2.5:/cinder_vol",
        "dst_host": NFS_DST,
    }


async def test_handover_netapp_block_manages_by_lun_path(tmp_path):
    executor, calls, clouds, plan, mig, settings = netapp_setup(tmp_path, "iscsi")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    await executor.run(StepName.CUTOVER, ctx)
    assert [arg for cloud, op, arg in calls if cloud == "dst" and op == "manage"] == [
        "/vol/flex_a/volume-vol-data",
        "/vol/flex_a/volume-vol-root",
    ]
    assert {m["host"] for m in clouds["dst"].managed} == {"hostgroup@ontap_iscsi#flex_a"}


@pytest.mark.parametrize(
    ("dst_pools", "reason"),
    [
        ([("hostgroup@ontap_nfs#10.20.0.5:/cinder_gold", ONTAP_NFS)], "no pool for the export"),
        ([("hostgroup@ontap_nfs#flex_a", ONTAP_ISCSI)], "same driver family"),
        (None, "Cinder pools of RHOSO 18.0"),
    ],
)
async def test_handover_refuses_before_stop_when_a_pool_cannot_be_resolved(
    tmp_path, dst_pools, reason
):
    executor, calls, clouds, plan, mig, settings = netapp_setup(tmp_path, "nfs")
    clouds["dst"].pools = dst_pools
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match=reason):
        await executor.run(StepName.CUTOVER, ctx)
    assert calls == [], "nothing changed: the VM keeps running"
    assert rec.downtime_marks == 0
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert result.details["note"] == "nothing to roll back"


async def test_the_handovers_pool_refusal_names_the_destination_provider(tmp_path):
    """SDD §1 naming, §7.3.1 (review RV20-05): a refusal built for a migration names the
    destination provider - in the run's step 0 and in the read-only readiness report."""
    executor, calls, clouds, plan, mig, settings = netapp_setup(tmp_path, "nfs")
    clouds["dst"].pools = [("hostgroup@ceph#gold", CEPH)]
    named = DST.model_copy(update={"name": "OpenStack 2026.2 Hibiscus (DC3)"})
    ctx, _ = make_ctx(plan, mig, SRC, named, settings)
    with pytest.raises(PermanentStepError) as refused:
        await executor.run(StepName.CUTOVER, ctx)
    assert "OpenStack 2026.2 Hibiscus (DC3) lists no pools for hostgroup@ontap_nfs" in str(
        refused.value
    )
    assert "the destination lists" not in str(refused.value)
    assert calls == [], "nothing changed: the VM keeps running"


def test_the_runs_storage_resolution_names_the_destination_provider(tmp_path):
    """SDD §1 naming, §7.3.1 (review RV20-05): the run resolves each volume's destination host
    again when it records the source definition, after the stop; a refusal there names the
    destination provider too."""
    executor, calls, clouds, plan, mig, settings = netapp_setup(tmp_path, "nfs")
    clouds["dst"].pools = [("hostgroup@ceph#gold", CEPH)]
    src, dst = clouds["src"], clouds["dst"]
    attachments = [{"volume_id": "vol-data", "volume_type": "ceph-ssd", "host": NFS_SRC}]
    with pytest.raises(PermanentStepError) as refused:
        executor._resolve_storage(
            src, dst, attachments, plan.handover.backend_map, "OpenStack 2026.2 Hibiscus (DC3)"
        )
    assert str(refused.value).startswith(
        "volume vol-data: OpenStack 2026.2 Hibiscus (DC3) lists no pools for hostgroup@ontap_nfs"
    ), str(refused.value)


async def test_every_handover_text_about_the_destination_cloud_names_its_provider(tmp_path):
    """SDD §1 naming (review RV21-05): a refusal or failure of the handover about the destination
    cloud names its provider - the pools it cannot read, step 0's port problems, the server it
    fails to create - never "the destination", nor a network without its cloud."""
    name = "OpenStack 2026.2 Hibiscus (DC3)"
    named = DST.model_copy(update={"name": name})

    async def refusal(case, change=lambda dst: None, plan_of=lambda p: p, **kw):
        executor, _, clouds, plan, mig, settings = setup(tmp_path / case, **kw)
        change(clouds["dst"])
        with pytest.raises(PermanentStepError) as refused:
            await executor.run(
                StepName.CUTOVER, make_ctx(plan_of(plan), mig, SRC, named, settings)[0]
            )
        return str(refused.value)

    def unreadable(dst):
        dst.pools = None

    def private(dst):
        dst.network_list = [
            NS(
                id="private-net",
                name="rhoso-app",
                project_id="tenant-b",
                is_shared=False,
                is_router_external=False,
            )
        ]

    def held(port_id, **change):
        def put(dst):
            base = {
                "id": port_id,
                "network_id": "dst-net-rhoso-app",
                "mac_address": "fa:16:3e:99:99:99",
                "fixed_ips": [{"ip_address": "10.0.0.5"}],
                "device_id": "other-vm",
                "created_by_nova": True,
                "description": "",
                "project_id": "dst-project",
            }
            dst.ports[port_id] = NS(**{**base, **change})

        return put

    where = f"port port-1 (fa:16:3e:00:00:01) on 'rhoso-app' of {name}"
    assert f"cannot read the Cinder pools of {name} (scheduler-stats" in await refusal(
        "pools", unreadable
    )
    assert f"{where} (project tenant-b): it is neither shared" in await refusal("private", private)

    def outside(dst):
        dst.subnet_list = [subnet("dst-net-rhoso-app", "10.9.0.0/24")]

    moved = await refusal("address", outside)
    assert f"{where}: 10.0.0.5 is no host address of any subnet" in moved
    assert f"{where}: 10.0.0.5 is held by port taken" in await refusal("taken", held("taken"))
    same = held("same-mac", mac_address="fa:16:3e:00:00:01", fixed_ips=[])
    mac = await refusal("mac", same, neutron)
    assert f"{where}: MAC fa:16:3e:00:00:01 is held by port same-mac" in mac
    created = await refusal("create", plan_of=neutron, dst_crash_on="create_server")
    assert created.startswith(f"create the server in {name}: "), created
    # the run reads the pools again when it records the source definition, after the stop
    executor, _, clouds, plan, _, _ = setup(tmp_path / "run-pools")
    clouds["dst"].pools = None
    vtype = next(iter(plan.handover.backend_map))
    attachments = [{"volume_id": "vol-data", "volume_type": vtype, "host": "any"}]
    with pytest.raises(
        PermanentStepError, match=f"cannot read the Cinder pools of {re.escape(name)}"
    ):
        executor._resolve_storage(
            clouds["src"], clouds["dst"], attachments, plan.handover.backend_map, name
        )

    async def rollback_failure(case, crash, plan_of=lambda p: p):
        executor, _, _, plan, mig, settings = setup(tmp_path / case, dst_crash_on=crash)
        ctx = make_ctx(plan_of(plan), mig, SRC, named, settings)[0]
        mig.destination_server_id = (
            await executor.run(StepName.CUTOVER, ctx)
        ).destination_server_id
        with pytest.raises(PermanentStepError) as failed:
            await executor.run(
                StepName.ROLLBACK, make_ctx(plan_of(plan), mig, SRC, named, settings)[0]
            )
        return str(failed.value)

    deleted = await rollback_failure("delete-server", "delete_server")
    assert deleted.startswith(f"delete the server in {name}: "), deleted
    ports = await rollback_failure("delete-port", "delete_port", neutron)
    assert ports.startswith(f"delete the port made for port-1 in {name}: "), ports


async def test_the_run_resolves_storage_with_the_destination_providers_name(tmp_path, monkeypatch):
    """SDD §1 naming, §7.3.1 (review RV20-05): the run passes the destination provider's name to
    the storage resolution it records with the source definition."""
    executor, calls, clouds, plan, mig, settings = netapp_setup(tmp_path, "nfs")
    real = executor._resolve_storage
    named: list[str] = []

    def spy(*args):
        named.append(args[-1])
        return real(*args)

    monkeypatch.setattr(executor, "_resolve_storage", spy)
    destination = DST.model_copy(update={"name": "OpenStack 2026.2 Hibiscus (DC3)"})
    await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, destination, settings)[0])
    assert named == ["OpenStack 2026.2 Hibiscus (DC3)"]


async def test_a_providers_name_never_decides_whether_a_failure_is_retried(tmp_path):
    """Review RV20-12: a failed call is retried or not by what failed - the error - never by the
    label naming it, which carries the destination provider's name (T6): a provider named "Lab
    Timeout DC" must not turn a permanent failure into a retried one."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)

    def in_use():
        raise RuntimeError("volume vol-1 is in use")

    with pytest.raises(PermanentStepError) as permanent:
        await executor._call("manage vol-1 in Lab Timeout DC", in_use)
    assert type(permanent.value) is PermanentStepError
    assert str(permanent.value) == "manage vol-1 in Lab Timeout DC: volume vol-1 is in use"

    def busy():
        raise RuntimeError("HTTP 503 Service Unavailable")

    with pytest.raises(TransientStepError):
        await executor._call("manage vol-1 in DC3", busy)


async def test_handover_rollback_netapp_manages_back_with_the_source_share(tmp_path):
    executor, calls, clouds, plan, mig, settings = netapp_setup(tmp_path, "nfs")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    cut = await executor.run(StepName.CUTOVER, ctx)
    calls.clear()
    mig.destination_server_id = cut.destination_server_id
    await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    # RHOSO renamed the files to its own ids; the source mounts the export at its own address
    assert [arg for cloud, op, arg in calls if cloud == "src" and op == "manage"] == [
        "192.0.2.5:/cinder_vol/volume-dst-vol-2",
        "192.0.2.5:/cinder_vol/volume-dst-vol-1",
    ]
    assert {m["host"] for m in clouds["src"].managed} == {NFS_SRC}


async def test_handover_resume_of_a_definition_without_storage_stays_rbd(tmp_path):
    """A run journaled before §7.3.1 has no `storage` in its definition: it resumes as RBD."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="delete_server")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError):
        await executor.run(StepName.CUTOVER, ctx)
    definition = executor.run_dir(ctx) / "source-server.json"
    legacy = json.loads(definition.read_text())
    legacy.pop("storage")
    definition.write_text(json.dumps(legacy))
    clouds["src"].pools = clouds["dst"].pools = None  # not even readable any more
    calls.clear()
    await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert [arg for cloud, op, arg in calls if op == "manage"] == [
        "volume-vol-data",
        "volume-vol-root",
    ]
    assert [m["host"] for m in clouds["dst"].managed] == [
        "hostgroup@ceph-hdd#hdd",
        "hostgroup@ceph-ssd#ssd",
    ]


UEFI_WINDOWS = {
    "hw_firmware_type": "uefi",
    "hw_machine_type": "q35",
    "os_type": "windows",
    "os_distro": "windows",
    "img_hide_hypervisor_id": "true",
    "architecture": "x86_64",
    # Glance bookkeeping that must not be copied
    "image_id": "0f1e2d3c",
    "checksum": "abc",
    "size": "21474836480",
}
BOOT_PROPS = {k: v for k, v in UEFI_WINDOWS.items() if k not in ("image_id", "checksum", "size")}


async def test_handover_restores_boot_properties_on_managed_volumes(tmp_path):
    """SDD §7.3 step 7: manage drops volume_image_metadata; a UEFI Windows guest needs it back."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].volumes["vol-root"].volume_image_metadata = dict(UEFI_WINDOWS)
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    await executor.run(StepName.CUTOVER, ctx)
    assert ops(calls)[-2:] == [
        ("dst", "set_image_metadata", "dst-vol-2"),  # the boot volume, before the server boots
        ("dst", "create_server", "web-01"),
    ]
    assert clouds["dst"].image_metadata == {"dst-vol-2": BOOT_PROPS}

    calls.clear()
    mig.destination_server_id = "dst-new-1"
    await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    # the re-managed source volume boots the same way again
    assert ("src", "set_image_metadata", "src-vol-1") in ops(calls)
    assert clouds["src"].image_metadata == {"src-vol-1": BOOT_PROPS}


async def test_handover_fails_fast_when_cinder_refuses_the_unmanage(tmp_path):
    """A snapshot taken after the checks makes Cinder answer 400: the step fails with Cinder's
    reason at once (openstacksdk returns 4xx answers instead of raising), and the rollback
    recreates the deleted source server on its volumes."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    nova_delete = clouds["src"].compute.delete_server

    def delete_then_snapshot(server, ignore_missing=True):
        nova_delete(server, ignore_missing=ignore_missing)
        clouds["src"].volumes["vol-data"].snapshots = 1

    clouds["src"].compute.delete_server = delete_then_snapshot
    executor.wait_s = 0.05
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="HTTP 400.*have snapshots"):
        await executor.run(StepName.CUTOVER, ctx)
    calls.clear()
    result = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert ops(calls) == [
        ("src", "create_port", "fa:16:3e:00:00:01"),  # Nova deleted the port it created
        ("src", "create_server", "web-01"),
    ]
    assert result.details["source_running"] is True


async def test_handover_fails_when_rhoso_refuses_the_boot_properties(tmp_path):
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].volumes["vol-root"].volume_image_metadata = {"hw_firmware_type": "uefi"}
    clouds["dst"].refuse_metadata = True
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="boot properties.*HTTP 400"):
        await executor.run(StepName.CUTOVER, ctx)
    assert all(op != "create_server" for _, op, _ in ops(calls)), "no server without its UEFI"


async def test_handover_readiness_reports_references_without_changing_anything(tmp_path):
    """The read-only report the lab runs first (and step 0 uses): per volume its family, pools and
    the exact manage reference, plus every problem — no call that changes a cloud."""
    executor, calls, clouds, plan, mig, settings = netapp_setup(tmp_path, "nfs")
    report = await executor.readiness(SRC, DST, "srv-1", plan.handover.backend_map)
    assert calls == []
    assert report["problems"] == []
    by_id = {v["volume_id"]: v for v in report["volumes"]}
    assert by_id["vol-root"] == {
        "volume_id": "vol-root",
        "volume_type": "ceph-ssd",
        "family": "netapp_nfs",
        "source_pool": NFS_SRC,
        "destination_host": NFS_DST,
        "reference": {"source-name": "10.20.0.5:/cinder_vol/volume-vol-root"},
        "delete_on_termination": True,
    }
    clouds["src"].volumes["vol-data"].snapshots = 1
    clouds["dst"].pools = [("hostgroup@ontap_nfs#10.20.0.5:/cinder_gold", ONTAP_NFS)]
    report = await executor.readiness(SRC, DST, "srv-1", plan.handover.backend_map)
    assert any("no pool for the export" in p for p in report["problems"])
    assert any("1 snapshot" in p for p in report["problems"])
    assert calls == []


def test_an_http_error_body_is_redacted_before_it_is_cut():
    """SDD §13.4: an HTTP error body is redacted over its cut plus 16384 characters before it is
    cut to 300 — a URL whose password the cut splits would otherwise reach Migration.error."""
    from seamless_migrate.executors.handover import _checked

    body = "x" * 270 + "\n postgresql://svc:Sup3rS3cretPw@db.example:5432/seamless"
    with pytest.raises(RuntimeError) as failed:
        _checked(NS(status_code=500, text=body), "attach the volume")
    message = str(failed.value)
    assert message.startswith("attach the volume: HTTP 500: xxx") and "\n" not in message
    assert "Sup3r" not in message and "[REDACTED]" in message


def test_existing_server_and_write_json_helpers(tmp_path):
    from seamless_migrate.executors.handover import _existing_server, _write_json

    class ResourceNotFound(Exception):
        pass

    class Gone(Exception):
        status_code = 404

    class Boom(Exception):
        status_code = 500

    def compute(exc):
        return NS(compute=NS(get_server=lambda sid: (_ for _ in ()).throw(exc)))

    assert _existing_server(compute(ResourceNotFound("x")), "srv") is None
    assert _existing_server(compute(Gone("x")), "srv") is None
    with pytest.raises(Boom):
        _existing_server(compute(Boom("x")), "srv")
    assert _existing_server(NS(compute=NS(get_server=lambda sid: "server")), "srv") == "server"

    target = tmp_path / "nested" / "journal.json"
    _write_json(target, {"b": 1, "a": [1, 2]})
    assert json.loads(target.read_text()) == {"a": [1, 2], "b": 1}
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(TypeError):
        _write_json(target, {"bad": object()})
    assert json.loads(target.read_text()) == {"a": [1, 2], "b": 1}, "atomic: old content kept"
    assert [p.name for p in target.parent.iterdir()] == ["journal.json"], "temp file removed"


def test_port_description_matches_the_collection():
    """SDD §6.6/§7.3: the handover marks the ports it creates through Neutron as the collection
    does - one convention for every such port (plugins/module_utils/server_port.py, loaded
    without its imports)."""
    source = (
        Path(__file__).resolve().parents[2] / "plugins/module_utils/server_port.py"
    ).read_text()
    tree = ast.parse(source)
    func = next(n for n in tree.body if getattr(n, "name", None) == "port_description")
    namespace: dict = {}
    exec(compile(ast.Module(body=[func], type_ignores=[]), "server_port.py", "exec"), namespace)
    for port_id in ("port-1", "8b0c01a1-205f-490b-a4fc-ee8f44711bc7"):
        assert port_description(port_id) == namespace["port_description"](port_id)


def subnet(network_id, cidr, mode=None):
    return NS(
        id=f"sub-{cidr}",
        network_id=network_id,
        cidr=cidr,
        ipv6_address_mode=mode,
        ipv6_ra_mode=mode,
    )


async def test_handover_step_0_refuses_a_network_step_8_could_not_resolve(tmp_path):
    """SDD §7.3 step 0: step 8 takes the credential's own network of that name, else the only one
    it sees; none, several in its project, or several elsewhere refuse before the stop."""
    cases = {
        "none": [],
        "two-own": [
            NS(id="a", name="rhoso-app", project_id="dst-project"),
            NS(id="b", name="rhoso-app", project_id="dst-project"),
        ],
        "two-elsewhere": [
            NS(id="a", name="rhoso-app", project_id="tenant-a"),
            NS(id="b", name="rhoso-app", project_id="tenant-b"),
        ],
    }
    for name, listed in cases.items():
        executor, calls, clouds, plan, mig, settings = setup(tmp_path / name)
        clouds["dst"].network_list = listed
        with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
            await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
        assert "rhoso-app" in str(refused.value), name
        assert "port-1" in str(refused.value), name
        # the VM keeps running: nothing was stopped
        assert [op for op in ops(calls) if op[1] == "stop_server"] == [], name


async def test_handover_step_0_refuses_a_security_group_step_8_could_not_find(tmp_path):
    """SDD §7.3 step 0: step 8 asks for the server's groups by name (Nova looks each up in the
    project, `default` aside) and in "neutron" mode for each port's groups; one the destination
    credential's project has none or several of refuses before the stop, not after step 4 deleted
    the source server."""
    web = NS(id="sg-a", name="web", project_id="dst-project")
    twice = [web, NS(id="sg-b", name="web", project_id="dst-project")]
    # the server's groups, in either mode: here only another project has 'web'
    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "server")
    clouds["src"].servers["srv-1"].security_groups = [{"name": "default"}, {"name": "web"}]
    clouds["dst"].group_list = [NS(id="sg-c", name="web", project_id="tenant-b")]
    with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert (
        "the server: RHOSO 18.0 has no security group named 'web' in the credential's project"
        in (str(refused.value))
    )
    assert "'default'" not in str(refused.value)
    assert [op for op in ops(calls) if op[1] == "stop_server"] == []
    # in "neutron" mode each port's groups too: two of the project carry the port's 'web'
    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "port")
    clouds["dst"].group_list = twice
    with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
        await executor.run(StepName.CUTOVER, make_ctx(neutron(plan), mig, SRC, DST, settings)[0])
    assert (
        "port port-1: RHOSO 18.0 has 2 security groups named 'web' in the credential's project"
        in str(refused.value)
    )
    assert [op for op in ops(calls) if op[1] == "stop_server"] == []
    # through Nova the port's own groups are never asked for: the same destination passes
    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "nova")
    clouds["dst"].group_list = twice
    await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert clouds["dst"].created


async def test_handover_creates_the_server_with_the_flavor_preflight_matched(tmp_path):
    """SDD §7.3 step 8, review RT-01: with no flavor mapped, the flavor preflight matched for the
    migration (its resolved mappings, §7.2) is the server's; a plan mapping still wins."""
    matched = Mappings(flavors={"m1.small": "rhoso.medium"})
    for name, flavors, expected in (
        ("matched", {}, "flavor-rhoso.medium"),
        ("mapped", {"m1.small": "rhoso.small"}, "flavor-rhoso.small"),
    ):
        executor, calls, clouds, plan, mig, settings = setup(tmp_path / name)
        # RHOSO has no flavor of the source's name: step 0 checks the one step 8 uses
        clouds["dst"].flavor_names = {"rhoso.medium", "rhoso.small"}
        mappings = plan.mappings.model_copy(update={"flavors": flavors})
        plan = plan.model_copy(update={"mappings": mappings})
        mig = mig.model_copy(update={"resolved_mappings": matched})
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
        assert clouds["dst"].created[0]["flavor_id"] == expected, name


async def test_handover_step_0_refuses_a_flavor_or_a_volume_type_rhoso_lacks(tmp_path):
    """SDD §7.3 step 0, review RT-01/RT-02: steps 6 and 8 run after step 4 deleted the source
    server; a flavor or a volume type they would ask RHOSO for and it lacks refuses before the
    stop."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "flavor")
    clouds["dst"].flavor_names = {"rhoso.large"}
    with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert "the server's flavor 'm1.small': RHOSO 18.0 has no flavor named 'rhoso.small'" in (
        str(refused.value)
    )
    assert [op for op in ops(calls) if op[1] == "stop_server"] == []
    # ceph-ssd is unmapped and RHOSO lacks a type of that name; ceph-hdd maps to rbd-hdd, it has
    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "type")
    clouds["dst"].type_names = ("rbd-hdd",)
    with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert "volume vol-root: RHOSO 18.0 has no volume type named 'ceph-ssd'" in str(refused.value)
    # vol-data is checked under its mapped type, which RHOSO has
    assert "vol-data" not in str(refused.value)
    assert [op for op in ops(calls) if op[1] == "stop_server"] == []


async def test_handover_step_8_uses_the_network_step_0_resolved(tmp_path):
    """SDD §7.3 step 8: the credential's own network of that name among same-named ones of other
    projects, and a shared one another project owns when it is the only one of that name."""
    own = NS(id="own-net", name="rhoso-app", project_id="dst-project")
    other = NS(id="other-net", name="rhoso-app", project_id="tenant-b")
    shared = NS(id="shared-net", name="rhoso-app", project_id="admin-project", is_shared=True)
    for name, listed, expected in (
        ("own", [other, own], "own-net"),
        ("shared", [shared], "shared-net"),
    ):
        executor, calls, clouds, plan, mig, settings = setup(tmp_path / name)
        clouds["dst"].network_list = listed
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
        assert clouds["dst"].created[0]["networks"] == [
            {"uuid": expected, "fixed_ip": "10.0.0.5"}
        ], name
        executor, calls, clouds, plan, mig, settings = setup(tmp_path / f"{name}-neutron")
        clouds["dst"].network_list = listed
        await executor.run(StepName.CUTOVER, make_ctx(neutron(plan), mig, SRC, DST, settings)[0])
        [attrs] = clouds["dst"].port_attrs
        assert attrs["network_id"] == expected, name


def test_handover_step_0_reads_a_real_sources_microversion(tmp_path):
    """Review RV18-02: step 0 split str(max_microversion) on "." - keystoneauth's tuple (2, 96)
    read as (0, 0) - and refused every real source as below 2.85, so storage handover could not
    run on a real cloud. It reads the tuple; a source below 2.85 is still refused, its maximum
    named as major.minor (SDD §7.3 step 0)."""
    caracal = InProcessCloud(tmp_path / "caracal", compute="2.96")
    conn = caracal.connect(make_provider(cloud="dst"), caracal.settings)
    assert HandoverExecutor._unmanage_blockers(conn, [], "srv-1") == []

    train = InProcessCloud(tmp_path / "train", compute="2.79")
    conn = train.connect(make_provider(cloud="dst"), train.settings)
    assert HandoverExecutor._unmanage_blockers(conn, [], "srv-1") == [
        "the source compute API supports microversion 2.79; keeping the volumes when the source "
        "server is deleted needs 2.85"
    ]


async def test_handover_step_0_refuses_a_network_the_credential_may_not_put_a_port_on(tmp_path):
    """SDD §7.3 step 0, §6.6: another project's network takes the port only when it is shared with
    the credential's project or external, and from a token with the admin or advsvc role. The
    admin credential sees every project's private networks: step 8 must never use one."""
    private = NS(
        id="private-net",
        name="rhoso-app",
        project_id="tenant-b",
        is_shared=False,
        is_router_external=False,
    )
    shared = NS(
        id="shared-net",
        name="rhoso-app",
        project_id="admin-project",
        is_shared=True,
        is_router_external=False,
    )
    external = NS(
        id="ext-net",
        name="rhoso-app",
        project_id="admin-project",
        is_shared=False,
        is_router_external=True,
    )
    for name, listed, roles, refusal in (
        (
            "private",
            [private],
            ["admin", "member", "reader"],
            "'rhoso-app' of RHOSO 18.0 (project tenant-b): it is neither shared with the "
            "credential's project "
            "nor external",
        ),
        (
            "member",
            [shared],
            ["member", "reader"],
            "'rhoso-app' of RHOSO 18.0 (project admin-project): Neutron lets only the admin or "
            "advsvc role, or "
            "that project, choose a port's address or MAC there",
        ),
    ):
        executor, calls, clouds, plan, mig, settings = setup(tmp_path / name)
        clouds["dst"].network_list = listed
        clouds["dst"].roles = roles
        with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
            await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
        assert refusal in str(refused.value), name
        assert "port-1" in str(refused.value), name
        # the VM keeps running: nothing was stopped
        assert [op for op in ops(calls) if op[1] == "stop_server"] == [], name
    for name, listed, roles in (
        ("shared", [shared], ["admin"]),
        ("external", [external], ["admin"]),
        ("advsvc", [shared], ["advsvc"]),
    ):
        executor, calls, clouds, plan, mig, settings = setup(tmp_path / name)
        clouds["dst"].network_list = listed
        clouds["dst"].roles = roles
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
        assert clouds["dst"].created[0]["networks"][0]["uuid"] == listed[0].id, name


async def test_the_handover_port_check_follows_the_policy_profile(tmp_path):
    """SDD §7.3 step 0, §6.6, §21.5: another project's shared network takes the port from the
    roles the destination's policy profile counts - service under secure-RBAC, advsvc only under
    the legacy rules, admin alone when the profile is unknown."""
    shared = NS(
        id="shared-net",
        name="rhoso-app",
        project_id="admin-project",
        is_shared=True,
        is_router_external=False,
    )
    for name, roles, profile, refusal in (
        (
            "secure advsvc",
            ["advsvc"],
            "secure_rbac",
            "secure-RBAC defaults let only the admin or service role",
        ),
        ("legacy service", ["service"], "legacy", "Neutron lets only the admin or advsvc role"),
        ("unknown service", ["service"], "unknown", "the policy profile of RHOSO 18.0 is unknown"),
    ):
        executor, calls, clouds, plan, mig, settings = setup(tmp_path / name)
        clouds["dst"].network_list = [shared]
        clouds["dst"].roles = roles
        destination = DST.model_copy(update={"capabilities": {"policy_profile": profile}})
        with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
            await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, destination, settings)[0])
        assert refusal in str(refused.value), name
    for name, roles, profile in (
        ("secure service", ["service"], "secure_rbac"),
        ("unknown admin", ["admin"], "unknown"),
    ):
        executor, calls, clouds, plan, mig, settings = setup(tmp_path / name)
        clouds["dst"].network_list = [shared]
        clouds["dst"].roles = roles
        destination = DST.model_copy(update={"capabilities": {"policy_profile": profile}})
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, destination, settings)[0])
        assert clouds["dst"].created[0]["networks"][0]["uuid"] == "shared-net", name


async def test_the_handover_judges_the_declared_profile_after_the_report_was_reset(tmp_path):
    """Review RV18-04: with the destination's check report reset to {} (new credentials, allowed
    while a plan runs), step 0 judged it legacy and let an advsvc-only token put the port on
    another project's network of a declared secure-RBAC cloud - refused by Neutron after the
    source stopped. It reads the provider's declarations (SDD §7.3 step 0, §21.5)."""
    shared = NS(
        id="shared-net",
        name="rhoso-app",
        project_id="admin-project",
        is_shared=True,
        is_router_external=False,
    )
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["dst"].network_list = [shared]
    clouds["dst"].roles = ["advsvc"]
    destination = DST.model_copy(update={"policy_profile": "secure_rbac", "capabilities": {}})
    with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, destination, settings)[0])
    assert "secure-RBAC defaults let only the admin or service role" in str(refused.value)


async def test_the_handover_judges_unreadable_token_roles_as_the_kit_does(tmp_path):
    """SDD §7.3 step 0, §7.2 (review RV20-07): when the token's roles cannot be read, step 0
    judges the destination check report's; when neither names any, a port that needs a role is
    refused before the stop - a report new credentials reset never lets a token through - while
    the credential's own network, which needs none, still takes the port."""
    shared = NS(
        id="shared-net",
        name="rhoso-app",
        project_id="admin-project",
        is_shared=True,
        is_router_external=False,
    )
    external = NS(
        id="ext-net",
        name="rhoso-app",
        project_id="admin-project",
        is_shared=False,
        is_router_external=True,
    )

    def unreadable(session):
        raise AttributeError("this auth plugin has no access info")

    def run_with(name, listed, capabilities):
        executor, calls, clouds, plan, mig, settings = setup(tmp_path / name)
        if listed is not None:
            clouds["dst"].network_list = listed
        clouds["dst"].session = NS(auth=NS(get_access=unreadable))
        destination = DST.model_copy(update={"name": "DC3 Jakarta", "capabilities": capabilities})
        ctx = make_ctx(plan, mig, SRC, destination, settings)[0]
        return executor, calls, clouds, ctx

    # it names the provider to check (SDD §1 naming, review RV23-07)
    unread = (
        "the credential's token roles could not be read, and a port there needs a role: check "
        "DC3 Jakarta, then retry"
    )
    for name, listed, capabilities in (
        ("reset report", [shared], {}),
        ("report without roles", [shared], {"policy_profile": "legacy"}),
        ("external", [external], {}),
    ):
        executor, calls, clouds, ctx = run_with(name, listed, capabilities)
        with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
            await executor.run(StepName.CUTOVER, ctx)
        assert unread in str(refused.value), name
        assert "port-1" in str(refused.value), name
        assert [op for op in ops(calls) if op[1] in ("stop_server", "delete_server")] == [], name
    # the check report's roles judge the token instead: a member is refused, an admin goes through
    executor, calls, clouds, ctx = run_with("report member", [shared], {"roles": ["member"]})
    with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
        await executor.run(StepName.CUTOVER, ctx)
    assert "Neutron lets only the admin or advsvc role" in str(refused.value)
    executor, calls, clouds, ctx = run_with("report admin", [shared], {"roles": ["admin"]})
    await executor.run(StepName.CUTOVER, ctx)
    assert clouds["dst"].created[0]["networks"][0]["uuid"] == "shared-net"
    # the token's roles read for the run win over the report's: a member is refused although the
    # report still names admin
    executor, calls, clouds, plan, mig, settings = setup(tmp_path / "live member")
    clouds["dst"].network_list = [shared]
    clouds["dst"].roles = ["member"]
    stale = DST.model_copy(update={"capabilities": {"roles": ["admin"]}})
    with pytest.raises(PermanentStepError, match="storage handover refused") as refused:
        await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, stale, settings)[0])
    assert "Neutron lets only the admin or advsvc role" in str(refused.value)
    # the credential's own network needs no role: unreadable roles never refuse it
    executor, calls, clouds, ctx = run_with("own", None, {})
    await executor.run(StepName.CUTOVER, ctx)
    assert clouds["dst"].created


async def test_handover_step_0_refuses_an_address_neutron_would_refuse(tmp_path):
    """SDD §7.3 step 0 and §6.6: each address step 8 gives a port must be one Neutron takes on
    that network - a host address of its subnets; on a SLAAC subnet only the kept MAC's EUI-64
    address in "neutron" mode, and no explicit address through Nova (a new MAC)."""
    from seamless_migrate.domain.models import NicOverride

    net = "dst-net-rhoso-app"

    async def cutover(name, subnets, plan_of=lambda p: p):
        executor, calls, clouds, plan, mig, settings = setup(tmp_path / name)
        clouds["dst"].subnet_list = subnets
        ctx = make_ctx(plan_of(plan), mig, SRC, DST, settings)[0]
        try:
            await executor.run(StepName.CUTOVER, ctx)
        except PermanentStepError as exc:
            assert [op for op in ops(calls) if op[1] == "stop_server"] == [], name
            return str(exc)
        return None

    v4 = subnet(net, "10.0.0.0/24")
    # the port's own 10.0.0.5 (Nova takes its first address)
    assert await cutover("ok", [v4]) is None
    refused = await cutover("outside", [subnet(net, "10.9.0.0/24")])
    assert "10.0.0.5 is no host address of any subnet" in refused
    entry = NicOverride(fixed_ips=["10.0.0.0"])
    refused = await cutover("network-address", [v4], lambda p: overridden(p, entry))
    assert "10.0.0.0 is no host address" in refused
    # SLAAC: the kept MAC fa:16:3e:00:00:01 derives fd00:20::f816:3eff:fe00:1 on fd00:20::/64
    slaac = subnet(net, "fd00:20::/64", "slaac")
    eui = NicOverride(fixed_ips=["10.0.0.5", "fd00:20::f816:3eff:fe00:1"])
    other = NicOverride(fixed_ips=["10.0.0.5", "fd00:20::5"])
    assert (
        await cutover(
            "eui64", [v4, slaac], lambda p: overridden(p, eui, port_creation_mode="neutron")
        )
        is None
    )
    refused = await cutover(
        "not-eui64", [v4, slaac], lambda p: overridden(p, other, port_creation_mode="neutron")
    )
    assert "fd00:20::5 is not fd00:20::f816:3eff:fe00:1" in refused
    v6_only = NicOverride(fixed_ips=["fd00:20::f816:3eff:fe00:1"])
    refused = await cutover("nova-slaac", [v4, slaac], lambda p: overridden(p, v6_only))
    assert "SLAAC or DHCPv6-stateless" in refused


async def test_handover_step_0_looks_up_the_network_a_nic_override_names(tmp_path):
    """SDD §7.3 step 0: the port's RHOSO network is its NIC override's when the entry names one;
    the read-only report the lab runs says the same when it is given the plan."""
    from seamless_migrate.domain.models import NicOverride

    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    # the mapped rhoso-app resolves; the override's rhoso-dmz is in two other projects
    clouds["dst"].network_list = [
        NS(id="app", name="rhoso-app", project_id="dst-project"),
        NS(id="dmz-a", name="rhoso-dmz", project_id="tenant-a"),
        NS(id="dmz-b", name="rhoso-dmz", project_id="tenant-b"),
    ]
    moved = overridden(plan, NicOverride(network="rhoso-dmz"))
    report = await executor.readiness(SRC, DST, "srv-1", plan.handover.backend_map, moved)
    [problem] = [p for p in report["problems"] if "port-1" in p]
    assert "2 networks named 'rhoso-dmz', none in the credential's project" in problem
    # without the plan the report checks the volumes only, as before
    plain = await executor.readiness(SRC, DST, "srv-1", plan.handover.backend_map)
    assert not [p for p in plain["problems"] if "port-1" in p]
    with pytest.raises(PermanentStepError, match="rhoso-dmz"):
        await executor.run(StepName.CUTOVER, make_ctx(moved, mig, SRC, DST, settings)[0])
    assert [op for op in ops(calls) if op[1] == "stop_server"] == []
    # the mapped network alone is fine
    await executor.run(StepName.CUTOVER, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert clouds["dst"].created[0]["networks"] == [{"uuid": "app", "fixed_ip": "10.0.0.5"}]


async def test_handover_step_0_refuses_an_address_or_mac_another_port_holds(tmp_path):
    """SDD §7.3 step 0: Neutron refuses an address another port of the network holds, and in
    "neutron" mode the MAC; only the unbound port an earlier attempt created is used again."""

    def leftover(port_id, **change):
        base = {
            "id": port_id,
            "network_id": "dst-net-rhoso-app",
            "mac_address": "fa:16:3e:99:99:99",
            "fixed_ips": [{"ip_address": "10.0.0.5"}],
            "device_id": "other-vm",
            "created_by_nova": True,
            "description": "",
            "project_id": "dst-project",
        }
        return NS(**{**base, **change})

    async def cutover(name, ports, plan_of=lambda p: p):
        executor, calls, clouds, plan, mig, settings = setup(tmp_path / name)
        for port in ports:
            clouds["dst"].ports[port.id] = port
        try:
            await executor.run(
                StepName.CUTOVER, make_ctx(plan_of(plan), mig, SRC, DST, settings)[0]
            )
        except PermanentStepError as exc:
            assert [op for op in ops(calls) if op[1] == "stop_server"] == [], name
            return str(exc)
        return None

    refused = await cutover("nova-taken", [leftover("taken")])
    assert (
        "port port-1 (fa:16:3e:00:00:01) on 'rhoso-app' of RHOSO 18.0: 10.0.0.5 is held by port "
        "taken" in refused
    )
    refused = await cutover("neutron-taken", [leftover("taken")], neutron)
    assert "10.0.0.5 is held by port taken" in refused
    # the earlier attempt's own unbound port: step 8 uses it again
    mine = leftover(
        "mine",
        mac_address="fa:16:3e:00:00:01",
        fixed_ips=[{"ip_address": "10.0.0.5"}, {"ip_address": "fd00::5"}],
        device_id="",
        created_by_nova=False,
        description="os-migrate:port-1",
    )
    assert await cutover("neutron-mine", [mine], neutron) is None
    # another port with the MAC: refused when the MAC is kept, not through Nova (a new MAC)
    same = leftover(
        "same-mac", mac_address="fa:16:3e:00:00:01", fixed_ips=[{"ip_address": "10.0.0.77"}]
    )
    refused = await cutover("neutron-mac", [same], neutron)
    assert "MAC fa:16:3e:00:00:01 is held by port same-mac" in refused
    assert await cutover("nova-mac", [same]) is None
