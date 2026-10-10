from seamless_migrate.domain.enums import ProviderKind, Severity, Strategy
from seamless_migrate.domain.models import Estimate, Finding, HandoverConfig
from seamless_migrate.planning.selector import eligibility, select_strategy, tie_set
from tests.factories import make_disk, make_plan, make_vm

OK_CAPS = {"admin": True, "conversion_host": True}


def est(strategy: Strategy, downtime: float, eligible: bool = True, slo: float = 300) -> Estimate:
    return Estimate(
        strategy=strategy,
        eligible=eligible,
        reasons=[] if eligible else ["nope"],
        precopy_s=0,
        passes=0,
        downtime_s=downtime,
        total_s=downtime,
        final_delta_bytes=0,
        meets_slo=downtime <= slo,
    )


def test_ineligible_strategies_marked():
    vm = make_vm(power_state="error")
    result = eligibility(vm, ProviderKind.openstack, make_plan(), OK_CAPS, OK_CAPS, [])
    assert set(result) == {Strategy.cold, Strategy.warm, Strategy.storage_handover}
    assert any("error" in r for r in result[Strategy.cold])
    assert any("error" in r for r in result[Strategy.warm])
    assert any("not enabled" in r for r in result[Strategy.storage_handover])

    no_conv = eligibility(
        make_vm(), ProviderKind.openstack, make_plan(), {"admin": True}, OK_CAPS, []
    )
    assert any("conversion host" in r for r in no_conv[Strategy.cold])
    assert any("conversion host" in r for r in no_conv[Strategy.warm])

    vmw = eligibility(make_vm(cbt_enabled=True), ProviderKind.vmware, make_plan(), {}, {}, [])
    assert set(vmw) == {Strategy.vmware_cold, Strategy.vmware_warm}
    assert vmw[Strategy.vmware_cold] == [] and vmw[Strategy.vmware_warm] == []
    assert eligibility(make_vm(power_state="error"), ProviderKind.vmware, make_plan(), {}, {}, [])[
        Strategy.vmware_cold
    ]


def test_blocker_findings_make_all_ineligible():
    blocker = Finding(code="VM_VGPU", severity=Severity.blocker, message="vgpu")
    warning = Finding(code="NET_MTU_SHRINK", severity=Severity.warning, message="mtu")
    result = eligibility(
        make_vm(), ProviderKind.openstack, make_plan(), OK_CAPS, OK_CAPS, [blocker, warning]
    )
    assert all(any("VM_VGPU" in r for r in reasons) for reasons in result.values())


def test_multiattach_blocks_warm():
    vm = make_vm(disks=[make_disk(multiattach=True)])
    result = eligibility(vm, ProviderKind.openstack, make_plan(), OK_CAPS, OK_CAPS, [])
    assert result[Strategy.cold] == []
    assert any("multi-attach" in r for r in result[Strategy.warm])


def test_handover_requires_backend_map_and_admin():
    plan = make_plan(handover=HandoverConfig(enabled=True, backend_map={"ceph-ssd": "h@rbd#pool"}))
    vm = make_vm(disks=[make_disk(volume_type="ceph-ssd")])
    assert (
        eligibility(vm, ProviderKind.openstack, plan, OK_CAPS, OK_CAPS, [])[
            Strategy.storage_handover
        ]
        == []
    )

    unmapped = make_vm(disks=[make_disk(), make_disk(id="d2", volume_type="ceph-hdd")])
    reasons = eligibility(unmapped, ProviderKind.openstack, plan, OK_CAPS, OK_CAPS, [])[
        Strategy.storage_handover
    ]
    assert any("ceph-hdd" in r for r in reasons)

    no_admin = eligibility(vm, ProviderKind.openstack, plan, {"admin": False}, OK_CAPS, [])
    assert any("admin" in r for r in no_admin[Strategy.storage_handover])
    no_dst_admin = eligibility(vm, ProviderKind.openstack, plan, OK_CAPS, {}, [])
    assert any("admin" in r for r in no_dst_admin[Strategy.storage_handover])

    image_root = make_vm(disks=[make_disk(kind="image_root", volume_type=None)])
    assert eligibility(image_root, ProviderKind.openstack, plan, OK_CAPS, OK_CAPS, [])[
        Strategy.storage_handover
    ]
    multi = make_vm(disks=[make_disk(multiattach=True)])
    assert eligibility(multi, ProviderKind.openstack, plan, OK_CAPS, OK_CAPS, [])[
        Strategy.storage_handover
    ]
    disabled = make_plan(handover=HandoverConfig(enabled=False, backend_map={"ceph-ssd": "x"}))
    assert eligibility(vm, ProviderKind.openstack, disabled, OK_CAPS, OK_CAPS, [])[
        Strategy.storage_handover
    ]


def test_vmware_warm_requires_cbt():
    kind, plan = ProviderKind.vmware, make_plan()
    assert eligibility(make_vm(cbt_enabled=False), kind, plan, {}, {}, [])[Strategy.vmware_warm]
    assert eligibility(make_vm(cbt_enabled=None), kind, plan, {}, {}, [])[Strategy.vmware_warm]
    assert (
        eligibility(make_vm(cbt_enabled=True), kind, plan, {}, {}, [])[Strategy.vmware_warm] == []
    )
    independent = make_vm(cbt_enabled=True, disks=[make_disk(kind="vmdk", independent=True)])
    reasons = eligibility(independent, kind, plan, {}, {}, [])[Strategy.vmware_warm]
    assert any("independent" in r for r in reasons)


def test_override_ignored_when_ineligible():
    vm = make_vm()
    estimates = [
        est(Strategy.cold, 1200),
        est(Strategy.warm, 400, eligible=False),
        est(Strategy.storage_handover, 280, eligible=False),
    ]
    plan = make_plan(strategy_overrides={vm.source_id: Strategy.warm})
    strategy, reason = select_strategy(vm, estimates, plan)
    assert strategy == Strategy.cold
    assert "override" in reason and "ignored" in reason

    ok_plan = make_plan(strategy_overrides={vm.source_id: Strategy.cold})
    estimates[1] = est(Strategy.warm, 400)
    assert select_strategy(vm, estimates, ok_plan)[0] == Strategy.cold

    default_plan = make_plan(default_strategy=Strategy.warm)
    assert select_strategy(vm, estimates, default_plan) == (
        Strategy.warm,
        "plan default strategy warm",
    )


def test_min_downtime_tie_prefers_simpler():
    vm, plan = make_vm(), make_plan()
    estimates = [est(Strategy.cold, 1000), est(Strategy.warm, 950)]
    assert tie_set(estimates) == [Strategy.cold, Strategy.warm]
    strategy, reason = select_strategy(vm, estimates, plan)
    assert strategy == Strategy.cold and "tie" in reason

    # relative rule: 9 % apart on a long cutover is still a tie
    wide = [est(Strategy.cold, 3000), est(Strategy.warm, 2750)]
    assert tie_set(wide) == [Strategy.cold, Strategy.warm]

    clear = [est(Strategy.cold, 2000), est(Strategy.warm, 400), est(Strategy.storage_handover, 280)]
    assert tie_set(clear) == [Strategy.storage_handover]
    assert select_strategy(vm, clear, plan)[0] == Strategy.storage_handover

    # ineligible estimates never take part
    assert tie_set([est(Strategy.cold, 100, eligible=False), est(Strategy.warm, 900)]) == [
        Strategy.warm
    ]


def test_simplest_meeting_slo():
    vm = make_vm()
    plan = make_plan(selection_policy="simplest_meeting_slo")
    estimates = [
        est(Strategy.cold, 1000),
        est(Strategy.warm, 250),
        est(Strategy.storage_handover, 290),
    ]
    assert select_strategy(vm, estimates, plan)[0] == Strategy.storage_handover
    none_meet = [est(Strategy.cold, 1000), est(Strategy.warm, 700)]
    strategy, reason = select_strategy(vm, none_meet, plan)
    assert strategy == Strategy.warm and "SLO" in reason


def test_no_eligible_strategy_falls_back_to_simplest():
    vm = make_vm()
    estimates = [est(Strategy.cold, 100, eligible=False), est(Strategy.warm, 50, eligible=False)]
    strategy, reason = select_strategy(vm, estimates, make_plan())
    assert strategy == Strategy.cold and "no eligible" in reason


def _storage(*entries):
    return [
        {"pool": pool, "vendor": None, "protocol": None, "family": fam} for pool, fam in entries
    ]


SRC_NETAPP = {
    "admin": True,
    "storage_backends": _storage(
        ("overcloud@ontap_nfs#192.0.2.5:/cinder_vol", "netapp_nfs"),
        ("overcloud@ontap_nfs#192.0.2.5:/cinder_gold", "netapp_nfs"),
        ("overcloud@lvm#lvm", "other"),
    ),
}
DST_NETAPP = {
    "admin": True,
    "storage_backends": _storage(("hostgroup@ontap_nfs#10.20.0.5:/cinder_vol", "netapp_nfs")),
}


def _netapp_vm(pool):
    return make_vm(
        disks=[
            make_disk(id="v1", volume_type="netapp-nfs", device="/dev/vda", pool=pool),
        ]
    )


NETAPP_PLAN = make_plan(
    handover=HandoverConfig(enabled=True, backend_map={"netapp-nfs": "hostgroup@ontap_nfs"})
)


def test_handover_ineligible_on_unsupported_backend_family():
    vm = _netapp_vm("overcloud@lvm#lvm")
    reasons = eligibility(vm, ProviderKind.openstack, NETAPP_PLAN, SRC_NETAPP, DST_NETAPP)
    assert any(
        "unsupported storage family 'other'" in r for r in reasons[Strategy.storage_handover]
    )
    # cold and warm do not depend on the backend
    assert not any("storage family" in r for r in reasons[Strategy.cold])


def test_handover_ineligible_when_netapp_pool_has_no_destination():
    vm = _netapp_vm("overcloud@ontap_nfs#192.0.2.5:/cinder_gold")
    reasons = eligibility(vm, ProviderKind.openstack, NETAPP_PLAN, SRC_NETAPP, DST_NETAPP)
    [reason] = [r for r in reasons[Strategy.storage_handover] if "v1" in r]
    assert "no pool for the export /cinder_gold" in reason


def test_handover_eligible_on_netapp_nfs_with_matching_export():
    vm = _netapp_vm("overcloud@ontap_nfs#192.0.2.5:/cinder_vol")
    reasons = eligibility(vm, ProviderKind.openstack, NETAPP_PLAN, SRC_NETAPP, DST_NETAPP)
    assert reasons[Strategy.storage_handover] == []
    # a pool the source does not list (stale inventory, no admin at planning) is checked at cutover
    unknown = _netapp_vm("overcloud@elsewhere#x")
    reasons = eligibility(unknown, ProviderKind.openstack, NETAPP_PLAN, SRC_NETAPP, DST_NETAPP)
    assert reasons[Strategy.storage_handover] == []


def test_handover_ineligible_with_encrypted_disk():
    """Cinder refuses to unmanage encrypted volumes (SDD §9.2): handover is ruled out at planning,
    not discovered after the VM was stopped."""
    plan = make_plan(handover=HandoverConfig(enabled=True, backend_map={"ceph-ssd": "h@rbd#pool"}))
    vm = make_vm(disks=[make_disk(id="v1", volume_type="ceph-ssd", encrypted=True)])
    caps = {"admin": True}
    reasons = eligibility(vm, ProviderKind.openstack, plan, caps, caps)
    assert any("encrypted" in r for r in reasons[Strategy.storage_handover])
    assert not any("encrypted" in r for r in reasons[Strategy.cold])
