import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from seamless_migrate.domain.enums import (
    Phase,
    PlanStatus,
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


def test_vmref_strips_nul_from_tenant_strings():
    """PostgreSQL JSONB rejects NUL characters; tenant-controlled strings are cleaned."""
    vm = make_vm(name="web\x00-01", os_type="lin\x00ux", tags={"ow\x00ner": "fin\x00ance"})
    assert vm.name == "web-01" and vm.os_type == "linux"
    assert vm.tags == {"owner": "finance"}
    assert "\x00" not in vm.model_dump_json()
