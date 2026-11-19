"""``/hooks`` routes: the hook catalog an admin keeps (SDD §12, §23.3, plan task N3)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Request, Response
from pydantic import ValidationError

from ..domain.enums import PlanStatus, Role
from ..domain.models import (
    HOOK_TOKEN,
    REQUEST_BODY,
    Hook,
    HookView,
    Plan,
    PlanSpec,
    hook_secret_name,
    hook_view,
)
from ..events import emit
from ..plan_checks import reserved_tsig_secret, unknown_hook
from ..security.auth import Principal
from ..security.destinations import DestinationNotAllowed, check_stored
from ..security.secret_store import SecretStoreError, secret_store
from ..store import ConflictError
from .deps import ApiError, require_role, services, validation_message

router = APIRouter(tags=["hooks"])
log = logging.getLogger(__name__)

#: PATCH /hooks/{name} (SDD §12); the name, the kind and the server-owned fields are immutable.
#: ``secret`` is the bearer token, write-only: it reaches the secret store and no answer
HOOK_EDITABLE_FIELDS = frozenset(
    {"description", "playbook", "url", "secret", "timeout_s", "revert"}
)
#: what a request never sets (SDD §12): ignored on POST, refused on PATCH
_SERVER_OWNED = ("created_at", "updated_at", "secret_updated_at")
#: plans that need no hook any more: a completed plan may still roll back or finalize (SDD §12)
RELEASED_PLAN_STATUSES = frozenset({PlanStatus.cancelled})
#: the store's label of a hook's secret (SDD §13.3)
_LABEL = "seamless.io/hook"


def _hook(body: dict[str, Any]) -> Hook:
    """``body`` as a hook, a field it does not take refused at any depth (SDD §12, audit AP-01)."""
    try:
        return Hook.model_validate(body, context=REQUEST_BODY)
    except ValidationError as exc:
        raise ApiError(
            422, "validation_error", validation_message(exc.errors(), "invalid hook")
        ) from None


def _token(value: Any, kind: str) -> str:
    """A webhook hook's bearer token as a request gives it (SDD §12, §13.3), else 422 - which
    never repeats the value."""
    if kind != "webhook":
        raise ApiError(422, "validation_error", f"secret: a {kind} hook takes none")
    if not isinstance(value, str) or not HOOK_TOKEN.fullmatch(value):
        raise ApiError(
            422,
            "validation_error",
            "secret: the bearer token, 1-4096 visible ASCII characters without a space",
        )
    return value


async def _write_token(request: Request, hook: Hook, token: str) -> None:
    """The token into the secret store under ``hook-{name}`` (SDD §13.3); 503 when the store
    cannot take it - the reason names the store, never the value."""
    svc = services(request)
    try:
        store = secret_store(svc.settings)
        await asyncio.to_thread(
            store.write, hook_secret_name(hook.name), {"token": token}, {_LABEL: hook.name}
        )
    except SecretStoreError as exc:
        raise ApiError(503, "unavailable", f"secret store: {exc}") from None


async def _check_hook(request: Request, hook: Hook) -> None:
    """What the catalog decides of a hook (SDD §12): its revert is another hook of the catalog,
    and a webhook's URL names no destination the control plane refuses (§13.5)."""
    db = services(request).db
    if hook.url is not None:
        try:
            check_stored(hook.url, services(request).settings)
        except DestinationNotAllowed as exc:
            raise ApiError(422, "validation_error", str(exc)) from None
    if hook.revert is not None and hook.revert not in {h.name for h in await db.list("hook", Hook)}:
        raise ApiError(422, "validation_error", f"revert: no hook {hook.revert!r} in the catalog")


def _view(hook: Hook, principal: Principal) -> HookView:
    """The hook as the principal's role reads it (SDD §12)."""
    return hook_view(hook, principal.role.at_least(Role.admin))


async def check_plan_hooks(request: Request, spec: PlanSpec) -> None:
    """A plan names catalog hooks alone (SDD §12): checked under the lock DELETE /hooks/{name}
    takes - the provider lock -, so no deletion lands between the check and the plan's write."""
    names = {h.name for h in await services(request).db.list("hook", Hook)}
    why = unknown_hook(spec, names)
    if why is not None:
        raise ApiError(422, "validation_error", why)
    # a DNS target's TSIG secret is never a notification channel's nor a hook's (SDD §13.3)
    why = reserved_tsig_secret(spec)
    if why is not None:
        raise ApiError(422, "validation_error", why)


async def _emit(request: Request, kind: str, hook: Hook, actor: str, **data: Any) -> None:
    """A hook.* event (SDD §4.3): the hook's name and kind - never its URL or its secret."""
    svc = services(request)
    verb = kind.split(".", 1)[1]
    await emit(
        svc.store,
        svc.bus,
        kind,
        f"hook {hook.name} {verb}",
        actor=actor,
        data={"name": hook.name, "kind": hook.kind, **data},
    )


@router.get("/hooks", response_model=list[HookView])
async def list_hooks(
    request: Request, principal: Principal = Depends(require_role(Role.viewer))
) -> list[HookView]:
    hooks = sorted(await services(request).db.list("hook", Hook), key=lambda h: h.name)
    return [_view(hook, principal) for hook in hooks]


@router.post("/hooks", response_model=HookView, status_code=201)
async def create_hook(
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.admin)),
) -> HookView:
    svc = services(request)
    now = svc.orchestrator.now()
    body = {k: v for k, v in body.items() if k not in _SERVER_OWNED}
    given = "secret" in body and body["secret"] is not None
    secret = body.pop("secret", None)
    hook = _hook(body)
    token = _token(secret, hook.kind) if given else None
    hook = hook.model_copy(
        update={
            "created_at": now,
            "updated_at": now,
            "secret_updated_at": now if token is not None else None,
        }
    )
    async with svc.orchestrator.provider_lock():
        await _check_hook(request, hook)
        if hook.name in {h.name for h in await svc.db.list("hook", Hook)}:
            # before the token is written: a refused registration replaces no hook's secret
            raise ApiError(409, "conflict", f"hook {hook.name!r} already exists")
        if token is not None:
            await _write_token(request, hook, token)
        try:
            await svc.db.put("hook", hook, expected_version=0)
        except ConflictError:
            raise ApiError(409, "conflict", f"hook {hook.name!r} already exists") from None
    await _emit(request, "hook.created", hook, principal.name)
    return _view(hook, principal)


@router.get("/hooks/{name}", response_model=HookView)
async def get_hook(
    name: str, request: Request, principal: Principal = Depends(require_role(Role.viewer))
) -> HookView:
    return _view(await services(request).db.get("hook", name, Hook), principal)


async def _update(
    request: Request, name: str, body: dict[str, Any], principal: Principal
) -> HookView:
    """PATCH's and PUT …/secret's write (SDD §12): the merged hook checked as POST checks one, a
    token written to the store before the document that records when."""
    svc = services(request)
    body = dict(body)
    given = "secret" in body
    secret = body.pop("secret", None)
    async with svc.orchestrator.provider_lock():
        hook, version = await svc.db.get_versioned("hook", name, Hook)
        token = _token(secret, hook.kind) if given else None
        merged = _hook({**hook.model_dump(mode="json", exclude={"secret"}), **body})
        now = svc.orchestrator.now()
        updated = merged.model_copy(
            update={
                "created_at": hook.created_at,
                "updated_at": now,
                # a token the API takes replaces the secret an earlier version's hook named
                "secret": None if token is not None else hook.secret,
                "secret_updated_at": now if token is not None else hook.secret_updated_at,
            }
        )
        await _check_hook(request, updated)
        if token is not None:
            await _write_token(request, updated, token)
        await svc.db.put("hook", updated, expected_version=version)
    fields = sorted([*body, "secret"] if given else body)
    await _emit(request, "hook.updated", updated, principal.name, fields=fields)
    return _view(updated, principal)


@router.patch("/hooks/{name}", response_model=HookView)
async def update_hook(
    name: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.admin)),
) -> HookView:
    unknown = sorted(set(body) - HOOK_EDITABLE_FIELDS)
    if unknown:
        raise ApiError(422, "validation_error", f"fields cannot be changed: {', '.join(unknown)}")
    return await _update(request, name, body, principal)


@router.put("/hooks/{name}/secret", response_model=HookView)
async def set_hook_secret(
    name: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.admin)),
) -> HookView:
    """Write-only (SDD §12, §13.3): the bearer token reaches the secret store under hook-{name}
    and no route returns it."""
    await services(request).db.get("hook", name, Hook)  # 404 before the body is judged
    if set(body) != {"token"}:
        raise ApiError(422, "validation_error", 'a hook\'s secret is {"token"} alone')
    return await _update(request, name, {"secret": body["token"]}, principal)


@router.delete("/hooks/{name}", status_code=204)
async def delete_hook(
    name: str, request: Request, principal: Principal = Depends(require_role(Role.admin))
) -> Response:
    svc = services(request)
    # SDD §12: plan creations and edits check their hooks under the same lock
    async with svc.orchestrator.provider_lock():
        hook = await svc.db.get("hook", name, Hook)  # 404 when missing
        plans = [
            f"{p.name} ({p.id})"
            for p in await svc.db.list("plan", Plan)
            if p.status not in RELEASED_PLAN_STATUSES and name in p.network_cutover.hook_names()
        ]
        if plans:
            rest = len(plans) - 10
            raise ApiError(
                409,
                "conflict",
                f"hook {name} is named by plan(s) {', '.join(plans[:10])}"
                f"{f' and {rest} more' if rest > 0 else ''}: remove it from their network_cutover "
                "first",
            )
        reverting = sorted(h.name for h in await svc.db.list("hook", Hook) if h.revert == name)
        if reverting:
            raise ApiError(
                409,
                "conflict",
                f"hook {name} is the revert of hook(s) {', '.join(reverting)}: change their "
                "revert first",
            )
        await svc.db.delete("hook", name)
    # the secret the store holds for the hook goes with it (SDD §13.3), as a channel's; the
    # deletion is audited whatever the store answers, saying when the secret was left
    left: dict[str, Any] = {}
    try:
        await asyncio.to_thread(secret_store(svc.settings).delete, hook_secret_name(name))
    except SecretStoreError as exc:
        left = {"secret_left": hook_secret_name(name)}
        log.warning("a deleted hook's secret could not be removed: %s", exc)
    await _emit(request, "hook.deleted", hook, principal.name, **left)
    return Response(status_code=204)
