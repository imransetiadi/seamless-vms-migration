"""Demo seeding (``seamless serve --demo``): providers, two running plans and the notification
channels.

Idempotent: providers, plans and channels that already exist (by id / name) are left untouched.
Plans are auto-waved, validated and started so the dashboard shows every phase within minutes: the
finance plan cuts over automatically (simulated failures exercise rollback), the VMware plan waits
for approvals. Each maps its GPU VM to a GPU flavor of the destination, so it migrates (SDD §10,
§22.10). VMs blocked by pre-flight are cancelled with an explanatory reason. One channel of each
kind is subscribed to both plans, and records every message instead of sending it (§7.4, §24.9).
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import UTC, datetime

from .config import Settings
from .domain.enums import DecisionType, Phase, ProviderKind, ProviderRole
from .domain.models import (
    AdvisorPolicy,
    CephAccess,
    ConversionHostConfig,
    DnsTarget,
    Mappings,
    Migration,
    NetworkCutover,
    NicOverride,
    NotificationChannel,
    NotificationSubscription,
    Plan,
    PolicyScope,
    Provider,
    VerificationConfig,
)
from .events import put_and_emit
from .executors.simulated import server_flavor
from .notify.catalog import channel_event, subscription_event
from .orchestrator import Orchestrator
from .planning.probes import is_database, suggest_probes
from .providers.base import ProviderError
from .providers.fake import (
    DEMO_CEPH_FSID,
    DEMO_DESTINATION_POOL,
    DEMO_DNS_NEW_ADDRESS,
    DEMO_DNS_VM,
    DEMO_DNS_ZONES,
    DEMO_PORTAL_POOL,
    DEMO_STACK_SERVERS,
    FakeSourceProvider,
)
from .store import Store

log = logging.getLogger(__name__)

FINANCE_PLAN = "Finance apps (RHOSP 17.1 → RHOSO)"
#: Finance VMs the demo leaves unplanned, so a new plan can take them (SDD §10): one VM has one
#: migration across plans (§5.4), and the finance plan holds the rest for good once it completes.
UNPLANNED_FINANCE_VMS = frozenset({"report-01", "batch-01"})
#: left for the operator to plan: dc2-portal-02's warm passes meet what the cutover will refuse
#: (SDD §7.4); dc2-ml-02's MIG-backed vGPU shows the cutover gate's wait for GPU capacity, and
#: dc2-cuda-01's DirectPath I/O GPU a cold-only VM with a PCI alias flavor (§22.10)
UNPLANNED_VMWARE_VMS = frozenset({"dc2-portal-02", "dc2-ml-02", "dc2-cuda-01"})
#: the vGPU profiles the demo destinations declare for their two vGPU flavors: no API names them
#: (SDD §10, §22.3)
DEMO_GPU_PROFILES = {"g1.a100-4c": "A100-4C", "g1.a100-3-20c": "A100-3-20C"}
#: the GPU flavor the demo plans map their GPU VMs to (SDD §10, §22.10)
DEMO_GPU_FLAVOR = "g1.a100-4c"
VMWARE_PLAN = "DC2 VMware exit"
DEMO_PLAN_NAMES = (FINANCE_PLAN, VMWARE_PLAN)
DEMO_ACTOR = "demo"
FINANCE_POLICY = "Finance — approval required"
#: ADV-17 (SDD §5.4): the VMware plan's first cutovers, asked for by one person, approved by another
DEMO_REQUESTER = "sari"
DEMO_APPROVER = "rina"
DEMO_APPROVAL_COMMENTS = (
    "Change CHG-2041 approved by the DC2 service owner; backups checked this morning.",
    "Agreed with the application team for tonight's window; rollback steps reviewed.",
)
#: the demo server's relay and sender while SEAMLESS_SMTP_HOST and SEAMLESS_SMTP_FROM are unset
#: (SDD §7.4, §15.1): a relay of the demo's fakes, as its clouds are, which nothing reaches
DEMO_SMTP_HOST = "relay.example.com"
DEMO_SMTP_FROM = "seamless@example.com"
#: one channel of each kind, each subscribed to the seeded plans (SDD §10, §24.9): in demo mode
#: every sender records instead of sending (§7.4), so the Slack channel needs no URL - a Slack
#: channel's URL lives only in its secret (§24.3), and the demo holds none
DEMO_CHANNELS = (
    NotificationChannel(
        name="app-owners",
        kind="email",
        description="Finance and DC2 application owners (the demo records, sends none)",
        recipients=["finance-app-owners@example.com", "dc2-service-owners@example.org"],
    ),
    NotificationChannel(
        name="migration-team",
        kind="slack",
        description="The migration team's Slack channel (the demo records, sends none)",
    ),
    NotificationChannel(
        name="change-hub",
        kind="webhook",
        description="The change-management hub's receiver (the demo records, sends none)",
        url="https://change-hub.example.com/hooks/seamless",
    ),
)


def demo_server_settings(settings: Settings) -> Settings:
    """The demo server's settings (SDD §7.4, §15.1): the demo's relay and sender where the
    environment names none, so an email channel registers and is edited as with a real relay -
    and its messages are recorded, never sent."""
    missing = {
        name: value
        for name, value in (("smtp_host", DEMO_SMTP_HOST), ("smtp_from", DEMO_SMTP_FROM))
        if not getattr(settings, name)
    }
    return settings.model_copy(update=missing) if missing else settings


def _demo_host_key(host: str) -> str:
    """A synthetic ed25519 public key of the demo conversion host ``host``, in OpenSSH's one-line
    form: what an operator declares after reading it out of band (SDD §10, §26.3). Made up from the
    name, so no private half exists anywhere."""
    kind = b"ssh-ed25519"
    point = hashlib.sha256(b"seamless demo conversion host " + host.encode()).digest()
    blob = b"".join(len(part).to_bytes(4, "big") + part for part in (kind, point))
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {host}"


DEMO_PROVIDERS = (
    Provider(
        id="rhosp17-finance",
        name="RHOSP 17.1 Finance (DC1)",
        kind=ProviderKind.openstack,
        role=ProviderRole.source,
        endpoint="https://overcloud.dc1.example.com:13000/v3",
        cloud="rhosp17",
        distribution="rhosp",
        conversion_host=ConversionHostConfig(
            name="os-migrate-conv-src", flavor="m1.large", external_network="public"
        ),
        # the demo cluster's Ceph access, its key as written (SDD §10, §28.4, plan task CE11)
        ceph=CephAccess(
            fsid=DEMO_CEPH_FSID,
            monitors=["ceph-mon-0.dc1.example.com", "ceph-mon-1.dc1.example.com"],
            key_updated_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        ),
    ),
    Provider(
        id="vcenter-dc2",
        name="vCenter DC2",
        kind=ProviderKind.vmware,
        role=ProviderRole.source,
        endpoint="https://vcenter.dc2.example.com/sdk",
        credentials_secret="vcenter-dc2",
        distribution="vmware",
    ),
    Provider(
        id="rhoso18",
        name="RHOSO 18.0",
        kind=ProviderKind.rhoso,
        role=ProviderRole.destination,
        endpoint="https://keystone-public-openstack.apps.ocp.example.com/v3",
        cloud="rhoso",
        distribution="rhoso",
        conversion_host=ConversionHostConfig(
            name="os-migrate-conv-dst",
            flavor="m1.large",
            external_network="public",
            address="192.0.2.50",
            # the VMware plan's host, which the operator brings: its keys declared (SDD §26.3)
            ssh_host_keys=[_demo_host_key("os-migrate-conv-dst")],
        ),
        gpu_profiles=dict(DEMO_GPU_PROFILES),
    ),
    # a community OpenStack destination beside RHOSO 18.0 (SDD §10, §21; plan task T9)
    Provider(
        id="hibiscus-dc3",
        name="OpenStack 2026.2 Hibiscus (DC3)",
        kind=ProviderKind.openstack,
        role=ProviderRole.destination,
        endpoint="https://keystone.dc3.example.com:5000/v3",
        cloud="hibiscus",
        distribution="kolla",
        release="2026.2",
        conversion_host=ConversionHostConfig(
            name="os-migrate-conv-dc3",
            flavor="m1.large",
            external_network="public",
            address="192.0.2.60",
            ssh_host_keys=[_demo_host_key("os-migrate-conv-dc3")],
        ),
        gpu_profiles=dict(DEMO_GPU_PROFILES),
    ),
)


async def _plans(settings: Settings) -> list[Plan]:
    finance_vms = await FakeSourceProvider(ProviderKind.openstack, settings.demo_seed).list_vms()
    vmware_vms = await FakeSourceProvider(ProviderKind.vmware, settings.demo_seed).list_vms()
    planned = [vm for vm in finance_vms if vm.name not in UNPLANNED_FINANCE_VMS]
    # the finance plan probes its databases' services (ADV-18, SDD §7.4); the VMware plan probes
    # nothing, so its validation warns about its database VMs (APP_PROBE_MISSING)
    probed = {vm.source_id: suggest_probes(vm) for vm in planned if is_database(vm)}
    by_name = {vm.name: vm for vm in finance_vms}
    renamed = by_name[DEMO_DNS_VM]
    return [
        Plan(
            name=FINANCE_PLAN,
            description="Side-by-side migration of the finance estate; cuts over automatically.",
            source_provider_id="rhosp17-finance",
            destination_provider_id="rhoso18",
            vm_ids=[vm.source_id for vm in planned],
            mappings=Mappings(
                networks={"finance-app": "finance-app", "finance-db": "finance-db"},
                volume_types={"ceph-ssd": "ceph-ssd", "ceph-hdd": "ceph-hdd"},
                # gpu-ml-01's GPU comes with its flavor (SDD §22.4, §22.10)
                flavors={"g1.vgpu": DEMO_GPU_FLAVOR},
            ),
            downtime_slo_s=600,
            require_approval=False,
            auto_cutover=True,
            # the finance plan tracks its Ceph volumes' extents (SDD §28, plan task CE11)
            extent_tracking=True,
            keep_warm_interval_s=120,
            verification=VerificationConfig(timeout_s=120),
            probe_overrides={vm_id: probes for vm_id, probes in probed.items() if probes},
            # every network cut-over rule runs in the demo (SDD §10, §23.9, plan task N11):
            # web-01's floating IP keeps its address, ledger-web-01's record moves to the
            # address its NIC override gives it, and the portal pool's members move with
            # the member helper
            network_cutover=NetworkCutover(
                floating_ips="keep_address",
                dns_targets={
                    renamed.source_id: DnsTarget(
                        kind="designate", zone=DEMO_DNS_ZONES[0], record=DEMO_DNS_VM
                    )
                },
                lb_pools={DEMO_PORTAL_POOL: DEMO_DESTINATION_POOL},
            ),
            nic_overrides={
                renamed.source_id: {
                    str(renamed.nics[0].mac): NicOverride(fixed_ips=[DEMO_DNS_NEW_ADDRESS])
                }
            },
            # the portal's autoscaled pair is the plan's to move (owner decision 9)
            acknowledged_findings={
                by_name[name].source_id: ["SRC_VM_AUTOSCALED"]
                for name, (_, group) in DEMO_STACK_SERVERS.items()
                if group
            },
        ),
        Plan(
            name=VMWARE_PLAN,
            description="VMware exit for DC2; cutovers wait for an approver.",
            source_provider_id="vcenter-dc2",
            destination_provider_id="rhoso18",
            vm_ids=[vm.source_id for vm in vmware_vms if vm.name not in UNPLANNED_VMWARE_VMS],
            mappings=Mappings(
                networks={"DC2-Prod": "dc2-prod", "DC2-DMZ": "dc2-dmz"},
                # dc2-ml-01's vGPU, by its profile (SDD §22.4, §22.10)
                gpus={"grid_a100-4c": DEMO_GPU_FLAVOR},
            ),
            downtime_slo_s=900,
            require_approval=True,
            auto_cutover=False,
            keep_warm_interval_s=300,
            verification=VerificationConfig(timeout_s=120),
        ),
    ]


async def seed_demo(
    store: Store, settings: Settings, orchestrator: Orchestrator | None = None
) -> None:
    """Create the demo providers and plans (idempotent) and start the plans; then hand this
    process's new fakes what the migrations recorded - their sources stopped, their servers
    created (SDD §8.1, §10). A server runs it at each takeover of the orchestrator's lease (§8)."""
    if orchestrator is None:
        from .api.app import build_services

        orchestrator = build_services(settings, store).orchestrator
    db = orchestrator.db  # async facade over ``store``: never block the event loop
    existing_providers = {p.id for p in await db.list("provider", Provider)}
    for provider in DEMO_PROVIDERS:
        if provider.id not in existing_providers:
            await db.put("provider", provider, expected_version=0)
            continue
        # demo databases seeded before SDD §4.2 Distribution existed get the platform preset
        stored, version = await db.get_versioned("provider", provider.id, Provider)
        if stored.distribution is None and provider.distribution is not None:
            await db.put(
                "provider",
                stored.model_copy(update={"distribution": provider.distribution}),
                expected_version=version,
            )
    # SDD §14.6 (ADV-04): the spec's demo policy, in force before the demo plans are validated
    if FINANCE_POLICY not in {p.name for p in await db.list("advisor_policy", AdvisorPolicy)}:
        await db.put(
            "advisor_policy",
            AdvisorPolicy(
                id="pol-finance",
                name=FINANCE_POLICY,
                scope=PolicyScope(provider_ids=["rhosp17-finance"]),
                decision_types=[DecisionType.migration_strategy],
                mode="always_require_approval",
                threshold=settings.jev_min_confidence,
                require_override_reason=True,
                updated_by=DEMO_ACTOR,
            ),
            expected_version=0,
        )
    existing_plans = {p.name for p in await db.list("plan", Plan)}
    for plan in await _plans(settings):
        if plan.name in existing_plans:
            continue
        await db.put("plan", plan, expected_version=0)
        await orchestrator.auto_waves(plan.id, 8, DEMO_ACTOR)
        await orchestrator.validate_plan(plan.id, DEMO_ACTOR)
        blocked = await db.list("migration", Migration, plan_id=plan.id, phase=Phase.blocked)
        for migration in blocked:
            codes = ", ".join(f.code for f in migration.findings if f.severity == "blocker")
            await orchestrator.cancel(
                migration.id, DEMO_ACTOR, f"excluded from the demo: blocked by {codes}"
            )
        await orchestrator.start_plan(plan.id, DEMO_ACTOR)
        if plan.name == VMWARE_PLAN:
            await _seed_cutover_approvals(orchestrator, plan.id)
        log.info("demo plan %r seeded and started", plan.name)
    await _seed_notifications(orchestrator)
    await _restore_source_power(orchestrator)
    await _restore_destination_servers(orchestrator)


async def _seed_notifications(orchestrator: Orchestrator) -> None:
    """One channel of each kind, each subscribed to the seeded plans with the default topics (SDD
    §10, §24.9), added as an admin adds one - each write with its event, so each channel starts at
    its own creation, after the seed's other events (§24.6). A channel the store holds is left as
    it is, its subscriptions with it: a restart adds nothing, and keeps what an admin changed."""
    store, bus = orchestrator.store, orchestrator.bus
    held = {c.name for c in await orchestrator.db.list("notification_channel", NotificationChannel)}
    plans = {p.name: p.id for p in await orchestrator.db.list("plan", Plan)}
    plan_ids = [plans[name] for name in DEMO_PLAN_NAMES if name in plans]
    for channel in DEMO_CHANNELS:
        if channel.name in held:
            continue
        now = orchestrator.now()
        channel = channel.model_copy(update={"created_at": now, "updated_at": now})
        await put_and_emit(
            store,
            bus,
            "notification_channel",
            channel,
            0,
            channel_event("channel.created", channel, DEMO_ACTOR),
        )
        for plan_id in plan_ids:
            sub = NotificationSubscription(
                channel=channel.name, plan_id=plan_id, created_by=DEMO_ACTOR, created_at=now
            )
            await put_and_emit(
                store,
                bus,
                "notification_subscription",
                sub,
                0,
                subscription_event("subscription.created", sub, DEMO_ACTOR),
            )
        log.info("demo notification channel %r seeded", channel.name)


#: a migration in these stopped its source: the simulated cutover did, before a restart
_SOURCE_STOPPED = frozenset({Phase.cutover, Phase.verifying, Phase.completed, Phase.finalized})


async def _restore_source_power(orchestrator: Orchestrator) -> None:
    """A restart builds new fakes, whose sources run (SDD §8.1): stop again those a simulated
    cutover stopped, so the source watch flags only a source the demo powered on."""
    plans = {p.id: p for p in await orchestrator.db.list("plan", Plan)}
    for m in await orchestrator.db.list("migration", Migration):
        stopped = m.phase in _SOURCE_STOPPED or (m.downtime_started_at and not m.downtime_ended_at)
        plan = plans.get(m.plan_id)
        if not stopped or plan is None:
            continue
        source = await orchestrator.source_client(plan)
        power_off = getattr(source, "power_off", None)
        if power_off is not None:
            await power_off(m.vm.source_id)


async def _restore_destination_servers(orchestrator: Orchestrator) -> None:
    """A restart builds new fake destinations, whose Placement holds no server (SDD §10): hand
    them again the servers the migrations recorded, each with the flavor its plan names for it,
    as the simulated cutover created it (§7.4), so the GPU units they hold stay taken."""
    providers = {p.id: p for p in await orchestrator.db.list("provider", Provider)}
    plans = {p.id: p for p in await orchestrator.db.list("plan", Plan)}
    for m in await orchestrator.db.list("migration", Migration):
        plan = plans.get(m.plan_id)
        if not m.destination_server_id or plan is None:
            continue
        source = providers.get(plan.source_provider_id)
        destination = providers.get(plan.destination_provider_id)
        if source is None or destination is None:
            continue
        create = getattr(orchestrator.providers.get(destination), "create_server", None)
        if create is None:
            continue
        try:
            await create(m.destination_server_id, server_flavor(m.vm, plan, source))
        except ProviderError as exc:
            log.warning("demo server of %s not restored: %s", m.vm.name, exc)


async def _seed_cutover_approvals(orchestrator: Orchestrator, plan_id: str) -> None:
    """Two cutovers of the first wave, requested and approved by two people (ADV-17); the rest
    of the plan waits for an approver."""
    plan = await orchestrator.db.get("plan", plan_id, Plan)
    order = {wave.id: index for index, wave in enumerate(plan.waves)}
    migrations = await orchestrator.db.list("migration", Migration, plan_id=plan_id)
    waiting = sorted(
        (m for m in migrations if m.phase == Phase.ready),
        key=lambda m: (order.get(m.wave_id or "", len(order)), m.vm.name),
    )
    for migration, comment in zip(waiting, DEMO_APPROVAL_COMMENTS, strict=False):
        await orchestrator.request_cutover(migration.id, DEMO_REQUESTER)
        await orchestrator.approve(migration.id, DEMO_APPROVER, comment)
