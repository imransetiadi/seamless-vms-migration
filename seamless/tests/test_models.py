import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from seamless_migrate.domain.enums import (
    Phase,
    PlanStatus,
    ProviderKind,
    ProviderRole,
    Role,
    Strategy,
    SyncPassKind,
)
from seamless_migrate.domain.models import (
    AdvisorNote,
    CutoverWindow,
    Estimate,
    Event,
    Finding,
    Migration,
    Plan,
    Provider,
    SyncPass,
    new_migration_id,
    new_plan_id,
)
from tests.factories import GIB, make_disk, make_migration, make_plan, make_provider, make_vm


def test_vmref_used_bytes_fallback_is_60_percent():
    vm = make_vm(
        disks=[
            make_disk(size_gb=100, used_gb=None),
            make_disk(id="vol-2", size_gb=50, used_gb=40.0),
        ]
    )
    assert vm.disk_bytes == 150 * GIB
    # one disk lacks used_gb -> 60 % of the provisioned size (floored)
    assert vm.used_bytes == int(150 * GIB * 0.6)

    exact = make_vm(disks=[make_disk(size_gb=100, used_gb=12.5), make_disk(id="v2", used_gb=0.5)])
    assert exact.used_bytes == int(13 * GIB)


def test_vmref_derived_sizes_are_serialized():
    vm = make_vm(disks=[make_disk(size_gb=10, used_gb=None)])
    dumped = json.loads(vm.model_dump_json())
    assert dumped["disk_bytes"] == 10 * GIB
    assert dumped["used_bytes"] == int(10 * GIB * 0.6)


def test_models_roundtrip_json():
    plan = make_plan(
        strategy_overrides={"vm-1": Strategy.cold},
        cutover_window=CutoverWindow(
            start=datetime(2026, 10, 10, 22, tzinfo=UTC), end=datetime(2026, 10, 11, 4, tzinfo=UTC)
        ),
    )
    assert Plan.model_validate_json(plan.model_dump_json()) == plan

    mig = make_migration(
        phase=Phase.awaiting_cutover,
        sync_passes=[
            SyncPass(
                number=1,
                kind=SyncPassKind.full,
                started_at=datetime(2026, 10, 8, 1, tzinfo=UTC),
                ended_at=datetime(2026, 10, 8, 2, tzinfo=UTC),
                bytes_scanned=10,
                bytes_changed=5,
                bytes_transferred=5,
                duration_s=3600.0,
            )
        ],
        estimate=Estimate(
            strategy=Strategy.warm,
            eligible=True,
            precopy_s=10.0,
            passes=2,
            downtime_s=300.0,
            total_s=310.0,
            final_delta_bytes=1024,
            meets_slo=True,
        ),
        findings=[
            Finding(code="NET_MTU_SHRINK", severity="warning", message="mtu", remediation=None)
        ],
        advisor_notes=[
            AdvisorNote(kind="strategy", source="rules", summary="kept", confidence=None)
        ],
    )
    assert Migration.model_validate_json(mig.model_dump_json()) == mig


def test_timestamps_serialize_as_utc_z():
    plan = make_plan(created_at=datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC))
    data = json.loads(plan.model_dump_json())
    assert data["created_at"] == "2026-10-08T12:00:00Z"
    # naive datetimes are interpreted as UTC
    ev = Event(
        seq=1, ts=datetime(2026, 10, 8, 12, 0, 0), kind="plan.created", actor="a", message="m"
    )
    assert json.loads(ev.model_dump_json())["ts"] == "2026-10-08T12:00:00Z"


def test_ids_have_documented_shapes():
    assert new_plan_id().startswith("plan-") and len(new_plan_id()) == len("plan-") + 8
    assert new_migration_id().startswith("mig-") and len(new_migration_id()) == len("mig-") + 10
    assert make_plan().id.startswith("plan-")
    assert make_migration().id.startswith("mig-")


def test_provider_id_regex_enforced():
    make_provider(id="rhoso-18")
    for bad in ("A", "-abc", "x", "has_underscore", "a" * 64):
        with pytest.raises(ValidationError):
            make_provider(id=bad)


def test_provider_release_is_optional_and_validated_as_a_series():
    """SDD §4.2: a provider declares no series unless its operator does, and a declared one reads
    as releases.openstack.org names them - a year, a dot, 1 or 2 (§10.1, §21.3)."""
    assert make_provider().release is None
    for series in ("2023.1", "2024.2", "2026.2", "2027.1"):
        assert make_provider(release=series).release == series
    for bad in (
        "2026.3",
        "26.2",
        "2026.2.1",
        "2026",
        "Hibiscus",
        " 2026.2",
        "2026.2 ",
        "1999.1",
        "",
    ):
        with pytest.raises(ValidationError, match="release"):
            make_provider(release=bad)


def test_a_release_on_a_vmware_provider_is_refused():
    """SDD §4.2: an OpenStack series means nothing for vCenter - refused, never stored."""
    vmware = {"id": "vc-01", "kind": ProviderKind.vmware, "endpoint": "https://vcenter.example"}
    assert make_provider(**vmware).release is None
    with pytest.raises(ValidationError, match="release"):
        make_provider(**vmware, release="2026.2")
    destination = {"id": "dst-hibiscus", "role": ProviderRole.destination, "release": "2026.2"}
    assert make_provider(**destination).release == "2026.2"


def test_provider_policy_profile_is_declared_or_null_and_refused_on_vmware():
    """SDD §4.2, §21.5: a provider declares secure_rbac or legacy, or nothing (by kind and series,
    §10.1); a vmware provider declares none."""
    assert make_provider().policy_profile is None
    for profile in ("secure_rbac", "legacy"):
        assert make_provider(policy_profile=profile).policy_profile == profile
    for bad in ("unknown", "strict", "SECURE_RBAC", ""):
        with pytest.raises(ValidationError, match="policy_profile"):
            make_provider(policy_profile=bad)
    vmware = {"id": "vc-01", "kind": ProviderKind.vmware, "endpoint": "https://vcenter.example"}
    with pytest.raises(ValidationError, match="policy_profile"):
        make_provider(**vmware, policy_profile="legacy")


def test_plan_port_creation_mode_is_nova_unless_neutron_is_chosen():
    """SDD §4.2: "nova" keeps today's ports; "neutron" recreates each with its MAC, every fixed
    IP and its security groups; nothing else is a mode."""
    assert make_plan().port_creation_mode == "nova"
    assert make_plan(port_creation_mode="neutron").port_creation_mode == "neutron"
    with pytest.raises(ValidationError, match="port_creation_mode"):
        make_plan(port_creation_mode="ovs")


def test_plan_nic_overrides_keep_lower_case_macs_and_canonical_addresses():
    """SDD §4.2: per VM and NIC MAC (stored lower case) a RHOSO network and addresses; null keeps
    the mapping or the NIC's own; the addresses in their canonical form."""
    from seamless_migrate.domain.models import invalid_plan_settings

    plan = make_plan(
        nic_overrides={
            "vm-1": {
                "FA:16:3E:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["fd00:0::0005"]},
                "fa:16:3e:00:00:02": {"network": "rhoso-dmz"},
            }
        }
    )
    entries = plan.nic_overrides["vm-1"]
    assert sorted(entries) == ["fa:16:3e:00:00:01", "fa:16:3e:00:00:02"]
    assert entries["fa:16:3e:00:00:01"].fixed_ips == ["fd00::5"]
    assert entries["fa:16:3e:00:00:02"].fixed_ips is None
    assert invalid_plan_settings(plan) == []
    assert make_plan().nic_overrides == {}


def test_plan_nic_overrides_refuse_what_cannot_be_applied():
    """SDD §12: a key that is no MAC, an entry with neither field, an empty address list, a value
    that is no IP address and the same address twice are refused (422)."""
    from seamless_migrate.domain.models import invalid_plan_settings

    plan = make_plan(
        nic_overrides={
            "vm-1": {
                "not-a-mac": {"network": "rhoso-dmz"},
                "fa:16:3e:00:00:01": {},
                "fa:16:3e:00:00:02": {"fixed_ips": []},
                "fa:16:3e:00:00:03": {"fixed_ips": ["10.0.0.300", "10.0.0.5", "10.0.0.5"]},
                "fa:16:3e:00:00:04": {"network": ""},
            }
        }
    )
    problems = invalid_plan_settings(plan)
    assert problems == [
        "nic_overrides[vm-1][fa:16:3e:00:00:01]: give a network, addresses or both",
        "nic_overrides[vm-1][fa:16:3e:00:00:02]: fixed_ips must list at least one address "
        "(leave it out to keep the NIC's own)",
        "nic_overrides[vm-1][fa:16:3e:00:00:03]: not an IP address: 10.0.0.300",
        "nic_overrides[vm-1][fa:16:3e:00:00:03]: 10.0.0.5 given twice",
        "nic_overrides[vm-1][fa:16:3e:00:00:04]: network must not be empty",
        "nic_overrides[vm-1][not-a-mac]: not a MAC address",
    ]


def test_plan_defaults_match_sdd():
    plan = make_plan()
    assert plan.default_strategy == "auto"
    assert plan.selection_policy == "min_downtime"
    assert plan.downtime_slo_s == 600
    assert plan.require_approval is True and plan.auto_cutover is False
    assert plan.keep_warm_interval_s == 900
    assert plan.convergence_threshold_bytes == 1073741824
    assert plan.max_sync_passes == 5
    assert plan.link_bps == 131072000.0
    assert plan.estimator_overrides == {}
    assert plan.prestage_resources == [
        "networks",
        "subnets",
        "routers",
        "router_interfaces",
        "security_groups",
        "security_group_rules",
    ]
    assert plan.status == PlanStatus.draft
    assert plan.verification.console_success_patterns == [
        "login:",
        "Cloud-init v\\. .* finished",
        "Reached target .*Multi-User",
    ]
    assert plan.verification.timeout_s == 600
    assert plan.handover.enabled is False


def test_role_ordering():
    assert Role.viewer.rank < Role.operator.rank < Role.approver.rank < Role.admin.rank
    assert Role.approver.at_least(Role.operator)
    assert not Role.viewer.at_least(Role.operator)


def test_plan_policy_bounds():
    import pytest
    from pydantic import ValidationError

    assert make_plan(keep_warm_interval_s=60).keep_warm_interval_s == 60
    with pytest.raises(ValidationError):
        make_plan(keep_warm_interval_s=10)  # back-to-back delta passes
    with pytest.raises(ValidationError):
        make_plan(downtime_slo_s=0)


def test_vmref_strips_nul_from_tenant_strings():
    """PostgreSQL JSONB rejects NUL characters; tenant-controlled strings are cleaned."""
    vm = make_vm(name="web\x00-01", os_type="lin\x00ux", tags={"ow\x00ner": "fin\x00ance"})
    assert vm.name == "web-01" and vm.os_type == "linux"
    assert vm.tags == {"owner": "finance"}
    assert "\x00" not in vm.model_dump_json()


def test_keep_sync_history_drops_the_oldest_passes_between_the_first_and_the_latest():
    """SDD §5.4: the first max_sync_passes passes and the latest 20 are kept; the bytes of a
    dropped pass move to sync_bytes_dropped and pass numbers keep counting."""
    from seamless_migrate.domain.models import SyncPass, keep_sync_history, next_pass_number, utcnow

    m = make_migration()
    assert next_pass_number(m) == 1
    for n in range(1, 31):
        kind = "full" if n == 1 else "delta"
        m.sync_passes.append(
            SyncPass(number=n, kind=kind, started_at=utcnow(), bytes_transferred=n)
        )
    keep_sync_history(m, keep_first=5)
    assert [p.number for p in m.sync_passes] == [1, 2, 3, 4, 5, *range(11, 31)]
    assert m.sync_bytes_dropped == sum(range(6, 11))
    assert next_pass_number(m) == 31
    keep_sync_history(m, keep_first=5)  # at the bound nothing more is dropped
    assert len(m.sync_passes) == 25 and m.sync_bytes_dropped == sum(range(6, 11))


def test_prestage_resources_name_resources_os_migrate_can_pre_stage():
    """SDD §4.2: an entry without an export/import playbook pair (a typo) or naming the
    workloads themselves is refused; the defaults and "flavors" pass."""
    from seamless_migrate.domain.models import DEFAULT_PRESTAGE, invalid_plan_settings

    assert invalid_plan_settings(make_plan(prestage_resources=[*DEFAULT_PRESTAGE, "flavors"])) == []
    assert invalid_plan_settings(make_plan(prestage_resources=[])) == []
    assert invalid_plan_settings(
        make_plan(prestage_resources=["networks", "flavours", "workloads"])
    ) == ["prestage_resources: cannot pre-stage 'flavours', 'workloads'"]


def test_prestage_resources_are_the_playbook_pairs_of_the_collection():
    from pathlib import Path

    from seamless_migrate.domain.models import PRESTAGE_RESOURCES

    playbooks = Path(__file__).resolve().parents[2] / "playbooks"
    pairs = {
        p.stem.removeprefix("export_")
        for p in playbooks.glob("export_*.yml")
        if (playbooks / ("import_" + p.stem.removeprefix("export_") + ".yml")).exists()
    }
    assert set(PRESTAGE_RESOURCES) == pairs - {"workloads"}


LABEL_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "provider_name_cases.json").read_text(encoding="utf-8")
)["label_cases"]


@pytest.mark.parametrize("case", LABEL_CASES, ids=[c["note"] for c in LABEL_CASES])
def test_a_provider_stored_with_a_blank_name_loads_and_is_named_by_its_id(case):
    """SDD §1 naming, §4.2 (reviews RV20-11, RV21-08): the API refuses a blank name at the request
    boundary, so a provider stored blank before keeps loading; a text names it by its id - blank
    by the rule the dashboard shares (provider_name_cases.json): no character outside Unicode's
    separators, controls and format characters."""
    stored = make_provider(id="dst-case", name="RHOSO").model_dump(mode="json")
    loaded = Provider.model_validate({**stored, "name": case["name"]})
    assert loaded.name == case["name"] and loaded.label == case["label"]
