"""Pre-flight validation: the finding catalog of SDD §9.3, and the inventories of SDD §10."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from ..domain.enums import ProviderKind, Severity, Strategy
from ..domain.models import Finding, Mappings, Plan, Provider, VMRef
from ..guest_os import identify

S = Strategy


@dataclass
class SourceInventory:
    #: network name -> MTU (None when unknown)
    networks: dict[str, int | None] = field(default_factory=dict)
    projects: list[str] = field(default_factory=list)

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
}

_SRIOV_VNIC_TYPES = frozenset({"direct", "direct-physical", "macvtap"})
_QUOTA_KEYS = ("cores", "ram_mb", "instances", "volumes", "gigabytes")


def is_legacy_os(os_type: str | None) -> bool:
    """True when the guest is out of standard vendor support (SDD §9.5 catalog)."""
    return identify(os_type).lifecycle == "legacy"


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


def resolve_mappings(vm: VMRef, plan: Plan, dst_inv: DestinationInventory) -> Mappings:
    """Mappings preflight matched automatically for ``vm`` (``Migration.resolved_mappings``).

    0.1.0 resolves flavors only: when the plan has no mapping for the VM's flavor and no
    same-named destination flavor fits, the smallest fitting flavor is recorded.
    """
    out = Mappings()
    if vm.flavor and vm.flavor not in plan.mappings.flavors:
        chosen = fitting_flavor(vm, dst_inv)
        if chosen is not None and chosen["name"] != vm.flavor:
            out.flavors[vm.flavor] = str(chosen["name"])
    return out


def _names(items: Iterable[str]) -> str:
    return ", ".join(sorted(set(items)))


def run_preflight(
    vm: VMRef,
    plan: Plan,
    src_inv: SourceInventory,
    dst_inv: DestinationInventory,
    all_vms: Sequence[VMRef] | None = None,
    *,
    source: Provider | None = None,
    destination: Provider | None = None,
) -> list[Finding]:
    """Evaluate the SDD §9.3 catalog for ``vm`` within ``plan``.

    ``all_vms`` are the plan's selected VMs (for duplicate names and cumulative quotas);
    ``source``/``destination`` enable the conversion-host check.
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

    missing_nets = [
        nic.network
        for nic in vm.nics
        if nic.network not in maps.networks and nic.network not in dst_inv.networks
    ]
    # a mapped target must exist: nothing creates a network under the *mapped* name
    bad_net_targets = [
        f"{nic.network} -> {maps.networks[nic.network]}"
        for nic in vm.nics
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

    dst_flavor_names = {str(f.get("name")) for f in dst_inv.flavors if f.get("name")}
    if vm.flavor and vm.flavor in maps.flavors:
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
        dst_mtu = dst_inv.networks.get(maps.networks.get(nic.network, nic.network))
        if src_mtu is not None and dst_mtu is not None and dst_mtu < src_mtu:
            shrinks.append(f"{nic.network} {src_mtu} -> {dst_mtu}")
    if shrinks:
        out.append(finding("NET_MTU_SHRINK", f"Destination MTU is smaller: {_names(shrinks)}."))

    sriov = [nic.network for nic in vm.nics if nic.vnic_type in _SRIOV_VNIC_TYPES]
    if sriov:
        out.append(finding("NET_SRIOV_PORT", f"SR-IOV/macvtap port(s) on: {_names(sriov)}."))

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
    return out


def has_blocker(findings: Iterable[Finding]) -> bool:
    return any(f.severity == Severity.blocker for f in findings)
