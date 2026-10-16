"""Pre-flight validation: the finding catalog of SDD §9.3, and the inventories of SDD §10."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from ..domain.enums import ProviderKind, Severity, Strategy
from ..domain.models import Finding, Mappings, NicOverride, Plan, Provider, VMRef
from ..guest_os import identify
from .ipam import address_refusal
from .networks import kit_subnet, policy_profile_of, port_refusal, resolve_network
from .platforms import SERIES, parse_microversion

S = Strategy


@dataclass
class SourceInventory:
    #: network name -> MTU (None when unknown)
    networks: dict[str, int | None] = field(default_factory=dict)
    projects: list[str] = field(default_factory=list)
    #: the source credential's own keypairs: what export_keypairs.yml can export (SDD §10)
    keypairs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    "MAP_NETWORK_MISSING": CatalogEntry(
        Severity.blocker, (), "Add a network mapping or create a same-named network in RHOSO."
    ),
    "MAP_NETWORK_PRESTAGED": CatalogEntry(
        Severity.info,
        (),
        "Nothing to do: pre-staging creates the network in RHOSO with the same name. Add a "
        "network mapping to use an existing RHOSO network instead.",
    ),
    "MAP_FLAVOR_PRESTAGED": CatalogEntry(
        Severity.info,
        (),
        "Nothing to do: pre-staging creates the flavor in RHOSO with the same name, size and "
        "extra specs. Add a flavor mapping to use an existing RHOSO flavor instead.",
    ),
    "DST_KEYPAIR_MISSING": CatalogEntry(
        Severity.blocker,
        (),
        "Create the keypair (its public key) for the RHOSO migration user, or pre-stage keypairs "
        "when the source migration user owns it.",
    ),
    "MAP_KEYPAIR_PRESTAGED": CatalogEntry(
        Severity.info,
        (),
        "Nothing to do: pre-staging imports the keypair (name and public key) for the RHOSO "
        "migration user.",
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
    "NET_MTU_SHRINK": CatalogEntry(
        Severity.warning,
        (),
        "Lower the guest MTU or map to a destination network with a larger MTU "
        "(ML2/OVN Geneve networks typically use 1442).",
    ),
    "NET_SRIOV_PORT": CatalogEntry(
        Severity.warning,
        (),
        "SR-IOV/macvtap ports need an equivalent physnet in RHOSO; map the network accordingly.",
    ),
    "NET_PORT_IPS": CatalogEntry(
        Severity.warning,
        (),
        'Turn on "Keep MAC addresses and every fixed IP" in the plan (port_creation_mode '
        '"neutron"), give each such port exactly one fixed IP before the cutover, or migrate the '
        "VM by storage handover when its ports have at most one address each.",
    ),
    "MAP_NETWORK_AMBIGUOUS": CatalogEntry(
        Severity.blocker,
        (),
        "Send the NIC to a name one RHOSO network carries: one of the credential's project, or the "
        "only one of that name - map its network to such a name, or rename one of the networks.",
    ),
    "NET_ADDRESS_REFUSED": CatalogEntry(
        Severity.blocker,
        (),
        "Give the NIC an address its RHOSO network takes: map its network to one whose subnets "
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
        "Fix the VM's network overrides on its migration page: an existing RHOSO network, "
        "addresses inside its subnets, each given once in the plan. VMware VMs take none.",
    ),
    "MAP_NIC_OVERRIDE": CatalogEntry(
        Severity.info,
        (),
        "Nothing to do: each NIC goes where its override says.",
    ),
    "VM_PCI_PASSTHROUGH": CatalogEntry(
        Severity.blocker,
        (),
        "PCI passthrough devices cannot be migrated; rebuild the VM with an equivalent flavor.",
    ),
    "VM_VGPU": CatalogEntry(
        Severity.blocker,
        (),
        "vGPU instances cannot be migrated; rebuild the VM with a RHOSO vGPU flavor.",
    ),
    "VOL_ENCRYPTED": CatalogEntry(
        Severity.warning,
        (),
        "The Barbican key must be re-created in RHOSO before the volume can be attached.",
    ),
    "GUEST_OS_LEGACY": CatalogEntry(
        Severity.warning,
        (),
        "Out of vendor support: it still migrates; test the application on RHOSO, check the "
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
        "Map the volume type to a RHOSO Cinder backend in handover.backend_map.",
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
    "unknown": "the provider declares its policy profile or series, so the check counts that "
    "profile's role: service under secure-RBAC, advsvc under the legacy rules",
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


def resource_demand(vm: VMRef, plan: Plan | None = None) -> dict[str, int]:
    """Quota the VM needs at the destination. An ``image_root`` disk becomes a destination volume
    on the warm path (``boot_disk_copy``), so it counts unless the plan is cold-only."""
    kinds = {"volume", "vmdk"}
    if plan is None or plan.default_strategy != Strategy.cold:
        kinds.add("image_root")
    volumes = [d for d in vm.disks if d.kind in kinds]
    return {
        "cores": vm.vcpus,
        "ram_mb": vm.ram_mb,
        "instances": 1,
        "volumes": len(volumes),
        "gigabytes": sum(d.size_gb for d in volumes),
    }


def _quota_shortfall(
    vm: VMRef, plan: Plan, dst_inv: DestinationInventory, all_vms: Sequence[VMRef]
) -> tuple[str, list[str]] | None:
    project = destination_project(vm, plan, dst_inv)
    if project is None or project not in dst_inv.quotas:
        return None
    totals = dict.fromkeys(_QUOTA_KEYS, 0)
    for other in all_vms:
        if destination_project(other, plan, dst_inv) == project:
            for key, value in resource_demand(other, plan).items():
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


#: Extra-spec prefixes that make a destination flavor unsuitable for automatic matching: it
#: would pin the server to special hardware or hosts (SDD §9.3 ``MAP_FLAVOR_AUTO``).
_CONSTRAINED_SPEC_PREFIXES = (
    "pci_passthrough:",
    "resources:",
    "trait:",
    "aggregate_instance_extra_specs:",
)


def _constrained(flavor: dict[str, Any]) -> bool:
    specs = flavor.get("extra_specs") or {}
    return any(str(k).startswith(_CONSTRAINED_SPEC_PREFIXES) for k in specs)


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
    (an existing destination flavor is never pre-staged over)."""
    return bool(
        vm.flavor
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
    same-named destination flavor fits, the smallest fitting flavor is recorded.
    """
    out = Mappings()
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
                f"{mac}: RHOSO has no network {network!r}"
                + (
                    ""
                    if entries[mac].network
                    else " yet: pre-staging would create it, but its subnets cannot be checked "
                    "before; name an existing RHOSO network"
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
) -> list[Finding]:
    """Evaluate the SDD §9.3 catalog for ``vm`` within ``plan``.

    ``all_vms`` are the plan's selected VMs (for duplicate names and cumulative quotas);
    ``source``/``destination`` enable the conversion-host check; ``dst_caps`` are the
    destination's capabilities as validation's check just reported them (its token's roles).
    """
    selected = list(all_vms) if all_vms is not None else [vm]
    out: list[Finding] = []
    maps = plan.mappings

    if vm.power_state == "error":
        out.append(finding("SRC_VM_ERROR_STATE", f"Source VM {vm.name} is in error state."))
    elif vm.power_state == "transitioning":
        out.append(
            finding(
                "SRC_VM_TRANSITIONAL_STATE",
                f"Source VM {vm.name} has a Nova task in flight (resize, migration, rescue, "
                "rebuild or reboot); a stop or snapshot would fail.",
            )
        )

    twins = [v for v in selected if v.source_id != vm.source_id and v.name == vm.name]
    if twins:
        where = _names(f"{t.project or '-'}/{t.source_id}" for t in twins)
        out.append(
            finding(
                "SRC_VM_DUPLICATE_NAME",
                f"Another selected VM is also named {vm.name!r} ({where}); os-migrate selects "
                "workloads by name.",
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
                f"Pre-staging creates same-named RHOSO network(s): {_names(missing_nets)}.",
            )
        )
    elif missing_nets:
        out.append(
            finding(
                "MAP_NETWORK_MISSING",
                f"No mapping or same-named RHOSO network for: {_names(missing_nets)}."
                + ("" if prestaged or "networks" not in plan.prestage_resources else "")
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
                f"Network mapping target(s) do not exist in RHOSO: {_names(bad_net_targets)}.",
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
                "The cutover cannot tell these RHOSO networks apart: " + "; ".join(ambiguous) + ".",
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
        why = None if network is None else port_refusal(network, roles, profile)
        if network is not None and why:
            denied.append(
                f"{(nic.mac or '?').lower()} on {network['name']!r} "
                f"(project {network.get('project_id')}): {why}"
            )
    if denied:
        out.append(
            finding(
                "NET_PORT_NOT_PERMITTED",
                "The RHOSO credential may not put these NICs on their network: "
                + "; ".join(denied)
                + ".",
                remediation=_port_remediation(profile),
            )
        )

    if keypair_prestaged(vm, plan, src_inv, dst_inv):
        out.append(
            finding(
                "MAP_KEYPAIR_PRESTAGED",
                f"Pre-staging imports keypair {vm.key_name!r} for the RHOSO migration user.",
            )
        )
    elif keypair_missing(vm, dst_inv):
        owned = vm.key_name in src_inv.keypairs
        out.append(
            finding(
                "DST_KEYPAIR_MISSING",
                f"The VM boots with keypair {vm.key_name!r}, which the RHOSO migration user does "
                "not have: the cutover would stop at its destination check."
                + (
                    " Pre-stage keypairs to import it."
                    if owned
                    else " Another user owns it at the source, so pre-staging cannot export it: "
                    "create it, its public key, for the RHOSO migration user."
                ),
            )
        )

    dst_flavor_names = {str(f.get("name")) for f in dst_inv.flavors if f.get("name")}
    if flavor_prestaged(vm, plan, dst_inv) and (
        source is None or source.kind != ProviderKind.vmware
    ):
        out.append(
            finding(
                "MAP_FLAVOR_PRESTAGED",
                f"Pre-staging creates RHOSO flavor {vm.flavor!r} with the source's size and "
                "extra specs.",
            )
        )
    elif vm.flavor and vm.flavor in maps.flavors:
        target = maps.flavors[vm.flavor]
        if target not in dst_flavor_names:
            out.append(
                finding(
                    "MAP_FLAVOR_MISSING",
                    f"Flavor {vm.flavor!r} is mapped to {target!r}, which does not exist in RHOSO.",
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
                    f"No mapping for flavor {vm.flavor!r} and no RHOSO flavor with >= {vm.vcpus} "
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
                    f"No mapping for flavor {vm.flavor!r}: the smallest fitting RHOSO flavor "
                    f"{name!r} ({vcpus} vCPUs, {ram_mb} MB RAM, {disk_gb} GB disk) will be used"
                    + (
                        f"; the source flavor's {', '.join(dropped)} are not carried over."
                        if dropped
                        else "."
                    ),
                    severity=Severity.warning if dropped else None,
                    remediation=(
                        "Map the flavor to a RHOSO flavor with the same hw:* extra specs."
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
                f"Volume type mapping target(s) do not exist in RHOSO: {_names(bad_type_targets)}.",
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
                f"No mapping or same-named RHOSO volume type for: {_names(missing_types)}; "
                "the plan maps volume types, so the destination default cannot be used.",
                severity=Severity.blocker,
                remediation="Add a volume type mapping for it (the plan preserves volume types).",
            )
        )
    elif missing_types:
        out.append(
            finding(
                "MAP_VOLUME_TYPE_MISSING",
                f"No mapping or same-named RHOSO volume type for: {_names(missing_types)}.",
            )
        )

    if dst_inv.quotas:
        mapped_project = maps.projects.get(vm.project or "")
        if mapped_project is not None and mapped_project not in dst_inv.quotas:
            out.append(
                finding(
                    "DST_PROJECT_MISSING",
                    f"Project {vm.project!r} is mapped to {mapped_project!r}, which the "
                    "destination does not list.",
                )
            )
        elif (
            vm.project  # VMware VMs report none: the kit charges the credential's project
            and mapped_project is None
            and vm.project not in dst_inv.quotas
            and len(dst_inv.quotas) != 1
        ):
            out.append(
                finding(
                    "DST_PROJECT_MISSING",
                    f"No destination project for {vm.project!r}: no mapping, no same-named "
                    f"project, and the destination has {len(dst_inv.quotas)} projects.",
                )
            )
    shortfall = _quota_shortfall(vm, plan, dst_inv, selected)
    if shortfall is not None:
        project, short = shortfall
        out.append(
            finding(
                "DST_QUOTA_INSUFFICIENT",
                f"Destination project {project!r} lacks quota for the plan's VMs: "
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
        out.append(finding("NET_MTU_SHRINK", f"Destination MTU is smaller: {_names(shrinks)}."))

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
                    "Neutron would refuse these addresses on their RHOSO network: "
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
                    "Neutron would refuse these addresses on their RHOSO network: "
                    + "; ".join(refused)
                    + ".",
                )
            )

    out.extend(_nic_override_findings(vm, plan, dst_inv, selected, source))

    if "pci_passthrough:alias" in vm.flavor_extra_specs:
        out.append(
            finding(
                "VM_PCI_PASSTHROUGH",
                "Flavor requests PCI passthrough "
                f"({vm.flavor_extra_specs['pci_passthrough:alias']}).",
            )
        )
    if "resources:VGPU" in vm.flavor_extra_specs:
        out.append(finding("VM_VGPU", "Flavor requests a vGPU (resources:VGPU)."))

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
                lacking.append(f"destination {destination.id}")
        else:
            if source.conversion_host is None:
                lacking.append(f"source {source.id}")
            if destination.conversion_host is None:
                lacking.append(f"destination {destination.id}")
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
    return out


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
            f"Destination {destination.id} lacks what every strategy needs: {'; '.join(lacking)}.",
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
            f"Destination {destination.id} runs {runs}, read as Tier 3 (End of Life upstream): no "
            "Seamless release validated it there. The capability checks still apply."
        )
    elif tier.get("tier") == "outside":
        sentences.append(
            {
                "below_floor": f"Destination {destination.id} runs a series older than "
                f"{SERIES[0].name}, the support window's floor: no Seamless release validated it.",
                "newer_than_validated": f"Destination {destination.id} runs a series newer than "
                f"{SERIES[-1].name}, the newest series of the support window: no Seamless release "
                "validated it.",
            }.get(
                str(reason),
                f"Destination {destination.id}'s series could not be detected (its compute API "
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
                f"Destination {destination.id} declares series {destination.release}, but its APIs "
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
