"""FastAPI application factory (SDD §12): REST + SSE under ``/api/v1`` and the dashboard SPA."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import Response
from starlette.routing import Match

from .. import __version__
from ..ai.advisor import Advisor
from ..ai.jev import JevClient
from ..ai.knowledge import KnowledgeService
from ..ai.memory import MemoryClient
from ..ai.redaction import redact
from ..config import Settings
from ..domain.fsm import InvalidTransition
from ..events import EventBus
from ..executors.base import ProcessGroups
from ..executors.router import ExecutorRouter
from ..leader import Leadership
from ..lease import new_holder, replica_address
from ..notify.notifier import Notifier, senders_for
from ..orchestrator import BadRequest, NotAllowed, Orchestrator, Unprocessable
from ..providers.base import ProviderError
from ..providers.registry import ProviderRegistry
from ..security.auth import TokenStore
from ..store import ConflictError, NotFound, Store, UnstorableValue
from . import (
    routes_advisor,
    routes_events,
    routes_hooks,
    routes_migrations,
    routes_misc,
    routes_notifications,
    routes_plans,
    routes_providers,
    routes_users,
)
from .deps import ApiError, Services, validation_message

log = logging.getLogger(__name__)
API_PREFIX = "/api/v1"
Hook = Callable[[Services], Awaitable[None]]

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    502: "provider_error",
}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(422, "validation_error", validation_message(exc.errors()))

    @app.exception_handler(ValidationError)
    async def model_validation(_: Request, exc: ValidationError) -> JSONResponse:
        return _error(422, "validation_error", validation_message(exc.errors()))

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @app.exception_handler(ConflictError)
    async def conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return _error(409, "conflict", str(exc))

    @app.exception_handler(UnstorableValue)
    async def unstorable_value(_: Request, exc: UnstorableValue) -> JSONResponse:
        # a value the store keeps no copy of - a source cloud's name holding a NUL - names its
        # field, never the value (SDD §11, §12)
        return _error(422, "validation_error", str(exc))

    @app.exception_handler(InvalidTransition)
    async def invalid_transition(_: Request, exc: InvalidTransition) -> JSONResponse:
        return _error(409, "conflict", str(exc))

    @app.exception_handler(NotAllowed)
    async def not_allowed(_: Request, exc: NotAllowed) -> JSONResponse:
        return _error(409, "conflict", str(exc))

    @app.exception_handler(BadRequest)
    async def bad_request(_: Request, exc: BadRequest) -> JSONResponse:
        return _error(400, "bad_request", str(exc))

    @app.exception_handler(Unprocessable)
    async def unprocessable(_: Request, exc: Unprocessable) -> JSONResponse:
        return _error(422, "validation_error", str(exc))

    @app.exception_handler(ProviderError)
    async def provider_error(_: Request, exc: ProviderError) -> JSONResponse:
        # SDK messages may carry endpoint URLs or credentials: same redaction as the orchestrator
        return _error(502, "provider_error", redact(str(exc))[:500])

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        # Starlette's ServerErrorMiddleware answers this one outside the headers middleware:
        # keep the error envelope and the security headers universal (SDD §12, Security.md R-05)
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        response = _error(500, "internal_error", "internal error")
        _apply_security_headers(response.headers, request.url.path)
        return response


#: Security headers (Security.md R-05). The dashboard needs inline styles (Recharts); it bundles
#: its fonts (SDD §16), so nothing loads from another host. Swagger UI loads from jsdelivr.
_CSP_APP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'"
)
#: Swagger UI boots from an inline script: it gets a per-response nonce (no 'unsafe-inline').
_CSP_DOCS = (
    "default-src 'self'; script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; style-src "
    "'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: "
    "https://fastapi.tiangolo.com; connect-src 'self'; frame-ancestors 'none'"
)


def _apply_security_headers(headers: Any, path: str) -> None:
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    headers.setdefault("Content-Security-Policy", _CSP_APP)


#: SDD §12: the largest request body. A 5,000-VM plan with an override per VM is about 0.5 MB.
MAX_BODY_BYTES = 1024 * 1024
#: SDD §12: responses of 1 KiB and more are gzip-compressed when the client accepts gzip.
GZIP_MIN_BYTES = 1024
GZIP_LEVEL = 6


class GZipExceptEventStream:
    """Gzip responses (SDD §12), never the event stream: a compressor holds small writes back until
    its buffer fills, which would delay live events (older Starlette versions compress
    ``text/event-stream`` too, so the stream is routed around the compressor)."""

    def __init__(self, app, stream_path: str = "/api/v1/events/stream"):
        self.app = app
        self.stream_path = stream_path
        self.gzip = GZipMiddleware(app, minimum_size=GZIP_MIN_BYTES, compresslevel=GZIP_LEVEL)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == self.stream_path:
            await self.app(scope, receive, send)
            return
        await self.gzip(scope, receive, send)


class BodyLimitMiddleware:
    """Refuse a request body over ``max_bytes`` with 413 (SDD §12, Security.md R-17).

    FastAPI parses a route's body before its auth dependency runs, so without a limit an
    unauthenticated client could make the server buffer any amount of data. A larger
    ``Content-Length`` is answered before anything reads the body; a body without one (chunked)
    is counted as it arrives, and the message that passes the limit never reaches the app.
    """

    def __init__(self, app: Callable[..., Awaitable[None]], max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or ()).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            response = _error(413, "payload_too_large", self._message())
            await response(scope, receive, send)
            return
        received = 0

        async def counted() -> Any:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI re-raises an HTTPException met while reading the body: the error
                    # handlers answer it as 413 payload_too_large
                    raise StarletteHTTPException(413, self._message())
            return message

        await self.app(scope, counted, send)

    def _message(self) -> str:
        return f"request body larger than {self.max_bytes} bytes"


class EnvelopeCORSMiddleware(CORSMiddleware):
    """Starlette's CORS, whose refused preflight is the error envelope (SDD §12, audit AP-09): the
    CORS layer answers a preflight before any route, so no error handler sees it."""

    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        if response.status_code < 400:
            return response
        refused = _error(response.status_code, "bad_request", bytes(response.body).decode())
        for name, value in response.headers.items():  # Vary and the allowed methods stay
            if name not in ("content-type", "content-length"):
                refused.headers[name] = value
        return refused


def _install_security_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        _apply_security_headers(response.headers, request.url.path)
        return response


def _install_api_docs(app: FastAPI, settings: Settings) -> None:
    """``/api/openapi.json`` and ``/api/docs``: public in demo mode, viewer role otherwise."""
    from fastapi.openapi.docs import get_swagger_ui_html

    from ..domain.enums import Role
    from .deps import require_role

    async def docs_access(request: Request) -> None:
        if not settings.demo:
            await require_role(Role.viewer)(request)

    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi_json(request: Request) -> Any:
        await docs_access(request)
        return JSONResponse(app.openapi())

    @app.get("/api/docs", include_in_schema=False)
    async def swagger_ui(request: Request) -> Any:
        await docs_access(request)
        page = get_swagger_ui_html(openapi_url="/api/openapi.json", title="Seamless Migrate API")
        nonce = secrets.token_urlsafe(16)
        html = page.body.decode("utf-8").replace("<script>", f'<script nonce="{nonce}">')
        response = HTMLResponse(html, status_code=page.status_code)
        response.headers["Content-Security-Policy"] = _CSP_DOCS.format(nonce=nonce)
        return response


#: FastAPI's default documentation routes, disabled (SDD §12, Security.md R-04): the SPA fallback
#: answers them with 404 too, so a stale link or a scanner never reads the dashboard as the API docs
_DISABLED_DOCS = ("docs", "redoc", "openapi.json")


def _api_path_takes_another_method(request: Request) -> bool:
    """Whether a route other than this fallback matches the request's path for another method
    (``Match.PARTIAL``); the API's routes sit in included routers, which answer it too."""
    fallback = request.scope.get("route")
    return any(
        route is not fallback and route.matches(request.scope)[0] == Match.PARTIAL
        for route in request.app.router.routes
    )


def _dashboard_file(root: Path, full_path: str) -> str | None:
    """The dashboard's file at ``full_path``, contained first (SDD §12): a path whose ".." leaves
    the directory is never looked at, and a symlink inside it counts only when what it names
    stays inside; None for any other path - the index answers it - as for a NUL byte, which
    names no file (review RV33-05)."""
    inside = f"{root}{os.sep}"
    candidate = os.path.normpath(os.path.join(root, full_path))
    if not (full_path and "\x00" not in full_path and candidate.startswith(inside)):
        return None
    real = os.path.realpath(candidate)
    return real if real.startswith(inside) and os.path.isfile(real) else None


def _install_spa(app: FastAPI, dist: Path) -> None:
    root = dist.resolve()
    index = root / "index.html"
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str, request: Request) -> Any:
        first = full_path.split("/", 1)[0]
        if first == "api" and _api_path_takes_another_method(request):
            # the GET fallback must not turn a wrong method on an API route into a 404 (SDD §12)
            return _error(405, "method_not_allowed", "Method Not Allowed")
        if first == "api" or first in _DISABLED_DOCS:
            return _error(404, "not_found", "Not Found")
        return FileResponse(_dashboard_file(root, full_path) or index)


def build_services(
    settings: Settings,
    store: Store | None = None,
    orchestrator: Orchestrator | None = None,
    sse_heartbeat_s: float = 15.0,
) -> Services:
    """Wire store, bus, providers, executors, AI integration and the orchestrator. The store built
    here, when none is given, is the server's own and fenced (SDD §11): it writes under the lease
    the lifespan takes; the CLI passes its unfenced one."""
    if orchestrator is not None:
        store = orchestrator.store
        bus = orchestrator.bus
        jev = getattr(orchestrator.advisor, "jev", None)
        memory = getattr(orchestrator.knowledge, "memory", None)
    else:
        store = store or Store(settings.db_url, fenced=True)
        bus = EventBus(store)
        registry = ProviderRegistry(settings)
        jev = JevClient(settings) if settings.jev_mode != "off" else None
        memory = MemoryClient.from_settings(settings)
        # one book of the playbooks' process groups for the executors and the hook runner: a
        # deposition kills them (SDD §7.1)
        groups = ProcessGroups()
        orchestrator = Orchestrator(
            store,
            bus,
            registry,
            ExecutorRouter(settings, providers=registry, groups=groups),
            Advisor(jev, settings),
            KnowledgeService(memory, store, bus),
            settings,
            process_groups=groups,
        )
    store.create_schema()
    if settings.tokens_file is not None:
        tokens = TokenStore.from_file(settings.tokens_file)
    else:
        tokens = TokenStore([])
        if not settings.auth_disabled:
            log.warning("SEAMLESS_TOKENS_FILE is not set: only database users can authenticate")
    return Services(
        settings=settings,
        store=store,
        bus=bus,
        orchestrator=orchestrator,
        tokens=tokens,
        jev=jev,
        memory=memory,
        sse_heartbeat_s=sse_heartbeat_s,
    )


async def _leadership(svc: Services, holder: str, hooks: list[Hook]) -> Leadership:
    """The replica's leadership of its orchestrator (SDD §8, §15): the lease's holder and address,
    and the takeover's start hooks - the demo seed. Its address is resolved off the event loop."""
    address = await asyncio.to_thread(replica_address, svc.settings.port)
    return Leadership(
        svc.store,
        svc.orchestrator,
        holder=holder,
        address=address,
        lease_s=svc.settings.leader_lease_s,
        start_hooks=[functools.partial(hook, svc) for hook in hooks],
        notifier=svc.notifier,
    )


def create_app(
    settings: Settings,
    store: Store | None = None,
    orchestrator: Orchestrator | None = None,
    *,
    sse_heartbeat_s: float = 15.0,
    on_startup: list[Hook] | None = None,
) -> FastAPI:
    """Build the API. The lifespan starts the orchestrator (after ``on_startup`` hooks). With a
    fenced store - the server's own - the replica's leadership does (SDD §8, §15): its elector's
    first try comes before the server serves, the hooks and the orchestrator run at each takeover
    and stop at each deposition, and the stop releases the lease."""
    svc = build_services(settings, store, orchestrator, sse_heartbeat_s)
    # SDD §8.7: the notifier runs beside the orchestrator, in the server's process alone
    svc.notifier = Notifier(svc.store, svc.bus, settings, senders_for(settings))
    hooks = list(on_startup or [])
    holder = new_holder()  # new at each process start (SDD §11)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if not svc.store.fenced:  # a store given by a test or the CLI: no lease
            for hook in hooks:
                await hook(svc)
            await svc.orchestrator.start()
            await svc.notifier.start()
            try:
                yield
            finally:
                await svc.notifier.stop()
                await svc.orchestrator.stop()
            return
        svc.leadership = await _leadership(svc, holder, hooks)
        await svc.leadership.start()
        try:
            yield
        finally:
            await svc.leadership.stop()

    app = FastAPI(
        title="Seamless Migrate",
        version=__version__,
        lifespan=lifespan,
        # the OpenAPI document and Swagger UI are served by _install_api_docs: public in demo
        # mode, viewer-authenticated otherwise (Security.md R-04)
        docs_url=None,
        openapi_url=None,
        redoc_url=None,
    )
    app.state.services = svc
    _install_error_handlers(app)
    # innermost: the compressed response still gets the security headers (SDD §12)
    app.add_middleware(GZipExceptEventStream, stream_path=f"{API_PREFIX}/events/stream")
    # added before the security headers middleware, which therefore wraps it: a 413 carries the
    # R-05 headers too
    app.add_middleware(BodyLimitMiddleware)
    if settings.cors_origins:
        # inside the security headers too: the CORS layer answers preflights itself (audit AP-09)
        app.add_middleware(
            EnvelopeCORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )
    _install_security_headers(app)
    _install_api_docs(app, settings)
    for module in (
        routes_misc,
        routes_advisor,
        routes_providers,
        routes_hooks,
        routes_notifications,
        routes_plans,
        routes_migrations,
        routes_events,
        routes_users,
    ):
        app.include_router(module.router, prefix=API_PREFIX)
    dist = settings.dashboard_dir
    if dist is not None and (Path(dist) / "index.html").is_file():
        _install_spa(app, Path(dist))
    return app
