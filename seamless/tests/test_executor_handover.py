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


class FakeCloud:
    def __init__(self, name, calls, servers=None, volumes=None, crash_on=None):
        self.name = name
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
    return FakeCloud("src", calls, {"srv-1": server}, volumes, crash_on=crash_on)


def setup(tmp_path, crash_on=None):
    calls = []
    clouds = {"src": source_cloud(calls, crash_on), "dst": FakeCloud("dst", calls)}
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
