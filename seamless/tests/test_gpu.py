"""SDD §22.2-§22.4 (plan task G1): the GPU requests a flavor's extra specs ask for, a VMware
device's GPU key, NVIDIA profile sizes and pool names - planning/gpu.py, a pure module."""

import pytest

from seamless_migrate.domain.enums import ProviderKind
from seamless_migrate.domain.models import GpuDevice, Mappings
from seamless_migrate.planning.gpu import (
    ProfileSize,
    gpu_flavor,
    gpu_key,
    gpu_requests,
    pool_name,
    vgpu_profile_size,
)
from tests.factories import make_plan, make_vm


def vgpu(**kw):
    return GpuDevice(kind="vgpu", resource_class="VGPU", **kw)


def test_gpu_requests_reads_a_bare_vgpu_request_with_its_traits():
    """SDD §22.2: the unnumbered group's VGPU with its trait:<name> entries, required and
    forbidden, sorted; a spec that is no GPU request is no part of it."""
    specs = {
        "resources:VGPU": "1",
        "trait:CUSTOM_NVIDIA_A100_4C": "required",
        "trait:CUSTOM_NVIDIA_OLD": "forbidden",
        "trait:CUSTOM_ANY": "preferred",  # neither required nor forbidden: no part of the request
        "hw:cpu_policy": "dedicated",
    }
    assert gpu_requests(specs) == [
        vgpu(count=1, traits=["CUSTOM_NVIDIA_A100_4C"], forbidden_traits=["CUSTOM_NVIDIA_OLD"])
    ]


def test_gpu_requests_reads_each_numbered_group_with_its_own_traits():
    """SDD §22.2: Nova's granular groups, resources<suffix>:VGPU, each with the traits of its own
    suffix - the unnumbered group's traits belong to no numbered group - in suffix order; a numbered
    group's traits are its GPU provider's own (same_provider, §22.3; PR #62 review)."""
    specs = {
        "resources_GPU-b:VGPU": "2",
        "trait_GPU-b:CUSTOM_B": "required",
        "trait_GPU-b:CUSTOM_A": "required",
        "resources1:VGPU": "1",
        "trait1:CUSTOM_ONE": "required",
        "trait:CUSTOM_ROOT": "required",
        "resources:VCPU": "4",
    }
    assert gpu_requests(specs) == [
        vgpu(count=1, traits=["CUSTOM_ONE"], same_provider=True),
        vgpu(count=2, traits=["CUSTOM_A", "CUSTOM_B"], same_provider=True),
    ]
    assert [device.same_provider for device in gpu_requests(specs)] == [True, True]
    [unnumbered] = gpu_requests({"resources:VGPU": "1", "trait:CUSTOM_X": "required"})
    assert unnumbered.same_provider is False


def test_gpu_requests_reads_each_pci_alias_with_its_count():
    """SDD §22.2: pci_passthrough:alias=<alias>:<n>[,...] is one passthrough request per alias; a
    count left out, or one Python's int() cannot read, reads as 1 (Nova refuses to boot it)."""
    specs = {"pci_passthrough:alias": "a100:2, t4, l4:x"}
    assert gpu_requests(specs) == [
        GpuDevice(kind="passthrough", alias="a100", count=2),
        GpuDevice(kind="passthrough", alias="t4", count=1),
        GpuDevice(kind="passthrough", alias="l4", count=1),
    ]


def test_a_custom_resource_class_is_a_vgpu_only_with_a_declared_profile():
    """SDD §22.2: Nova's mdev_class gives a vGPU type a CUSTOM_ class, which no API tells from any
    other custom class: it is a vGPU only when its flavor's provider declares a profile."""
    specs = {"resources1:CUSTOM_NVIDIA_A100_4C": "1", "trait1:CUSTOM_DC3": "required"}
    assert gpu_requests(specs) == []
    assert gpu_requests(specs, declared_profile="A100-4C") == [
        GpuDevice(
            kind="vgpu",
            profile="A100-4C",
            count=1,
            resource_class="CUSTOM_NVIDIA_A100_4C",
            traits=["CUSTOM_DC3"],
            same_provider=True,
        )
    ]


@pytest.mark.parametrize(
    "specs",
    [
        {"resources1:VCPU": "4"},
        {"resources:CUSTOM_VGPU_X": "1"},
        {"resources1:VGPU_DISPLAY_HEAD": "1"},
        {"trait1:CUSTOM_VGPU": "required"},
        {"trait:CUSTOM_NVIDIA_A40": "required"},
        {"aggregate_instance_extra_specs:gpu": "true"},
        {"pci_passthrough:alias": ""},
        {},
    ],
)
def test_another_resource_class_or_a_trait_alone_is_no_gpu(specs):
    """SDD §22.2 (as §9.3's VM_VGPU reads it, review PL-08): another resource class, a trait alone,
    an aggregate pin or an empty alias list asks for no GPU."""
    assert gpu_requests(specs) == []


def test_a_zero_amount_asks_for_no_gpu():
    """SDD §22.2: a zero takes a resource out of a request, as Nova reads it - that group asks for
    no GPU, and the flavor's other groups keep theirs."""
    assert gpu_requests({"resources:VGPU": "0", "trait:CUSTOM_X": "required"}) == []
    assert gpu_requests({"resources1:VGPU": "0", "resources2:VGPU": "1"}) == [
        vgpu(count=1, same_provider=True)
    ]


@pytest.mark.parametrize(
    "amount", ["", " ", "one", "-1", "1.5", "²", "1e3", "+0", "-0", "0_0", "٠"]
)
def test_an_amount_nova_skips_asks_for_no_gpu(amount):
    """SDD §22.2 (PR #57 review): Nova's RequestGroup.add_resource reads an amount with int() and
    skips a negative or unreadable one with a warning, and strip_zeros drops a zero in any form - a
    server boots with no vGPU for it, where this read one."""
    assert gpu_requests({"resources:VGPU": amount}) == []
    assert gpu_requests({"resources1:VGPU": amount, "resources2:VGPU": "1"}) == [
        vgpu(count=1, same_provider=True)
    ]


@pytest.mark.parametrize(
    ("amount", "count"), [("+2", 2), ("２", 2), ("1_0", 10), (" 3 ", 3), ("٣", 3), ("4", 4)]
)
def test_an_amount_reads_as_pythons_int_reads_it(amount, count):
    """SDD §22.2 (PR #57 review): a sign, underscores between digits, spaces around them and any
    script's decimal digits are an amount Nova's int() reads - "+2" asks for two vGPUs, not one."""
    assert gpu_requests({"resources:VGPU": amount}) == [vgpu(count=count)]


def test_an_alias_count_reads_as_nova_reads_it():
    """SDD §22.2 (PR #57 review): Nova asks for int(count) devices of an alias - a zero or negative
    count for none, "２" for two -, and a count it cannot read fails the boot: it reads as 1."""
    alias = "pci_passthrough:alias"
    assert gpu_requests({alias: "a100:0"}) == []
    assert gpu_requests({alias: "a100:-1, t4:1"}) == [GpuDevice(kind="passthrough", alias="t4")]
    assert gpu_requests({alias: "a100:２"}) == [
        GpuDevice(kind="passthrough", alias="a100", count=2)
    ]
    assert gpu_requests({alias: "a100: 3 , t4:x, l4"}) == [
        GpuDevice(kind="passthrough", alias="a100", count=3),
        GpuDevice(kind="passthrough", alias="t4", count=1),
        GpuDevice(kind="passthrough", alias="l4", count=1),
    ]


def test_a_declared_profile_goes_to_each_entry_of_its_flavor():
    """SDD §22.2, §22.3: the declared profile is each vGPU entry's profile; on a flavor whose GPU
    requests are PCI aliases alone it goes to those entries (a vGPU handed out as a PCI device),
    and never to an alias beside a vGPU request."""
    two = gpu_requests({"resources1:VGPU": "1", "resources2:VGPU": "1"}, declared_profile="A100-4C")
    assert [d.profile for d in two] == ["A100-4C", "A100-4C"]
    assert gpu_requests({"pci_passthrough:alias": "vgpu-vf:1"}, declared_profile="L4-24Q") == [
        GpuDevice(kind="passthrough", alias="vgpu-vf", count=1, profile="L4-24Q")
    ]
    mixed = gpu_requests(
        {"resources:VGPU": "1", "pci_passthrough:alias": "nic:1"}, declared_profile="A100-4C"
    )
    assert [(d.kind, d.profile) for d in mixed] == [("vgpu", "A100-4C"), ("passthrough", None)]
    assert gpu_requests({"resources:VGPU": "1"})[0].profile is None


def test_the_gpu_key_is_the_profile_or_the_vendor_and_device_ids():
    """SDD §22.2: what a plan's GPU mapping names for a VMware device (§22.4)."""
    assert gpu_key(GpuDevice(kind="vgpu", profile="grid_a100-4c")) == "grid_a100-4c"
    assert gpu_key(GpuDevice(kind="vgpu", profile="grid_a100-3-20c", count=2)) == "grid_a100-3-20c"
    assert gpu_key(GpuDevice(kind="passthrough", vendor_id="10de", device_id="20b5")) == (
        "10de:20b5"
    )


def test_a_passthrough_device_without_ids_has_no_key():
    """SDD §22.2: a device vCenter reports without both ids, a vGPU without a profile and an
    OpenStack alias have no key - no GPU mapping can name them."""
    for device in (
        GpuDevice(kind="passthrough"),
        GpuDevice(kind="passthrough", vendor_id="10de"),
        GpuDevice(kind="passthrough", device_id="20b5"),
        GpuDevice(kind="passthrough", alias="a100"),
        GpuDevice(kind="vgpu"),
    ):
        assert gpu_key(device) is None, device


@pytest.mark.parametrize(
    ("name", "size"),
    [
        ("grid_a100-4c", ProfileSize("A100", None, 4, "C")),
        ("A100-4C", ProfileSize("A100", None, 4, "C")),
        ("GRID A100-4C", ProfileSize("A100", None, 4, "C")),
        ("nvidia_l4-24q", ProfileSize("L4", None, 24, "Q")),
        ("T4-16Q", ProfileSize("T4", None, 16, "Q")),
        ("a16-2b", ProfileSize("A16", None, 2, "B")),
        ("A40-48A", ProfileSize("A40", None, 48, "A")),
        ("grid_a100-3-20c", ProfileSize("A100", 3, 20, "C")),
        ("A100D-7-80C", ProfileSize("A100D", 7, 80, "C")),
        ("nvidia_h100-1-10c", ProfileSize("H100", 1, 10, "C")),
    ],
)
def test_vgpu_profile_size_reads_time_sliced_and_mig_backed_names(name, size):
    """SDD §22.2: <gpu>-<framebuffer GB><series>, MIG-backed <gpu>-<slices>-<framebuffer GB>
    <series>, case-insensitive, with or without vCenter's grid_/nvidia_ prefix or NVIDIA's
    "GRID "."""
    assert vgpu_profile_size(name) == size


@pytest.mark.parametrize(
    "name",
    [
        "nvidia-471",
        "A100",
        "a100-4x",
        "A100-0C",
        "A100-0-20C",
        "A100--4C",
        "4C",
        "grid-a100-4c",
        "grid_a100-4c-extra",
        "A100-4C ",
        "",
    ],
)
def test_a_name_outside_nvidias_scheme_has_no_size(name):
    """SDD §22.2: an mdev type id, a bare GPU model or anything else has no size, and
    MAP_GPU_DOWNSIZED compares nothing for it (§22.5)."""
    assert vgpu_profile_size(name) is None


def test_pool_name_lists_the_class_then_required_then_forbidden_traits():
    """SDD §10, §22.7: the class, the required traits sorted, then the forbidden ones sorted,
    each prefixed with "!"."""
    assert pool_name("VGPU", ["CUSTOM_B", "CUSTOM_A"], ["CUSTOM_Z", "CUSTOM_Y"]) == (
        "VGPU CUSTOM_A CUSTOM_B !CUSTOM_Y !CUSTOM_Z"
    )
    assert pool_name("VGPU", [], []) == "VGPU"
    assert pool_name("CUSTOM_NVIDIA_A100_4C", [], ["CUSTOM_OLD"]) == (
        "CUSTOM_NVIDIA_A100_4C !CUSTOM_OLD"
    )


def test_gpu_flavor_is_the_one_flavor_a_vms_gpu_keys_map_to():
    """SDD §22.4 (plan task G4): a VMware VM's GPU flavor is the one flavor every GPU key of it maps
    to - none for a key no mapping names, a device without a key, or keys that map to different
    flavors -; an OpenStack VM's is its flavor's explicit mapping, never a same-named flavor; a VM
    without a GPU has none."""
    vgpu = GpuDevice(kind="vgpu", profile="grid_a100-4c")
    ids = GpuDevice(kind="passthrough", vendor_id="10de", device_id="20b5")
    vmw = ProviderKind.vmware

    def plan(**gpus):
        return make_plan(mappings=Mappings(gpus=gpus))

    both = make_vm(flavor=None, gpus=[vgpu, ids])
    assert gpu_flavor(both, plan(**{"grid_a100-4c": "g1", "10de:20b5": "g1"}), vmw) == "g1"
    assert gpu_flavor(both, plan(**{"grid_a100-4c": "g1", "10de:20b5": "p1"}), vmw) is None
    assert gpu_flavor(both, plan(**{"grid_a100-4c": "g1"}), vmw) is None
    no_key = make_vm(flavor=None, gpus=[vgpu, GpuDevice(kind="passthrough")])
    assert gpu_flavor(no_key, plan(**{"grid_a100-4c": "g1"}), vmw) is None
    specs = {"resources:VGPU": "1"}
    osp = make_vm(flavor="g1.vgpu", flavor_extra_specs=specs, gpus=gpu_requests(specs))
    openstack = ProviderKind.openstack
    assert (
        gpu_flavor(osp, make_plan(mappings=Mappings(flavors={"g1.vgpu": "g1"})), openstack) == "g1"
    )
    assert gpu_flavor(osp, make_plan(), openstack) is None
    assert (
        gpu_flavor(make_vm(), make_plan(mappings=Mappings(flavors={"m1.small": "g1"})), openstack)
        is None
    )
