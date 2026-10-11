from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Severity, Strategy
from seamless_migrate.domain.models import (
    DEFAULT_PRESTAGE,
    AppProbe,
    HandoverConfig,
    Mappings,
    Nic,
    VerificationConfig,
)
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


def check(
    vm=None, plan=None, src=None, dst=None, all_vms=None, source=SRC, destination=DST, dst_caps=None
):
    vm = vm or make_vm()
    return run_preflight(
        vm,
        plan or make_plan(),
        src or src_inv(),
        dst or dst_inv(),
        all_vms if all_vms is not None else [vm],
        source=source,
        destination=destination,
        dst_caps=dst_caps,
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
        "MAP_FLAVOR_PRESTAGED",
        "DST_KEYPAIR_MISSING",
        "MAP_KEYPAIR_PRESTAGED",
        "MAP_VOLUME_TYPE_MISSING",
        "DST_QUOTA_INSUFFICIENT",
        "DST_PROJECT_MISSING",
        "NET_MTU_SHRINK",
        "NET_SRIOV_PORT",
        "NET_PORT_IPS",
        "MAP_NETWORK_AMBIGUOUS",
        "NET_ADDRESS_REFUSED",
        "NET_PORT_NOT_PERMITTED",
        "MAP_NIC_OVERRIDE_INVALID",
        "MAP_NIC_OVERRIDE",
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
        "APP_PROBE_MISSING",
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


def test_finding_map_flavor_prestaged():
    """SDD §9.3: with "flavors" pre-staged a flavor RHOSO lacks is created with the same name
    and specs: an info finding, no automatic match, no blocker."""
    prestage = make_plan(prestage_resources=[*DEFAULT_PRESTAGE, "flavors"])
    for vm in (
        make_vm(flavor="custom.4x8", vcpus=4, ram_mb=8192),  # would be auto-matched
        make_vm(flavor="m1.huge", vcpus=64, ram_mb=262144),  # nothing fits
    ):
        found = check(vm, plan=prestage)
        f = only(found, "MAP_FLAVOR_PRESTAGED")
        assert f.severity == Severity.info and repr(vm.flavor) in f.message
        assert all(x.code not in ("MAP_FLAVOR_MISSING", "MAP_FLAVOR_AUTO") for x in found)
        assert resolve_mappings(vm, prestage, dst_inv()) == Mappings()
    # a same-named destination flavor is used as it is: never pre-staged over
    same = make_vm(flavor="m1.large", vcpus=4, ram_mb=8192)
    assert all(not x.code.startswith("MAP_FLAVOR") for x in check(same, plan=prestage))
    # an explicit mapping wins
    mapped = make_plan(
        prestage_resources=[*DEFAULT_PRESTAGE, "flavors"],
        mappings=Mappings(flavors={"custom.4x8": "m1.large"}),
    )
    vm = make_vm(flavor="custom.4x8", vcpus=4, ram_mb=8192)
    assert all(not x.code.startswith("MAP_FLAVOR") for x in check(vm, plan=mapped))
    # without "flavors" in the list the automatic match stays
    assert only(check(vm), "MAP_FLAVOR_AUTO")


def test_finding_dst_keypair_missing_and_prestaged():
    """SDD §9.3: a key_name the destination credential does not own fails the cutover's
    destination check, so it blocks - unless pre-staging imports it, which needs "keypairs" in
    the list and the source credential to own it (os-migrate exports only its own)."""
    vm = make_vm(key_name="ops-key")
    src = src_inv(keypairs=["ops-key"])
    f = only(check(vm, src=src), "DST_KEYPAIR_MISSING")
    assert f.severity == Severity.blocker and "'ops-key'" in f.message
    prestage = make_plan(prestage_resources=[*DEFAULT_PRESTAGE, "keypairs"])
    found = check(vm, plan=prestage, src=src)
    assert only(found, "MAP_KEYPAIR_PRESTAGED").severity == Severity.info
    assert all(x.code != "DST_KEYPAIR_MISSING" for x in found)
    # owned by another user: pre-staging cannot export it
    other = check(vm, plan=prestage, src=src_inv(keypairs=[]))
    f = only(other, "DST_KEYPAIR_MISSING")
    assert "another user" in f.message.lower()
    assert all(x.code != "MAP_KEYPAIR_PRESTAGED" for x in other)
    # the destination credential owns it, or the VM has no keypair: nothing to say
    for quiet in (
        check(vm, src=src, dst=dst_inv(keypairs=["ops-key"])),
        check(make_vm(key_name=None), plan=prestage, src=src),
    ):
        assert all("KEYPAIR" not in x.code for x in quiet)


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


def test_finding_net_port_ips_names_each_nic_with_its_addresses():
    """SDD §9.3: a NIC without exactly one fixed IP (dual-stack included) is named with its
    addresses; VMware NICs go through the migration kit and are not checked."""
    vm = make_vm(
        nics=[
            Nic(network="app-net", fixed_ips=["10.0.0.5", "fd00::5"], mtu=1500),
            Nic(network="db-net", fixed_ips=[], mtu=9000),
            Nic(network="app-net", fixed_ips=["10.0.0.6"], mtu=1500),
        ]
    )
    f = only(check(vm), "NET_PORT_IPS")
    assert f.severity == Severity.warning
    assert "app-net (10.0.0.5, fd00::5)" in f.message and "db-net (no fixed IP)" in f.message
    assert "10.0.0.6" not in f.message
    assert all(x.code != "NET_PORT_IPS" for x in check(make_vm()))
    vmware = make_provider(id="vc", kind=ProviderKind.vmware, cloud=None, credentials_secret="s")
    assert all(x.code != "NET_PORT_IPS" for x in check(vm, source=vmware))


def test_finding_net_port_ips_only_in_nova_port_mode():
    """SDD §9.3: in "neutron" mode cold and warm keep every address, so nothing is said; the
    handover's eligibility reason remains. In "nova" mode the remediation offers "neutron"."""
    vm = make_vm(nics=[Nic(network="app-net", fixed_ips=["10.0.0.5", "fd00::5"], mtu=1500)])
    nova = only(check(vm), "NET_PORT_IPS")
    assert "neutron" in (nova.remediation or "").lower()
    neutron = make_plan(port_creation_mode="neutron")
    assert all(x.code != "NET_PORT_IPS" for x in check(vm, plan=neutron))


def override_dst(**kw):
    """A destination with a DMZ network beside app-net, and their subnets (SDD §11)."""
    return dst_inv(
        networks={"app-net": 1500, "rhoso-dmz": 1442},
        subnets=[
            {"name": "app-v4", "network": "app-net", "cidr": "10.0.0.0/24"},
            {"name": "dmz-v4", "network": "rhoso-dmz", "cidr": "192.168.10.0/24"},
            {"name": "dmz-v6", "network": "rhoso-dmz", "cidr": "fd00:10::/64"},
        ],
        **kw,
    )


def test_finding_map_nic_override_names_where_each_nic_goes():
    """SDD §9.3: an applicable entry is info naming the NIC, its RHOSO network and addresses; the
    MTU check and the port rule look at where the NIC goes."""
    vm = make_vm(source_id="vm-1")
    plan = make_plan(
        nic_overrides={
            "vm-1": {"fa:16:3e:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["192.168.10.5"]}}
        }
    )
    found = check(vm, plan=plan, dst=override_dst())
    info = only(found, "MAP_NIC_OVERRIDE")
    assert info.severity == Severity.info
    assert "fa:16:3e:00:00:01 (app-net) -> rhoso-dmz: 192.168.10.5" in info.message
    assert all(f.code != "MAP_NIC_OVERRIDE_INVALID" for f in found)
    # rhoso-dmz has MTU 1442: smaller than the NIC's 1500
    assert "rhoso-dmz" in only(found, "NET_MTU_SHRINK").message


def test_a_nic_override_settles_the_network_and_address_count():
    """A NIC sent elsewhere is not a MAP_NETWORK_MISSING; a dual-stack NIC given one address is no
    NET_PORT_IPS in nova mode; a network-only entry keeps the NIC's own addresses."""
    from seamless_migrate.domain.models import Nic

    nic = Nic(network="lost-net", mac="fa:16:3e:00:00:01", fixed_ips=["10.0.0.5", "fd00::5"])
    vm = make_vm(source_id="vm-1", nics=[nic])
    plan = make_plan(
        nic_overrides={
            "vm-1": {"fa:16:3e:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["192.168.10.5"]}}
        }
    )
    codes = [f.code for f in check(vm, plan=plan, dst=override_dst())]
    assert "MAP_NETWORK_MISSING" not in codes and "MAP_NETWORK_PRESTAGED" not in codes
    assert "NET_PORT_IPS" not in codes
    kept = make_plan(
        nic_overrides={"vm-1": {"fa:16:3e:00:00:01": {"network": "rhoso-dmz"}}},
        port_creation_mode="neutron",
    )
    found = check(vm, plan=kept, dst=override_dst())
    # its own 10.0.0.5 lies in no subnet of rhoso-dmz
    invalid = only(found, "MAP_NIC_OVERRIDE_INVALID")
    assert "10.0.0.5" in invalid.message and "rhoso-dmz" in invalid.message


def test_finding_map_nic_override_invalid_names_each_problem():
    """SDD §9.3: an unknown MAC, a network RHOSO lacks, an address in no subnet of the target
    network and an address another NIC of the plan gets block the VM; on a VMware source any entry
    does - the migration kit keeps the guest's network settings."""
    from seamless_migrate.domain.models import Nic

    vm = make_vm(
        source_id="vm-1",
        nics=[
            Nic(network="app-net", mac="fa:16:3e:00:00:01", fixed_ips=["10.0.0.5"]),
            Nic(network="app-net", mac="fa:16:3e:00:00:02", fixed_ips=["10.0.0.6"]),
            Nic(network="app-net", mac="fa:16:3e:00:00:03", fixed_ips=["10.0.0.7"]),
        ],
    )
    other = make_vm(source_id="vm-2", name="db-01")  # keeps 10.0.0.5 on app-net
    plan = make_plan(
        vm_ids=["vm-1", "vm-2"],
        nic_overrides={
            "vm-1": {
                "fa:16:3e:00:00:99": {"network": "rhoso-dmz"},
                "fa:16:3e:00:00:01": {"network": "rhoso-missing"},
                "fa:16:3e:00:00:02": {"fixed_ips": ["172.16.0.9"]},
                "fa:16:3e:00:00:03": {"fixed_ips": ["10.0.0.5"]},
            }
        },
    )
    f = only(
        check(vm, plan=plan, dst=override_dst(), all_vms=[vm, other]), "MAP_NIC_OVERRIDE_INVALID"
    )
    assert f.severity == Severity.blocker
    assert "fa:16:3e:00:00:99: the VM has no NIC with this MAC" in f.message
    assert "fa:16:3e:00:00:01: RHOSO has no network 'rhoso-missing'" in f.message
    assert "fa:16:3e:00:00:02: 172.16.0.9 lies in no subnet of 'app-net'" in f.message
    assert "fa:16:3e:00:00:03: 10.0.0.5 also goes to db-01" in f.message
    # addresses only, on a network pre-staging would create: its subnets cannot be checked yet
    lost = make_vm(
        source_id="vm-3",
        nics=[Nic(network="lost-net", mac="fa:16:3e:00:00:05", fixed_ips=["10.9.0.5"])],
    )
    later = make_plan(nic_overrides={"vm-3": {"fa:16:3e:00:00:05": {"fixed_ips": ["10.9.0.6"]}}})
    f = only(check(lost, plan=later, dst=override_dst()), "MAP_NIC_OVERRIDE_INVALID")
    assert "fa:16:3e:00:00:05: RHOSO has no network 'lost-net' yet" in f.message
    assert "name an existing RHOSO network" in f.message
    vmware = make_provider(id="vc", kind=ProviderKind.vmware, cloud=None, credentials_secret="s")
    vmw = only(check(vm, plan=plan, dst=override_dst(), source=vmware), "MAP_NIC_OVERRIDE_INVALID")
    assert "migration kit keeps the guest's network settings" in vmw.message


def test_finding_map_nic_override_invalid_refuses_what_neutron_gives_no_vm():
    """SDD §9.3, as Neutron's check_subnet_ip: an IPv4 subnet's network address (at any prefix
    length), its broadcast address below a /31 and an IPv6 subnet's first address are no host
    addresses - a typo like 192.168.10.0 would otherwise fail after the source stops."""
    from seamless_migrate.domain.models import Nic

    dst = dst_inv(
        networks={"app-net": 1500, "rhoso-dmz": 1500, "rhoso-p2p": 1500},
        subnets=[
            {"name": "app-v4", "network": "app-net", "cidr": "10.0.0.0/24"},
            {"name": "dmz-v4", "network": "rhoso-dmz", "cidr": "192.168.10.0/24"},
            {"name": "dmz-v6", "network": "rhoso-dmz", "cidr": "fd00:10::/64"},
            {"name": "p2p-v4", "network": "rhoso-p2p", "cidr": "10.255.0.0/31"},
            {"name": "wide-v6", "network": "rhoso-p2p", "cidr": "fd99::/16"},
        ],
    )
    macs = [f"fa:16:3e:00:00:0{i}" for i in range(1, 5)]
    vm = make_vm(
        source_id="vm-1",
        nics=[
            Nic(network="app-net", mac=m, fixed_ips=[f"10.0.0.{i + 5}"]) for i, m in enumerate(macs)
        ],
    )

    def entries(*targets):
        return {
            "vm-1": {
                m: {"network": n, "fixed_ips": a} for m, (n, a) in zip(macs, targets, strict=True)
            }
        }

    reserved = make_plan(
        nic_overrides=entries(
            ("rhoso-dmz", ["192.168.10.0"]),
            ("rhoso-dmz", ["192.168.10.255", "fd00:10::"]),
            ("rhoso-dmz", ["192.168.10.7", "fd00:10::7"]),
            ("rhoso-p2p", ["10.255.0.0"]),
        )
    )
    f = only(check(vm, plan=reserved, dst=dst), "MAP_NIC_OVERRIDE_INVALID")
    assert (
        "fa:16:3e:00:00:01: 192.168.10.0 is the network address of 192.168.10.0/24, "
        "not a host address" in f.message
    )
    assert "fa:16:3e:00:00:02: 192.168.10.255 is the broadcast address of 192.168.10.0/24" in (
        f.message
    )
    assert "fa:16:3e:00:00:02: fd00:10:: is the network address of fd00:10::/64" in f.message
    assert "fa:16:3e:00:00:04: 10.255.0.0 is the network address of 10.255.0.0/31" in f.message
    # one problem per address; the NIC with host addresses has none
    assert f.message.count("not a host address") == 4
    assert "fa:16:3e:00:00:03" not in f.message and "in no subnet" not in f.message
    # host addresses: a /31 and an IPv6 subnet have no broadcast address
    hosts = make_plan(
        nic_overrides=entries(
            ("rhoso-dmz", ["192.168.10.1"]),
            ("rhoso-dmz", ["192.168.10.254"]),
            ("rhoso-dmz", ["fd00:10::1", "fd00:10::ffff:ffff:ffff:ffff"]),
            ("rhoso-p2p", ["10.255.0.1", "fd99:ffff:ffff:ffff:ffff:ffff:ffff:ffff"]),
        )
    )
    found = check(vm, plan=hosts, dst=dst)
    assert all(x.code != "MAP_NIC_OVERRIDE_INVALID" for x in found), found
    assert only(found, "MAP_NIC_OVERRIDE").severity == Severity.info


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


def test_finding_app_probe_missing_for_a_database_vm_without_an_application_probe():
    """SDD §9.3, ADV-18: a database VM verified only by its boot is flagged at validation."""
    db = make_vm(source_id="vm-db", name="ledger-db-01", tags={"role": "postgresql"})
    f = only(check(vm=db), "APP_PROBE_MISSING")
    assert f.severity == Severity.warning and f.strategies == []
    assert f.message == (
        "ledger-db-01 is a database VM without an application probe: verification would check only "
        "that it booted. Suggested: TCP 5432."
    )
    probe = AppProbe(kind="tcp", port=5432)

    def flagged(plan):
        return "APP_PROBE_MISSING" in [x.code for x in check(vm=db, plan=plan)]

    # a probe for every VM, or one for this VM, clears it; an empty override does not
    assert not flagged(make_plan(verification=VerificationConfig(app_probes=[probe])))
    assert not flagged(make_plan(probe_overrides={"vm-db": [probe]}))
    assert flagged(
        make_plan(
            verification=VerificationConfig(app_probes=[probe]), probe_overrides={"vm-db": []}
        )
    )
    # a database that names no engine gets no suggestion; a web VM is never flagged
    plain = only(check(vm=make_vm(name="dc2-db-01")), "APP_PROBE_MISSING")
    assert plain.message.endswith("verification would check only that it booted.")
    assert "APP_PROBE_MISSING" not in [x.code for x in check()]


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


def net(network_id, name, own=True, mtu=1500, project_id=None, shared=None, external=False):
    """A DestinationInventory.network_details entry (SDD §10); another project's is shared
    unless said otherwise."""
    return {
        "id": network_id,
        "name": name,
        "mtu": mtu,
        "project_id": project_id or ("finance-id" if own else f"{network_id}-owner"),
        "own": own,
        "shared": (not own) if shared is None else shared,
        "external": external,
    }


def subnet_of(network_id, name, cidr, mode=None):
    return {
        "name": f"{network_id}-{cidr}",
        "network": name,
        "network_id": network_id,
        "cidr": cidr,
        "ipv6_address_mode": mode,
        "ipv6_ra_mode": mode,
    }


def resolved_dst(details, subnets=(), **kw):
    names = {}
    for d in details:
        names[d["name"]] = d["mtu"]
    return dst_inv(networks=names, network_details=list(details), subnets=list(subnets), **kw)


def test_finding_map_network_ambiguous_resolves_a_name_as_the_cutover_does():
    """SDD §9.3, §7.3 step 0: the credential's own network of the name, else the only one it
    sees; several of its project, or several elsewhere and none of its project, block."""
    vm = make_vm(
        source_id="vm-1",
        nics=[Nic(network="app-net", mac="fa:16:3e:00:00:01", fixed_ips=["10.0.0.5"])],
    )
    plan = make_plan(mappings=Mappings(networks={"app-net": "rhoso-app"}))
    two_own = check(vm, plan=plan, dst=resolved_dst([net("a", "rhoso-app"), net("b", "rhoso-app")]))
    f = only(two_own, "MAP_NETWORK_AMBIGUOUS")
    assert f.severity == Severity.blocker
    assert "fa:16:3e:00:00:01 (app-net)" in f.message
    assert "2 networks of the credential's project are named 'rhoso-app'" in f.message
    elsewhere = resolved_dst([net("a", "rhoso-app", own=False), net("b", "rhoso-app", own=False)])
    f = only(check(vm, plan=plan, dst=elsewhere), "MAP_NETWORK_AMBIGUOUS")
    assert "2 networks of other projects are named 'rhoso-app' (a-owner, b-owner)" in f.message
    # the credential's own among others, or another project's shared one alone: resolved
    for details in (
        [net("x", "rhoso-app", own=False), net("a", "rhoso-app")],
        [net("x", "rhoso-app", own=False)],
    ):
        codes = [x.code for x in check(vm, plan=plan, dst=resolved_dst(details))]
        assert "MAP_NETWORK_AMBIGUOUS" not in codes and "MAP_NETWORK_MISSING" not in codes
    # a VMware NIC too: Seamless resolves its port group's network for the migration kit (§7.2)
    vmware = make_provider(id="vc", kind=ProviderKind.vmware, cloud=None, credentials_secret="s")
    codes = [x.code for x in check(vm, plan=plan, dst=elsewhere, source=vmware)]
    assert "MAP_NETWORK_AMBIGUOUS" in codes
    # an override to such a name: the ambiguity alone, no empty MAP_NIC_OVERRIDE
    moved = make_plan(nic_overrides={"vm-1": {"fa:16:3e:00:00:01": {"network": "rhoso-app"}}})
    codes = [x.code for x in check(vm, plan=moved, dst=elsewhere)]
    assert "MAP_NETWORK_AMBIGUOUS" in codes
    assert "MAP_NIC_OVERRIDE" not in codes and "MAP_NIC_OVERRIDE_INVALID" not in codes


def test_finding_net_address_refused_checks_a_nics_own_addresses_on_its_network():
    """SDD §9.3, §6.6: a NIC without an override keeps addresses Neutron must take on its resolved
    network - Nova keeps the first only; on a SLAAC subnet the kept MAC's EUI-64 address through
    Neutron, none through Nova."""
    nic = Nic(
        network="app-net",
        mac="fa:16:3e:00:00:01",
        fixed_ips=["10.0.0.5", "fd00:20::f816:3eff:fe00:1"],
    )
    vm = make_vm(source_id="vm-1", nics=[nic])
    details = [net("app", "app-net")]
    v4 = subnet_of("app", "app-net", "10.0.0.0/24")
    slaac = subnet_of("app", "app-net", "fd00:20::/64", "slaac")
    nova, neutron = make_plan(), make_plan(port_creation_mode="neutron")
    # Nova keeps 10.0.0.5 only, which its subnet holds
    assert "NET_ADDRESS_REFUSED" not in [
        x.code for x in check(vm, plan=nova, dst=resolved_dst(details, [v4]))
    ]
    f = only(
        check(
            vm, plan=nova, dst=resolved_dst(details, [subnet_of("app", "app-net", "10.9.0.0/24")])
        ),
        "NET_ADDRESS_REFUSED",
    )
    assert f.severity == Severity.blocker
    assert "fa:16:3e:00:00:01 on 'app-net': 10.0.0.5 is no host address of any subnet" in f.message
    # through Neutron every address, the SLAAC one as the kept MAC derives it
    assert "NET_ADDRESS_REFUSED" not in [
        x.code for x in check(vm, plan=neutron, dst=resolved_dst(details, [v4, slaac]))
    ]
    other = make_vm(
        source_id="vm-1",
        nics=[
            Nic(network="app-net", mac="fa:16:3e:00:00:01", fixed_ips=["10.0.0.5", "fd00:20::5"])
        ],
    )
    f = only(
        check(other, plan=neutron, dst=resolved_dst(details, [v4, slaac])), "NET_ADDRESS_REFUSED"
    )
    assert "fd00:20::5 is not fd00:20::f816:3eff:fe00:1" in f.message
    # through Nova a SLAAC address alone: Neutron takes none (a new MAC)
    v6 = make_vm(
        source_id="vm-1",
        nics=[
            Nic(network="app-net", mac="fa:16:3e:00:00:01", fixed_ips=["fd00:20::f816:3eff:fe00:1"])
        ],
    )
    f = only(check(v6, plan=nova, dst=resolved_dst(details, [v4, slaac])), "NET_ADDRESS_REFUSED")
    assert "SLAAC or DHCPv6-stateless" in f.message
    # not checked: a NIC with an override (MAP_NIC_OVERRIDE_*), a network pre-staging creates, an
    # inventory without subnets, a VMware VM
    entry = make_plan(nic_overrides={"vm-1": {"fa:16:3e:00:00:01": {"network": "app-net"}}})
    gone = resolved_dst(details, [subnet_of("app", "app-net", "10.9.0.0/24")])
    assert "NET_ADDRESS_REFUSED" not in [x.code for x in check(vm, plan=entry, dst=gone)]
    later = make_vm(
        source_id="vm-1",
        nics=[Nic(network="new-net", mac="fa:16:3e:00:00:01", fixed_ips=["10.0.0.5"])],
    )
    codes = [x.code for x in check(later, dst=gone)]
    assert "MAP_NETWORK_PRESTAGED" in codes and "NET_ADDRESS_REFUSED" not in codes
    assert "NET_ADDRESS_REFUSED" not in [x.code for x in check(vm, dst=resolved_dst(details))]
    # a VMware NIC's IPv4 address is checked by the migration kit's rule (its own test)
    vmware = make_provider(id="vc", kind=ProviderKind.vmware, cloud=None, credentials_secret="s")
    assert "NET_ADDRESS_REFUSED" in [x.code for x in check(vm, dst=gone, source=vmware)]


def test_finding_net_port_not_permitted_follows_neutrons_rule_for_another_projects_network():
    """SDD §9.3, §6.6: another project's network takes a NIC only when it is shared with the
    credential's project or external, and from a token with the admin or advsvc role - Neutron's
    policy lets only those, or the network's project, choose the address or MAC the cutover asks
    for after the source stops. Without reported roles only the first half is judged."""
    vm = make_vm(
        source_id="vm-1",
        nics=[Nic(network="app-net", mac="FA:16:3E:00:00:01", fixed_ips=["10.0.0.5"])],
    )
    plan = make_plan(mappings=Mappings(networks={"app-net": "rhoso-net"}))
    member = {"admin": False, "roles": ["member", "reader"]}

    def found(network, caps, plan=plan, **kw):
        dst = resolved_dst([network], [subnet_of(network["id"], "rhoso-net", "10.0.0.0/24")])
        return check(vm, plan=plan, dst=dst, dst_caps=caps, **kw)

    shared = net("p", "rhoso-net", own=False, project_id="project-admin")
    external = net(
        "e", "rhoso-net", own=False, project_id="project-admin", shared=False, external=True
    )
    private = net("b", "rhoso-net", own=False, project_id="project-b", shared=False)
    f = only(found(shared, member), "NET_PORT_NOT_PERMITTED")
    assert f.severity == Severity.blocker and f.remediation
    assert f.message == (
        "The RHOSO credential may not put these NICs on their network: fa:16:3e:00:00:01 on "
        "'rhoso-net' (project project-admin): Neutron lets only the admin or advsvc role, or that "
        "project, choose a port's address or MAC there, and the credential's token has neither."
    )
    assert only(found(external, member), "NET_PORT_NOT_PERMITTED").message == f.message
    # with the admin or advsvc role a shared or external network takes it
    for roles in (["admin", "member", "reader"], ["advsvc"]):
        for network in (shared, external):
            assert "NET_PORT_NOT_PERMITTED" not in [
                x.code for x in found(network, {"roles": roles})
            ]
    # another project's private network never does: the admin sees it, the VM must not land there
    for caps in (member, {"admin": True, "roles": ["admin"]}, None, {}):
        f = only(found(private, caps), "NET_PORT_NOT_PERMITTED")
        assert f.message == (
            "The RHOSO credential may not put these NICs on their network: fa:16:3e:00:00:01 on "
            "'rhoso-net' (project project-b): it is neither shared with the credential's project "
            "nor external."
        )
    # no reported roles (a destination checked by another implementation): that half is not judged
    for caps in (None, {"admin": True}):
        assert "NET_PORT_NOT_PERMITTED" not in [x.code for x in found(shared, caps)]
    # the credential's own network, whatever the roles
    own = net("o", "rhoso-net")
    assert "NET_PORT_NOT_PERMITTED" not in [x.code for x in found(own, member)]
    # a NIC override's network is judged the same way
    moved = make_plan(nic_overrides={"vm-1": {"fa:16:3e:00:00:01": {"network": "rhoso-net"}}})
    assert "NET_PORT_NOT_PERMITTED" in [x.code for x in found(shared, member, plan=moved)]
    # a VMware NIC too: the migration kit creates every port with the NIC's MAC (§7.2)
    vmware = make_provider(id="vc", kind=ProviderKind.vmware, cloud=None, credentials_secret="s")
    assert "NET_PORT_NOT_PERMITTED" in [x.code for x in found(shared, member, source=vmware)]


def test_finding_net_address_refused_checks_a_vmware_nic_by_the_migration_kits_rule():
    """SDD §9.3, §7.2: the kit sends a VMware NIC's IPv4 guest addresses only, all with one subnet
    of its network: each must be a host address there, and one subnet must hold them all."""
    vmware = make_provider(id="vc", kind=ProviderKind.vmware, cloud=None, credentials_secret="s")
    details = [net("prod", "dc2-prod")]
    first = subnet_of("prod", "dc2-prod", "10.2.0.0/24")
    second = subnet_of("prod", "dc2-prod", "10.2.1.0/24")
    plan = make_plan(mappings=Mappings(networks={"DC2-Prod": "dc2-prod"}))

    def found(*addresses, subnets=(first, second)):
        vm = make_vm(
            source_id="vm-1",
            nics=[Nic(network="DC2-Prod", mac="00:50:56:AA:BB:CC", fixed_ips=list(addresses))],
        )
        return check(vm, plan=plan, dst=resolved_dst(details, subnets), source=vmware)

    # one subnet holds them; an IPv6 address the kit never sends is not judged
    assert "NET_ADDRESS_REFUSED" not in [x.code for x in found("10.2.1.5", "10.2.1.6", "fd00::5")]
    f = only(found("10.2.0.5", "10.2.1.5"), "NET_ADDRESS_REFUSED")
    assert f.message == (
        "Neutron would refuse these addresses on their RHOSO network: 00:50:56:aa:bb:cc on "
        "'dc2-prod': 10.2.0.5, 10.2.1.5 lie in different subnets of the port's network: the "
        "migration kit gives a NIC's IPv4 addresses one subnet."
    )
    f = only(found("10.2.0.255"), "NET_ADDRESS_REFUSED")
    assert (
        "00:50:56:aa:bb:cc on 'dc2-prod': 10.2.0.255 is no host address of any subnet" in f.message
    )
    # no IPv4 address at all: nothing to refuse
    assert "NET_ADDRESS_REFUSED" not in [x.code for x in found("fe80::1")]


def test_mtu_and_nic_override_subnets_follow_the_resolved_network():
    """SDD §9.3: same-named networks of other projects lend neither their MTU nor their subnets
    (review NIC-03: an address in another project's network of that name passed validation)."""
    vm = make_vm(
        source_id="vm-1",
        nics=[
            Nic(network="app-net", mac="fa:16:3e:00:00:01", fixed_ips=["192.168.10.5"], mtu=1500)
        ],
    )
    details = [
        net("own-dmz", "rhoso-dmz", mtu=1500),
        net("other-dmz", "rhoso-dmz", own=False, mtu=1400),
    ]
    subnets = [
        subnet_of("own-dmz", "rhoso-dmz", "192.168.10.0/24"),
        subnet_of("other-dmz", "rhoso-dmz", "10.2.0.0/24"),
    ]
    # the name->MTU view kept the other project's 1400
    dst = dst_inv(networks={"rhoso-dmz": 1400}, network_details=details, subnets=subnets)
    moved = make_plan(nic_overrides={"vm-1": {"fa:16:3e:00:00:01": {"network": "rhoso-dmz"}}})
    found = check(vm, plan=moved, dst=dst)
    assert "NET_MTU_SHRINK" not in [x.code for x in found]
    assert only(found, "MAP_NIC_OVERRIDE").severity == Severity.info
    elsewhere = make_plan(
        nic_overrides={
            "vm-1": {"fa:16:3e:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["10.2.0.9"]}}
        }
    )
    f = only(check(vm, plan=elsewhere, dst=dst), "MAP_NIC_OVERRIDE_INVALID")
    assert "fa:16:3e:00:00:01: 10.2.0.9 lies in no subnet of 'rhoso-dmz'" in f.message


def test_finding_map_nic_override_invalid_follows_neutron_on_slaac_subnets():
    """SDD §9.3, §6.6: an override's address on a SLAAC subnet - the kept MAC's EUI-64 address in
    "neutron" mode, none in "nova" mode."""
    vm = make_vm(
        source_id="vm-1",
        nics=[Nic(network="app-net", mac="fa:16:3e:00:00:01", fixed_ips=["10.0.0.5"])],
    )
    dst = resolved_dst(
        [net("app", "app-net")],
        [
            subnet_of("app", "app-net", "10.0.0.0/24"),
            subnet_of("app", "app-net", "fd00:20::/64", "slaac"),
        ],
    )

    def entry(address, mode):
        return make_plan(
            nic_overrides={"vm-1": {"fa:16:3e:00:00:01": {"fixed_ips": ["10.0.0.6", address]}}},
            port_creation_mode=mode,
        )

    eui = "fd00:20::f816:3eff:fe00:1"
    assert "MAP_NIC_OVERRIDE_INVALID" not in [
        x.code for x in check(vm, plan=entry(eui, "neutron"), dst=dst)
    ]
    f = only(check(vm, plan=entry("fd00:20::5", "neutron"), dst=dst), "MAP_NIC_OVERRIDE_INVALID")
    assert f"fa:16:3e:00:00:01: fd00:20::5 is not {eui}" in f.message
    f = only(check(vm, plan=entry(eui, "nova"), dst=dst), "MAP_NIC_OVERRIDE_INVALID")
    assert "SLAAC or DHCPv6-stateless" in f.message
