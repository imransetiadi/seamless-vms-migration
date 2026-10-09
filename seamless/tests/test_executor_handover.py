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
    def __init__(self, body, status=202):
        self._body = body
        self.status_code = status

    def json(self):
        return self._body


class FakeCompute:
    def __init__(self, cloud):
        self.c = cloud

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
        return [NS(volume_id=v, device=d) for v, d in server.attachments]

    def delete_volume_attachment(self, server, volume, ignore_missing=True):
        self.c.record("detach", volume)

    def delete_server(self, server, ignore_missing=True):
        self.c.record("delete_server", getattr(server, "id", server))

    def wait_for_delete(self, res, interval=2, wait=120):
        return res

    def find_flavor(self, name, ignore_missing=True):
        return NS(id=f"flavor-{name}", name=name)

    def create_server(self, **params):
        self.c.record("create_server", params["name"])
        self.c.created.append(params)
        server = NS(
            id=f"{self.c.name}-new-{len(self.c.created)}", name=params["name"], status="ACTIVE"
        )
        self.c.servers[server.id] = server
        return server


class FakeBlockStorage:
    def __init__(self, cloud):
        self.c = cloud

    def get_volume(self, volume_id):
        return self.c.volumes[volume_id]

    def wait_for_status(self, res, status="available", failures=None, interval=2, wait=120):
        return res

    def wait_for_delete(self, res, interval=2, wait=120):
        return res

    def backend_pools(self):
        if self.c.pools is None:
            raise RuntimeError("HTTP 403: Policy doesn't allow scheduler_extension:scheduler_stats")
        return [NS(name=name, capabilities=caps) for name, caps in self.c.pools]

    def post(self, url, json=None, **kw):
        if url.endswith("/action") and "os-unmanage" in json:
            vid = url.split("/")[2]
            self.c.record("unmanage", vid)
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
        ),
        "vol-data": NS(
            id="vol-data",
            name="web-01-data",
            size=100,
            volume_type="ceph-hdd",
            is_bootable=False,
            host="overcloud@tripleo_ceph#hdd",
            status="in-use",
        ),
    }
    pools = [("overcloud@tripleo_ceph#ssd", CEPH), ("overcloud@tripleo_ceph#hdd", CEPH)]
    return FakeCloud("src", calls, {"srv-1": server}, volumes, crash_on=crash_on, pools=pools)


def setup(tmp_path, crash_on=None):
    calls = []
    dst_pools = [("hostgroup@ceph-ssd#ssd", CEPH), ("hostgroup@ceph-hdd#hdd", CEPH)]
    clouds = {
        "src": source_cloud(calls, crash_on),
        "dst": FakeCloud("dst", calls, pools=dst_pools),
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
    assert ops(calls) == [
        ("src", "stop_server", "srv-1"),
        ("src", "detach", "vol-data"),
        ("src", "unmanage", "vol-data"),
        ("src", "unmanage", "vol-root"),
        ("src", "delete_server", "srv-1"),
        ("dst", "manage", "volume-vol-data"),
        ("dst", "manage", "volume-vol-root"),
        ("dst", "create_server", "web-01"),
    ]
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


async def test_handover_journal_resume_skips_done_steps(tmp_path):
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="delete_server")
    ctx, rec = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="delete the source server"):
        await executor.run(StepName.CUTOVER, ctx)
    journal = json.loads((executor.run_dir(ctx) / JOURNAL_FILE).read_text())
    assert {
        "stop_source",
        "save_definition",
        "detach:vol-data",
        "unmanage_src:vol-data",
        "unmanage_src:vol-root",
    } <= set(journal["done"])
    assert "delete_source" not in journal["done"]
    first = list(calls)
    calls.clear()

    # control plane restarts; a fresh executor resumes from the journal
    executor2 = HandoverExecutor(settings, conn_factory=lambda p: clouds[p.cloud], poll_s=0)
    ctx2, rec2 = make_ctx(plan, mig, SRC, DST, settings)
    result = await executor2.run(StepName.CUTOVER, ctx2)
    assert ops(calls) == [
        ("src", "delete_server", "srv-1"),
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
    ] == [(0, False), (-1, False)]
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


async def test_handover_rollback_before_unmanage_reattaches_and_starts(tmp_path):
    """Crash after the first detach: the rollback reattaches that volume and starts the source."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="unmanage")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="unmanage"):
        await executor.run(StepName.CUTOVER, ctx)
    calls.clear()
    attached = []
    clouds["src"].compute.create_volume_attachment = lambda server_id, volume_id, device: (
        attached.append((server_id, volume_id, device))
    )
    rb_ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    result = await executor.run(StepName.ROLLBACK, rb_ctx)
    assert attached == [("srv-1", "vol-data", "/dev/vdb")]
    assert ops(calls) == [("src", "start_server", "srv-1")]
    assert result.details == {"source_running": True}, "same ids: nothing to report"
    assert not (executor.run_dir(rb_ctx) / JOURNAL_FILE).exists()


async def test_handover_rollback_after_unmanage_deletes_stale_source_and_recreates(tmp_path):
    """Crash before the source server was deleted: both volumes are already unmanaged at the
    source, so the rollback manages them back, removes the stale server and recreates it."""
    executor, calls, clouds, plan, mig, settings = setup(tmp_path, crash_on="delete_server")
    ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    with pytest.raises(PermanentStepError, match="delete the source server"):
        await executor.run(StepName.CUTOVER, ctx)
    calls.clear()
    rb_ctx, _ = make_ctx(plan, mig, SRC, DST, settings)
    result = await executor.run(StepName.ROLLBACK, rb_ctx)
    assert ops(calls) == [
        ("src", "manage", "volume-vol-root"),
        ("src", "manage", "volume-vol-data"),
        ("src", "delete_server", "srv-1"),
        ("src", "create_server", "web-01"),
    ]
    new_vm = result.details["vm"]
    assert new_vm["source_id"] == "src-new-1"
    assert {d["id"] for d in new_vm["disks"]} == {"src-vol-1", "src-vol-2"}
    # a second rollback finds the archived journal gone and does nothing
    calls.clear()
    again = await executor.run(StepName.ROLLBACK, make_ctx(plan, mig, SRC, DST, settings)[0])
    assert calls == [] and again.details["note"] == "nothing to roll back"


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
