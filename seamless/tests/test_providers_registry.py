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


async def test_a_rotated_project_credential_signs_in_at_the_next_step(tmp_path, monkeypatch):
    """SDD §10, §13.3, §27.5 (plan task TC5): a step's implementation of a project credential is
    the registry's, reused while that credential stays as it is; once an admin writes it again -
    its updated_at moves -, the next step signs in with the new one, through Seamless's own
    connect() over the in-process keystoneauth session, and the old connection is closed. Another
    project's rotation and a change of the provider's state rebuild nothing; a project the provider
    holds no credential for is a provider error, never its own credential; forget() closes them
    all."""
    import functools
    from datetime import timedelta

    from seamless_migrate.domain.enums import ProviderKind, ProviderRole
    from seamless_migrate.domain.models import utcnow
    from seamless_migrate.providers import registry as registry_module
    from seamless_migrate.providers.openstack import OpenStackProvider
    from tests.cloud_support import AUTH_URL, InProcessCloud
    from tests.credential_support import project_credentials, tenant_project
    from tests.test_api_resources import _keystone_project

    cloud = InProcessCloud(tmp_path / "cloud")
    closed: list[str | None] = []

    def connecting(provider, settings, project_id=None):
        conn = cloud.connect(provider, settings, project_id)
        conn.close = lambda: closed.append(project_id)
        return conn

    monkeypatch.setattr(
        registry_module,
        "OpenStackProvider",
        functools.partial(OpenStackProvider, connect_fn=connecting),
    )
    tenant_a, tenant_b = _keystone_project(cloud, "tenant-a"), _keystone_project(cloud, "tenant-b")
    provider = make_provider(
        id="dst-rhoso",
        kind=ProviderKind.rhoso,
        role=ProviderRole.destination,
        cloud="dst",
        endpoint=AUTH_URL,
        conversion_host=None,
        project_credentials=project_credentials(
            "dst-rhoso", tenant_a.project_id, tenant_b.project_id
        ),
    )

    def write(tenant, credential_id, secret):
        folder = cloud.settings.secrets_dir / provider.project_credentials[tenant.project_id].secret
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "application_credential_id").write_text(credential_id)
        (folder / "application_credential_secret").write_text(secret)

    for tenant in (tenant_a, tenant_b):
        write(tenant, tenant.credential_id, tenant.secret)
    registry = ProviderRegistry(cloud.settings)

    first = registry.get(provider, tenant_a.project_id)
    other = registry.get(provider, tenant_b.project_id)
    await first.find_server("web-01")
    await other.find_server("web-01")
    assert cloud.sign_ins == [tenant_a.credential_id, tenant_b.credential_id]
    # the next step, the credential as it was: the same connection, no sign-in
    again = registry.get(provider.model_copy(update={"status": "ok"}), tenant_a.project_id)
    assert again is first
    await again.find_server("web-02")
    assert len(cloud.sign_ins) == 2 and closed == []

    # an admin writes tenant-a's credential again: a new application credential, a new updated_at
    rotated_id, rotated_secret = tenant_project(), tenant_project()
    cloud.app_credentials[rotated_id] = {
        **cloud.app_credentials[tenant_a.credential_id],
        "secret": rotated_secret,
    }
    del cloud.app_credentials[tenant_a.credential_id]  # the old one deleted in Keystone
    write(tenant_a, rotated_id, rotated_secret)
    written = provider.project_credentials[tenant_a.project_id].model_copy(
        update={"updated_at": utcnow() + timedelta(seconds=1)}
    )
    rotated = provider.model_copy(
        update={
            "project_credentials": {**provider.project_credentials, tenant_a.project_id: written}
        }
    )
    second = registry.get(rotated, tenant_a.project_id)
    assert second is not first and closed == [tenant_a.project_id]
    await second.find_server("web-01")
    assert cloud.sign_ins[-1] == rotated_id
    # tenant-b's credential did not change: its connection stays
    assert registry.get(rotated, tenant_b.project_id) is other

    # no credential for a project: a provider error, nothing signed in, never the provider's own
    signed = list(cloud.sign_ins)
    with pytest.raises(ProviderError, match="holds no credential for project"):
        registry.get(rotated, tenant_project())
    assert cloud.sign_ins == signed and "svc" not in cloud.sign_ins
    registry.forget(provider.id)
    assert sorted(closed) == sorted([tenant_a.project_id, tenant_a.project_id, tenant_b.project_id])


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
