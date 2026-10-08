"""``/providers`` routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from ..domain.enums import PlanStatus, ProviderRole, Role
from ..domain.models import Plan, Provider
from ..events import emit
from ..security.auth import Principal
from ..store import ConflictError
from .deps import ApiError, require_role, services

router = APIRouter(tags=["providers"])
TERMINAL_PLAN_STATUSES = frozenset({PlanStatus.completed, PlanStatus.failed})


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
        update={
            "status": "unknown",
            "status_message": None,
            "last_checked_at": None,
            "capabilities": {},
        }
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
