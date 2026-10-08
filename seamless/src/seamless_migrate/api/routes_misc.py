"""Health, identity, stats, advisor and metrics routes."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import PlainTextResponse

from .. import __version__
from ..domain.enums import Role
from ..domain.models import Migration, Plan
from ..metrics import advisor_calls, render_metrics
from ..security.auth import Principal
from ..stats import Stats, compute_stats
from .deps import metrics_access, require_role, services
from .schemas import (
    AdvisorStatus,
    Health,
    Hit,
    JevStatus,
    Me,
    MemoryStatus,
    SimilarIncidentsRequest,
    SimilarIncidentsResponse,
)

router = APIRouter(tags=["misc"])


@router.get("/health", response_model=Health)
async def health(request: Request) -> Health:
    """Liveness: always 200 while the process answers; the body says what is degraded."""
    return await _health(request)


@router.get("/ready", response_model=Health)
async def ready(request: Request, response: Response) -> Health:
    """Readiness: the same body as ``/health`` with HTTP 503 while degraded, so Kubernetes
    stops routing to a replica whose database or orchestrator loop is unavailable."""
    body = await _health(request)
    if body.status != "ok":
        response.status_code = 503
    return body


async def _health(request: Request) -> Health:
    svc = services(request)
    db_ok = await asyncio.to_thread(svc.store.ping)
    orchestrator = svc.orchestrator.health()
    return Health(
        status="ok" if db_ok and orchestrator["healthy"] else "degraded",
        version=__version__,
        demo=svc.settings.demo,
        db="ok" if db_ok else "error",
        orchestrator=orchestrator,
    )


@router.get("/me", response_model=Me)
async def me(principal: Principal = Depends(require_role(Role.viewer))) -> Me:
    return Me(name=principal.name, role=principal.role)


@router.get("/stats", response_model=Stats)
async def stats(
    request: Request,
    plan_id: str | None = Query(default=None),
    _: Principal = Depends(require_role(Role.viewer)),
) -> Stats:
    svc = services(request)
    migrations = await svc.all_documents("migration", Migration)
    if plan_id:
        migrations = [m for m in migrations if m.plan_id == plan_id]
    plans = {p.id: p for p in await svc.all_documents("plan", Plan)}
    return compute_stats(migrations, plans, svc.orchestrator.now())


@router.get("/advisor/status", response_model=AdvisorStatus)
async def advisor_status(
    request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> AdvisorStatus:
    svc = services(request)
    jev = svc.jev
    jev_status = (
        JevStatus(**jev.status())
        if jev is not None
        else JevStatus(mode=svc.settings.jev_mode, available=False, last_error=None)
    )
    memory = svc.memory
    if memory is None:
        memory_status = MemoryStatus(enabled=False, available=False, last_error=None)
    else:
        if memory.available is None:
            await memory.health()
        memory_status = MemoryStatus(**memory.status())
    return AdvisorStatus(jev=jev_status, memory=memory_status)


@router.post("/advisor/similar-incidents", response_model=SimilarIncidentsResponse)
async def similar_incidents(
    body: SimilarIncidentsRequest,
    request: Request,
    _: Principal = Depends(require_role(Role.operator)),
) -> SimilarIncidentsResponse:
    memory = services(request).memory
    if memory is None:
        return SimilarIncidentsResponse(hits=[])
    hits = await memory.search(body.query, limit=body.limit)
    return SimilarIncidentsResponse(
        hits=[Hit(title=h.title, content=h.content, score=h.score) for h in hits]
    )


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request, _: Principal | None = Depends(metrics_access)) -> str:
    svc = services(request)
    migrations = await svc.all_documents("migration", Migration)
    text = render_metrics(
        migrations,
        svc.orchestrator.step_stats,
        advisor_calls(svc.jev),
        svc.orchestrator.tick_stats,
    )
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")
