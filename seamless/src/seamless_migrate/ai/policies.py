"""Advisor policies (SDD §14.6, ADV-04): which policy is in force for a decision.

The most specific level wins - a plan, then the plan's source provider, then everywhere - and
within one level the strictest policy. Without a stored policy the built-in ``global-default``
keeps the behaviour before policies existed.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from ..config import Settings
from ..domain.enums import DecisionType
from ..domain.models import AdvisorPolicy, PolicyScope
from .decision import GLOBAL_DEFAULT_POLICY_ID, PolicyRule, default_policy

GLOBAL_DEFAULT = GLOBAL_DEFAULT_POLICY_ID


def builtin_default(settings: Settings) -> AdvisorPolicy:
    """The built-in default as a policy, for listing it beside the stored ones (read-only)."""
    rule = default_policy(settings)
    since = datetime(1970, 1, 1, tzinfo=UTC)
    return AdvisorPolicy(
        id=rule.id,
        name="Global default",
        scope=PolicyScope(),
        mode=rule.mode,
        threshold=rule.threshold,
        min_evidence_for_auto_apply=rule.min_evidence_for_auto_apply,
        require_override_reason=rule.require_override_reason,
        require_distinct_approver=rule.require_distinct_approver,
        created_at=since,
        updated_at=since,
        updated_by="system",
    )


def rule_of(policy: AdvisorPolicy) -> PolicyRule:
    return PolicyRule(
        id=policy.id,
        mode=policy.mode,
        threshold=policy.threshold,
        min_evidence_for_auto_apply=policy.min_evidence_for_auto_apply,
        require_override_reason=policy.require_override_reason,
        require_distinct_approver=policy.require_distinct_approver,
    )


def _strictest_first(policy: AdvisorPolicy) -> tuple[bool, float, int, bool, bool, str]:
    return (
        policy.mode != "always_require_approval",
        -policy.threshold,
        -policy.min_evidence_for_auto_apply,
        not policy.require_override_reason,
        not policy.require_distinct_approver,
        policy.id,
    )


def resolve_policy(
    policies: Iterable[AdvisorPolicy],
    plan_id: str,
    provider_id: str,
    decision_type: DecisionType,
    settings: Settings,
) -> PolicyRule:
    """The policy in force for a decision of ``decision_type`` in the plan."""
    applicable = [p for p in policies if not p.decision_types or decision_type in p.decision_types]
    levels = (
        [p for p in applicable if plan_id in p.scope.plan_ids],
        [p for p in applicable if provider_id in p.scope.provider_ids],
        [p for p in applicable if not p.scope.plan_ids and not p.scope.provider_ids],
    )
    for level in levels:
        if level:
            return rule_of(min(level, key=_strictest_first))
    return default_policy(settings)
