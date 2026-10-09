import json
from types import SimpleNamespace as NS

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Strategy
from seamless_migrate.domain.models import HandoverConfig, Mappings
from seamless_migrate.executors.base import PermanentStepError, StepName
from seamless_migrate.executors.handover import JOURNAL_FILE, HandoverExecutor
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
        return self.c.servers[server_id]

    def stop_server(self, server):
        self.c.record("stop_server", server.id)
        server.status = "SHUTOFF"

    def start_server(self, server):
        self.c.record("start_server", server.id)

    def wait_for_server(self, server, status="ACTIVE", failures=None, interval=2, wait=120):
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
        self.c.servers[server.id] = server
        return server


class FakeBlockStorage:
    def __init__(self, cloud):
        self.c = cloud

    def get_volume(self, volume_id):
        if volume_id not in self.c.volumes:
            raise HTTPError(f"HTTP 404: volume {volume_id} not found")
        return self.c.volumes[volume_id]

    def snapshots(self, details=True, **query):
        return [
            NS(id=f"snap-{i}", volume_id=query.get("volume_id"))
            for i in range(getattr(self.c.volumes.get(query.get("volume_id")), "snapshots", 0))
        ]

    def wait_for_status(self, res, status="available", failures=None, interval=2, wait=120):
        if getattr(res, "status", status) != status:
            raise HTTPError(f"timeout waiting for {res.id} to be {status} (is {res.status})")
        return res

    def wait_for_delete(self, res, interval=2, wait=120):
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
    def __init__(self, cloud):
        self.c = cloud

    def ports(self, device_id=None):
        return [NS(network_id="net-1", fixed_ips=[{"ip_address": "10.0.0.5"}])]

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
        self.created = []
        self.managed = []
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
    return FakeCloud("src", calls, {"srv-1": server}, volumes, crash_on=crash_on, pools=pools)


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
    assert ops(calls) == [("src", "create_server", "web-01")]
    bdm = clouds["src"].created[0]["block_device_mapping"]
    assert [(b["uuid"], b["boot_index"], b["delete_on_termination"]) for b in bdm] == [
        ("vol-root", 0, True),
        ("vol-data", -1, False),
    ]
    assert result.details["vm"]["source_id"] == "src-new-1"


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
    assert ops(calls) == [("src", "create_server", "web-01")]
    assert result.details["source_running"] is True


async def test_handover_fails_when_rhoso_refuses_the_boot_properties(tmp_path):
    executor, calls, clouds, plan, mig, settings = setup(tmp_path)
    clouds["src"].volumes["vol-root"].volume_image_metadata = {"hw_firmware_type": "uefi"}
    clouds["dst"].refuse_metadata = True
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="boot properties.*HTTP 400"):
        await executor.run(StepName.CUTOVER, ctx)
    assert all(op != "create_server" for _, op, _ in ops(calls)), "no server without its UEFI"


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
