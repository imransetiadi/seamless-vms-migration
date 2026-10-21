import asyncio

import pytest

from seamless_migrate.api.app import build_services
from seamless_migrate.config import Settings
from seamless_migrate.demo import DEMO_PLAN_NAMES, seed_demo
from seamless_migrate.domain.enums import Phase, PlanStatus, ProviderKind, ProviderRole
from seamless_migrate.domain.models import AdvisorPolicy, Mappings, Migration, Plan, Provider
from seamless_migrate.orchestrator import NotAllowed
from seamless_migrate.providers.fake import FakeSourceProvider


async def test_demo_seed_idempotent(tmp_path, store):
    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)

    providers = store.list("provider", Provider)
    assert len(providers) == 4
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
        "hibiscus-dc3": "kolla",
    }
    assert sum(p.role == ProviderRole.destination for p in providers) == 2
    plans = store.list("plan", Plan)
    assert sorted(p.name for p in plans) == sorted(DEMO_PLAN_NAMES)
    assert DEMO_PLAN_NAMES == ("Finance apps (RHOSP 17.1 → RHOSO)", "DC2 VMware exit")
    assert all(p.status == PlanStatus.running and p.waves for p in plans)
    migrations = store.list("migration", Migration)
    # report-01 and batch-01 stay unplanned, so a new plan can take them (SDD §10, §5.4), and
    # dc2-portal-02, dc2-ml-02 and dc2-cuda-01 for the operator to plan (§7.4, §22.10)
    assert len(migrations) == 22 + 13
    planned = {m.vm.name for m in migrations}
    assert not planned & {"report-01", "batch-01", "dc2-portal-02", "dc2-ml-02", "dc2-cuda-01"}
    finance = next(p for p in plans if p.source_provider_id == "rhosp17-finance")
    assert len(finance.vm_ids) == 22
    # gpu-ml-01's GPU mapping applies (§22.10): no demo VM is blocked, so the seed cancels none
    assert not [m for m in migrations if m.phase in (Phase.blocked, Phase.cancelled)]
    strategies = {m.strategy for m in migrations}
    assert {"cold", "warm", "vmware_warm", "vmware_cold"} <= {str(s) for s in strategies}

    events_before = store.max_seq()
    await seed_demo(store, settings, services.orchestrator)
    assert len(store.list("provider", Provider)) == 4
    assert len(store.list("plan", Plan)) == 2
    assert len(store.list("migration", Migration)) == 35
    assert store.max_seq() == events_before, "a second seed changes nothing"


async def test_demo_seeds_a_community_destination_beside_rhoso(tmp_path, store):
    """SDD §10, plan T9: the demo holds a community OpenStack destination - 2026.2 Hibiscus, the
    Kolla-Ansible preset - beside RHOSO 18.0. Its check reports its series, Tier 1 and the
    secure-RBAC profile; RHOSO's stays validated and legacy; the demo plans keep rhoso18."""
    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)

    hibiscus = store.get("provider", "hibiscus-dc3", Provider)
    assert (hibiscus.kind, hibiscus.role, hibiscus.distribution, hibiscus.release) == (
        ProviderKind.openstack,
        ProviderRole.destination,
        "kolla",
        "2026.2",
    )
    assert {p.destination_provider_id for p in store.list("plan", Plan)} == {"rhoso18"}
    caps = (await services.orchestrator.check_provider("hibiscus-dc3")).capabilities
    assert caps["compute_microversion"] == "2.104"
    assert caps["release_range"] == ["2026.2"] and caps["release"] == "2026.2"
    assert caps["support_tier"] == {
        "tier": "tier1",
        "reason": None,
        "declared_outside_range": False,
    }
    assert caps["policy_profile"] == "secure_rbac" and caps["tls"] is True
    rhoso = (await services.orchestrator.check_provider("rhoso18")).capabilities
    assert rhoso["support_tier"]["tier"] == "validated" and rhoso["policy_profile"] == "legacy"
    assert rhoso["compute_microversion"] == "2.95"


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


async def test_demo_seeds_the_finance_approval_policy_once(tmp_path, store):
    """SDD §14.6 (ADV-04): 'Finance — approval required' for the finance source provider, before
    the demo plans are validated, and only once."""
    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    await seed_demo(store, settings, services.orchestrator)
    [policy] = store.list("advisor_policy", AdvisorPolicy)
    assert (policy.name, policy.scope.provider_ids, policy.mode) == (
        "Finance — approval required",
        ["rhosp17-finance"],
        "always_require_approval",
    )
    assert policy.require_override_reason and policy.decision_types == ["migration_strategy"]


async def test_the_demo_vmware_plan_has_cutovers_requested_and_approved_by_two_people(
    tmp_path, store
):
    """ADV-17: the seeded approvals read like an operator's — a requester, another approver and a
    comment — and the seed starts nothing a person did not ask for twice."""
    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    vmware = next(p for p in store.list("plan", Plan) if p.source_provider_id == "vcenter-dc2")
    assert vmware.require_approval and vmware.require_distinct_approver
    requested = [
        m for m in store.list("migration", Migration, plan_id=vmware.id) if m.cutover_requested_by
    ]
    assert requested, "at least one requested cutover"
    for m in requested:
        assert m.approvals, m.vm.name
        assert all(a.actor != m.cutover_requested_by for a in m.approvals), m.vm.name
        assert all(a.comment and len(a.comment) >= 20 for a in m.approvals), m.vm.name
    assert len(requested) < len(vmware.vm_ids), "others still wait for an approver"


def test_the_demo_seed_carries_no_placeholder_strings():
    """ADV-17: no test strings such as 'demo46' in the seed (grep test)."""
    import re
    from pathlib import Path

    import seamless_migrate.demo as demo
    import seamless_migrate.providers.fake as fake

    placeholder = re.compile(
        r"\bdemo\d+\b|\btest\d+\b|\b(foo|bar|baz|qux|asdf|qwerty|lorem|ipsum|dummy)\b|xxx+",
        re.IGNORECASE,
    )
    for module in (demo, fake):
        text = Path(module.__file__).read_text(encoding="utf-8")
        found = [match.group(0) for match in placeholder.finditer(text)]
        assert found == [], module.__name__


async def test_every_demo_migration_keeps_the_invariants(tmp_path, store):
    """ADV-16 property test (SDD §5.5): run the demo seed to the end; no migration breaks an
    invariant, and a warm migration's downtime is never shorter than its final sync."""
    import asyncio

    from seamless_migrate.invariants import check_invariants

    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e6, demo_failure_rate=0.0, tick_s=0.01
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    orch = services.orchestrator
    await orch.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while loop.time() < deadline:
            migrations = store.list("migration", Migration)
            for m in migrations:
                if m.phase in (Phase.ready, Phase.awaiting_cutover) and not m.cutover_requested:
                    try:  # it may have moved on since the list was read
                        await orch.request_cutover(m.id, "sari")
                        await orch.approve(m.id, "rina", "Checked for the property test")
                    except NotAllowed:
                        continue
            # billing-db-01's service is down on purpose and it rolls back (ADV-18)
            if all(
                m.phase in (Phase.completed, Phase.cancelled, Phase.rolled_back) for m in migrations
            ):
                break
            await asyncio.sleep(0.05)
    finally:
        await orch.stop()
    plans = {p.id: p for p in store.list("plan", Plan)}
    completed = [m for m in store.list("migration", Migration) if m.phase == Phase.completed]
    assert len(completed) >= 30
    for m in completed:
        assert m.data_issues == [], (m.vm.name, m.data_issues)
        assert check_invariants(m, plans[m.plan_id], settings) == [], m.vm.name
        final = [p for p in m.sync_passes if p.kind == "final"]
        if final:
            assert m.actual_downtime_s >= final[-1].duration_s, m.vm.name


async def test_the_demo_probes_its_finance_databases_and_rolls_back_the_one_whose_service_is_down(
    tmp_path, store
):
    """ADV-18 (SDD §7.4): the finance plan probes its three databases with their suggested probes;
    billing-db-01 carries `demo-fault: app-down`, so its probe fails after boot and the plan's
    automatic rollback rolls it back; the VMware plan probes nothing, its databases are flagged."""
    import asyncio

    from seamless_migrate.demo import FINANCE_PLAN, VMWARE_PLAN
    from seamless_migrate.domain.models import AppProbe

    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e6, demo_failure_rate=0.0, tick_s=0.01
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    plans = {p.name: p for p in store.list("plan", Plan)}
    finance, vmware = plans[FINANCE_PLAN], plans[VMWARE_PLAN]
    names = {m.vm.source_id: m.vm.name for m in store.list("migration", Migration)}
    assert {names[vm_id]: probes for vm_id, probes in finance.probe_overrides.items()} == {
        "ledger-db-01": [AppProbe(kind="tcp", port=5432)],
        "payroll-db-01": [AppProbe(kind="tcp", port=3306)],
        "billing-db-01": [AppProbe(kind="tcp", port=1521)],
    }
    assert vmware.probe_overrides == {} and vmware.verification.app_probes == []
    flagged = sorted(
        m.vm.name
        for m in store.list("migration", Migration)
        if m.plan_id == vmware.id and "APP_PROBE_MISSING" in [f.code for f in m.findings]
    )
    assert flagged == ["dc2-db-01", "dc2-db-02"]

    orch = services.orchestrator
    await orch.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while loop.time() < deadline:
            by_name = {
                m.vm.name: m for m in store.list("migration", Migration) if m.plan_id == finance.id
            }
            if (
                by_name["billing-db-01"].phase == Phase.rolled_back
                and by_name["ledger-db-01"].phase == Phase.completed
            ):
                break
            await asyncio.sleep(0.05)
    finally:
        await orch.stop()
    billing, ledger = by_name["billing-db-01"], by_name["ledger-db-01"]
    assert billing.phase == Phase.rolled_back, billing.phase
    assert billing.verification is not None and billing.verification.passed is False
    assert [(p.kind, p.result) for p in billing.verification.probes] == [("tcp", "failed")]
    assert billing.verification.probes[0].target.endswith(":1521")
    assert ledger.phase == Phase.completed and ledger.review_required is False
    assert [p.result for p in ledger.verification.probes] == ["passed"]


async def test_the_demo_keeps_finished_sources_stopped_and_flags_one_powered_on(tmp_path, store):
    """ADV-19 (SDD §8.1): a simulated cutover stops the fake source; a restart keeps it stopped; the
    demo power route turns one on, and the next source watch flags its migration."""
    import asyncio

    settings = Settings(
        data_dir=tmp_path / "data",
        demo=True,
        demo_speed=1e6,
        demo_failure_rate=0.0,
        tick_s=0.01,
        source_watch_s=0.0,
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    orch = services.orchestrator
    await orch.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while loop.time() < deadline:
            done = [m for m in store.list("migration", Migration) if m.phase == Phase.completed]
            if len(done) >= 5:
                break
            await asyncio.sleep(0.05)
        # nothing powered on: no completed migration is flagged
        await asyncio.sleep(0.1)
        assert done and all(m.source_running_at is None for m in store.list("migration", Migration))
        target = done[0]
        plan = next(p for p in store.list("plan", Plan) if p.id == target.plan_id)
        source = await orch.source_client(plan)
        assert (await source.get_vm(target.vm.source_id)).power_state == "stopped"
        await source.power_on(target.vm.source_id)
        for _ in range(100):
            flagged = next(m for m in store.list("migration", Migration) if m.id == target.id)
            if flagged.source_running_at is not None:
                break
            await asyncio.sleep(0.05)
        assert flagged.source_running_at is not None
    finally:
        await orch.stop()

    # a restart builds new fakes: the seed stops the finished sources again, the powered-on too
    restarted = build_services(settings, store)
    await seed_demo(store, settings, restarted.orchestrator)
    plan = next(p for p in store.list("plan", Plan) if p.id == target.plan_id)
    source = await restarted.orchestrator.source_client(plan)
    others = [m for m in done[1:] if m.plan_id == plan.id]
    for m in others:
        assert (await source.get_vm(m.vm.source_id)).power_state == "stopped", m.vm.name


async def test_a_demo_plan_with_the_dashboards_default_tcp_ports_verifies(tmp_path, store):
    """SDD §7.4: the New plan dialog verifies TCP 22 (3389 on Windows) by default; the demo's guests
    answer those checks, so a plan made from the dashboard completes instead of rolling back on a
    real connection to a simulated address."""
    import asyncio

    from seamless_migrate.domain.models import VerificationConfig

    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e6, demo_failure_rate=0.0, tick_s=0.01
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    vms = await FakeSourceProvider(ProviderKind.openstack, settings.demo_seed).list_vms()
    report = next(vm for vm in vms if vm.name == "report-01")
    plan = Plan(
        name="from the dashboard",
        source_provider_id="rhosp17-finance",
        destination_provider_id="rhoso18",
        vm_ids=[report.source_id],
        mappings=Mappings(
            networks={"finance-app": "finance-app", "finance-db": "finance-db"},
            volume_types={"ceph-ssd": "ceph-ssd", "ceph-hdd": "ceph-hdd"},
        ),
        require_approval=False,
        auto_cutover=True,
        verification=VerificationConfig(tcp_ports=[22], windows_tcp_ports=[3389], timeout_s=60),
    )
    store.put("plan", plan)
    orch = services.orchestrator
    await orch.validate_plan(plan.id, "tester")
    await orch.start_plan(plan.id, "tester")
    await orch.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        m = None
        while loop.time() < deadline:
            m = next(x for x in store.list("migration", Migration) if x.plan_id == plan.id)
            if m.phase in (Phase.completed, Phase.failed, Phase.rolled_back):
                break
            await asyncio.sleep(0.05)
    finally:
        await orch.stop()
    assert m is not None and m.phase == Phase.completed, (m.phase, m.error)
    assert {c["name"]: c["ok"] for c in m.verification.checks}["tcp:22"] is True


#: the demo destinations' vGPU flavors' requests (SDD §10, §22.10)
A100_4C = {"resources:VGPU": "1", "trait:CUSTOM_NVIDIA_A100_4C": "required"}
A100_3_20C = {"resources:VGPU": "1", "trait:CUSTOM_NVIDIA_A100_3_20C": "required"}
#: the profiles rhoso18 and hibiscus-dc3 declare for them (SDD §22.3, §22.10)
DEMO_GPU_PROFILES = {"g1.a100-4c": "A100-4C", "g1.a100-3-20c": "A100-3-20C"}
#: the pools of the demo destinations' GPU flavors, by pool name: free, providers, most on one
DEMO_POOLS = [
    ("VGPU CUSTOM_NVIDIA_A100_4C", 4, 1, 4),
    ("VGPU CUSTOM_NVIDIA_A100_3_20C", 2, 1, 2),
    ("VGPU", 6, 2, 4),
]


def _pools(capacity) -> list[tuple[str, int, int, int]]:
    """A GPU capacity's pools by name, with their free units, providers and most free on one."""
    from seamless_migrate.planning.gpu import pool_name

    return [
        (
            pool_name(p.resource_class, p.traits, p.forbidden_traits),
            p.free,
            p.providers,
            p.largest_free,
        )
        for p in capacity.pools
    ]


def _gpu_checks(m: Migration) -> dict[str, dict]:
    """The GPU checks of the migration's last verification (SDD §7.5), by name."""
    return {c["name"]: c for c in m.verification.checks if c["name"].startswith("gpu_")}


async def _approve_waiting_cutovers(orch, store) -> None:
    """The VMware plan's cutovers, each requested by one person and approved by another (ADV-17)."""
    for m in store.list("migration", Migration):
        if m.phase in (Phase.ready, Phase.awaiting_cutover) and not m.cutover_requested:
            try:  # it may have moved on since the list was read
                await orch.request_cutover(m.id, "sari")
                await orch.approve(m.id, "rina", "Checked with the ML team for tonight")
            except NotAllowed:
                continue


async def test_demo_destinations_list_gpu_flavors_pools_and_declared_profiles(tmp_path, store):
    """SDD §10, §22.10 (plan task G9): rhoso18 and hibiscus-dc3 declare the profiles of the two vGPU
    flavors, and each fake lists the three GPU flavors - in its inventory's flavors and
    gpu_flavors and in gpu_flavors(), the declared profiles applied - and reports the three pools
    §22.3 reads for them alike in its inventory, its check and the gate's read: A100-4C's 4 units
    free on 1 provider, A100-3-20C's 2 on 1, and the class VGPU alone, 6 on 2 and 4 on one."""
    from seamless_migrate.planning.gpu import gpu_requests

    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    specs = {
        "g1.a100-4c": A100_4C,
        "g1.a100-3-20c": A100_3_20C,
        "p1.a100": {"pci_passthrough:alias": "a100:1"},
    }
    sizes = {
        "g1.a100-4c": (8, 32768, 200),
        "g1.a100-3-20c": (16, 65536, 300),
        "p1.a100": (16, 131072, 400),
    }
    expected = [
        {
            "name": name,
            "vcpus": vcpus,
            "ram_mb": ram_mb,
            "disk_gb": disk_gb,
            "gpus": gpu_requests(specs[name], DEMO_GPU_PROFILES.get(name)),
        }
        for name, (vcpus, ram_mb, disk_gb) in sizes.items()
    ]
    for provider_id in ("rhoso18", "hibiscus-dc3"):
        provider = store.get("provider", provider_id, Provider)
        assert provider.gpu_profiles == DEMO_GPU_PROFILES
        client = services.orchestrator.providers.get(provider)
        inventory = await client.inventory()
        listed = {f["name"]: f for f in inventory.flavors}
        for name, (vcpus, ram_mb, disk_gb) in sizes.items():
            assert listed[name] == {
                "name": name,
                "vcpus": vcpus,
                "ram_mb": ram_mb,
                "disk_gb": disk_gb,
                "extra_specs": specs[name],
            }
        assert inventory.gpu_flavors == expected == await client.gpu_flavors()
        assert (
            expected[0]["gpus"][0].profile == "A100-4C" and expected[2]["gpus"][0].profile is None
        )
        assert (
            inventory.gpu_capacity.status == "ok" and _pools(inventory.gpu_capacity) == DEMO_POOLS
        )
        check = (await services.orchestrator.check_provider(provider_id)).capabilities
        pools = check["gpu_capacity"]["pools"]
        assert check["gpu_capacity"]["status"] == "ok"
        assert [(p["traits"], p["free"], p["providers"], p["largest_free"]) for p in pools] == [
            (["CUSTOM_NVIDIA_A100_4C"], 4, 1, 4),
            (["CUSTOM_NVIDIA_A100_3_20C"], 2, 1, 2),
            ([], 6, 2, 4),
        ]
        # the cutover gate's read of the A100-4C flavor: its pool and its class's (SDD §22.7)
        gate = await client.gpu_capacity(expected[0]["gpus"])
        assert _pools(gate) == [DEMO_POOLS[0], DEMO_POOLS[2]]
    sources = [p for p in store.list("provider", Provider) if p.role == ProviderRole.source]
    assert all(p.gpu_profiles == {} for p in sources)


async def test_demo_maps_and_migrates_its_gpu_vms(tmp_path, store):
    """SDD §10, §22.10 (plan task G9): the finance plan maps gpu-ml-01's g1.vgpu to g1.a100-4c and
    the VMware plan dc2-ml-01's grid_a100-4c to g1.a100-4c, so both validate - told their GPU
    flavor, nothing blocked - and migrate instead of being cancelled as blocked: each server is
    created with g1.a100-4c and takes an A100-4C unit, its verification passes gpu_flavor and
    gpu_allocation, and the destination's check reads two units fewer."""
    from seamless_migrate.demo import FINANCE_PLAN, VMWARE_PLAN

    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e6, demo_failure_rate=0.0, tick_s=0.01
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    plans = {p.name: p for p in store.list("plan", Plan)}
    assert plans[FINANCE_PLAN].mappings.flavors == {"g1.vgpu": "g1.a100-4c"}
    assert plans[VMWARE_PLAN].mappings.gpus == {"grid_a100-4c": "g1.a100-4c"}
    names = ("gpu-ml-01", "dc2-ml-01")
    seeded = {m.vm.name: m for m in store.list("migration", Migration) if m.vm.name in names}
    assert sorted(seeded) == sorted(names)
    for name, m in seeded.items():
        severities = {f.code: f.severity for f in m.findings}
        assert severities["MAP_GPU_FLAVOR"] == "info", (name, severities)
        assert "blocker" not in severities.values(), (name, severities)
        assert m.phase not in (Phase.blocked, Phase.cancelled), name
    assert seeded["dc2-ml-01"].vm.gpus[0].profile == "grid_a100-4c"
    orch = services.orchestrator
    await orch.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while loop.time() < deadline:
            gpu = {m.vm.name: m for m in store.list("migration", Migration) if m.vm.name in names}
            if all(m.phase == Phase.completed for m in gpu.values()):
                break
            await _approve_waiting_cutovers(orch, store)
            await asyncio.sleep(0.05)
    finally:
        await orch.stop()
    for name, m in gpu.items():
        assert m.phase == Phase.completed, (name, m.phase, m.error)
        checks = _gpu_checks(m)
        assert checks["gpu_flavor"] == {
            "name": "gpu_flavor",
            "ok": True,
            "detail": "flavor g1.a100-4c, the GPU flavor: VGPU CUSTOM_NVIDIA_A100_4C ×1",
        }, name
        assert checks["gpu_allocation"] == {
            "name": "gpu_allocation",
            "ok": True,
            "detail": "VGPU: 1 held, 1 asked for",
        }, name
    pools = (await orch.check_provider("rhoso18")).capabilities["gpu_capacity"]["pools"]
    assert [p["free"] for p in pools] == [2, 2, 4]


async def test_demo_leaves_the_mig_and_passthrough_vms_unplanned(tmp_path, store):
    """SDD §10, §22.10 (plan task G9): the fake vCenter holds dc2-ml-02 - a MIG-backed
    grid_a100-3-20c vGPU - and dc2-cuda-01 - a DirectPath I/O GPU 10de:20b5 on a VM powered off,
    which only vmware_cold takes (§22.6) -, and no seeded plan holds either: planned from the
    dashboard with their GPU mappings, both validate, each told its GPU flavor."""
    from seamless_migrate.demo import UNPLANNED_VMWARE_VMS
    from seamless_migrate.domain.models import GpuDevice

    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    inventory = await FakeSourceProvider(ProviderKind.vmware, settings.demo_seed).list_vms()
    vms = {vm.name: vm for vm in inventory}
    mig, cuda = vms["dc2-ml-02"], vms["dc2-cuda-01"]
    assert (mig.vcpus, mig.ram_mb, mig.power_state, mig.cbt_enabled) == (16, 65536, "running", True)
    assert mig.gpus == [GpuDevice(kind="vgpu", profile="grid_a100-3-20c")]
    assert (cuda.vcpus, cuda.ram_mb, cuda.power_state) == (16, 131072, "stopped")
    assert cuda.gpus == [GpuDevice(kind="passthrough", vendor_id="10de", device_id="20b5")]
    assert UNPLANNED_VMWARE_VMS == {"dc2-portal-02", "dc2-ml-02", "dc2-cuda-01"}
    held = {vm_id for p in store.list("plan", Plan) for vm_id in p.vm_ids}
    assert {vm.name for vm in inventory if vm.source_id not in held} == UNPLANNED_VMWARE_VMS
    plan = Plan(
        name="GPU VMs of DC2",
        source_provider_id="vcenter-dc2",
        destination_provider_id="rhoso18",
        vm_ids=[mig.source_id, cuda.source_id],
        mappings=Mappings(
            networks={"DC2-Prod": "dc2-prod"},
            gpus={"grid_a100-3-20c": "g1.a100-3-20c", "10de:20b5": "p1.a100"},
        ),
    )
    store.put("plan", plan)
    await services.orchestrator.validate_plan(plan.id, "tester")
    planned = {m.vm.name: m for m in store.list("migration", Migration) if m.plan_id == plan.id}
    for name, m in planned.items():
        codes = {f.code: f.severity for f in m.findings}
        assert m.phase == Phase.ready, (name, codes)
        assert codes["MAP_GPU_FLAVOR"] == "info", (name, codes)
    assert planned["dc2-cuda-01"].strategy == "vmware_cold"
    warm = next(e for e in planned["dc2-cuda-01"].estimates if e.strategy == "vmware_warm")
    assert not warm.eligible
    # a PCI alias's capacity Placement does not show (SDD §22.5)
    assert "DST_GPU_CAPACITY_UNKNOWN" in [f.code for f in planned["dc2-cuda-01"].findings]


async def test_a_demo_plan_with_the_mig_vm_waits_for_gpu_capacity_then_cuts_over(tmp_path, store):
    """SDD §7.4, §10, §22.7, §22.10 (plan task G9): a plan with dc2-ml-02, its grid_a100-3-20c
    mapped to g1.a100-3-20c, validates with room in the A100-3-20C pool; the gate's first two
    reads find the pool full, as if another tenant held its vGPUs, so the migration waits at its
    gate - migration.gpu_wait announcing it, the source running -, and the third read finds room:
    the wait ends, the cutover starts and the verification passes its GPU checks."""
    from seamless_migrate.domain.models import VerificationConfig

    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e6, demo_failure_rate=0.0, tick_s=0.01
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    vms = await FakeSourceProvider(ProviderKind.vmware, settings.demo_seed).list_vms()
    [mig] = [vm for vm in vms if vm.name == "dc2-ml-02"]
    plan = Plan(
        name="ML inference on MIG",
        source_provider_id="vcenter-dc2",
        destination_provider_id="rhoso18",
        vm_ids=[mig.source_id],
        mappings=Mappings(
            networks={"DC2-Prod": "dc2-prod"}, gpus={"grid_a100-3-20c": "g1.a100-3-20c"}
        ),
        require_approval=False,
        auto_cutover=True,
        verification=VerificationConfig(timeout_s=60),
    )
    store.put("plan", plan)
    orch = services.orchestrator
    await orch.validate_plan(plan.id, "tester")
    [m] = [x for x in store.list("migration", Migration) if x.plan_id == plan.id]
    codes = [f.code for f in m.findings]
    # the validation sees room in the pool (SDD §22.5, §22.10)
    assert m.phase == Phase.ready and "MAP_GPU_FLAVOR" in codes, codes
    assert "DST_GPU_CAPACITY_INSUFFICIENT" not in codes
    await orch.start_plan(plan.id, "tester")
    await orch.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while loop.time() < deadline:
            m = store.get("migration", m.id, Migration)
            if m.phase in (Phase.completed, Phase.failed, Phase.rolled_back):
                break
            await asyncio.sleep(0.02)
    finally:
        await orch.stop()
    assert m.phase == Phase.completed, (m.phase, m.error)
    waits = [
        e
        for e in store.events(since_seq=0, limit=100000)
        if e.kind == "migration.gpu_wait" and e.migration_id == m.id
    ]
    pool = "VGPU CUSTOM_NVIDIA_A100_3_20C"
    assert [e.data for e in waits] == [
        {"waiting": True, "pool": pool, "needed": 1, "free": 0},
        {"waiting": False, "pool": pool, "needed": 1, "free": 2},
    ]
    assert "RHOSO 18.0" in waits[0].message and "the source VM keeps running" in waits[0].message
    # the source ran on through the wait: the downtime clock opened after it ended
    assert m.downtime_started_at is not None and m.downtime_started_at >= waits[-1].ts
    assert m.gpu_wait is None
    checks = _gpu_checks(m)
    assert checks["gpu_flavor"]["ok"] and "flavor g1.a100-3-20c" in checks["gpu_flavor"]["detail"]
    assert checks["gpu_allocation"]["detail"] == "VGPU: 1 held, 1 asked for"
    assert checks["gpu_allocation"]["ok"]


async def test_a_restart_hands_the_new_fakes_the_servers_the_demo_created(tmp_path, store):
    """SDD §10 (plan task G9): a restart builds new fakes, whose Placement holds no server; the seed
    hands them the servers the migrations recorded, with their flavors - as it stops their sources
    again (§8.1) -, so a cut-over GPU VM's server keeps its unit and its GPU flavor."""
    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e6, demo_failure_rate=0.0, tick_s=0.01
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    orch = services.orchestrator
    await orch.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while loop.time() < deadline:
            [gpu] = [m for m in store.list("migration", Migration) if m.vm.name == "gpu-ml-01"]
            if gpu.phase == Phase.completed:
                break
            await asyncio.sleep(0.05)
    finally:
        await orch.stop()
    assert gpu.phase == Phase.completed and gpu.destination_server_id, (gpu.phase, gpu.error)
    rhoso = store.get("provider", "rhoso18", Provider)
    restarted = build_services(settings, store)
    fresh = restarted.orchestrator.providers.get(rhoso)
    assert _pools((await fresh.inventory()).gpu_capacity) == DEMO_POOLS
    await seed_demo(store, settings, restarted.orchestrator)
    pools = _pools((await fresh.inventory()).gpu_capacity)
    assert [free for _, free, _, _ in pools] == [3, 2, 5]
    assert (await fresh.get_server(gpu.destination_server_id))["flavor"]["name"] == "g1.a100-4c"
    assert await fresh.server_allocations(gpu.destination_server_id) == {"VGPU": 1}


async def test_the_demo_seed_cancels_a_vm_its_preflight_blocks(tmp_path, store, monkeypatch):
    """SDD §10: since gpu-ml-01's flavor is mapped no demo VM is blocked (plan task G9); the seed
    still cancels one its pre-flight blocks, naming the blocker - a VM in error, here."""
    import dataclasses

    import seamless_migrate.providers.fake as fake

    errored = tuple(
        dataclasses.replace(spec, power_state="error") if spec.name == "etl-01" else spec
        for spec in fake._OPENSTACK
    )
    monkeypatch.setattr(fake, "_OPENSTACK", errored)
    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    [cancelled] = [m for m in store.list("migration", Migration) if m.phase == Phase.cancelled]
    assert cancelled.vm.name == "etl-01"
    assert Phase.blocked in [c.to_phase for c in cancelled.phase_history]
    reason = "cancelled: excluded from the demo: blocked by SRC_VM_ERROR_STATE"
    assert cancelled.phase_history[-1].reason == reason


async def test_the_cutover_refusal_demo_vm_is_left_for_the_operator_to_plan(tmp_path, store):
    """SDD §7.4: dc2-portal-02 carries `demo-fault: cutover-refusal`, and no seeded plan holds it -
    the seeded plans and the smoke run are unchanged; planned warm from the dashboard it shows
    what the cutover will refuse."""
    from seamless_migrate.demo import VMWARE_PLAN

    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e6, demo_failure_rate=0.0, tick_s=0.01
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    await services.orchestrator.stop()
    held = {vm_id for p in store.list("plan", Plan) for vm_id in p.vm_ids}
    plans = {p.name: p for p in store.list("plan", Plan)}
    inventory = await FakeSourceProvider(ProviderKind.vmware, settings.demo_seed).list_vms()
    [portal] = [vm for vm in inventory if vm.name == "dc2-portal-02"]
    assert portal.tags.get("demo-fault") == "cutover-refusal"
    assert portal.source_id not in held
    # dc2-ml-02 and dc2-cuda-01 stay unplanned too (SDD §10, §22.10)
    assert len(plans[VMWARE_PLAN].vm_ids) == len(inventory) - 3
