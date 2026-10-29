"""The followers' relay (SDD §12, §13.1, §25.6; plan task HA4).

A server replica that does not lead answers ``/api/v1/health``, ``/ready`` and ``/metrics``, the
API documents and the dashboard's files itself, and relays every other request whose path is
``/api/v1`` or lies under it - the event stream too - to the address the lease row names, as it
reads the row at the request, over plain HTTP: the method, the path as sent and its query, the
body, and of the caller's headers ``Authorization``, ``Content-Type`` and ``Accept-Encoding`` alone,
with ``X-Forwarded-For`` extended by the caller's address as this replica reads it and the marker
``X-Seamless-Relayed``. The leader authenticates, authorizes, audits and answers the request as a
direct one; the answer passes back as it came. The middleware sits inside the body limit, so a body
over it never leaves the follower, and outside the compressor, so the leader's answer is not
compressed again.

503 ``no_leader`` with ``Retry-After: 2`` while no replica leads, when the leader does not answer,
for a marked request this replica would relay - never relayed again -, and with authentication off,
whose checks of another site's page read headers a relay does not carry. A failed relay is logged
with its method and path, never a header, the query or the body.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from .. import lease as lease_module
from ..ai.redaction import redact_cut
from ..lease import LeaseRow

log = logging.getLogger(__name__)

#: the API a follower relays (SDD §12)
API_PREFIX = "/api/v1"
#: what a follower answers itself under it: its own health, readiness and metrics (SDD §12)
LOCAL_PATHS = frozenset({f"{API_PREFIX}/health", f"{API_PREFIX}/ready", f"{API_PREFIX}/metrics"})
#: the event stream: relayed without a bound (SDD §12)
STREAM_PATH = f"{API_PREFIX}/events/stream"
#: the relay's marker (SDD §12): a replica that does not lead never relays a marked request again
MARKER = "x-seamless-relayed"
#: the caller's headers a relay sends, and no other (SDD §12)
FORWARDED = ("authorization", "content-type", "accept-encoding")
#: the leader's headers a follower does not pass back: the hop-by-hop ones (RFC 9110 §7.6.1) and
#: those its own server writes (SDD §12)
DROPPED = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
        b"date",
        b"server",
    }
)
#: every relayed request but the event stream has its answer within this many seconds (SDD §12)
RELAY_TIMEOUT_S = 300.0
#: when a client may ask again after 503 no_leader: the lease's retry period (SDD §12)
RETRY_AFTER_S = 2
#: a relay connects within the retry period, and never within less than 2 s (SDD §12)
CONNECT_FLOOR_S = 2.0
#: a failed relay to one address is logged once in this many seconds: a failover fails each one
_LOG_EVERY_S = 10.0

Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def relayed(path: str) -> bool:
    """Whether a follower relays a request for ``path`` (SDD §12): one whose path is ``/api/v1`` or
    lies under it, but its own health, readiness and metrics."""
    return (path == API_PREFIX or path.startswith(f"{API_PREFIX}/")) and path not in LOCAL_PATHS


def _error(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )


def no_leader(message: str) -> JSONResponse:
    """503 no_leader, Retry-After: 2 (SDD §12)."""
    return _error(503, "no_leader", message, {"Retry-After": str(RETRY_AFTER_S)})


def _why(exc: BaseException) -> str:
    """A failure in words for one log line: its class and its text, redacted and cut."""
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {redact_cut(text, 200)}" if text else type(exc).__name__


class _Gone(Exception):
    """The caller went before its request's body came whole."""


async def _body(receive: Receive) -> bytes:
    """The request's body, read through the body limit's count (SDD §12): past the limit it raises
    the limit's 413 and nothing more is read."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _Gone
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


async def _until_gone(receive: Receive) -> None:
    """Until the caller goes: the body is read, so the next message is the disconnect."""
    while (await receive())["type"] != "http.disconnect":
        pass


def _passed_back(raw: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """The leader's answer's headers as the follower passes them back (SDD §12)."""
    return [(name.lower(), value) for name, value in raw if name.lower() not in DROPPED]


class RelayMiddleware:
    """A follower's relay of the API to the leader (SDD §12, §25.6), for a server's replica: one
    without a leadership - a store with no lease, a test's - leads alone and relays nothing."""

    def __init__(self, app: Callable[..., Awaitable[None]], services: Any) -> None:
        self.app = app
        self.services = services
        self._logged: dict[str, float] = {}

    async def __call__(self, scope: Any, receive: Receive, send: Send) -> None:
        leadership = self.services.leadership
        if (
            scope["type"] != "http"
            or leadership is None
            or not relayed(scope["path"])
            or leadership.leads()
        ):
            await self.app(scope, receive, send)
            return
        await self._relay(scope, receive, send, leadership)

    async def _relay(self, scope: Any, receive: Receive, send: Send, leadership: Any) -> None:
        headers = Headers(scope=scope)
        if MARKER in headers:
            refusal = "this replica does not lead, and never relays a relayed request again"
            await no_leader(refusal)(scope, receive, send)
            return
        if self.services.settings.auth_disabled:
            refusal = "this replica does not lead, and with authentication off it relays nothing"
            await no_leader(refusal)(scope, receive, send)
            return
        try:
            body = await _body(receive)
        except StarletteHTTPException as exc:  # the body limit (SDD §12): never relayed
            if exc.status_code != 413:
                raise
            await _error(413, "payload_too_large", str(exc.detail))(scope, receive, send)
            return
        except _Gone:
            return
        row = await self._row(leadership)
        client = self.services.relay_client
        if row is None or row.expired or row.holder == leadership.holder or client is None:
            await no_leader("no replica leads the orchestrator")(scope, receive, send)
            return
        request = self._request(scope, headers, row.address, body)
        await self._exchange(scope, receive, send, client, request, row.address)

    async def _row(self, leadership: Any) -> LeaseRow | None:
        try:
            return await asyncio.to_thread(leadership.row)
        except Exception as exc:  # noqa: BLE001 - no leader known: 503
            self._log_once("lease", f"could not read the lease row to relay a request: {_why(exc)}")
            return None

    def _request(self, scope: Any, headers: Headers, address: str, body: bytes) -> httpx.Request:
        """The request as the follower sends it to the leader at ``address`` (SDD §12)."""
        sent = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name in FORWARDED
            for value in headers.getlist(name)
        ]
        client = scope.get("client")
        forwarded = [value for value in headers.getlist("x-forwarded-for") if value.strip()]
        forwarded.append(client[0] if client else "unknown")
        sent.append((b"x-forwarded-for", ", ".join(forwarded).encode("latin-1")))
        sent.append((MARKER.encode("latin-1"), b"1"))
        path = scope.get("raw_path") or quote(scope["path"]).encode("latin-1")
        query = scope.get("query_string") or b""
        target = path + b"?" + query if query else path
        url = httpx.URL(f"http://{address}").copy_with(raw_path=target)
        # the connection within the retry period; the reads unbounded here - a stream has no
        # bound, and any other request is bounded whole by RELAY_TIMEOUT_S (``_exchange``)
        connect = max(CONNECT_FLOOR_S, lease_module.RETRY_S)
        timeout = httpx.Timeout(None, connect=connect, pool=connect)
        extensions = {"timeout": timeout.as_dict()}
        return httpx.Request(
            scope["method"], url, headers=sent, content=body, extensions=extensions
        )

    async def _exchange(
        self,
        scope: Any,
        receive: Receive,
        send: Send,
        client: httpx.AsyncClient,
        request: httpx.Request,
        address: str,
    ) -> None:
        """Send ``request`` and pass the leader's answer back as it flows, until it ended or the
        caller went (SDD §12): a stream without a bound, any other request within
        RELAY_TIMEOUT_S; 503 no_leader while no answer came, and an answer cut off midway ended
        where it was cut."""
        started = False

        async def forward() -> None:
            nonlocal started
            upstream = await client.send(request, stream=True)
            try:
                await send(
                    {
                        "type": "http.response.start",
                        "status": upstream.status_code,
                        "headers": _passed_back(upstream.headers.raw),
                    }
                )
                started = True
                async for chunk in upstream.aiter_raw():
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            finally:
                await upstream.aclose()
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        async def bounded() -> None:
            if scope["path"] == STREAM_PATH:
                await forward()
                return
            async with asyncio.timeout(RELAY_TIMEOUT_S):
                await forward()

        relay = asyncio.create_task(bounded(), name=f"relay:{scope['method']} {scope['path']}")
        gone = asyncio.create_task(_until_gone(receive), name="relay:caller")
        try:
            await asyncio.wait({relay, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            relay.cancel()
            gone.cancel()
            await asyncio.gather(relay, gone, return_exceptions=True)
        if relay.cancelled():
            return  # the caller went: nothing to answer
        failure = relay.exception()
        if failure is None:
            return
        if not isinstance(failure, httpx.HTTPError | TimeoutError | OSError):
            raise failure
        self._log_once(
            address,
            f"could not relay {scope['method']} {redact_cut(scope['path'], 200)} to the leader at "
            f"{address}: {_why(failure)}",
        )
        if not started:
            await no_leader("the leader does not answer")(scope, receive, send)
            return
        with contextlib.suppress(Exception):  # an answer cut off midway ends where it was cut
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    def _log_once(self, key: str, message: str) -> None:
        now = time.monotonic()
        last = self._logged.get(key)
        if last is None or now - last >= _LOG_EVERY_S:
            self._logged[key] = now
            log.warning("%s", message)


__all__ = [
    "LOCAL_PATHS",
    "MARKER",
    "RELAY_TIMEOUT_S",
    "RETRY_AFTER_S",
    "RelayMiddleware",
    "no_leader",
    "relayed",
]
