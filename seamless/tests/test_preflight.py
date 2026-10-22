import ast
import json
import re
import string
from pathlib import Path

import pytest

from seamless_migrate.domain.enums import Phase, ProviderKind, ProviderRole, Severity, Strategy
from seamless_migrate.domain.models import (
    ACKNOWLEDGEABLE_FINDINGS,
    DEFAULT_PRESTAGE,
    AppProbe,
    GpuDevice,
    HandoverConfig,
    Mappings,
    Nic,
    VerificationConfig,
    VMRef,
    Wave,
    invalid_plan_settings,
)
from seamless_migrate.planning.gpu import gpu_requests
from seamless_migrate.planning.platforms import destination_release
from seamless_migrate.planning.preflight import (
    AUTOSCALED_ACKNOWLEDGED_REMEDIATION,
    CATALOG,
    KIT_TWIN_REMEDIATION,
    DestinationInventory,
    DnsRead,
    GpuCapacity,
    GpuPool,
    MemberRead,
    PoolMember,
    SourceInventory,
    StackPlace,
    StackRead,
    destination_flavor,
    fitting_flavor,
    flavor_prestaged,
    has_blocker,
    quota_share,
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
    vm=None,
    plan=None,
    src=None,
    dst=None,
    all_vms=None,
    source=SRC,
    destination=DST,
    dst_caps=None,
    shares=None,
    dns_ttl=None,
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
        shares=shares,
        dns_ttl=dns_ttl,
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
        # the Heat and Octavia codes (SDD §9.3, plan task N2)
        "SRC_VM_HEAT_STACK",
        "SRC_VM_AUTOSCALED",
        "SRC_VM_HEAT_UNKNOWN",
        "SRC_VM_LB_MEMBER",
        "SRC_VM_LB_UNKNOWN",
        # the network codes (SDD §9.3, plan task N2b)
        "SRC_PORT_ADDRESS_PAIRS",
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
        "DST_DNS_INTEGRATION_MISSING",
        "NET_MTU_SHRINK",
        "NET_SRIOV_PORT",
        "NET_PORT_IPS",
        "NET_PREFIX_SPLIT",
        "MAP_NETWORK_AMBIGUOUS",
        "NET_ADDRESS_REFUSED",
        "NET_PORT_NOT_PERMITTED",
        "MAP_NIC_OVERRIDE_INVALID",
        "MAP_NIC_OVERRIDE",
        "MAP_FIP_ADDRESS_CHANGE",
        "DST_FIP_ADDRESS_UNAVAILABLE",
        "NET_DNS_TTL_LONG",
        "VM_PCI_PASSTHROUGH",
        "VM_VGPU",
        # the GPU codes (SDD §22.5, plan task G4)
        "MAP_GPU_FLAVOR",
        "MAP_GPU_DOWNSIZED",
        "DST_GPU_CAPACITY_INSUFFICIENT",
        "DST_GPU_CAPACITY_UNKNOWN",
        "GUEST_GPU_DRIVER",
        "GUEST_GPU_LICENSE",
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


@pytest.mark.parametrize("power_state", ["running", "stopped", "transitioning"])
def test_a_nova_task_in_flight_is_a_transitional_blocker_naming_the_task(power_state):
    """SDD §9.3 (review PV-05): a task Nova runs whatever the status - a backup's image upload under
    ACTIVE, a stop in flight - is a blocker before approval, naming the task: the stop or the
    snapshot would meet Nova's 409 after it."""
    vm = make_vm(power_state=power_state, task_state="image_uploading")
    f = only(check(vm), "SRC_VM_TRANSITIONAL_STATE")
    assert f.severity == Severity.blocker
    assert f.message == (
        "Source VM web-01 has a Nova task in flight (image_uploading); a stop or snapshot "
        "would fail."
    )
    # a server in error with a task - a delete Nova runs on it - meets both conditions (SDD §9.3)
    codes = {x.code for x in check(make_vm(power_state="error", task_state="deleting"))}
    assert {"SRC_VM_ERROR_STATE", "SRC_VM_TRANSITIONAL_STATE"} <= codes


#: the source's Heat and Octavia reads (SDD §10, plan task N2): synthetic names under example,
#: addresses of the documentation ranges
ASG = "OS::Heat::AutoScalingGroup"
HEAT_CODES = ("SRC_VM_HEAT_STACK", "SRC_VM_AUTOSCALED", "SRC_VM_HEAT_UNKNOWN")
LB_CODES = ("SRC_VM_LB_MEMBER", "SRC_VM_LB_UNKNOWN")


def web_vm(**kw):
    """A VM with one NIC on app-net at 192.0.2.10 and one on db-net at 2001:db8::10."""
    nics = [
        Nic(network="app-net", mac="fa:16:3e:00:00:01", fixed_ips=["192.0.2.10"], mtu=1500),
        Nic(network="db-net", mac="fa:16:3e:00:00:02", fixed_ips=["2001:db8::10"], mtu=1500),
    ]
    return make_vm(**{"nics": nics, **kw})


def stacked(**places):
    """A source inventory whose Heat read holds each server's place (SDD §10)."""
    return src_inv(stacks=StackRead(servers={k: StackPlace(**v) for k, v in places.items()}))


def found_codes(findings, codes):
    return [f.code for f in findings if f.code in codes]


def test_finding_src_vm_heat_stack():
    """SDD §9.3 (plan task N2): a VM whose server is a resource of a Heat stack at the source - the
    stack is not moved, and a stack update there may recreate the server: a warning naming the
    top-level stack and its resource. A ResourceGroup's member is a stack's resource, never an
    autoscaling group's; a VM no stack holds gets nothing, nor one the read did not name."""
    src = stacked(**{"vm-1": {"stack": "example-web", "resource": "web_server"}})
    f = only(check(web_vm(), src=src), "SRC_VM_HEAT_STACK")
    assert (f.severity, f.strategies) == (Severity.warning, [])
    assert f.message == (
        "web-01 belongs to Heat stack 'example-web' at RHOSP 17.1 (its resource 'web_server'): "
        "the stack is not moved, and a stack update there may recreate the server."
    )
    assert f.remediation == CATALOG["SRC_VM_HEAT_STACK"].remediation
    assert "runbook for Heat stacks" in f.remediation
    assert found_codes(check(web_vm(), src=src), HEAT_CODES) == ["SRC_VM_HEAT_STACK"]
    # a ResourceGroup's member: the read names no autoscaling group for it
    group = stacked(**{"vm-1": {"stack": "example-web", "resource": "web_group"}})
    assert found_codes(check(web_vm(), src=group), HEAT_CODES) == ["SRC_VM_HEAT_STACK"]
    assert found_codes(check(web_vm(source_id="vm-2"), src=src), HEAT_CODES) == []
    assert found_codes(check(web_vm()), HEAT_CODES) == []


def test_finding_src_vm_autoscaled_blocks_until_acknowledged():
    """SDD §9.3 (owner decision 9, §23.8): a member of a Heat autoscaling group is blocked - the
    group may replace a server the cutover stopped, which leaves two live copies - until the plan
    acknowledges it for the VM (plan.acknowledged_findings, §4.2). Acknowledged, it stays a
    finding, a warning that says so, with its own remediation; another VM's acknowledgement, or a
    plan acknowledging nothing, keeps it a blocker. The stack warning comes beside it."""
    place = {"stack": "example-web", "resource": "web_asg", "group": "web_asg", "group_type": ASG}
    src = stacked(**{"vm-1": place})
    found = check(web_vm(), src=src)
    assert found_codes(found, HEAT_CODES) == ["SRC_VM_HEAT_STACK", "SRC_VM_AUTOSCALED"]
    f = only(found, "SRC_VM_AUTOSCALED")
    assert (f.severity, f.strategies) == (Severity.blocker, [])
    text = (
        "web-01 is a member of autoscaling group 'web_asg' (OS::Heat::AutoScalingGroup) of Heat "
        "stack 'example-web' at RHOSP 17.1: the group may replace the server once the cutover "
        "stops it, which leaves two live copies."
    )
    assert f.message == text
    assert f.remediation == CATALOG["SRC_VM_AUTOSCALED"].remediation
    assert "acknowledged_findings" in f.remediation
    assert has_blocker(found)
    # the plan acknowledges it for the VM: a warning that names the acknowledgement
    plan = make_plan(acknowledged_findings={"vm-1": ["SRC_VM_AUTOSCALED"]})
    assert plan.acknowledged_findings == {"vm-1": ["SRC_VM_AUTOSCALED"]}
    acknowledged = check(web_vm(), plan=plan, src=src)
    f = only(acknowledged, "SRC_VM_AUTOSCALED")
    assert f.severity == Severity.warning
    assert f.message == f"{text} The plan acknowledges it (acknowledged_findings)."
    assert f.remediation == AUTOSCALED_ACKNOWLEDGED_REMEDIATION
    assert f.remediation != CATALOG["SRC_VM_AUTOSCALED"].remediation
    assert not has_blocker(acknowledged)
    # another VM's acknowledgement is not this one's
    other = make_plan(acknowledged_findings={"vm-2": ["SRC_VM_AUTOSCALED"]})
    assert only(check(web_vm(), plan=other, src=src), "SRC_VM_AUTOSCALED").severity == "blocker"
    # an AWS autoscaling group counts alike
    aws = stacked(**{"vm-1": {**place, "group_type": "AWS::AutoScaling::AutoScalingGroup"}})
    assert only(check(web_vm(), src=aws), "SRC_VM_AUTOSCALED").severity == Severity.blocker


def test_finding_src_vm_heat_unknown_when_heat_cannot_be_read():
    """SDD §9.3, §10 (plan task N2): Heat refused the source credential, or its read failed -
    whether the VM is a stack's resource or an autoscaling group's member is not known. A warning
    naming why, never a guess and never a blocker; a source without Heat reads ok and says
    nothing."""
    refused = src_inv(stacks=StackRead(status="unknown", reason="forbidden", detail="HTTP 403"))
    found = check(web_vm(), src=refused)
    f = only(found, "SRC_VM_HEAT_UNKNOWN")
    assert (f.severity, f.strategies) == (Severity.warning, [])
    assert f.message == (
        "Whether web-01 belongs to a Heat stack or an autoscaling group at RHOSP 17.1 is not "
        "known: Heat refused the source credential (HTTP 403)."
    )
    assert found_codes(found, HEAT_CODES) == ["SRC_VM_HEAT_UNKNOWN"] and not has_blocker(found)
    failed = src_inv(stacks=StackRead(status="unknown", reason="error", detail="ConnectTimeout"))
    assert only(check(web_vm(), src=failed), "SRC_VM_HEAT_UNKNOWN").message == (
        "Whether web-01 belongs to a Heat stack or an autoscaling group at RHOSP 17.1 is not "
        "known: its read failed (ConnectTimeout)."
    )


def test_finding_src_vm_lb_member():
    """SDD §9.3 (plan task N2): an address of the VM's NIC is a member of an Octavia pool at the
    source - matched by its address as Neutron writes it, on the NIC's network when the read names
    the member's -: one warning naming each membership, its address and port, pool and load
    balancer. A member on another network, or of another address, is not the VM's."""
    members = [
        PoolMember(
            load_balancer="example-lb",
            pool="web-pool",
            address="192.0.2.10",
            port=443,
            network="app-net",
        ),
        PoolMember(
            load_balancer="example-lb",
            pool="admin-pool",
            address="192.0.2.10",
            port=8443,
            network=None,
        ),
        # the same address on another network: another project's VM, not this one
        PoolMember(
            load_balancer="other-lb",
            pool="other-pool",
            address="192.0.2.10",
            port=80,
            network="other-net",
        ),
        PoolMember(
            load_balancer="example-lb",
            pool="db-pool",
            address="2001:db8::10",
            port=5432,
            network="db-net",
        ),
        PoolMember(
            load_balancer="example-lb",
            pool="web-pool",
            address="192.0.2.11",
            port=443,
            network="app-net",
        ),
    ]
    src = src_inv(lb_members=MemberRead(members=members))
    f = only(check(web_vm(), src=src), "SRC_VM_LB_MEMBER")
    assert (f.severity, f.strategies) == (Severity.warning, [])
    assert f.message == (
        "Addresses of web-01 are members of Octavia pools at RHOSP 17.1: 192.0.2.10 port 443 in "
        "pool 'web-pool' of load balancer 'example-lb'; 192.0.2.10 port 8443 in pool 'admin-pool' "
        "of load balancer 'example-lb'; 2001:db8::10 port 5432 in pool 'db-pool' of load balancer "
        "'example-lb'. The memberships are not moved."
    )
    assert "runbook for Octavia load balancers" in f.remediation
    assert f.remediation == CATALOG["SRC_VM_LB_MEMBER"].remediation
    # an address written another way is the same address
    long_form = web_vm(
        nics=[Nic(network="db-net", mac="fa:16:3e:00:00:02", fixed_ips=["2001:0db8:0:0::10"])]
    )
    assert "2001:db8::10 port 5432" in only(check(long_form, src=src), "SRC_VM_LB_MEMBER").message
    lonely = web_vm(
        nics=[Nic(network="app-net", mac="fa:16:3e:00:00:03", fixed_ips=["192.0.2.99"])]
    )
    assert found_codes(check(lonely, src=src), LB_CODES) == []
    assert found_codes(check(web_vm()), LB_CODES) == []


def test_finding_src_vm_lb_unknown_when_octavia_cannot_be_read():
    """SDD §9.3, §10 (plan task N2): Octavia refused the source credential, or its read failed -
    whether an address of the VM is a pool member is not known: a warning naming why, never a
    blocker."""
    refused = src_inv(
        lb_members=MemberRead(status="unknown", reason="forbidden", detail="HTTP 403")
    )
    found = check(web_vm(), src=refused)
    f = only(found, "SRC_VM_LB_UNKNOWN")
    assert (f.severity, f.strategies) == (Severity.warning, [])
    assert f.message == (
        "Whether addresses of web-01 are members of Octavia pools at RHOSP 17.1 is not known: "
        "Octavia refused the source credential (HTTP 403)."
    )
    assert found_codes(found, LB_CODES) == ["SRC_VM_LB_UNKNOWN"] and not has_blocker(found)
    failed = src_inv(lb_members=MemberRead(status="unknown", reason="error", detail="HTTP 503"))
    assert only(check(web_vm(), src=failed), "SRC_VM_LB_UNKNOWN").message.endswith(
        "is not known: its read failed (HTTP 503)."
    )


NETWORK_CODES = (
    "SRC_PORT_ADDRESS_PAIRS",
    "MAP_FIP_ADDRESS_CHANGE",
    "DST_DNS_INTEGRATION_MISSING",
    "NET_PREFIX_SPLIT",
)
VMWARE_SRC = make_provider(id="src-vc", name="vCenter 8", kind=ProviderKind.vmware)


def test_finding_src_port_address_pairs():
    """SDD §9.3 (plan task N2b, owner decision 11): a NIC of an OpenStack VM with allowed address
    pairs - a keepalived VIP, say - is created at the destination without them: one warning
    naming each such NIC by its MAC and network, with its pairs' addresses. A NIC without pairs is
    not named; a VM without any gets nothing."""
    vm = web_vm(
        nics=[
            Nic(
                network="app-net",
                mac="FA:16:3E:00:00:01",
                fixed_ips=["192.0.2.10"],
                allowed_address_pairs=["192.0.2.100", "192.0.2.96/28"],
            ),
            Nic(network="db-net", mac="fa:16:3e:00:00:02", fixed_ips=["2001:db8::10"]),
            Nic(
                network="db-net",
                mac="fa:16:3e:00:00:03",
                fixed_ips=["2001:db8::11"],
                allowed_address_pairs=["2001:db8::100"],
            ),
        ]
    )
    f = only(check(vm), "SRC_PORT_ADDRESS_PAIRS")
    assert (f.severity, f.strategies) == (Severity.warning, [])
    assert f.message == (
        "NICs of web-01 have allowed address pairs, which the import does not carry: "
        "fa:16:3e:00:00:01 on 'app-net' (192.0.2.100, 192.0.2.96/28); fa:16:3e:00:00:03 on "
        "'db-net' (2001:db8::100). An address the VM takes over from a peer is not reachable in "
        "RHOSO 18.0."
    )
    assert f.remediation == CATALOG["SRC_PORT_ADDRESS_PAIRS"].remediation
    assert "allowed-address" in f.remediation
    found = check(vm)
    assert found_codes(found, NETWORK_CODES) == ["SRC_PORT_ADDRESS_PAIRS"]
    assert not has_blocker(found)
    assert found_codes(check(web_vm()), NETWORK_CODES) == []


def test_finding_map_fip_address_change():
    """SDD §9.3 (plan task N2b, §23.1): os-migrate's floating_ip_mode auto attaches a detached
    floating IP of the source's address, else creates one with a new address: a warning for cold
    and warm naming each floating IP of the VM the destination's project does not hold detached -
    an address written another way is the same; none when it holds each, nor for a VM without."""
    vm = web_vm(
        nics=[
            Nic(
                network="app-net",
                mac="fa:16:3e:00:00:01",
                fixed_ips=["192.0.2.10"],
                floating_ips=["203.0.113.10", "203.0.113.11"],
            ),
            Nic(
                network="db-net",
                mac="fa:16:3e:00:00:02",
                fixed_ips=["2001:db8::10"],
                floating_ips=["2001:db8:f::0010"],
            ),
        ]
    )
    held = dst_inv(floating_ips=["203.0.113.11"])
    f = only(check(vm, dst=held), "MAP_FIP_ADDRESS_CHANGE")
    assert (f.severity, f.strategies) == (Severity.warning, [Strategy.cold, Strategy.warm])
    assert f.message == (
        "Floating IPs of web-01 that RHOSO 18.0 does not hold detached in its project: "
        "203.0.113.10, 2001:db8:f::10. os-migrate creates a floating IP with a new address for "
        "each, so clients, allowlists and DNS records of the old address lose the VM."
    )
    assert f.remediation == CATALOG["MAP_FIP_ADDRESS_CHANGE"].remediation
    every = dst_inv(floating_ips=["203.0.113.10", "203.0.113.11", "2001:db8:f::10"])
    assert found_codes(check(vm, dst=every), NETWORK_CODES) == []
    assert found_codes(check(web_vm()), NETWORK_CODES) == []
    # the strategies that move no floating IP stay eligible: a warning bars none (SDD §9.2)
    assert not has_blocker(check(vm, dst=held))


def test_finding_dst_dns_integration_missing():
    """SDD §9.3 (plan task N2b, §23.5): a NIC published in DNS by Neutron's DNS integration at the
    source - a DNS name with a domain - and a destination without the dns-integration extension,
    or without a Designate zone of that domain (compared lower case with its trailing dot): a
    warning naming the NICs and what is lacking. A destination whose extensions or zones could not
    be read is not judged on them; a NIC without a DNS name gets nothing."""
    vm = web_vm(
        nics=[
            Nic(
                network="app-net",
                mac="fa:16:3e:00:00:01",
                fixed_ips=["192.0.2.10"],
                dns_name="web-01",
                dns_domain="Example.COM.",
            ),
            Nic(
                network="db-net",
                mac="fa:16:3e:00:00:02",
                fixed_ips=["2001:db8::10"],
                dns_name="web-01-db",
                dns_domain="db.example.com.",
            ),
            # a domain without a name publishes nothing
            Nic(network="db-net", mac="fa:16:3e:00:00:03", dns_domain="example.com."),
        ]
    )

    def dns(extension, zones):
        return dst_inv(dns=DnsRead(extension=extension, zones=zones))

    f = only(
        check(vm, dst=dns(False, ["example.com.", "db.example.com."])),
        "DST_DNS_INTEGRATION_MISSING",
    )
    assert (f.severity, f.strategies) == (Severity.warning, [])
    assert f.message == (
        "NICs of web-01 are published in DNS at RHOSP 17.1 - fa:16:3e:00:00:01 as "
        "web-01.example.com., fa:16:3e:00:00:02 as web-01-db.db.example.com. - and RHOSO 18.0 "
        "lacks Neutron's dns-integration extension: the migrated ports are not published."
    )
    assert f.remediation == CATALOG["DST_DNS_INTEGRATION_MISSING"].remediation
    # the extension is there, one zone is not (its domain written without the trailing dot)
    zone = only(check(vm, dst=dns(True, ["EXAMPLE.com"])), "DST_DNS_INTEGRATION_MISSING")
    assert zone.message == (
        "NICs of web-01 are published in DNS at RHOSP 17.1 - fa:16:3e:00:00:02 as "
        "web-01-db.db.example.com. - and RHOSO 18.0 has no Designate zone db.example.com.: the "
        "migrated ports are not published."
    )
    both = only(check(vm, dst=dns(False, [])), "DST_DNS_INTEGRATION_MISSING")
    assert both.message.endswith(
        "lacks Neutron's dns-integration extension and has no Designate zone example.com., "
        "db.example.com.: the migrated ports are not published."
    )
    for fine in (
        dns(True, ["example.com.", "db.example.com."]),
        dns(None, ["example.com", "db.example.com"]),  # extensions unread: not judged
        dns(True, None),  # Designate unread: not judged
        dns(None, None),
    ):
        assert found_codes(check(vm, dst=fine), NETWORK_CODES) == []
    assert found_codes(check(web_vm(), dst=dns(False, [])), NETWORK_CODES) == []


def test_finding_net_prefix_split():
    """SDD §9.3 (plan task N2b, §23.6): when the source or the destination provider declares
    announces_tenant_networks, the plan's waves putting the VMs of one source network into more
    than one wave warn - the prefix would have an origin in each cloud: the message names each
    network, its waves by name in their order and the declaring providers. Nothing is judged
    before the waves are planned, nor without a declaration, nor for a network in one wave."""
    web = web_vm()
    peer = make_vm(
        source_id="vm-2", name="web-02", nics=[Nic(network="app-net", mac="fa:16:3e:00:00:12")]
    )
    db = make_vm(
        source_id="vm-3", name="db-01", nics=[Nic(network="db-net", mac="fa:16:3e:00:00:13")]
    )
    vms = [web, peer, db]
    waves = [
        Wave(id="w2", name="Wave 2", order=2, vm_ids=["vm-2"]),
        Wave(id="w1", name="Wave 1", order=1, vm_ids=["vm-1", "vm-3"]),
    ]
    plan = make_plan(vm_ids=["vm-1", "vm-2", "vm-3"], waves=waves)
    bgp = DST.model_copy(update={"announces_tenant_networks": True})
    f = only(check(web, plan=plan, all_vms=vms, destination=bgp), "NET_PREFIX_SPLIT")
    assert (f.severity, f.strategies) == (Severity.warning, [])
    assert f.message == (
        "The plan's waves split networks of web-01 that RHOSO 18.0 announces as tenant networks: "
        "'app-net' in waves 'Wave 1', 'Wave 2'. While one wave is cut over and the next is not, "
        "each prefix has an origin in both clouds, so its routes cannot be correct."
    )
    assert f.remediation == CATALOG["NET_PREFIX_SPLIT"].remediation
    # the VM of the other wave is named alike; the one whose network is in one wave is not
    assert (
        "'app-net' in waves 'Wave 1', 'Wave 2'"
        in only(check(peer, plan=plan, all_vms=vms, destination=bgp), "NET_PREFIX_SPLIT").message
    )
    assert found_codes(check(db, plan=plan, all_vms=vms, destination=bgp), NETWORK_CODES) == []
    # both providers declaring it are named
    announcing = SRC.model_copy(update={"announces_tenant_networks": True})
    both = only(
        check(web, plan=plan, all_vms=vms, source=announcing, destination=bgp), "NET_PREFIX_SPLIT"
    )
    assert "that RHOSP 17.1 and RHOSO 18.0 announce as tenant networks" in both.message
    # no declaration, or no waves yet: nothing
    assert found_codes(check(web, plan=plan, all_vms=vms), NETWORK_CODES) == []
    unplanned = make_plan(vm_ids=["vm-1", "vm-2", "vm-3"])
    assert (
        found_codes(check(web, plan=unplanned, all_vms=vms, destination=bgp), NETWORK_CODES) == []
    )


def test_finding_net_dns_ttl_long():
    """SDD §9.3 (plan task N7): the record's name as the target writes it - relative to its zone,
    or fully qualified - in the warning, which a TTL one second over the SLO gets and one equal to
    it does not (tests/test_dns_swap.py has the rest)."""
    target = {"kind": "designate", "zone": "example.com", "record": "db.example.com."}
    plan = make_plan(downtime_slo_s=300, network_cutover={"dns_targets": {"vm-1": target}})
    f = only(check(plan=plan, dns_ttl=301), "NET_DNS_TTL_LONG")
    assert f.message.startswith("The DNS record db.example.com. holds a TTL of 301 s")
    assert f.remediation == CATALOG["NET_DNS_TTL_LONG"].remediation
    assert "NET_DNS_TTL_LONG" not in [x.code for x in check(plan=plan, dns_ttl=300)]


def test_finding_dst_fip_address_unavailable():
    """SDD §9.3 (plan task N6a): the quota counts the floating IPs of every VM of the plan the
    destination's project does not hold detached - the other VM's too -, a VMware VM has none to
    keep, and under auto the blocker is never judged (tests/test_fip_move.py has its reasons)."""
    keep = make_plan(network_cutover={"floating_ips": "keep_address"})
    first = make_vm(
        nics=[Nic(network="app-net", mac="fa:16:3e:00:00:01", floating_ips=["203.0.113.10"])]
    )
    second = make_vm(
        source_id="vm-2",
        name="web-02",
        nics=[Nic(network="app-net", mac="fa:16:3e:00:00:02", floating_ips=["203.0.113.11"])],
    )
    quota = dst_inv(floating_ip_quota=1)
    f = only(
        check(first, plan=keep, dst=quota, all_vms=[first, second]), "DST_FIP_ADDRESS_UNAVAILABLE"
    )
    assert (f.severity, f.strategies) == (Severity.blocker, [])
    assert f.message == (
        "Floating IPs of web-01 that RHOSO 18.0 cannot keep: the project's floating IP quota "
        "leaves 1 for 2 addresses."
    )
    assert f.remediation == CATALOG["DST_FIP_ADDRESS_UNAVAILABLE"].remediation
    held = dst_inv(floating_ip_quota=1, floating_ips=["203.0.113.11"])
    assert "DST_FIP_ADDRESS_UNAVAILABLE" not in [
        x.code for x in check(first, plan=keep, dst=held, all_vms=[first, second])
    ]
    assert "DST_FIP_ADDRESS_UNAVAILABLE" not in [
        x.code for x in check(first, dst=quota, all_vms=[first, second])
    ]
    vmware = make_vm(flavor=None, nics=[Nic(network="VM Network", mac="00:50:56:00:00:01")])
    assert "DST_FIP_ADDRESS_UNAVAILABLE" not in [
        x.code for x in check(vmware, plan=keep, dst=quota, source=VMWARE_SRC)
    ]


def test_a_provider_declares_announced_tenant_networks_only_for_openstack():
    """SDD §4.2 (plan task N2b): announces_tenant_networks defaults to false, an OpenStack or
    RHOSO provider may declare it, and a vmware one is refused - vCenter announces nothing."""
    assert make_provider().announces_tenant_networks is False
    assert make_provider(announces_tenant_networks=True).announces_tenant_networks is True
    with pytest.raises(ValueError, match="announces_tenant_networks"):
        make_provider(kind=ProviderKind.vmware, announces_tenant_networks=True)
    assert VMWARE_SRC.announces_tenant_networks is False


def test_a_vmware_vms_nics_carry_none_of_the_network_findings():
    """SDD §9.3 (plan task N2b): the pairs, floating IPs and DNS names are an OpenStack port's; a
    VMware VM's NICs have none, so none of those findings is reported for it."""
    vm = make_vm(flavor=None, nics=[Nic(network="VM Network", mac="00:50:56:00:00:01")])
    assert (
        found_codes(
            check(vm, source=VMWARE_SRC, dst=dst_inv(dns=DnsRead(extension=False, zones=[]))),
            NETWORK_CODES,
        )
        == []
    )


def test_a_plan_acknowledges_only_the_acknowledgeable_findings():
    """SDD §4.2, §12 (plan task N2): plan.acknowledged_findings names, per VM, the codes the plan
    acknowledges - SRC_VM_AUTOSCALED alone; any other code is refused before the plan is stored,
    and a validation refuses a stored plan with one."""
    assert ACKNOWLEDGEABLE_FINDINGS == frozenset({"SRC_VM_AUTOSCALED"})
    fine = make_plan(acknowledged_findings={"vm-1": ["SRC_VM_AUTOSCALED"], "vm-2": []})
    assert fine.acknowledged_findings == {"vm-1": ["SRC_VM_AUTOSCALED"], "vm-2": []}
    assert invalid_plan_settings(fine) == []
    wrong = make_plan(
        acknowledged_findings={"vm-1": ["SRC_VM_HEAT_STACK", "SRC_VM_AUTOSCALED", "NOPE"]}
    )
    assert invalid_plan_settings(wrong) == [
        "acknowledged_findings[vm-1]: SRC_VM_HEAT_STACK, NOPE cannot be acknowledged "
        "(only SRC_VM_AUTOSCALED can)"
    ]


@pytest.mark.parametrize("quotas", [{}, None], ids=["quotas unreadable", "quotas read"])
def test_finding_dst_project_missing_against_the_listed_projects(quotas):
    """SDD §9.3 (review PL-06): the destination project is judged against the projects the
    inventory lists, not the quota table - which the provider leaves empty when its quota calls
    fail while it still lists the project: a mapping to a project the destination does not list
    blocks either way, and one to a listed project does not."""
    kw = {} if quotas is None else {"quotas": quotas}
    remapped = make_plan(mappings=Mappings(projects={"finance": "fin-rhoso"}))
    blocker = only(check(plan=remapped, dst=dst_inv(**kw)), "DST_PROJECT_MISSING")
    assert "'fin-rhoso'" in blocker.message
    listed = make_plan(mappings=Mappings(projects={"finance": "finance"}))
    assert "DST_PROJECT_MISSING" not in [f.code for f in check(plan=listed, dst=dst_inv(**kw))]


def test_finding_dst_project_missing_judges_nothing_without_a_project_list():
    """SDD §9.3 (review PL-06): a destination that lists no project gives nothing to judge by."""
    remapped = make_plan(mappings=Mappings(projects={"finance": "fin-rhoso"}))
    findings = check(plan=remapped, dst=dst_inv(projects=[], quotas={}))
    assert "DST_PROJECT_MISSING" not in [f.code for f in findings]


def test_finding_dst_project_missing_and_image_root_quota():
    dst = dst_inv(
        # the projects the destination lists, which the check judges by (review PL-06)
        projects=["finance-rhoso", "shop-rhoso"],
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
        },
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


#: the same, for a migration that holds the destination volumes an earlier attempt created (review
#: RV38A-01): its server alone wherever it would count all of it
HELD_SHARES = {
    phase: "nothing" if PHASE_SHARES[phase] == "nothing" else "server" for phase in Phase
}
SHARE_CASES = [(phase, False, share) for phase, share in PHASE_SHARES.items()] + [
    (phase, True, share) for phase, share in HELD_SHARES.items()
]


def test_every_phase_has_a_quota_share():
    assert set(PHASE_SHARES) == {None, *Phase}


@pytest.mark.parametrize(
    "phase, holds_volumes, expected",
    SHARE_CASES,
    ids=[f"{phase}{' holding its volumes' if held else ''}" for phase, held, _ in SHARE_CASES],
)
def test_the_quota_counts_what_a_cutover_will_still_create(phase, holds_volumes, expected):
    """SDD §9.3 (reviews PL-02, RV38A-01): the free quota is the limit less what is in use, so each
    VM counts what a cutover will still create - all of it while its migration runs, or runs again,
    from ready, but its server alone when it holds the destination volumes an earlier attempt
    created; its server alone while its destination volumes exist or are being created; nothing
    once what it holds is in use, or being released. A free quota of exactly the assessed VM's
    demand and the other VM's share fits; one unit less of any key does not."""
    assert quota_share(phase, holds_volumes=holds_volumes) == expected
    a = make_vm(source_id="a", name="a")
    b = make_vm(source_id="b", name="b")
    share = QUOTA_SHARES[expected]
    free = {key: QUOTA_SHARES["all"][key] + share[key] for key in share}
    shares = {} if phase is None else {"b": quota_share(phase, holds_volumes=holds_volumes)}

    def assessed(quota):
        return run_preflight(
            a,
            make_plan(),
            src_inv(),
            dst_inv(quotas={"finance": quota}),
            [a, b],
            source=SRC,
            destination=DST,
            shares=shares,
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


def test_finding_vm_vgpu_from_a_vmware_vgpu_device():
    """SDD §9.3, §22.2 (plan tasks G2, G4): a VMware VM's vGPU, which its provider reports in gpus
    and no longer as a flavor extra spec, blocks the VM while no GPU mapping applies to it."""
    vm = make_vm(gpus=[GpuDevice(kind="vgpu", profile="grid_a100-4c", count=2)])
    found = only(check(vm, source=VMWARE), "VM_VGPU")
    assert (found.severity, found.message) == (
        Severity.blocker,
        "The VM has a vGPU (grid_a100-4c ×2). The plan's GPU mappings name no flavor for "
        "'grid_a100-4c': map it to a GPU flavor of RHOSO 18.0.",
    )


def test_finding_vm_pci_passthrough_from_a_vmware_passthrough_device():
    """SDD §9.3, §22.2 (plan task G2): a VMware VM's DirectPath I/O devices, reported in gpus, block
    the VM as before; a device without ids is named as one."""
    vm = make_vm(
        gpus=[
            GpuDevice(kind="passthrough", vendor_id="10de", device_id="20b5"),
            GpuDevice(kind="passthrough"),
        ]
    )
    found = only(check(vm, source=VMWARE), "VM_PCI_PASSTHROUGH")
    assert (found.severity, found.message) == (
        Severity.blocker,
        "The VM has a PCI passthrough device (10de:20b5, a device without ids). No mapping can "
        "name its device without ids, so none applies to the VM.",
    )


# -- GPU mappings and the GPU findings (SDD §22.4, §22.5, plan task G4) --------------------------

A100 = "CUSTOM_NVIDIA_A100_4C"
L40 = "CUSTOM_NVIDIA_L40_4Q"
#: one vGPU with the A100 trait, two of them, one with the L40 trait (SDD §22.2)
ON_A100 = {"resources:VGPU": "1", f"trait:{A100}": "required"}
TWO_ON_A100 = {"resources:VGPU": "2", f"trait:{A100}": "required"}
ON_L40 = {"resources:VGPU": "1", f"trait:{L40}": "required"}
#: GPU flavors: (name, vcpus, ram_mb, disk_gb, extra specs, the profile their provider declares)
A4C = ("g1.a100-4c", 8, 32768, 80, ON_A100, "A100-4C")
A2C = ("g1.a100-2c", 4, 16384, 40, ON_A100, "A100-2C")
A4C2 = ("g1.a100-4c-x2", 16, 65536, 80, TWO_ON_A100, "A100-4C")
MIG = ("g1.a100-3-20c", 16, 65536, 80, ON_A100, "A100-3-20C")
L4Q = ("g1.l40-4q", 8, 32768, 80, ON_L40, "L40-4Q")
NOSIZE = ("g1.mdev", 8, 32768, 80, ON_A100, "nvidia-471")
PLAIN = ("g1.plain", 8, 32768, 80, ON_A100, None)
P1 = ("p1.a100", 16, 131072, 80, {"pci_passthrough:alias": "a100:1"}, None)
PVF = ("p1.l4-vgpu", 8, 32768, 80, {"pci_passthrough:alias": "l4-vf:1"}, "L4-24Q")
OPEN = [GpuPool("VGPU", [A100], [], free=8, providers=2, largest_free=4),
        GpuPool("VGPU", [L40], [], free=8, providers=2, largest_free=4),
        GpuPool("VGPU", [], [], free=16, providers=4, largest_free=4)]  # fmt: skip


def gpu_dst(*flavors, pools=OPEN, status="ok", reason=None, detail=None, **kw):
    """A destination with dst_inv()'s flavors and these GPU flavors - (name, vcpus, ram_mb, disk_gb,
    extra specs, the profile its provider declares) -, whose capacity reads `pools` (SDD §22.3)."""
    listed = [
        {"name": n, "vcpus": v, "ram_mb": r, "disk_gb": d, "extra_specs": dict(specs)}
        for n, v, r, d, specs, _ in flavors
    ]
    gpu = [
        {"name": n, "vcpus": v, "ram_mb": r, "disk_gb": d, "gpus": gpu_requests(specs, profile)}
        for n, v, r, d, specs, profile in flavors
    ]
    capacity = GpuCapacity(status=status, reason=reason, detail=detail, pools=list(pools))
    return dst_inv(
        flavors=[*dst_inv().flavors, *listed], gpu_flavors=gpu, gpu_capacity=capacity, **kw
    )


def vmware_gpu_vm(*gpus, **kw):
    """A VMware VM - no flavor - with these GPUs, 8 vCPUs and 32 GiB by default."""
    return make_vm(**{"flavor": None, "vcpus": 8, "ram_mb": 32768, "gpus": list(gpus), **kw})


def openstack_gpu_vm(flavor="g1.vgpu", specs=None, **kw):
    """An OpenStack VM whose flavor asks for a GPU, its gpus as its source reports them."""
    specs = specs if specs is not None else dict(ON_A100)
    fields = {"vcpus": 8, "ram_mb": 32768, "flavor_extra_specs": specs, "gpus": gpu_requests(specs)}
    return make_vm(**{"flavor": flavor, **fields, **kw})


VGPU_4C = GpuDevice(kind="vgpu", profile="grid_a100-4c")
PASS_ID = GpuDevice(kind="passthrough", vendor_id="10de", device_id="20b5")


def gpus_mapped(**mapping):
    return make_plan(mappings=Mappings(gpus=dict(mapping)))


def gpu_codes(found):
    return {f.code: f for f in found if "GPU" in f.code or f.code.startswith("VM_")}


def test_finding_vm_vgpu_without_a_gpu_mapping():
    """SDD §9.3, §22.4, §22.5 (plan task G4): a vGPU VM no GPU mapping names stays blocked, and the
    message names what to map in the destination provider - a VMware VM's GPU key, an OpenStack
    VM's flavor."""
    found = only(check(vmware_gpu_vm(VGPU_4C), source=VMWARE, dst=gpu_dst(A4C)), "VM_VGPU")
    assert (found.severity, found.message) == (
        Severity.blocker,
        "The VM has a vGPU (grid_a100-4c). The plan's GPU mappings name no flavor for "
        "'grid_a100-4c': map it to a GPU flavor of RHOSO 18.0.",
    )
    found = only(check(openstack_gpu_vm(), dst=gpu_dst(A4C)), "VM_VGPU")
    assert (found.severity, found.message) == (
        Severity.blocker,
        "Flavor requests a vGPU (resources:VGPU). The plan maps no flavor to 'g1.vgpu': map it to "
        "a GPU flavor of RHOSO 18.0.",
    )
    assert "MAP_GPU_FLAVOR" not in gpu_codes(check(openstack_gpu_vm(), dst=gpu_dst(A4C)))


def test_finding_vm_vgpu_whose_mapping_names_a_flavor_without_a_gpu():
    """SDD §22.4, §22.5 (plan task G4): a mapping to a flavor that asks for no GPU does not apply -
    the server would lose its GPU without a word -, so the VM stays blocked and the message names
    the mapping."""
    vmw = check(
        vmware_gpu_vm(VGPU_4C),
        plan=gpus_mapped(**{"grid_a100-4c": "m1.large"}),
        source=VMWARE,
        dst=gpu_dst(A4C),
    )
    assert only(vmw, "VM_VGPU").message == (
        "The VM has a vGPU (grid_a100-4c). Its GPU mapping, 'grid_a100-4c' to 'm1.large', names a "
        "flavor of RHOSO 18.0 that asks for no GPU: the server would lose its GPU."
    )
    osp = check(
        openstack_gpu_vm(),
        plan=make_plan(mappings=Mappings(flavors={"g1.vgpu": "m1.large"})),
        dst=gpu_dst(A4C),
    )
    assert only(osp, "VM_VGPU").message == (
        "Flavor requests a vGPU (resources:VGPU). Its flavor mapping, 'g1.vgpu' to 'm1.large', "
        "names a flavor of RHOSO 18.0 that asks for no GPU: the server would lose its GPU."
    )
    assert "MAP_GPU_FLAVOR" not in gpu_codes(vmw) and "MAP_GPU_FLAVOR" not in gpu_codes(osp)


def test_finding_vm_vgpu_whose_gpus_map_to_different_flavors():
    """SDD §22.4 (plan task G4): a VMware VM's GPUs whose keys map to different flavors - its server
    takes one - keep it blocked, the message naming each mapping."""
    found = check(
        vmware_gpu_vm(VGPU_4C, PASS_ID),
        plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-4c", "10de:20b5": "p1.a100"}),
        source=VMWARE,
        dst=gpu_dst(A4C, P1),
    )
    why = (
        "Its GPUs map to different flavors ('grid_a100-4c' to 'g1.a100-4c', '10de:20b5' to "
        "'p1.a100'): its server is created with one."
    )
    assert only(found, "VM_VGPU").message == f"The VM has a vGPU (grid_a100-4c). {why}"
    assert only(found, "VM_PCI_PASSTHROUGH").message == (
        f"The VM has a PCI passthrough device (10de:20b5). {why}"
    )


def test_finding_vm_vgpu_of_an_openstack_vm_ignores_a_same_named_gpu_flavor():
    """SDD §22.4 (plan task G4): a same-named destination flavor is no mapping for a GPU VM - its
    GPU type may differ -, so the VM without one stays blocked and nothing is matched."""
    vm = openstack_gpu_vm(flavor="g1.a100-4c")
    found = check(vm, dst=gpu_dst(A4C))
    assert only(found, "VM_VGPU").message.endswith(
        "The plan maps no flavor to 'g1.a100-4c': map it to a GPU flavor of RHOSO 18.0."
    )
    assert not {"MAP_FLAVOR_AUTO", "MAP_FLAVOR_MISSING", "MAP_GPU_FLAVOR"} & {f.code for f in found}
    # one a flavor of the destination would fit is matched to none either
    small = openstack_gpu_vm(flavor="g1.a100-4c", vcpus=2, ram_mb=4096)
    assert not {"MAP_FLAVOR_AUTO", "MAP_FLAVOR_MISSING"} & {
        f.code for f in check(small, dst=gpu_dst(A4C))
    }


def test_finding_map_flavor_missing_for_a_gpu_vms_mapped_flavor():
    """SDD §9.3, §22.4 (plan task G4): a GPU VM's mapping to a flavor the destination lacks is
    MAP_FLAVOR_MISSING's, as for any mapping target - no VM_VGPU beside it."""
    vmw = check(
        vmware_gpu_vm(VGPU_4C),
        plan=gpus_mapped(**{"grid_a100-4c": "g1.nope"}),
        source=VMWARE,
        dst=gpu_dst(A4C),
    )
    assert only(vmw, "MAP_FLAVOR_MISSING").message == (
        "The plan maps 'grid_a100-4c' to 'g1.nope', which does not exist in RHOSO 18.0."
    )
    osp = check(
        openstack_gpu_vm(),
        plan=make_plan(mappings=Mappings(flavors={"g1.vgpu": "g1.nope"})),
        dst=gpu_dst(A4C),
    )
    assert only(osp, "MAP_FLAVOR_MISSING").message == (
        "Flavor 'g1.vgpu' is mapped to 'g1.nope', which does not exist in RHOSO 18.0."
    )
    assert "VM_VGPU" not in gpu_codes(vmw) and "VM_VGPU" not in gpu_codes(osp)


def test_finding_vm_pci_passthrough_without_a_gpu_mapping():
    """SDD §22.4, §22.5 (plan task G4): a passthrough device no GPU mapping names keeps the VM
    blocked - a VMware device by its PCI ids, an OpenStack PCI alias by its flavor."""
    found = check(vmware_gpu_vm(PASS_ID), source=VMWARE, dst=gpu_dst(P1))
    assert only(found, "VM_PCI_PASSTHROUGH").message == (
        "The VM has a PCI passthrough device (10de:20b5). The plan's GPU mappings name no flavor "
        "for '10de:20b5': map it to a GPU flavor of RHOSO 18.0."
    )
    vm = openstack_gpu_vm(flavor="p1.src", specs={"pci_passthrough:alias": "a100:1"})
    assert only(check(vm, dst=gpu_dst(P1)), "VM_PCI_PASSTHROUGH").message == (
        "Flavor requests PCI passthrough (a100:1). The plan maps no flavor to 'p1.src': map it to "
        "a GPU flavor of RHOSO 18.0."
    )


def test_finding_vm_pci_passthrough_of_a_vmware_device_without_ids():
    """SDD §22.2, §22.4 (plan task G4): a VMware device without PCI ids has no GPU key, so no
    mapping can name it, whatever the plan maps."""
    found = check(
        vmware_gpu_vm(GpuDevice(kind="passthrough")),
        plan=gpus_mapped(**{"10de:20b5": "p1.a100"}),
        source=VMWARE,
        dst=gpu_dst(P1),
    )
    assert only(found, "VM_PCI_PASSTHROUGH").message == (
        "The VM has a PCI passthrough device (a device without ids). No mapping can name its "
        "device without ids, so none applies to the VM."
    )


def test_finding_map_gpu_flavor():
    """SDD §22.4, §22.5 (plan task G4): a GPU mapping that applies lifts the block; the info names
    the VM's GPUs, the GPU flavor with its size and GPU requests, and the Technology Preview."""
    found = check(
        vmware_gpu_vm(VGPU_4C),
        plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"}),
        source=VMWARE,
        dst=gpu_dst(A4C),
    )
    assert not {"VM_VGPU", "VM_PCI_PASSTHROUGH"} & set(gpu_codes(found))
    info = only(found, "MAP_GPU_FLAVOR")
    assert (info.severity, info.message) == (
        Severity.info,
        "The VM's GPUs (grid_a100-4c) go to flavor 'g1.a100-4c' of RHOSO 18.0: 8 vCPUs, 32768 MB "
        "RAM, 80 GB disk, vGPU VGPU CUSTOM_NVIDIA_A100_4C (A100-4C). GPU migration is a "
        "Technology Preview.",
    )
    # an OpenStack VM's flavor mapping is its GPU mapping
    osp = check(
        openstack_gpu_vm(),
        plan=make_plan(mappings=Mappings(flavors={"g1.vgpu": "g1.a100-4c"})),
        dst=gpu_dst(A4C),
    )
    assert "VM_VGPU" not in gpu_codes(osp)
    assert only(osp, "MAP_GPU_FLAVOR").message.startswith(
        "The VM's GPUs (VGPU) go to flavor 'g1.a100-4c' of RHOSO 18.0:"
    )


def test_finding_map_gpu_flavor_warns_when_the_flavor_is_smaller_than_the_vm():
    """SDD §22.4 (plan task G4): the GPU flavor decides the server's size; one with fewer vCPUs,
    less RAM or a smaller local root disk than the VM makes the info a warning, naming each."""
    vm = vmware_gpu_vm(
        VGPU_4C,
        vcpus=16,
        ram_mb=65536,
        disks=[make_disk(kind="image_root", size_gb=100, volume_type=None)],
    )
    found = only(
        check(
            vm, plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"}), source=VMWARE, dst=gpu_dst(A4C)
        ),
        "MAP_GPU_FLAVOR",
    )
    assert found.severity == Severity.warning
    assert found.message == (
        "The VM's GPUs (grid_a100-4c) go to flavor 'g1.a100-4c' of RHOSO 18.0: 8 vCPUs, 32768 MB "
        "RAM, 80 GB disk, vGPU VGPU CUSTOM_NVIDIA_A100_4C (A100-4C); it gives the VM 8 vCPUs for "
        "its 16, 32768 MB RAM for its 65536 and 80 GB of root disk for its 100. GPU migration is a "
        "Technology Preview."
    )
    assert found.remediation != CATALOG["MAP_GPU_FLAVOR"].remediation


def test_finding_map_gpu_downsized_fewer_gpus():
    """SDD §22.5 (plan task G4): a GPU flavor that gives the VM fewer GPUs than it has."""
    found = check(
        vmware_gpu_vm(GpuDevice(kind="vgpu", profile="grid_a100-4c", count=2)),
        plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"}),
        source=VMWARE,
        dst=gpu_dst(A4C),
    )
    warning = only(found, "MAP_GPU_DOWNSIZED")
    assert (warning.severity, warning.message) == (
        Severity.warning,
        "Flavor 'g1.a100-4c' of RHOSO 18.0 gives the VM less GPU than it has: 1 GPU for its 2.",
    )
    assert "MAP_GPU_FLAVOR" in gpu_codes(found)


def test_finding_map_gpu_downsized_smaller_framebuffer_or_fewer_mig_slices():
    """SDD §22.2, §22.5 (plan task G4): when the VM's GPU and the flavor's both have a profile with
    a size - VMware's own, else the one the provider declares -, a smaller framebuffer, or fewer MIG
    slices where both are MIG-backed, is less GPU."""
    smaller = check(
        vmware_gpu_vm(VGPU_4C),
        plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-2c"}),
        source=VMWARE,
        dst=gpu_dst(A2C),
    )
    assert only(smaller, "MAP_GPU_DOWNSIZED").message == (
        "Flavor 'g1.a100-2c' of RHOSO 18.0 gives the VM less GPU than it has: a 2 GB framebuffer "
        "for its 4 GB (A100-2C for grid_a100-4c)."
    )
    fewer = check(
        vmware_gpu_vm(GpuDevice(kind="vgpu", profile="grid_a100-4-20c")),
        plan=gpus_mapped(**{"grid_a100-4-20c": "g1.a100-3-20c"}),
        source=VMWARE,
        dst=gpu_dst(MIG),
    )
    assert only(fewer, "MAP_GPU_DOWNSIZED").message == (
        "Flavor 'g1.a100-3-20c' of RHOSO 18.0 gives the VM less GPU than it has: 3 MIG slices for "
        "its 4 (A100-3-20C for grid_a100-4-20c)."
    )
    # the GPUs are paired the largest first: the VM's 8 GB vGPU meets the flavor's one 4 GB vGPU
    listed = check(
        vmware_gpu_vm(VGPU_4C, GpuDevice(kind="vgpu", profile="grid_a100-8c")),
        plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-4c", "grid_a100-8c": "g1.a100-4c"}),
        source=VMWARE,
        dst=gpu_dst(A4C),
    )
    assert only(listed, "MAP_GPU_DOWNSIZED").message == (
        "Flavor 'g1.a100-4c' of RHOSO 18.0 gives the VM less GPU than it has: 1 GPU for its 2; a 4 "
        "GB framebuffer for its 8 GB (A100-4C for grid_a100-8c)."
    )
    # a MIG slice count is compared only where both are MIG-backed: a time-sliced 20 GB vGPU for a
    # MIG-backed 20 GB one is no downsizing
    sliced = ("g1.a100-20c", 16, 65536, 80, ON_A100, "A100-20C")
    time_sliced = check(
        vmware_gpu_vm(GpuDevice(kind="vgpu", profile="grid_a100-4-20c")),
        plan=gpus_mapped(**{"grid_a100-4-20c": "g1.a100-20c"}),
        source=VMWARE,
        dst=gpu_dst(sliced),
    )
    assert "MAP_GPU_DOWNSIZED" not in gpu_codes(time_sliced)
    # the same size, or more, is no downsizing
    same = check(
        vmware_gpu_vm(VGPU_4C),
        plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"}),
        source=VMWARE,
        dst=gpu_dst(A4C),
    )
    assert "MAP_GPU_DOWNSIZED" not in gpu_codes(same)


def test_finding_map_gpu_downsized_compares_nothing_without_a_profile_size():
    """SDD §22.2, §22.3 (plan task G4): without a declared profile, or with a name that is no NVIDIA
    profile (an mdev type), nothing is compared: no MAP_GPU_DOWNSIZED is a guess."""
    for flavor in (PLAIN, NOSIZE):
        found = check(
            vmware_gpu_vm(GpuDevice(kind="vgpu", profile="grid_a100-8c")),
            plan=gpus_mapped(**{"grid_a100-8c": flavor[0]}),
            source=VMWARE,
            dst=gpu_dst(flavor),
        )
        assert "MAP_GPU_DOWNSIZED" not in gpu_codes(found), flavor[0]
        assert "MAP_GPU_FLAVOR" in gpu_codes(found)


def _two_vmware_gpu_vms(**kw):
    first = vmware_gpu_vm(VGPU_4C, source_id="vm-1", name="ml-01", **kw)
    second = vmware_gpu_vm(VGPU_4C, source_id="vm-2", name="ml-02", **kw)
    return first, second


def test_finding_dst_gpu_capacity_insufficient_cumulative_per_pool():
    """SDD §22.5 (plan task G4): the plan's VMs together ask a pool for more units than it has free
    - each VM's finding names the pool, the units asked for and free, and the VMs."""
    first, second = _two_vmware_gpu_vms()
    plan = gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"})
    tight = [GpuPool("VGPU", [A100], [], free=1, providers=1, largest_free=1), OPEN[2]]
    for vm in (first, second):
        found = check(
            vm, plan=plan, all_vms=[first, second], source=VMWARE, dst=gpu_dst(A4C, pools=tight)
        )
        blocker = only(found, "DST_GPU_CAPACITY_INSUFFICIENT")
        assert (blocker.severity, blocker.message) == (
            Severity.blocker,
            "The plan's VMs ask VGPU CUSTOM_NVIDIA_A100_4C of RHOSO 18.0 for 2 units, more than "
            "its 1 free (ml-01, ml-02).",
        )
    roomy = [GpuPool("VGPU", [A100], [], free=2, providers=1, largest_free=2), OPEN[2]]
    found = check(
        first, plan=plan, all_vms=[first, second], source=VMWARE, dst=gpu_dst(A4C, pools=roomy)
    )
    assert "DST_GPU_CAPACITY_INSUFFICIENT" not in gpu_codes(found)


def test_finding_dst_gpu_capacity_insufficient_per_resource_class_across_pools():
    """SDD §22.5 (plan task G4): pools of one class can share providers - a provider with both
    traits counts in both -, so the class alone, which unites them, holds the plan's VMs of every
    pool of that class too."""
    a100 = vmware_gpu_vm(VGPU_4C, source_id="vm-1", name="ml-01")
    l40 = vmware_gpu_vm(
        GpuDevice(kind="vgpu", profile="grid_l40-4q"), source_id="vm-2", name="ml-02"
    )
    plan = gpus_mapped(**{"grid_a100-4c": "g1.a100-4c", "grid_l40-4q": "g1.l40-4q"})
    shared = [
        GpuPool("VGPU", [A100], [], free=1, providers=1, largest_free=1),
        GpuPool("VGPU", [L40], [], free=1, providers=1, largest_free=1),
        GpuPool("VGPU", [], [], free=1, providers=1, largest_free=1),
    ]
    found = check(
        a100, plan=plan, all_vms=[a100, l40], source=VMWARE, dst=gpu_dst(A4C, L4Q, pools=shared)
    )
    assert only(found, "DST_GPU_CAPACITY_INSUFFICIENT").message == (
        "The plan's VMs ask VGPU of RHOSO 18.0 for 2 units across its pools, more than its 1 free "
        "(ml-01, ml-02)."
    )
    # a traitless request of another class is that class's pool, never this one's (G4 review)
    specs = {"resources1:VGPU": "1", f"trait1:{A100}": "required", "resources2:CUSTOM_MDEV_X": "1"}
    two = ("g1.two-classes", 8, 32768, 80, specs, "A100-4C")
    first, second = _two_vmware_gpu_vms()
    pools = [
        GpuPool("VGPU", [A100], [], same_provider=True, free=2, providers=1, largest_free=2),
        GpuPool("VGPU", [], [], free=1, providers=1, largest_free=1),
        GpuPool("CUSTOM_MDEV_X", [], [], free=4, providers=1, largest_free=4),
    ]
    found = check(
        first,
        plan=gpus_mapped(**{"grid_a100-4c": "g1.two-classes"}),
        all_vms=[first, second],
        source=VMWARE,
        dst=gpu_dst(two, pools=pools),
    )
    assert only(found, "DST_GPU_CAPACITY_INSUFFICIENT").message == (
        "The plan's VMs ask VGPU of RHOSO 18.0 for 2 units across its pools, more than its 1 free "
        "(ml-01, ml-02)."
    )


def test_finding_dst_gpu_capacity_insufficient_counts_what_each_cutover_still_creates():
    """SDD §9.3, §22.5 (plan task G4): as for the quota, a VM counts what its cutover will still
    create - its server's units until the server exists, none once it does."""
    first, second = _two_vmware_gpu_vms()
    plan = gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"})
    tight = [GpuPool("VGPU", [A100], [], free=1, providers=1, largest_free=1), OPEN[2]]

    def codes(shares):
        return gpu_codes(
            run_preflight(
                first,
                plan,
                src_inv(),
                gpu_dst(A4C, pools=tight),
                [first, second],
                source=VMWARE,
                destination=DST,
                shares=shares,
            )
        )

    assert "DST_GPU_CAPACITY_INSUFFICIENT" in codes({})
    assert "DST_GPU_CAPACITY_INSUFFICIENT" in codes({"vm-2": quota_share(Phase.syncing)})
    assert "DST_GPU_CAPACITY_INSUFFICIENT" not in codes({"vm-2": quota_share(Phase.verifying)})
    # a VM whose server exists asks for nothing more, whatever the others ask: vm-2 alone is more
    # than an empty pool holds, and only vm-2 is told
    tight[0] = GpuPool("VGPU", [A100], [], free=0, providers=0, largest_free=0)
    assert "DST_GPU_CAPACITY_INSUFFICIENT" not in codes({"vm-1": quota_share(Phase.verifying)})


def test_finding_dst_gpu_capacity_insufficient_counts_a_vm_a_mapping_gives_a_gpu():
    """SDD §9.3, §22.5 (plan task G4 review): the capacity counts every VM of the plan whose
    server is created with a vGPU flavor - a VM without a GPU that a flavor mapping sends to one
    included."""
    gpu = openstack_gpu_vm(source_id="vm-1", name="ml-01")
    plain = make_vm(source_id="vm-2", name="app-01", flavor="m1.small")
    plan = make_plan(mappings=Mappings(flavors={"g1.vgpu": "g1.a100-4c", "m1.small": "g1.a100-4c"}))
    tight = [GpuPool("VGPU", [A100], [], free=1, providers=1, largest_free=1), OPEN[2]]
    for vm in (gpu, plain):
        found = check(vm, plan=plan, all_vms=[gpu, plain], dst=gpu_dst(A4C, pools=tight))
        assert only(found, "DST_GPU_CAPACITY_INSUFFICIENT").message == (
            "The plan's VMs ask VGPU CUSTOM_NVIDIA_A100_4C of RHOSO 18.0 for 2 units, more than "
            "its 1 free (app-01, ml-01)."
        ), vm.name


def test_finding_dst_gpu_capacity_insufficient_names_the_first_ten_vms():
    """SDD §22.5 (plan task G4 review): a finding names the VMs that ask the pool - the first ten,
    then how many more -, so a plan of hundreds of GPU VMs stores no message of kilobytes."""
    vms = [vmware_gpu_vm(VGPU_4C, source_id=f"vm-{i:02d}", name=f"ml-{i:02d}") for i in range(12)]
    tight = [GpuPool("VGPU", [A100], [], free=1, providers=1, largest_free=1), OPEN[2]]
    found = check(
        vms[0],
        plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"}),
        all_vms=vms,
        source=VMWARE,
        dst=gpu_dst(A4C, pools=tight),
    )
    names = ", ".join(f"ml-{i:02d}" for i in range(10))
    assert only(found, "DST_GPU_CAPACITY_INSUFFICIENT").message == (
        "The plan's VMs ask VGPU CUSTOM_NVIDIA_A100_4C of RHOSO 18.0 for 12 units, more than its "
        f"1 free ({names} and 2 more)."
    )


def test_finding_dst_gpu_capacity_insufficient_for_a_group_no_provider_can_hold():
    """SDD §22.3, §22.5 (plan task G4): Nova fills a request group from one provider, so a group of
    n > 1 units needs a provider with n free, whatever the pool holds in all."""
    vm = vmware_gpu_vm(GpuDevice(kind="vgpu", profile="grid_a100-4c", count=2))
    spread = [GpuPool("VGPU", [A100], [], free=4, providers=4, largest_free=1), OPEN[2]]
    found = check(
        vm,
        plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-4c-x2"}),
        source=VMWARE,
        dst=gpu_dst(A4C2, pools=spread),
    )
    assert only(found, "DST_GPU_CAPACITY_INSUFFICIENT").message == (
        "Its flavor asks VGPU CUSTOM_NVIDIA_A100_4C of RHOSO 18.0 for 2 units on one provider, and "
        "none has 2 free (the most on one is 1)."
    )
    # a group of one unit is the pool's own sentence alone, its unit in the singular (§22.5: n > 1)
    full = [GpuPool("VGPU", [A100], [], free=0, providers=0, largest_free=0), OPEN[2]]
    found = check(
        vmware_gpu_vm(VGPU_4C),
        plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"}),
        source=VMWARE,
        dst=gpu_dst(A4C, pools=full),
    )
    assert only(found, "DST_GPU_CAPACITY_INSUFFICIENT").message == (
        "The plan's VMs ask VGPU CUSTOM_NVIDIA_A100_4C of RHOSO 18.0 for 1 unit, more than its 0 "
        "free (web-01)."
    )


def test_finding_dst_gpu_capacity_insufficient_where_placement_refuses_the_pool():
    """SDD §22.3, §22.5 (PR #62 review): a pool whose request Placement refuses as a bad one
    (400) - a trait it does not know - counts nothing, and Nova's request for that flavor is
    refused alike."""
    refused = [
        GpuPool("VGPU", [A100], [], detail="HTTP 400 placement.undefined_code"),
        OPEN[2],
    ]
    found = check(
        vmware_gpu_vm(VGPU_4C),
        plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"}),
        source=VMWARE,
        dst=gpu_dst(A4C, pools=refused),
    )
    assert only(found, "DST_GPU_CAPACITY_INSUFFICIENT").message == (
        "Placement refuses the request for VGPU CUSTOM_NVIDIA_A100_4C of RHOSO 18.0 (HTTP 400 "
        "placement.undefined_code): no server is created with flavor 'g1.a100-4c'."
    )


def test_finding_dst_gpu_capacity_unknown_when_placement_cannot_be_read():
    """SDD §22.3, §22.5 (plan task G4): a capacity that cannot be read warns, by its reason - the
    credential's roles, no Placement, the failure -, and nothing is guessed: no shortfall."""
    vm = vmware_gpu_vm(VGPU_4C)
    plan = gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"})
    for reason, detail, why in (
        (
            "forbidden",
            "HTTP 403 placement.undefined_code",
            "Placement refuses its credential (HTTP 403 placement.undefined_code): its default "
            "policy lets only the admin role read it",
        ),
        ("no_placement", None, "its catalog has no Placement"),
        ("error", "HTTP 503", "the read failed (HTTP 503)"),
    ):
        unknown = gpu_dst(A4C, pools=[], status="unknown", reason=reason, detail=detail)
        found = check(vm, plan=plan, source=VMWARE, dst=unknown)
        warning = only(found, "DST_GPU_CAPACITY_UNKNOWN")
        assert (warning.severity, warning.message) == (
            Severity.warning,
            f"The GPU capacity of RHOSO 18.0 cannot be read: {why}.",
        )
        assert "DST_GPU_CAPACITY_INSUFFICIENT" not in gpu_codes(found)
    # a read that holds no pool for a request guesses nothing for it (G4 review)
    found = check(vm, plan=plan, source=VMWARE, dst=gpu_dst(A4C, pools=[OPEN[2]]))
    assert only(found, "DST_GPU_CAPACITY_UNKNOWN").message == (
        "The GPU capacity read of RHOSO 18.0 holds no pool VGPU CUSTOM_NVIDIA_A100_4C: nothing is "
        "guessed for it."
    )
    assert "DST_GPU_CAPACITY_INSUFFICIENT" not in gpu_codes(found)


def test_finding_dst_gpu_capacity_unknown_for_a_pci_alias():
    """SDD §22.3, §22.5 (plan task G4): a GPU flavor that asks for a PCI alias has no pool - the
    device class its alias asks for lives in nova.conf -, so its capacity is unknown."""
    found = check(
        vmware_gpu_vm(PASS_ID),
        plan=gpus_mapped(**{"10de:20b5": "p1.a100"}),
        source=VMWARE,
        dst=gpu_dst(P1),
    )
    assert only(found, "DST_GPU_CAPACITY_UNKNOWN").message == (
        "Flavor 'p1.a100' of RHOSO 18.0 asks for PCI alias a100, whose devices nova.conf "
        "configures: Placement does not show their capacity."
    )


def test_gpu_shortfalls_name_the_pool_and_the_units_needed_and_free_in_the_preflights_order():
    """SDD §22.5, §22.7 (plan task G6): the room rule the pre-flight and the cutover gate share,
    each shortfall with the pool and the units needed and free that the gate's wait names - a
    pool Placement refuses, a pool asked for more than its free, its class across its pools, then
    a request group no provider holds; a pool the read does not hold guesses nothing."""
    from seamless_migrate.planning.gpu import gpu_units
    from seamless_migrate.planning.preflight import gpu_shortfalls

    a100 = GpuDevice(kind="vgpu", resource_class="VGPU", traits=[A100], same_provider=True, count=2)
    l40 = GpuDevice(kind="vgpu", resource_class="VGPU", traits=[L40], same_provider=True)
    alias = GpuDevice(kind="passthrough", alias="a100")
    asked = gpu_units([a100, l40, alias])
    assert asked == {
        ("VGPU", (A100,), (), True): 2,
        ("VGPU", (), (), False): 3,
        ("VGPU", (L40,), (), True): 1,
    }
    refused = "HTTP 400 placement.undefined_code"
    pools = [
        GpuPool("VGPU", [A100], [], same_provider=True, free=2, providers=2, largest_free=1),
        GpuPool("VGPU", [L40], [], same_provider=True, detail=refused),
        GpuPool("VGPU", [], [], free=2, providers=2, largest_free=1),
    ]
    assert [
        (s.kind, s.pool, s.needed, s.free, s.detail)
        for s in gpu_shortfalls([a100, l40, alias], asked, pools)
    ] == [
        ("refused", f"VGPU {L40}", 1, 0, refused),
        ("class", "VGPU", 3, 2, None),
        ("group", f"VGPU {A100}", 2, 1, None),
    ]
    # a request without traits asks its class's pool, once: that check is its own, never "class"
    bare = GpuDevice(kind="vgpu", resource_class="VGPU")
    assert gpu_units([bare]) == {("VGPU", (), (), False): 1}
    assert gpu_shortfalls([bare], {("VGPU", (), (), False): 2}, pools[2:]) == []
    short = gpu_shortfalls([bare], {("VGPU", (), (), False): 3}, pools[2:])
    assert [(s.kind, s.pool, s.needed, s.free) for s in short] == [("pool", "VGPU", 3, 2)]
    assert gpu_shortfalls([a100], gpu_units([a100]), []) == []


def test_finding_guest_gpu_driver():
    """SDD §22.5 (plan task G4): every GPU VM, whatever its strategy and whether or not a mapping
    applies yet: the guest's NVIDIA driver must suit the destination; virt-v2v leaves a VMware
    guest's driver as it is."""
    mapped = gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"})
    vmw = only(
        check(vmware_gpu_vm(VGPU_4C), plan=mapped, source=VMWARE, dst=gpu_dst(A4C)),
        "GUEST_GPU_DRIVER",
    )
    assert (vmw.severity, vmw.message) == (
        Severity.warning,
        "The guest's NVIDIA driver must suit RHOSO 18.0: a vGPU guest driver of a release its "
        "hosts' vGPU manager supports (NVIDIA's compatibility table). virt-v2v neither installs "
        "nor updates the driver.",
    )
    osp = only(check(openstack_gpu_vm(), dst=gpu_dst(A4C)), "GUEST_GPU_DRIVER")
    assert osp.message == (
        "The guest's NVIDIA driver must suit RHOSO 18.0: a vGPU guest driver of a release its "
        "hosts' vGPU manager supports (NVIDIA's compatibility table)."
    )
    passthrough = only(
        check(vmware_gpu_vm(PASS_ID), source=VMWARE, dst=gpu_dst(P1)), "GUEST_GPU_DRIVER"
    )
    assert passthrough.message.startswith(
        "The guest's NVIDIA driver must suit RHOSO 18.0: the data center driver for its "
        "passed-through NVIDIA GPU."
    )
    # a device of another vendor, and an OpenStack PCI alias, are a PCI device: Seamless cannot
    # tell an NVIDIA GPU (§22.1, G4 review)
    amd = GpuDevice(kind="passthrough", vendor_id="1002", device_id="740f")
    for found in (
        check(vmware_gpu_vm(amd), source=VMWARE, dst=gpu_dst(P1)),
        check(
            openstack_gpu_vm(flavor="p1.src", specs={"pci_passthrough:alias": "fpga:1"}),
            plan=make_plan(mappings=Mappings(flavors={"p1.src": "p1.a100"})),
            dst=gpu_dst(P1),
        ),
    ):
        assert only(found, "GUEST_GPU_DRIVER").message.startswith(
            "The guest's driver must suit RHOSO 18.0: the driver of its passed-through PCI device."
        )


def test_finding_guest_gpu_driver_names_a_change_of_kind():
    """SDD §22.4, §22.5 (plan task G4): a mapping may change the kind - a passed-through GPU to a
    vGPU, or back - or the GPU model; the guest then needs a new driver, and the message says so."""
    kind = only(
        check(
            vmware_gpu_vm(PASS_ID),
            plan=gpus_mapped(**{"10de:20b5": "g1.a100-4c"}),
            source=VMWARE,
            dst=gpu_dst(A4C),
        ),
        "GUEST_GPU_DRIVER",
    )
    assert kind.message == (
        "The guest's NVIDIA driver must suit RHOSO 18.0: a vGPU guest driver of a release its "
        "hosts' vGPU manager supports (NVIDIA's compatibility table). The mapping changes the GPU "
        "from a passed-through NVIDIA GPU to a vGPU, so the guest needs a new driver. virt-v2v "
        "neither installs nor updates the driver."
    )
    model = only(
        check(
            vmware_gpu_vm(VGPU_4C),
            plan=gpus_mapped(**{"grid_a100-4c": "g1.l40-4q"}),
            source=VMWARE,
            dst=gpu_dst(L4Q),
        ),
        "GUEST_GPU_DRIVER",
    )
    assert (
        "The mapping changes the GPU model from A100 to L40, so the guest needs a new driver."
        in (model.message)
    )


def test_finding_guest_gpu_license():
    """SDD §22.5 (plan task G4): a vGPU is licensed from the NVIDIA License System, whose license
    server the guest must reach from its destination network; a passed-through GPU is not."""
    found = only(
        check(
            vmware_gpu_vm(VGPU_4C),
            plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"}),
            source=VMWARE,
            dst=gpu_dst(A4C),
        ),
        "GUEST_GPU_LICENSE",
    )
    assert (found.severity, found.message) == (
        Severity.info,
        "The guest licenses its vGPU from the NVIDIA License System: its license server, a CLS "
        "instance or an on-premises DLS instance, must be reachable from its network in RHOSO "
        "18.0, or the guest runs its GPU unlicensed.",
    )
    passthrough = check(
        vmware_gpu_vm(PASS_ID),
        plan=gpus_mapped(**{"10de:20b5": "p1.a100"}),
        source=VMWARE,
        dst=gpu_dst(P1),
    )
    assert "GUEST_GPU_LICENSE" not in gpu_codes(passthrough)
    # the VM's GPU is a vGPU: its guest holds a license, whatever the flavor gives it
    vgpu_to_pci = check(
        vmware_gpu_vm(VGPU_4C),
        plan=gpus_mapped(**{"grid_a100-4c": "p1.a100"}),
        source=VMWARE,
        dst=gpu_dst(P1),
    )
    assert "GUEST_GPU_LICENSE" in gpu_codes(vgpu_to_pci)


def test_finding_guest_gpu_license_for_a_vgpu_handed_out_as_a_pci_device():
    """SDD §22.3, §22.5 (plan task G4): a destination whose vGPU uses the vendor-specific VFIO
    framework hands it out as a PCI device: the alias entry's declared profile makes it a vGPU."""
    found = check(
        vmware_gpu_vm(PASS_ID),
        plan=gpus_mapped(**{"10de:20b5": "p1.l4-vgpu"}),
        source=VMWARE,
        dst=gpu_dst(PVF),
    )
    assert only(found, "GUEST_GPU_LICENSE").severity == Severity.info


def test_a_gpu_vm_is_never_matched_to_a_flavor_by_size_or_name():
    """SDD §22.4 (plan task G4): a GPU VM's flavor is named by its plan and never matched - by size
    or by a same-named destination flavor -: nothing resolves for it, and without its mapping its
    server has no flavor."""
    dst = gpu_dst(A4C)
    plan = make_plan()
    # small enough for m1.small, which would be matched to a VM without a GPU
    for vm in (
        openstack_gpu_vm(flavor="g1.a100-4c", vcpus=2, ram_mb=4096),
        openstack_gpu_vm(flavor="g1.src", vcpus=2, ram_mb=4096),
    ):
        assert resolve_mappings(vm, plan, dst) == Mappings(), vm.flavor
        assert destination_flavor(vm, plan, dst) is None, vm.flavor
    assert resolve_mappings(make_vm(flavor="g1.src"), plan, dst).flavors == {"g1.src": "m1.small"}
    # a VM that only the GPU flavor fits is matched to none: a GPU flavor is never matched
    assert fitting_flavor(make_vm(vcpus=8, ram_mb=32768), dst) is None
    vmw = vmware_gpu_vm(VGPU_4C)
    assert destination_flavor(vmw, plan, dst, ProviderKind.vmware) is None
    mapped = gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"})
    assert destination_flavor(vmw, mapped, dst, ProviderKind.vmware)["name"] == "g1.a100-4c"


def test_flavor_pre_staging_never_creates_a_gpu_vms_flavor():
    """SDD §7.2, §22.4 (plan task G4): pre-staging would create the source flavor with its extra
    specs, whose GPU type may not exist at the destination: a GPU VM's flavor is never pre-staged,
    and the VM stays blocked until its plan maps it."""
    plan = make_plan(prestage_resources=[*DEFAULT_PRESTAGE, "flavors"])
    vm = openstack_gpu_vm(flavor="g1.src-vgpu")
    assert not flavor_prestaged(vm, plan, gpu_dst(A4C))
    found = check(vm, plan=plan, dst=gpu_dst(A4C))
    assert "MAP_FLAVOR_PRESTAGED" not in [f.code for f in found]
    assert "VM_VGPU" in gpu_codes(found)
    # a VM without a GPU is pre-staged as before
    assert flavor_prestaged(make_vm(flavor="m1.src"), plan, gpu_dst(A4C))


def test_the_quota_counts_a_gpu_vm_by_its_gpu_flavor():
    """SDD §9.3, §22.4 (plan task G4): a GPU VM's server is created with its GPU flavor, so the
    quota counts its cores and RAM by it - a VMware VM's too, as the kit no longer sizes it."""
    vm = vmware_gpu_vm(VGPU_4C, vcpus=2, ram_mb=4096)
    plan = gpus_mapped(**{"grid_a100-4c": "g1.a100-4c"})
    demand = resource_demand(vm, plan, gpu_dst(A4C), ProviderKind.vmware)
    assert (demand["cores"], demand["ram_mb"]) == (8, 32768)
    small = {"cores": 4, "ram_mb": 1_000_000, "instances": 50, "volumes": 50, "gigabytes": 10_000}
    found = check(vm, plan=plan, source=VMWARE, dst=gpu_dst(A4C, quotas={"finance": small}))
    assert "cores 8 > 4 free" in only(found, "DST_QUOTA_INSUFFICIENT").message
    # the source's kind decides, not the VM's record: a VMware VM recorded with a flavor name
    named = vmware_gpu_vm(VGPU_4C, vcpus=2, ram_mb=4096, flavor="m1.small")
    found = check(named, plan=plan, source=VMWARE, dst=gpu_dst(A4C, quotas={"finance": small}))
    assert "cores 8 > 4 free" in only(found, "DST_QUOTA_INSUFFICIENT").message


def test_gpu_findings_name_the_destination_provider():
    """SDD §1 naming, §9.3 (plan task G4): every GPU finding about the destination cloud names the
    destination provider, never "the destination"; its remediation, fixed by the catalog, says
    "the destination"."""
    dc3 = make_provider(
        id="dst-dc3", name="DC3 Jakarta", kind=ProviderKind.openstack, role=ProviderRole.destination
    )
    two = GpuDevice(kind="vgpu", profile="grid_a100-4c", count=2)
    found = [
        *check(vmware_gpu_vm(VGPU_4C), source=VMWARE, destination=dc3, dst=gpu_dst(A4C)),
        *check(
            vmware_gpu_vm(two, vcpus=16),
            plan=gpus_mapped(**{"grid_a100-4c": "g1.a100-2c"}),
            source=VMWARE,
            destination=dc3,
            dst=gpu_dst(A2C, pools=[GpuPool("VGPU", [A100], [], free=0), OPEN[2]]),
        ),
        *check(
            vmware_gpu_vm(PASS_ID),
            plan=gpus_mapped(**{"10de:20b5": "p1.a100"}),
            source=VMWARE,
            destination=dc3,
            dst=gpu_dst(P1, pools=[], status="unknown", reason="no_placement"),
        ),
    ]
    gpu = [f for f in found if f.code in {*GPU_CODES, "VM_VGPU", "VM_PCI_PASSTHROUGH"}]
    assert {f.code for f in gpu} >= GPU_CODES
    for f in gpu:
        assert "DC3 Jakarta" in f.message and "the destination" not in f.message, f.code
        assert "DC3 Jakarta" not in (f.remediation or ""), f.code


GPU_CODES = {
    "MAP_GPU_FLAVOR",
    "MAP_GPU_DOWNSIZED",
    "DST_GPU_CAPACITY_INSUFFICIENT",
    "DST_GPU_CAPACITY_UNKNOWN",
    "GUEST_GPU_DRIVER",
    "GUEST_GPU_LICENSE",
}


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
    # plan task N2b
    "SRC_PORT_ADDRESS_PAIRS",
    "MAP_FIP_ADDRESS_CHANGE",
    "DST_DNS_INTEGRATION_MISSING",
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
    # a port's address pairs, a floating IP and a DNS name the destination does not carry
    # (plan task N2b)
    ported = make_vm(
        nics=[
            Nic(
                network="app-net",
                allowed_address_pairs=["10.0.0.100"],
                floating_ips=["203.0.113.10"],
                dns_name="web-01",
                dns_domain="example.com.",
            )
        ]
    )
    found += check(ported, dst=dst_inv(dns=DnsRead(extension=False)), destination=destination)
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
    `source_kind` the source provider's kind and `gpu_profiles` its declared profiles; a network
    case's `destination` gives the destination's label, networks, subnets, the token's roles and
    the policy profile, a GPU case's its flavors, the profiles it declares and its GPU capacity,
    and `phases` the phase of the plan's migration of a VM that has one, by which its share is
    counted (quota_share, §9.3, §22.5). A code a case expects as null is not found (PR #64 review);
    `severities` and `remediations` give a finding's own where they are not its catalog's (a
    MAP_GPU_FLAVOR warning, plan task G12). A Heat or Octavia case gives the source's label and its
    inventory's reads, and the plan its acknowledgements (plan task N2); a network case's
    `destination` its detached floating IPs, its DNS and whether it announces tenant networks, and
    the plan its waves (plan task N2b)."""
    plan = make_plan(**case["plan"])
    vm = VMRef.model_validate(case["vm"])
    others = [VMRef.model_validate(other) for other in case.get("others", [])]
    source = VMWARE if case.get("source_kind") == "vmware" else SRC
    if "gpu_profiles" in case:
        source = source.model_copy(update={"gpu_profiles": case["gpu_profiles"]})
    if "source_label" in case:
        source = source.model_copy(update={"name": case["source_label"]})
    shares = {vm_id: quota_share(Phase(phase)) for vm_id, phase in case.get("phases", {}).items()}
    found = check(
        vm,
        plan=plan,
        src=_source_inventory(case),
        all_vms=[vm, *others],
        source=source,
        shares=shares,
        **_destination(case),
    )
    messages = {f.code: f.message for f in found}
    assert {code: messages.get(code) for code in case["expected"]} == case["expected"]
    by_code = {f.code: f for f in found}
    severities = case.get("severities", {})
    assert {code: by_code[code].severity.value for code in severities} == severities
    remediations = case.get("remediations", {})
    assert {code: by_code[code].remediation for code in remediations} == remediations


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


def _source_inventory(case):
    """check()'s source inventory for a Heat or Octavia case: its stacks and pool members as the
    source's inventory reads them (SDD §10, plan task N2); the default one for any other case."""
    reads = case.get("source_inventory")
    if reads is None:
        return None
    stacks = reads.get("stacks", {})
    members = reads.get("lb_members", {})
    return src_inv(
        stacks=StackRead(
            **{
                **stacks,
                "servers": {k: StackPlace(**v) for k, v in stacks.get("servers", {}).items()},
            }
        ),
        lb_members=MemberRead(
            **{**members, "members": [PoolMember(**m) for m in members.get("members", [])]}
        ),
    )


def _destination(case):
    """check()'s destination for a finding case: its own when the case gives one (SDD §9.3) - with
    its flavors, its GPU flavors among them as its inventory lists them with the profiles it
    declares, and its GPU capacity, when the case gives them (§22.3, plan task G12)."""
    dest = case.get("destination")
    if dest is None:
        return {}
    details = dest["networks"]
    gpu = {}
    if "flavors" in dest:
        declared = dest.get("gpu_profiles", {})
        gpu["flavors"] = dest["flavors"]
        gpu["gpu_flavors"] = [
            {**{key: f[key] for key in ("name", "vcpus", "ram_mb", "disk_gb")}, "gpus": gpus}
            for f in dest["flavors"]
            if (gpus := gpu_requests(f["extra_specs"], declared.get(f["name"])))
        ]
    if "gpu_capacity" in dest:
        capacity = dest["gpu_capacity"]
        gpu["gpu_capacity"] = GpuCapacity(
            status=capacity["status"],
            reason=capacity.get("reason"),
            detail=capacity.get("detail"),
            pools=[GpuPool(**pool) for pool in capacity["pools"]],
        )
    # a network case's floating IPs, DNS and BGP declaration (SDD §9.3, §10, plan task N2b)
    if "floating_ips" in dest:
        gpu["floating_ips"] = dest["floating_ips"]
    if "dns" in dest:
        gpu["dns"] = DnsRead(**dest["dns"])
    return {
        "dst": dst_inv(
            networks={n["name"]: n.get("mtu") for n in details},
            network_details=details,
            subnets=dest.get("subnets", []),
            **gpu,
        ),
        "destination": make_provider(
            id="dst-case",
            name=dest["label"],
            kind=ProviderKind(dest.get("kind", "openstack")),
            role=ProviderRole.destination,
            cloud="dst",
            announces_tenant_networks=dest.get("announces_tenant_networks", False),
        ),
        "dst_caps": {k: dest[k] for k in ("roles", "policy_profile") if k in dest} or None,
    }
