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
