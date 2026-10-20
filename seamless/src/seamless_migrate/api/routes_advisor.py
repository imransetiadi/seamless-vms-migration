"""Advisor policies, approval and override (SDD §12, §13.2, §14.6, ADV-04)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from ..ai.policies import builtin_default
from ..domain.enums import Role
from ..domain.models import (
    AdvisorPolicy,
    AdvisorPolicySpec,
    AdvisorRecommendation,
    Event,
    Plan,
    Provider,
)
from ..events import delete_and_emit, new_event, put_all_and_emit
from ..security.auth import Principal
from ..store import ConflictError, NotFound
from .deps import (
    ApiError,
    Services,
    refuse_unknown_fields,
    require_permission,
    require_role,
    services,
)
from .schemas import EmptyRequest, OverrideRequest, PolicyList

router = APIRouter(tags=["advisor"])


async def _check_scope(svc: Services, spec: AdvisorPolicySpec) -> None:
    """A policy names providers and plans that exist (422 otherwise)."""
    providers = {p.id for p in await svc.db.list("provider", Provider)}
    plans = {p.id for p in await svc.db.list("plan", Plan)}
    unknown = [p for p in spec.scope.provider_ids if p not in providers] + [
        p for p in spec.scope.plan_ids if p not in plans
    ]
    if unknown:
        raise ApiError(422, "validation_error", f"unknown provider or plan: {', '.join(unknown)}")


def _audit(kind: str, principal: Principal, policy_id: str, before: Any, after: Any) -> Event:
    """A policy change's audit event, written in the change's transaction (SDD §14.5, review
    RV51-11): a store error leaves the policy as it was and no event."""

    def dump(policy: AdvisorPolicy | None) -> Any:
        return policy.model_dump(mode="json") if policy is not None else None

    return new_event(
        kind,
        f"advisor policy {policy_id} {kind.removeprefix('policy.')}",
        actor=f"user:{principal.name}",
        data={"target_id": policy_id, "before": dump(before), "after": dump(after)},
    )


@router.get("/advisor/policies", response_model=PolicyList)
async def list_policies(
    request: Request, _: Principal = Depends(require_role(Role.viewer))
) -> PolicyList:
    svc = services(request)
    items = await svc.db.list("advisor_policy", AdvisorPolicy)
    return PolicyList(items=items, default=builtin_default(svc.settings))


@router.post("/advisor/policies", response_model=AdvisorPolicy, status_code=201)
async def create_policy(
    body: AdvisorPolicySpec,
    request: Request,
    principal: Principal = Depends(require_permission("advisor.policy.manage")),
) -> AdvisorPolicy:
    svc = services(request)
    await refuse_unknown_fields(request, AdvisorPolicySpec)
    await _check_scope(svc, body)
    policy = AdvisorPolicy(**body.model_dump(), updated_by=principal.name)
    created = _audit("policy.created", principal, policy.id, None, policy)
    await put_all_and_emit(svc.store, svc.bus, [("advisor_policy", policy, 0)], [created])
    return policy


@router.put("/advisor/policies/{policy_id}", response_model=AdvisorPolicy)
async def replace_policy(
    policy_id: str,
    body: AdvisorPolicySpec,
    request: Request,
    principal: Principal = Depends(require_permission("advisor.policy.manage")),
) -> AdvisorPolicy:
    svc = services(request)
    await refuse_unknown_fields(request, AdvisorPolicySpec)
    await _check_scope(svc, body)
    before, version = await svc.db.get_versioned("advisor_policy", policy_id, AdvisorPolicy)
    after = AdvisorPolicy(
        **body.model_dump(),
        id=policy_id,
        created_at=before.created_at,
        updated_by=principal.name,
    )
    updated = _audit("policy.updated", principal, policy_id, before, after)
    try:
        await put_all_and_emit(svc.store, svc.bus, [("advisor_policy", after, version)], [updated])
    except ConflictError:
        raise ApiError(
            409, "conflict", f"policy {policy_id} changed meanwhile; try again"
        ) from None
    return after


@router.delete("/advisor/policies/{policy_id}", status_code=204)
async def delete_policy(
    policy_id: str,
    request: Request,
    principal: Principal = Depends(require_permission("advisor.policy.manage")),
) -> Response:
    svc = services(request)
    before, version = await svc.db.get_versioned("advisor_policy", policy_id, AdvisorPolicy)
    deleted = _audit("policy.deleted", principal, policy_id, before, None)
    try:
        # the version read: a replace landing in between is refused, never deleted unseen with a
        # stale before in its audit event (SDD §12, §14.5, review ST-05)
        await delete_and_emit(svc.store, svc.bus, "advisor_policy", policy_id, version, [deleted])
    except NotFound:
        raise  # deleted meanwhile
    except ConflictError:
        raise ApiError(
            409, "conflict", f"policy {policy_id} changed meanwhile; try again"
        ) from None
    return Response(status_code=204)


@router.post("/advisor/recommendations/{rec_id}/approve", response_model=AdvisorRecommendation)
async def approve_recommendation(
    rec_id: str,
    request: Request,
    body: EmptyRequest | None = None,
    principal: Principal = Depends(require_permission("advisor.approve")),
) -> AdvisorRecommendation:
    return await services(request).orchestrator.approve_recommendation(rec_id, principal.name)


@router.post("/advisor/recommendations/{rec_id}/override", response_model=AdvisorRecommendation)
async def override_recommendation(
    rec_id: str,
    body: OverrideRequest,
    request: Request,
    principal: Principal = Depends(require_permission("advisor.approve")),
) -> AdvisorRecommendation:
    return await services(request).orchestrator.override_recommendation(
        rec_id, str(body.option), body.reason, principal.name
    )
