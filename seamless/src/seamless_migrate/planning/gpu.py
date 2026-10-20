"""GPU requests, keys, profile sizes and pool names (SDD §10, §22.2-§22.4, §22.7): a pure module,
no I/O, that the providers, the pre-flight and the cutover gate share."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..domain.enums import ProviderKind
from ..domain.models import GpuDevice, Plan, VMRef

#: Nova's resource class of a vGPU (SDD §22.2)
VGPU = "VGPU"
#: the flavor extra spec that asks for PCI devices by alias: <alias>:<n>[,<alias>:<n>...]
PCI_ALIAS = "pci_passthrough:alias"

#: a resource request, bare or in one of Nova's request groups - resources<suffix>:<class>, the
#: suffix as §9.3's VM_VGPU reads it (review PL-08)
_RESOURCES = re.compile(r"^resources(?P<suffix>[A-Za-z0-9_-]*):(?P<cls>[A-Z0-9_]+)$")
#: a request group's trait: trait<suffix>:<name>, set to required or forbidden
_TRAIT = re.compile(r"^trait(?P<suffix>[A-Za-z0-9_-]*):(?P<name>.+)$")
#: NVIDIA's profile names, with or without vCenter's grid_/nvidia_ prefix or the "GRID " of the
#: mediated device types: <gpu>-<framebuffer GB><series>, MIG-backed
#: <gpu>-<slices>-<framebuffer GB><series> (SDD §22.2)
_PROFILE = re.compile(
    r"(?:grid_|nvidia_|grid )?(?P<gpu>[a-z0-9]+)-(?:(?P<slices>[1-9][0-9]?)-)?"
    r"(?P<framebuffer>[1-9][0-9]*)(?P<series>[abcq])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProfileSize:
    """What an NVIDIA vGPU profile's name says of its size (SDD §22.2): `grid_a100-3-20c` is an
    A100's 3 MIG slices with 20 GB of framebuffer, series C (compute)."""

    gpu: str
    slices: int | None
    framebuffer_gb: int
    series: str


def _int(value: str) -> int | None:
    """``value`` as Nova reads an amount or a count, with Python's ``int()`` - spaces around it, a
    sign, underscores between digits and any script's decimal digits included -, else None."""
    try:
        return int(value)
    except ValueError:
        return None


def _amount(value: str) -> int:
    """A resource amount as Nova's ``RequestGroup.add_resource`` reads it, 0 for none: it skips a
    negative or unreadable amount with a warning, and strips a zero from the request (SDD §22.2)."""
    amount = _int(value)
    return amount if amount is not None and amount > 0 else 0


def _aliases(value: str | None) -> list[tuple[str, int]]:
    """The aliases of `pci_passthrough:alias` with their counts, as Nova asks for int(count)
    devices: a zero or negative count asks for none; a count left out, or one int() cannot read,
    reads as 1 - Nova refuses to boot such a flavor (SDD §22.2)."""
    out = []
    for item in (value or "").split(","):
        name, _, count = item.partition(":")
        if not name.strip():
            continue
        number = _int(count)
        if number is None or number > 0:
            out.append((name.strip(), 1 if number is None else number))
    return out


def gpu_requests(
    extra_specs: Mapping[str, str], declared_profile: str | None = None
) -> list[GpuDevice]:
    """The GPU requests of a flavor's extra specs (SDD §22.2): each request group's `VGPU` with
    the group's required and forbidden traits, in suffix order (the unnumbered group first, a
    numbered one `same_provider`), then each PCI alias. A `CUSTOM_` class is a vGPU only with a
    declared profile, which each vGPU entry takes - or each alias entry, on a flavor whose GPU
    requests are aliases alone."""
    classes: dict[str, list[tuple[str, int]]] = {}
    traits: dict[tuple[str, str], list[str]] = {}
    for key, value in extra_specs.items():
        if match := _RESOURCES.match(str(key)):
            classes.setdefault(match["suffix"], []).append((match["cls"], _amount(str(value))))
        elif (match := _TRAIT.match(str(key))) and value in ("required", "forbidden"):
            traits.setdefault((match["suffix"], str(value)), []).append(match["name"])
    entries = [
        GpuDevice(
            kind="vgpu",
            profile=declared_profile,
            count=amount,
            resource_class=cls,
            traits=sorted(traits.get((suffix, "required"), [])),
            forbidden_traits=sorted(traits.get((suffix, "forbidden"), [])),
            same_provider=bool(suffix),
        )
        for suffix in sorted(classes)
        for cls, amount in sorted(classes[suffix])
        if amount and (cls == VGPU or (declared_profile and cls.startswith("CUSTOM_")))
    ]
    alias_profile = None if entries else declared_profile
    entries += [
        GpuDevice(kind="passthrough", alias=alias, count=count, profile=alias_profile)
        for alias, count in _aliases(extra_specs.get(PCI_ALIAS))
    ]
    return entries


@dataclass(frozen=True)
class PoolRequest:
    """A pool the GPU capacity read asks Placement of (SDD §22.3): a vGPU request's resource class
    with its required and forbidden traits - a numbered group's held by the GPU's own provider
    (`same_provider`), the unnumbered group's by it or its compute node -, or a class alone;
    `groups`, the units of each request group of more than one unit that asks of it, which the read
    takes again as <class>:<n>."""

    resource_class: str
    traits: tuple[str, ...] = ()
    forbidden_traits: tuple[str, ...] = ()
    same_provider: bool = False
    groups: tuple[int, ...] = ()


def pool_key(
    resource_class: str, traits: Iterable[str], forbidden: Iterable[str], same_provider: bool
) -> tuple[str, tuple[str, ...], tuple[str, ...], bool]:
    """What tells a pool from another (SDD §22.3): its class, its required and forbidden traits,
    and whether they are a numbered group's - a request without traits is its class's pool."""
    required, refused = tuple(sorted(traits)), tuple(sorted(forbidden))
    return resource_class, required, refused, same_provider and bool(required or refused)


def gpu_pools(requests: Iterable[GpuDevice]) -> list[PoolRequest]:
    """The pools of the vGPU requests among ``requests`` (SDD §22.3): one per distinct request
    with traits - its class with its required and forbidden traits, and whether its group is a
    numbered one -, in the order they come, then one per resource class alone, which unites that
    class's pools and is the pool of a request without traits. A PCI alias request has none: the
    device class its alias asks for lives in nova.conf."""
    groups: dict[tuple[str, tuple[str, ...], tuple[str, ...], bool], set[int]] = {}
    for device in requests:
        if device.kind != "vgpu" or not device.resource_class:
            continue
        key = pool_key(
            device.resource_class, device.traits, device.forbidden_traits, device.same_provider
        )
        sizes = groups.setdefault(key, set())
        if device.count > 1:
            sizes.add(device.count)
    for resource_class in dict.fromkeys(cls for cls, *_ in list(groups)):
        groups.setdefault((resource_class, (), (), False), set())
    return [
        PoolRequest(cls, traits, forbidden, same_provider, tuple(sorted(sizes)))
        for (cls, traits, forbidden, same_provider), sizes in groups.items()
    ]


def vgpu_request_keys(
    extra_specs: Mapping[str, str], declared_profile: str | None = None
) -> list[str]:
    """The extra-spec keys whose requests `gpu_requests` reads as vGPUs, sorted: what §9.3's
    `VM_VGPU` names for an OpenStack flavor (SDD §9.3, §22.2)."""
    return sorted(
        str(key)
        for key, value in extra_specs.items()
        if (match := _RESOURCES.match(str(key)))
        and _amount(str(value))
        and (match["cls"] == VGPU or (declared_profile and match["cls"].startswith("CUSTOM_")))
    )


def gpu_key(device: GpuDevice) -> str | None:
    """What a plan's GPU mapping names a VMware device by (SDD §22.2, §22.4): a vGPU's profile, a
    passthrough device's `<vendor_id>:<device_id>`; None when it has neither."""
    if device.kind == "vgpu":
        return device.profile or None
    if device.vendor_id and device.device_id:
        return f"{device.vendor_id}:{device.device_id}"
    return None


def vm_gpus(vm: VMRef, declared_profile: str | None = None) -> list[GpuDevice]:
    """The VM's GPUs (SDD §22.2): as its source reported them, else - a VM recorded before 0.3.0 -
    what its flavor's extra specs ask for, with the profile its source declares for the flavor."""
    return list(vm.gpus) or gpu_requests(vm.flavor_extra_specs, declared_profile)


def gpu_flavor(vm: VMRef, plan: Plan, source_kind: ProviderKind | str) -> str | None:
    """A GPU VM's GPU flavor, the destination flavor its server is created with, as its plan names
    it (SDD §22.4): for a VMware VM the one flavor ``mappings.gpus`` maps each of its GPU keys to -
    None when a device has no key, a key no mapping names, or its keys map to different flavors -;
    for an OpenStack VM the explicit mapping of its flavor. Never a same-named or a matched flavor;
    None for a VM without a GPU."""
    gpus = vm_gpus(vm)
    if not gpus:
        return None
    if ProviderKind(source_kind) != ProviderKind.vmware:
        return plan.mappings.flavors.get(vm.flavor) if vm.flavor else None
    named = {plan.mappings.gpus.get(key) if key else None for key in map(gpu_key, gpus)}
    flavor = named.pop()
    return flavor if not named else None


def vgpu_profile_size(name: str) -> ProfileSize | None:
    """The size an NVIDIA vGPU profile's name gives (SDD §22.2), read case-insensitively; None for
    any other name - an mdev type id such as `nvidia-471` among them."""
    match = _PROFILE.fullmatch(name)
    if match is None:
        return None
    return ProfileSize(
        gpu=match["gpu"].upper(),
        slices=int(match["slices"]) if match["slices"] else None,
        framebuffer_gb=int(match["framebuffer"]),
        series=match["series"].upper(),
    )


def pool_name(resource_class: str, traits: Iterable[str], forbidden_traits: Iterable[str]) -> str:
    """A pool's name, as the cutover gate's wait gives it (SDD §10, §22.7): the class, the required
    traits sorted, then the forbidden ones sorted, each prefixed with "!"."""
    return " ".join(
        [resource_class, *sorted(traits), *(f"!{name}" for name in sorted(forbidden_traits))]
    )
