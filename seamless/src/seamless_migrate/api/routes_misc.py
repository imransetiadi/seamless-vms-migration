"""Health, identity, stats, advisor and metrics routes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError

from .. import __version__
from ..ai.payloads import console_allowed, data_sent
from ..ai.recommendations import LEGACY_PREFIX, legacy_by_id, listing
from ..domain.enums import DecisionType, Role
from ..domain.models import AdvisorRecommendation, AdvisorSnapshot, Migration, Plan, VMRef
from ..lease import LeaseRow
from ..metrics import advisor_calls, render_metrics
from ..security.auth import Principal
from ..stats import Stats, compute_stats
from ..store import NotFound
from .deps import ApiError, metrics_access, require_role, services
from .schemas import (
    AdvisorNoteEntry,
    AdvisorNoteList,
    AdvisorStatus,
    CurrentUser,
    DataSentList,
    DemoPowerRequest,
    Health,
    Hit,
    JevStatus,
    LeaderHealth,
    MemoryStatus,
    OrchestratorHealth,
    RecommendationPage,
    SimilarIncidentsRequest,
    SimilarIncidentsResponse,
)

router = APIRouter(tags=["misc"])
log = logging.getLogger(__name__)

#: a follower's report of the leader's orchestrator before the leader wrote it after its takeover:
#: one before its start - and while no replica leads, or the row cannot be read: one that does not
#: run (SDD §12)
BEFORE_START = {"running": False, "last_tick_age_s": None, "ticks": 0, "healthy": True}
NONE_RUNS = {"running": False, "last_tick_age_s": None, "ticks": 0, "healthy": False}


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
    """The health of the replica that answers (SDD §12, §25.6): ``status`` and ``db`` judge it -
    the leader by its tick loop, a follower by its campaign -, ``orchestrator`` is the leader's and
    ``leader`` names the lease's holder. A server whose store holds no lease leads alone."""
    svc = services(request)
    db_ok = await asyncio.to_thread(svc.store.ping)
    leadership = svc.leadership
    if leadership is None:
        orchestrator = svc.orchestrator.health()
        leader = LeaderHealth(leading=True)
        alive = bool(orchestrator["healthy"])
    else:
        leading = leadership.leads()
        row = await _lease_row(leadership)
        leader = _leader(row, leading, leadership)
        if leading:
            orchestrator = svc.orchestrator.health()
            alive = bool(orchestrator["healthy"])
        else:
            orchestrator = _reported(row)
            alive = leadership.campaigning()
    return Health(
        status="ok" if db_ok and alive else "degraded",
        version=__version__,
        demo=svc.settings.demo,
        demo_speed=svc.settings.demo_speed if svc.settings.demo else None,
        auth_disabled=svc.settings.auth_disabled,
        db="ok" if db_ok else "error",
        orchestrator=orchestrator,
        leader=leader,
    )


async def _lease_row(leadership: Any) -> LeaseRow | None:
    """The lease row as the store reads it now, None when it cannot be read (SDD §12)."""
    try:
        return await asyncio.to_thread(leadership.row)
    except Exception:  # noqa: BLE001 - the health answers whatever the database says
        log.debug("the lease row could not be read", exc_info=True)
        return None


def _leader(row: LeaseRow | None, leading: bool, leadership: Any) -> LeaderHealth:
    """/health's leader (SDD §12): the valid lease's holder, epoch and acquisition; a leader that
    cannot read the row names itself and its epoch; nobody while no replica leads."""
    if row is not None and not row.expired:
        return LeaderHealth(
            leading=leading, holder=row.holder, epoch=row.epoch, since=row.acquired_at
        )
    if leading:
        return LeaderHealth(leading=True, holder=leadership.holder, epoch=leadership.epoch)
    return LeaderHealth(leading=False)


def _reported(row: LeaseRow | None) -> dict[str, Any]:
    """The leader's orchestrator as the lease row holds it, which a follower reports (SDD §12)."""
    if row is None or row.expired:
        return dict(NONE_RUNS)
    if row.health is None:
        return dict(BEFORE_START)
    try:
        return OrchestratorHealth.model_validate(row.health).model_dump()
    except ValidationError:  # written by no version of this one: as if none were written
        return dict(BEFORE_START)


@router.get("/me", response_model=CurrentUser)
async def me(
    request: Request, principal: Principal = Depends(require_role(Role.viewer))
) -> CurrentUser:
    return CurrentUser(
        name=principal.name,
        role=principal.role,
        host_key_checking=services(request).settings.ansible_host_key_checking,
    )


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
        svc.notifier.counts if svc.notifier is not None else None,
    )
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")
