import asyncio
import json
import smtplib
import socket
from collections import defaultdict
from email import message_from_bytes, policy
from urllib.parse import urlsplit

import httpx
import pytest

import seamless_migrate.notify.email as email_module
import seamless_migrate.notify.notifier as notifier_module
from seamless_migrate.api.app import build_services
from seamless_migrate.config import Settings
from seamless_migrate.demo import DEMO_PLAN_NAMES, FINANCE_PLAN, seed_demo
from seamless_migrate.domain.enums import Phase, PlanStatus, ProviderKind, ProviderRole
from seamless_migrate.domain.models import (
    AdvisorPolicy,
    Mappings,
    Migration,
    NotificationChannel,
    NotificationCursor,
    NotificationSubscription,
    Plan,
    Provider,
)
from seamless_migrate.events import emit
from seamless_migrate.notify.notifier import Notifier, Timing, senders_for
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


# -- the demo's network cut-over (SDD §10, §23.9, plan task N11) -------------------------------


async def _run_finance(tmp_path, store, names, timeout=60.0):
    """The seeded demo run until the finance migrations of ``names`` end (SDD §10)."""
    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e6, demo_failure_rate=0.0, tick_s=0.01
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    orch = services.orchestrator
    await orch.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        ends = (Phase.completed, Phase.rolled_back, Phase.failed, Phase.cancelled)
        while loop.time() < deadline:
            mine = [m for m in store.list("migration", Migration) if m.vm.name in names]
            if len(mine) == len(names) and all(m.phase in ends for m in mine):
                break
            await asyncio.sleep(0.05)
    finally:
        await orch.stop()
    by_name = {m.vm.name: m for m in store.list("migration", Migration) if m.vm.name in names}
    providers = {p.id: p for p in store.list("provider", Provider)}
    source = orch.providers.get(providers["rhosp17-finance"])
    destination = orch.providers.get(providers["rhoso18"])
    return by_name, source, destination


def _actions(store, m, action):
    return [
        (e.data["step"], e.data["outcome"])
        for e in store.events(since_seq=0, migration_id=m.id, limit=10000)
        if e.kind == "migration.action" and e.data.get("action") == action
    ]


async def test_the_demo_moves_a_floating_ip_with_its_address(tmp_path, store):
    """SDD §8.3, §10 (plan task N11): the finance plan keeps web-01's floating IP's address -
    reserved at the destination with the pre-staging, moved from the source's port to the new
    server's after the stop -, and the portal pool's member helper drains and joins web-01."""
    by_name, source, destination = await _run_finance(tmp_path, store, {"web-01"})
    m = by_name["web-01"]
    assert m.phase == Phase.completed
    [move] = m.fip_moves
    assert (move.address, move.state) == ("203.0.113.10", "moved")
    assert destination._cloud.floating["203.0.113.10"] == f"port-{m.destination_server_id}"
    assert source._floating["203.0.113.10"] is None
    assert _actions(store, m, "floating_ip") == [("reserve", "succeeded"), ("move", "succeeded")]
    # the member helper: drained at the source, joined at the destination (SDD §8.5)
    [lb] = m.lb_moves
    assert (lb.source_pool, lb.pool, lb.state) == (
        "example-portal-pool",
        "example-portal-pool-rhoso",
        "joined",
    )
    assert _actions(store, m, "lb_member") == [("drain", "succeeded"), ("join", "succeeded")]


async def test_the_demo_swaps_a_dns_record(tmp_path, store):
    """SDD §8.4, §10 (plan task N11): ledger-web-01's NIC override gives it 10.20.1.133 at the
    destination, so the swap moves its record in the demo destination's Designate zone from its
    source address to that one."""
    by_name, _, destination = await _run_finance(tmp_path, store, {"ledger-web-01"})
    m = by_name["ledger-web-01"]
    assert m.phase == Phase.completed
    swap = m.dns_swap
    assert (swap.name, swap.old, swap.new, swap.state) == (
        "ledger-web-01.internal.example.org.",
        m.vm.nics[0].fixed_ips[0],
        "10.20.1.133",
        "swapped",
    )
    record = await destination.dns_recordset(
        "internal.example.org.", "ledger-web-01.internal.example.org.", "A"
    )
    assert (record["records"], record["ttl"], record["status"]) == (["10.20.1.133"], 300, "ACTIVE")
    assert _actions(store, m, "dns") == [("swap", "succeeded")]


async def test_the_demo_shows_the_heat_and_octavia_findings(tmp_path, store):
    """SDD §9.3, §10 (plan task N11): the seeded finance plan's validation warns of web-01's
    place in the example-portal stack, of app-01's and app-02's in its autoscaling group - which
    the plan acknowledges, so neither is blocked -, and of api-gw-01's membership of a pool the
    plan does not map; the portal pool's members are the helper's."""
    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    findings = {
        m.vm.name: {f.code: f for f in m.findings} for m in store.list("migration", Migration)
    }
    assert "SRC_VM_HEAT_STACK" in findings["web-01"]
    for name in ("app-01", "app-02"):
        autoscaled = findings[name]["SRC_VM_AUTOSCALED"]
        assert autoscaled.severity == "warning"
        assert autoscaled.message.endswith("The plan acknowledges it (acknowledged_findings).")
    assert "SRC_VM_LB_MEMBER" in findings["api-gw-01"]
    for name in ("web-01", "web-02", "web-03"):
        assert "SRC_VM_LB_MEMBER" not in findings[name]
    assert not [m for m in store.list("migration", Migration) if m.phase == Phase.blocked]


async def test_the_demo_source_has_ceph_access_and_a_plan_that_tracks_extents(tmp_path, store):
    """SDD §7.4, §10, §28.4 (plan task CE11): the demo's RHOSP source declares the demo cluster's
    Ceph access with its key as written, its rbd pools report that fsid, the finance plan tracks
    extents, and the migrations it seeds carry their tracked disks - none for a NetApp VM."""
    from seamless_migrate.domain.models import Migration, Plan
    from seamless_migrate.planning.preflight import qualifying_disks, tracking_ready
    from seamless_migrate.providers.fake import DEMO_CEPH_FSID, DEMO_DST_CEPH_FSID

    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    source = store.get("provider", "rhosp17-finance", Provider)
    assert source.ceph is not None and source.ceph.fsid == DEMO_CEPH_FSID
    assert source.ceph.key_updated_at is not None and source.ceph.client == "seamless"
    rbd = [b for b in source.capabilities["storage_backends"] if b["family"] == "rbd"]
    assert rbd and all(b["ceph_fsid"] == DEMO_CEPH_FSID and b["ceph_pool"] for b in rbd)
    destination = store.get("provider", "rhoso18", Provider)
    dst_rbd = [b for b in destination.capabilities["storage_backends"] if b["family"] == "rbd"]
    assert dst_rbd and all(b["ceph_fsid"] == DEMO_DST_CEPH_FSID for b in dst_rbd)
    plans = {p.name: p for p in store.list("plan", Plan)}
    finance = plans["Finance apps (RHOSP 17.1 → RHOSO)"]
    assert finance.extent_tracking is True
    assert tracking_ready(finance, source)
    assert all(not p.extent_tracking for name, p in plans.items() if name != finance.name)
    migrations = [m for m in store.list("migration", Migration) if m.plan_id == finance.id]
    by_name = {m.vm.name: m for m in migrations}
    assert by_name, "the finance plan was validated"
    for name, m in by_name.items():
        assert m.tracked_disks == qualifying_disks(m.vm, source), name
    assert by_name["portal-01"].tracked_disks == []  # its volume is on NetApp
    tracked = [name for name, m in by_name.items() if m.tracked_disks]
    assert tracked, "a Ceph VM's migration tracks its volumes"


def test_the_demo_destinations_declare_their_conversion_hosts_keys():
    """SDD §10, §26.3, §26.5 (plan task ST7): host keys are checked by default, and the VMware
    plan's destination host is one the operator brings, which Seamless never pins from a console
    the demo does not print: each demo destination declares its conversion host's keys -
    synthetic ed25519 keys, as an operator reads them out of band -, so no demo plan is blocked by
    CONV_HOST_KEY_UNKNOWN, and the provider page shows them as declared."""
    from seamless_migrate.demo import DEMO_PROVIDERS
    from seamless_migrate.security.host_keys import public_key_problem

    for provider in DEMO_PROVIDERS:
        if provider.role != ProviderRole.destination:
            continue
        keys = provider.conversion_host.ssh_host_keys
        assert len(keys) == 1 and keys[0].startswith("ssh-ed25519 "), provider.id
        assert public_key_problem(keys[0]) is None
    declared = [
        p.conversion_host.ssh_host_keys[0]
        for p in DEMO_PROVIDERS
        if p.role == ProviderRole.destination
    ]
    assert len(set(declared)) == len(declared)  # each host its own key


# -- the demo's notifications (SDD §7.4, §10, §24.9, plan task NT10) ---------------------------

#: the seeded channels and their kinds (SDD §10)
DEMO_CHANNEL_KINDS = {"app-owners": "email", "migration-team": "slack", "change-hub": "webhook"}
#: the notifier's clocks shortened, as tests/test_notify.py's are (SDD §24.6)
FAST_NOTIFIER = Timing(
    tick_s=0.02,
    backoff_s=(0.03, 0.05, 0.08, 0.1),
    jitter=0.0,
    retry_after_max_s=0.2,
    pace_s=0.01,
    expiry_s=3600.0,
    cursor_write_s=0.05,
    restart_s=0.05,
)


@pytest.fixture
def nothing_leaves(monkeypatch) -> list[str]:
    """Every way a notification could leave the process, refused and recorded (SDD §24.9): a
    socket's connection, httpx's network transports, an SMTP session, and the notifier's read of
    a channel's secret."""
    tried: list[str] = []

    def refused(what: str):
        def refuse(*args, **kwargs):
            tried.append(what)
            raise ConnectionRefusedError(f"{what}: nothing leaves the demo's process")

        return refuse

    for owner, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (httpx.HTTPTransport, "handle_request"),
        (httpx.AsyncHTTPTransport, "handle_async_request"),
        (smtplib.SMTP, "connect"),
    ):
        monkeypatch.setattr(owner, name, refused(f"{owner.__name__}.{name}"))
    # the notifier reads a channel's secret, and the email sender the relay's, as §13.3 resolves a
    # secret (read_secret, since NT12): in demo mode neither may read one
    monkeypatch.setattr(notifier_module, "read_secret", refused("a channel secret's read"))
    monkeypatch.setattr(email_module, "read_secret", refused("the relay secret's read"))
    return tried


def _seeded_channels(store) -> dict[str, NotificationChannel]:
    return {c.name: c for c in store.list("notification_channel", NotificationChannel)}


def _sent(store, **data) -> list:
    """The notification.sent events whose data holds ``data``."""
    return [
        e
        for e in store.events(since_seq=0, limit=100000, kinds=["notification.sent"])
        if all(e.data.get(key) == value for key, value in data.items())
    ]


async def _until(found, timeout=60.0):
    """``found()`` once it holds something, else what it holds at the timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if result := found():
            return result
        await asyncio.sleep(0.02)
    return found()


async def test_the_demo_records_notifications_and_sends_none(
    tmp_path, store, nothing_leaves, monkeypatch
):
    """SDD §7.4, §10, §24.9 (plan task NT10, §24.11 decision 11): in demo mode every sender
    records instead of sending. The seed holds one channel of each kind - lists under example.com
    and example.org, an https example.com receiver, no Slack URL anywhere -, each created with its
    event and subscribed to both seeded plans with the default topics. A message to each - a
    cutover's downtime, and the channel's test - is rendered in its kind's format as the real
    sender renders it and recorded as notification.sent, and nothing leaves the process: no
    connection, no SMTP session, no secret read, whatever a channel names."""
    import inspect

    import seamless_migrate.demo as demo

    # the guard holds: a request or an SMTP session would be refused, and recorded
    with pytest.raises(ConnectionRefusedError):
        async with httpx.AsyncClient() as client:
            await client.post("https://change-hub.example.com/hooks/seamless")
    with pytest.raises(ConnectionRefusedError):
        smtplib.SMTP("relay.example.com", 587)
    assert len(nothing_leaves) == 2
    nothing_leaves.clear()
    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e7, demo_failure_rate=0.0
    )
    senders = senders_for(settings)
    assert {kind: type(sender).__name__ for kind, sender in senders.items()} == dict.fromkeys(
        ("email", "slack", "webhook"), "RecordingSender"
    )
    # outside demo mode the server runs the real senders (SDD §8.7)
    real = senders_for(Settings(data_dir=tmp_path / "real"))
    assert [type(sender).__name__ for sender in real.values()] == [
        "EmailSender",
        "SlackSender",
        "WebhookSender",
    ]
    rendered: dict[str, tuple[str, bytes]] = {}
    for sender in senders.values():

        def spy(channel, message, key, render=sender.render):
            body = render(channel, message, key)
            rendered[key] = (channel.kind, body)
            return body

        monkeypatch.setattr(sender, "render", spy)

    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    channels = _seeded_channels(store)
    assert {name: c.kind for name, c in channels.items()} == DEMO_CHANNEL_KINDS
    email, slack, webhook = (channels[name] for name in DEMO_CHANNEL_KINDS)
    assert email.recipients
    assert all(r.endswith(("@example.com", "@example.org")) for r in email.recipients)
    assert urlsplit(webhook.url).scheme == "https"
    assert urlsplit(webhook.url).hostname.endswith(".example.com")
    assert slack.url is None
    assert "hooks.slack.com" not in inspect.getsource(demo)
    plans = {p.id for p in store.list("plan", Plan)}
    subs = store.list("notification_subscription", NotificationSubscription)
    assert sorted((s.channel, s.plan_id) for s in subs) == sorted(
        (name, plan_id) for name in channels for plan_id in plans
    )
    assert all(s.topics == ["approval", "downtime", "problem"] for s in subs)
    catalog = [
        e
        for e in store.events(since_seq=0, limit=100000)
        if e.kind in ("channel.created", "subscription.created")
    ]
    created = sorted(e.data["name"] for e in catalog if e.kind == "channel.created")
    assert created == sorted(channels)
    subscribed = sorted(e.data["id"] for e in catalog if e.kind == "subscription.created")
    assert subscribed == sorted(s.id for s in subs)
    assert {e.actor for e in catalog} == {"demo"}

    finance = next(p for p in store.list("plan", Plan) if p.name == FINANCE_PLAN)
    m = next(x for x in store.list("migration", Migration) if x.plan_id == finance.id)
    notifier = Notifier(store, services.bus, settings, senders, timing=FAST_NOTIFIER)
    await notifier.start()
    try:
        down = await emit(
            store,
            services.bus,
            "migration.downtime_started",
            "the source VM stopped",
            plan_id=finance.id,
            migration_id=m.id,
        )
        await _until(lambda: len(_sent(store, event_seq=down.seq)) >= len(channels))
        tested = [await notifier.send_test(channel) for channel in channels.values()]
    finally:
        await notifier.stop()
    assert tested == [{"outcome": "sent", "reason": None}] * len(channels)
    sent = _sent(store, event_seq=down.seq)
    title = f"{m.vm.name} is down for its cutover"
    assert sorted(
        (e.data["channel"], e.data["kind"], e.data["trigger"], e.data["tries"], e.data["reason"])
        for e in sent
    ) == sorted(
        (name, kind, "downtime.started", 1, None) for name, kind in DEMO_CHANNEL_KINDS.items()
    )
    assert sorted(e.message for e in sent) == sorted(f"{title} -> {name}" for name in channels)
    assert all(e.plan_id == finance.id and e.migration_id == m.id for e in sent)
    assert sorted(e.data["channel"] for e in _sent(store, trigger="test")) == sorted(channels)
    assert store.events(since_seq=0, limit=100000, kinds=["notification.failed"]) == []

    # each message rendered in its kind's format, as the real sender renders it (SDD §24.5)
    assert sorted(key.split("_test_")[0] for key in rendered if "_test_" in key) == sorted(channels)
    kind, raw = rendered[f"app-owners_{down.seq}"]
    mail = message_from_bytes(raw, policy=policy.default)
    assert kind == "email" and mail["Subject"] == f"[Seamless] {title}"
    assert [a.addr_spec for a in mail["To"].addresses] == email.recipients
    assert mail["Message-ID"] == f"<app-owners_{down.seq}@seamless>"
    assert mail["Auto-Submitted"] == "auto-generated"
    assert "The source VM stopped at" in mail.get_content()
    kind, raw = rendered[f"migration-team_{down.seq}"]
    payload = json.loads(raw)
    assert kind == "slack" and payload["text"] == title
    assert [block["type"] for block in payload["blocks"]] == ["header", "section", "context"]
    assert payload["unfurl_links"] is False and payload["unfurl_media"] is False
    kind, raw = rendered[f"change-hub_{down.seq}"]
    document = json.loads(raw)
    assert kind == "webhook" and document["type"] == "downtime.started"
    assert document["data"]["title"] == title and document["data"]["migration_id"] == m.id
    assert document["data"]["event_seq"] == down.seq
    assert nothing_leaves == []


async def test_the_demos_cutovers_notify_their_downtime(tmp_path, store, nothing_leaves):
    """SDD §7.4, §10, §24.9 (plan task NT10): the seeded demo run with its notifier beside its
    orchestrator, a demo cutover notifies its downtime on each seeded channel: the source's stop
    and the VM up on the destination are recorded as notification.sent, listed on the Events page
    in seq order with every other event, and each channel's state on the Notifications page reads
    sent; nothing leaves the process."""
    settings = Settings(
        data_dir=tmp_path / "data", demo=True, demo_speed=1e6, demo_failure_rate=0.0, tick_s=0.01
    )
    services = build_services(settings, store)
    await seed_demo(store, settings, services.orchestrator)
    channels = _seeded_channels(store)
    assert {name: c.kind for name, c in channels.items()} == DEMO_CHANNEL_KINDS
    downtime = {"downtime.started", "downtime.ended"}

    def notified() -> list[Migration]:
        """The completed migrations whose downtime every channel recorded as sent."""
        seen: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for e in _sent(store):
            if e.migration_id is not None:
                seen[e.migration_id][e.data["channel"]].add(e.data["trigger"])
        return [
            m
            for m in store.list("migration", Migration)
            if m.phase == Phase.completed and all(downtime <= seen[m.id][c] for c in channels)
        ]

    orch = services.orchestrator
    notifier = Notifier(store, services.bus, settings, senders_for(settings), timing=FAST_NOTIFIER)
    await orch.start()
    await notifier.start()
    try:
        done = await _until(notified)
    finally:
        await notifier.stop()
        await orch.stop()
    assert done, "no demo cutover notified its downtime on every channel"
    m = done[0]
    of_m = {e.seq: e for e in store.events(since_seq=0, migration_id=m.id, limit=100000)}
    for name, kind in DEMO_CHANNEL_KINDS.items():
        mine = {e.data["trigger"]: e for e in _sent(store, channel=name) if e.migration_id == m.id}
        started, ended = mine["downtime.started"], mine["downtime.ended"]
        assert started.data["kind"] == ended.data["kind"] == kind
        assert started.message == f"{m.vm.name} is down for its cutover -> {name}"
        assert ended.message.startswith(f"{m.vm.name} is up on the destination after ")
        assert ended.message.endswith(f" -> {name}")
        assert of_m[started.data["event_seq"]].kind == "migration.downtime_started"
        assert of_m[ended.data["event_seq"]].kind == "migration.downtime_ended"
        assert of_m[ended.data["event_seq"]].data["cause"] == "verified"
        # the channel's state, as the Notifications page shows it (SDD §16, §24.3)
        cursor = store.get("notification_cursor", name, NotificationCursor)
        assert (cursor.last_outcome, cursor.failures_in_a_row, cursor.last_reason) == (
            "sent",
            0,
            None,
        )
    assert store.events(since_seq=0, limit=100000, kinds=["notification.failed"]) == []
    # the Events page lists events by time: their time order is their seq order (SDD §4.2)
    events = store.events(since_seq=0, limit=100000)
    inverted = [
        (a.seq, a.kind, b.seq, b.kind)
        for a, b in zip(events, events[1:], strict=False)
        if b.ts < a.ts
    ]
    assert inverted == []
    assert nothing_leaves == []


def test_the_demo_server_edits_and_adds_email_channels_with_a_relay_of_fakes(
    tmp_path, nothing_leaves
):
    """SDD §7.4, §15.1 (plan task NT10): the demo server - the app `seamless serve --demo` runs -
    takes a relay of the demo's fakes while SEAMLESS_SMTP_HOST and SEAMLESS_SMTP_FROM are unset,
    so from the Notifications page an admin disables and enables the seeded email channel and
    adds one as with a real relay, and each channel's test answers sent; nothing reaches the
    relay. A relay or a sender the environment names is kept, and outside demo mode the relay
    stays unset."""
    from fastapi.testclient import TestClient

    from seamless_migrate.cli import build_app
    from seamless_migrate.demo import demo_server_settings

    named = Settings(data_dir=tmp_path / "named", demo=True, smtp_host="mail.example.org")
    kept = demo_server_settings(named)
    assert (kept.smtp_host, kept.smtp_from) == ("mail.example.org", "seamless@example.com")
    plain = build_app(Settings(data_dir=tmp_path / "plain"))
    assert plain.state.services.settings.smtp_host is None
    plain.state.services.store.dispose()
    settings = Settings(
        data_dir=tmp_path / "data",
        secrets_dir=tmp_path / "secrets",
        demo=True,
        demo_speed=1.0,
        demo_failure_rate=0.0,
        auth_disabled=True,
    )
    assert (settings.smtp_host, settings.smtp_from) == (None, None)
    app = build_app(settings)
    served = app.state.services
    assert (served.settings.smtp_host, served.settings.smtp_from) == (
        "relay.example.com",
        "seamless@example.com",
    )
    channels = "/api/v1/notifications/channels"
    try:
        # authentication off on loopback, as `seamless serve --demo` binds it (SDD §13.1)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            listed = client.get(channels).json()
            assert {c["name"]: c["kind"] for c in listed} == DEMO_CHANNEL_KINDS
            for enabled in (False, True):
                answer = client.patch(f"{channels}/app-owners", json={"enabled": enabled})
                assert answer.status_code == 200, answer.text
                assert answer.json()["enabled"] is enabled
            added = client.post(
                channels,
                json={"name": "night-shift", "kind": "email", "recipients": ["oncall@example.org"]},
            )
            assert added.status_code == 201, added.text
            for name in (*DEMO_CHANNEL_KINDS, "night-shift"):
                tested = client.post(f"{channels}/{name}/test")
                assert tested.json() == {"outcome": "sent", "reason": None}, (name, tested.text)
    finally:
        served.store.dispose()
    assert nothing_leaves == []
