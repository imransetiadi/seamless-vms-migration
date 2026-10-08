"""``/events`` and the SSE stream (SDD §12).

Each SSE frame is ``id: <seq or 0>\\nevent: <kind>\\ndata: <Event JSON>\\n\\n``; a
``: heartbeat`` comment is sent after ``sse_heartbeat_s`` (15 s) without events. Clients resume
with ``?since=<last seq>``; without ``since`` only live events are streamed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from ..domain.enums import Role
from ..domain.models import Event
from ..events import Subscription
from ..security.auth import Principal
from .deps import require_role, services

router = APIRouter(tags=["events"])
HEARTBEAT = ": heartbeat\n\n"


def format_sse(event: Event) -> str:
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {event.model_dump_json()}\n\n"


async def sse_frames(
    subscription: Subscription,
    heartbeat_s: float,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    try:
        while True:
            try:
                event = await asyncio.wait_for(subscription.__anext__(), timeout=heartbeat_s)
            except TimeoutError:
                if is_disconnected is not None and await is_disconnected():
                    return
                yield HEARTBEAT
                continue
            except StopAsyncIteration:
                return
            yield format_sse(event)
    finally:
        await subscription.aclose()


@router.get("/events", response_model=list[Event])
async def list_events(
    request: Request,
    since: int = Query(default=0, ge=0),
    plan_id: str | None = None,
    migration_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=1000),
    _: Principal = Depends(require_role(Role.viewer)),
) -> list[Event]:
    return await services(request).db.events(
        since_seq=since, plan_id=plan_id, migration_id=migration_id, limit=limit
    )


@router.get("/events/stream")
async def stream_events(
    request: Request,
    since: int | None = Query(default=None, ge=0),
    _: Principal = Depends(require_role(Role.viewer)),
) -> StreamingResponse:
    svc = services(request)
    start = since if since is not None else await svc.db.max_seq()
    subscription = svc.bus.subscribe(since=start)
    return StreamingResponse(
        sse_frames(subscription, svc.sse_heartbeat_s, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
