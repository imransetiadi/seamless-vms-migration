from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Severity, Strategy
from seamless_migrate.domain.models import HandoverConfig, Mappings, Nic
from seamless_migrate.planning.preflight import (
    CATALOG,
    DestinationInventory,
    SourceInventory,
    fitting_flavor,
    resolve_mappings,
    run_preflight,
)
from tests.factories import make_disk, make_plan, make_provider, make_vm

SRC = make_provider()
DST = make_provider(id="dst-rhoso", kind=ProviderKind.rhoso, role=ProviderRole.destination)


def src_inv(**kw) -> SourceInventory:
    data = {"networks": {"app-net": 1500, "db-net": 9000}, "projects": ["finance"]}
    data.update(kw)
    return SourceInventory(**data)


def dst_inv(**kw) -> DestinationInventory:
    data = {
        "networks": {"app-net": 1500},
        "flavors": [
            {"name": "m1.small", "vcpus": 2, "ram_mb": 4096, "disk_gb": 20, "extra_specs": {}},
            {"name": "m1.large", "vcpus": 8, "ram_mb": 16384, "disk_gb": 80, "extra_specs": {}},
        ],
        "volume_types": ["ceph-ssd"],
        "quotas": {
            "finance": {
                "cores": 100,
                "ram_mb": 1_000_000,
                "instances": 50,
                "volumes": 50,
                "gigabytes": 10_000,
            }
        },
        "projects": ["finance"],
    }
    data.update(kw)
    return DestinationInventory(**data)


def check(vm=None, plan=None, src=None, dst=None, all_vms=None, source=SRC, destination=DST):
    vm = vm or make_vm()
    return run_preflight(
        vm,
        plan or make_plan(),
        src or src_inv(),
        dst or dst_inv(),
        all_vms if all_vms is not None else [vm],
        source=source,
        destination=destination,
    )


def only(findings, code):
    matches = [f for f in findings if f.code == code]
    assert matches, f"{code} not reported; got {[f.code for f in findings]}"
    assert len(matches) == 1
    return matches[0]


def test_catalog_is_complete():
    assert set(CATALOG) == {
        "SRC_VM_ERROR_STATE",
        "SRC_VM_DUPLICATE_NAME",
        "SRC_VM_MULTIATTACH",
        "SRC_VM_EPHEMERAL_ROOT",
        "MAP_NETWORK_MISSING",
        "MAP_NETWORK_PRESTAGED",
        "MAP_FLAVOR_MISSING",
        "MAP_FLAVOR_AUTO",
        "MAP_VOLUME_TYPE_MISSING",
        "DST_QUOTA_INSUFFICIENT",
        "NET_MTU_SHRINK",
        "NET_SRIOV_PORT",
        "VM_PCI_PASSTHROUGH",
        "VM_VGPU",
        "VOL_ENCRYPTED",
        "GUEST_OS_LEGACY",
        "VMW_CBT_DISABLED",
        "VMW_INDEPENDENT_DISK",
        "VMW_SNAPSHOTS_PRESENT",
        "VMW_TOOLS_MISSING",
        "CONV_HOST_MISSING",
        "HANDOVER_BACKEND_UNMAPPED",
    }


def test_clean_vm_has_no_findings():
    assert check() == []


def test_finding_src_vm_error_state():
    f = only(check(make_vm(power_state="error")), "SRC_VM_ERROR_STATE")
    assert f.severity == Severity.blocker and f.strategies == [] and f.remediation


def test_finding_src_vm_duplicate_name():
    vm = make_vm()
    twin = make_vm(source_id="vm-2", project="hr")
    f = only(check(vm, all_vms=[vm, twin]), "SRC_VM_DUPLICATE_NAME")
    assert f.severity == Severity.blocker


def test_duplicate_names_blocked():
    """Names are what os-migrate filters on: duplicates across projects block every copy."""
    a = make_vm(source_id="a", name="app.server[1]", project="finance")
    b = make_vm(source_id="b", name="app.server[1]", project="hr")
    c = make_vm(source_id="c", name="app.server[12]", project="hr")
    vms = [a, b, c]
    for vm in (a, b):
        assert only(check(vm, all_vms=vms), "SRC_VM_DUPLICATE_NAME").severity == Severity.blocker
    assert all(f.code != "SRC_VM_DUPLICATE_NAME" for f in check(c, all_vms=vms))


def test_finding_src_vm_multiattach():
    f = only(check(make_vm(disks=[make_disk(multiattach=True)])), "SRC_VM_MULTIATTACH")
    assert f.severity == Severity.warning
    assert set(f.strategies) == {Strategy.warm, Strategy.storage_handover}


def test_finding_src_vm_ephemeral_root():
    vm = make_vm(disks=[make_disk(kind="image_root", volume_type=None)])
    f = only(check(vm), "SRC_VM_EPHEMERAL_ROOT")
    assert f.severity == Severity.info and f.strategies == [Strategy.warm]
    # a data-only ephemeral disk is not the root disk
    data_eph = make_vm(disks=[make_disk(), make_disk(id="e", kind="ephemeral", bootable=False)])
    assert all(x.code != "SRC_VM_EPHEMERAL_ROOT" for x in check(data_eph))


def test_finding_map_network_missing():
    vm = make_vm(nics=[Nic(network="db-net", mtu=9000)])
    no_prestage = make_plan(prestage_resources=["subnets"])
    f = only(check(vm, plan=no_prestage), "MAP_NETWORK_MISSING")
    assert f.severity == Severity.blocker and "db-net" in f.message
    assert all(x.code != "MAP_NETWORK_PRESTAGED" for x in check(vm, plan=no_prestage))
    mapped = make_plan(mappings=Mappings(networks={"db-net": "app-net"}), prestage_resources=[])
    assert all(not x.code.startswith("MAP_NETWORK") for x in check(vm, plan=mapped))


def test_finding_map_network_prestaged():
    # the default plan pre-stages networks: they are created with the same name (info)
    vm = make_vm(nics=[Nic(network="db-net", mtu=9000)])
    f = only(check(vm), "MAP_NETWORK_PRESTAGED")
    assert f.severity == Severity.info and "db-net" in f.message
    assert all(x.code != "MAP_NETWORK_MISSING" for x in check(vm))
    assert "networks" in make_plan().prestage_resources


def test_finding_map_flavor_missing():
    vm = make_vm(flavor="m1.huge", vcpus=64, ram_mb=262144)
    f = only(check(vm), "MAP_FLAVOR_MISSING")
    assert f.severity == Severity.blocker
    assert resolve_mappings(vm, make_plan(), dst_inv()) == Mappings()
    # a destination flavor that fits is enough
    fits = make_vm(flavor="custom.4x8", vcpus=4, ram_mb=8192)
    assert all(x.code != "MAP_FLAVOR_MISSING" for x in check(fits))
    # image-booted VMs also need a large enough flavor root disk
    big_root = make_vm(
        flavor="custom",
        vcpus=2,
        ram_mb=4096,
        disks=[make_disk(kind="image_root", size_gb=200, volume_type=None)],
    )
    assert only(check(big_root), "MAP_FLAVOR_MISSING")
    mapped = make_plan(mappings=Mappings(flavors={"m1.huge": "m1.large"}))
    assert all(x.code != "MAP_FLAVOR_MISSING" for x in check(vm, plan=mapped))


def test_finding_map_flavor_auto_records_resolved_mapping():
    dst = dst_inv(
        flavors=[
            {"name": "m1.large", "vcpus": 8, "ram_mb": 16384, "disk_gb": 80, "extra_specs": {}},
            {"name": "c2.medium", "vcpus": 4, "ram_mb": 8192, "disk_gb": 40, "extra_specs": {}},
            {"name": "m2.medium", "vcpus": 4, "ram_mb": 8192, "disk_gb": 20, "extra_specs": {}},
            {"name": "tiny", "vcpus": 1, "ram_mb": 1024, "disk_gb": 0, "extra_specs": {}},
        ]
    )
    vm = make_vm(flavor="custom.4x8", vcpus=4, ram_mb=8192)
    f = only(check(vm, dst=dst), "MAP_FLAVOR_AUTO")
    assert f.severity == Severity.info and "'m2.medium'" in f.message and "custom.4x8" in f.message
    # the smallest fitting flavor (vCPUs, RAM, then disk) is what the migration will use
    assert fitting_flavor(vm, dst)["name"] == "m2.medium"
    assert resolve_mappings(vm, make_plan(), dst) == Mappings(flavors={"custom.4x8": "m2.medium"})
    # a same-named destination flavor that fits is preferred and needs no finding
    same = make_vm(flavor="m1.large", vcpus=4, ram_mb=8192)
    assert all(not x.code.startswith("MAP_FLAVOR") for x in check(same, dst=dst))
    assert resolve_mappings(same, make_plan(), dst) == Mappings()
    # image-booted roots need the flavor disk too
    big_root = make_vm(
        flavor="custom",
        vcpus=4,
        ram_mb=8192,
        disks=[make_disk(kind="image_root", size_gb=30, volume_type=None)],
    )
    assert fitting_flavor(big_root, dst)["name"] == "c2.medium"
    # an explicit plan mapping disables the automatic match
    mapped = make_plan(mappings=Mappings(flavors={"custom.4x8": "m1.large"}))
    assert all(not x.code.startswith("MAP_FLAVOR") for x in check(vm, plan=mapped, dst=dst))
    assert resolve_mappings(vm, mapped, dst) == Mappings()


def test_finding_map_volume_type_missing():
    vm = make_vm(disks=[make_disk(volume_type="ceph-hdd")])
    f = only(check(vm), "MAP_VOLUME_TYPE_MISSING")
    assert f.severity == Severity.warning and "ceph-hdd" in f.message
    mapped = make_plan(mappings=Mappings(volume_types={"ceph-hdd": "ceph-ssd"}))
    assert all(x.code != "MAP_VOLUME_TYPE_MISSING" for x in check(vm, plan=mapped))
    # when the plan maps volume types the executor preserves them (SDD §6.4): an unmapped
    # type would fail volume creation after the source was stopped, so it blocks
    other = make_vm(disks=[make_disk(volume_type="ceph-nvme")])
    blocker = only(check(other, plan=mapped), "MAP_VOLUME_TYPE_MISSING")
    assert blocker.severity == Severity.blocker and "ceph-nvme" in blocker.message
    assert "mapping" in (blocker.remediation or "")


def test_finding_dst_quota_insufficient():
    tight = dst_inv(
        quotas={
            "finance": {
                "cores": 1,
                "ram_mb": 1_000_000,
                "instances": 5,
                "volumes": 5,
                "gigabytes": 1000,
            }
        }
    )
    f = only(check(dst=tight), "DST_QUOTA_INSUFFICIENT")
    assert f.severity == Severity.blocker and "cores" in f.message


def test_quota_aggregates_across_plan():
    quotas = {
        "finance": {
            "cores": 6,
            "ram_mb": 100_000,
            "instances": 10,
            "volumes": 10,
            "gigabytes": 1000,
        }
    }
    a = make_vm(source_id="a", name="a", vcpus=4)
    b = make_vm(source_id="b", name="b", vcpus=4)
    other = make_vm(source_id="c", name="c", vcpus=4, project="hr")
    vms = [a, b, other]
    # each VM fits on its own, the plan does not: both finance VMs are blocked
    for vm in (a, b):
        assert only(check(vm, dst=dst_inv(quotas=quotas), all_vms=vms), "DST_QUOTA_INSUFFICIENT")
    # the project mapping decides which destination project is charged
    remap = make_plan(mappings=Mappings(projects={"finance": "finance-new"}))
    assert all(
        f.code != "DST_QUOTA_INSUFFICIENT"
        for f in check(a, plan=remap, dst=dst_inv(quotas=quotas), all_vms=vms)
    )
    # an unlimited (-1) quota never blocks
    unlimited = {
        "finance": {"cores": -1, "ram_mb": -1, "instances": -1, "volumes": -1, "gigabytes": -1}
    }
    assert all(
        f.code != "DST_QUOTA_INSUFFICIENT"
        for f in check(a, dst=dst_inv(quotas=unlimited), all_vms=vms)
    )


def test_finding_net_mtu_shrink():
    f = only(check(dst=dst_inv(networks={"app-net": 1442})), "NET_MTU_SHRINK")
    assert f.severity == Severity.warning and "1442" in f.message


def test_finding_net_sriov_port():
    vm = make_vm(nics=[Nic(network="app-net", vnic_type="direct", mtu=1500)])
    assert only(check(vm), "NET_SRIOV_PORT").severity == Severity.warning


def test_finding_vm_pci_passthrough():
    vm = make_vm(flavor_extra_specs={"pci_passthrough:alias": "a1:1"})
    assert only(check(vm), "VM_PCI_PASSTHROUGH").severity == Severity.blocker


def test_finding_vm_vgpu():
    vm = make_vm(flavor_extra_specs={"resources:VGPU": "1"})
    assert only(check(vm), "VM_VGPU").severity == Severity.blocker


def test_finding_vol_encrypted():
    vm = make_vm(disks=[make_disk(encrypted=True)])
    assert only(check(vm), "VOL_ENCRYPTED").severity == Severity.warning


def test_finding_guest_os_legacy():
    for os_type in ("rhel6", "centos-5.11", "rhel6_64Guest", "windows2008", "windows-server-2003"):
        f = only(check(make_vm(os_type=os_type)), "GUEST_OS_LEGACY")
        assert f.severity == Severity.warning
    for modern in ("rhel9", "rhel-8.10", "centos7", "windows2019", "windows-server-2022", None):
        assert all(x.code != "GUEST_OS_LEGACY" for x in check(make_vm(os_type=modern)))


def test_finding_vmw_cbt_disabled():
    f = only(check(make_vm(cbt_enabled=False)), "VMW_CBT_DISABLED")
    assert f.severity == Severity.warning and f.strategies == [Strategy.vmware_warm]
    assert all(x.code != "VMW_CBT_DISABLED" for x in check(make_vm(cbt_enabled=None)))


def test_finding_vmw_independent_disk():
    vm = make_vm(disks=[make_disk(kind="vmdk", independent=True, volume_type=None)])
    f = only(check(vm), "VMW_INDEPENDENT_DISK")
    assert f.severity == Severity.warning and f.strategies == [Strategy.vmware_warm]


def test_finding_vmw_snapshots_present():
    assert only(check(make_vm(snapshot_count=3)), "VMW_SNAPSHOTS_PRESENT").severity == (
        Severity.warning
    )


def test_finding_vmw_tools_missing():
    assert only(check(make_vm(tools_ok=False)), "VMW_TOOLS_MISSING").severity == Severity.info
    assert all(x.code != "VMW_TOOLS_MISSING" for x in check(make_vm(tools_ok=None)))


def test_finding_conv_host_missing():
    no_conv = make_provider(conversion_host=None)
    f = only(check(source=no_conv), "CONV_HOST_MISSING")
    assert f.severity == Severity.warning
    assert set(f.strategies) == {
        Strategy.cold,
        Strategy.warm,
        Strategy.vmware_cold,
        Strategy.vmware_warm,
    }
    # VMware sources only need the RHOSO conversion host
    vcenter = make_provider(id="vcenter", kind=ProviderKind.vmware, conversion_host=None)
    assert all(x.code != "CONV_HOST_MISSING" for x in check(source=vcenter))
    assert only(
        check(
            source=vcenter,
            destination=make_provider(
                id="dst",
                kind=ProviderKind.rhoso,
                role=ProviderRole.destination,
                conversion_host=None,
            ),
        ),
        "CONV_HOST_MISSING",
    )


def test_finding_handover_backend_unmapped():
    plan = make_plan(handover=HandoverConfig(enabled=True, backend_map={"other": "h@b#p"}))
    f = only(check(plan=plan), "HANDOVER_BACKEND_UNMAPPED")
    assert f.severity == Severity.info and f.strategies == [Strategy.storage_handover]
    ok = make_plan(handover=HandoverConfig(enabled=True, backend_map={"ceph-ssd": "h@b#p"}))
    assert check(plan=ok) == []
