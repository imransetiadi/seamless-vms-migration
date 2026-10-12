"""decide(): the only place an advisor decision is made (SDD §14.4, ADV-01)."""

from datetime import UTC, datetime

import pytest

from seamless_migrate.ai.decision import (
    GLOBAL_DEFAULT_POLICY_ID,
    Advice,
    Approve,
    Override,
    PolicyRule,
    decide,
    default_policy,
)
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import DecisionStatus, ReasonCode

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
AUTO = PolicyRule(id="p-auto", mode="auto_apply_above_threshold", threshold=0.6)
STRICT = PolicyRule(id="p-finance", mode="always_require_approval", threshold=0.6)


def advice(option="warm", confidence=0.7, **kw):
    data = {
        "option": option,
        "confidence": confidence,
        "distribution": {"cold": 0.2, "warm": 0.75, "none": 0.05},
        "scope": ("cold", "warm"),
        "options": ("cold", "warm", "storage_handover"),
    }
    data.update(kw)
    return Advice(**data)


def test_above_the_threshold_with_enough_evidence_it_is_applied_by_the_policy():
    d = decide(advice(confidence=0.7), AUTO, 0, "cold", now=NOW)
    assert (d.status, d.reason_code, d.final_option) == (
        DecisionStatus.auto_applied,
        ReasonCode.ABOVE_THRESHOLD_AUTO_APPLY,
        "warm",
    )
    assert (d.actor.kind, d.actor.id, d.policy_id, d.threshold_used) == (
        "policy",
        "p-auto",
        "p-auto",
        0.6,
    )
    assert d.decided_at == NOW


def test_below_the_threshold_the_default_is_kept():
    """The spec's example: Warm at 0.57 against 0.60 - the decision is Cold."""
    d = decide(advice(confidence=0.57), AUTO, 0, "cold", now=NOW)
    assert (d.status, d.reason_code, d.final_option, d.threshold_used) == (
        DecisionStatus.below_threshold,
        ReasonCode.BELOW_THRESHOLD_DEFAULT_KEPT,
        "cold",
        0.6,
    )


def test_a_policy_that_always_requires_approval_waits_whatever_the_confidence():
    d = decide(advice(confidence=0.99), STRICT, 9, "cold", now=NOW)
    assert (d.status, d.reason_code, d.final_option) == (
        DecisionStatus.pending_approval,
        ReasonCode.POLICY_REQUIRES_APPROVAL,
        None,
    )
    assert d.actor.id == "p-finance"


def test_too_little_evidence_waits_for_approval_instead_of_applying():
    policy = PolicyRule(
        id="p", mode="auto_apply_above_threshold", threshold=0.6, min_evidence_for_auto_apply=3
    )
    d = decide(advice(confidence=0.9), policy, 2, "cold", now=NOW)
    assert (d.status, d.reason_code, d.final_option) == (
        DecisionStatus.pending_approval,
        ReasonCode.INSUFFICIENT_EVIDENCE,
        None,
    )
    assert (
        decide(advice(confidence=0.9), policy, 3, "cold", now=NOW).status
        == DecisionStatus.auto_applied
    )


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"option": None, "confidence": None, "unavailable": "timed out after 20 s"}, "timed out"),
        ({"option": "ask_user", "escaped": True}, "ask_user"),
        ({"option": "investigate"}, "investigate"),
        ({"option": "none"}, "none"),
        ({"option": None}, "no option"),
        ({"status": "escalate"}, "escalate"),
        ({"status": "invalid_response"}, "invalid_response"),
        (
            {"option": "storage_handover", "confidence": 0.95},
            "storage_handover",
        ),  # outside the scope
    ],
)
def test_no_usable_recommendation_falls_back_to_the_rules(kw, why):
    d = decide(advice(**kw), AUTO, 0, "cold", now=NOW)
    assert (d.status, d.reason_code, d.final_option) == (
        DecisionStatus.fallback_rules,
        ReasonCode.ADVISOR_UNAVAILABLE_FALLBACK,
        "cold",
    )
    assert (d.actor.kind, d.actor.id) == ("system", "fallback")
    assert why in (d.reason_text or "")


def test_a_user_approves_the_recommended_option():
    d = decide(advice(confidence=0.99), STRICT, 0, "cold", action=Approve(user="alice"), now=NOW)
    assert (d.status, d.reason_code, d.final_option) == (
        DecisionStatus.approved,
        ReasonCode.APPROVED_BY_USER,
        "warm",
    )
    assert (d.actor.kind, d.actor.id) == ("user", "alice")


def test_a_user_overrides_with_another_eligible_option_and_a_reason():
    action = Override(user="bob", option="cold", reason="month-end freeze on this VM")
    d = decide(advice(), STRICT, 0, "cold", action=action, now=NOW)
    assert (d.status, d.reason_code, d.final_option, d.reason_text) == (
        DecisionStatus.overridden,
        ReasonCode.OVERRIDDEN_BY_USER,
        "cold",
        "month-end freeze on this VM",
    )
    assert (d.actor.kind, d.actor.id) == ("user", "bob")


def test_nothing_to_approve_or_an_ineligible_override_is_refused():
    with pytest.raises(ValueError, match="nothing to approve"):
        decide(
            advice(option="ask_user", escaped=True),
            STRICT,
            0,
            "cold",
            action=Approve(user="a"),
            now=NOW,
        )
    with pytest.raises(ValueError, match="not an eligible option"):
        decide(
            advice(), STRICT, 0, "cold", action=Override(user="a", option="vmware_cold"), now=NOW
        )


def test_every_reason_code_has_a_path():
    produced = {
        decide(advice(confidence=0.7), AUTO, 0, "cold", now=NOW).reason_code,
        decide(advice(confidence=0.5), AUTO, 0, "cold", now=NOW).reason_code,
        decide(advice(), STRICT, 0, "cold", now=NOW).reason_code,
        decide(
            advice(),
            PolicyRule(
                id="p",
                mode="auto_apply_above_threshold",
                threshold=0.6,
                min_evidence_for_auto_apply=1,
            ),
            0,
            "cold",
            now=NOW,
        ).reason_code,
        decide(advice(option=None, unavailable="off"), AUTO, 0, "cold", now=NOW).reason_code,
        decide(advice(), STRICT, 0, "cold", action=Approve(user="a"), now=NOW).reason_code,
        decide(
            advice(), STRICT, 0, "cold", action=Override(user="a", option="cold"), now=NOW
        ).reason_code,
    }
    assert produced == set(ReasonCode)


def test_the_global_default_policy_keeps_todays_behaviour():
    """Until stored policies exist: auto-apply above SEAMLESS_JEV_MIN_CONFIDENCE, no evidence
    needed."""
    policy = default_policy(Settings(jev_min_confidence=0.65))
    assert (policy.id, policy.mode, policy.threshold, policy.min_evidence_for_auto_apply) == (
        GLOBAL_DEFAULT_POLICY_ID,
        "auto_apply_above_threshold",
        0.65,
        0,
    )
    assert (
        decide(advice(confidence=0.64), policy, 0, "cold", now=NOW).status
        == DecisionStatus.below_threshold
    )
    assert (
        decide(advice(confidence=0.65), policy, 0, "cold", now=NOW).status
        == DecisionStatus.auto_applied
    )


def _old_applied(data: dict, confidence: float | None, threshold: float) -> bool:
    """The inline rule of the advisor before ADV-01 (9719b27, ai/advisor.py)."""
    selected, status = data.get("selected"), data.get("status")
    confident = isinstance(confidence, int | float) and float(confidence) >= threshold
    in_scope = isinstance(selected, str) and selected in data["scope"]
    return bool(
        in_scope
        and confident
        and not data.get("escaped")
        and status not in ("escalate", "invalid_response")
    )


#: Strategy notes as the advisor wrote them before ADV-01: (data, confidence).
OLD_NOTES = [
    ({"selected": "warm", "status": "ok"}, 0.65),
    ({"selected": "warm", "status": "ok"}, 0.57),
    ({"selected": "warm", "status": None}, 0.6),
    ({"selected": "ask_user", "status": "ok", "escaped": True}, 0.8),
    ({"selected": "warm", "status": "escalate"}, 0.9),
    ({"selected": "warm", "status": "invalid_response"}, 0.9),
    ({"selected": "storage_handover", "status": "ok"}, 0.9),
    ({"selected": None, "status": "ok"}, None),
    ({"selected": "warm", "status": "ok"}, None),
    ({"selected": "cold", "status": "ok"}, 0.99),
]


@pytest.mark.parametrize(("data", "confidence"), OLD_NOTES)
def test_decide_replays_the_decisions_of_old_strategy_notes(data, confidence):
    """ADV-03 regression: the inputs of notes written before decide() existed produce the same
    decisions now - the strategy the old rule ran, and auto-apply exactly when it applied."""
    note = {**data, "scope": ["cold", "warm"], "deterministic": "cold"}
    threshold = Settings().jev_min_confidence
    applied = _old_applied(note, confidence, threshold)
    replayed = decide(
        Advice(
            option=note["selected"],
            confidence=confidence,
            scope=tuple(note["scope"]),
            options=("cold", "warm", "storage_handover"),
            status=note["status"],
            escaped=bool(note.get("escaped")),
        ),
        default_policy(Settings()),
        0,
        note["deterministic"],
        now=NOW,
    )
    assert replayed.final_option == (note["selected"] if applied else note["deterministic"])
    assert (replayed.status == DecisionStatus.auto_applied) == applied
