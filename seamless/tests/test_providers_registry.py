"""ProviderRegistry caching (SDD §10): one implementation per provider configuration, rebuilt
when the provider document or the mounted credentials change."""

import os

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.providers.base import ProviderError
from seamless_migrate.providers.registry import ProviderRegistry
from tests.factories import make_provider


def test_registry_reuses_until_the_provider_or_clouds_yaml_changes(tmp_path):
    clouds = tmp_path / "clouds.yaml"
    clouds.write_text("clouds: {src: {auth: {password: one}}}\n")
    settings = Settings(data_dir=tmp_path / "data", clouds_yaml=clouds)
    registry = ProviderRegistry(settings)
    provider = make_provider(id="src-1", cloud="src")

    first = registry.get(provider)
    assert registry.get(provider) is first
    assert registry.get(provider.model_copy(update={"status": "ok"})) is first  # state only

    # a rotated clouds.yaml (new content, new mtime) reconnects
    clouds.write_text("clouds: {src: {auth: {password: two-rotated}}}\n")
    os.utime(clouds, ns=(1, clouds.stat().st_mtime_ns + 1_000_000_000))
    rotated = registry.get(provider)
    assert rotated is not first
    assert registry.get(provider) is rotated

    # the replaced implementation is closed (its session must not linger)
    closed: list[str] = []
    rotated.close = lambda: closed.append("rotated")  # type: ignore[attr-defined]
    # a changed endpoint rebuilds; forget() drops the cache
    changed = provider.model_copy(update={"endpoint": "https://other.example:5000/v3"})
    assert registry.get(changed) is not rotated
    assert closed == ["rotated"]
    registry.forget(provider.id)
    assert registry.get(changed) is not registry.get(provider)


async def test_a_demo_fake_rebuilt_for_a_changed_document_keeps_what_its_cloud_holds(tmp_path):
    """SDD §10 (plan task G9 review): a demo provider's document changed - a rename, its declared
    profiles, a written credential - rebuilds its fake, which takes over what the simulated cloud
    holds, as a real cloud keeps it: the servers the cutovers created with their flavors and GPU
    units, the servers the rollbacks deleted, the gate's reads of the other tenant's GPU, and the
    sources' power states. The rebuilt fake reads the new document, and a fake replaced before
    the rebuild still writes to the same cloud."""
    from seamless_migrate.demo import DEMO_PROVIDERS
    from seamless_migrate.planning.gpu import gpu_requests

    settings = Settings(data_dir=tmp_path / "data", demo=True)
    registry = ProviderRegistry(settings)
    providers = {p.id: p for p in DEMO_PROVIDERS}
    rhoso, vcenter = providers["rhoso18"], providers["vcenter-dc2"]
    old = registry.get(rhoso)
    await old.create_server("sim-mig-00000000c1", "g1.a100-4c")
    await old.create_server("sim-mig-00000000c2", "g1.a100-4c")
    await old.delete_server("sim-mig-00000000c2")
    mig = gpu_requests({"resources:VGPU": "1", "trait:CUSTOM_NVIDIA_A100_3_20C": "required"})
    assert [p.free for p in (await old.gpu_capacity(mig)).pools] == [0, 3]  # the tenant's first
    source = registry.get(vcenter)
    [vm, *_] = [v for v in await source.list_vms() if v.power_state == "running"]
    await source.power_off(vm.source_id)

    renamed = rhoso.model_copy(update={"name": "RHOSO 18.0 (DC2)", "gpu_profiles": {}})
    new = registry.get(renamed)
    assert new is not old and new.provider == renamed
    assert (await new.get_server("sim-mig-00000000c1"))["flavor"]["name"] == "g1.a100-4c"
    assert await new.server_allocations("sim-mig-00000000c1") == {"VGPU": 1}
    with pytest.raises(ProviderError):
        await new.get_server("sim-mig-00000000c2")
    pools = (await new.inventory()).gpu_capacity.pools
    assert [p.free for p in pools] == [3, 2, 5]
    # the declared profiles are the new document's
    assert all(g.profile is None for f in await new.gpu_flavors() for g in f["gpus"])
    # the gate's second read still finds the tenant's GPU full, the third finds it free
    assert [p.free for p in (await new.gpu_capacity(mig)).pools] == [0, 3]
    assert [p.free for p in (await new.gpu_capacity(mig)).pools] == [2, 5]
    # a call on the replaced fake lands in the same cloud
    await old.create_server("sim-mig-00000000c3", "g1.a100-4c")
    assert await new.server_allocations("sim-mig-00000000c3") == {"VGPU": 1}

    rebuilt_source = registry.get(vcenter.model_copy(update={"name": "vCenter DC2 (renamed)"}))
    assert rebuilt_source is not source
    assert (await rebuilt_source.get_vm(vm.source_id)).power_state == "stopped"
