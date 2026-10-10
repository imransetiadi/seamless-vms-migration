"""``/providers`` routes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Request, Response
from pydantic import ValidationError

from ..domain.enums import PlanStatus, ProviderKind, ProviderRole, Role
from ..domain.models import ConversionHostConfig, Plan, Provider, utcnow
from ..events import emit
from ..security.auth import Principal
from ..security.secret_store import (
    SecretStoreError,
    conversion_key_secret_name,
    credentials_secret_name,
    secret_store,
)
from ..store import ConflictError
from .deps import ApiError, require_role, services
from .schemas import ConversionKeyRequest, ProviderCredentialsRequest

log = logging.getLogger(__name__)
router = APIRouter(tags=["providers"])
#: plans that release their providers: only a completed one — a failed plan can be started
#: again (SDD §8, §12)
TERMINAL_PLAN_STATUSES = frozenset({PlanStatus.completed})
#: plans whose provider must not change under them (PATCH answers 409)
ACTIVE_PLAN_STATUSES = frozenset({PlanStatus.running, PlanStatus.paused})
#: PATCH /providers/{id} (SDD §12); id, kind, role and server-owned fields are immutable
PROVIDER_EDITABLE_FIELDS = frozenset(
    {
        "name",
        "endpoint",
        "cloud",
        "credentials_secret",
        "region",
        "verify_tls",
        "ca_cert_path",
        "conversion_host",
        "distribution",
    }
)
_SERVER_OWNED = {
    "status": "unknown",
    "status_message": None,
    "last_checked_at": None,
    "capabilities": {},
}


def _summarize(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "; ".join(parts) or "invalid provider"


async def _plans_using(request: Request, provider_id: str, statuses: frozenset[str]) -> list[str]:
    return [
        p.id
        for p in await services(request).db.list("plan", Plan)
        if p.status in statuses and provider_id in (p.source_provider_id, p.destination_provider_id)
    ]


def _credential_values(provider: Provider, body: ProviderCredentialsRequest) -> dict[str, str]:
    """The keys to store for ``provider`` (SDD §13.3), or a 422 naming what is missing."""
    values = body.values()
    if provider.kind == ProviderKind.vmware:
        allowed = {"username", "password", "datacenter"}
        required = ("username", "password")
    else:
        allowed = set(ProviderCredentialsRequest.model_fields) - {"datacenter"}
        app = bool(
            values.get("application_credential_id") or values.get("application_credential_secret")
        )
        required = (
            ("application_credential_id", "application_credential_secret")
            if app
            else ("username", "password", "project_name")
        )
    extra = sorted(set(values) - allowed)
    if extra:
        raise ApiError(
            422, "validation_error", f"not used by a {provider.kind} provider: {', '.join(extra)}"
        )
    missing = [k for k in required if not values.get(k)]
    if missing:
        raise ApiError(422, "validation_error", f"missing: {', '.join(missing)}")
    return values


@router.get("/providers", response_model=list[Provider])
async def list_providers(
    request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> list[Provider]:
    return await services(request).db.list("provider", Provider)


@router.post("/providers", response_model=Provider, status_code=201)
async def create_provider(
    body: Provider, request: Request, principal: Principal = Depends(require_role(Role.admin))
) -> Provider:
    svc = services(request)
    provider = body.model_copy(
        update={**_SERVER_OWNED, "credentials_updated_at": None, "conversion_key_updated_at": None}
    )
    try:
        await svc.db.put("provider", provider, expected_version=0)
    except ConflictError:
        raise ApiError(409, "conflict", f"provider {provider.id!r} already exists") from None
    await emit(
        svc.store,
        svc.bus,
        "provider.created",
        f"provider {provider.id} created",
        actor=principal.name,
        data={"provider_id": provider.id, "kind": str(provider.kind), "role": str(provider.role)},
    )
    return provider


@router.get("/providers/{provider_id}", response_model=Provider)
async def get_provider(
    provider_id: str, request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> Provider:
    return await services(request).db.get("provider", provider_id, Provider)


@router.patch("/providers/{provider_id}", response_model=Provider)
async def update_provider(
    provider_id: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.admin)),
) -> Provider:
    svc = services(request)
    unknown = sorted(set(body) - PROVIDER_EDITABLE_FIELDS)
    if unknown:
        raise ApiError(422, "validation_error", f"fields cannot be changed: {', '.join(unknown)}")
    provider, version = await svc.db.get_versioned("provider", provider_id, Provider)
    users = await _plans_using(request, provider_id, ACTIVE_PLAN_STATUSES)
    if users:
        raise ApiError(
            409, "conflict", f"provider is used by running or paused plan(s): {', '.join(users)}"
        )
    try:
        updated = Provider.model_validate(
            {**provider.model_dump(mode="json"), **body, **_SERVER_OWNED}
        )
    except ValidationError as exc:
        raise ApiError(422, "validation_error", _summarize(exc)) from None
    await svc.db.put("provider", updated, expected_version=version)
    await emit(
        svc.store,
        svc.bus,
        "provider.updated",
        f"provider {provider_id} updated",
        actor=principal.name,
        data={"provider_id": provider_id, "fields": sorted(body)},
    )
    return updated


async def _store_secret(
    request: Request, name: str, values: dict[str, str], provider_id: str
) -> None:
    store = secret_store(services(request).settings)
    try:
        await asyncio.to_thread(store.write, name, values, {"seamless.io/provider": provider_id})
    except SecretStoreError as exc:
        raise ApiError(503, "unavailable", f"secret store: {exc}") from None


@router.put("/providers/{provider_id}/credentials", response_model=Provider)
async def set_provider_credentials(
    provider_id: str,
    body: ProviderCredentialsRequest,
    request: Request,
    principal: Principal = Depends(require_role(Role.admin)),
) -> Provider:
    """Write-only (SDD §12/§13.3): the values go to the secret store, never to the database."""
    svc = services(request)
    provider, version = await svc.db.get_versioned("provider", provider_id, Provider)
    values = _credential_values(provider, body)
    name = credentials_secret_name(provider_id)
    await _store_secret(request, name, values, provider_id)
    updated = provider.model_copy(
        update={"credentials_secret": name, "credentials_updated_at": utcnow(), **_SERVER_OWNED}
    )
    await svc.db.put("provider", updated, expected_version=version)
    await emit(
        svc.store,
        svc.bus,
        "provider.credentials_updated",
        f"credentials of provider {provider_id} updated",
        actor=principal.name,
        data={"provider_id": provider_id, "secret": name, "keys": sorted(values)},
    )
    return updated


@router.put("/providers/{provider_id}/conversion-key", response_model=Provider)
async def set_conversion_key(
    provider_id: str,
    body: ConversionKeyRequest,
    request: Request,
    principal: Principal = Depends(require_role(Role.admin)),
) -> Provider:
    """Write-only SSH private key of an existing conversion host (SDD §12/§13.3)."""
    svc = services(request)
    provider, version = await svc.db.get_versioned("provider", provider_id, Provider)
    key = body.private_key.strip()
    if "PRIVATE KEY-----" not in key or not key.startswith("-----BEGIN"):
        raise ApiError(422, "validation_error", "private_key must be an OpenSSH or PEM private key")
    name = conversion_key_secret_name(provider_id)
    await _store_secret(request, name, {"private_key": key + "\n"}, provider_id)
    host = (provider.conversion_host or ConversionHostConfig()).model_copy(
        update={"ssh_key_secret": name}
    )
    updated = provider.model_copy(
        update={
            "conversion_host": host,
            "conversion_key_updated_at": utcnow(),
            **_SERVER_OWNED,
        }
    )
    await svc.db.put("provider", updated, expected_version=version)
    await emit(
        svc.store,
        svc.bus,
        "provider.credentials_updated",
        f"conversion-host key of provider {provider_id} updated",
        actor=principal.name,
        data={"provider_id": provider_id, "secret": name, "keys": ["private_key"]},
    )
    return updated


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: str, request: Request, principal: Principal = Depends(require_role(Role.admin))
) -> Response:
    svc = services(request)
    await svc.db.get("provider", provider_id, Provider)  # 404 when missing
    users = [
        p.id
        for p in await svc.db.list("plan", Plan)
        if p.status not in TERMINAL_PLAN_STATUSES
        and provider_id in (p.source_provider_id, p.destination_provider_id)
    ]
    if users:
        raise ApiError(409, "conflict", f"provider is used by plan(s): {', '.join(users)}")
    await svc.db.delete("provider", provider_id)
    forget = getattr(svc.providers, "forget", None)
    if forget is not None:
        forget(provider_id)
    # the secrets the store manages for this provider go with it (SDD §13.3)
    store = secret_store(svc.settings)
    for name in (credentials_secret_name(provider_id), conversion_key_secret_name(provider_id)):
        try:
            await asyncio.to_thread(store.delete, name)
        except SecretStoreError as exc:
            log.warning("could not delete secret %s of provider %s: %s", name, provider_id, exc)
    await emit(
        svc.store,
        svc.bus,
        "provider.deleted",
        f"provider {provider_id} deleted",
        actor=principal.name,
        data={"provider_id": provider_id},
    )
    return Response(status_code=204)


@router.post("/providers/{provider_id}/check", response_model=Provider)
async def check_provider(
    provider_id: str, request: Request, principal: Principal = Depends(require_role(Role.operator))
) -> Provider:
    return await services(request).orchestrator.check_provider(provider_id, principal.name)


@router.get("/providers/{provider_id}/inventory")
async def provider_inventory(
    provider_id: str, request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> Any:
    svc = services(request)
    provider = await svc.db.get("provider", provider_id, Provider)
    impl = svc.providers.get(provider)
    if provider.role == ProviderRole.source:
        return [vm.model_dump(mode="json") for vm in await impl.list_vms()]
    return (await impl.inventory()).to_dict()
