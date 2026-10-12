"""Health, identity, stats, advisor and metrics routes."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import PlainTextResponse

from .. import __version__
from ..ai.payloads import console_allowed, data_sent
from ..ai.recommendations import LEGACY_PREFIX, legacy_by_id, listing
from ..domain.enums import DecisionType, Role
from ..domain.models import AdvisorRecommendation, AdvisorSnapshot, Migration, Plan, VMRef
from ..metrics import advisor_calls, render_metrics
from ..security.auth import Principal
from ..stats import Stats, compute_stats
from ..store import NotFound
from .deps import ApiError, metrics_access, require_role, services
from .schemas import (
    AdvisorNoteEntry,
    AdvisorNoteList,
    AdvisorStatus,
    DataSentList,
    DemoPowerRequest,
    Health,
    Hit,
    JevStatus,
    Me,
    MemoryStatus,
    RecommendationPage,
    SimilarIncidentsRequest,
    SimilarIncidentsResponse,
)

router = APIRouter(tags=["misc"])


@router.post("/demo/vms/{source_id}/power", response_model=VMRef)
async def demo_power(
    source_id: str,
    body: DemoPowerRequest,
    request: Request,
    _: Principal = Depends(require_role(Role.operator)),
) -> VMRef:
    """Power a fake source VM on or off, so the source watch shows (demo mode only, SDD §8.1)."""
    svc = services(request)
    if not svc.settings.demo:
        raise ApiError(404, "not_found", "no such route outside demo mode")
    return await svc.orchestrator.demo_power(source_id, on=body.state == "on")


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
        demo_speed=svc.settings.demo_speed if svc.settings.demo else None,
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
    # where the model runs and whether the console excerpt may go there (SDD §13.4, ADV-06)
    where = {
        "deployment": svc.settings.jev_deployment,
        "endpoint_host": svc.settings.jev_endpoint_host,
        "console_sent": jev is not None and jev.enabled and console_allowed(svc.settings),
    }
    jev_status = (
        JevStatus(**jev.status(), **where)
        if jev is not None
        else JevStatus(mode=svc.settings.jev_mode, available=False, last_error=None, **where)
    )
    memory = svc.memory
    if memory is None:
        memory_status = MemoryStatus(enabled=False, available=False, last_error=None)
    else:
        if memory.available is None:
            await memory.health()
        memory_status = MemoryStatus(**memory.status())
    return AdvisorStatus(jev=jev_status, memory=memory_status)


@router.get("/advisor/data-sent", response_model=DataSentList)
async def advisor_data_sent(
    request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> DataSentList:
    """What each decision type sends to Jev, from the serializers' field specs (SDD §13.4)."""
    return DataSentList.model_validate({"items": data_sent(services(request).settings)})


@router.get("/advisor/recommendations", response_model=RecommendationPage)
async def list_recommendations(
    request: Request,
    migration_id: str | None = None,
    plan_id: str | None = None,
    decision_type: DecisionType | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: Principal = Depends(require_role(Role.viewer)),
) -> RecommendationPage:
    """Stored recommendations and legacy strategy notes, newest first (SDD §12, §14.4)."""
    svc = services(request)
    items = listing(
        await svc.all_documents("recommendation", AdvisorRecommendation),
        await svc.all_documents("migration", Migration),
        migration_id=migration_id,
        plan_id=plan_id,
        decision_type=decision_type,
    )
    return RecommendationPage(
        items=items[offset : offset + limit], total=len(items), offset=offset, limit=limit
    )


@router.get("/advisor/recommendations/{rec_id}", response_model=AdvisorRecommendation)
async def get_recommendation(
    rec_id: str, request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> AdvisorRecommendation:
    svc = services(request)
    if rec_id.startswith(LEGACY_PREFIX):
        migration_id = rec_id.removeprefix(LEGACY_PREFIX).rpartition("-")[0]
        try:
            migration = await svc.db.get("migration", migration_id, Migration)
        except NotFound:
            raise NotFound("recommendation", rec_id) from None
        found = legacy_by_id(rec_id, migration)
        if found is None:
            raise NotFound("recommendation", rec_id)
        return found
    return await svc.db.get("recommendation", rec_id, AdvisorRecommendation)


@router.get("/advisor/notes", response_model=AdvisorNoteList)
async def list_advisor_notes(
    request: Request,
    limit: int = Query(20, ge=1, le=200),
    _: Principal = Depends(require_role(Role.viewer)),
) -> AdvisorNoteList:
    """Every migration's advisor notes but strategy notes - those are listed as legacy
    recommendations (SDD §14.4) -, newest first: the Advisor page reads no migration list (SDD §12,
    §16). Notes of the same time keep the migrations' creation order and each one's note order."""
    entries = [
        AdvisorNoteEntry(
            **note.model_dump(), migration_id=m.id, plan_id=m.plan_id, vm_name=m.vm.name
        )
        for m in await services(request).all_documents("migration", Migration)
        for note in m.advisor_notes
        if note.kind != "strategy"
    ]
    # a stable sort: equal times keep the order above
    entries.sort(key=lambda entry: entry.created_at, reverse=True)
    return AdvisorNoteList(items=entries[:limit])


@router.get("/advisor/snapshots/{sha256}", response_model=AdvisorSnapshot)
async def get_snapshot(
    request: Request,
    sha256: str = Path(pattern=r"^[0-9a-f]{64}$"),
    _: Principal = Depends(require_role(Role.viewer)),
) -> AdvisorSnapshot:
    """A redacted advisor input; its hash can be recomputed from the payload (SDD §14.5)."""
    return await services(request).db.get("advisor_snapshot", sha256, AdvisorSnapshot)


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
