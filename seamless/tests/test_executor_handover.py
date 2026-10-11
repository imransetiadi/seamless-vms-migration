import ast
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Strategy
from seamless_migrate.domain.models import HandoverConfig, Mappings
from seamless_migrate.executors.base import PermanentStepError, StepName
from seamless_migrate.executors.handover import (
    JOURNAL_FILE,
    HandoverExecutor,
    _cinder_bootable,
    _mark_boot,
    port_description,
)
from tests.executor_support import make_ctx
from tests.factories import make_disk, make_migration, make_plan, make_provider, make_vm

SRC = make_provider(cloud="src")
DST = make_provider(
    id="dst-rhoso", kind=ProviderKind.rhoso, role=ProviderRole.destination, cloud="dst"
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

    def find_flavor(self, name, ignore_missing=True):
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

    def ports(self, device_id=None, description=None):
        return [
            p
            for p in self.c.ports.values()
            if (device_id is None or p.device_id == device_id)
            and (description is None or getattr(p, "description", "") == description)
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

    def get_network(self, network_id):
        return NS(id=network_id, name="app-net")

    def find_network(self, name, ignore_missing=True):
        return NS(id=f"{self.c.name}-net-{name}", name=name)


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
        self.current_project_id = f"{name}-project"
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


async def test_handover_in_neutron_mode_resumes_on_the_port_it_created(tmp_path):
    """A crash after the port and before the server: the resume boots on the journaled port, and
    a port created but not yet journaled is found by its description, unbound."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, dst_crash_on="create_server")
    ctx, _ = make_ctx(neutron(plan), mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="create the destination server"):
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
        await executor.run(StepName.CUTOVER, make_ctx(neutron(plan), mig, SRC, DST, settings)[0])
        [created] = clouds["dst"].port_attrs
        assert created["network_id"] == "dst-net-rhoso-app", name
        assert clouds["dst"].created[0]["networks"] != [{"port": name}], name


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
    override's network and addresses, in any order, the MAC in any case; a port with the NIC's
    own addresses, or on the mapped network, is not its port."""
    from seamless_migrate.domain.models import NicOverride

    entry = NicOverride(network="rhoso-dmz", fixed_ips=["192.168.10.5", "fd00:10::5"])
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    leftover = {
        "mac_address": "fa:16:3e:00:00:01",
        "device_id": "",
        "created_by_nova": False,
        "description": "os-migrate:port-1",
        "project_id": "dst-project",
    }
    clouds["dst"].ports["own-addresses"] = NS(
        id="own-addresses",
        network_id="dst-net-rhoso-dmz",
        fixed_ips=[{"ip_address": "10.0.0.5"}, {"ip_address": "fd00::5"}],
        **leftover,
    )
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
    with pytest.raises(PermanentStepError, match="create the destination server"):
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
    with pytest.raises(PermanentStepError, match="create the destination server"):
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
    with pytest.raises(PermanentStepError, match="create the destination server"):
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
    with pytest.raises(PermanentStepError, match="manage vol-data in RHOSO"):
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
    with pytest.raises(PermanentStepError, match="manage vol-data in RHOSO"):
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
    with pytest.raises(PermanentStepError, match="manage vol-data in RHOSO"):
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
    # a second rollback finds the archived journal gone and does nothing
    calls.clear()
    again = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert calls == [] and again.details["note"] == "nothing to roll back"


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
        (None, "Cinder pools of the destination"),
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
