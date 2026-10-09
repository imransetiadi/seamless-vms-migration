"""FastAPI application factory (SDD §12): REST + SSE under ``/api/v1`` and the dashboard SPA."""

from __future__ import annotations

import logging
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
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..ai.advisor import Advisor
from ..ai.jev import JevClient
from ..ai.knowledge import KnowledgeService
from ..ai.memory import MemoryClient, redact
from ..config import Settings
from ..domain.fsm import InvalidTransition
from ..events import EventBus
from ..executors.router import ExecutorRouter
from ..orchestrator import BadRequest, NotAllowed, Orchestrator
from ..providers.base import ProviderError
from ..providers.registry import ProviderRegistry
from ..security.auth import TokenStore
from ..store import ConflictError, NotFound, Store
from . import routes_events, routes_migrations, routes_misc, routes_plans, routes_providers
from .deps import ApiError, Services

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


def _validation_message(errors: list[Any]) -> str:
    parts = []
    for err in errors[:5]:
        location = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        parts.append(f"{location}: {err.get('msg')}" if location else str(err.get("msg")))
    return "; ".join(parts) or "invalid request"


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
        return _error(422, "validation_error", _validation_message(list(exc.errors())))

    @app.exception_handler(ValidationError)
    async def model_validation(_: Request, exc: ValidationError) -> JSONResponse:
        return _error(422, "validation_error", _validation_message(list(exc.errors())))

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @app.exception_handler(ConflictError)
    async def conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return _error(409, "conflict", str(exc))

    @app.exception_handler(InvalidTransition)
    async def invalid_transition(_: Request, exc: InvalidTransition) -> JSONResponse:
        return _error(409, "conflict", str(exc))

    @app.exception_handler(NotAllowed)
    async def not_allowed(_: Request, exc: NotAllowed) -> JSONResponse:
        return _error(409, "conflict", str(exc))

    @app.exception_handler(BadRequest)
    async def bad_request(_: Request, exc: BadRequest) -> JSONResponse:
        return _error(400, "bad_request", str(exc))

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


#: Security headers (Security.md R-05). The dashboard needs inline styles (Recharts) and the
#: Fira fonts from Google Fonts (dashboard/index.html); Swagger UI loads from jsdelivr.
_CSP_APP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' "
    "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' "
    "data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
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


def _install_spa(app: FastAPI, dist: Path) -> None:
    root = dist.resolve()
    index = root / "index.html"
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Any:
        first = full_path.split("/", 1)[0]
        if first == "api" or first in _DISABLED_DOCS:
            return _error(404, "not_found", "Not Found")
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index)


def build_services(
    settings: Settings,
    store: Store | None = None,
    orchestrator: Orchestrator | None = None,
    sse_heartbeat_s: float = 15.0,
) -> Services:
    """Wire store, bus, providers, executors, AI integration and the orchestrator."""
    if orchestrator is not None:
        store = orchestrator.store
        bus = orchestrator.bus
        jev = getattr(orchestrator.advisor, "jev", None)
        memory = getattr(orchestrator.knowledge, "memory", None)
    else:
        store = store or Store(settings.db_url)
        bus = EventBus(store)
        registry = ProviderRegistry(settings)
        jev = JevClient(settings) if settings.jev_mode != "off" else None
        memory = MemoryClient.from_settings(settings)
        orchestrator = Orchestrator(
            store,
            bus,
            registry,
            ExecutorRouter(settings, providers=registry),
            Advisor(jev, settings),
            KnowledgeService(memory, store, bus),
            settings,
        )
    store.create_schema()
    if settings.tokens_file is not None:
        tokens = TokenStore.from_file(settings.tokens_file)
    else:
        tokens = TokenStore([])
        if not settings.auth_disabled:
            log.warning("SEAMLESS_TOKENS_FILE is not set: every API call will be refused")
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


def create_app(
    settings: Settings,
    store: Store | None = None,
    orchestrator: Orchestrator | None = None,
    *,
    sse_heartbeat_s: float = 15.0,
    on_startup: list[Hook] | None = None,
) -> FastAPI:
    """Build the API. The lifespan starts the orchestrator (after ``on_startup`` hooks)."""
    svc = build_services(settings, store, orchestrator, sse_heartbeat_s)
    hooks = list(on_startup or [])

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        for hook in hooks:
            await hook(svc)
        await svc.orchestrator.start()
        try:
            yield
        finally:
            await svc.orchestrator.stop()

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
    # added before the security headers middleware, which therefore wraps it: a 413 carries the
    # R-05 headers too
    app.add_middleware(BodyLimitMiddleware)
    _install_security_headers(app)
    _install_api_docs(app, settings)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )
    for module in (routes_misc, routes_providers, routes_plans, routes_migrations, routes_events):
        app.include_router(module.router, prefix=API_PREFIX)
    dist = settings.dashboard_dir
    if dist is not None and (Path(dist) / "index.html").is_file():
        _install_spa(app, Path(dist))
    return app
