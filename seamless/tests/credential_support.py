"""Credentials in tests (SDD §27.5, §27.10, plan task TC5): every fake provider call recorded with
the credential it came with - the provider's own, named by the provider's id, or a project
credential, ``{provider id}/{project id}`` -, so a test fails a step that used the provider's
credential where §27.5 names the plan's, or the reverse. Project ids are made up in the test (32
hex digits), names are ``tenant-a``..``tenant-d``, application credentials are generated."""

from __future__ import annotations

import inspect
import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from seamless_migrate.domain.enums import ProviderRole
from seamless_migrate.domain.models import ProjectCredential, Provider, utcnow
from seamless_migrate.providers.base import ProviderError
from seamless_migrate.security.secret_store import project_secret_name


def own(provider: Provider) -> str:
    """How a call made with the provider's own credential is recorded."""
    return provider.id


def of_project(provider: Provider, project_id: str) -> str:
    """How a call made with the provider's credential for the project is recorded."""
    return f"{provider.id}/{project_id}"


class Credentialed:
    """``impl`` as one credential reaches it: each call of one of its coroutine methods recorded in
    ``calls`` as ``(credential, method)``, and refused as Keystone refuses a revoked credential
    when ``refused``; anything else is ``impl``'s own."""

    def __init__(
        self, impl: Any, credential: str, calls: list[tuple[str, str]], refused: bool = False
    ) -> None:
        self._impl, self._credential, self._calls = impl, credential, calls
        self._refused = refused

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._impl, name)
        if not inspect.iscoroutinefunction(attr):
            return attr
        credential, calls, refused = self._credential, self._calls, self._refused

        async def call(*args: Any, **kwargs: Any) -> Any:
            calls.append((credential, name))
            if refused:
                raise ProviderError("the sign-in failed: HTTP 401")
            return await attr(*args, **kwargs)

        return call


class CredentialRegistry:
    """A provider registry over one double per side (SDD §10, §27.5): ``get(provider)`` acts with
    the provider's own credential, ``get(provider, project_id)`` with its project credential - one
    the provider holds, else a provider error, never its own -; every call is recorded. A
    credential of ``by_credential`` reaches its own double, a project of ``revoked`` is refused at
    each call (its credential cannot sign in)."""

    def __init__(self, source: Any, destination: Any) -> None:
        self.source, self.destination = source, destination
        self.calls: list[tuple[str, str]] = []
        self.by_credential: dict[str, Any] = {}
        self.revoked: set[str] = set()

    def _double(self, provider: Provider, credential: str) -> Any:
        if credential in self.by_credential:
            return self.by_credential[credential]
        return self.destination if provider.role == ProviderRole.destination else self.source

    def get(self, provider: Provider, project_id: str | None = None) -> Any:
        if project_id is None:
            credential = own(provider)
            return Credentialed(self._double(provider, credential), credential, self.calls)
        if project_id not in provider.project_credentials:
            raise ProviderError(f"{provider.id}: it holds no credential for project {project_id}")
        credential = of_project(provider, project_id)
        double = self._double(provider, credential)
        return Credentialed(double, credential, self.calls, project_id in self.revoked)

    def used(self, method: str) -> list[str]:
        """The credentials ``method`` was called with, in order."""
        return [credential for credential, called in self.calls if called == method]

    def methods(self, credential: str) -> set[str]:
        """The methods called with ``credential``."""
        return {called for used, called in self.calls if used == credential}


def tenant_project() -> str:
    """A made-up Keystone project id: 32 hex digits."""
    return secrets.token_hex(16)


def project_credentials(provider_id: str, *project_ids: str) -> dict[str, ProjectCredential]:
    """A provider's project credentials for the projects (SDD §4.2, §27.4)."""
    return {
        project_id: ProjectCredential(
            secret=project_secret_name(provider_id, project_id), updated_at=utcnow()
        )
        for project_id in project_ids
    }


def with_projects(h: Any, plan: Any, *, roles: tuple[str, ...] = ("member", "reader")) -> Any:
    """The harness's providers (tests/orch_support.py) each hold the credential of a made-up tenant
    project - tenant-a at the source, tenant-c at the destination -, their checks sign in with it
    with ``roles``, the plan names them, and a registry that records each call's credential
    replaces the harness's (SDD §27.5, plan task TC5). Returns ``(registry, projects)``, projects
    holding ``source``, ``destination`` and the stored ``plan``."""
    named = {"source": tenant_project(), "destination": tenant_project()}
    for side, provider_id, double, name in (
        ("source", plan.source_provider_id, h.source, "tenant-a"),
        ("destination", plan.destination_provider_id, h.destination, "tenant-c"),
    ):
        provider = h.store.get("provider", provider_id, Provider)
        credentials = project_credentials(provider_id, named[side])
        h.store.put("provider", provider.model_copy(update={"project_credentials": credentials}))
        double.check_project = _signing_in(named[side], name, roles)
    registry = CredentialRegistry(h.source, h.destination)
    h.orch.providers = registry
    stored = plan.model_copy(
        update={"source_project": named["source"], "destination_project": named["destination"]}
    )
    h.store.put("plan", stored)
    return registry, SimpleNamespace(**named, plan=stored)


def _signing_in(project_id: str, name: str, roles: tuple[str, ...]) -> Any:
    """A provider's sign-in with its credential for ``project_id`` (SDD §10 check_project)."""

    async def check_project(asked: str) -> dict[str, Any]:
        if asked != project_id:
            raise ProviderError(f"the sign-in failed for project {asked}: HTTP 401")
        return {
            "project_id": project_id,
            "project_name": name,
            "domain": "Default",
            "user_id": f"user-{name}",
            "roles": list(roles),
            "restricted": True,
            "expires_at": None,
        }

    return check_project


def write_application_credential(secrets_dir: Path, provider_id: str, project_id: str) -> str:
    """The secret of the provider's credential for the project, as PUT .../credentials writes it to
    the files store (SDD §13.3): a generated application credential's id and secret. Returns the
    id."""
    folder = Path(secrets_dir) / project_secret_name(provider_id, project_id)
    folder.mkdir(parents=True, exist_ok=True)
    credential_id = secrets.token_hex(16)
    (folder / "application_credential_id").write_text(credential_id)
    (folder / "application_credential_secret").write_text(secrets.token_urlsafe(32))
    return credential_id
