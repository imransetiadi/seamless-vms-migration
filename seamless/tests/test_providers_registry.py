"""ProviderRegistry caching (SDD §10): one implementation per provider configuration, rebuilt
when the provider document or the mounted credentials change."""

import os

from seamless_migrate.config import Settings
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
