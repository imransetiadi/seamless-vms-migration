"""Build provider implementations from :class:`Provider` documents (SDD §10)."""

from __future__ import annotations

import os
import threading
from typing import Any

from ..config import Settings
from ..domain.enums import ProviderKind, ProviderRole
from ..domain.models import Provider
from .fake import FakeDestinationProvider, FakeSourceProvider
from .openstack import OpenStackProvider
from .vmware import VMwareProvider

# Fields that do not change how a provider is reached.
_STATE_FIELDS = {"status", "status_message", "last_checked_at", "capabilities"}


def build(provider: Provider, settings: Settings) -> Any:
    """Return the implementation for ``provider`` (demo fakes when ``settings.demo``)."""
    if settings.demo:
        if provider.role == ProviderRole.destination:
            return FakeDestinationProvider(seed=settings.demo_seed)
        return FakeSourceProvider(provider.kind, seed=settings.demo_seed)
    if provider.kind == ProviderKind.vmware:
        return VMwareProvider(provider, settings)
    return OpenStackProvider(provider, settings)


class ProviderRegistry:
    """Caches one implementation per provider configuration (connections are reused)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[str, tuple[str, Any]] = {}
        self._lock = threading.Lock()

    def _credentials_stamp(self) -> str:
        """Change marker of the mounted credential files (a rotated clouds.yaml reconnects)."""
        if self.settings.clouds_yaml is None:
            return "-"  # credentials from the secret store: the provider document's stamp moves
        try:
            st = os.stat(self.settings.clouds_yaml)
        except OSError:
            return "-"
        return f"{st.st_mtime_ns}:{st.st_size}"

    def get(self, provider: Provider) -> Any:
        fingerprint = provider.model_dump_json(exclude=_STATE_FIELDS)
        if not self.settings.demo:
            fingerprint += "|" + self._credentials_stamp()
        with self._lock:
            cached = self._cache.get(provider.id)
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
            impl = build(provider, self.settings)
            self._cache[provider.id] = (fingerprint, impl)
        if cached is not None:
            _close(cached[1])  # the replaced implementation must not keep its session
        return impl

    def forget(self, provider_id: str) -> None:
        with self._lock:
            cached = self._cache.pop(provider_id, None)
        if cached is not None:
            _close(cached[1])


def _close(impl: Any) -> None:
    close = getattr(impl, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # best effort
            pass
