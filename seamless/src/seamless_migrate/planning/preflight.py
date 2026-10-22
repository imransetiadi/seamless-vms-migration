"""Pre-flight validation: the finding catalog of SDD §9.3, and the inventories of SDD §10."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from ..domain.enums import Phase, ProviderKind, Severity, Strategy
from ..domain.models import Finding, GpuDevice, Mappings, NicOverride, Plan, Provider, VMRef, Wave
from ..guest_os import identify
from .gpu import (
    PoolKey,
    ProfileSize,
    gpu_flavor,
    gpu_key,
    gpu_names,
    gpu_units,
    pool_key,
    pool_name,
    vgpu_profile_size,
    vgpu_request_keys,
    vm_gpus,
)
from .ipam import address_refusal
from .kit import kit_vm_name
from .networks import kit_subnet, policy_profile_of, port_refusal, resolve_network
from .platforms import SERIES, parse_microversion

S = Strategy

#: the Heat resource types of an autoscaling group: a scaling policy resizes them on an alarm's
#: signal, without a stack update; a ResourceGroup's member is a stack's resource only (SDD §9.3)
AUTOSCALING_GROUP_TYPES = frozenset(
    {"OS::Heat::AutoScalingGroup", "AWS::AutoScaling::AutoScalingGroup"}
)


@dataclass
class StackPlace:
    """Where a server is in the source's Heat stacks (SDD §10): the top-level stack, its resource
    the server is or is nested under, and the autoscaling group it is a member of, if any."""

    stack: str
    resource: str
    group: str | None = None
    group_type: str | None = None


@dataclass
class StackRead:
    """The source's Heat stacks, by the servers they hold (SDD §10, plan task N2). `unknown` when
    they cannot be read: Heat refused the credential (`forbidden`) or another failure (`error`),
    `detail` naming it by its HTTP status or its kind, never by its message text; a catalog
    without Heat reads `ok` with nothing."""

    status: Literal["ok", "unknown"] = "ok"
    reason: Literal["forbidden", "error"] | None = None
    detail: str | None = None
    servers: dict[str, StackPlace] = field(default_factory=dict)


@dataclass
class PoolMember:
    """A member of an Octavia pool of the source (SDD §10): its load balancer and pool by their
    names (else ids), the pool's id - what a plan's lb_pools maps (§8.5) -, its address as Neutron
    writes it, its protocol port, and its subnet's network - None when the subnets cannot be
    read."""

    load_balancer: str
    pool: str
    pool_id: str
    address: str
    port: int
    network: str | None = None


@dataclass
class MemberRead:
    """The members of the source's Octavia pools (SDD §10, plan task N2), read as `StackRead`."""

    status: Literal["ok", "unknown"] = "ok"
    reason: Literal["forbidden", "error"] | None = None
    detail: str | None = None
    members: list[PoolMember] = field(default_factory=list)


@dataclass
class PoolPair:
    """A mapped pair of Octavia pools as validation read it (SDD §8.5, §10 ``lb_pool``): the source
    pool and the destination pool the plan maps it to; None for one the read could not see."""

    source: dict[str, Any] | None = None
    destination: dict[str, Any] | None = None


@dataclass
class SourceInventory:
    #: network name -> MTU (None when unknown)
    networks: dict[str, int | None] = field(default_factory=dict)
    projects: list[str] = field(default_factory=list)
    #: the source credential's own keypairs: what export_keypairs.yml can export (SDD §10)
    keypairs: list[str] = field(default_factory=list)
    #: the servers of the credential's project's Heat stacks, where each one is (SDD §10)
    stacks: StackRead = field(default_factory=StackRead)
    #: the members of the credential's project's Octavia pools (SDD §10)
    lb_members: MemberRead = field(default_factory=MemberRead)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GpuPool:
    """A Placement pool of the destination's GPU requests (SDD §22.3): what a request asks Placement
    for, and counts only - no provider's name or uuid (§22.12)."""

    resource_class: str
    traits: list[str] = field(default_factory=list)
    forbidden_traits: list[str] = field(default_factory=list)
    #: its traits are a numbered request group's, held by the GPU's own provider; false for the
    #: unnumbered group's, held by it or its compute node, and for a class alone
    same_provider: bool = False
    #: the units free now on the providers that hold the class and the traits
    free: int = 0
    #: the resource providers among them with at least one unit free
    providers: int = 0
    #: the most units free on one of them - below n when the read again with <class>:<n> for a
    #: request group of n > 1 units finds no provider (max_unit caps it)
    largest_free: int = 0
    #: Placement refused the pool's request as a bad one (400: a trait or a resource class it does
    #: not know): its HTTP status and error code; the counts are then 0
    detail: str | None = None


@dataclass
class GpuCapacity:
    """The destination's GPU capacity as Placement shows it (SDD §22.3). `unknown` when it cannot
    be read: Placement refused the credential (`forbidden`), the catalog has no Placement
    (`no_placement`), or another failure (`error`); `detail` names the failure by its HTTP status
    and Placement error code, never by its message text (§22.12)."""

    status: Literal["ok", "unknown"] = "ok"
    reason: Literal["forbidden", "no_placement", "error"] | None = None
    detail: str | None = None
    read_at: datetime | None = None
    pools: list[GpuPool] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """As a destination's check report and the inventory's answer hold it (SDD §10, §22.3)."""
        return {**asdict(self), "read_at": self.read_at.isoformat() if self.read_at else None}


@dataclass
class DnsRead:
    """The destination's DNS as Neutron's DNS integration needs it (SDD §10, plan task N2b):
    whether Neutron lists the `dns-integration` extension - None when its extensions cannot be
    read - and the names of the Designate zones the credential sees - [] for a catalog without
    Designate, None when Designate cannot be read. What is None is not judged (§9.3)."""

    extension: bool | None = None
    zones: list[str] | None = None


@dataclass
class DestinationInventory:
    networks: dict[str, int | None] = field(default_factory=dict)
    #: [{name, vcpus, ram_mb, disk_gb, extra_specs}]
    flavors: list[dict[str, Any]] = field(default_factory=list)
    volume_types: list[str] = field(default_factory=list)
    #: project -> free {cores, ram_mb, instances, volumes, gigabytes}; None/-1 = unlimited
    quotas: dict[str, dict[str, int | None]] = field(default_factory=dict)
    projects: list[str] = field(default_factory=list)
    #: the destination credential's own keypairs: the server is created as that user (SDD §10)
    keypairs: list[str] = field(default_factory=list)
    #: [{id, name, network, network_id, cidr, ipv6_address_mode, ipv6_ra_mode}]: where a NIC's
    #: addresses must lie (SDD §10, §9.3)
    subnets: list[dict[str, Any]] = field(default_factory=list)
    #: [{id, name, mtu, project_id, own, shared, external}]: every network the credential sees, what
    #: a NIC's network name resolves to (SDD §10, §7.3 step 0)
    network_details: list[dict[str, Any]] = field(default_factory=list)
    #: the role names of the credential's token, read with the inventory - live for every run -;
    #: None when they cannot be read: what the VMware kit's map judges (SDD §10, §7.2, RV19-01)
    roles: list[str] | None = None
    #: [{name, vcpus, ram_mb, disk_gb, gpus: [GpuDevice]}]: the flavors whose extra specs ask for a
    #: GPU, with the profile their provider declares (SDD §22.3)
    gpu_flavors: list[dict[str, Any]] = field(default_factory=list)
    #: the Placement pools of their vGPU requests, read with the inventory (SDD §22.3)
    gpu_capacity: GpuCapacity = field(default_factory=GpuCapacity)
    #: the addresses of the credential's project's floating IPs that no port holds: what
    #: os-migrate's `auto` attaches; [] when they cannot be read (SDD §10, §9.3)
    floating_ips: list[str] = field(default_factory=list)
    #: Neutron's DNS integration and Designate's zones (SDD §10, §9.3)
    dns: DnsRead = field(default_factory=DnsRead)
    #: the addresses of the credential's project's floating IPs a port holds; [] when they cannot
    #: be read (SDD §10, §9.3)
    attached_floating_ips: list[str] = field(default_factory=list)
    #: how many more floating IPs the project may allocate; None when unlimited or not read
    floating_ip_quota: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "gpu_flavors": [
                {**flavor, "gpus": [gpu.model_dump(mode="json") for gpu in flavor["gpus"]]}
                for flavor in self.gpu_flavors
            ],
            "gpu_capacity": self.gpu_capacity.to_dict(),
        }


@dataclass(frozen=True)
class CatalogEntry:
    severity: Severity
    strategies: tuple[Strategy, ...]
    remediation: str


CATALOG: dict[str, CatalogEntry] = {
    "SRC_VM_ERROR_STATE": CatalogEntry(
        Severity.blocker,
        (),
        "Repair or reset the source VM (running or stopped), then re-validate.",
    ),
    "SRC_VM_TRANSITIONAL_STATE": CatalogEntry(
        Severity.blocker,
        (),
        "Wait until the Nova task finishes and the VM is ACTIVE or SHUTOFF, then re-validate.",
    ),
    "SRC_VM_VOLUME_DETACHED": CatalogEntry(
        Severity.blocker,
        (),
        "Attach the volume(s) to the source VM again, then re-validate: an earlier cutover "
        "attempt's export may have left them detached, and the migration never drops a disk the VM "
        "had at a cutover attempt. To migrate the VM without one, cancel the migration and plan "
        "the VM anew.",
    ),
    "SRC_VM_DUPLICATE_NAME": CatalogEntry(
        Severity.blocker,
        (),
        "Rename one of the VMs (os-migrate selects workloads by name) or migrate them in "
        "separate plans.",
    ),
    "SRC_VM_NAME_UNUSABLE": CatalogEntry(
        Severity.blocker,
        (),
        "Rename the VM in vCenter so that its name holds a Latin letter or a digit, then "
        "re-validate: the migration kit names the VM's server, ports and volumes after it.",
    ),
    "SRC_VM_MULTIATTACH": CatalogEntry(
        Severity.warning,
        (S.warm, S.storage_handover),
        "Detach the shared volume before cutover or migrate the VM cold.",
    ),
    "SRC_VM_EPHEMERAL_ROOT": CatalogEntry(
        Severity.info,
        (S.warm,),
        "The image-booted root disk is pre-copied through server snapshots (boot_disk_copy), "
        "which makes passes slower.",
    ),
    # the source's Heat stacks and Octavia pools (SDD §9.3, §10, plan task N2)
    "SRC_VM_HEAT_STACK": CatalogEntry(
        Severity.warning,
        (),
        "Seamless moves the server, not its stack: run no stack update at the source until the VM "
        "is finalized - an update may recreate the server - and follow the runbook for Heat "
        "stacks for the stack's other resources.",
    ),
    "SRC_VM_AUTOSCALED": CatalogEntry(
        Severity.blocker,
        (),
        "Suspend the group's scaling at the source - its scaling policies' alarms - as the runbook "
        "for Heat stacks says, then acknowledge the finding for the VM in the plan "
        "(acknowledged_findings) and re-validate.",
    ),
    "SRC_VM_HEAT_UNKNOWN": CatalogEntry(
        Severity.warning,
        (),
        "Give the source credential a role Heat's policy lets list the project's stacks and their "
        "resources, or check with the runbook for Heat stacks whether the VM belongs to a stack or "
        "an autoscaling group: an autoscaling group may replace the server once the cutover stops "
        "it.",
    ),
    "SRC_VM_LB_MEMBER": CatalogEntry(
        Severity.warning,
        (),
        "Move the membership as the runbook for Octavia load balancers says: drain the source "
        "member before the cutover, and add the migrated server to a pool at the destination once "
        "it is verified.",
    ),
    "MAP_LB_POOL_INCOMPATIBLE": CatalogEntry(
        Severity.warning,
        (),
        "Map the source pool to a pool of a load balancer whose provider serves what the pool "
        "uses - an amphora one for HTTP, TLS termination or session persistence -, or accept "
        "that the clients reach the VM through an L4 pool after the cutover.",
    ),
    "SRC_VM_LB_UNKNOWN": CatalogEntry(
        Severity.warning,
        (),
        "Give the source credential a role Octavia's policy lets list the project's pools and "
        "their members, or check the VM's memberships yourself with the runbook for Octavia load "
        "balancers.",
    ),
    # the network codes (SDD §9.3, §23.8, plan task N2b): none blocks
    "SRC_PORT_ADDRESS_PAIRS": CatalogEntry(
        Severity.warning,
        (),
        "Add the address pairs to the migrated server's ports at the destination once it is "
        "created (openstack port set --allowed-address), and check that the peers sharing an "
        "address - a keepalived pair's other member - reach it there.",
    ),
    "MAP_NETWORK_MISSING": CatalogEntry(
        Severity.blocker,
        (),
        "Add a network mapping or create a same-named network on the destination.",
    ),
    "MAP_NETWORK_PRESTAGED": CatalogEntry(
        Severity.info,
        (),
        "Nothing to do: pre-staging creates the network on the destination with the same name. "
        "Add a network mapping to use an existing destination network instead.",
    ),
    "MAP_FLAVOR_PRESTAGED": CatalogEntry(
        Severity.info,
        (),
        "Nothing to do: pre-staging creates the flavor on the destination with the same name, "
        "size and extra specs. Add a flavor mapping to use an existing destination flavor instead.",
    ),
    "DST_KEYPAIR_MISSING": CatalogEntry(
        Severity.blocker,
        (),
        "Create the keypair (its public key) for the destination's migration user, or pre-stage "
        "keypairs "
        "when the source migration user owns it.",
    ),
    "MAP_KEYPAIR_PRESTAGED": CatalogEntry(
        Severity.info,
        (),
        "Nothing to do: pre-staging imports the keypair (name and public key) for the "
        "destination's migration user.",
    ),
    "MAP_FLAVOR_MISSING": CatalogEntry(
        Severity.blocker,
        (),
        "Add a flavor mapping or create a destination flavor with enough vCPUs, RAM and root disk.",
    ),
    "MAP_FLAVOR_AUTO": CatalogEntry(
        Severity.info,
        (),
        "Add an explicit flavor mapping to override the automatically matched flavor.",
    ),
    "MAP_VOLUME_TYPE_MISSING": CatalogEntry(
        Severity.warning,
        (),
        "Add a volume type mapping; otherwise the destination default volume type is used.",
    ),
    "DST_QUOTA_INSUFFICIENT": CatalogEntry(
        Severity.blocker, (), "Raise the destination project quotas or split the plan."
    ),
    "DST_PROJECT_MISSING": CatalogEntry(
        Severity.blocker,
        (),
        "Map the VM's project to an existing destination project (plan.mappings.projects).",
    ),
    "DST_DNS_INTEGRATION_MISSING": CatalogEntry(
        Severity.warning,
        (),
        "Enable Neutron's DNS integration at the destination (an extension driver with Designate "
        "as its external DNS driver) and create the domain's zone for the destination "
        "credential's project, or publish the migrated VM's names in DNS yourself after the "
        "cutover.",
    ),
    "NET_MTU_SHRINK": CatalogEntry(
        Severity.warning,
        (),
        "Lower the guest MTU or map to a destination network with a larger MTU "
        "(ML2/OVN Geneve networks typically use 1442).",
    ),
    "NET_SRIOV_PORT": CatalogEntry(
        Severity.warning,
        (),
        "SR-IOV/macvtap ports need an equivalent physnet on the destination; map the network "
        "accordingly.",
    ),
    "NET_PORT_IPS": CatalogEntry(
        Severity.warning,
        (),
        'Turn on "Keep MAC addresses and every fixed IP" in the plan (port_creation_mode '
        '"neutron"), give each such port exactly one fixed IP before the cutover, or migrate the '
        "VM by storage handover when its ports have at most one address each.",
    ),
    "NET_PREFIX_SPLIT": CatalogEntry(
        Severity.warning,
        (),
        "Put the VMs of each announced network into one wave, or have the network's prefix "
        "routed by hand while its waves are cut over.",
    ),
    "NET_LB_POOL_SPLIT": CatalogEntry(
        Severity.warning,
        (),
        "Put each pool's members into one wave: give them one app tag and plan the waves again, "
        "as VMs that share an app tag stay together. Or accept that between its waves the pool "
        "serves from both clouds: its members already cut over are drained from the source pool "
        "and join the destination pool once verified - by the member helper for a pool the plan "
        "maps, else as the runbook for Octavia load balancers says.",
    ),
    "MAP_NETWORK_AMBIGUOUS": CatalogEntry(
        Severity.blocker,
        (),
        "Send the NIC to a name one destination network carries: one of the credential's "
        "project, or the "
        "only one of that name - map its network to such a name, or rename one of the networks.",
    ),
    "NET_ADDRESS_REFUSED": CatalogEntry(
        Severity.blocker,
        (),
        "Give the NIC an address its destination network takes: map its network to one whose "
        "subnets "
        "hold it, or set the address in a NIC override; for a SLAAC address keep the MAC "
        '(port_creation_mode "neutron").',
    ),
    "NET_PORT_NOT_PERMITTED": CatalogEntry(
        Severity.blocker,
        (),
        # each finding names the role of the destination's own profile (_port_remediation)
        "Send the NIC to a network of the credential's project: map its network to one, or set "
        "a NIC override on the VM's migration page. Another project's shared network takes it "
        "once the destination credential has the role its policy profile counts: service under "
        "secure-RBAC, advsvc under the legacy rules. An external network that is not shared "
        "needs the admin role: Nova attaches it for no other.",
    ),
    "MAP_NIC_OVERRIDE_INVALID": CatalogEntry(
        Severity.blocker,
        (),
        "Fix the VM's network overrides on its migration page: an existing destination network, "
        "addresses inside its subnets, each given once in the plan. VMware VMs take none.",
    ),
    "MAP_NIC_OVERRIDE": CatalogEntry(
        Severity.info,
        (),
        "Nothing to do: each NIC goes where its override says.",
    ),
    "DST_FIP_ADDRESS_UNAVAILABLE": CatalogEntry(
        Severity.blocker,
        (),
        "Keep only addresses the destination can hold: an address of a subnet of its external "
        "network, free there or held detached by the destination credential's project, a token "
        "with a role that may choose a floating IP's address, and the quota for them - or let the "
        'plan take new addresses (network_cutover.floating_ips "auto").',
    ),
    "NET_DNS_TTL_LONG": CatalogEntry(
        Severity.warning,
        (),
        "Lower the record's TTL below the plan's downtime SLO well before the window - a pre_stop "
        "hook may do it - or accept that clients keep the old address until the TTL runs out.",
    ),
    "MAP_FIP_ADDRESS_CHANGE": CatalogEntry(
        Severity.warning,
        (S.cold, S.warm),
        "To keep an address, have the destination credential's project hold a floating IP of that "
        "address, with no port, before the cutover (an admin can create one with a given address "
        "on the external network); else update the clients, allowlists and DNS records of the "
        "old address after it.",
    ),
    "VM_PCI_PASSTHROUGH": CatalogEntry(
        Severity.blocker,
        (),
        "Map the VM's passthrough device in its plan to a GPU flavor of the destination: a GPU "
        "mapping of its PCI ids (VMware) or a flavor mapping (OpenStack). A device without ids "
        "cannot be mapped.",
    ),
    "VM_VGPU": CatalogEntry(
        Severity.blocker,
        (),
        "Map the VM's vGPU in its plan to a GPU flavor of the destination: a GPU mapping of its "
        "profile (VMware) or a flavor mapping (OpenStack).",
    ),
    "MAP_GPU_FLAVOR": CatalogEntry(
        Severity.info,
        (),
        "Check the GPU flavor's size and GPU before the cutover: GPU migration is a Technology "
        "Preview.",
    ),
    "MAP_GPU_DOWNSIZED": CatalogEntry(
        Severity.warning,
        (),
        "Map the VM to a GPU flavor of the destination that gives at least the GPU it has, or "
        "confirm that its workload fits the smaller one.",
    ),
    "DST_GPU_CAPACITY_INSUFFICIENT": CatalogEntry(
        Severity.blocker,
        (),
        "Free GPU capacity in the destination, or move some of the plan's GPU VMs to a later plan "
        "or to another GPU flavor's pool, then re-validate.",
    ),
    "DST_GPU_CAPACITY_UNKNOWN": CatalogEntry(
        Severity.warning,
        (),
        "Give the destination credential a role Placement lets read its allocation candidates "
        "(admin by default), or check the destination's GPU capacity yourself; a PCI alias's "
        "devices are configured in nova.conf.",
    ),
    "GUEST_GPU_DRIVER": CatalogEntry(
        Severity.warning,
        (),
        "Install the NVIDIA driver the destination needs in the guest - a vGPU guest driver its "
        "vGPU manager supports, or the data center driver for a passed-through GPU - and test it "
        "after the cutover.",
    ),
    "GUEST_GPU_LICENSE": CatalogEntry(
        Severity.info,
        (),
        "Make the NVIDIA License System (a CLS instance or an on-premises DLS instance) reachable "
        "from the VM's destination network, and check the guest's license after the cutover.",
    ),
    "VOL_ENCRYPTED": CatalogEntry(
        Severity.warning,
        (),
        "The Barbican key must be re-created on the destination before the volume can be attached.",
    ),
    "GUEST_OS_LEGACY": CatalogEntry(
        Severity.warning,
        (),
        "Out of vendor support: it still migrates; test the application on the destination, "
        "check the "
        "virtio drivers and plan a longer verification.",
    ),
    "GUEST_OS_UNKNOWN": CatalogEntry(
        Severity.info,
        (),
        "Set the os_distro and os_version image properties (OpenStack) or run VMware Tools so "
        "the guest can be identified; verification uses the Linux profile meanwhile.",
    ),
    "GUEST_CONVERSION_UNVERIFIED": CatalogEntry(
        Severity.warning,
        (S.vmware_cold, S.vmware_warm),
        "virt-v2v converts this guest but Red Hat does not support the conversion: run a test "
        "conversion of a copy first.",
    ),
    "GUEST_CONVERSION_UNSUPPORTED": CatalogEntry(
        Severity.warning,
        (S.vmware_cold, S.vmware_warm),
        "The RHEL 9 conversion host has no drivers for this guest: install the virtio storage "
        "and network drivers from an older virtio-win release inside the guest and test the "
        "conversion, or migrate the VM another way.",
    ),
    "VMW_CBT_DISABLED": CatalogEntry(
        Severity.warning,
        (S.vmware_warm,),
        "Enable Changed Block Tracking on the VM or accept a cold migration.",
    ),
    "VMW_INDEPENDENT_DISK": CatalogEntry(
        Severity.warning,
        (S.vmware_warm,),
        "Independent disks are excluded from snapshots; change the disk mode or migrate cold.",
    ),
    "VMW_SNAPSHOTS_PRESENT": CatalogEntry(
        Severity.warning, (), "Consolidate or delete the VM snapshots before migrating."
    ),
    "VMW_TOOLS_MISSING": CatalogEntry(
        Severity.info, (), "Install or start VMware Tools for graceful shutdown and IP discovery."
    ),
    "DST_CAPABILITY_MISSING": CatalogEntry(
        Severity.blocker,
        (),
        "Use a destination whose APIs provide the capability (SDD §21.4); upgrading the "
        "destination's services raises their maxima.",
    ),
    "DST_RELEASE_OUTSIDE_SUPPORT": CatalogEntry(
        Severity.warning,
        (),
        "Prefer a Tier 1 series for production (SDD §21.1); declare the destination's series on "
        "the provider when its APIs cannot tell it, and run the destination lab matrix (QASuite "
        "§7.1) before relying on it.",
    ),
    "CONV_HOST_MISSING": CatalogEntry(
        Severity.warning,
        (S.cold, S.warm, S.vmware_cold, S.vmware_warm),
        "Configure a conversion host on the provider.",
    ),
    "HANDOVER_BACKEND_UNMAPPED": CatalogEntry(
        Severity.info,
        (S.storage_handover,),
        "Map the volume type to a Cinder backend of the destination in handover.backend_map.",
    ),
    "APP_PROBE_MISSING": CatalogEntry(
        Severity.warning,
        (),
        "Add an application probe for its service to the plan, for every VM or for this one "
        "(probe_overrides), or use the suggested probe on the plan page.",
    ),
}

_SRIOV_VNIC_TYPES = frozenset({"direct", "direct-physical", "macvtap"})
_QUOTA_KEYS = ("cores", "ram_mb", "instances", "volumes", "gigabytes")


def is_legacy_os(os_type: str | None) -> bool:
    """True when the guest is out of standard vendor support (SDD §9.5 catalog)."""
    return identify(os_type).lifecycle == "legacy"


def keep_detached_volumes(previous: VMRef, current: VMRef) -> tuple[VMRef, list[Finding]]:
    """``current`` with every volume of ``previous`` - the VM as a migration whose history reached
    cutover was validated - that it no longer lists, and the blocker ``SRC_VM_VOLUME_DETACHED``
    naming them: a cold export detaches the data volumes, and one that failed may have left them
    so; a validation that dropped them would let the next cutover complete one disk short
    (SDD §5.4, §9.3, review RV7-01). Root and ephemeral disks are no volumes and never kept."""
    listed = {d.id for d in current.disks}
    gone = [d for d in previous.disks if d.kind == "volume" and d.id not in listed]
    if not gone:
        return current, []
    kept = current.model_copy(update={"disks": [*current.disks, *gone]})
    names = ", ".join(d.id for d in gone)
    return kept, [
        finding(
            "SRC_VM_VOLUME_DETACHED",
            f"volume(s) {names} of {current.name}, which it had at a cutover attempt, are no "
            "longer attached to it",
        )
    ]


#: what lets another project's shared network take the port, by the destination's policy profile:
#: the least role that profile counts, never admin for this alone (SDD §21.5, Security.md C4-06)
_PORT_ROLE_ADVICE = {
    "secure_rbac": "the destination credential has the service role for the migration window "
    "(Neutron's secure-RBAC defaults); admin is not needed for this",
    "legacy": "the destination credential has the advsvc role (Neutron's legacy rules)",
    "unknown": "a check of the provider detects its series, or the provider declares its policy "
    "profile or series, so the check counts that profile's role: service under secure-RBAC, "
    "advsvc under the legacy rules",
}


def _port_remediation(profile: str) -> str:
    """NET_PORT_NOT_PERMITTED's remediation for the destination's policy ``profile`` (review
    RV18-10: it sent every operator to the admin role)."""
    return (
        "Send the NIC to a network of the credential's project: map its network to one, or set a "
        "NIC override on the VM's migration page. Another project's shared network takes it once "
        f"{_PORT_ROLE_ADVICE[profile]}. An external network that is not shared needs the admin "
        "role: Nova attaches it for no other."
    )


#: SRC_VM_AUTOSCALED's remediation once the plan acknowledges it (SDD §9.3, owner decision 9)
AUTOSCALED_ACKNOWLEDGED_REMEDIATION = (
    "Keep the group's scaling suspended at the source until the VM is finalized, as the runbook "
    "for Heat stacks says: a server the group creates meanwhile is a second live copy."
)

#: SRC_VM_DUPLICATE_NAME's remediation for two VMware VMs the migration kit names alike (§9.3)
KIT_TWIN_REMEDIATION = (
    "Rename one of the VMs in vCenter: the migration kit names a VM's server, ports and volumes "
    "after its name as it writes it, so the two would share them - in separate plans too, when "
    "they go to one destination project."
)


def finding(
    code: str,
    message: str,
    severity: Severity | None = None,
    remediation: str | None = None,
) -> Finding:
    entry = CATALOG[code]
    return Finding(
        code=code,
        severity=severity or entry.severity,
        message=message,
        remediation=remediation or entry.remediation,
        strategies=list(entry.strategies),
    )


def destination_project(vm: VMRef, plan: Plan, dst_inv: DestinationInventory) -> str | None:
    """Destination project charged for ``vm`` (mapping, same name, or the only known one)."""
    if vm.project and vm.project in plan.mappings.projects:
        return plan.mappings.projects[vm.project]
    if vm.project and vm.project in dst_inv.quotas:
        return vm.project
    if len(dst_inv.quotas) == 1:
        return next(iter(dst_inv.quotas))
    return vm.project


#: the GPU rows of §9.3 (SDD §22.5): what the advisor's evidence leaves out, as nothing about GPUs
#: reaches Jev in 0.3.0 (§14.4, §22.12)
GPU_CODES = frozenset(
    {
        "VM_VGPU",
        "VM_PCI_PASSTHROUGH",
        "MAP_GPU_FLAVOR",
        "MAP_GPU_DOWNSIZED",
        "DST_GPU_CAPACITY_INSUFFICIENT",
        "DST_GPU_CAPACITY_UNKNOWN",
        "GUEST_GPU_DRIVER",
        "GUEST_GPU_LICENSE",
    }
)


def _kind_of(vm: VMRef, source_kind: ProviderKind | str | None) -> ProviderKind:
    """The VM's source kind, as given, else as the VM shows it: a VMware VM has no flavor."""
    if source_kind is not None:
        return ProviderKind(source_kind)
    return ProviderKind.vmware if vm.flavor is None else ProviderKind.openstack


def _flavor_named(dst_inv: DestinationInventory, name: str | None) -> dict[str, Any] | None:
    return next((f for f in dst_inv.flavors if f.get("name") == name), None) if name else None


def destination_flavor(
    vm: VMRef,
    plan: Plan,
    dst_inv: DestinationInventory,
    source_kind: ProviderKind | str | None = None,
) -> dict[str, Any] | None:
    """The destination flavor ``vm``'s server is created with (SDD §9.3, review PL-01): a GPU VM's
    GPU flavor, which its plan names and nothing matches (§22.4); else the mapped one, else the
    same-named or automatically matched one. ``None`` for a VMware VM without a GPU flavor (the
    migration kit chooses), for a flavor pre-staging creates with the source's size, and when none
    resolves."""
    if vm_gpus(vm):
        return _flavor_named(dst_inv, gpu_flavor(vm, plan, _kind_of(vm, source_kind)))
    if not vm.flavor or flavor_prestaged(vm, plan, dst_inv):
        return None
    target = plan.mappings.flavors.get(vm.flavor)
    if target is not None:
        return _flavor_named(dst_inv, target)
    return fitting_flavor(vm, dst_inv)


def resource_demand(
    vm: VMRef,
    plan: Plan | None = None,
    dst_inv: DestinationInventory | None = None,
    source_kind: ProviderKind | str | None = None,
) -> dict[str, int]:
    """Quota the VM needs at the destination. An ``image_root`` disk becomes a destination volume
    on the warm path (``boot_disk_copy``), so it counts unless the plan is cold-only. Its cores and
    RAM are those of the destination flavor its server is created with, when one resolves - a GPU
    VM's GPU flavor, a VMware VM's included (SDD §9.3, §22.4, review PL-01)."""
    kinds = {"volume", "vmdk"}
    if plan is None or plan.default_strategy != Strategy.cold:
        kinds.add("image_root")
    volumes = [d for d in vm.disks if d.kind in kinds]
    flavor = (
        destination_flavor(vm, plan, dst_inv, source_kind)
        if plan is not None and dst_inv is not None
        else None
    )
    return {
        "cores": int(flavor.get("vcpus") or 0) if flavor else vm.vcpus,
        "ram_mb": int(flavor.get("ram_mb") or 0) if flavor else vm.ram_mb,
        "instances": 1,
        "volumes": len(volumes),
        "gigabytes": sum(d.size_gb for d in volumes),
    }


#: the phases whose migration has created its destination volumes, or is creating them: its
#: server is still to come (SDD §9.3, review PL-02)
SERVER_STILL_TO_COME = frozenset(
    {Phase.precopy, Phase.syncing, Phase.awaiting_cutover, Phase.cutover}
)
#: the phases whose migration holds what it created, or is releasing it - in use, so already off
#: the free quota (SDD §9.3, review PL-02)
NOTHING_STILL_TO_COME = frozenset(
    {Phase.verifying, Phase.completed, Phase.finalized, Phase.rolling_back, Phase.cancelled}
)


#: what a cutover of a VM will still create at the destination: all of it, its server alone, or
#: nothing (SDD §9.3)
QuotaShare = Literal["all", "server", "nothing"]


def quota_share(phase: Phase | None, *, holds_volumes: bool = False) -> QuotaShare:
    """What a cutover of a VM whose migration is in ``phase`` (``None``: no migration yet) will
    still create at the destination (SDD §9.3, reviews PL-02, RV38A-01): nothing once what it holds
    is in use, or being released; its server alone while its destination volumes exist or are
    being created, and when it holds the destination volumes an earlier attempt created, which
    the next attempt uses again; all of it otherwise."""
    if phase in NOTHING_STILL_TO_COME:
        return "nothing"
    if phase in SERVER_STILL_TO_COME or holds_volumes:
        return "server"
    return "all"


def remaining_demand(
    vm: VMRef,
    plan: Plan | None,
    share: QuotaShare,
    dst_inv: DestinationInventory | None = None,
    source_kind: ProviderKind | str | None = None,
) -> dict[str, int]:
    """The part of ``vm``'s demand its ``share`` counts (SDD §9.3, reviews PL-02, RV38A-01)."""
    demand = resource_demand(vm, plan, dst_inv, source_kind)
    if share == "nothing":
        return dict.fromkeys(demand, 0)
    if share == "server":
        return {**demand, "volumes": 0, "gigabytes": 0}
    return demand


def _quota_shortfall(
    vm: VMRef,
    plan: Plan,
    dst_inv: DestinationInventory,
    all_vms: Sequence[VMRef],
    shares: Mapping[str, QuotaShare],
    source_kind: ProviderKind | str | None = None,
) -> tuple[str, list[str]] | None:
    project = destination_project(vm, plan, dst_inv)
    if project is None or project not in dst_inv.quotas:
        return None
    totals = dict.fromkeys(_QUOTA_KEYS, 0)
    for other in all_vms:
        if destination_project(other, plan, dst_inv) == project:
            share = remaining_demand(
                other, plan, shares.get(other.source_id, "all"), dst_inv, source_kind
            )
            for key, value in share.items():
                totals[key] += value
    free = dst_inv.quotas[project]
    short = []
    for key in _QUOTA_KEYS:
        limit = free.get(key)
        if limit is None or limit < 0:
            continue
        if totals[key] > limit:
            short.append(f"{key} {totals[key]} > {limit} free")
    return (project, short) if short else None


def _flavor_fits(flavor: dict[str, Any], vm: VMRef, root_gb: int) -> bool:
    return (
        int(flavor.get("vcpus") or 0) >= vm.vcpus
        and int(flavor.get("ram_mb") or 0) >= vm.ram_mb
        and int(flavor.get("disk_gb") or 0) >= root_gb
    )


def _local_root_gb(vm: VMRef) -> int:
    """Root disk size the flavor must provide (image-booted / ephemeral roots only)."""
    root = vm.root_disk()
    if root is not None and root.kind in ("image_root", "ephemeral"):
        return root.size_gb
    return 0


def _flavor_size(flavor: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        int(flavor.get("vcpus") or 0),
        int(flavor.get("ram_mb") or 0),
        int(flavor.get("disk_gb") or 0),
        str(flavor.get("name") or ""),
    )


#: Extra specs that make a destination flavor unsuitable for automatic matching: it would pin the
#: server to special hardware or hosts (SDD §9.3 ``MAP_FLAVOR_AUTO``) - a resource or trait request,
#: bare or in one of Nova's request groups (``resources<suffix>:``, ``trait<suffix>:``, review
#: PL-08), a PCI alias, an aggregate pin.
_CONSTRAINED_SPEC = re.compile(
    r"^(?:(?:resources|trait)[A-Za-z0-9_-]*:|pci_passthrough:|aggregate_instance_extra_specs:)"
)


def _vm_gpus(vm: VMRef, source: Provider | None) -> tuple[list[GpuDevice], str | None]:
    """The VM's GPUs as §9.3's two GPU rows read them (SDD §22.2, plan task G2), with the profile
    its source declares for its flavor: as the source reported them, else - a VM recorded before
    G2 - what its flavor's extra specs ask for, so a VMware VM reported with them stays blocked."""
    declared = source.gpu_profiles.get(vm.flavor) if source is not None and vm.flavor else None
    return vm_gpus(vm, declared), declared


@dataclass(frozen=True)
class _GpuMapping:
    """Whether a GPU VM's mapping applies (SDD §22.4): its GPU flavor when it does; why not when
    no mapping names one, a VMware key is missing, its keys split or the flavor asks for no GPU;
    neither for a mapped flavor that does not exist, which is MAP_FLAVOR_MISSING's."""

    flavor: dict[str, Any] | None = None
    why: str | None = None


def _quoted(names: Iterable[str]) -> str:
    return ", ".join(repr(name) for name in names)


def _gpu_mapping(
    vm: VMRef,
    plan: Plan,
    gpus: Sequence[GpuDevice],
    kind: ProviderKind,
    dst_inv: DestinationInventory,
    where: str,
) -> _GpuMapping:
    """The mapping of a GPU VM, as §22.4 decides whether it applies: a VMware VM's GPU keys all to
    one GPU flavor, an OpenStack VM's flavor to one - never a same-named flavor."""
    if kind == ProviderKind.vmware:
        keys = list(dict.fromkeys(gpu_key(device) for device in gpus))
        if None in keys:
            return _GpuMapping(
                why="No mapping can name its device without ids, so none applies to the VM."
            )
        named = [key for key in keys if key is not None]
        unnamed = [key for key in named if key not in plan.mappings.gpus]
        if unnamed:
            return _GpuMapping(
                why=f"The plan's GPU mappings name no flavor for {_quoted(unnamed)}: map "
                f"{'it' if len(unnamed) == 1 else 'them'} to a GPU flavor of {where}."
            )
        targets = {key: plan.mappings.gpus[key] for key in named}
        if len(set(targets.values())) > 1:
            pairs = ", ".join(f"{key!r} to {flavor!r}" for key, flavor in targets.items())
            return _GpuMapping(
                why=f"Its GPUs map to different flavors ({pairs}): its server is created with one."
            )
        target = next(iter(targets.values()))
        mapping = f"Its GPU mapping, {_quoted(named)} to {target!r},"
    else:
        target = plan.mappings.flavors.get(vm.flavor) if vm.flavor else None
        if target is None:
            return _GpuMapping(
                why=f"The plan maps no flavor to {vm.flavor!r}: map it to a GPU flavor of {where}."
            )
        mapping = f"Its flavor mapping, {vm.flavor!r} to {target!r},"
    if _flavor_named(dst_inv, target) is None:
        return _GpuMapping()
    gpu_flavor_of = next((f for f in dst_inv.gpu_flavors if f.get("name") == target), None)
    if gpu_flavor_of is None:
        return _GpuMapping(
            why=f"{mapping} names a flavor of {where} that asks for no GPU: the server would lose "
            "its GPU."
        )
    return _GpuMapping(flavor=gpu_flavor_of)


def _gpu_blockers(
    vm: VMRef, gpus: Sequence[GpuDevice], declared: str | None, why: str
) -> list[Finding]:
    """`VM_PCI_PASSTHROUGH` and `VM_VGPU` for a GPU VM no mapping applies to (SDD §9.3, §22.5): an
    OpenStack flavor's requests named by their extra specs, a VMware VM's devices by what names
    them, then why no mapping applies."""
    out = []
    passthrough = [g for g in gpus if g.kind == "passthrough"]
    if passthrough:
        alias = vm.flavor_extra_specs.get("pci_passthrough:alias")
        message = (
            f"Flavor requests PCI passthrough ({alias})."
            if alias is not None and any(g.alias for g in passthrough)
            else f"The VM has a PCI passthrough device ({gpu_names(passthrough)})."
        )
        out.append(finding("VM_PCI_PASSTHROUGH", f"{message} {why}"))
    vgpus = [g for g in gpus if g.kind == "vgpu"]
    if vgpus:
        keys = vgpu_request_keys(vm.flavor_extra_specs, declared)
        message = (
            f"Flavor requests a vGPU ({', '.join(keys)})."
            if keys and any(g.resource_class for g in vgpus)
            else f"The VM has a vGPU ({gpu_names(vgpus)})."
        )
        out.append(finding("VM_VGPU", f"{message} {why}"))
    return out


def _missing_gpu_targets(
    vm: VMRef, plan: Plan, kind: ProviderKind, flavors: set[str], where: str
) -> list[str]:
    """The mappings of a GPU VM whose flavor does not exist at the destination, in words: its
    flavor mapping, or for a VMware VM each GPU key's (SDD §9.3 MAP_FLAVOR_MISSING, §22.4)."""
    if kind != ProviderKind.vmware:
        target = plan.mappings.flavors.get(vm.flavor) if vm.flavor else None
        if target is None or target in flavors:
            return []
        return [f"Flavor {vm.flavor!r} is mapped to {target!r}, which does not exist in {where}."]
    keys = dict.fromkeys(key for key in map(gpu_key, vm_gpus(vm)) if key is not None)
    return [
        f"The plan maps {key!r} to {plan.mappings.gpus[key]!r}, which does not exist in {where}."
        for key in keys
        if key in plan.mappings.gpus and plan.mappings.gpus[key] not in flavors
    ]


def _requests_text(requests: Iterable[GpuDevice]) -> str:
    """A GPU flavor's requests in words: "vGPU VGPU CUSTOM_NVIDIA_A100_4C (A100-4C)" (SDD §22.5)."""
    words = []
    for device in requests:
        what = (
            f"vGPU {pool_name(device.resource_class or '', device.traits, device.forbidden_traits)}"
            if device.kind == "vgpu"
            else f"PCI alias {device.alias}"
        )
        words.append(
            what
            + (f" ×{device.count}" if device.count > 1 else "")
            + (f" ({device.profile})" if device.profile else "")
        )
    return ", ".join(words)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _first_names(names: Iterable[str], limit: int = 10) -> str:
    """The names sorted, the first ``limit`` of them, then how many more: a message of a plan of
    hundreds of VMs stays short (SDD §22.5)."""
    ordered = sorted(set(names))
    if len(ordered) <= limit:
        return ", ".join(ordered)
    return f"{', '.join(ordered[:limit])} and {len(ordered) - limit} more"


def _and(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _sized(devices: Iterable[GpuDevice]) -> list[tuple[ProfileSize, str]]:
    """Each GPU unit whose profile has a size (SDD §22.2), with the profile, the largest first."""
    units = []
    for device in devices:
        size = vgpu_profile_size(device.profile) if device.profile else None
        if size is not None:
            units += [(size, str(device.profile))] * device.count
    return sorted(
        units, key=lambda unit: (unit[0].framebuffer_gb, unit[0].slices or 0), reverse=True
    )


def _nvidia_passthrough(devices: Iterable[GpuDevice]) -> bool:
    """Whether every passed-through device is known to be an NVIDIA GPU: a VMware device of vendor
    id 10de. An OpenStack PCI alias names no device, so it is "a PCI device" (SDD §22.1)."""
    passthrough = [d for d in devices if d.kind == "passthrough"]
    return bool(passthrough) and all(d.vendor_id == "10de" for d in passthrough)


def _gpu_words(devices: Sequence[GpuDevice]) -> str:
    """What the guest gets, in words: a vGPU, a passed-through NVIDIA GPU or a PCI device."""
    if _is_vgpu(devices):
        return "a vGPU"
    if _nvidia_passthrough(devices):
        return "a passed-through NVIDIA GPU"
    return "a passed-through PCI device"


def _is_vgpu(devices: Iterable[GpuDevice]) -> bool:
    """A vGPU among them: one of kind vgpu, or a PCI device with a vGPU profile (SDD §22.3)."""
    return any(d.kind == "vgpu" or (d.kind == "passthrough" and d.profile) for d in devices)


def _gpu_flavor_findings(
    vm: VMRef, gpus: Sequence[GpuDevice], flavor: dict[str, Any], where: str
) -> list[Finding]:
    """`MAP_GPU_FLAVOR` and `MAP_GPU_DOWNSIZED` for a GPU mapping that applies (SDD §22.5)."""
    vcpus, ram_mb, disk_gb, name = _flavor_size(flavor)
    root = _local_root_gb(vm)
    less = [
        text
        for short, text in (
            (vcpus < vm.vcpus, f"{vcpus} vCPUs for its {vm.vcpus}"),
            (ram_mb < vm.ram_mb, f"{ram_mb} MB RAM for its {vm.ram_mb}"),
            (disk_gb < root, f"{disk_gb} GB of root disk for its {root}"),
        )
        if short
    ]
    out = [
        finding(
            "MAP_GPU_FLAVOR",
            f"The VM's GPUs ({gpu_names(gpus)}) go to flavor {name!r} of {where}: {vcpus} vCPUs, "
            f"{ram_mb} MB RAM, {disk_gb} GB disk, {_requests_text(flavor['gpus'])}"
            + (f"; it gives the VM {_and(less)}" if less else "")
            + ". GPU migration is a Technology Preview.",
            severity=Severity.warning if less else None,
            # the GPU flavor decides the server's size, one per GPU key of a plan (SDD §22.4)
            remediation=(
                "Map the VM's GPU to a GPU flavor of the destination of at least its size, or put "
                "the VMs that need another size in another plan, which maps their GPU to another "
                "flavor."
            )
            if less
            else None,
        )
    ]
    has = sum(device.count for device in gpus)
    gives = sum(device.count for device in flavor["gpus"])
    reasons = [f"{gives} GPU{'' if gives == 1 else 's'} for its {has}"] if gives < has else []
    # compared one by one, the largest first: as many pairs as both sides have sizes for
    pairs = list(zip(_sized(gpus), _sized(flavor["gpus"]), strict=False))
    smaller = next(((v, f) for v, f in pairs if f[0].framebuffer_gb < v[0].framebuffer_gb), None)
    if smaller is not None:
        (own, own_name), (other, other_name) = smaller
        reasons.append(
            f"a {other.framebuffer_gb} GB framebuffer for its {own.framebuffer_gb} GB "
            f"({other_name} for {own_name})"
        )
    fewer = next(
        (
            (v, f)
            for v, f in pairs
            if v[0].slices is not None and f[0].slices is not None and f[0].slices < v[0].slices
        ),
        None,
    )
    if fewer is not None:
        (own, own_name), (other, other_name) = fewer
        reasons.append(
            f"{_count(other.slices, 'MIG slice')} for its {own.slices} ({other_name} for "
            f"{own_name})"
        )
    if reasons:
        out.append(
            finding(
                "MAP_GPU_DOWNSIZED",
                f"Flavor {name!r} of {where} gives the VM less GPU than it has: "
                f"{'; '.join(reasons)}.",
            )
        )
    return out


def _guest_gpu_findings(
    gpus: Sequence[GpuDevice], flavor: dict[str, Any] | None, kind: ProviderKind, where: str
) -> list[Finding]:
    """`GUEST_GPU_DRIVER` for every GPU VM, and `GUEST_GPU_LICENSE` for a vGPU's (SDD §22.5): the
    driver the GPU it gets needs - the flavor's when a mapping applies, else its own -, and the
    change of kind or model the mapping makes."""
    target = flavor["gpus"] if flavor is not None else gpus
    to_vgpu = _is_vgpu(target)
    if to_vgpu:
        message = (
            f"The guest's NVIDIA driver must suit {where}: a vGPU guest driver of a release its "
            "hosts' vGPU manager supports (NVIDIA's compatibility table)."
        )
    elif _nvidia_passthrough(target):
        message = (
            f"The guest's NVIDIA driver must suit {where}: the data center driver for its "
            "passed-through NVIDIA GPU."
        )
    else:
        message = (
            f"The guest's driver must suit {where}: the driver of its passed-through PCI device."
        )
    if flavor is not None:
        models = sorted({size.gpu for size, _ in _sized(gpus)})
        new_models = sorted({size.gpu for size, _ in _sized(target)})
        if _is_vgpu(gpus) != to_vgpu:
            message += (
                f" The mapping changes the GPU from {_gpu_words(gpus)} to {_gpu_words(target)}, so "
                "the guest needs a new driver."
            )
        elif models and new_models and models != new_models:
            message += (
                f" The mapping changes the GPU model from {', '.join(models)} to "
                f"{', '.join(new_models)}, so the guest needs a new driver."
            )
    if kind == ProviderKind.vmware:
        message += " virt-v2v neither installs nor updates the driver."
    out = [finding("GUEST_GPU_DRIVER", message)]
    if _is_vgpu(gpus) or to_vgpu:
        out.append(
            finding(
                "GUEST_GPU_LICENSE",
                "The guest licenses its vGPU from the NVIDIA License System: its license server, "
                "a CLS instance or an on-premises DLS instance, must be reachable from its network "
                f"in {where}, or the guest runs its GPU unlicensed.",
            )
        )
    return out


def server_gpu_flavor(
    vm: VMRef, plan: Plan, gpu_flavors: Iterable[dict[str, Any]], kind: ProviderKind | str
) -> dict[str, Any] | None:
    """The GPU flavor among ``gpu_flavors`` that ``vm``'s server is created with, if any (SDD
    §22.5, §22.7): a GPU VM's GPU flavor, or the GPU flavor a mapping sends a VM without a GPU
    to."""
    if vm_gpus(vm):
        name = gpu_flavor(vm, plan, kind)
    else:
        name = plan.mappings.flavors.get(vm.flavor) if vm.flavor else None
    return next((f for f in gpu_flavors if f.get("name") == name), None) if name else None


@dataclass(frozen=True)
class Shortfall:
    """Where vGPU requests find no room in a capacity read (SDD §22.3, §22.5, §22.7): ``kind``
    "refused" - Placement refused the pool's request, its ``detail`` -, "pool" - the units asked of
    it pass its free -, "class" - across its class's pools - or "group" - no provider has the
    ``needed`` units a request group takes from one, ``free`` the most free on one."""

    key: PoolKey
    #: the pool by pool_name, or a class alone
    pool: str
    kind: Literal["refused", "pool", "class", "group"]
    needed: int
    free: int
    detail: str | None = None


def gpu_shortfalls(
    requests: Iterable[GpuDevice], asked: Mapping[PoolKey, int], pools: Iterable[GpuPool]
) -> list[Shortfall]:
    """Where the vGPU requests among ``requests`` find no room in the read's ``pools`` when
    ``asked`` gives the units asked of each pool (``gpu_units``, SDD §22.3, §22.5, §22.7) - the
    pre-flight's rule and the cutover gate's, in the order the pre-flight words it: each pool they
    ask of that Placement refuses or whose free is below what is asked of it; each class across
    its pools, unless a request without traits asks of the class's pool, whose check is its own;
    then each request group of n > 1 units that no provider holds. A pool the read does not hold
    is skipped: nothing is guessed."""
    vgpus = [g for g in requests if g.kind == "vgpu" and g.resource_class]
    read = {
        pool_key(p.resource_class, p.traits, p.forbidden_traits, p.same_provider): p for p in pools
    }
    keys = list(
        dict.fromkeys(
            pool_key(g.resource_class or "", g.traits, g.forbidden_traits, g.same_provider)
            for g in vgpus
        )
    )
    out: list[Shortfall] = []
    for key in keys:
        pool = read.get(key)
        if pool is None:
            continue
        name = pool_name(pool.resource_class, pool.traits, pool.forbidden_traits)
        if pool.detail is not None:
            out.append(Shortfall(key, name, "refused", asked.get(key, 0), pool.free, pool.detail))
        elif asked.get(key, 0) > pool.free:
            out.append(Shortfall(key, name, "pool", asked[key], pool.free))
    for cls in dict.fromkeys(g.resource_class or "" for g in vgpus):
        key = (cls, (), (), False)
        alone = read.get(key)
        if alone is None or alone.detail is not None or key in keys:
            continue  # a request without traits asks its class's pool: that check was its own
        if asked.get(key, 0) > alone.free:
            out.append(Shortfall(key, cls, "class", asked[key], alone.free))
    for device in vgpus:
        key = pool_key(
            device.resource_class or "",
            device.traits,
            device.forbidden_traits,
            device.same_provider,
        )
        pool = read.get(key)
        # a group of n > 1 units needs one provider with n free (SDD §22.3, §22.5)
        if (
            pool is not None
            and pool.detail is None
            and device.count > 1
            and device.count > pool.largest_free
        ):
            name = pool_name(pool.resource_class, pool.traits, pool.forbidden_traits)
            out.append(Shortfall(key, name, "group", device.count, pool.largest_free))
    return out


def _unknown_capacity(capacity: GpuCapacity) -> str:
    """Why the destination's GPU capacity cannot be read, in words (SDD §22.3, §22.5)."""
    if capacity.reason == "forbidden":
        return (
            f"Placement refuses its credential ({capacity.detail}): its default policy lets only "
            "the admin role read it"
        )
    if capacity.reason == "no_placement":
        return "its catalog has no Placement"
    return f"the read failed ({capacity.detail})"


def _capacity_findings(
    vm: VMRef,
    flavor: dict[str, Any],
    plan: Plan,
    dst_inv: DestinationInventory,
    kind: ProviderKind,
    where: str,
    selected: Sequence[VMRef],
    shares: Mapping[str, QuotaShare],
) -> list[Finding]:
    """`DST_GPU_CAPACITY_UNKNOWN` and `DST_GPU_CAPACITY_INSUFFICIENT` for a VM whose server is
    created with a GPU flavor (SDD §22.5): the plan's VMs each count what their cutover will still
    create, per pool and per resource class across its pools; a group of n > 1 units needs a
    provider with n free; a pool Placement refused counts nothing. Nothing is guessed."""
    capacity = dst_inv.gpu_capacity
    vgpus = [g for g in flavor["gpus"] if g.kind == "vgpu" and g.resource_class]
    pools = {
        pool_key(p.resource_class, p.traits, p.forbidden_traits, p.same_provider): p
        for p in capacity.pools
    }
    unknown = []
    if vgpus and capacity.status == "unknown":
        unknown.append(
            f"The GPU capacity of {where} cannot be read: {_unknown_capacity(capacity)}."
        )
    elif vgpus:
        # a read that holds no pool for a request: nothing is guessed for it (SDD §22.5)
        unknown += [
            f"The GPU capacity read of {where} holds no pool "
            f"{pool_name(g.resource_class or '', g.traits, g.forbidden_traits)}: nothing is "
            "guessed for it."
            for g in vgpus
            if pool_key(g.resource_class or "", g.traits, g.forbidden_traits, g.same_provider)
            not in pools
        ]
    unknown += [
        f"Flavor {flavor['name']!r} of {where} asks for PCI alias {g.alias}, whose devices "
        "nova.conf configures: Placement does not show their capacity."
        for g in flavor["gpus"]
        if g.kind == "passthrough"
    ]
    out = [finding("DST_GPU_CAPACITY_UNKNOWN", " ".join(unknown))] if unknown else []
    if not vgpus or capacity.status != "ok" or shares.get(vm.source_id) == "nothing":
        return out
    # the units each pool, and each class across its pools, is asked for by the plan's VMs
    asked: dict[PoolKey, int] = {}
    askers: dict[PoolKey, set[str]] = {}
    for other in selected:
        other_flavor = server_gpu_flavor(other, plan, dst_inv.gpu_flavors, kind)
        if other_flavor is None or shares.get(other.source_id) == "nothing":
            continue
        for key, units in gpu_units(other_flavor["gpus"]).items():
            asked[key] = asked.get(key, 0) + units
            askers.setdefault(key, set()).add(other.name)
    reasons = []
    for short in gpu_shortfalls(vgpus, asked, capacity.pools):
        if short.kind == "refused":
            reasons.append(
                f"Placement refuses the request for {short.pool} of {where} ({short.detail}): no "
                f"server is created with flavor {flavor['name']!r}."
            )
        elif short.kind == "group":
            reasons.append(
                f"Its flavor asks {short.pool} of {where} for {short.needed} units on one "
                f"provider, and none has {short.needed} free (the most on one is {short.free})."
            )
        else:
            across = " across its pools" if short.kind == "class" else ""
            reasons.append(
                f"The plan's VMs ask {short.pool} of {where} for {_count(short.needed, 'unit')}"
                f"{across}, more than its {short.free} free ({_first_names(askers[short.key])})."
            )
    if reasons:
        out.append(finding("DST_GPU_CAPACITY_INSUFFICIENT", " ".join(dict.fromkeys(reasons))))
    return out


def _gpu_findings(
    vm: VMRef,
    plan: Plan,
    dst_inv: DestinationInventory,
    source: Provider | None,
    where: str,
    selected: Sequence[VMRef],
    shares: Mapping[str, QuotaShare],
) -> list[Finding]:
    """§9.3's GPU rows for ``vm`` (SDD §22.4, §22.5): a GPU VM is blocked until a GPU mapping
    applies to it, then told of its GPU flavor and any downsizing; every GPU VM of its guest's
    driver and a vGPU's license; and a VM whose server is created with a GPU flavor of the
    destination's capacity for it."""
    gpus, declared = _vm_gpus(vm, source)
    kind = _kind_of(vm, source.kind if source is not None else None)
    out: list[Finding] = []
    flavor = None
    if gpus:
        mapping = _gpu_mapping(vm, plan, gpus, kind, dst_inv, where)
        flavor = mapping.flavor
        if mapping.why is not None:
            out += _gpu_blockers(vm, gpus, declared, mapping.why)
        if flavor is not None:
            out += _gpu_flavor_findings(vm, gpus, flavor, where)
        out += _guest_gpu_findings(gpus, flavor, kind, where)
    else:
        flavor = server_gpu_flavor(vm, plan, dst_inv.gpu_flavors, kind)
    if flavor is not None:
        out += _capacity_findings(vm, flavor, plan, dst_inv, kind, where, selected, shares)
    return out


def _constrained(flavor: dict[str, Any]) -> bool:
    specs = flavor.get("extra_specs") or {}
    return any(_CONSTRAINED_SPEC.match(str(k)) for k in specs)


def _hw_specs(vm: VMRef) -> list[str]:
    """Source ``hw:*`` extra specs (CPU pinning, huge pages, NUMA) an auto-matched flavor drops."""
    return sorted(k for k in vm.flavor_extra_specs if str(k).startswith("hw:"))


def fitting_flavor(vm: VMRef, dst_inv: DestinationInventory) -> dict[str, Any] | None:
    """The destination flavor to use when the plan maps none (SDD §9.3 ``MAP_FLAVOR_*``).

    A same-named flavor that fits wins; otherwise the smallest fitting flavor (by vCPUs, RAM,
    root disk, name) among those without scheduler/PCI/vGPU/aggregate/trait extra specs.
    ``None`` when nothing fits.
    """
    root_gb = _local_root_gb(vm)
    fits = [
        f
        for f in dst_inv.flavors
        if f.get("name") and _flavor_fits(f, vm, root_gb) and not _constrained(f)
    ]
    if not fits:
        return None
    same = next((f for f in fits if f["name"] == vm.flavor), None)
    return same or min(fits, key=_flavor_size)


def flavor_prestaged(vm: VMRef, plan: Plan, dst_inv: DestinationInventory) -> bool:
    """Whether pre-staging creates ``vm``'s flavor (SDD §7.2, §9.3 ``MAP_FLAVOR_PRESTAGED``):
    "flavors" is pre-staged and the flavor has no mapping and no same-named destination flavor
    (an existing destination flavor is never pre-staged over) - and never a GPU VM's, whose GPU
    type may not exist at the destination (§22.4)."""
    return bool(
        vm.flavor
        and not vm_gpus(vm)
        and "flavors" in plan.prestage_resources
        and vm.flavor not in plan.mappings.flavors
        and vm.flavor not in {str(f.get("name")) for f in dst_inv.flavors if f.get("name")}
    )


def keypair_missing(vm: VMRef, dst_inv: DestinationInventory) -> bool:
    """Whether ``vm`` boots with a keypair the destination credential lacks (SDD §9.3)."""
    return bool(vm.key_name and vm.key_name not in dst_inv.keypairs)


def keypair_prestaged(
    vm: VMRef, plan: Plan, src_inv: SourceInventory, dst_inv: DestinationInventory
) -> bool:
    """Whether pre-staging imports ``vm``'s keypair (SDD §7.2, ``MAP_KEYPAIR_PRESTAGED``):
    "keypairs" is pre-staged, the destination lacks it and the source credential owns it."""
    return bool(
        keypair_missing(vm, dst_inv)
        and "keypairs" in plan.prestage_resources
        and vm.key_name in src_inv.keypairs
    )


def resolve_mappings(vm: VMRef, plan: Plan, dst_inv: DestinationInventory) -> Mappings:
    """Mappings preflight matched automatically for ``vm`` (``Migration.resolved_mappings``).

    0.1.0 resolves flavors only: when the plan has no mapping for the VM's flavor and no
    same-named destination flavor fits, the smallest fitting flavor is recorded - never for a GPU
    VM, whose GPU mapping owns its flavor (SDD §22.4).
    """
    out = Mappings()
    if vm_gpus(vm):
        return out
    if (
        vm.flavor
        and vm.flavor not in plan.mappings.flavors
        and not flavor_prestaged(vm, plan, dst_inv)
    ):
        chosen = fitting_flavor(vm, dst_inv)
        if chosen is not None and chosen["name"] != vm.flavor:
            out.flavors[vm.flavor] = str(chosen["name"])
    return out


def _names(items: Iterable[str]) -> str:
    return ", ".join(sorted(set(items)))


def _canonical_ip(address: str) -> str:
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        return address


def _in_subnets(address: str, cidrs: Sequence[str]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def _network_list(dst_inv: DestinationInventory) -> list[dict[str, Any]]:
    """The networks the destination credential sees (SDD §10); an inventory from before
    network_details lists each name once, as the credential's own network."""
    if dst_inv.network_details:
        return dst_inv.network_details
    return [
        {"id": None, "name": name, "mtu": mtu, "project_id": None, "own": True, "shared": False}
        for name, mtu in dst_inv.networks.items()
    ]


def _subnets_of(dst_inv: DestinationInventory, network: dict[str, Any]) -> list[dict[str, Any]]:
    """A resolved network's subnets: by its id, else (an inventory from before it) by name."""
    if network.get("id") is not None:
        return [s for s in dst_inv.subnets if s.get("network_id") == network["id"]]
    return [s for s in dst_inv.subnets if s.get("network") == network["name"]]


def _no_host_address(address: str, cidrs: Sequence[str]) -> str | None:
    """What makes ``address`` no host address of the subnets that hold it, as Neutron's
    check_subnet_ip rules for a port created with no owner (SDD §9.3): an IPv4 subnet's network
    address at any prefix length, its broadcast address below a /31 (netaddr gives a /31 or /32
    none); an IPv6 subnet's first address is taken. None for a host address of one of them, or an
    address none of them holds."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    why = None
    for cidr in cidrs:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if ip not in net:
            continue
        if net.version == 4 and ip == net.network_address:
            why = why or f"the network address of {net}"
        elif net.version == 4 and net.prefixlen < 31 and ip == net.broadcast_address:
            why = why or f"the broadcast address of {net}"
        else:
            return None
    return why


def _nic_override_findings(
    vm: VMRef,
    plan: Plan,
    dst_inv: DestinationInventory,
    selected: Sequence[VMRef],
    source: Provider | None,
    where: str = "the destination",
) -> list[Finding]:
    """MAP_NIC_OVERRIDE_INVALID / MAP_NIC_OVERRIDE (SDD §9.3): every entry checked before
    anything stops - its MAC, its network, its addresses host addresses of the target network's
    subnets and given to no other NIC of the plan."""
    entries = plan.nic_overrides.get(vm.source_id) or {}
    if not entries:
        return []
    if source is not None and source.kind == ProviderKind.vmware:
        return [
            finding(
                "MAP_NIC_OVERRIDE_INVALID",
                f"{vm.name} is a VMware VM: the migration kit keeps the guest's network "
                f"settings, so its NIC overrides cannot be applied ({_names(sorted(entries))}).",
            )
        ]
    # every address the plan's NICs get, on the network each goes to
    holders: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for other in selected:
        for n in other.nics:
            network, addresses = plan.nic_target(other.source_id, n)
            for a in addresses:
                key = (network, _canonical_ip(a))
                holders.setdefault(key, []).append(
                    (other.source_id, (n.mac or "").lower(), other.name)
                )
    nics = {n.mac.lower(): n for n in vm.nics if n.mac}
    problems: list[str] = []
    applied: list[str] = []
    for mac in sorted(entries):
        nic = nics.get(mac)
        if nic is None:
            problems.append(f"{mac}: the VM has no NIC with this MAC")
            continue
        network, addresses = plan.nic_target(vm.source_id, nic)
        if network not in dst_inv.networks:
            # the entry's network, or the mapped one pre-staging would create later: its subnets
            # cannot be checked before anything stops
            problems.append(
                f"{mac}: {where} has no network {network!r}"
                + (
                    ""
                    if entries[mac].network
                    else " yet: pre-staging would create it, but its subnets cannot be checked "
                    f"before; name an existing network of {where}"
                )
            )
            continue
        resolved, _ = resolve_network(_network_list(dst_inv), network)
        if resolved is None:
            # MAP_NETWORK_AMBIGUOUS names it
            continue
        subnets = _subnets_of(dst_inv, resolved)
        cidrs = [str(s.get("cidr")) for s in subnets]
        outside = [a for a in addresses if not _in_subnets(a, cidrs)]
        if outside:
            verb = "lies" if len(outside) == 1 else "lie"
            problems.append(f"{mac}: {', '.join(outside)} {verb} in no subnet of {network!r}")
        neutron = plan.port_creation_mode == "neutron"
        for a in addresses:
            reserved = _no_host_address(a, cidrs)
            if reserved:
                problems.append(f"{mac}: {a} is {reserved}, not a host address")
            elif a not in outside:
                # on a SLAAC or DHCPv6-stateless subnet only what Neutron derives (§6.6)
                why = address_refusal(a, subnets, neutron=neutron, mac=mac)
                if why:
                    problems.append(f"{mac}: {a} {why}")
        for a in addresses:
            for vm_id, other_mac, name in holders.get((network, _canonical_ip(a)), []):
                if (vm_id, other_mac) != (vm.source_id, mac):
                    problems.append(f"{mac}: {a} also goes to {name} ({other_mac})")
        applied.append(f"{mac} ({nic.network}) -> {network}: {', '.join(addresses)}")
    if problems:
        return [
            finding(
                "MAP_NIC_OVERRIDE_INVALID",
                f"NIC override(s) of {vm.name} cannot be applied: {'; '.join(problems)}.",
            )
        ]
    if not applied:
        return []
    return [finding("MAP_NIC_OVERRIDE", f"NIC override(s): {'; '.join(applied)}.")]


def run_preflight(
    vm: VMRef,
    plan: Plan,
    src_inv: SourceInventory,
    dst_inv: DestinationInventory,
    all_vms: Sequence[VMRef] | None = None,
    *,
    source: Provider | None = None,
    destination: Provider | None = None,
    dst_caps: Mapping[str, Any] | None = None,
    shares: Mapping[str, QuotaShare] | None = None,
    dns_ttl: int | None = None,
    lb_pools: Mapping[str, PoolPair] | None = None,
) -> list[Finding]:
    """Evaluate the SDD §9.3 catalog for ``vm`` within ``plan``.

    ``all_vms`` are the plan's selected VMs (for duplicate names and cumulative quotas);
    ``shares`` what a cutover of each one will still create, by source id - all of it for a VM
    without one (``quota_share``; the quota counts what a cutover will still create);
    ``source``/``destination`` enable the conversion-host check; ``dst_caps`` are the
    destination's capabilities as validation's check just reported them (its token's roles);
    ``dns_ttl`` the TTL of the VM's DNS target's record as validation read it, None unread (§8.4);
    ``lb_pools`` each mapped pair of Octavia pools as validation read it, by source pool id (§8.5).
    """
    selected = list(all_vms) if all_vms is not None else [vm]
    out: list[Finding] = []
    maps = plan.mappings
    # the destination provider a message about the destination cloud names (SDD §1 naming, §9.3)
    where = destination.label if destination is not None else "the destination"

    if vm.power_state == "error":
        out.append(finding("SRC_VM_ERROR_STATE", f"Source VM {vm.name} is in error state."))
    if vm.power_state == "transitioning" or vm.task_state:
        # a task Nova runs whatever the status - an image upload under ACTIVE, a stop in flight, a
        # delete of a server in error - is named (SDD §9.3, review PV-05)
        task = vm.task_state or "resize, migration, rescue, rebuild or reboot"
        out.append(
            finding(
                "SRC_VM_TRANSITIONAL_STATE",
                f"Source VM {vm.name} has a Nova task in flight ({task}); a stop or snapshot "
                "would fail.",
            )
        )

    # the migration kit names a VMware VM's server, ports and volumes after its name as it writes
    # it, and finds a volume again by that name: from a VMware source the kit's names decide, the
    # same name included (SDD §7.2, §9.3, reviews RV26-04, RV28-04)
    kit = (
        kit_vm_name(vm.name) if source is not None and source.kind == ProviderKind.vmware else None
    )
    twins = [
        v for v in selected if v.source_id != vm.source_id and kit is None and v.name == vm.name
    ]
    kin = [
        v for v in selected if v.source_id != vm.source_id and kit and kit_vm_name(v.name) == kit
    ]
    if twins:
        # never `where`: every later message names the destination provider by it (§1 naming)
        others = _names(f"{t.project or '-'}/{t.source_id}" for t in twins)
        out.append(
            finding(
                "SRC_VM_DUPLICATE_NAME",
                f"Another selected VM is also named {vm.name!r} ({others}); os-migrate selects "
                "workloads by name.",
            )
        )
    elif kin:
        others = _names(f"{t.project or '-'}/{t.source_id}" for t in kin)
        out.append(
            finding(
                "SRC_VM_DUPLICATE_NAME",
                f"Another selected VM has the name {kit!r} as the migration kit writes it "
                f"({others}); the kit names the VM's server, ports and volumes after it.",
                remediation=KIT_TWIN_REMEDIATION,
            )
        )
    if kit == "":
        out.append(
            finding(
                "SRC_VM_NAME_UNUSABLE",
                f"The migration kit keeps no character of the name {vm.name!r} - only Latin "
                "letters, digits, '_' and '-' after its transliteration - so it would create the "
                "VM's server, ports and volumes without a name.",
            )
        )

    multi = [d.name or d.id for d in vm.disks if d.multiattach]
    if multi:
        out.append(finding("SRC_VM_MULTIATTACH", f"Multi-attach disk(s): {_names(multi)}."))

    root = vm.root_disk()
    if root is not None and root.kind in ("image_root", "ephemeral"):
        out.append(
            finding("SRC_VM_EPHEMERAL_ROOT", f"The root disk is {root.kind.replace('_', ' ')}.")
        )

    out.extend(_stack_findings(vm, plan, src_inv.stacks, source))
    out.extend(_member_findings(vm, plan, src_inv.lb_members, source))
    out.extend(_lb_pool_findings(vm, plan, src_inv.lb_members, lb_pools))

    # a NIC its override sends to another network is checked by MAP_NIC_OVERRIDE_INVALID
    mapped_nics = [
        nic
        for nic in vm.nics
        if not (plan.nic_override(vm.source_id, nic.mac) or NicOverride()).network
    ]
    missing_nets = [
        nic.network
        for nic in mapped_nics
        if nic.network not in maps.networks and nic.network not in dst_inv.networks
    ]
    # a mapped target must exist: nothing creates a network under the *mapped* name
    bad_net_targets = [
        f"{nic.network} -> {maps.networks[nic.network]}"
        for nic in mapped_nics
        if nic.network in maps.networks and maps.networks[nic.network] not in dst_inv.networks
    ]
    # pre-staging creates same-named networks for OpenStack sources only: the VMware kit has
    # no prestage step (SDD §4.2 / §7.2)
    prestaged = "networks" in plan.prestage_resources and (
        source is None or source.kind != ProviderKind.vmware
    )
    if missing_nets and prestaged:
        out.append(
            finding(
                "MAP_NETWORK_PRESTAGED",
                f"Pre-staging creates same-named network(s) in {where}: {_names(missing_nets)}.",
            )
        )
    elif missing_nets:
        out.append(
            finding(
                "MAP_NETWORK_MISSING",
                f"No mapping or same-named network in {where} for: {_names(missing_nets)}."
                + (
                    " VMware sources are not pre-staged: map every port group."
                    if source is not None and source.kind == ProviderKind.vmware
                    else ""
                ),
            )
        )
    if bad_net_targets:
        out.append(
            finding(
                "MAP_NETWORK_MISSING",
                f"Network mapping target(s) do not exist in {where}: {_names(bad_net_targets)}.",
                remediation="Fix the mapping: the destination network must exist.",
            )
        )

    # every NIC's RHOSO network as the cutover resolves its name (SDD §7.3 step 0, §9.3); a VMware
    # NIC's too: Seamless gives the migration kit the network it resolves (§7.2)
    openstack_source = source is None or source.kind != ProviderKind.vmware
    seen = _network_list(dst_inv)
    resolved: dict[int, dict[str, Any]] = {}
    ambiguous: list[str] = []
    for nic in vm.nics:
        # the VMware kit keeps the guest's network settings: no NIC override moves its NICs (§7.2)
        target = (
            plan.nic_target(vm.source_id, nic)[0]
            if openstack_source
            else maps.networks.get(nic.network, nic.network)
        )
        network, why = resolve_network(seen, target)
        if network is not None:
            resolved[id(nic)] = network
        elif why:
            ambiguous.append(f"{(nic.mac or '?').lower()} ({nic.network}): {why}")
    if ambiguous:
        out.append(
            finding(
                "MAP_NETWORK_AMBIGUOUS",
                f"The cutover cannot tell these networks of {where} apart: "
                + "; ".join(ambiguous)
                + ".",
            )
        )

    # whether the credential may put each NIC's port on its resolved network: another project's
    # takes it only when shared or external, and from a role the destination's policy profile
    # counts (SDD §6.6, §9.3, §21.5); the VMware migration kit creates every port with the NIC's
    # MAC (§7.2)
    roles = (dst_caps or {}).get("roles")
    profile = policy_profile_of(dst_caps, destination)
    denied = []
    for nic in vm.nics:
        network = resolved.get(id(nic))
        why = None if network is None else port_refusal(network, roles, profile, where=where)
        if network is not None and why:
            denied.append(
                f"{(nic.mac or '?').lower()} on {network['name']!r} "
                f"(project {network.get('project_id')}): {why}"
            )
    if denied:
        out.append(
            finding(
                "NET_PORT_NOT_PERMITTED",
                f"The credential of {where} may not put these NICs on their network: "
                + "; ".join(denied)
                + ".",
                remediation=_port_remediation(profile),
            )
        )

    if keypair_prestaged(vm, plan, src_inv, dst_inv):
        out.append(
            finding(
                "MAP_KEYPAIR_PRESTAGED",
                f"Pre-staging imports keypair {vm.key_name!r} for the migration user of {where}.",
            )
        )
    elif keypair_missing(vm, dst_inv):
        owned = vm.key_name in src_inv.keypairs
        out.append(
            finding(
                "DST_KEYPAIR_MISSING",
                f"The VM boots with keypair {vm.key_name!r}, which the migration user of {where} "
                "does "
                "not have: the cutover would stop at its destination check."
                + (
                    " Pre-stage keypairs to import it."
                    if owned
                    else " Another user owns it at the source, so pre-staging cannot export it: "
                    f"create it, its public key, for the migration user of {where}."
                ),
            )
        )

    dst_flavor_names = {str(f.get("name")) for f in dst_inv.flavors if f.get("name")}
    kind = _kind_of(vm, source.kind if source is not None else None)
    if vm_gpus(vm):
        # the GPU mapping owns a GPU VM's flavor: nothing is matched or pre-staged, and only a
        # mapped flavor that does not exist is missing (SDD §22.4)
        missing = _missing_gpu_targets(vm, plan, kind, dst_flavor_names, where)
        if missing:
            out.append(
                finding(
                    "MAP_FLAVOR_MISSING",
                    " ".join(missing),
                    remediation="Fix the mapping: the destination flavor must exist.",
                )
            )
    elif flavor_prestaged(vm, plan, dst_inv) and (
        source is None or source.kind != ProviderKind.vmware
    ):
        out.append(
            finding(
                "MAP_FLAVOR_PRESTAGED",
                f"Pre-staging creates flavor {vm.flavor!r} in {where} with the source's size and "
                "extra specs.",
            )
        )
    elif vm.flavor and vm.flavor in maps.flavors:
        target = maps.flavors[vm.flavor]
        if target not in dst_flavor_names:
            out.append(
                finding(
                    "MAP_FLAVOR_MISSING",
                    f"Flavor {vm.flavor!r} is mapped to {target!r}, which does not exist in "
                    f"{where}.",
                    remediation="Fix the mapping: the destination flavor must exist.",
                )
            )
    else:
        root_gb = _local_root_gb(vm)
        chosen = fitting_flavor(vm, dst_inv)
        if chosen is None:
            out.append(
                finding(
                    "MAP_FLAVOR_MISSING",
                    f"No mapping for flavor {vm.flavor!r} and no flavor of {where} with "
                    f">= {vm.vcpus} "
                    f"vCPUs, >= {vm.ram_mb} MB RAM and >= {root_gb} GB root disk (flavors with "
                    "PCI, vGPU, trait or aggregate extra specs are never matched automatically).",
                )
            )
        elif vm.flavor is not None and chosen["name"] != vm.flavor:
            # VMware VMs carry no flavor: the migration kit sizes the server itself, so an
            # auto-match finding would name a flavor nothing uses (only the blocker applies)
            vcpus, ram_mb, disk_gb, name = _flavor_size(chosen)
            dropped = _hw_specs(vm)
            out.append(
                finding(
                    "MAP_FLAVOR_AUTO",
                    f"No mapping for flavor {vm.flavor!r}: the smallest fitting flavor of {where}, "
                    f"{name!r} ({vcpus} vCPUs, {ram_mb} MB RAM, {disk_gb} GB disk), will be used"
                    + (
                        f"; the source flavor's {', '.join(dropped)} are not carried over."
                        if dropped
                        else "."
                    ),
                    severity=Severity.warning if dropped else None,
                    remediation=(
                        "Map the flavor to a destination flavor with the same hw:* extra specs."
                        if dropped
                        else None
                    ),
                )
            )

    missing_types = [
        d.volume_type
        for d in vm.disks
        if d.volume_type
        and d.volume_type not in maps.volume_types
        and d.volume_type not in dst_inv.volume_types
    ]
    bad_type_targets = [
        f"{d.volume_type} -> {maps.volume_types[d.volume_type]}"
        for d in vm.disks
        if d.volume_type
        and d.volume_type in maps.volume_types
        and maps.volume_types[d.volume_type] not in dst_inv.volume_types
    ]
    if bad_type_targets:
        out.append(
            finding(
                "MAP_VOLUME_TYPE_MISSING",
                f"Volume type mapping target(s) do not exist in {where}: "
                f"{_names(bad_type_targets)}.",
                severity=Severity.blocker,
                remediation="Fix the mapping: the destination volume type must exist.",
            )
        )
    if missing_types and maps.volume_types:
        # SDD §6.4: with mapped volume types the executor preserves them, so an
        # unmapped type would fail volume creation after the source was stopped.
        out.append(
            finding(
                "MAP_VOLUME_TYPE_MISSING",
                f"No mapping or same-named volume type in {where} for: {_names(missing_types)}; "
                f"the plan maps volume types, so the default volume type of {where} cannot be "
                "used.",
                severity=Severity.blocker,
                remediation="Add a volume type mapping for it (the plan preserves volume types).",
            )
        )
    elif missing_types:
        out.append(
            finding(
                "MAP_VOLUME_TYPE_MISSING",
                f"No mapping or same-named volume type in {where} for: {_names(missing_types)}.",
            )
        )

    # the projects the destination inventory lists, never the quota table, which the provider
    # leaves empty when its quota calls fail (SDD §9.3, review PL-06)
    if dst_inv.projects:
        mapped_project = maps.projects.get(vm.project or "")
        if mapped_project is not None and mapped_project not in dst_inv.projects:
            out.append(
                finding(
                    "DST_PROJECT_MISSING",
                    f"Project {vm.project!r} is mapped to {mapped_project!r}, which {where} "
                    "does not list.",
                )
            )
        elif (
            vm.project  # VMware VMs report none: the kit charges the credential's project
            and mapped_project is None
            and vm.project not in dst_inv.projects
            and len(dst_inv.projects) != 1
        ):
            out.append(
                finding(
                    "DST_PROJECT_MISSING",
                    f"No destination project for {vm.project!r}: no mapping, no same-named "
                    f"project, and {where} has {len(dst_inv.projects)} projects.",
                )
            )
    shortfall = _quota_shortfall(vm, plan, dst_inv, selected, shares or {}, kind)
    if shortfall is not None:
        project, short = shortfall
        out.append(
            finding(
                "DST_QUOTA_INSUFFICIENT",
                f"Project {project!r} of {where} lacks quota for the plan's VMs: "
                + "; ".join(short)
                + ".",
            )
        )

    shrinks = []
    for nic in vm.nics:
        src_mtu = nic.mtu if nic.mtu is not None else src_inv.networks.get(nic.network)
        target = plan.nic_target(vm.source_id, nic)[0]
        dst_mtu = (
            resolved[id(nic)].get("mtu") if id(nic) in resolved else dst_inv.networks.get(target)
        )
        if src_mtu is not None and dst_mtu is not None and dst_mtu < src_mtu:
            shrinks.append(
                f"{nic.network} {src_mtu} -> {dst_mtu}"
                if target == maps.networks.get(nic.network, nic.network)
                else f"{nic.network} {src_mtu} -> {target} {dst_mtu}"
            )
    if shrinks:
        out.append(finding("NET_MTU_SHRINK", f"The MTU in {where} is smaller: {_names(shrinks)}."))

    sriov = [nic.network for nic in vm.nics if nic.vnic_type in _SRIOV_VNIC_TYPES]
    if sriov:
        out.append(finding("NET_SRIOV_PORT", f"SR-IOV/macvtap port(s) on: {_names(sriov)}."))

    # through Nova os-migrate recreates each port with exactly one fixed IP; the handover keeps one
    # per port (SDD §9.2, §9.3). Through Neutron every address is kept, so nothing is said; VMware
    # NICs go through the migration kit.
    # the addresses a NIC gets: its override's, else its own (SDD §9.2)
    addresses = {id(nic): plan.nic_target(vm.source_id, nic)[1] for nic in vm.nics}
    odd_ports = [nic for nic in vm.nics if len(addresses[id(nic)]) != 1]
    if (
        odd_ports
        and plan.port_creation_mode == "nova"
        and (source is None or source.kind != ProviderKind.vmware)
    ):
        listed = ", ".join(
            f"{n.network} ({', '.join(addresses[id(n)]) or 'no fixed IP'})" for n in odd_ports
        )
        out.append(
            finding(
                "NET_PORT_IPS",
                f"NIC(s) without exactly one fixed IP: {listed}. Cold and warm migration cannot "
                "recreate them; storage handover keeps one address per port.",
            )
        )

    # a NIC without an override keeps its own addresses: Neutron must take them on its resolved
    # network (an override's are MAP_NIC_OVERRIDE_*'s); a network pre-staging creates, or an
    # inventory without subnets, cannot be checked (SDD §9.3, §6.6)
    if openstack_source and dst_inv.subnets:
        neutron = plan.port_creation_mode == "neutron"
        refused: list[str] = []
        for nic in vm.nics:
            network = resolved.get(id(nic))
            if network is None or plan.nic_override(vm.source_id, nic.mac) is not None:
                continue
            subnets = _subnets_of(dst_inv, network)
            for address in nic.fixed_ips if neutron else nic.fixed_ips[:1]:
                why = address_refusal(address, subnets, neutron=neutron, mac=nic.mac)
                if why:
                    refused.append(
                        f"{(nic.mac or '?').lower()} on {network['name']!r}: {address} {why}"
                    )
        if refused:
            out.append(
                finding(
                    "NET_ADDRESS_REFUSED",
                    f"Neutron would refuse these addresses on their network in {where}: "
                    + "; ".join(refused)
                    + ".",
                )
            )
    # the VMware migration kit sends a NIC's IPv4 guest addresses only, all with one subnet of its
    # network (SDD §7.2, §9.3)
    if not openstack_source and dst_inv.subnets:
        refused = []
        for nic in vm.nics:
            network = resolved.get(id(nic))
            if network is None:
                continue
            _, why = kit_subnet(nic, _subnets_of(dst_inv, network))
            if why:
                refused.append(f"{(nic.mac or '?').lower()} on {network['name']!r}: {why}")
        if refused:
            out.append(
                finding(
                    "NET_ADDRESS_REFUSED",
                    f"Neutron would refuse these addresses on their network in {where}: "
                    + "; ".join(refused)
                    + ".",
                )
            )

    out.extend(_nic_override_findings(vm, plan, dst_inv, selected, source, where))
    if openstack_source:
        out.extend(_port_findings(vm, plan, dst_inv, source, where))
        out.extend(_fip_keep_findings(vm, plan, dst_inv, selected, destination, dst_caps, where))
    out.extend(_prefix_split(vm, plan, selected, source, destination))
    out.extend(_pool_split(vm, plan, src_inv.lb_members, selected, source))

    out.extend(_gpu_findings(vm, plan, dst_inv, source, where, selected, shares or {}))

    encrypted = [d.name or d.id for d in vm.disks if d.encrypted]
    if encrypted:
        out.append(finding("VOL_ENCRYPTED", f"Encrypted disk(s): {_names(encrypted)}."))

    guest = vm.guest_os
    if guest.lifecycle == "legacy":
        out.append(
            finding("GUEST_OS_LEGACY", f"Guest OS {guest.label} is out of standard vendor support.")
        )
    if guest.family == "unknown":
        shown = f" ({vm.os_type})" if vm.os_type else ""
        out.append(finding("GUEST_OS_UNKNOWN", f"The guest OS is not identified{shown}."))
    vmware = (source is not None and source.kind == ProviderKind.vmware) or any(
        d.kind == "vmdk" for d in vm.disks
    )
    if vmware and guest.v2v in ("tech_preview", "unverified"):
        level = (
            "a Technology Preview" if guest.v2v == "tech_preview" else "not supported by Red Hat"
        )
        out.append(
            finding(
                "GUEST_CONVERSION_UNVERIFIED",
                f"Converting {guest.label} with virt-v2v is {level}.",
            )
        )
    if vmware and guest.v2v == "unsupported":
        out.append(
            finding(
                "GUEST_CONVERSION_UNSUPPORTED",
                f"virt-v2v on the RHEL 9 conversion host cannot prepare {guest.label}.",
            )
        )

    if vm.cbt_enabled is False:
        out.append(finding("VMW_CBT_DISABLED", "Changed Block Tracking is disabled."))
    independent = [d.name or d.id for d in vm.disks if d.independent]
    if independent:
        out.append(finding("VMW_INDEPENDENT_DISK", f"Independent disk(s): {_names(independent)}."))
    if vm.snapshot_count > 0:
        out.append(finding("VMW_SNAPSHOTS_PRESENT", f"The VM has {vm.snapshot_count} snapshot(s)."))
    if vm.tools_ok is False:
        out.append(finding("VMW_TOOLS_MISSING", "VMware Tools are not running."))

    if source is not None and destination is not None:
        lacking: list[str] = []
        if source.kind == ProviderKind.vmware:
            if destination.conversion_host is None:
                lacking.append(f"destination {destination.label}")
        else:
            if source.conversion_host is None:
                lacking.append(f"source {source.id}")
            if destination.conversion_host is None:
                lacking.append(f"destination {destination.label}")
        if lacking:
            out.append(
                finding(
                    "CONV_HOST_MISSING",
                    f"No conversion host configured on: {', '.join(lacking)}.",
                )
            )

    if destination is not None and dst_caps:
        out.extend(_destination_capabilities(destination, dst_caps))
        out.extend(_release_outside_support(destination, dst_caps))

    if plan.handover.enabled:
        unmapped = [
            d.volume_type or "<default>"
            for d in vm.disks
            if d.kind == "volume" and (d.volume_type or "") not in plan.handover.backend_map
        ]
        if unmapped:
            out.append(
                finding(
                    "HANDOVER_BACKEND_UNMAPPED",
                    f"Volume type(s) without a handover backend: {_names(unmapped)}.",
                )
            )
    missing = _app_probe_missing(vm, plan)
    if missing is not None:
        out.append(missing)
    target = plan.network_cutover.dns_targets.get(vm.source_id)
    if target is not None and dns_ttl is not None and dns_ttl > plan.downtime_slo_s:
        # imported here: dns_swap imports the providers, which import this module
        from ..dns_swap import record_name

        out.append(
            finding(
                "NET_DNS_TTL_LONG",
                f"The DNS record {record_name(target)} holds a TTL of {dns_ttl} s, longer than "
                f"the plan's downtime SLO of {plan.downtime_slo_s} s: after the swap, clients may "
                f"reach the old address for up to {dns_ttl} s.",
            )
        )
    return out


def _source_label(source: Provider | None) -> str:
    return source.label if source is not None else "the source"


def _unread(service: str, read: StackRead | MemberRead) -> str:
    """Why a read of the source's Heat or Octavia is unknown (SDD §10): by its HTTP status or the
    failure's kind, never its message text."""
    if read.reason == "forbidden":
        return f"{service} refused the source credential ({read.detail or 'HTTP 403'})"
    return f"its read failed ({read.detail or 'no answer'})"


def _stack_findings(
    vm: VMRef, plan: Plan, stacks: StackRead, source: Provider | None
) -> list[Finding]:
    """SRC_VM_HEAT_STACK, SRC_VM_AUTOSCALED and SRC_VM_HEAT_UNKNOWN (SDD §9.3, plan task N2): the
    VM's place in the source's Heat stacks, or why it is not known; an autoscaling group's member
    is blocked until the plan acknowledges it (owner decision 9)."""
    at = _source_label(source)
    if stacks.status == "unknown":
        return [
            finding(
                "SRC_VM_HEAT_UNKNOWN",
                f"Whether {vm.name} belongs to a Heat stack or an autoscaling group at {at} is not "
                f"known: {_unread('Heat', stacks)}.",
            )
        ]
    place = stacks.servers.get(vm.source_id)
    if place is None:
        return []
    out = [
        finding(
            "SRC_VM_HEAT_STACK",
            f"{vm.name} belongs to Heat stack {place.stack!r} at {at} (its resource "
            f"{place.resource!r}): the stack is not moved, and a stack update there may recreate "
            "the server.",
        )
    ]
    if place.group is not None:
        text = (
            f"{vm.name} is a member of autoscaling group {place.group!r} ({place.group_type}) of "
            f"Heat stack {place.stack!r} at {at}: the group may replace the server once the "
            "cutover stops it, which leaves two live copies."
        )
        if "SRC_VM_AUTOSCALED" in plan.acknowledged_findings.get(vm.source_id, []):
            out.append(
                finding(
                    "SRC_VM_AUTOSCALED",
                    f"{text} The plan acknowledges it (acknowledged_findings).",
                    severity=Severity.warning,
                    remediation=AUTOSCALED_ACKNOWLEDGED_REMEDIATION,
                )
            )
        else:
            out.append(finding("SRC_VM_AUTOSCALED", text))
    return out


def _memberships(vm: VMRef, members: Sequence[PoolMember]) -> list[PoolMember]:
    """The VM's memberships of the source's Octavia pools (SDD §9.3): each of ``members`` that is
    an address of a NIC of the VM, on the NIC's network when the read names the member's."""
    held: list[PoolMember] = []
    for nic in vm.nics:
        addresses = {_canonical_ip(a) for a in nic.fixed_ips}
        held += [
            m
            for m in members
            if _canonical_ip(m.address) in addresses and m.network in (None, nic.network)
        ]
    return held


def _member_findings(
    vm: VMRef, plan: Plan, read: MemberRead, source: Provider | None
) -> list[Finding]:
    """SRC_VM_LB_MEMBER and SRC_VM_LB_UNKNOWN (SDD §9.3, plan task N2): each Octavia pool member at
    the source that is an address of the VM's NIC - on the NIC's network when the read names the
    member's -, or why it is not known; a member of a pool the plan maps is the helper's (§8.5)."""
    at = _source_label(source)
    if read.status == "unknown":
        return [
            finding(
                "SRC_VM_LB_UNKNOWN",
                f"Whether addresses of {vm.name} are members of Octavia pools at {at} is not "
                f"known: {_unread('Octavia', read)}.",
            )
        ]
    mapped = plan.network_cutover.lb_pools
    held = [
        f"{_canonical_ip(m.address)} port {m.port} in pool {m.pool!r} of load balancer "
        f"{m.load_balancer!r}"
        for m in _memberships(vm, read.members)
        if m.pool_id not in mapped
    ]
    if not held:
        return []
    return [
        finding(
            "SRC_VM_LB_MEMBER",
            f"Addresses of {vm.name} are members of Octavia pools at {at}: "
            f"{'; '.join(dict.fromkeys(held))}. The memberships are not moved.",
        )
    ]


def _lb_pool_findings(
    vm: VMRef, plan: Plan, read: MemberRead, pairs: Mapping[str, PoolPair] | None
) -> list[Finding]:
    """MAP_LB_POOL_INCOMPATIBLE (SDD §9.3, §8.5, plan task N9): each membership of a pool the plan
    maps whose destination pool's provider lacks what the source pool uses - OVN's L4 limits; a
    pair either read could not see is not judged."""
    # imported here: the helper imports the providers, which import this module
    from ..lb_helper import lacks

    out: list[Finding] = []
    seen: set[str] = set()
    for m in _memberships(vm, read.members):
        pair = (pairs or {}).get(m.pool_id)
        if m.pool_id not in plan.network_cutover.lb_pools or m.pool_id in seen or pair is None:
            continue
        seen.add(m.pool_id)
        if pair.source is None or pair.destination is None:
            continue
        missing = lacks(pair.source, pair.destination)
        if not missing:
            continue
        features = ", ".join(missing[:-1]) + (" and " if len(missing) > 1 else "") + missing[-1]
        source_name = pair.source.get("name") or m.pool_id
        pool = pair.destination.get("name") or pair.destination.get("id")
        out.append(
            finding(
                "MAP_LB_POOL_INCOMPATIBLE",
                f"{vm.name} is a member of source pool {source_name!r}, which the plan maps to "
                f"pool {pool!r} of provider {pair.destination.get('provider')}: it serves TCP, "
                f"UDP and SCTP alone, so the pool loses {features}.",
            )
        )
    return out


def _dns_domain(domain: str) -> str:
    """A DNS domain as §9.3 compares it: lower case, with its trailing dot."""
    return domain.lower().rstrip(".") + "."


def _port_findings(
    vm: VMRef, plan: Plan, dst_inv: DestinationInventory, source: Provider | None, where: str
) -> list[Finding]:
    """SRC_PORT_ADDRESS_PAIRS, MAP_FIP_ADDRESS_CHANGE and DST_DNS_INTEGRATION_MISSING (SDD §9.3,
    plan task N2b): what an OpenStack VM's ports carry at the source that the migration does not -
    their allowed address pairs, their floating IPs' addresses, their names in DNS."""
    out: list[Finding] = []
    paired = [
        f"{(nic.mac or '?').lower()} on {nic.network!r} ({', '.join(nic.allowed_address_pairs)})"
        for nic in vm.nics
        if nic.allowed_address_pairs
    ]
    if paired:
        out.append(
            finding(
                "SRC_PORT_ADDRESS_PAIRS",
                f"NICs of {vm.name} have allowed address pairs, which the import does not carry: "
                f"{'; '.join(paired)}. An address the VM takes over from a peer is not reachable "
                f"in {where}.",
            )
        )
    # os-migrate's `auto` attaches a detached floating IP of the source's address, else creates
    # one with a new address (SDD §7.2, §23.1); under keep_address the move keeps it (§8.3)
    detached = {_canonical_ip(a) for a in dst_inv.floating_ips}
    moved = (
        []
        if plan.network_cutover.floating_ips == "keep_address"
        else [
            address
            for address in dict.fromkeys(
                _canonical_ip(a) for nic in vm.nics for a in nic.floating_ips
            )
            if address not in detached
        ]
    )
    if moved:
        out.append(
            finding(
                "MAP_FIP_ADDRESS_CHANGE",
                f"Floating IPs of {vm.name} that {where} does not hold detached in its project: "
                f"{', '.join(moved)}. os-migrate creates a floating IP with a new address for "
                "each, so clients, allowlists and DNS records of the old address lose the VM.",
            )
        )
    # Neutron's DNS integration is checked, never driven; what could not be read is not judged
    # (SDD §9.3, §23.5)
    dns = dst_inv.dns
    zones = None if dns.zones is None else {_dns_domain(z) for z in dns.zones}
    published = [nic for nic in vm.nics if nic.dns_name and nic.dns_domain]
    no_zone = [
        nic
        for nic in published
        if zones is not None and _dns_domain(nic.dns_domain or "") not in zones
    ]
    lacking = published if dns.extension is False else no_zone
    if lacking:
        named = ", ".join(
            f"{(nic.mac or '?').lower()} as {nic.dns_name}.{_dns_domain(nic.dns_domain or '')}"
            for nic in lacking
        )
        why = []
        if dns.extension is False:
            why.append("lacks Neutron's dns-integration extension")
        if no_zone:
            domains = dict.fromkeys(_dns_domain(nic.dns_domain or "") for nic in no_zone)
            why.append(f"has no Designate zone {', '.join(domains)}")
        out.append(
            finding(
                "DST_DNS_INTEGRATION_MISSING",
                f"NICs of {vm.name} are published in DNS at {_source_label(source)} - {named} - "
                f"and {where} {' and '.join(why)}: the migrated ports are not published.",
            )
        )
    return out


#: the roles Neutron's create_floatingip:floating_ip_address takes, by policy profile (SDD §9.3)
_FIP_ADDRESS_ROLES = {"secure_rbac": ("admin", "manager")}


def _fip_keep_findings(
    vm: VMRef,
    plan: Plan,
    dst_inv: DestinationInventory,
    selected: Sequence[VMRef],
    destination: Provider | None,
    dst_caps: Mapping[str, Any] | None,
    where: str,
) -> list[Finding]:
    """DST_FIP_ADDRESS_UNAVAILABLE (SDD §9.3, §8.3, plan task N6a): under keep_address, the floating
    IPs of the VM the destination cannot keep - outside its external subnets, held by a port of
    its project -, a token that may not choose an address for those it must create, or a quota
    too small for the plan's; each judged only when the destination reports what it needs."""
    if plan.network_cutover.floating_ips != "keep_address":
        return []
    mine = list(dict.fromkeys(_canonical_ip(a) for nic in vm.nics for a in nic.floating_ips))
    if not mine:
        return []
    external = {n["id"] for n in dst_inv.network_details if n.get("external")}
    cidrs = [s["cidr"] for s in dst_inv.subnets if s.get("network_id") in external]
    attached = {_canonical_ip(a) for a in dst_inv.attached_floating_ips}
    detached = {_canonical_ip(a) for a in dst_inv.floating_ips}
    reasons: list[str] = []
    for address in mine:
        if cidrs and not _in_subnets(address, cidrs):
            reasons.append(f"{address} lies in no subnet of an external network of {where}")
        elif address in attached:
            reasons.append(f"{address} is held by a port of its project")
    extras: list[str] = []
    roles = (dst_caps or {}).get("roles")
    allowed = _FIP_ADDRESS_ROLES.get(policy_profile_of(dst_caps, destination), ("admin",))
    if roles is not None and not set(roles) & set(allowed) and set(mine) - detached:
        extras.append(
            "the credential's token has no role that may choose a floating IP's address "
            f"({' or '.join(allowed)})"
        )
    wanted = {
        _canonical_ip(a) for other in selected for nic in other.nics for a in nic.floating_ips
    } - detached
    if dst_inv.floating_ip_quota is not None and dst_inv.floating_ip_quota < len(wanted):
        extras.append(
            f"the project's floating IP quota leaves {dst_inv.floating_ip_quota} for "
            f"{len(wanted)} addresses"
        )
    if not reasons and not extras:
        return []
    tail = ", and ".join(extras)
    if reasons and tail:
        text = f"{'; '.join(reasons)}. {tail[0].upper()}{tail[1:]}"
    else:
        text = "; ".join(reasons) or tail
    return [
        finding(
            "DST_FIP_ADDRESS_UNAVAILABLE",
            f"Floating IPs of {vm.name} that {where} cannot keep: {text}.",
        )
    ]


def _prefix_split(
    vm: VMRef,
    plan: Plan,
    selected: Sequence[VMRef],
    source: Provider | None,
    destination: Provider | None,
) -> list[Finding]:
    """NET_PREFIX_SPLIT (SDD §9.3, §23.6, plan task N2b): a network of the VM whose VMs the plan's
    waves put into more than one wave, while a provider declares that its cloud announces tenant
    networks - between those waves the prefix has an origin in each cloud. Nothing is judged before
    the waves are planned."""
    declaring = [
        p.label for p in (source, destination) if p is not None and p.announces_tenant_networks
    ]
    if not declaring or not plan.waves:
        return []
    split: list[str] = []
    for network in dict.fromkeys(nic.network for nic in vm.nics):
        waves = {
            wave.id: wave
            for other in selected
            if any(nic.network == network for nic in other.nics)
            and (wave := plan.wave_of(other.source_id)) is not None
        }
        if len(waves) > 1:
            names = ", ".join(repr(w.name) for w in sorted(waves.values(), key=lambda w: w.order))
            split.append(f"{network!r} in waves {names}")
    if not split:
        return []
    verb = "announces" if len(declaring) == 1 else "announce"
    return [
        finding(
            "NET_PREFIX_SPLIT",
            f"The plan's waves split networks of {vm.name} that {' and '.join(declaring)} {verb} "
            f"as tenant networks: {'; '.join(split)}. While one wave is cut over and the next is "
            "not, each prefix has an origin in both clouds, so its routes cannot be correct.",
        )
    ]


def _pool_split(
    vm: VMRef,
    plan: Plan,
    read: MemberRead,
    selected: Sequence[VMRef],
    source: Provider | None,
) -> list[Finding]:
    """NET_LB_POOL_SPLIT (SDD §9.3, §23.7): each Octavia pool at the source the VM is a member of -
    one the plan maps too - whose members among the plan's VMs the plan's waves put into more than
    one wave: between those waves the pool's members are in both clouds. A member that is no VM of
    the plan, or a VM of the plan in no wave, is not counted; nothing is judged before the waves
    are planned."""
    pools: dict[str, PoolMember] = {}
    for m in _memberships(vm, read.members):
        pools.setdefault(m.pool_id, m)
    if not pools or not plan.waves:
        return []
    # the members of the VM's pools alone: each VM of the plan is matched against those
    members = [m for m in read.members if m.pool_id in pools]
    waves: dict[str, dict[str, Wave]] = {pool_id: {} for pool_id in pools}
    for other in selected:
        wave = plan.wave_of(other.source_id)
        if wave is None:
            continue
        for m in _memberships(other, members):
            waves[m.pool_id][wave.id] = wave
    split = [
        f"pool {m.pool!r} of load balancer {m.load_balancer!r} in waves "
        + ", ".join(repr(w.name) for w in sorted(held.values(), key=lambda w: w.order))
        for pool_id, m in pools.items()
        if len(held := waves[pool_id]) > 1
    ]
    if not split:
        return []
    return [
        finding(
            "NET_LB_POOL_SPLIT",
            f"The plan's waves split Octavia pools at {_source_label(source)} that {vm.name} is a "
            f"member of: {'; '.join(split)}. While one wave is cut over and the next is not, each "
            "pool's members are in both clouds, and its load balancer reaches only those not yet "
            "cut over.",
        )
    ]


#: (service, check-report key, floor, why) every strategy needs (SDD §21.4); a gap one strategy
#: alone has is that strategy's eligibility reason (planning.selector), never a finding
_CAPABILITY_FLOOR: tuple[tuple[str, str, str, str], ...] = (
    ("compute API", "compute_microversion", "2.60", " (multi-attach)"),
    ("block storage API", "block_storage_microversion", "3.0", ""),
)


def _destination_capabilities(destination: Provider, caps: Mapping[str, Any]) -> list[Finding]:
    """DST_CAPABILITY_MISSING (SDD §21.4): each maximum of the destination's check report below
    the floor every strategy needs; one the check could not read is not judged."""
    lacking = [
        f"{service} {caps[key]}, below {floor}{why}"
        for service, key, floor, why in _CAPABILITY_FLOOR
        if (read := parse_microversion(caps.get(key))) is not None
        and read < parse_microversion(floor)
    ]
    if not lacking:
        return []
    return [
        finding(
            "DST_CAPABILITY_MISSING",
            f"{destination.label} lacks what every strategy needs: {'; '.join(lacking)}.",
        )
    ]


def _series_text(names: Sequence[str]) -> str:
    return f"{names[0]} – {names[-1]}" if len(names) > 1 else (names[0] if names else "no series")


def _release_outside_support(destination: Provider, caps: Mapping[str, Any]) -> list[Finding]:
    """DST_RELEASE_OUTSIDE_SUPPORT (SDD §21.4, §10.1): a Tier 3 or Outside destination, or one
    whose declared series its APIs contradict; a warning, never a block."""
    tier = caps.get("support_tier")
    if not isinstance(tier, Mapping) or tier.get("tier") == "validated":
        return []
    found = [str(name) for name in caps.get("release_range") or []]
    sentences = []
    reason = tier.get("reason")
    if tier.get("tier") == "tier3":
        # the series the check settled on, else the range (review RV18-11)
        runs = str(caps.get("release") or _series_text(found))
        sentences.append(
            f"{destination.label} runs {runs}, read as Tier 3 (End of Life upstream): no "
            "Seamless release validated it there. The capability checks still apply."
        )
    elif tier.get("tier") == "outside":
        sentences.append(
            {
                "below_floor": f"{destination.label} runs a series older than "
                f"{SERIES[0].name}, the support window's floor: no Seamless release validated it.",
                "newer_than_validated": f"{destination.label} runs a series newer than "
                f"{SERIES[-1].name}, the newest series of the support window: no Seamless release "
                "validated it.",
            }.get(
                str(reason),
                f"The series of {destination.label} could not be detected (its compute API "
                "maximum was not read): no Seamless release can be said to have validated it.",
            )
            + " The capability checks still apply."
        )
    if tier.get("declared_outside_range") and destination.release:
        if sentences:
            sentences.append(
                f"It declares series {destination.release}, which its APIs do not run: correct "
                "the declared series."
            )
        else:
            label = str(tier.get("tier", "")).replace("tier", "Tier ")
            sentences.append(
                f"{destination.label} declares series {destination.release}, but its APIs "
                f"read as {_series_text(found)}: correct the declared series; the tier is the one "
                f"its APIs read ({label})."
            )
    if not sentences:
        return []
    return [finding("DST_RELEASE_OUTSIDE_SUPPORT", " ".join(sentences))]


def _app_probe_missing(vm: VMRef, plan: Plan) -> Finding | None:
    """A database VM verified only by its boot (SDD §9.3, ADV-18)."""
    # imported here: the wave planner (planning.waves, behind planning.probes) imports this module
    from .probes import describe_probe, is_database, probes_for, suggest_probes

    if not is_database(vm) or probes_for(vm, plan):
        return None
    suggested = suggest_probes(vm)
    hint = f" Suggested: {', '.join(describe_probe(p) for p in suggested)}." if suggested else ""
    return finding(
        "APP_PROBE_MISSING",
        f"{vm.name} is a database VM without an application probe: verification would check only "
        f"that it booted.{hint}",
    )


def has_blocker(findings: Iterable[Finding]) -> bool:
    return any(f.severity == Severity.blocker for f in findings)
