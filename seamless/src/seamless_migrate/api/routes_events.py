"""``/events`` and the SSE stream (SDD §12).

Each SSE frame is ``id: <seq or 0>\\nevent: <kind>\\ndata: <Event JSON>\\n\\n``; a
``: heartbeat`` comment is sent after ``sse_heartbeat_s`` (15 s) without events. Clients resume
with ``?since=<last seq>``; without ``since`` only live events are streamed. The stream checks its
principal again at each heartbeat, and at least once per heartbeat interval while events flow,
and ends once it is no longer accepted (a database user revoked, SDD §13.1).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from ..domain.enums import Role
from ..domain.models import Event
from ..events import AUDIT_KINDS, Subscription
from ..security.auth import Principal
from .deps import authenticate, require_role, services

#: Largest sequence/offset the database integer columns hold (a bigger value is a 422, not a 500).
MAX_SEQ = 2**63 - 1

router = APIRouter(tags=["events"])
HEARTBEAT = ": heartbeat\n\n"


def format_sse(event: Event) -> str:
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {event.model_dump_json()}\n\n"


async def sse_frames(
    subscription: Subscription,
    heartbeat_s: float,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    still_allowed: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """The stream's frames. ``still_allowed`` runs at each heartbeat, and before an event once a
    heartbeat interval has passed since it last ran: a principal no longer accepted - a database
    user whose row was deleted - ends the stream, whose reconnect gets 401 (SDD §12, §13.1)."""
    checked = time.monotonic()
    try:
        while True:
            try:
                event = await asyncio.wait_for(subscription.__anext__(), timeout=heartbeat_s)
            except TimeoutError:
                if is_disconnected is not None and await is_disconnected():
                    return
                if still_allowed is not None:
                    checked = time.monotonic()
                    if not await still_allowed():
                        return
                yield HEARTBEAT
                continue
            except StopAsyncIteration:
                return
            if still_allowed is not None and time.monotonic() - checked >= heartbeat_s:
                checked = time.monotonic()
                if not await still_allowed():
                    return
            yield format_sse(event)
    finally:
        await subscription.aclose()


@router.get("/events", response_model=list[Event])
async def list_events(
    request: Request,
    since: int = Query(default=0, ge=0, le=MAX_SEQ),
    before: int | None = Query(default=None, ge=1, le=MAX_SEQ),
    plan_id: str | None = None,
    migration_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=1000),
    tail: bool = False,
    audit: bool = False,
    _: Principal = Depends(require_role(Role.viewer)),
) -> list[Event]:
    # tail: the newest ``limit`` matching events instead of the first (SDD §12), still ascending;
    # before: only events below that seq, so tail + before is the page before the oldest shown;
    # audit: only the audit events (SDD §14.5)
    return await services(request).db.events(
        since_seq=since,
        before_seq=before,
        plan_id=plan_id,
        migration_id=migration_id,
        limit=limit,
        tail=tail,
        kinds=AUDIT_KINDS if audit else None,
    )


@router.get("/events/stream")
async def stream_events(
    request: Request,
    since: int | None = Query(default=None, ge=0, le=MAX_SEQ),
    _: Principal = Depends(require_role(Role.viewer)),
) -> StreamingResponse:
    svc = services(request)
    start = since if since is not None else await svc.db.max_seq()
    subscription = svc.bus.subscribe(since=start)

    async def still_allowed() -> bool:
        return await authenticate(request) is not None

    return StreamingResponse(
        sse_frames(subscription, svc.sse_heartbeat_s, request.is_disconnected, still_allowed),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
