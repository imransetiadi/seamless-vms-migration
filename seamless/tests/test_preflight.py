import ast
import json
import re
import string
from pathlib import Path

import pytest

from seamless_migrate.domain.enums import Phase, ProviderKind, ProviderRole, Severity, Strategy
from seamless_migrate.domain.models import (
    DEFAULT_PRESTAGE,
    AppProbe,
    HandoverConfig,
    Mappings,
    Nic,
    VerificationConfig,
    VMRef,
)
from seamless_migrate.planning.platforms import destination_release
from seamless_migrate.planning.preflight import (
    CATALOG,
    KIT_TWIN_REMEDIATION,
    DestinationInventory,
    SourceInventory,
    fitting_flavor,
    resolve_mappings,
    resource_demand,
    run_preflight,
)
from seamless_migrate.planning.selector import eligibility
from seamless_migrate.providers.openstack import OpenStackProvider
from tests.cloud_support import AUTH_URL, InProcessCloud
from tests.factories import make_disk, make_plan, make_provider, make_vm

SRC = make_provider()
# the destination provider a message about the destination cloud names (SDD §1 naming, plan T6)
DST = make_provider(
    id="dst-rhoso", name="RHOSO 18.0", kind=ProviderKind.rhoso, role=ProviderRole.destination
)


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


def test_the_finding_catalog_is_the_one_the_dashboards_mock_reads():
    """SDD §9.3, §16: each code's severity, strategies and default remediation, as the shared
    finding_catalog.json holds them for the dashboard's offline mock - a remediation changed here
    fails this test until the fixture, and with it the mock, says the same."""
    shared = json.loads(
        (Path(__file__).parent / "fixtures" / "finding_catalog.json").read_text(encoding="utf-8")
    )["catalog"]
    assert {
        code: {
            "severity": entry.severity.value,
            "strategies": [strategy.value for strategy in entry.strategies],
            "remediation": entry.remediation,
        }
        for code, entry in CATALOG.items()
    } == shared


def test_the_qasuite_counts_the_mocks_codes_and_the_shared_finding_cases():
    """docs/QASuite.md FR-04 states how many of the catalog's codes the dashboard's offline mock
    implements and how many shared finding cases hold its messages to the API's - as the suite
    is, not as it was (CLAUDE.md §8.2, review RV28-05)."""
    root = Path(__file__).resolve().parents[2]
    row = next(
        line
        for line in (root / "docs" / "QASuite.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| FR-04 |")
    )
    implemented, total = map(int, re.search(r"\((\d+) of its (\d+) codes", row).groups())
    shared = int(re.search(r"`mockData\.test\.ts`; (\d+) cases:", row).group(1))
    mock = (root / "dashboard" / "src" / "api" / "mockData.ts").read_text(encoding="utf-8")
    named = {code for code in CATALOG if re.search(f"['\"`]{re.escape(code)}['\"`]", mock)}
    assert (implemented, total, shared) == (
        len(named),
        len(CATALOG),
        len(ELIGIBILITY_CASES["finding_cases"]),
    )


def test_catalog_is_complete():
    assert set(CATALOG) == {
        "SRC_VM_ERROR_STATE",
        "SRC_VM_TRANSITIONAL_STATE",
        "SRC_VM_DUPLICATE_NAME",
        "SRC_VM_NAME_UNUSABLE",
        "SRC_VM_VOLUME_DETACHED",
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
        "DST_CAPABILITY_MISSING",
        "DST_RELEASE_OUTSIDE_SUPPORT",
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


def test_finding_src_vm_name_unusable():
    """SDD §9.3 (review RV26-04): a VMware VM's name the migration kit keeps no character of."""
    f = only(check(make_vm(name="サーバー"), source=VMWARE), "SRC_VM_NAME_UNUSABLE")
    assert f.severity == Severity.blocker and f.strategies == [] and "vCenter" in f.remediation


def test_duplicate_names_blocked():
    """Names are what os-migrate filters on: duplicates across projects block every copy."""
    a = make_vm(source_id="a", name="app.server[1]", project="finance")
    b = make_vm(source_id="b", name="app.server[1]", project="hr")
    c = make_vm(source_id="c", name="app.server[12]", project="hr")
    vms = [a, b, c]
    for vm in (a, b):
        assert only(check(vm, all_vms=vms), "SRC_VM_DUPLICATE_NAME").severity == Severity.blocker
    assert all(f.code != "SRC_VM_DUPLICATE_NAME" for f in check(c, all_vms=vms))


def test_a_vm_with_a_same_named_twin_still_names_the_destination_provider():
    """SDD §1 naming: the duplicate-name finding named the other VMs with the variable every later
    message names the destination provider by, so for a VM with a same-named twin each finding
    about the destination cloud named the twins ("in 'hr/vm-2'") instead of the provider."""
    vm = make_vm(nics=[Nic(network="db-net", mtu=9000)])
    twin = make_vm(source_id="vm-2", project="hr")
    found = check(vm, plan=make_plan(prestage_resources=["subnets"]), all_vms=[vm, twin])
    assert "(hr/vm-2)" in only(found, "SRC_VM_DUPLICATE_NAME").message
    assert only(found, "MAP_NETWORK_MISSING").message == (
        "No mapping or same-named network in RHOSO 18.0 for: db-net."
    )


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
    # both messages name the destination provider (SDD §1 naming, §9.3, review RV20-05)
    assert "which RHOSO 18.0 does not list" in f.message
    # unmapped, no same-named project, several destination projects: nothing to charge
    f = only(check(vm, dst=dst), "DST_PROJECT_MISSING")
    assert "and RHOSO 18.0 has 2 projects" in f.message
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


def test_a_missing_network_comes_before_a_port_its_credential_may_not_place():
    """SDD §9.3, §16 (review RV25-03): the findings come in one order - a network the destination
    lacks before the ports of a resolved network - the order the dashboard's mock gives too
    (mockData.test.ts, the same VM and inventory)."""
    nics = [
        Nic(network="gone-net", mac="fa:16:3e:00:00:01", fixed_ips=["10.0.0.5"], mtu=1500),
        Nic(network="other-net", mac="fa:16:3e:00:00:02", fixed_ips=["10.0.0.6"], mtu=1500),
    ]
    vm = make_vm(nics=nics)
    other = {"id": "n1", "name": "other-net", "mtu": 1500, "project_id": "p-other"}
    details = [{**other, "own": False, "shared": False, "external": False}]
    dst = dst_inv(networks={"other-net": 1500}, network_details=details)
    found = check(vm, plan=make_plan(prestage_resources=[]), src=SourceInventory(), dst=dst)
    assert [
        (f.code, f.message) for f in found if f.code.startswith(("MAP_NETWORK", "NET_PORT"))
    ] == [
        ("MAP_NETWORK_MISSING", "No mapping or same-named network in RHOSO 18.0 for: gone-net."),
        (
            "NET_PORT_NOT_PERMITTED",
            "The credential of RHOSO 18.0 may not put these NICs on their network: "
            "fa:16:3e:00:00:02 on 'other-net' (project p-other): it is neither shared with the "
            "credential's project nor external.",
        ),
    ]


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


def test_finding_map_keypair_prestaged():
    """SDD §9.3, §7.2: pre-staging imports a keypair only where the destination credential lacks
    it - one it owns already is used as it is, and nothing is said - and the info names the key and
    the destination, with the catalog's "nothing to do"."""
    vm = make_vm(key_name="ops-key")
    src = src_inv(keypairs=["ops-key"])
    prestage = make_plan(prestage_resources=[*DEFAULT_PRESTAGE, "keypairs"])
    f = only(check(vm, plan=prestage, src=src), "MAP_KEYPAIR_PRESTAGED")
    assert "'ops-key'" in f.message and DST.label in f.message
    assert f.remediation == CATALOG["MAP_KEYPAIR_PRESTAGED"].remediation
    assert f.remediation.startswith("Nothing to do")
    owned = check(vm, plan=prestage, src=src, dst=dst_inv(keypairs=["ops-key"]))
    assert all("KEYPAIR" not in x.code for x in owned)


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
    assert "fa:16:3e:00:00:01: RHOSO 18.0 has no network 'rhoso-missing'" in f.message
    assert "fa:16:3e:00:00:02: 172.16.0.9 lies in no subnet of 'app-net'" in f.message
    assert "fa:16:3e:00:00:03: 10.0.0.5 also goes to db-01" in f.message
    # addresses only, on a network pre-staging would create: its subnets cannot be checked yet
    lost = make_vm(
        source_id="vm-3",
        nics=[Nic(network="lost-net", mac="fa:16:3e:00:00:05", fixed_ips=["10.9.0.5"])],
    )
    later = make_plan(nic_overrides={"vm-3": {"fa:16:3e:00:00:05": {"fixed_ips": ["10.9.0.6"]}}})
    f = only(check(lost, plan=later, dst=override_dst()), "MAP_NIC_OVERRIDE_INVALID")
    assert "fa:16:3e:00:00:05: RHOSO 18.0 has no network 'lost-net' yet" in f.message
    assert "name an existing network of RHOSO 18.0" in f.message
    vmware = make_provider(id="vc", kind=ProviderKind.vmware, cloud=None, credentials_secret="s")
    vmw = only(check(vm, plan=plan, dst=override_dst(), source=vmware), "MAP_NIC_OVERRIDE_INVALID")
    assert "migration kit keeps the guest's network settings" in vmw.message


def test_finding_map_nic_override_invalid_refuses_what_neutron_gives_no_vm():
    """SDD §9.3, as Neutron's check_subnet_ip for a port created with no owner: an IPv4 subnet's
    network address (at any prefix length) and its broadcast address below a /31 are no host
    addresses - a typo like 192.168.10.0 would otherwise fail after the source stops - while an
    IPv6 subnet's first address is taken (review NC-08)."""
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
    assert "fd00:10::" not in f.message
    assert "fa:16:3e:00:00:04: 10.255.0.0 is the network address of 10.255.0.0/31" in f.message
    # one problem per address; the NIC with host addresses has none
    assert f.message.count("not a host address") == 3
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


#: what each phase of a VM's migration still asks of the destination's quota (SDD §9.3, review
#: PL-02), for a VM of 2 vCPUs, 4 GiB and one 20 GiB volume
QUOTA_SHARES = {
    "all": {"cores": 2, "ram_mb": 4096, "instances": 1, "volumes": 1, "gigabytes": 20},
    "server": {"cores": 2, "ram_mb": 4096, "instances": 1, "volumes": 0, "gigabytes": 0},
    "nothing": {"cores": 0, "ram_mb": 0, "instances": 0, "volumes": 0, "gigabytes": 0},
}
PHASE_SHARES = {
    None: "all",
    Phase.pending: "all",
    Phase.validating: "all",
    Phase.blocked: "all",
    Phase.ready: "all",
    Phase.failed: "all",
    Phase.rolled_back: "all",
    Phase.precopy: "server",
    Phase.syncing: "server",
    Phase.awaiting_cutover: "server",
    Phase.cutover: "server",
    Phase.verifying: "nothing",
    Phase.completed: "nothing",
    Phase.finalized: "nothing",
    Phase.rolling_back: "nothing",
    Phase.cancelled: "nothing",
}


def test_every_phase_has_a_quota_share():
    assert set(PHASE_SHARES) == {None, *Phase}


@pytest.mark.parametrize("phase", list(PHASE_SHARES), ids=str)
def test_the_quota_counts_what_a_cutover_will_still_create(phase):
    """SDD §9.3 (review PL-02): the free quota is the limit less what is in use, so each VM counts
    what a cutover will still create - all of it while its migration runs, or runs again, from
    ready; its server alone while its destination volumes exist or are being created; nothing once
    what it holds is in use, or being released. A free quota of exactly the assessed VM's demand
    and the other VM's share fits; one unit less of any key does not."""
    a = make_vm(source_id="a", name="a")
    b = make_vm(source_id="b", name="b")
    share = QUOTA_SHARES[PHASE_SHARES[phase]]
    free = {key: QUOTA_SHARES["all"][key] + share[key] for key in share}
    phases = {} if phase is None else {"b": phase}

    def assessed(quota):
        return run_preflight(
            a,
            make_plan(),
            src_inv(),
            dst_inv(quotas={"finance": quota}),
            [a, b],
            source=SRC,
            destination=DST,
            phases=phases,
        )

    assert "DST_QUOTA_INSUFFICIENT" not in [f.code for f in assessed(free)]
    for key, value in free.items():
        short = only(assessed({**free, key: value - 1}), "DST_QUOTA_INSUFFICIENT")
        assert f"{key} {value} > {value - 1} free" in short.message


#: the destination flavors of the PL-01 tests: a larger one, and one named like the source's
#: m1.small with another size
SIZED_FLAVORS = [
    {"name": "m1.small", "vcpus": 4, "ram_mb": 8192, "disk_gb": 20, "extra_specs": {}},
    {"name": "m1.large", "vcpus": 8, "ram_mb": 16384, "disk_gb": 80, "extra_specs": {}},
]


PRICED_CASES = [
    ("the mapped flavor", {}, {"mappings": Mappings(flavors={"m1.small": "m1.large"})}, (8, 16384)),
    (
        "the automatically matched flavor",
        {"flavor": "c5.medium", "vcpus": 5, "ram_mb": 6144},
        {},
        (8, 16384),
    ),
    ("the same-named flavor's own size", {}, {}, (4, 8192)),
    (
        "a flavor pre-staging creates: the VM's size",
        {"flavor": "c3.medium", "vcpus": 3, "ram_mb": 6144},
        {"prestage_resources": ["flavors"]},
        (3, 6144),
    ),
    ("a VMware VM: the VM's size", {"flavor": None, "vcpus": 3, "ram_mb": 6144}, {}, (3, 6144)),
    (
        "no fitting flavor: the VM's size",
        {"flavor": "x9.huge", "vcpus": 32, "ram_mb": 6144},
        {},
        (32, 6144),
    ),
]


@pytest.mark.parametrize(
    "case, vm_kw, plan_kw, priced", PRICED_CASES, ids=[c[0] for c in PRICED_CASES]
)
def test_the_quota_prices_a_vm_with_its_destination_flavor(case, vm_kw, plan_kw, priced):
    """SDD §9.3 (review PL-01): Nova charges the flavor the server is created with - the mapped
    one, else the same-named or automatically matched one -, so a VM's cores and ram are that
    flavor's; its own for a flavor pre-staging creates with the source's size, a VMware VM (the
    migration kit chooses) and when no destination flavor resolves."""
    vm = make_vm(**vm_kw)
    plan = make_plan(**plan_kw)
    cores, ram_mb = priced

    def assessed(quota):
        return check(vm, plan=plan, dst=dst_inv(flavors=SIZED_FLAVORS, quotas={"finance": quota}))

    tight = {"cores": cores - 1, "ram_mb": ram_mb, "instances": 5, "volumes": 5, "gigabytes": 1000}
    short = only(assessed(tight), "DST_QUOTA_INSUFFICIENT")
    assert f"cores {cores} > {cores - 1} free" in short.message, case
    short = only(
        assessed({**tight, "cores": cores, "ram_mb": ram_mb - 1}), "DST_QUOTA_INSUFFICIENT"
    )
    assert f"ram_mb {ram_mb} > {ram_mb - 1} free" in short.message, case
    fits = {**tight, "cores": cores}
    assert "DST_QUOTA_INSUFFICIENT" not in [f.code for f in assessed(fits)], case
    demand = resource_demand(vm, plan, dst_inv(flavors=SIZED_FLAVORS))
    assert (demand["cores"], demand["ram_mb"]) == priced, case


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


@pytest.mark.parametrize("key", ["resources1:VGPU", "resources_GPU-a:VGPU"])
def test_finding_vm_vgpu_in_a_request_group(key):
    """SDD §9.3 (review PL-08): a vGPU requested in a request group - resources<suffix>:VGPU, Nova's
    granular form - blocks as the bare resources:VGPU does."""
    assert only(check(make_vm(flavor_extra_specs={key: "1"})), "VM_VGPU").severity == (
        Severity.blocker
    )


@pytest.mark.parametrize(
    "key",
    [
        "resources1:VCPU",
        "resources:CUSTOM_VGPU_X",
        "resources1:VGPU_DISPLAY_HEAD",
        "trait1:CUSTOM_VGPU",
    ],
)
def test_finding_vm_vgpu_names_the_vgpu_class_only(key):
    """SDD §9.3 (review PL-08): another resource class, or a trait, in a request group is no
    vGPU."""
    assert "VM_VGPU" not in [f.code for f in check(make_vm(flavor_extra_specs={key: "1"}))]


@pytest.mark.parametrize(
    "spec",
    [
        {"resources1:VGPU": "1"},
        {"trait1:CUSTOM_NVIDIA_A40": "required"},
        {"resources_GPU:CUSTOM_FPGA": "1"},
        {"trait:HW_CPU_X86_AVX512F": "required"},
    ],
    ids=lambda spec: next(iter(spec)),
)
def test_flavor_auto_match_skips_request_group_flavors(spec):
    """SDD §9.3 (review PL-08): a flavor with a resource or trait request in a request group -
    resources<suffix>:*, trait<suffix>:* - pins the server to special hardware as the bare forms
    do: it is never matched automatically, though it is the smallest flavor that fits."""
    vm = make_vm(flavor="c2.small")  # no destination flavor of that name
    special = {"name": "g1.small", "vcpus": 2, "ram_mb": 4096, "disk_gb": 20, "extra_specs": spec}
    plain = {"name": "m1.large", "vcpus": 8, "ram_mb": 16384, "disk_gb": 80, "extra_specs": {}}
    chosen = fitting_flavor(vm, dst_inv(flavors=[special, plain]))
    assert chosen is not None and chosen["name"] == "m1.large"


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


def test_each_finding_code_has_a_test_named_for_it():
    """QASuite FR-04: each finding code of SDD §9.3 has a ``test_finding_<code_lower>`` test or
    ``test_finding_<code_lower>_<case>`` tests, and each such test is named for a code. A test
    counts for the longest code its name begins with: MAP_NIC_OVERRIDE_INVALID's tests do not
    stand in for MAP_NIC_OVERRIDE."""
    import re
    from pathlib import Path

    from seamless_migrate.config import find_repo_root

    sdd = (find_repo_root() / "docs" / "SDD.md").read_text(encoding="utf-8")
    section = sdd[sdd.index("### 9.3") : sdd.index("### 9.4")]
    codes = sorted(set(re.findall(r"`([A-Z][A-Z0-9_]{4,})`", section)), key=len, reverse=True)
    tested, unnamed = set(), []
    for path in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        for name in re.findall(r"^(?:async )?def (test_finding_\w+)\(", text, re.M):
            code = next(
                (c for c in codes if f"{name}_".startswith(f"test_finding_{c.lower()}_")), None
            )
            if code is None:
                unnamed.append(f"{path.name}::{name}")
            tested.add(code)
    assert sorted(set(codes) - tested) == [], "codes without a test_finding_ test"
    assert unnamed == [], "test_finding_ tests named for no code of SDD §9.3"


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


COMMUNITY = make_provider(
    id="dst-flamingo",
    name="OpenStack 2026.1 Flamingo (DC2)",
    kind=ProviderKind.openstack,
    role=ProviderRole.destination,
    distribution="openstack_community",
)


def community_caps(**kw):
    """A destination check report (SDD §10) of a Tier 1 community cloud, changed by ``kw``."""
    caps = {
        "admin": True,
        "roles": ["admin", "member", "reader"],
        "compute_microversion": "2.100",
        "block_storage_microversion": "3.71",
        "release_range": ["2025.1", "2025.2"],
        "release": None,
        "support_tier": {"tier": "tier1", "reason": None, "declared_outside_range": False},
        "policy_profile": "secure_rbac",
        "tls": True,
        "plain_http": [],
    }
    caps.update(kw)
    return caps


def test_finding_dst_capability_missing():
    """SDD §21.4: capabilities block, names never do. A maximum below the floor every strategy
    needs - compute 2.60, block storage 3.0 - blocks whatever the series; one the check could not
    read is not judged. A gap one strategy alone has is that strategy's eligibility reason
    (test_storage_handover_needs_the_sources_compute_2_85), never a finding."""

    def found(destination=COMMUNITY, **caps):
        findings = check(destination=destination, dst_caps=community_caps(**caps))
        return [f for f in findings if f.code == "DST_CAPABILITY_MISSING"]

    assert found() == []
    every = found(compute_microversion="2.53", block_storage_microversion="2.0")
    assert len(every) == 1 and every[0].severity == Severity.blocker and every[0].remediation
    assert every[0].strategies == []  # every strategy
    # no strategy avoids a floor every strategy needs: none is offered (review 18, nit 7)
    assert "strategy" not in every[0].remediation, every[0].remediation
    assert every[0].message == (
        "OpenStack 2026.1 Flamingo (DC2) lacks what every strategy needs: compute API 2.53, "
        "below 2.60 (multi-attach); block storage API 2.0, below 3.0."
    )
    # the handover's 2.85 is the source's, the selector's eligibility reason (SDD §21.4)
    assert found(compute_microversion="2.79") == []
    # a Tier 3 or Outside cloud with every capability runs (the tier only warns)
    outside = {"tier": "outside", "reason": "newer_than_validated", "declared_outside_range": False}
    assert found(compute_microversion="2.105", support_tier=outside) == []
    # a maximum the check could not read is not judged; nor is a check report without them
    assert found(compute_microversion="", block_storage_microversion="") == []
    assert [
        f for f in check(destination=COMMUNITY, dst_caps={}) if f.code == "DST_CAPABILITY_MISSING"
    ] == []
    assert [f for f in check(destination=COMMUNITY) if f.code == "DST_CAPABILITY_MISSING"] == []
    # RHOSO 18.0 is judged by the same floor
    assert (
        found(destination=DST, compute_microversion="2.95", block_storage_microversion="3.70") == []
    )


TIER3 = {"tier": "tier3", "reason": "end_of_life", "declared_outside_range": False}


@pytest.mark.parametrize(
    "release_range, support_tier, release, message",
    [
        (
            ["2024.1", "2024.2"],
            TIER3,
            None,
            "OpenStack 2026.1 Flamingo (DC2) runs 2024.1 – 2024.2, read as Tier 3 (End of Life "
            "upstream): no Seamless release validated it there. The capability checks still "
            "apply.",
        ),
        (
            [],
            {"tier": "outside", "reason": "below_floor", "declared_outside_range": False},
            None,
            "OpenStack 2026.1 Flamingo (DC2) runs a series older than 2023.1, the support window's "
            "floor: no Seamless release validated it. The capability checks still apply.",
        ),
        (
            [],
            {"tier": "outside", "reason": "newer_than_validated", "declared_outside_range": False},
            None,
            # no Seamless release validated 2026.2 yet: it is the window's newest (review 18, nit 8)
            "OpenStack 2026.1 Flamingo (DC2) runs a series newer than 2026.2, the newest series "
            "of the support window: no Seamless release validated it. The capability checks still "
            "apply.",
        ),
        (
            [],
            {"tier": "outside", "reason": "not_detected", "declared_outside_range": False},
            None,
            "The series of OpenStack 2026.1 Flamingo (DC2) could not be detected (its compute API "
            "maximum was not read): no Seamless release can be said to have validated it. The "
            "capability checks still apply.",
        ),
        (
            ["2026.2"],
            {"tier": "tier1", "reason": None, "declared_outside_range": True},
            "2025.1",
            "OpenStack 2026.1 Flamingo (DC2) declares series 2025.1, but its APIs read as 2026.2: "
            "correct the declared series; the tier is the one its APIs read (Tier 1).",
        ),
        (
            ["2024.1", "2024.2"],
            {**TIER3, "declared_outside_range": True},
            "2026.2",
            "OpenStack 2026.1 Flamingo (DC2) runs 2024.1 – 2024.2, read as Tier 3 (End of Life "
            "upstream): no Seamless release validated it there. The capability checks still "
            "apply. It declares series 2026.2, which its APIs do not run: correct the declared "
            "series.",
        ),
    ],
    ids=[
        "tier 3",
        "below the floor",
        "newer",
        "not detected",
        "a declared series outside",
        "tier 3 and a declared series outside",
    ],
)
def test_finding_dst_release_outside_support(release_range, support_tier, release, message):
    """SDD §21.4, §10.1: a Tier 3 or Outside destination, or one whose declared series its APIs
    contradict (QASuite LAB-D02), is warned of - never blocked."""
    destination = COMMUNITY.model_copy(update={"release": release})
    caps = community_caps(release_range=release_range, support_tier=support_tier)
    f = only(check(destination=destination, dst_caps=caps), "DST_RELEASE_OUTSIDE_SUPPORT")
    assert f.severity == Severity.warning and f.strategies == [] and f.remediation
    assert f.message == message


@pytest.mark.parametrize(
    "release, compute", [("2026.2", ""), ("2022.2", "2.93"), ("2027.1", "2.105")]
)
def test_following_the_remediation_never_asks_to_correct_a_series_nothing_contradicts(
    release, compute
):
    """Review RV18-06: the warning's remediation asks for the series when the APIs cannot tell
    it; declared so - with nothing detected, or a correct series below or above the window -, the
    next validation told the operator its APIs do not run it: "correct the declared series"."""
    declared = COMMUNITY.model_copy(update={"release": release})
    caps = destination_release("openstack", release, None, compute, "")
    told = [
        f.message
        for f in check(destination=declared, dst_caps=caps)
        if f.code == "DST_RELEASE_OUTSIDE_SUPPORT" and "correct the declared series" in f.message
    ]
    assert told == []


def test_declaring_the_series_settles_the_unknown_profile_the_refusal_asks_about():
    """Review RV18-06: under the unknown profile NET_PORT_NOT_PERMITTED asks to check the provider
    or declare its policy profile or series (review 19); with nothing detected a declared series
    was ignored, so the service role stayed refused. Declared, 2026.2 reads secure-RBAC (SDD
    §10.1, §21.5)."""
    service = {"roles": ["service", "member"]}
    before = _another_projects_port(
        {**destination_release("openstack", None, None, "", ""), **service}
    )
    # a check is allowed while a plan runs, a PATCH that declares is not (SDD §12, §21.5)
    assert (
        "check RHOSO 18.0 so its series is detected, or declare its policy profile or series"
        in before.message
    )
    after = _another_projects_port(
        {**destination_release("openstack", "2026.2", None, "", ""), **service}
    )
    assert after is None, after.message


def test_a_tier_3_warning_names_the_series_the_check_settled_on():
    """Review RV18-11: a declared series inside the range is the series the check settles on
    (release) and decides the tier, yet the Tier 3 sentence named the range - "runs 2024.1 –
    2024.2" for a declared 2024.2, though 2024.1 is Tier 2, not End of Life (SDD §10.1)."""
    declared = COMMUNITY.model_copy(update={"release": "2024.2"})
    caps = destination_release("openstack", "2024.2", None, "2.96", "")
    found = only(check(destination=declared, dst_caps=caps), "DST_RELEASE_OUTSIDE_SUPPORT")
    assert found.message.startswith(
        "OpenStack 2026.1 Flamingo (DC2) runs 2024.2, read as Tier 3 (End of Life upstream)"
    ), found.message


def test_no_release_warning_for_a_supported_or_validated_destination():
    for tier in ("tier1", "tier2"):
        caps = community_caps(
            support_tier={"tier": tier, "reason": None, "declared_outside_range": False}
        )
        assert "DST_RELEASE_OUTSIDE_SUPPORT" not in [
            f.code for f in check(destination=COMMUNITY, dst_caps=caps)
        ]
    validated = {"tier": "validated", "reason": None, "declared_outside_range": False}
    assert "DST_RELEASE_OUTSIDE_SUPPORT" not in [
        f.code for f in check(destination=DST, dst_caps=community_caps(support_tier=validated))
    ]
    # RHOSO 18.0 never, whatever series its provider declares (a report the check never makes)
    declared = DST.model_copy(update={"release": "2026.2"})
    stale = community_caps(support_tier={**validated, "declared_outside_range": True})
    assert "DST_RELEASE_OUTSIDE_SUPPORT" not in [
        f.code for f in check(destination=declared, dst_caps=stale)
    ]
    # a check report from before the series was reported (no support_tier) is not judged
    assert "DST_RELEASE_OUTSIDE_SUPPORT" not in [
        f.code for f in check(destination=COMMUNITY, dst_caps={"admin": True})
    ]


def _another_projects_port(caps, destination=DST):
    """NET_PORT_NOT_PERMITTED, or None, for a NIC that keeps its address on another project's
    shared network, judged with ``destination``'s check report ``caps`` (SDD §6.6, §9.3)."""
    vm = make_vm(
        source_id="vm-1",
        nics=[Nic(network="app-net", mac="FA:16:3E:00:00:01", fixed_ips=["10.0.0.5"])],
    )
    plan = make_plan(mappings=Mappings(networks={"app-net": "rhoso-net"}))
    shared = net("p", "rhoso-net", own=False, project_id="project-admin")
    dst = resolved_dst([shared], [subnet_of("p", "rhoso-net", "10.0.0.0/24")])
    found = [
        f
        for f in check(vm, plan=plan, dst=dst, destination=destination, dst_caps=caps)
        if f.code == "NET_PORT_NOT_PERMITTED"
    ]
    return found[0] if found else None


def test_finding_net_port_not_permitted_counts_service_under_secure_rbac():
    """SDD §21.5, §6.6: under the secure-RBAC defaults (2023.2 on) Neutron lets the admin or
    service role, or the network's project, choose a port's address on another project's network
    - and advsvc no longer."""
    secure = {"policy_profile": "secure_rbac"}
    for roles in (["service", "member"], ["admin"]):
        assert _another_projects_port({**secure, "roles": roles}) is None, roles
    for roles in (["advsvc", "member"], ["member", "reader"]):
        refused = _another_projects_port({**secure, "roles": roles})
        assert refused is not None and refused.severity == Severity.blocker, roles
        assert refused.message == (
            "The credential of RHOSO 18.0 may not put these NICs on their network: "
            "fa:16:3e:00:00:01 on 'rhoso-net' (project project-admin): Neutron's secure-RBAC "
            "defaults let only the admin or service role, or that project, choose a port's address "
            "or MAC there, and the credential's token has neither."
        )


def test_finding_net_port_not_permitted_counts_advsvc_only_under_the_legacy_profile():
    """SDD §21.5, §6.6: advsvc counts under the legacy rules alone - also for an RHOSO 18.0
    report that names no profile, legacy by its kind -, and an unknown profile counts only admin,
    the role both count."""
    for caps in ({"policy_profile": "legacy"}, {}):
        assert _another_projects_port({**caps, "roles": ["advsvc"]}) is None, caps
        refused = _another_projects_port({**caps, "roles": ["service", "member"]})
        assert "Neutron lets only the admin or advsvc role" in refused.message, caps
    unknown = {"policy_profile": "unknown"}
    assert _another_projects_port({**unknown, "roles": ["admin"]}) is None
    for roles in (["advsvc"], ["service"]):
        refused = _another_projects_port({**unknown, "roles": roles})
        assert refused is not None, roles
        assert "the policy profile of RHOSO 18.0 is unknown, so only the admin role" in (
            refused.message
        ), roles
    # a value the check never reports counts as unknown: admin alone
    assert _another_projects_port({"policy_profile": "strict", "roles": ["advsvc"]}) is not None


def test_net_port_not_permitted_names_the_provider_whose_policy_profile_is_unknown():
    """SDD §1 naming (review RV23-07): a finding's message names the destination provider by its
    label - the provider whose policy profile is unknown and the one to check - never "the
    destination"; by its id when the stored name is blank."""
    dc3 = DST.model_copy(update={"name": "DC3 Jakarta"})
    refused = _another_projects_port({"policy_profile": "unknown", "roles": ["advsvc"]}, dc3)
    assert refused is not None and refused.message == (
        "The credential of DC3 Jakarta may not put these NICs on their network: "
        "fa:16:3e:00:00:01 on 'rhoso-net' (project project-admin): the policy profile of DC3 "
        "Jakarta is unknown, so only the admin role, or that project, may choose a port's address "
        "or MAC there, and the credential's token lacks it (check DC3 Jakarta so its series is "
        "detected, or declare its policy profile or series)."
    )
    blank = DST.model_copy(update={"name": " "})
    refused = _another_projects_port({"policy_profile": "unknown", "roles": ["advsvc"]}, blank)
    assert refused is not None
    assert "the policy profile of dst-rhoso is unknown" in refused.message
    assert "(check dst-rhoso so its series is detected" in refused.message


async def _real_check(tmp_path, **cloud):
    """The check report of a community destination on an in-process cloud: Seamless's own
    connect() over a real keystoneauth session (tests/cloud_support.py)."""
    real = InProcessCloud(tmp_path, **cloud)
    provider = COMMUNITY.model_copy(update={"endpoint": AUTH_URL, "cloud": "dst"})
    report = await OpenStackProvider(provider, real.settings, connect_fn=real.connect).check()
    return {**report, "conversion_host": True}


async def test_a_real_tier_1_destination_is_not_warned_and_its_service_role_takes_the_port(
    tmp_path,
):
    """Review RV18-01: a real 2026.2 cloud read "not detected" - DST_RELEASE_OUTSIDE_SUPPORT on
    every VM, and its profile unknown, so NET_PORT_NOT_PERMITTED refused the service role SDD
    §21.5 and Security.md C4-06 prescribe. Read through a real keystoneauth session, it is Tier 1
    and secure-RBAC (SDD §21.3, §21.4, §21.5)."""
    caps = await _real_check(tmp_path, compute="2.104", block_storage=None, roles=("service",))

    findings = check(destination=COMMUNITY, dst_caps=caps)
    assert [f.message for f in findings if f.code == "DST_RELEASE_OUTSIDE_SUPPORT"] == []
    refused = _another_projects_port({**caps, "roles": ["service", "member"]})
    assert refused is None, refused.message


async def test_a_real_destination_below_the_compute_floor_is_blocked(tmp_path):
    """Review RV18-01: a real cloud's compute maximum never parsed, so one below 2.60 was never
    blocked (SDD §21.4)."""
    caps = await _real_check(tmp_path, compute="2.53", block_storage="3.70")

    found = only(check(destination=COMMUNITY, dst_caps=caps), "DST_CAPABILITY_MISSING")
    assert "compute API 2.53, below 2.60" in found.message, found.message


@pytest.mark.parametrize(
    "declared, roles, refused",
    [
        ({"policy_profile": "secure_rbac"}, ["advsvc"], True),
        ({"policy_profile": "secure_rbac"}, ["service"], False),
        ({"release": "2026.2"}, ["advsvc"], True),  # its series' profile: secure-RBAC
        ({"release": "2023.1"}, ["advsvc"], False),  # legacy
        ({}, ["advsvc"], True),  # nothing declared: unknown, admin alone
        ({}, ["admin"], False),
    ],
)
def test_a_report_that_names_no_profile_reads_as_the_destinations_declarations(
    declared, roles, refused
):
    """Review RV18-04: a check report that names no profile - reset by new credentials or a
    provider change, or from before 0.2.0 - read legacy, so advsvc took a port on another
    project's network of a declared secure-RBAC destination. It reads as the provider's
    declarations alone (SDD §21.5)."""
    destination = COMMUNITY.model_copy(update=declared)
    found = _another_projects_port({"roles": roles}, destination)
    assert (found is not None) is refused, found and found.message
    # RHOSO 18.0: legacy by its kind, as before (D14)
    assert _another_projects_port({"roles": ["advsvc"]}) is None


PORT_REMEDIATION = (
    "Send the NIC to a network of the credential's project: map its network to one, or set a NIC "
    "override on the VM's migration page. Another project's shared network takes it once {role}. "
    "An external network that is not shared needs the admin role: Nova attaches it for no other."
)


@pytest.mark.parametrize(
    "profile, role",
    [
        (
            "secure_rbac",
            "the destination credential has the service role for the migration window "
            "(Neutron's secure-RBAC defaults); admin is not needed for this",
        ),
        ("legacy", "the destination credential has the advsvc role (Neutron's legacy rules)"),
        (
            "unknown",
            "a check of the provider detects its series, or the provider declares its policy "
            "profile or series, so the check counts that profile's role: service under "
            "secure-RBAC, advsvc under the legacy rules",
        ),
    ],
    ids=["secure_rbac", "legacy", "unknown"],
)
def test_the_port_refusals_remediation_names_the_least_role_its_profile_counts(profile, role):
    """Review RV18-10: the remediation sent every operator to the admin role, the over-privilege
    Security.md C4-06 rules out ("never admin for this alone"): under secure-RBAC the service role
    clears it. It names the least role the destination's policy profile counts (SDD §21.5)."""
    refused = _another_projects_port({"policy_profile": profile, "roles": ["member"]})
    assert refused.remediation == PORT_REMEDIATION.format(role=role)


HIBISCUS = make_provider(
    id="hibiscus-dc3",
    name="OpenStack 2026.2 Hibiscus (DC3)",
    kind=ProviderKind.openstack,
    role=ProviderRole.destination,
    distribution="kolla",
)

#: the findings whose message means the destination cloud, and whose message names its provider
DESTINATION_FINDINGS = {
    "MAP_NETWORK_PRESTAGED",
    "MAP_FLAVOR_PRESTAGED",
    "MAP_KEYPAIR_PRESTAGED",
    "MAP_NETWORK_MISSING",
    "MAP_FLAVOR_MISSING",
    "MAP_VOLUME_TYPE_MISSING",
    "DST_KEYPAIR_MISSING",
    "NET_PORT_NOT_PERMITTED",
    # review RV20-05
    "DST_PROJECT_MISSING",
    "DST_QUOTA_INSUFFICIENT",
    "NET_MTU_SHRINK",
    "DST_CAPABILITY_MISSING",
    "DST_RELEASE_OUTSIDE_SUPPORT",
}


def _destination_findings(destination):
    """Findings about ``destination``'s cloud, for VMs and plans that meet them (SDD §9.3)."""
    found = []
    # pre-staging creates a network and a flavor and imports a keypair
    prestage = make_plan(prestage_resources=[*DEFAULT_PRESTAGE, "flavors", "keypairs"])
    vm = make_vm(nics=[Nic(network="db-net", mtu=9000)], flavor="m1.custom", key_name="ops-key")
    found += check(vm, plan=prestage, src=src_inv(keypairs=["ops-key"]), destination=destination)
    # nothing pre-staged: a network, flavor, keypair and volume type the destination lacks
    bare = make_plan(
        prestage_resources=[],
        mappings=Mappings(networks={"app-net": "nope-net"}, volume_types={"ceph-ssd": "nope"}),
    )
    vm = make_vm(
        nics=[Nic(network="app-net"), Nic(network="db-net")],
        flavor="m1.huge",
        vcpus=64,
        ram_mb=262144,
        key_name="ops-key",
        disks=[make_disk(volume_type="ceph-ssd"), make_disk(volume_type="ceph-hdd")],
    )
    found += check(vm, plan=bare, destination=destination)
    # a flavor mapped to one the destination lacks
    mapped = make_plan(mappings=Mappings(flavors={"m1.custom": "nope"}))
    found += check(make_vm(flavor="m1.custom"), plan=mapped, destination=destination)
    found.append(_another_projects_port({"roles": ["member"]}, destination))
    # a project the destination does not list, one it lacks quota in, a smaller MTU (RV20-05)
    found += check(
        plan=make_plan(mappings=Mappings(projects={"finance": "finance-prod"})),
        destination=destination,
    )
    big = make_vm(vcpus=4096, ram_mb=2**24)
    found += check(big, dst=dst_inv(networks={"app-net": 1400}), destination=destination)
    # capabilities below every strategy's floor, and a series no Seamless release validated
    found += check(destination=destination, dst_caps=community_caps(compute_microversion="2.53"))
    found += check(
        destination=destination,
        dst_caps=community_caps(release_range=["2024.1", "2024.2"], support_tier=TIER3),
    )
    return [f for f in found if f is not None]


def test_no_user_facing_text_assumes_rhoso_for_a_community_destination():
    """SDD §1 naming, §9.3, plan T6 (PRD FR-34): a finding's message names the destination
    provider where it means the destination cloud - "do not exist in OpenStack 2026.2 Hibiscus
    (DC3)" -, and its remediation, fixed by the catalog, says "the destination": no finding says
    RHOSO for a community destination. This test covers the pre-flight's findings; the other texts
    are guarded by test_every_text_names_the_destination_provider_by_its_label (the executors,
    the orchestrator) and the dashboard's destination-wording.test.ts (review 20 docs nit)."""
    assert [code for code, entry in CATALOG.items() if "RHOSO" in entry.remediation] == []
    found = _destination_findings(HIBISCUS)
    assert DESTINATION_FINDINGS <= {f.code for f in found}, sorted(
        DESTINATION_FINDINGS - {f.code for f in found}
    )
    said = [
        f"{f.code}: {f.message} {f.remediation or ''}"
        for f in found
        if "RHOSO" in (f.message + (f.remediation or ""))
    ]
    assert said == []
    named = {f.code for f in found if HIBISCUS.name in f.message}
    assert DESTINATION_FINDINGS <= named, sorted(DESTINATION_FINDINGS - named)


#: the control plane's texts that are about the Red Hat product itself (SDD §1 naming): the demo's
#: RHOSO 18.0 provider and the plan that moves to it
PRODUCT_TEXTS = {
    ("demo.py", "Finance apps (RHOSP 17.1 → RHOSO)"),
    ("demo.py", "RHOSO 18.0"),
    # what leaves for Jev names an RHOSO destination by the product itself (SDD §13.4, RV20-01)
    ("planning/platforms.py", "RHOSO 18.0"),
}


def test_no_control_plane_text_says_rhoso_but_the_products_own():
    """SDD §1 naming, plan T6: beside the findings, an executor's refusal or failure and an event
    name the destination provider, the advisor's question names its kind and series (RV20-01), and
    fixed texts say "the destination": of the control plane's string literals - docstrings and
    comments aside - only the Red Hat product's own say RHOSO."""
    root = Path(__file__).parents[1] / "src" / "seamless_migrate"
    said = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and ast.get_docstring(node, clean=False) is not None
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "RHOSO" in node.value and id(node) not in docstrings:
                    said.add((str(path.relative_to(root)), node.value))
    assert said <= PRODUCT_TEXTS, sorted(said - PRODUCT_TEXTS)


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
        "The credential of RHOSO 18.0 may not put these NICs on their network: "
        "fa:16:3e:00:00:01 on 'rhoso-net' (project project-admin): Neutron lets only the admin or "
        "advsvc role, or that project, choose a port's address or MAC there, and the credential's "
        "token has neither."
    )
    assert only(found(external, member), "NET_PORT_NOT_PERMITTED").message == f.message
    # with the admin or advsvc role a shared network takes it, an unshared external one only from
    # the admin role: Nova attaches it for no other (review RT-03)
    for roles in (["admin", "member", "reader"], ["advsvc"]):
        assert "NET_PORT_NOT_PERMITTED" not in [x.code for x in found(shared, {"roles": roles})]
    assert "NET_PORT_NOT_PERMITTED" not in [
        x.code for x in found(external, {"roles": ["admin", "member", "reader"]})
    ]
    assert "Nova lets only the admin role attach such a network" in (
        only(found(external, {"roles": ["advsvc"]}), "NET_PORT_NOT_PERMITTED").message
    )
    # another project's private network never does: the admin sees it, the VM must not land there
    for caps in (member, {"admin": True, "roles": ["admin"]}, None, {}):
        f = only(found(private, caps), "NET_PORT_NOT_PERMITTED")
        assert f.message == (
            "The credential of RHOSO 18.0 may not put these NICs on their network: "
            "fa:16:3e:00:00:01 on 'rhoso-net' (project project-b): it is neither shared with the "
            "credential's project nor external."
        )
    # no reported roles (a destination checked by another implementation): that half is not judged
    for caps in (None, {"admin": True}):
        assert "NET_PORT_NOT_PERMITTED" not in [x.code for x in found(shared, caps)]
    # the credential's own network, whatever the roles - unless it is external and not shared
    own = net("o", "rhoso-net")
    assert "NET_PORT_NOT_PERMITTED" not in [x.code for x in found(own, member)]
    own_external = net("x", "rhoso-net", project_id="project-me", shared=False, external=True)
    assert only(found(own_external, member), "NET_PORT_NOT_PERMITTED").message == (
        "The credential of RHOSO 18.0 may not put these NICs on their network: "
        "fa:16:3e:00:00:01 on 'rhoso-net' (project project-me): it is external and not shared, and "
        "Nova lets only the admin role attach such a network to a server; the credential's token "
        "lacks it."
    )
    assert "NET_PORT_NOT_PERMITTED" not in [
        x.code for x in found(own_external, {"roles": ["admin"]})
    ]
    own_shared_external = {**own_external, "shared": True}
    assert "NET_PORT_NOT_PERMITTED" not in [x.code for x in found(own_shared_external, member)]
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
        "Neutron would refuse these addresses on their network in RHOSO 18.0: 00:50:56:aa:bb:cc on "
        "'dc2-prod': 10.2.0.5, 10.2.1.5 lie in different subnets of the port's network: the "
        "migration kit gives a NIC's IPv4 addresses one subnet."
    )
    f = only(found("10.2.0.255"), "NET_ADDRESS_REFUSED")
    assert (
        "00:50:56:aa:bb:cc on 'dc2-prod': 10.2.0.255 is no host address of any subnet" in f.message
    )
    # no IPv4 address at all: nothing to refuse
    assert "NET_ADDRESS_REFUSED" not in [x.code for x in found("fe80::1")]
    # the kit keeps the guest's network settings: a NIC override (itself refused) never moves
    # a VMware NIC, which is judged on the network its port group maps to (§7.2)
    elsewhere = make_plan(
        mappings=Mappings(networks={"DC2-Prod": "dc2-prod"}),
        nic_overrides={"vm-1": {"00:50:56:aa:bb:cc": {"network": "twice"}}},
    )
    twice = details + [net("t1", "twice"), net("t2", "twice")]
    vm = make_vm(
        source_id="vm-1",
        nics=[Nic(network="DC2-Prod", mac="00:50:56:AA:BB:CC", fixed_ips=["10.2.0.5"])],
    )
    codes = [
        x.code
        for x in check(vm, plan=elsewhere, dst=resolved_dst(twice, [first, second]), source=vmware)
    ]
    assert "MAP_NIC_OVERRIDE_INVALID" in codes and "MAP_NETWORK_AMBIGUOUS" not in codes


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


def test_a_blank_destination_name_never_reaches_a_message():
    """SDD §1 naming (review RV20-11): a destination provider stored with a blank name, before the
    API refused one, is named by its id - never "do not exist in : ..."."""
    blank = HIBISCUS.model_copy(update={"id": "blank-dst", "name": "  "})
    plan = make_plan(mappings=Mappings(networks={"app-net": "gone-net"}))
    [found] = [f for f in check(plan=plan, destination=blank) if f.code == "MAP_NETWORK_MISSING"]
    assert "do not exist in blank-dst: app-net -> gone-net." in found.message, found.message


def test_every_text_names_the_destination_provider_by_its_label():
    """SDD §1 naming (review RV20-11): a text that names the destination provider reads its label -
    its name, else its id - never its bare name, which a provider stored before the API refused a
    blank one may lack."""
    root = Path(__file__).parents[1] / "src" / "seamless_migrate"
    bare = re.compile(r"\b(?:destination|provider)\.name\b")
    found = [
        f"{path.relative_to(root)}:{number}: {line.strip()}"
        for path in sorted(root.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if bare.search(line)
    ]
    assert found == []


def test_every_planning_text_about_the_destination_cloud_names_its_provider():
    """SDD §1 naming (review RV21-05): the conversion-host reason, CONV_HOST_MISSING and the
    blocking MAP_VOLUME_TYPE_MISSING name the destination provider by its label, as the
    admin-rights reason does - never "the destination provider", nor its id."""
    name = "OpenStack 2026.2 Hibiscus (DC3)"
    src_caps = {"conversion_host": True, "admin": True}
    dst_caps = {"conversion_host": False, "admin": True}
    reasons = eligibility(make_vm(), "openstack", make_plan(), src_caps, dst_caps, (), name)
    assert f"no conversion host configured on {name}" in reasons[Strategy.cold]
    dst = make_provider(
        id="dst-dc3",
        name=name,
        kind=ProviderKind.openstack,
        role=ProviderRole.destination,
        conversion_host=None,
    )
    conv = only(check(destination=dst), "CONV_HOST_MISSING")
    assert conv.message == f"No conversion host configured on: destination {name}."
    vcenter = make_provider(id="vcenter", kind=ProviderKind.vmware)
    conv = only(check(source=vcenter, destination=dst), "CONV_HOST_MISSING")
    assert conv.message == f"No conversion host configured on: destination {name}."
    mapped = make_plan(mappings=Mappings(volume_types={"ceph-hdd": "ceph-ssd"}))
    other = make_vm(disks=[make_disk(volume_type="ceph-nvme")])
    blocker = only(check(other, plan=mapped, destination=dst), "MAP_VOLUME_TYPE_MISSING")
    assert blocker.message.endswith(f"so the default volume type of {name} cannot be used.")


#: the control plane's fixed texts, which SDD §1 naming lets say "the destination": the catalog's
#: remediations and the role advice they are built from, and the generic texts the advisor gives
#: Jev, which no provider's name may reach (§13.4)
FIXED_TEXTS = {
    ("planning/preflight.py", "CATALOG"),
    ("planning/preflight.py", "_PORT_ROLE_ADVICE"),
    ("ai/advisor.py", "STRATEGY_DESCRIPTIONS"),
    ("ai/payloads.py", "STRATEGY_REQUEST_FIELDS"),
}


def test_no_text_built_for_a_migration_calls_its_destination_the_destination():
    """SDD §1 naming (reviews RV21-05, RV22-05, RV23-06, RV24-04, RV25-05): a text built for a plan
    or a migration - a finding's message, an eligibility reason, an executor's refusal, failure or
    log line, an event, a transition's reason - names the destination provider by its label; "the
    destination" is for the fixed texts (a catalog remediation, the dashboard's copy). Every text
    of the control plane is read - each string literal, and what a + chain, a %-template (given a
    tuple or a mapping), str.format, str.format_map, a join of literals, a logging call or an
    f-string builds from literals - but a docstring, the fixed texts of FIXED_TEXTS, a
    remediation= value, and the bare "the destination" where a text built without a provider falls
    back to it: a parameter's default, an if-else's else, the right of an or, an assignment in an
    except handler, a returned value, a get or getattr default. It reads expressions, not data
    flow: a text completed with += or carried through a variable, an argument or a call's return
    is out of its reach."""
    root = Path(__file__).parents[1] / "src" / "seamless_migrate"
    words = re.compile(r"the destination\b|destination's|destination provider", re.I)
    logging = {"debug", "info", "warning", "warn", "error", "exception", "critical"}

    def mapping(node):
        """A dict literal's string keys with their folded values ("{}" unknown); {} for another."""
        if not isinstance(node, ast.Dict):
            return {}
        return {
            k.value: fold(v) or "{}"
            for k, v in zip(node.keys, node.values, strict=True)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }

    def fields(template, args, kwargs):
        """``template`` with each replacement field filled from ``args``/``kwargs``, "{}" for a
        field the guard cannot know - an attribute, an index, a name it was not given - never an
        error."""
        try:
            parsed = list(string.Formatter().parse(template))
        except ValueError:
            return template
        out, auto = [], 0
        for literal, field, _spec, _conversion in parsed:
            out.append(literal)
            if field is None:
                continue
            if field == "":
                out.append(args[auto] if auto < len(args) else "{}")
                auto += 1
            elif field.isdigit():
                out.append(args[int(field)] if int(field) < len(args) else "{}")
            else:
                out.append(kwargs.get(field, "{}"))
        return "".join(out)

    def percent(template, values):
        """``template % values``, or the template and the values side by side when they do not
        fit."""
        try:
            return template % values
        except (TypeError, ValueError, KeyError):
            given = values.values() if isinstance(values, dict) else values
            return template + " " + " ".join(given)

    def fold(node):
        """The text ``node`` builds from literals, "{}" for each part it cannot know; None when it
        builds no text."""
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            return "".join(
                (fold(part.value) or "{}") if isinstance(part, ast.FormattedValue) else fold(part)
                for part in node.values
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            left, right = fold(node.left), fold(node.right)
            if isinstance(node.op, ast.Add):
                return None if left is None and right is None else (left or "{}") + (right or "{}")
            if left is None:
                return None
            if isinstance(node.right, ast.Dict):
                return percent(left, mapping(node.right))
            args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            return percent(left, tuple(fold(arg) or "{}" for arg in args))
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            return None
        method, receiver = node.func.attr, fold(node.func.value)
        if method in ("format", "format_map") and receiver is not None:
            if method == "format_map":
                return fields(receiver, [], mapping(node.args[0]) if node.args else {})
            args = [fold(arg) or "{}" for arg in node.args]
            return fields(
                receiver, args, {k.arg: fold(k.value) or "{}" for k in node.keywords if k.arg}
            )
        if method == "join" and receiver is not None and node.args:
            items = node.args[0]
            if isinstance(items, (ast.Tuple, ast.List)):
                return receiver.join(fold(item) or "{}" for item in items.elts)
            return None
        # a log line: log.warning(template, *args), log.log(level, template, *args)
        given = node.args[1:] if method == "log" else node.args
        if (method in logging or method == "log") and given and fold(given[0]) is not None:
            values = tuple(fold(arg) or "{}" for arg in given[1:])
            return percent(fold(given[0]), values) if values else fold(given[0])
        return None

    def fallback(node, parents):
        """Whether the bare words stand where a text built without a provider falls back to them."""
        parent = parents.get(id(node))
        return (
            isinstance(parent, ast.arguments)
            or (isinstance(parent, ast.IfExp) and parent.orelse is node)
            or (
                isinstance(parent, ast.BoolOp)
                and isinstance(parent.op, ast.Or)
                and parent.values[-1] is node
            )
            or (
                isinstance(parent, ast.Assign)
                and parent.value is node
                and isinstance(parents.get(id(parent)), ast.ExceptHandler)
            )
            or (isinstance(parent, ast.Return) and parent.value is node)
            or (
                isinstance(parent, ast.Call)
                and (
                    (
                        isinstance(parent.func, ast.Attribute)
                        and parent.func.attr == "get"
                        and parent.args[1:2] == [node]
                    )
                    or (
                        isinstance(parent.func, ast.Name)
                        and parent.func.id == "getattr"
                        and parent.args[2:3] == [node]
                    )
                )
            )
        )

    def fixed(tree, path):
        """The ids of the nodes the rule lets say "the destination"."""
        skip = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    skip.add(id(first.value))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {(path, t.id) for t in targets if isinstance(t, ast.Name)}
                if names & FIXED_TEXTS:
                    skip.update(id(n) for n in ast.walk(node.value))
            elif isinstance(node, ast.keyword) and node.arg == "remediation":
                skip.update(id(n) for n in ast.walk(node.value))
        return skip

    found = []
    for path in sorted(root.rglob("*.py")):
        name = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = fixed(tree, name)
        parents = {
            id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(tree):
            text = None if id(node) in skip else fold(node)
            if text is None or not words.search(text):
                continue
            if (
                text == "the destination"
                and isinstance(node, ast.Constant)
                and fallback(node, parents)
            ):
                continue
            found.append(f"{name}:{node.lineno}: {text}")
    assert found == []


#: one line saying "the destination", injected into orchestrator.py for the guard to find
INJECTED = {
    # the control: a plain message given to an error named *Error
    "PermanentStepError": (
        "\n\ndef _rv22():\n    raise PermanentStepError('the destination refused it')\n"
    ),
    # an error of the control plane whose name does not end in Error
    "NotAllowed": "\n\ndef _rv22():\n    raise NotAllowed('the destination refused it')\n",
    # an error whose message is not its first argument (api.deps.ApiError)
    "ApiError": (
        "\n\ndef _rv22():\n    raise ApiError(409, 'conflict', 'the destination refused it')\n"
    ),
    # an event built for a migration (SDD §1 naming), its message the second argument
    "_emit": (
        "\n\nasync def _rv22(self, m):\n"
        "    await self._emit('migration.error', 'the destination refused it', migration=m)\n"
    ),
}


@pytest.mark.parametrize("case", list(INJECTED))
def test_the_wording_guard_reads_every_plain_message_given_to_an_error_or_an_event(
    case, monkeypatch
):
    """SDD §1 naming (review RV22-05): the guard reads a plain message wherever an error or an
    event takes it - an error raised under any name, ApiError's third argument, _emit's second -
    not only the first argument of a call named *Error."""
    target = Path(__file__).parents[1] / "src" / "seamless_migrate" / "orchestrator.py"
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        text = real(self, *args, **kwargs)
        return text + INJECTED[case] if Path(self) == target else text

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(AssertionError, match="orchestrator.py"):
        test_no_text_built_for_a_migration_calls_its_destination_the_destination()


#: texts the guard missed (review RV23-06) and the shape RV23-07 had: injected into orchestrator.py
INJECTED_RV23 = {
    "_emit by keyword": (
        "\n\nasync def _rv23(self, m):\n"
        "    await self._emit(\n"
        "        'migration.error', message='the destination refused it', migration=m\n"
        "    )\n"
    ),
    "new_event by keyword": (
        "\n\ndef _rv23(m):\n"
        "    return new_event('migration.error', message='the destination refused it')\n"
    ),
    "a module constant given to an error": (
        "\n\nREFUSED_RV23 = 'the destination refused it'\n\n\n"
        "def _rv23():\n    raise NotAllowed(REFUSED_RV23)\n"
    ),
    # the migration.phase event's message: "{vm}: a -> b (reason)"
    "a transition's reason": (
        "\n\nasync def _rv23(self, m, v):\n"
        "    await self._transition(m, v, P.failed, 'the destination refused it')\n"
    ),
    # RV23-07: a rule's text a function hands on, which a caller puts in a refusal
    "a module constant a function returns": (
        "\n\n_RULE_RV23 = {'unknown': \"the destination's policy profile is unknown\"}\n\n\n"
        "def _rv23(profile):\n    return _RULE_RV23[profile]\n"
    ),
}


@pytest.mark.parametrize("case", list(INJECTED_RV23))
def test_the_wording_guard_reads_every_literal_of_the_control_plane(case, monkeypatch):
    """SDD §1 naming (reviews RV23-06, RV23-07): the guard reads every string literal, wherever it
    stands - a keyword argument, a module constant an error is given or a function returns, a
    transition's reason - not only the plain strings a call takes by position."""
    target = Path(__file__).parents[1] / "src" / "seamless_migrate" / "orchestrator.py"
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        text = real(self, *args, **kwargs)
        return text + INJECTED_RV23[case] if Path(self) == target else text

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(AssertionError, match="orchestrator.py"):
        test_no_text_built_for_a_migration_calls_its_destination_the_destination()


#: texts built from several literals that say "the destination" (review RV24-04): injected into
#: orchestrator.py
INJECTED_RV24 = {
    "a text split over +": (
        "\n\ndef _rv24():\n    raise NotAllowed('check the ' + 'destination, then retry')\n"
    ),
    "the bare literal in a %-template": (
        "\n\ndef _rv24():\n    raise NotAllowed('%s refused the port' % 'the destination')\n"
    ),
    "the bare literal given to str.format": (
        "\n\ndef _rv24():\n    raise NotAllowed('{} refused the port'.format('the destination'))\n"
    ),
    "the bare literal in an f-string": (
        "\n\ndef _rv24():\n    raise NotAllowed(f\"{'the destination'} refused the port\")\n"
    ),
    "the bare literal + a literal": (
        "\n\ndef _rv24(m):\n"
        "    return new_event('migration.error', 'the destination' + ' refused the port')\n"
    ),
    # the bare words as a whole message, where no text falls back to them
    "the bare words given to an error": (
        "\n\ndef _rv24():\n    raise NotAllowed('the destination')\n"
    ),
    # the words in no literal of their own: only the text the template builds says them
    "words a %-template builds": (
        "\n\ndef _rv24():\n    raise NotAllowed('check the %s, then retry' % 'destination')\n"
    ),
    "words str.format builds": (
        "\n\ndef _rv24():\n    raise NotAllowed('check the {}, then retry'.format('destination'))\n"
    ),
    "words an f-string builds": (
        "\n\ndef _rv24():\n    raise NotAllowed(f\"check the {'destination'}, then retry\")\n"
    ),
}


#: words a logging call, a mapping, a join or format_map gives a text built for a migration
#: (review RV25-05): injected into orchestrator.py
INJECTED_RV25 = {
    "a log line given its words by the logging call": (
        "\n\ndef _rv25():\n"
        "    log.warning('could not reach the %s: %s', 'destination', 'timeout')\n"
    ),
    "a %-template given its words by a mapping": (
        "\n\ndef _rv25():\n"
        "    raise NotAllowed('check the %(side)s, then retry' % {'side': 'destination'})\n"
    ),
    "a text joined from its words": (
        "\n\ndef _rv25():\n"
        "    raise NotAllowed(' '.join(('check the', 'destination', 'then retry')))\n"
    ),
    "str.format_map": (
        "\n\ndef _rv25():\n"
        "    raise NotAllowed('check the {side}, then retry'.format_map({'side': 'destination'}))\n"
    ),
}


@pytest.mark.parametrize("case", list(INJECTED_RV25))
def test_the_wording_guard_reads_the_words_a_call_gives_a_text(case, monkeypatch):
    """SDD §1 naming (review RV25-05): a log line, a %-template given a mapping, a join of
    literals and str.format_map say "the destination" as plainly as a literal does."""
    target = Path(__file__).parents[1] / "src" / "seamless_migrate" / "orchestrator.py"
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        text = real(self, *args, **kwargs)
        return text + INJECTED_RV25[case] if Path(self) == target else text

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(AssertionError, match="orchestrator.py"):
        test_no_text_built_for_a_migration_calls_its_destination_the_destination()


@pytest.mark.parametrize("case", list(INJECTED_RV24))
def test_the_wording_guard_reads_a_text_built_from_several_literals(case, monkeypatch):
    """SDD §1 naming (review RV24-04): a refusal or an event that says "the destination" is found
    however its text is put together - a + chain, a %-template, str.format, an f-string - and the
    bare "the destination" passes only where a text built without a provider falls back to it."""
    target = Path(__file__).parents[1] / "src" / "seamless_migrate" / "orchestrator.py"
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        text = real(self, *args, **kwargs)
        return text + INJECTED_RV24[case] if Path(self) == target else text

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(AssertionError, match="orchestrator.py"):
        test_no_text_built_for_a_migration_calls_its_destination_the_destination()


#: the texts SDD §1 naming lets say "the destination", injected into orchestrator.py
FIXED_RV23 = {
    "a docstring": '\n\ndef _rv23():\n    """What the destination is given."""\n',
    "a remediation": (
        "\n\ndef _rv23():\n    return finding('X', 'msg', remediation='Ask the destination.')\n"
    ),
    "the fallback when no provider is known": (
        "\n\ndef _rv23(cloud: str = 'the destination'):\n    return cloud\n"
    ),
    # where else a text built without a provider falls back to the bare words (review RV24-04)
    "the fallback of an if-else": (
        "\n\ndef _rv23(p):\n    return p.label if p is not None else 'the destination'\n"
    ),
    "the fallback after or": "\n\ndef _rv23(label):\n    return label or 'the destination'\n",
    "the fallback of an except handler": (
        "\n\ndef _rv23(names, p):\n    try:\n        names[p] = p.label\n"
        "    except AttributeError:\n        names[p] = 'the destination'\n"
    ),
    # review RV25-05: a format field the guard cannot know never errors, and two more fallbacks
    "a str.format field reading the label": (
        "\n\ndef _rv23(p):\n    return '{p.label} refused the port'.format(p=p)\n"
    ),
    "a str.format field reading an index": (
        "\n\ndef _rv23(names):\n    return '{n[dst]} refused the port'.format(n=names)\n"
    ),
    "the fallback a function returns": (
        "\n\ndef _rv23(p):\n    if p is None:\n        return 'the destination'\n"
        "    return p.label\n"
    ),
    "the fallback given to dict.get": (
        "\n\ndef _rv23(names, pid):\n    return names.get(pid, 'the destination')\n"
    ),
}


@pytest.mark.parametrize("case", list(FIXED_RV23))
def test_the_wording_guard_leaves_the_fixed_texts_alone(case, monkeypatch):
    """SDD §1 naming (review RV23-06): a docstring, a catalog remediation and the bare fallback a
    text built without a provider uses say "the destination" - the guard lets them."""
    target = Path(__file__).parents[1] / "src" / "seamless_migrate" / "orchestrator.py"
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        text = real(self, *args, **kwargs)
        return text + FIXED_RV23[case] if Path(self) == target else text

    monkeypatch.setattr(Path, "read_text", read_text)
    test_no_text_built_for_a_migration_calls_its_destination_the_destination()


ELIGIBILITY_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "eligibility_cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", ELIGIBILITY_CASES["finding_cases"], ids=lambda case: case["why"])
def test_the_shared_handover_finding_cases(case):
    """SDD §9.3, §16 (review RV20-08): the findings as the API words them, which the dashboard's
    mock words the same way (mockData.test.ts); `others` are the plan's other selected VMs,
    `source_kind` the source provider's kind; a network case's `destination` gives the
    destination's label, networks, subnets, the token's roles and the policy profile."""
    plan = make_plan(**case["plan"])
    vm = VMRef.model_validate(case["vm"])
    others = [VMRef.model_validate(other) for other in case.get("others", [])]
    source = VMWARE if case.get("source_kind") == "vmware" else SRC
    found = check(vm, plan=plan, all_vms=[vm, *others], source=source, **_destination(case))
    assert {f.code: f.message for f in found if f.code in case["expected"]} == case["expected"]


def test_the_kits_names_judge_a_vmware_sources_vms_only_and_once():
    """SDD §9.3 (reviews RV26-04, RV28-04): only a VMware source's VMs are compared by the
    migration kit's names - an OpenStack VM keeps its own, so web.01 and web_01 are two VMs there
    and a Japanese name is fine. From a VMware source a VM named exactly alike is a kit twin too:
    one finding with the kit's reason and remedy names every VM written alike; two names the kit
    keeps nothing of are no twins, each is unusable."""

    def codes(vm, others, source):
        found = check(vm, plan=make_plan(), all_vms=[vm, *others], source=source)
        names = ("SRC_VM_DUPLICATE_NAME", "SRC_VM_NAME_UNUSABLE")
        return [f.code for f in found if f.code in names]

    dotted, underscored = (
        make_vm(source_id="vm-1", name="web.01"),
        make_vm(source_id="vm-2", name="web_01"),
    )
    assert codes(dotted, [underscored], SRC) == []
    assert codes(make_vm(source_id="vm-3", name="サーバー"), [], SRC) == []
    twin = make_vm(source_id="vm-4", name="web.01")
    found = check(dotted, plan=make_plan(), all_vms=[dotted, twin, underscored], source=VMWARE)
    duplicates = [f for f in found if f.code == "SRC_VM_DUPLICATE_NAME"]
    assert [(f.message, f.remediation) for f in duplicates] == [
        (
            "Another selected VM has the name 'web_01' as the migration kit writes it "
            "(finance/vm-2, finance/vm-4); the kit names the VM's server, ports and volumes after "
            "it.",
            KIT_TWIN_REMEDIATION,
        )
    ]
    # an OpenStack source keeps os-migrate's reason and remedy for the same name
    found = check(dotted, plan=make_plan(), all_vms=[dotted, twin], source=SRC)
    assert [f.message for f in found if f.code == "SRC_VM_DUPLICATE_NAME"] == [
        "Another selected VM is also named 'web.01' (finance/vm-4); os-migrate selects workloads "
        "by name."
    ]
    japanese, cyrillic = (
        make_vm(source_id="vm-5", name="サーバー"),
        make_vm(source_id="vm-6", name="веб"),
    )
    assert codes(japanese, [cyrillic], VMWARE) == ["SRC_VM_NAME_UNUSABLE"]


def _destination(case):
    """check()'s destination for a finding case: its own when the case gives one (SDD §9.3)."""
    dest = case.get("destination")
    if dest is None:
        return {}
    details = dest["networks"]
    return {
        "dst": dst_inv(
            networks={n["name"]: n.get("mtu") for n in details},
            network_details=details,
            subnets=dest.get("subnets", []),
        ),
        "destination": make_provider(
            id="dst-case",
            name=dest["label"],
            kind=ProviderKind(dest.get("kind", "openstack")),
            role=ProviderRole.destination,
            cloud="dst",
        ),
        "dst_caps": {k: dest[k] for k in ("roles", "policy_profile") if k in dest} or None,
    }
