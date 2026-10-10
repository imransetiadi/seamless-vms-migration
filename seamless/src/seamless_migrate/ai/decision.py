"""Advisor decisions (SDD §14.4, ADV-01).

``decide()`` is the only place a decision is taken on an advisor recommendation: the engine
reads only ``Decision.final_option`` (through ``migration.strategy``) and the dashboard never
compares a confidence with a threshold.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ..config import Settings
from ..domain.enums import DecisionStatus, ReasonCode
from ..domain.models import AdvisorActor, Decision

GLOBAL_DEFAULT_POLICY_ID = "global-default"
#: jev_decide's escape hatches: an answer that is not one of the options (SDD §14.1)
ESCAPE_HATCHES = frozenset({"ask_user", "investigate", "none"})
#: Jev statuses that make an answer unusable
UNUSABLE_STATUSES = frozenset({"escalate", "invalid_response"})


@dataclass(frozen=True)
class Advice:
    """What the advisor recommends, as Jev answered it (or why it did not)."""

    option: str | None
    confidence: float | None
    distribution: dict[str, float] = field(default_factory=dict)
    #: the options the advisor may choose between (the advisor scope, SDD §9.2)
    scope: Sequence[str] = ()
    #: every eligible option offered to Jev - what an override may pick
    options: Sequence[str] = ()
    status: str | None = None
    escaped: bool = False
    #: why Jev gave no answer at all (off, unreachable, timed out)
    unavailable: str | None = None
    model_version: str | None = None
    deterministic: str | None = None
    deterministic_reason: str | None = None
    #: the jev_decide arguments, built even when Jev is not asked: the input snapshot (§14.5)
    request: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PolicyRule:
    """The part of an advisor policy ``decide()`` needs (stored policies arrive with ADV-04)."""

    id: str
    mode: Literal["auto_apply_above_threshold", "always_require_approval"]
    threshold: float
    min_evidence_for_auto_apply: int = 0
    require_override_reason: bool = False
    require_distinct_approver: bool = True


@dataclass(frozen=True)
class Approve:
    user: str


@dataclass(frozen=True)
class Override:
    user: str
    option: str
    reason: str | None = None


def default_policy(settings: Settings) -> PolicyRule:
    """The global default until stored policies exist: today's behaviour (SDD §14.4)."""
    return PolicyRule(
        id=GLOBAL_DEFAULT_POLICY_ID,
        mode="auto_apply_above_threshold",
        threshold=settings.jev_min_confidence,
    )


def unusable(advice: Advice) -> str | None:
    """Why the advice cannot be applied, or ``None`` when it can."""
    if advice.unavailable:
        return f"advisor unavailable: {advice.unavailable}"
    if advice.option is None:
        return f"the advisor gave no option{f' (status {advice.status})' if advice.status else ''}"
    if advice.escaped or advice.option in ESCAPE_HATCHES:
        return f"the advisor answered {advice.option}, not an option"
    if advice.status in UNUSABLE_STATUSES:
        return f"the advisor's answer was {advice.status}"
    if advice.option not in advice.scope:
        return f"{advice.option} is not among the options it may choose ({', '.join(advice.scope)})"
    return None


def decide(
    recommendation: Advice,
    policy: PolicyRule,
    evidence_count: int,
    default_option: str | None,
    *,
    action: Approve | Override | None = None,
    now: datetime,
) -> Decision:
    """Take the decision on ``recommendation`` - in the order of SDD §14.4."""
    why_not = unusable(recommendation)
    if isinstance(action, Approve):
        if why_not is not None:
            raise ValueError(f"nothing to approve: {why_not}")
        return Decision(
            final_option=recommendation.option,
            status=DecisionStatus.approved,
            reason_code=ReasonCode.APPROVED_BY_USER,
            actor=AdvisorActor(kind="user", id=action.user),
            policy_id=policy.id,
            decided_at=now,
        )
    if isinstance(action, Override):
        if action.option not in recommendation.options:
            raise ValueError(f"{action.option} is not an eligible option")
        return Decision(
            final_option=action.option,
            status=DecisionStatus.overridden,
            reason_code=ReasonCode.OVERRIDDEN_BY_USER,
            reason_text=action.reason,
            actor=AdvisorActor(kind="user", id=action.user),
            policy_id=policy.id,
            decided_at=now,
        )
    if why_not is not None:
        return Decision(
            final_option=default_option,
            status=DecisionStatus.fallback_rules,
            reason_code=ReasonCode.ADVISOR_UNAVAILABLE_FALLBACK,
            reason_text=why_not,
            actor=AdvisorActor(kind="system", id="fallback"),
            decided_at=now,
        )
    by_policy = {
        "actor": AdvisorActor(kind="policy", id=policy.id),
        "policy_id": policy.id,
        "threshold_used": policy.threshold,
        "decided_at": now,
    }
    if policy.mode == "always_require_approval":
        return Decision(
            final_option=None,
            status=DecisionStatus.pending_approval,
            reason_code=ReasonCode.POLICY_REQUIRES_APPROVAL,
            **by_policy,
        )
    confidence = recommendation.confidence
    if confidence is None or confidence < policy.threshold:
        return Decision(
            final_option=default_option,
            status=DecisionStatus.below_threshold,
            reason_code=ReasonCode.BELOW_THRESHOLD_DEFAULT_KEPT,
            **by_policy,
        )
    if evidence_count < policy.min_evidence_for_auto_apply:
        return Decision(
            final_option=None,
            status=DecisionStatus.pending_approval,
            reason_code=ReasonCode.INSUFFICIENT_EVIDENCE,
            **by_policy,
        )
    return Decision(
        final_option=recommendation.option,
        status=DecisionStatus.auto_applied,
        reason_code=ReasonCode.ABOVE_THRESHOLD_AUTO_APPLY,
        **by_policy,
    )
