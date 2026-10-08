"""``/migrations`` routes (actions are delegated to the orchestrator)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ..domain.enums import Phase, Role
from ..domain.models import Migration
from ..security.auth import Principal
from .deps import require_role, services
from .schemas import (
    ApproveRequest,
    CancelRequest,
    CutoverRequest,
    FinalizeRequest,
    RollbackRequest,
    StrategyRequest,
)

router = APIRouter(tags=["migrations"])


@router.get("/migrations", response_model=list[Migration])
async def list_migrations(
    request: Request,
    plan_id: str | None = Query(default=None),
    phase: Phase | None = Query(default=None),
    wave_id: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=5000),
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    _: Principal = Depends(require_role(Role.viewer)),
) -> list[Migration]:
    """Migrations in creation order; ``limit``/``offset`` page large plans (SDD §12)."""
    filters: dict[str, Any] = {}
    if plan_id is not None:
        filters["plan_id"] = plan_id
    if phase is not None:
        filters["phase"] = phase
    if wave_id is not None:
        filters["wave_id"] = wave_id
    return await services(request).db.list(
        "migration", Migration, limit=limit, offset=offset, **filters
    )


@router.get("/migrations/{migration_id}", response_model=Migration)
async def get_migration(
    migration_id: str, request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> Migration:
    return await services(request).db.get("migration", migration_id, Migration)


@router.post("/migrations/{migration_id}/approve", response_model=Migration)
async def approve(
    migration_id: str,
    request: Request,
    body: ApproveRequest | None = None,
    principal: Principal = Depends(require_role(Role.approver)),
) -> Migration:
    comment = body.comment if body else None
    return await services(request).orchestrator.approve(migration_id, principal.name, comment)


@router.post("/migrations/{migration_id}/cutover", response_model=Migration)
async def cutover(
    migration_id: str,
    request: Request,
    body: CutoverRequest | None = None,
    principal: Principal = Depends(require_role(Role.approver)),
) -> Migration:
    body = body or CutoverRequest()
    return await services(request).orchestrator.request_cutover(
        migration_id, principal.name, force_window=body.force_window, comment=body.comment
    )


@router.post("/migrations/{migration_id}/sync", response_model=Migration)
async def sync(
    migration_id: str,
    request: Request,
    principal: Principal = Depends(require_role(Role.operator)),
) -> Migration:
    return await services(request).orchestrator.request_sync(migration_id, principal.name)


@router.post("/migrations/{migration_id}/rollback", response_model=Migration)
async def rollback(
    migration_id: str,
    body: RollbackRequest,
    request: Request,
    principal: Principal = Depends(require_role(Role.operator)),
) -> Migration:
    return await services(request).orchestrator.rollback(migration_id, principal.name, body.reason)


@router.post("/migrations/{migration_id}/retry", response_model=Migration)
async def retry(
    migration_id: str,
    request: Request,
    principal: Principal = Depends(require_role(Role.operator)),
) -> Migration:
    return await services(request).orchestrator.retry(migration_id, principal.name)


@router.post("/migrations/{migration_id}/cancel", response_model=Migration)
async def cancel(
    migration_id: str,
    request: Request,
    body: CancelRequest | None = None,
    principal: Principal = Depends(require_role(Role.operator)),
) -> Migration:
    reason = body.reason if body else None
    return await services(request).orchestrator.cancel(migration_id, principal.name, reason)


@router.post("/migrations/{migration_id}/finalize", response_model=Migration)
async def finalize(
    migration_id: str,
    body: FinalizeRequest,
    request: Request,
    principal: Principal = Depends(require_role(Role.approver)),
) -> Migration:
    return await services(request).orchestrator.finalize(
        migration_id, principal.name, delete_source=body.delete_source, confirm=body.confirm
    )


@router.put("/migrations/{migration_id}/strategy", response_model=Migration)
async def set_strategy(
    migration_id: str,
    body: StrategyRequest,
    request: Request,
    principal: Principal = Depends(require_role(Role.operator)),
) -> Migration:
    return await services(request).orchestrator.set_strategy(
        migration_id, body.strategy, principal.name
    )
