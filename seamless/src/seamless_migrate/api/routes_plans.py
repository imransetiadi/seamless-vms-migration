"""``/plans`` routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import ValidationError

from ..domain.enums import PlanStatus, ProviderRole, Role
from ..domain.models import (
    PLAN_EDITABLE_FIELDS,
    Migration,
    Plan,
    PlanCreate,
    PlanSpec,
    Provider,
    ValidationReport,
    invalid_plan_settings,
    repeated_vm_ids,
)
from ..events import emit
from ..orchestrator import in_flight_refusal
from ..plan_status import derive_plan_status
from ..planning.estimator import invalid_estimator_overrides
from ..security.auth import Principal
from ..store import NotFound
from .deps import ApiError, _audit_denied, require_role, services
from .schemas import PlanFinalizeRequest, PlanFinalizeResult, PlanOut, WavesAutoRequest

router = APIRouter(tags=["plans"])

#: Plan fields that decide whether a cutover needs a human (SDD §12): approver-only.
POLICY_FIELDS = frozenset(
    {"require_approval", "require_distinct_approver", "auto_cutover", "cutover_window"}
)


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


#: SDD §12: the longest plan name and description a request may set; the stored model stays
#: permissive so plans written before the check keep loading
PLAN_NAME_MAX = 200
PLAN_DESCRIPTION_MAX = 2000


def _check_plan_texts(spec: PlanCreate) -> None:
    problems = []
    if len(spec.name) > PLAN_NAME_MAX:
        problems.append(f"name: at most {PLAN_NAME_MAX} characters (got {len(spec.name)})")
    if spec.description is not None and len(spec.description) > PLAN_DESCRIPTION_MAX:
        problems.append(
            f"description: at most {PLAN_DESCRIPTION_MAX} characters (got {len(spec.description)})"
        )
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


async def _out(request: Request, plans: list[Plan]) -> list[PlanOut]:
    """The plans with their derived status (SDD §8.1), from one read of their migrations."""
    if not plans:
        return []
    migrations = await services(request).db.list(
        "migration", Migration, plan_id=[p.id for p in plans]
    )
    return [
        PlanOut(**p.model_dump(), derived_status=derive_plan_status(p, migrations)) for p in plans
    ]


async def _one(request: Request, plan: Plan) -> PlanOut:
    return (await _out(request, [plan]))[0]


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    request: Request,
    status: PlanStatus | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    _: Principal = Depends(require_role(Role.viewer)),
) -> list[PlanOut]:
    """Plans in creation order; ``status``, ``limit`` and ``offset`` run in SQL (SDD §12)."""
    filters = {"status": status} if status is not None else {}
    plans = await services(request).db.list("plan", Plan, limit=limit, offset=offset, **filters)
    return await _out(request, plans)


@router.post("/plans", response_model=PlanOut, status_code=201)
async def create_plan(
    body: PlanCreate, request: Request, principal: Principal = Depends(require_role(Role.operator))
) -> PlanOut:
    svc = services(request)
    await _check_policy_fields(request, _non_default_policy_fields(body), principal)
    _check_plan_texts(body)
    _check_vm_ids(body)
    _check_plan_settings(body)
    _check_estimator_overrides(body)
    plan = Plan(**body.model_dump())
    # SDD §12: a provider's deletion waits until the plan that uses it is saved, and then refuses
    async with svc.orchestrator.provider_lock():
        await _check_providers(request, body)
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
    return await _one(request, plan)


@router.get("/plans/{plan_id}", response_model=PlanOut)
async def get_plan(
    plan_id: str, request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> PlanOut:
    return await _one(request, await services(request).db.get("plan", plan_id, Plan))


@router.patch("/plans/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_role(Role.operator)),
) -> PlanOut:
    svc = services(request)
    unknown = sorted(set(body) - PLAN_EDITABLE_FIELDS)
    if unknown:
        raise ApiError(422, "validation_error", f"fields cannot be changed: {', '.join(unknown)}")
    # SDD §8: an edit sets the plan's status (back to draft) like validate, auto-waves, start and
    # pause, so it runs under the plan's lock: it waits for a running validation, which would
    # otherwise mark the edited plan validated with settings it never saw
    async with svc.orchestrator.plan_lock(plan_id):
        plan, version = await svc.db.get_versioned("plan", plan_id, Plan)
        await _check_policy_fields(request, _changed_policy_fields(body, plan), principal)
        if plan.status not in (PlanStatus.draft, PlanStatus.validated):
            raise ApiError(409, "conflict", f"a {plan.status} plan cannot be edited")
        busy = await svc.orchestrator.in_flight(plan_id)
        if busy:
            raise ApiError(409, "conflict", in_flight_refusal(busy, "editing the plan"))
        try:
            merged = PlanCreate.model_validate(
                {**plan.model_dump(mode="json", include=PLAN_EDITABLE_FIELDS), **body}
            )
        except ValidationError as exc:
            raise ApiError(422, "validation_error", _summarize(exc)) from None
        _check_plan_texts(merged)
        _check_vm_ids(merged)
        _check_plan_settings(merged)
        _check_estimator_overrides(merged)
        updated = Plan.model_validate(
            {
                **plan.model_dump(mode="json"),
                **merged.model_dump(mode="json"),
                "status": PlanStatus.draft,
                "updated_at": svc.orchestrator.now(),
            }
        )
        async with svc.orchestrator.provider_lock():  # SDD §12, as for a new plan
            await _check_providers(request, merged)
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
    return await _one(request, updated)


@router.post("/plans/{plan_id}/waves/auto", response_model=PlanOut)
async def auto_waves(
    plan_id: str,
    request: Request,
    body: WavesAutoRequest | None = None,
    principal: Principal = Depends(require_role(Role.operator)),
) -> PlanOut:
    size = (body or WavesAutoRequest()).max_wave_size
    plan = await services(request).orchestrator.auto_waves(plan_id, size, principal.name)
    return await _one(request, plan)


@router.post("/plans/{plan_id}/validate", response_model=ValidationReport)
async def validate_plan(
    plan_id: str, request: Request, principal: Principal = Depends(require_role(Role.operator))
) -> ValidationReport:
    return await services(request).orchestrator.validate_plan(plan_id, principal.name)


@router.post("/plans/{plan_id}/start", response_model=PlanOut)
async def start_plan(
    plan_id: str, request: Request, principal: Principal = Depends(require_role(Role.operator))
) -> PlanOut:
    return await _one(
        request, await services(request).orchestrator.start_plan(plan_id, principal.name)
    )


@router.post("/plans/{plan_id}/pause", response_model=PlanOut)
async def pause_plan(
    plan_id: str, request: Request, principal: Principal = Depends(require_role(Role.operator))
) -> PlanOut:
    return await _one(
        request, await services(request).orchestrator.pause_plan(plan_id, principal.name)
    )


@router.post("/plans/{plan_id}/finalize", response_model=PlanFinalizeResult)
async def finalize_plan(
    plan_id: str,
    body: PlanFinalizeRequest,
    request: Request,
    principal: Principal = Depends(require_role(Role.approver)),
) -> PlanFinalizeResult:
    """Finalize a wave or the plan with one approval, auditing each VM (SDD §8.1, ADV-19)."""
    out = await services(request).orchestrator.finalize_plan(
        plan_id,
        principal.name,
        wave_id=body.wave_id,
        confirm=body.confirm,
        delete_source=body.delete_source,
    )
    return PlanFinalizeResult.model_validate(out)


def _summarize(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        location = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{location}: {err.get('msg')}" if location else str(err.get("msg")))
    return "; ".join(parts) or "invalid request"
