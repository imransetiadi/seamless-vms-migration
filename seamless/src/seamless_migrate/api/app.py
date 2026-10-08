"""FastAPI application factory (SDD §12): REST + SSE under ``/api/v1`` and the dashboard SPA."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..ai.advisor import Advisor
from ..ai.jev import JevClient
from ..ai.knowledge import KnowledgeService
from ..ai.memory import MemoryClient
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
        return _error(502, "provider_error", str(exc))


def _install_spa(app: FastAPI, dist: Path) -> None:
    root = dist.resolve()
    index = root / "index.html"
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Any:
        if full_path == "api" or full_path.startswith("api/"):
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
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.services = svc
    _install_error_handlers(app)
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
