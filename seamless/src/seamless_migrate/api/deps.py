"""Shared API plumbing: the service container, errors and the role dependency."""

from __future__ import annotations

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

    @property
    def db(self) -> AsyncStore:
        return self.orchestrator.db

    @property
    def providers(self) -> Any:
        return self.orchestrator.providers

    def allow_audit(self, client: str) -> bool:
        now = time.monotonic()
        window = self._denied[client]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= AUDIT_DENIED_PER_MINUTE:
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
        principal = authenticate(request)
        if principal is None:
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
