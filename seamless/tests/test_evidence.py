"""ADV-02: the evidence of a strategy recommendation (SDD §4.2, §14.4)."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from seamless_migrate.ai.evidence import build_strategy_evidence, evidence_count, keep_reasons
from seamless_migrate.ai.knowledge import KnowledgeService
from seamless_migrate.ai.policies import builtin_default
from seamless_migrate.api.app import create_app
from seamless_migrate.config import Settings
from seamless_migrate.demo import DEMO_PROVIDERS
from seamless_migrate.domain.enums import Phase, ProviderKind, Role, Strategy
from seamless_migrate.domain.models import (
    AdvisorPolicySpec,
    CutoverWindow,
    Estimate,
    EvidenceItem,
    Finding,
    Plan,
    VMRef,
)
from seamless_migrate.events import EventBus
from seamless_migrate.planning.preflight import finding
from seamless_migrate.store import Store
from tests.api_support import api_settings
from tests.factories import make_disk, make_migration, make_plan, make_vm
from tests.test_knowledge import memory_with

GIB = 2**30


def est(strategy: Strategy, downtime_s: float, meets_slo: bool) -> Estimate:
    return Estimate(
        strategy=strategy,
        precopy_s=600,
        passes=3,
        downtime_s=downtime_s,
        total_s=downtime_s + 600,
        final_delta_bytes=GIB,
        meets_slo=meets_slo,
    )


def by_key(items: list[EvidenceItem]) -> dict[str, EvidenceItem]:
    return {i.key: i for i in items}


def vmware_case(**vm_kw):
    vm = make_vm(
        source_id="vm-17",
        os_type="rhel9",
        cbt_enabled=True,
        disks=[
            make_disk(id="d1", size_gb=100, used_gb=40.0),
            make_disk(id="d2", size_gb=50, used_gb=10.0, bootable=False),
        ],
        **vm_kw,
    )
    plan = make_plan(
        id="plan-0000abcd",
        source_provider_id="vcenter-1",
        downtime_slo_s=900,
        cutover_window=CutoverWindow(
            start=datetime(2026, 10, 11, 22, tzinfo=UTC), end=datetime(2026, 10, 12, 2, tzinfo=UTC)
        ),
    )
    estimates = [est(Strategy.vmware_warm, 240, True), est(Strategy.vmware_cold, 3000, False)]
    return vm, plan, estimates


def test_each_fact_supports_opposes_or_gives_context_against_the_option():
    vm, plan, estimates = vmware_case(change_rate_bps=4 * 2**20)
    items = build_strategy_evidence(
        vm,
        plan,
        ProviderKind.vmware,
        estimates,
        [],
        "vmware_warm",
        None,
        migration_id="mig-0123456789",
    )
    assert [i.key for i in items] == [
        "downtime_estimate",
        "cbt",
        "mappings",
        "findings",
        "guest_os",
        "disk_total",
        "disk_count",
        "write_rate",
        "cutover_window",
        "source_kind",
    ]
    assert [i.weight for i in items] == sorted((i.weight for i in items), reverse=True)
    got = {i.key: (i.value, i.effect, i.source.kind, i.source.ref_id) for i in items}
    assert got == {
        "downtime_estimate": (240, "supports", "estimate", "mig-0123456789"),
        "cbt": (True, "supports", "inventory", "vm-17"),
        "mappings": (0, "supports", "preflight", "mig-0123456789"),
        "findings": (0, "supports", "preflight", "mig-0123456789"),
        "guest_os": ("RHEL 9", "supports", "inventory", "vm-17"),
        "disk_total": (150 * GIB, "context", "inventory", "vm-17"),
        "disk_count": (2, "context", "inventory", "vm-17"),
        "write_rate": (4 * 2**20, "context", "inventory", "vm-17"),
        "cutover_window": (
            "2026-10-11T22:00:00+00:00/2026-10-12T02:00:00+00:00",
            "context",
            "plan",
            "plan-0000abcd",
        ),
        "source_kind": ("vmware", "context", "provider", "vcenter-1"),
    }
    displays = {i.key: i.display for i in items}
    assert displays["downtime_estimate"] == "Estimated downtime 4m 00s, SLO 15m 00s"
    assert displays["cbt"] == "Changed Block Tracking is on"
    assert displays["guest_os"] == "RHEL 9; virt-v2v supported"
    assert displays["disk_total"] == "150 GiB provisioned, 50.0 GiB used"
    assert displays["disk_count"] == "2 disks"
    assert displays["write_rate"] == "Guest write rate 4.0 MiB/s, measured"
    assert displays["source_kind"] == "Source platform: VMware vSphere"
    assert displays["cutover_window"] == "Cutover window: 2026-10-11 22:00 – 2026-10-12 02:00 UTC"
    # five facts bear on the decision; context does not count
    assert evidence_count(items) == 5


def test_the_effects_follow_the_option_measured():
    vm, plan, estimates = vmware_case()
    cold = by_key(
        build_strategy_evidence(
            vm, plan, ProviderKind.vmware, estimates, [], "vmware_cold", None, migration_id="mig-1"
        )
    )
    assert cold["downtime_estimate"].effect == "opposes"  # over the SLO
    assert cold["downtime_estimate"].display == "Estimated downtime 50m 00s, SLO 15m 00s"
    assert cold["cbt"].effect == "context"  # a cold copy needs no CBT
    assert cold["guest_os"].effect == "supports"  # VMware options convert with virt-v2v
    off = make_vm(**{**vm.model_dump(), "cbt_enabled": False})
    warm = by_key(
        build_strategy_evidence(
            off, plan, ProviderKind.vmware, estimates, [], "vmware_warm", None, migration_id="mig-1"
        )
    )
    assert (warm["cbt"].value, warm["cbt"].effect, warm["cbt"].display) == (
        False,
        "opposes",
        "Changed Block Tracking is off",
    )


@pytest.mark.parametrize(
    ("os_type", "option", "effect", "display"),
    [
        ("rhel6", "vmware_warm", "opposes", "RHEL 6; virt-v2v unverified"),
        ("rhel9", "warm", "context", "RHEL 9; virt-v2v supported"),  # OpenStack: no conversion
    ],
)
def test_the_guest_os_bears_on_a_conversion_only(os_type, option, effect, display):
    vm = make_vm(os_type=os_type)
    kind = ProviderKind.vmware if option.startswith("vmware") else ProviderKind.openstack
    item = by_key(
        build_strategy_evidence(vm, make_plan(), kind, [], [], option, None, migration_id="mig-1")
    )["guest_os"]
    assert (item.effect, item.display) == (effect, display)


def test_an_unknown_fact_is_left_out():
    vm = make_vm(os_type=None, cbt_enabled=None, change_rate_bps=None)
    items = by_key(
        build_strategy_evidence(
            vm, make_plan(), ProviderKind.vmware, [], [], "vmware_cold", None, migration_id="mig-1"
        )
    )
    # no estimate for the option, CBT not reported, OS not identified, write rate not measured
    assert {"downtime_estimate", "cbt", "guest_os", "write_rate"}.isdisjoint(items)
    # OpenStack sources have no CBT item at all
    osp = by_key(
        build_strategy_evidence(
            make_vm(cbt_enabled=True),
            make_plan(),
            ProviderKind.openstack,
            [],
            [],
            "warm",
            None,
            migration_id="mig-1",
        )
    )
    assert "cbt" not in osp
    # a plan without a window is open any time: a known fact
    assert (osp["cutover_window"].value, osp["cutover_window"].display) == (
        None,
        "Cutover window: any time",
    )


def test_preflight_findings_that_apply_to_the_option_oppose_it():
    findings = [
        finding("MAP_NETWORK_MISSING", "network app-net has no mapping"),
        finding("MAP_NETWORK_PRESTAGED", "pre-staging creates db-net"),  # info: resolved itself
        finding("MAP_VOLUME_TYPE_MISSING", "volume type ceph-ssd has no mapping"),
        finding("VMW_CBT_DISABLED", "CBT is off"),  # warning that applies to vmware_warm only
        finding("GUEST_OS_UNKNOWN", "the guest could not be identified"),  # info
    ]
    vm = make_vm()
    warm = by_key(
        build_strategy_evidence(
            vm,
            make_plan(),
            ProviderKind.vmware,
            [],
            findings,
            "vmware_warm",
            None,
            migration_id="mig-1",
        )
    )
    assert (warm["mappings"].value, warm["mappings"].effect) == (2, "opposes")
    assert warm["mappings"].display == "Not mapped: MAP_NETWORK_MISSING, MAP_VOLUME_TYPE_MISSING"
    assert (warm["findings"].value, warm["findings"].effect) == (1, "opposes")
    assert (
        warm["findings"].display
        == "1 finding applies to VMware warm migration (CBT): VMW_CBT_DISABLED"
    )
    cold = by_key(
        build_strategy_evidence(
            vm,
            make_plan(),
            ProviderKind.vmware,
            [],
            findings,
            "vmware_cold",
            None,
            migration_id="mig-1",
        )
    )
    assert (cold["findings"].value, cold["findings"].effect) == (0, "supports")
    assert cold["findings"].display == "No blocker or warning applies to VMware cold migration"


def test_an_earlier_run_of_the_same_strategy_opposes_it():
    vm = make_vm()
    failed = make_migration(
        id="mig-earlier01", vm=vm, strategy=Strategy.warm, phase=Phase.ready, attempts=2
    )
    item = by_key(
        build_strategy_evidence(
            vm,
            make_plan(),
            ProviderKind.openstack,
            [],
            [],
            "warm",
            failed,
            migration_id="mig-earlier01",
        )
    )["earlier_runs"]
    assert (item.value, item.effect, item.display) == (
        2,
        "opposes",
        "2 earlier attempts with Warm migration",
    )
    assert (item.source.kind, item.source.ref_id) == ("run", "mig-earlier01")
    other = by_key(
        build_strategy_evidence(
            vm,
            make_plan(),
            ProviderKind.openstack,
            [],
            [],
            "cold",
            failed,
            migration_id="mig-earlier01",
        )
    )
    assert other["earlier_runs"].effect == "context"
    fresh = make_migration(id="mig-earlier01", vm=vm, attempts=0)
    assert "earlier_runs" not in by_key(
        build_strategy_evidence(
            vm,
            make_plan(),
            ProviderKind.openstack,
            [],
            [],
            "warm",
            fresh,
            migration_id="mig-earlier01",
        )
    )


def test_an_evidence_item_has_a_source():
    item = {
        "key": "disk_total",
        "label": "Disks",
        "value": 1,
        "display": "1 B",
        "effect": "context",
        "weight": 60,
    }
    with pytest.raises(ValidationError):
        EvidenceItem(**item)
    with pytest.raises(ValidationError):
        EvidenceItem(**item, source={"kind": "inventory", "ref_id": ""})
    with pytest.raises(ValidationError):
        EvidenceItem(**item, source={"kind": "somewhere", "ref_id": "vm-1"})
    assert (
        EvidenceItem(**item, source={"kind": "inventory", "ref_id": "vm-1"}).source.ref_id == "vm-1"
    )


def test_jev_reasons_keep_only_the_evidence_keys_they_name(caplog):
    vm, plan, estimates = vmware_case()
    items = build_strategy_evidence(
        vm, plan, ProviderKind.vmware, estimates, [], "vmware_warm", None, migration_id="mig-1"
    )
    raw = [
        {"key": "cbt", "text": "Changed Block Tracking is on, so the delta passes are small."},
        {"evidence": "downtime_estimate", "text": "  The estimate fits the SLO.  "},
        {"key": "license_cost", "text": "The licence is cheaper."},
        {"text": "no key"},
        {"key": "cbt", "text": "   "},
        "not a reason",
    ]
    with caplog.at_level(logging.WARNING, logger="seamless_migrate.ai.evidence"):
        kept = keep_reasons(raw, items, recommendation_id="rec-000000000001")
    assert [(r.key, r.text) for r in kept] == [
        ("cbt", "Changed Block Tracking is on, so the delta passes are small."),
        ("downtime_estimate", "The estimate fits the SLO."),
    ]
    assert any("license_cost" in m and "rec-000000000001" in m for m in caplog.messages)


async def test_validation_time_incidents_come_from_memory_at_most_three(store):
    seen = []
    hits = [
        {"title": f"incident {i}", "content": "z" * 3000, "score": 0.9 - i / 10} for i in range(5)
    ]
    service = KnowledgeService(memory_with(hits, seen), store, EventBus(store))
    vm = make_vm(os_type="rhel9", disks=[make_disk(size_gb=120)])
    incidents = await service.similar_for("vmware_warm", vm)
    assert [(i.title, len(i.content), i.score) for i in incidents] == [
        ("incident 0", 1000, 0.9),
        ("incident 1", 1000, 0.8),
        ("incident 2", 1000, 0.7),
    ]
    assert seen[0][1]["query"] == "vmware_warm rhel9 50-200G"
    # memory down: none, and the caller learns it failed (None) rather than an empty answer
    assert (
        await KnowledgeService(memory_with([], [], fail=True), store, EventBus(store)).similar_for(
            "warm", vm
        )
        is None
    )
    assert await KnowledgeService(None, store, EventBus(store)).similar_for("warm", vm) == []


def test_a_new_policy_asks_for_three_evidence_items_the_built_in_default_for_none():
    spec = AdvisorPolicySpec(name="Finance", mode="auto_apply_above_threshold", threshold=0.6)
    assert spec.min_evidence_for_auto_apply == 3
    assert builtin_default(Settings()).min_evidence_for_auto_apply == 0


def test_the_evidence_of_a_recommendation_matches_the_inventory(tmp_path):
    """ADV-02 acceptance: the disk size and CBT state a recommendation's evidence records are the
    values the Inventory page shows (GET /providers/{id}/inventory)."""
    settings, tokens = api_settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'evidence.db'}")
    store.create_schema()
    for provider in DEMO_PROVIDERS:
        store.put("provider", provider)

    def h(role: Role) -> dict[str, str]:
        return {"Authorization": f"Bearer {tokens[role]}"}

    with TestClient(create_app(settings, store)) as client:
        inventory = {
            vm["name"]: vm
            for vm in client.get(
                "/api/v1/providers/vcenter-dc2/inventory", headers=h(Role.viewer)
            ).json()
        }
        # CBT on: both VMware options are eligible; an SLO no option meets lets the advisor in
        names = ["dc2-web-01", "dc2-proxy-01", "dc2-app-01"]
        plan = client.post(
            "/api/v1/plans",
            headers=h(Role.operator),
            json={
                "name": "Evidence check",
                "source_provider_id": "vcenter-dc2",
                "destination_provider_id": "rhoso18",
                "vm_ids": [inventory[n]["source_id"] for n in names],
                "downtime_slo_s": 1,
                "mappings": {"networks": {"DC2-Prod": "dc2-prod", "DC2-DMZ": "dc2-dmz"}},
            },
        )
        assert plan.status_code == 201, plan.text
        plan_id = plan.json()["id"]
        validated = client.post(f"/api/v1/plans/{plan_id}/validate", headers=h(Role.operator))
        assert validated.status_code == 200, validated.text
        recs = client.get(
            f"/api/v1/advisor/recommendations?plan_id={plan_id}&limit=200", headers=h(Role.viewer)
        ).json()["items"]
    store.dispose()
    assert sorted(r["subject"]["name"] for r in recs) == sorted(names)
    for rec in recs:
        vm = inventory[rec["subject"]["name"]]
        evidence = {i["key"]: i for i in rec["evidence"]}
        assert evidence["disk_total"]["value"] == vm["disk_bytes"], vm["name"]
        assert evidence["cbt"]["value"] is vm["cbt_enabled"] is True, vm["name"]
        # every source resolves to this recommendation's VM, migration, plan or provider
        refs = {rec["subject"]["id"], rec["migration_id"], rec["plan_id"], rec["provider_id"]}
        assert all(item["source"]["ref_id"] in refs for item in rec["evidence"]), rec["evidence"]
        # Jev is off here: the effects are measured against the deterministic choice
        assert rec["recommendation"]["option"] is None
        assert rec["evidence_option"] == rec["decision"]["final_option"]


CASES = json.loads((Path(__file__).parent / "fixtures" / "evidence_cases.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_shared_evidence_cases(case):
    """The case table the dashboard's mock runs too (dashboard/src/api/mockEvidence.test.ts)."""
    vm = VMRef(**case["vm"])
    plan = Plan(**case["plan"])
    earlier = None
    if case["earlier"]:
        earlier = make_migration(vm=vm, plan_id=plan.id, phase=Phase.ready, **case["earlier"])
    items = build_strategy_evidence(
        vm,
        plan,
        case["provider_kind"],
        [Estimate(**e) for e in case["estimates"]],
        [Finding(**f) for f in case["findings"]],
        case["option"],
        earlier,
        migration_id=case["migration_id"],
    )
    assert [i.model_dump(mode="json") for i in items] == case["expected"]
