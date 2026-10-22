"""``/hooks`` routes: the hook catalog an admin keeps (SDD §12, §23.3, plan task N3)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body, Depends, Request, Response
from pydantic import ValidationError

from ..domain.enums import PlanStatus, Role
from ..domain.models import REQUEST_BODY, Hook, Plan, PlanSpec, Provider
from ..events import emit
from ..plan_checks import unknown_hook
from ..security.auth import Principal
from ..store import ConflictError
from .deps import ApiError, require_role, services, validation_message

router = APIRouter(tags=["hooks"])

#: PATCH /hooks/{name} (SDD §12); the name, the kind and the server-owned fields are immutable
HOOK_EDITABLE_FIELDS = frozenset(
    {"description", "playbook", "url", "secret", "timeout_s", "revert"}
)
#: plans that need no hook any more: a completed plan may still roll back or finalize (SDD §12)
RELEASED_PLAN_STATUSES = frozenset({PlanStatus.cancelled})


def _hook(body: dict[str, Any]) -> Hook:
    """``body`` as a hook, a field it does not take refused at any depth (SDD §12, audit AP-01)."""
    try:
        return Hook.model_validate(body, context=REQUEST_BODY)
    except ValidationError as exc:
        raise ApiError(
            422, "validation_error", validation_message(exc.errors(), "invalid hook")
        ) from None


async def _check_hook(request: Request, hook: Hook) -> None:
    """What the catalog decides of a hook (SDD §12): its revert is another hook of the catalog, and
    its secret no provider's credential - a webhook sends its secret's token to its URL."""
    db = services(request).db
    if hook.revert is not None and hook.revert not in {h.name for h in await db.list("hook", Hook)}:
        raise ApiError(422, "validation_error", f"revert: no hook {hook.revert!r} in the catalog")
    if hook.secret is None:
        return
    # compared as the secret readers fold a name (SDD §13.3, audit AP-04)
    if re.sub(r"[^a-z0-9]", "-", hook.secret.lower()).startswith("provider-"):
        raise ApiError(
            422,
            "validation_error",
            "secret: provider-* names belong to the secrets Seamless stores for each provider; "
            "a webhook would send it to its URL",
        )
    for provider in await db.list("provider", Provider):
        named = {provider.credentials_secret}
        if provider.conversion_host is not None:
            named.add(provider.conversion_host.ssh_key_secret)
        if hook.secret in named:
            raise ApiError(
                422,
                "validation_error",
                f"secret: provider {provider.id} names it as its credential; a webhook would send "
                "it to its URL",
            )


async def check_plan_hooks(request: Request, spec: PlanSpec) -> None:
    """A plan names catalog hooks alone (SDD §12): checked under the lock DELETE /hooks/{name}
    takes - the provider lock -, so no deletion lands between the check and the plan's write."""
    names = {h.name for h in await services(request).db.list("hook", Hook)}
    why = unknown_hook(spec, names)
    if why is not None:
        raise ApiError(422, "validation_error", why)


async def _emit(request: Request, kind: str, hook: Hook, actor: str, **data: Any) -> None:
    """A hook.* event (SDD §4.3): the hook's name and kind - never its URL or its secret's name."""
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


@router.get("/hooks", response_model=list[Hook])
async def list_hooks(
    request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> list[Hook]:
    return sorted(await services(request).db.list("hook", Hook), key=lambda h: h.name)


@router.post("/hooks", response_model=Hook, status_code=201)
async def create_hook(
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.admin)),
) -> Hook:
    svc = services(request)
    now = svc.orchestrator.now()
    hook = _hook(body).model_copy(update={"created_at": now, "updated_at": now})
    async with svc.orchestrator.provider_lock():
        await _check_hook(request, hook)
        try:
            await svc.db.put("hook", hook, expected_version=0)
        except ConflictError:
            raise ApiError(409, "conflict", f"hook {hook.name!r} already exists") from None
    await _emit(request, "hook.created", hook, principal.name)
    return hook


@router.get("/hooks/{name}", response_model=Hook)
async def get_hook(
    name: str, request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> Hook:
    return await services(request).db.get("hook", name, Hook)


@router.patch("/hooks/{name}", response_model=Hook)
async def update_hook(
    name: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.admin)),
) -> Hook:
    svc = services(request)
    unknown = sorted(set(body) - HOOK_EDITABLE_FIELDS)
    if unknown:
        raise ApiError(422, "validation_error", f"fields cannot be changed: {', '.join(unknown)}")
    async with svc.orchestrator.provider_lock():
        hook, version = await svc.db.get_versioned("hook", name, Hook)
        merged = _hook({**hook.model_dump(mode="json"), **body})
        updated = merged.model_copy(
            update={"created_at": hook.created_at, "updated_at": svc.orchestrator.now()}
        )
        await _check_hook(request, updated)
        await svc.db.put("hook", updated, expected_version=version)
    await _emit(request, "hook.updated", updated, principal.name, fields=sorted(body))
    return updated


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
    await _emit(request, "hook.deleted", hook, principal.name)
    return Response(status_code=204)
