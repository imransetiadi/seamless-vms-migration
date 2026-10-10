from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Severity, Strategy
from seamless_migrate.domain.models import HandoverConfig, Mappings, Nic
from seamless_migrate.planning.preflight import (
    CATALOG,
    DestinationInventory,
    SourceInventory,
    fitting_flavor,
    resolve_mappings,
    resource_demand,
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
        "SRC_VM_TRANSITIONAL_STATE",
        "SRC_VM_DUPLICATE_NAME",
        "SRC_VM_MULTIATTACH",
        "SRC_VM_EPHEMERAL_ROOT",
        "MAP_NETWORK_MISSING",
        "MAP_NETWORK_PRESTAGED",
        "MAP_FLAVOR_MISSING",
        "MAP_FLAVOR_AUTO",
        "MAP_VOLUME_TYPE_MISSING",
        "DST_QUOTA_INSUFFICIENT",
        "DST_PROJECT_MISSING",
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
        "GUEST_OS_UNKNOWN",
        "GUEST_CONVERSION_UNVERIFIED",
        "GUEST_CONVERSION_UNSUPPORTED",
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


def test_finding_src_vm_transitional_state():
    vm = make_vm(power_state="transitioning")
    f = only(check(vm), "SRC_VM_TRANSITIONAL_STATE")
    assert f.severity == Severity.blocker and "task in flight" in f.message
    assert all(x.code != "SRC_VM_ERROR_STATE" for x in check(vm))
    assert all(x.code != "SRC_VM_TRANSITIONAL_STATE" for x in check(make_vm(power_state="stopped")))


def test_finding_dst_project_missing_and_image_root_quota():
    dst = dst_inv(
        quotas={
            "finance-rhoso": {
                "cores": 100,
                "ram_mb": 10**6,
                "instances": 10,
                "volumes": 10,
                "gigabytes": 10**4,
            },
            "shop-rhoso": {
                "cores": 100,
                "ram_mb": 10**6,
                "instances": 10,
                "volumes": 10,
                "gigabytes": 10**4,
            },
        }
    )
    vm = make_vm(project="finance")
    # mapped to a project the destination does not list
    bad = make_plan(mappings=Mappings(projects={"finance": "fin-rhoso"}))
    f = only(check(vm, plan=bad, dst=dst), "DST_PROJECT_MISSING")
    assert f.severity == Severity.blocker and "fin-rhoso" in f.message
    # unmapped, no same-named project, several destination projects: nothing to charge
    f = only(check(vm, dst=dst), "DST_PROJECT_MISSING")
    assert "2 projects" in f.message
    # a mapping to an existing project, or a single destination project, is fine
    good = make_plan(mappings=Mappings(projects={"finance": "finance-rhoso"}))
    assert all(x.code != "DST_PROJECT_MISSING" for x in check(vm, plan=good, dst=dst))
    assert all(x.code != "DST_PROJECT_MISSING" for x in check(vm))
    # an image_root disk counts as a destination volume unless the plan is cold-only
    image = make_vm(disks=[make_disk(kind="image_root", size_gb=30, volume_type=None)])
    assert resource_demand(image, make_plan())["volumes"] == 1
    assert resource_demand(image, make_plan(default_strategy=Strategy.warm))["gigabytes"] == 30
    assert resource_demand(image, make_plan(default_strategy=Strategy.cold))["volumes"] == 0


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


def test_mapping_targets_must_exist_in_the_destination():
    """A mapping to a flavor, network or volume type that is not in RHOSO blocks (SDD §9.3)."""
    vm = make_vm(
        flavor="m1.huge",
        vcpus=64,
        ram_mb=262144,
        nics=[Nic(network="db-net", mtu=9000)],
        disks=[make_disk(volume_type="ceph-hdd")],
    )
    plan = make_plan(
        mappings=Mappings(
            flavors={"m1.huge": "m1.larg"},  # typo
            networks={"db-net": "gone-net"},
            volume_types={"ceph-hdd": "ceph-nvme"},
        )
    )
    findings = {f.code: f for f in check(vm, plan=plan)}
    assert "m1.larg" in findings["MAP_FLAVOR_MISSING"].message
    assert findings["MAP_FLAVOR_MISSING"].severity == Severity.blocker
    assert "db-net -> gone-net" in findings["MAP_NETWORK_MISSING"].message
    assert findings["MAP_NETWORK_MISSING"].severity == Severity.blocker
    assert "ceph-hdd -> ceph-nvme" in findings["MAP_VOLUME_TYPE_MISSING"].message
    assert findings["MAP_VOLUME_TYPE_MISSING"].severity == Severity.blocker
    # mappings to existing objects raise nothing
    good = make_plan(
        mappings=Mappings(
            flavors={"m1.huge": "m1.large"},
            networks={"db-net": "app-net"},
            volume_types={"ceph-hdd": "ceph-ssd"},
        )
    )
    assert all(not x.code.startswith("MAP_") for x in check(vm, plan=good))


def test_vmware_sources_are_never_prestaged():
    """The kit has no pre-stage step: an unmapped port group blocks instead of informing."""
    vmware = make_provider(id="vc", kind=ProviderKind.vmware, cloud=None, credentials_secret="s")
    vm = make_vm(nics=[Nic(network="VM Network")])
    f = only(check(vm, source=vmware), "MAP_NETWORK_MISSING")
    assert f.severity == Severity.blocker and "VMware sources are not pre-staged" in f.message
    assert all(x.code != "MAP_NETWORK_PRESTAGED" for x in check(vm, source=vmware))
    mapped = make_plan(mappings=Mappings(networks={"VM Network": "app-net"}))
    assert all(not x.code.startswith("MAP_NETWORK") for x in check(vm, plan=mapped, source=vmware))


def test_flavor_auto_match_skips_constrained_flavors_and_warns_about_hw_specs():
    dst = dst_inv(
        flavors=[
            {
                "name": "gpu.small",
                "vcpus": 4,
                "ram_mb": 8192,
                "disk_gb": 20,
                "extra_specs": {"resources:VGPU": "1"},
            },
            {
                "name": "pinned.small",
                "vcpus": 4,
                "ram_mb": 8192,
                "disk_gb": 20,
                "extra_specs": {"aggregate_instance_extra_specs:pinned": "true"},
            },
            {
                "name": "pci.small",
                "vcpus": 4,
                "ram_mb": 8192,
                "disk_gb": 20,
                "extra_specs": {"pci_passthrough:alias": "a1:1"},
            },
            {"name": "m2.medium", "vcpus": 4, "ram_mb": 8192, "disk_gb": 40, "extra_specs": {}},
        ]
    )
    vm = make_vm(flavor="custom.4x8", vcpus=4, ram_mb=8192)
    assert fitting_flavor(vm, dst)["name"] == "m2.medium"
    f = only(check(vm, dst=dst), "MAP_FLAVOR_AUTO")
    assert f.severity == Severity.info and "'m2.medium'" in f.message
    # a source flavor with hw:* specs: the match drops them, so the finding is a warning
    pinned = make_vm(
        flavor="custom.4x8",
        vcpus=4,
        ram_mb=8192,
        flavor_extra_specs={"hw:cpu_policy": "dedicated", "hw:mem_page_size": "large"},
    )
    f = only(check(pinned, dst=dst), "MAP_FLAVOR_AUTO")
    assert f.severity == Severity.warning and "hw:cpu_policy, hw:mem_page_size" in f.message
    # only constrained flavors fit: nothing is matched automatically
    constrained_only = dst_inv(flavors=[dst.flavors[0], dst.flavors[2]])
    assert fitting_flavor(vm, constrained_only) is None
    assert only(check(vm, dst=constrained_only), "MAP_FLAVOR_MISSING")


def test_vmware_vms_get_no_flavor_auto_finding():
    """A VMware VM has no flavor; the kit sizes the server, so only the blocker applies."""
    vm = make_vm(flavor=None, vcpus=4, ram_mb=8192)
    assert all(x.code != "MAP_FLAVOR_AUTO" for x in check(vm))
    assert resolve_mappings(vm, make_plan(), dst_inv()) == Mappings()
    huge = make_vm(flavor=None, vcpus=64, ram_mb=262144)
    assert only(check(huge), "MAP_FLAVOR_MISSING")


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
    """Legacy = out of standard vendor support as of the catalog date (SDD §9.5)."""
    legacy = (
        "rhel6",
        "centos-5.11",
        "rhel6_64Guest",
        "centos7",
        "rhel7",
        "ubuntu 18.04",
        "debian10",
        "sles12",
        "windows2008",
        "windows-server-2003",
        "Microsoft Windows Server 2012 R2 Standard",
    )
    for os_type in legacy:
        f = only(check(make_vm(os_type=os_type)), "GUEST_OS_LEGACY")
        assert f.severity == Severity.warning
        assert f.message.startswith(f"Guest OS {make_vm(os_type=os_type).guest_os.label}")
    modern = (
        "rhel9",
        "rhel-8.10",
        "rocky 9",
        "ubuntu 24.04",
        "debian12",
        "windows2019",
        "windows-server-2022",
        None,
    )
    for os_type in modern:
        assert all(x.code != "GUEST_OS_LEGACY" for x in check(make_vm(os_type=os_type)))


def test_finding_guest_os_unknown():
    for os_type in (None, "", "solaris11"):
        assert only(check(make_vm(os_type=os_type)), "GUEST_OS_UNKNOWN").severity == Severity.info
    for known in ("rhel9", "linux", "windows"):
        assert all(x.code != "GUEST_OS_UNKNOWN" for x in check(make_vm(os_type=known)))


VMWARE = make_provider(id="vc", kind=ProviderKind.vmware, conversion_host=None)


def test_finding_guest_conversion_unverified():
    for os_type in ("ubuntu64Guest", "debian12_64Guest", "rockylinux_64Guest", "rhel6_64Guest"):
        f = only(check(make_vm(os_type=os_type), source=VMWARE), "GUEST_CONVERSION_UNVERIFIED")
        assert f.severity == Severity.warning
        assert set(f.strategies) == {Strategy.vmware_cold, Strategy.vmware_warm}
    # OpenStack guests are not converted, and supported guests need no warning
    assert all(
        x.code != "GUEST_CONVERSION_UNVERIFIED" for x in check(make_vm(os_type="ubuntu 22.04"))
    )
    supported = check(make_vm(os_type="rhel9_64Guest"), source=VMWARE)
    assert not {x.code for x in supported} & {
        "GUEST_CONVERSION_UNVERIFIED",
        "GUEST_CONVERSION_UNSUPPORTED",
    }


def test_finding_guest_conversion_unsupported():
    for os_type in ("windows8Server64Guest", "winLonghorn64Guest", "rhel5_64Guest"):
        f = only(check(make_vm(os_type=os_type), source=VMWARE), "GUEST_CONVERSION_UNSUPPORTED")
        assert f.severity == Severity.warning
        assert "virtio" in (f.remediation or "")
    assert all(
        x.code != "GUEST_CONVERSION_UNSUPPORTED"
        for x in check(make_vm(os_type="windows2008"))  # OpenStack source: no conversion
    )


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


def test_catalog_matches_sdd_9_3():
    """Every finding code of SDD §9.3 is in the catalog and vice versa, with the severity the
    table states for its row (rows only; info variants named inside a row are checked by code)."""
    import re

    from seamless_migrate.config import find_repo_root

    sdd = (find_repo_root() / "docs" / "SDD.md").read_text(encoding="utf-8")
    section = sdd[sdd.index("### 9.3") : sdd.index("### 9.4")]
    codes = set(re.findall(r"`([A-Z][A-Z0-9_]{4,})`", section))
    assert codes == set(CATALOG)
    for code, severity in re.findall(
        r"^\| `([A-Z0-9_]+)` \| (blocker|warning|info)", section, re.M
    ):
        assert CATALOG[code].severity == severity, code
