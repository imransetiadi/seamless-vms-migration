"""``/plans`` routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import ValidationError

from ..domain.enums import PlanStatus, ProviderRole, Role
from ..domain.models import (
    PLAN_EDITABLE_FIELDS,
    Plan,
    PlanCreate,
    PlanSpec,
    Provider,
    ValidationReport,
    invalid_plan_settings,
    repeated_vm_ids,
)
from ..events import emit
from ..planning.estimator import invalid_estimator_overrides
from ..security.auth import Principal
from ..store import NotFound
from .deps import ApiError, _audit_denied, require_role, services
from .schemas import WavesAutoRequest

router = APIRouter(tags=["plans"])

#: Plan fields that decide whether a cutover needs a human (SDD §12): approver-only.
POLICY_FIELDS = frozenset({"require_approval", "auto_cutover", "cutover_window"})


async def _check_policy_fields(request: Request, fields: set[str], principal: Principal) -> None:
    touched = sorted(fields & POLICY_FIELDS)
    if touched and not principal.role.at_least(Role.approver):
        reason = f"setting {', '.join(touched)} requires the {Role.approver} role"
        await _audit_denied(request, reason, Role.approver, principal)
        raise ApiError(403, "forbidden", reason)


def _check_vm_ids(spec: PlanCreate) -> None:
    """One VM, one migration (SDD §12): a repeated id would make two migrations cut it over."""
    repeated = repeated_vm_ids(spec.vm_ids)
    if repeated:
        raise ApiError(
            422, "validation_error", f"vm_ids lists a VM more than once: {', '.join(repeated[:10])}"
        )


def _check_plan_settings(spec: PlanCreate) -> None:
    problems = invalid_plan_settings(spec)
    if problems:
        raise ApiError(422, "validation_error", "; ".join(problems))


def _check_estimator_overrides(spec: PlanCreate) -> None:
    problems = invalid_estimator_overrides(spec.estimator_overrides)
    if problems:
        raise ApiError(400, "bad_request", f"invalid estimator_overrides: {'; '.join(problems)}")


def _non_default_policy_fields(body: PlanCreate) -> set[str]:
    """Policy fields the request sets to something other than the default."""
    return {
        name
        for name in body.model_fields_set & POLICY_FIELDS
        if getattr(body, name) != PlanSpec.model_fields[name].default
    }


def _changed_policy_fields(body: dict[str, Any], plan: Plan) -> set[str]:
    """PATCH counterpart of :func:`_non_default_policy_fields`: policy fields whose value
    differs from the plan's current one (re-sending the current value changes nothing)."""
    current = plan.model_dump(mode="json", include=POLICY_FIELDS)
    return {name for name in set(body) & POLICY_FIELDS if body[name] != current.get(name)}


async def _check_providers(request: Request, spec: PlanCreate) -> None:
    db = services(request).db
    for provider_id, role in (
        (spec.source_provider_id, ProviderRole.source),
        (spec.destination_provider_id, ProviderRole.destination),
    ):
        try:
            provider = await db.get("provider", provider_id, Provider)
        except NotFound:
            raise ApiError(
                400, "bad_request", f"{role} provider {provider_id!r} does not exist"
            ) from None
        if provider.role != role:
            raise ApiError(400, "bad_request", f"provider {provider_id!r} is not a {role} provider")


@router.get("/plans", response_model=list[Plan])
async def list_plans(
    request: Request,
    status: PlanStatus | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    _: Principal = Depends(require_role(Role.viewer)),
) -> list[Plan]:
    """Plans in creation order; ``status``, ``limit`` and ``offset`` run in SQL (SDD §12)."""
    filters = {"status": status} if status is not None else {}
    return await services(request).db.list("plan", Plan, limit=limit, offset=offset, **filters)


@router.post("/plans", response_model=Plan, status_code=201)
async def create_plan(
    body: PlanCreate, request: Request, principal: Principal = Depends(require_role(Role.operator))
) -> Plan:
    svc = services(request)
    await _check_policy_fields(request, _non_default_policy_fields(body), principal)
    _check_vm_ids(body)
    _check_plan_settings(body)
    _check_estimator_overrides(body)
    await _check_providers(request, body)
    plan = Plan(**body.model_dump())
    await svc.db.put("plan", plan, expected_version=0)
    await emit(
        svc.store,
        svc.bus,
        "plan.created",
        f"plan {plan.name} created",
        plan_id=plan.id,
        actor=principal.name,
        data={"name": plan.name, "vms": len(plan.vm_ids)},
    )
    return plan


@router.get("/plans/{plan_id}", response_model=Plan)
async def get_plan(
    plan_id: str, request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> Plan:
    return await services(request).db.get("plan", plan_id, Plan)


@router.patch("/plans/{plan_id}", response_model=Plan)
async def update_plan(
    plan_id: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.operator)),
) -> Plan:
    svc = services(request)
    unknown = sorted(set(body) - PLAN_EDITABLE_FIELDS)
    if unknown:
        raise ApiError(422, "validation_error", f"fields cannot be changed: {', '.join(unknown)}")
    plan, version = await svc.db.get_versioned("plan", plan_id, Plan)
    await _check_policy_fields(request, _changed_policy_fields(body, plan), principal)
    if plan.status not in (PlanStatus.draft, PlanStatus.validated):
        raise ApiError(409, "conflict", f"a {plan.status} plan cannot be edited")
    try:
        merged = PlanCreate.model_validate(
            {**plan.model_dump(mode="json", include=PLAN_EDITABLE_FIELDS), **body}
        )
    except ValidationError as exc:
        raise ApiError(422, "validation_error", _summarize(exc)) from None
    _check_vm_ids(merged)
    _check_plan_settings(merged)
    _check_estimator_overrides(merged)
    await _check_providers(request, merged)
    updated = Plan.model_validate(
        {
            **plan.model_dump(mode="json"),
            **merged.model_dump(mode="json"),
            "status": PlanStatus.draft,
            "updated_at": svc.orchestrator.now(),
        }
    )
    await svc.db.put("plan", updated, expected_version=version)
    await emit(
        svc.store,
        svc.bus,
        "plan.updated",
        f"plan {updated.name} updated",
        plan_id=plan_id,
        actor=principal.name,
        data={"fields": sorted(body)},
    )
    return updated


@router.post("/plans/{plan_id}/waves/auto", response_model=Plan)
async def auto_waves(
    plan_id: str,
    request: Request,
    body: WavesAutoRequest | None = None,
    principal: Principal = Depends(require_role(Role.operator)),
) -> Plan:
    size = (body or WavesAutoRequest()).max_wave_size
    return await services(request).orchestrator.auto_waves(plan_id, size, principal.name)


@router.post("/plans/{plan_id}/validate", response_model=ValidationReport)
async def validate_plan(
    plan_id: str, request: Request, principal: Principal = Depends(require_role(Role.operator))
) -> ValidationReport:
    return await services(request).orchestrator.validate_plan(plan_id, principal.name)


@router.post("/plans/{plan_id}/start", response_model=Plan)
async def start_plan(
    plan_id: str, request: Request, principal: Principal = Depends(require_role(Role.operator))
) -> Plan:
    return await services(request).orchestrator.start_plan(plan_id, principal.name)


@router.post("/plans/{plan_id}/pause", response_model=Plan)
async def pause_plan(
    plan_id: str, request: Request, principal: Principal = Depends(require_role(Role.operator))
) -> Plan:
    return await services(request).orchestrator.pause_plan(plan_id, principal.name)


def _summarize(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        location = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{location}: {err.get('msg')}" if location else str(err.get("msg")))
    return "; ".join(parts) or "invalid request"
