"""Shared API plumbing: the service container, errors and the role dependency."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from ..config import Settings
from ..domain.enums import Role
from ..events import EventBus, emit
from ..orchestrator import Orchestrator
from ..security.auth import ANONYMOUS_ADMIN, DashboardUser, Principal, TokenStore, hash_token
from ..store import AsyncStore, Store, VersionedDocuments

log = logging.getLogger(__name__)

#: at most this many auth.denied events per client per minute (protects the events table)
#: Distinct client addresses tracked for audit throttling / lockout; the oldest are evicted.
MAX_TRACKED_CLIENTS = 10_000


def _bound(table: dict[str, Any], client: str) -> None:
    """Keep ``table`` under MAX_TRACKED_CLIENTS entries (insertion order = oldest first)."""
    while client not in table and len(table) >= MAX_TRACKED_CLIENTS:
        table.pop(next(iter(table)))


AUDIT_DENIED_PER_MINUTE = 30
#: The database users' digests are read again at the latest after this many seconds, whatever
#: the change stamp says: a deletion and a re-creation at the same version leave it unchanged.
USER_DIGESTS_MAX_AGE_S = 5.0


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
    #: per kind, the documents /stats, /metrics and /advisor/recommendations read (SDD §11)
    _doc_cache: dict[str, VersionedDocuments] = field(default_factory=dict)
    #: the database users' digests (SDD §13.1), the change stamp and the time they were read at
    _users: list[DashboardUser] = field(default_factory=list)
    _users_stamp: tuple[int, int] | None = None
    _users_read_at: float = 0.0
    _users_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def all_documents(self, kind: str, model_cls: type[Any]) -> list[Any]:
        """Every document of ``kind`` in creation order; only what changed is loaded again
        (``VersionedDocuments``) - a run writes a migration every second (SDD §11)."""
        cache = self._doc_cache.get(kind)
        if cache is None:
            cache = self._doc_cache[kind] = VersionedDocuments(self.db, kind, model_cls)
        return await cache.get()

    async def database_users(self) -> list[DashboardUser]:
        """The dashboard users, kept in memory (SDD §13.1): read again when the ``user`` kind's
        change stamp moved (one aggregate query, §11) or after USER_DIGESTS_MAX_AGE_S, so a
        failed authentication costs no document read."""
        stamp = await self.db.change_stamp("user")
        if self._users_current(stamp):
            return self._users
        async with self._users_lock:
            if self._users_current(stamp):
                return self._users
            users = await self.db.list("user", DashboardUser)
            self._users, self._users_stamp = users, stamp
            self._users_read_at = time.monotonic()
            return users

    def _users_current(self, stamp: tuple[int, int]) -> bool:
        return (
            stamp == self._users_stamp
            and time.monotonic() - self._users_read_at < USER_DIGESTS_MAX_AGE_S
        )

    @property
    def db(self) -> AsyncStore:
        return self.orchestrator.db

    @property
    def providers(self) -> Any:
        return self.orchestrator.providers

    def auth_locked(self, client: str) -> bool:
        """True while ``client`` exceeded ``auth_lockout_per_minute`` failed authentications
        in the last minute (Security.md API2). Consulted once the bootstrap file did not know the
        token - before the database users are looked up (SDD §13.1) - so a shared address
        (ingress, NAT) never locks out callers with valid bootstrap tokens."""
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


def validation_message(errors: Sequence[Any], fallback: str = "invalid request") -> str:
    """One line for a pydantic error list, as every 422 of SDD §12 carries it: the first five
    ``location: message`` items (a request body's ``body`` prefix dropped), else ``fallback``."""
    parts = []
    for err in list(errors)[:5]:
        location = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        parts.append(f"{location}: {err.get('msg')}" if location else str(err.get("msg")))
    return "; ".join(parts) or fallback


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _audit_denied(
    request: Request, reason: str, required: Role, principal: Principal | None = None
) -> None:
    svc = services(request)
    client = _client(request)
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


async def authenticate(request: Request) -> Principal | None:
    svc = services(request)
    if svc.settings.auth_disabled:
        return ANONYMOUS_ADMIN
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    principal = svc.tokens.authenticate(token.strip())
    if principal is not None:
        return principal
    if svc.auth_locked(_client(request)):
        return None  # SDD §13.1: a locked-out address never reaches the database
    digest = hash_token(token.strip())
    for user in await svc.database_users():  # no early exit, like the file tokens
        if hmac.compare_digest(user.sha256, digest):
            principal = Principal(user.name, user.role)
    return principal


#: Named permissions and the minimum role that holds each (SDD §13.2; TODO(ADV-04): confirm admin).
PERMISSIONS: dict[str, Role] = {
    "advisor.policy.manage": Role.admin,
    "advisor.approve": Role.admin,
}


def require_permission(name: str) -> Callable[[Request], Awaitable[Principal]]:
    """FastAPI dependency: the caller's principal, whose role must hold permission ``name``."""
    return require_role(PERMISSIONS[name])


def require_role(role: Role) -> Callable[[Request], Awaitable[Principal]]:
    """FastAPI dependency: the caller's principal, which must hold at least ``role``."""

    async def dependency(request: Request) -> Principal:
        svc = services(request)
        principal = await authenticate(request)
        if principal is None:
            client = _client(request)
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
