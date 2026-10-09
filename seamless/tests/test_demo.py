import pytest

from seamless_migrate.api.app import build_services
from seamless_migrate.config import Settings
from seamless_migrate.demo import DEMO_PLAN_NAMES, seed_demo
from seamless_migrate.domain.enums import Phase, PlanStatus, ProviderKind, ProviderRole
from seamless_migrate.domain.models import Mappings, Migration, Plan, Provider
from seamless_migrate.orchestrator import NotAllowed
from seamless_migrate.providers.fake import FakeSourceProvider


async def test_demo_seed_idempotent(tmp_path, store):
    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)

    providers = store.list("provider", Provider)
    assert len(providers) == 3
    # a demo database seeded before Distribution existed is backfilled on the next seed
    legacy = next(p for p in providers if p.id == "vcenter-dc2")
    _, version = store.get_versioned("provider", legacy.id, Provider)
    unset = legacy.model_copy(update={"distribution": None})
    store.put("provider", unset, expected_version=version)
    await seed_demo(store, settings, services.orchestrator)
    providers = store.list("provider", Provider)
    assert {p.id: p.distribution for p in providers} == {
        "rhosp17-finance": "rhosp",
        "vcenter-dc2": "vmware",
        "rhoso18": "rhoso",
    }
    assert sum(p.role == ProviderRole.destination for p in providers) == 1
    plans = store.list("plan", Plan)
    assert sorted(p.name for p in plans) == sorted(DEMO_PLAN_NAMES)
    assert DEMO_PLAN_NAMES == ("Finance apps (RHOSP 17.1 → RHOSO)", "DC2 VMware exit")
    assert all(p.status == PlanStatus.running and p.waves for p in plans)
    migrations = store.list("migration", Migration)
    # report-01 and batch-01 stay unplanned, so a new plan can take them (SDD §10, §5.4)
    assert len(migrations) == 22 + 12
    planned = {m.vm.name for m in migrations}
    assert not planned & {"report-01", "batch-01"}
    finance = next(p for p in plans if p.source_provider_id == "rhosp17-finance")
    assert len(finance.vm_ids) == 22
    assert not [m for m in migrations if m.phase == Phase.blocked]
    cancelled = [m for m in migrations if m.phase == Phase.cancelled]
    assert [m.vm.name for m in cancelled] == ["gpu-ml-01"]
    assert Phase.blocked in [c.to_phase for c in cancelled[0].phase_history]
    strategies = {m.strategy for m in migrations if m.phase != Phase.cancelled}
    assert {"cold", "warm", "vmware_warm", "vmware_cold"} <= {str(s) for s in strategies}

    events_before = store.max_seq()
    await seed_demo(store, settings, services.orchestrator)
    assert len(store.list("provider", Provider)) == 3
    assert len(store.list("plan", Plan)) == 2
    assert len(store.list("migration", Migration)) == 34
    assert store.max_seq() == events_before, "a second seed changes nothing"


async def test_demo_runs_through_phases(tmp_path, store):
    """With the orchestrator running, demo plans make progress on their own."""
    import asyncio

    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0, tick_s=0.01
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    await services.orchestrator.start()
    try:
        for _ in range(500):
            phases = {m.phase for m in store.list("migration", Migration)}
            if Phase.completed in phases and Phase.awaiting_cutover in phases:
                break
            await asyncio.sleep(0.02)
    finally:
        await services.orchestrator.stop()
    assert Phase.completed in phases and Phase.awaiting_cutover in phases


async def test_demo_clouds_report_netapp_and_ceph_storage_backends():
    """The demo shows a RHOSP 17.1 source on Ceph and NetApp ONTAP NFS (SDD §7.3.1, §10)."""
    from seamless_migrate.domain.enums import ProviderKind, Strategy
    from seamless_migrate.domain.models import HandoverConfig
    from seamless_migrate.planning.selector import eligibility
    from seamless_migrate.providers.fake import FakeDestinationProvider, FakeSourceProvider
    from tests.factories import make_plan

    src = FakeSourceProvider(ProviderKind.openstack, seed=42)
    dst = FakeDestinationProvider(seed=42)
    src_caps, dst_caps = await src.check(), await dst.check()
    families = {b["family"] for b in src_caps["storage_backends"]}
    assert families == {"rbd", "netapp_nfs"}
    assert {b["family"] for b in dst_caps["storage_backends"]} == {"rbd", "netapp_nfs"}
    assert src_caps["volume_backends"] == [b["pool"] for b in src_caps["storage_backends"]]

    vms = {vm.name: vm for vm in await src.list_vms()}
    pools = {b["pool"] for b in src_caps["storage_backends"]}
    volumes = [d for vm in vms.values() for d in vm.disks if d.kind == "volume"]
    assert volumes and all(d.pool in pools for d in volumes)
    netapp = [
        name for name, vm in vms.items() if any(d.volume_type == "netapp-nfs" for d in vm.disks)
    ]
    assert netapp == ["portal-01", "app-01", "app-02"]
    assert "netapp-nfs" in (await dst.inventory()).volume_types

    # a handover plan mapping both families is eligible on the demo clouds
    plan = make_plan(
        handover=HandoverConfig(
            enabled=True,
            backend_map={
                "netapp-nfs": "hostgroup@ontap-nfs",
                "ceph-ssd": "hostgroup@ceph-ssd#ssd",
                "ceph-hdd": "hostgroup@ceph-hdd#hdd",
            },
        )
    )
    reasons = eligibility(vms["app-01"], ProviderKind.openstack, plan, src_caps, dst_caps)
    assert reasons[Strategy.storage_handover] == []


async def test_demo_estate_covers_the_guest_os_range():
    """The demo shows every guest family and support level of SDD §9.5."""
    from seamless_migrate.domain.enums import ProviderKind
    from seamless_migrate.providers.fake import FakeSourceProvider

    openstack = await FakeSourceProvider(ProviderKind.openstack, seed=42).list_vms()
    vmware = await FakeSourceProvider(ProviderKind.vmware, seed=42).list_vms()
    distros = {vm.guest_os.distro for vm in openstack + vmware}
    assert {
        "rhel",
        "ubuntu",
        "debian",
        "rocky",
        "almalinux",
        "centos",
        "windows-server",
    } <= distros
    assert {vm.guest_os.lifecycle for vm in openstack} >= {"current", "legacy"}
    assert {vm.guest_os.v2v for vm in vmware} >= {"supported", "tech_preview", "unsupported"}
    assert all(vm.guest_os.family != "unknown" for vm in openstack + vmware)


async def test_demo_leaves_finance_vms_a_new_plan_can_take(tmp_path, store):
    """SDD §10: the finance plan holds its VMs for good once it completes (§5.4), so the seed leaves
    report-01 and batch-01 unplanned: a new plan validates with them, and is refused with a VM the
    finance plan holds."""
    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    vms = await FakeSourceProvider(ProviderKind.openstack, settings.demo_seed).list_vms()
    ids = {vm.name: vm.source_id for vm in vms}
    mappings = Mappings(
        networks={"finance-app": "finance-app", "finance-db": "finance-db"},
        volume_types={"ceph-ssd": "ceph-ssd", "ceph-hdd": "ceph-hdd"},
    )

    def new_plan(*names: str) -> Plan:
        plan = Plan(
            name="new",
            source_provider_id="rhosp17-finance",
            destination_provider_id="rhoso18",
            vm_ids=[ids[name] for name in names],
            mappings=mappings,
        )
        store.put("plan", plan)
        return plan

    free = new_plan("report-01", "batch-01")
    report = await services.orchestrator.validate_plan(free.id, "tester")
    assert sorted(item.vm_name for item in report.migrations) == ["batch-01", "report-01"]
    with pytest.raises(NotAllowed, match="another plan"):
        await services.orchestrator.validate_plan(new_plan("web-01").id, "tester")
