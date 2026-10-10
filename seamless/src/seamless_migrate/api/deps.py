"""Shared API plumbing: the service container, errors and the role dependency."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from ..config import Settings
from ..domain.enums import Role
from ..events import EventBus, emit
from ..orchestrator import Orchestrator
from ..security.auth import ANONYMOUS_ADMIN, Principal, TokenStore
from ..store import AsyncStore, Store

log = logging.getLogger(__name__)

#: at most this many auth.denied events per client per minute (protects the events table)
#: Distinct client addresses tracked for audit throttling / lockout; the oldest are evicted.
MAX_TRACKED_CLIENTS = 10_000


def _bound(table: dict[str, Any], client: str) -> None:
    """Keep ``table`` under MAX_TRACKED_CLIENTS entries (insertion order = oldest first)."""
    while client not in table and len(table) >= MAX_TRACKED_CLIENTS:
        table.pop(next(iter(table)))


AUDIT_DENIED_PER_MINUTE = 30


@dataclass
class Services:
    settings: Settings
    store: Store
    bus: EventBus
    orchestrator: Orchestrator
    tokens: TokenStore
    jev: Any = None
    memory: Any = None
    sse_heartbeat_s: float = 15.0
    _denied: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    _auth_failures: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    #: full document lists reused by /stats and /metrics while their change stamp holds
    _doc_cache: dict[str, tuple[tuple[int, int], list[Any]]] = field(default_factory=dict)
    _doc_cache_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def all_documents(self, kind: str, model_cls: type[Any]) -> list[Any]:
        """Every document of ``kind``, reloaded only when the store's change stamp moved
        (one aggregate query per call instead of loading and validating every row)."""
        stamp = await self.db.change_stamp(kind)
        cached = self._doc_cache.get(kind)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        async with self._doc_cache_lock:
            cached = self._doc_cache.get(kind)
            if cached is not None and cached[0] == stamp:
                return cached[1]
            rows = await self.db.list(kind, model_cls)
            self._doc_cache[kind] = (stamp, rows)
            return rows

    @property
    def db(self) -> AsyncStore:
        return self.orchestrator.db

    @property
    def providers(self) -> Any:
        return self.orchestrator.providers

    def auth_locked(self, client: str) -> bool:
        """True while ``client`` exceeded ``auth_lockout_per_minute`` failed authentications
        in the last minute (Security.md API2). Only consulted after a *failed* authentication,
        so a shared address (ingress, NAT) never locks out callers with valid tokens."""
        limit = self.settings.auth_lockout_per_minute
        if limit <= 0:
            return False
        window = self._auth_failures.get(client)
        if not window:
            return False
        now = time.monotonic()
        while window and now - window[0] > 60:
            window.popleft()
        if not window:
            del self._auth_failures[client]
            return False
        return len(window) >= limit

    def record_auth_failure(self, client: str) -> None:
        if self.settings.auth_lockout_per_minute > 0:
            _bound(self._auth_failures, client)
            self._auth_failures[client].append(time.monotonic())

    def allow_audit(self, client: str) -> bool:
        now = time.monotonic()
        _bound(self._denied, client)
        window = self._denied[client]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= AUDIT_DENIED_PER_MINUTE:
            self._denied.pop(client) if not window else None
            return False
        window.append(now)
        return True


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def services(request: Request) -> Services:
    return request.app.state.services


async def _audit_denied(
    request: Request, reason: str, required: Role, principal: Principal | None = None
) -> None:
    svc = services(request)
    client = request.client.host if request.client else "unknown"
    if not svc.allow_audit(client):
        log.warning("auth.denied events throttled for %s", client)
        return
    await emit(
        svc.store,
        svc.bus,
        "auth.denied",
        f"{request.method} {request.url.path}: {reason}",
        actor=principal.name if principal else "unauthenticated",
        data={
            "path": request.url.path,
            "method": request.method,
            "reason": reason,
            "required_role": str(required),
            "client": client,
        },
    )


def authenticate(request: Request) -> Principal | None:
    svc = services(request)
    if svc.settings.auth_disabled:
        return ANONYMOUS_ADMIN
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return svc.tokens.authenticate(token.strip())


def require_role(role: Role) -> Callable[[Request], Awaitable[Principal]]:
    """FastAPI dependency: the caller's principal, which must hold at least ``role``."""

    async def dependency(request: Request) -> Principal:
        svc = services(request)
        principal = authenticate(request)
        if principal is None:
            client = request.client.host if request.client else "unknown"
            if svc.auth_locked(client):
                await _audit_denied(request, "too many failed authentication attempts", role)
                raise ApiError(
                    429,
                    "too_many_requests",
                    "too many failed authentication attempts from this address; retry in a minute",
                )
            svc.record_auth_failure(client)
            await _audit_denied(request, "missing or invalid bearer token", role)
            raise ApiError(401, "unauthorized", "missing or invalid bearer token")
        if not principal.role.at_least(role):
            await _audit_denied(request, f"role {principal.role} is below {role}", role, principal)
            raise ApiError(403, "forbidden", f"this action requires the {role} role")
        return principal

    dependency.__name__ = f"require_{role}"
    return dependency


async def metrics_access(request: Request) -> Principal | None:
    """``/metrics`` is public when ``SEAMLESS_METRICS_PUBLIC`` is set, else viewer+."""
    if services(request).settings.metrics_public:
        return None
    return await require_role(Role.viewer)(request)
