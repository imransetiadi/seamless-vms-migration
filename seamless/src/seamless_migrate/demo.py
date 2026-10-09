"""Demo seeding (``seamless serve --demo``): providers and two running plans.

Idempotent: providers and plans that already exist (by id / name) are left untouched. Plans are
auto-waved, validated and started so the dashboard shows every phase within minutes: the finance
plan cuts over automatically (simulated failures exercise rollback), the VMware plan waits for
approvals. VMs blocked by pre-flight are cancelled with an explanatory reason.
"""

from __future__ import annotations

import logging

from .config import Settings
from .domain.enums import Phase, ProviderKind, ProviderRole
from .domain.models import (
    ConversionHostConfig,
    Mappings,
    Migration,
    Plan,
    Provider,
    VerificationConfig,
)
from .orchestrator import Orchestrator
from .providers.fake import FakeSourceProvider
from .store import Store

log = logging.getLogger(__name__)

FINANCE_PLAN = "Finance apps (RHOSP 17.1 → RHOSO)"
VMWARE_PLAN = "DC2 VMware exit"
DEMO_PLAN_NAMES = (FINANCE_PLAN, VMWARE_PLAN)
DEMO_ACTOR = "demo"

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
        ),
    ),
)


async def _plans(settings: Settings) -> list[Plan]:
    finance_vms = await FakeSourceProvider(ProviderKind.openstack, settings.demo_seed).list_vms()
    vmware_vms = await FakeSourceProvider(ProviderKind.vmware, settings.demo_seed).list_vms()
    return [
        Plan(
            name=FINANCE_PLAN,
            description="Side-by-side migration of the finance estate; cuts over automatically.",
            source_provider_id="rhosp17-finance",
            destination_provider_id="rhoso18",
            vm_ids=[vm.source_id for vm in finance_vms],
            mappings=Mappings(
                networks={"finance-app": "finance-app", "finance-db": "finance-db"},
                volume_types={"ceph-ssd": "ceph-ssd", "ceph-hdd": "ceph-hdd"},
            ),
            downtime_slo_s=600,
            require_approval=False,
            auto_cutover=True,
            keep_warm_interval_s=120,
            verification=VerificationConfig(timeout_s=120),
        ),
        Plan(
            name=VMWARE_PLAN,
            description="VMware exit for DC2; cutovers wait for an approver.",
            source_provider_id="vcenter-dc2",
            destination_provider_id="rhoso18",
            vm_ids=[vm.source_id for vm in vmware_vms],
            mappings=Mappings(networks={"DC2-Prod": "dc2-prod", "DC2-DMZ": "dc2-dmz"}),
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
    """Create the demo providers and plans (idempotent) and start the plans."""
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
        log.info("demo plan %r seeded and started", plan.name)
