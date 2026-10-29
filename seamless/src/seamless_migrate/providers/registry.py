"""Build provider implementations from :class:`Provider` documents (SDD §10)."""

from __future__ import annotations

import os
import threading
from typing import Any

from ..config import Settings
from ..domain.enums import ProviderKind, ProviderRole
from ..domain.models import Provider
from .base import ProviderError
from .fake import FakeDestinationProvider, FakeSourceProvider
from .openstack import OpenStackProvider
from .vmware import VMwareProvider

# Fields that do not change how a provider is reached.
_STATE_FIELDS = {"status", "status_message", "last_checked_at", "capabilities"}


def build(provider: Provider, settings: Settings) -> Any:
    """Return the implementation for ``provider`` (demo fakes when ``settings.demo``)."""
    if settings.demo:
        if provider.role == ProviderRole.destination:
            return FakeDestinationProvider(seed=settings.demo_seed, provider=provider)
        return FakeSourceProvider(provider.kind, seed=settings.demo_seed)
    if provider.kind == ProviderKind.vmware:
        return VMwareProvider(provider, settings)
    return OpenStackProvider(provider, settings)


def _credentials_stamp(settings: Settings) -> str:
    """Change marker of the mounted credential files (a rotated clouds.yaml reconnects)."""
    if settings.clouds_yaml is None:
        return "-"  # credentials from the secret store: the provider document's stamp moves
    try:
        st = os.stat(settings.clouds_yaml)
    except OSError:
        return "-"
    return f"{st.st_mtime_ns}:{st.st_size}"


def connection_fingerprint(
    provider: Provider, settings: Settings, project_id: str | None = None
) -> str:
    """How ``provider`` is reached: its document without its state - each project credential's
    ``updated_at`` in it -, and the mounted credentials' change marker. With ``project_id``, how
    its credential for that project is: the provider's reach without its project credentials,
    and that project credential's own entry, so a rotation of one project's credential rebuilds
    that project's connection alone. A connection built for another fingerprint is built again -
    a credential rotated while a plan runs (SDD §10, §13.3, §27.5)."""
    if project_id is None:
        fingerprint = provider.model_dump_json(exclude=_STATE_FIELDS)
    else:
        credential = provider.project_credentials[project_id]
        fingerprint = (
            provider.model_dump_json(exclude=_STATE_FIELDS | {"project_credentials"})
            + f"|{project_id}|"
            + credential.model_dump_json()
        )
    if not settings.demo:
        fingerprint += "|" + _credentials_stamp(settings)
    return fingerprint


class ProviderRegistry:
    """Caches one implementation per provider configuration, and one per provider and project
    credential (connections are reused until how they are reached changes)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[str, tuple[str, Any]] = {}
        #: by (provider id, project id): a step's implementation of a project credential (§27.5)
        self._projects: dict[tuple[str, str], tuple[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, provider: Provider, project_id: str | None = None) -> Any:
        """The provider's implementation (SDD §10): of its own credential, else - with
        ``project_id`` - of its credential for that Keystone project, which a step of a plan with
        projects acts with (§8's selection, §27.5); each cached under its fingerprint."""
        if project_id is not None:
            return self._project(provider, project_id)
        fingerprint = connection_fingerprint(provider, self.settings)
        with self._lock:
            cached = self._cache.get(provider.id)
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
            impl = build(provider, self.settings)
            if cached is not None:
                _carry_state(cached[1], impl)
            self._cache[provider.id] = (fingerprint, impl)
        if cached is not None:
            _close(cached[1])  # the replaced implementation must not keep its session
        return impl

    def for_project(self, provider: Provider, project_id: str) -> Any:
        """An implementation that acts with the provider's credential for the Keystone project
        ``project_id`` (SDD §10, §13.3, §27.4, plan task TC4): an OpenStackProvider on a connection
        of its own, built for the call and never cached, so each read signs in afresh with the
        credential as an admin last wrote it; its caller closes it after its read. The demo's fakes
        cannot sign in with a project credential until plan task TC9 (§27.10), nor can a VMware
        provider, which holds none: a provider error."""
        if self.settings.demo or provider.kind == ProviderKind.vmware:
            raise ProviderError(f"{provider.id}: it cannot sign in with a project credential")
        return OpenStackProvider(provider, self.settings, project_id=project_id)

    def _project(self, provider: Provider, project_id: str) -> Any:
        """A step's implementation of the provider's credential for the project (SDD §10, §27.5):
        an OpenStackProvider of that project, cached under ``connection_fingerprint`` - a
        credential an admin wrote again signs in at the next step, and the replaced one is closed.
        A project the provider holds no credential for is a provider error, never its own
        credential; the demo's fakes and a VMware provider sign in with none (plan task TC9)."""
        if self.settings.demo or provider.kind == ProviderKind.vmware:
            raise ProviderError(f"{provider.id}: it cannot sign in with a project credential")
        key = (provider.id, project_id)
        if project_id not in provider.project_credentials:
            with self._lock:
                gone = self._projects.pop(key, None)
            if gone is not None:
                _close(gone[1])  # a deleted credential's connection goes with it
            raise ProviderError(f"{provider.id}: it holds no credential for project {project_id}")
        fingerprint = connection_fingerprint(provider, self.settings, project_id)
        with self._lock:
            cached = self._projects.get(key)
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
            impl = OpenStackProvider(provider, self.settings, project_id=project_id)
            self._projects[key] = (fingerprint, impl)
        if cached is not None:
            _close(cached[1])  # the replaced connection must not keep its session
        return impl

    def forget(self, provider_id: str) -> None:
        with self._lock:
            cached = self._cache.pop(provider_id, None)
            projects = [
                self._projects.pop(key) for key in list(self._projects) if key[0] == provider_id
            ]
        for _, impl in ([cached] if cached is not None else []) + projects:
            _close(impl)


def _carry_state(old: Any, new: Any) -> None:
    """A demo fake rebuilt for a changed document takes over what its simulated cloud holds
    (SDD §10): a real cloud keeps its servers and its VMs' power whatever the provider's document
    says. A real provider holds no such state, and a new connection has nothing to carry."""
    adopt = getattr(new, "adopt", None)
    if callable(adopt) and type(old) is type(new):
        adopt(old)


def _close(impl: Any) -> None:
    close = getattr(impl, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # best effort
            pass
