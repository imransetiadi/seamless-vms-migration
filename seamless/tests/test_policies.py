"""Advisor policies, approval and override (SDD §13.2, §14.6, ADV-04)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from seamless_migrate.ai.policies import GLOBAL_DEFAULT, resolve_policy
from seamless_migrate.api.app import create_app
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import DecisionStatus, DecisionType, ReasonCode, Role
from seamless_migrate.domain.models import (
    AdvisorActor,
    AdvisorModelInfo,
    AdvisorPolicy,
    AdvisorRecommendation,
    AdvisorSubject,
    Decision,
    Estimate,
    Migration,
    Plan,
    PolicyScope,
    RecommendedOption,
)
from seamless_migrate.store import Store
from tests.api_support import Api, api_settings
from tests.factories import make_migration, make_plan, make_vm

T0 = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
STRATEGY = DecisionType.migration_strategy


def pol(pid, mode="auto_apply_above_threshold", threshold=0.6, *, providers=(), plans=(), **kw):
    return AdvisorPolicy(
        id=pid,
        name=pid,
        scope=PolicyScope(provider_ids=list(providers), plan_ids=list(plans)),
        mode=mode,
        threshold=threshold,
        updated_by="rina",
        **kw,
    )


def resolved(policies, plan_id="plan-1", provider_id="src-1", decision_type=STRATEGY):
    return resolve_policy(policies, plan_id, provider_id, decision_type, Settings())


def test_the_most_specific_level_wins():
    """Plan beats provider beats global beats the built-in default (SDD §14.6)."""
    everything = [
        pol("pol-global", threshold=0.5),
        pol("pol-provider", threshold=0.7, providers=["src-1"]),
        pol("pol-plan", threshold=0.8, plans=["plan-1"]),
    ]
    assert resolved(everything).id == "pol-plan"
    assert resolved(everything[:2]).id == "pol-provider"
    assert resolved(everything[:1]).id == "pol-global"
    # another plan, another provider: only the global one applies
    assert resolved(everything, plan_id="plan-2", provider_id="src-2").id == "pol-global"
    assert resolved([]).id == GLOBAL_DEFAULT


@pytest.mark.parametrize(
    ("policies", "winner"),
    [
        # the strictest mode wins a same-level conflict, whatever the threshold
        ([pol("pol-a", threshold=0.9), pol("pol-b", "always_require_approval", 0.1)], "pol-b"),
        # then the higher threshold
        ([pol("pol-a", threshold=0.7), pol("pol-b", threshold=0.8)], "pol-b"),
        # then the higher minimum evidence
        ([pol("pol-a", min_evidence_for_auto_apply=3), pol("pol-b")], "pol-a"),
        # then a required override reason
        ([pol("pol-a", require_override_reason=False), pol("pol-b")], "pol-b"),
        # then a required distinct approver (ADV-17)
        ([pol("pol-a", require_distinct_approver=False), pol("pol-b")], "pol-b"),
        # then the lower id
        ([pol("pol-b"), pol("pol-a")], "pol-a"),
    ],
)
def test_a_same_level_conflict_takes_the_strictest_policy(policies, winner):
    for p in policies:
        p.scope.provider_ids.append("src-1")
    assert resolved(policies).id == winner


def test_a_policy_for_other_decision_types_does_not_apply():
    tiers_only = pol("pol-tiers", "always_require_approval", providers=["src-1"])
    tiers_only.decision_types = [DecisionType.workload_tier]
    assert resolved([tiers_only]).id == GLOBAL_DEFAULT
    assert resolved([tiers_only], decision_type=DecisionType.workload_tier).id == "pol-tiers"


def test_without_a_stored_policy_the_built_in_default_keeps_todays_behaviour():
    rule = resolve_policy([], "plan-1", "src-1", STRATEGY, Settings(jev_min_confidence=0.65))
    assert (rule.id, rule.mode, rule.threshold, rule.min_evidence_for_auto_apply) == (
        GLOBAL_DEFAULT,
        "auto_apply_above_threshold",
        0.65,
        0,
    )
    assert rule.require_override_reason is False
    assert rule.require_distinct_approver is True  # ADV-17: on by default


# -- API ----------------------------------------------------------------------------------------


def rec_for(
    mid: str, *, status=DecisionStatus.pending_approval, legacy=False, requested_by=None
) -> AdvisorRecommendation:
    return AdvisorRecommendation(
        id=f"rec-{mid[-6:]}aa0001",
        created_at=T0,
        decision_type=STRATEGY,
        subject=AdvisorSubject(kind="vm", id=f"src-{mid}", name="web-01"),
        plan_id="plan-1",
        migration_id=mid,
        provider_id="src-osp",
        model=AdvisorModelInfo(name="jev", version="jev-1.13.0"),
        recommendation=RecommendedOption(option="warm", confidence=0.9, distribution={"warm": 0.9}),
        decision=Decision(
            final_option=None,
            status=status,
            reason_code=ReasonCode.POLICY_REQUIRES_APPROVAL,
            actor=AdvisorActor(kind="policy", id="pol-finance"),
            policy_id="pol-finance",
            threshold_used=0.6,
            decided_at=T0,
        ),
        legacy=legacy,
        requested_by=requested_by,
    )


def held_migration(mid: str):
    estimates = [
        Estimate(
            strategy=s,
            eligible=True,
            precopy_s=0,
            passes=0,
            downtime_s=100,
            total_s=100,
            final_delta_bytes=0,
            meets_slo=True,
        )
        for s in ("cold", "warm")
    ]
    return make_migration(
        id=mid,
        plan_id="plan-1",
        vm=make_vm(name="web-01", source_id=f"src-{mid}"),
        phase="ready",
        strategy="cold",
        estimates=estimates,
        advisor_hold=f"rec-{mid[-6:]}aa0001",
    )


@pytest.fixture
def api(tmp_path):
    settings, tokens = api_settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'policies.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        yield Api(client, store, settings, tokens)
    store.dispose()


SPEC = {
    "name": "Finance — approval required",
    "scope": {"provider_ids": [], "plan_ids": []},
    "decision_types": ["migration_strategy"],
    "mode": "always_require_approval",
    "threshold": 0.6,
    "min_evidence_for_auto_apply": 0,
    "require_override_reason": True,
}


def test_policies_are_listed_with_the_built_in_default(api):
    body = api.get("/api/v1/advisor/policies").json()
    assert body["items"] == []
    assert (body["default"]["id"], body["default"]["mode"], body["default"]["threshold"]) == (
        GLOBAL_DEFAULT,
        "auto_apply_above_threshold",
        api.settings.jev_min_confidence,
    )


def test_an_admin_creates_replaces_and_deletes_a_policy_and_each_change_is_audited(api):
    created = api.post("/api/v1/advisor/policies", json=SPEC)
    assert created.status_code == 201, created.text
    policy = created.json()
    assert policy["id"].startswith("pol-") and policy["updated_by"] == "admin-user"
    assert policy["require_distinct_approver"] is True  # the default (ADV-17)
    changed = api.client.put(
        f"/api/v1/advisor/policies/{policy['id']}",
        headers=api.h(Role.admin),
        json={**SPEC, "threshold": 0.75},
    )
    assert changed.status_code == 200 and changed.json()["threshold"] == 0.75
    assert (
        api.client.delete(
            f"/api/v1/advisor/policies/{policy['id']}", headers=api.h(Role.admin)
        ).status_code
        == 204
    )
    assert (
        api.client.delete(
            f"/api/v1/advisor/policies/{policy['id']}", headers=api.h(Role.admin)
        ).status_code
        == 404
    )
    audit = [e for e in api.store.events(since_seq=0, limit=1000) if e.kind.startswith("policy.")]
    assert [(e.kind, e.actor) for e in audit] == [
        ("policy.created", "user:admin-user"),
        ("policy.updated", "user:admin-user"),
        ("policy.deleted", "user:admin-user"),
    ]
    assert audit[0].data["before"] is None and audit[0].data["after"]["id"] == policy["id"]
    assert (audit[1].data["before"]["threshold"], audit[1].data["after"]["threshold"]) == (
        0.6,
        0.75,
    )
    assert audit[2].data["after"] is None and audit[2].data["target_id"] == policy["id"]


@pytest.mark.parametrize(
    "change",
    [
        {"scope": {"provider_ids": ["src-osp"], "plan_ids": ["plan-1"]}},
        {"scope": {"provider_ids": ["no-such-provider"], "plan_ids": []}},
        {"scope": {"provider_ids": [], "plan_ids": ["no-such-plan"]}},
        {"threshold": 1.5},
        {"name": ""},
        {"mode": "sometimes"},
    ],
)
def test_a_policy_the_api_cannot_resolve_is_refused_with_422(api, change):
    assert api.post("/api/v1/advisor/policies", json={**SPEC, **change}).status_code == 422


@pytest.mark.parametrize("role", [Role.viewer, Role.operator, Role.approver])
def test_only_advisor_policy_manage_changes_policies(api, role):
    assert api.post("/api/v1/advisor/policies", role=role, json=SPEC).status_code == 403


@pytest.mark.parametrize("role", [Role.viewer, Role.operator, Role.approver])
@pytest.mark.parametrize("action", ["approve", "override"])
def test_only_advisor_approve_approves_or_overrides(api, role, action):
    api.store.put("migration", held_migration("mig-0000000001"))
    api.store.put("recommendation", rec_for("mig-0000000001"))
    response = api.post(
        f"/api/v1/advisor/recommendations/rec-000001aa0001/{action}",
        role=role,
        json={"option": "cold", "reason": "Rehearsed the cold path twice"}
        if action == "override"
        else {},
    )
    assert response.status_code == 403


def test_an_override_without_its_required_reason_is_refused_by_the_api(api):
    """ADV-04: the API refuses it, not only the dashboard."""
    api.store.put("plan", make_plan(id="plan-1", source_provider_id="src-osp"))
    api.store.put(
        "advisor_policy", pol("pol-finance", "always_require_approval", providers=["src-osp"])
    )
    api.store.put("migration", held_migration("mig-0000000001"))
    api.store.put("recommendation", rec_for("mig-0000000001"))
    url = "/api/v1/advisor/recommendations/rec-000001aa0001/override"
    for reason in (None, "", "too short", " " * 12):
        response = api.post(url, json={"option": "cold", "reason": reason})
        assert response.status_code == 422, (reason, response.text)
    ok = api.post(url, json={"option": "cold", "reason": "Rehearsed the cold path twice"})
    assert ok.status_code == 200, ok.text
    assert (ok.json()["decision"]["status"], ok.json()["decision"]["final_option"]) == (
        "overridden",
        "cold",
    )


def _held_for_a_decision(api):
    api.store.put("plan", make_plan(id="plan-1", source_provider_id="src-osp"))
    api.store.put(
        "advisor_policy", pol("pol-finance", "always_require_approval", providers=["src-osp"])
    )
    api.store.put("migration", held_migration("mig-0000000001"))
    api.store.put("recommendation", rec_for("mig-0000000001"))
    return "mig-0000000001", "rec-000001aa0001"


def _decision_events(api):
    return [e.kind for e in api.store.events() if e.kind.startswith("decision.")]


@pytest.mark.parametrize("half", ["migration", "recommendation"])
def test_an_approval_cut_short_by_a_failed_write_is_completed_by_the_retry(api, refuse_write, half):
    """SDD §14.6 (review ST-07): an approval writes the migration - the approved strategy, the
    hold cleared - before the decision and its event. A failure of either write leaves no
    decision approved while its migration stays held, and the approver's retry approves it with
    one decision.approved event. The decision written first stayed approved, the migration held
    and every later Approve was refused as "already approved"."""
    mid, rid = _held_for_a_decision(api)
    url = f"/api/v1/advisor/recommendations/{rid}/approve"
    refused = refuse_write(api.store, "UPDATE documents", half)
    with pytest.raises(RuntimeError, match="briefly unavailable"):
        api.post(url, json={})
    assert refused == [f"UPDATE documents {half}"]
    rec = api.store.get("recommendation", rid, AdvisorRecommendation)
    m = api.store.get("migration", mid, Migration)
    assert rec.decision.status == DecisionStatus.pending_approval
    # the migration's write refused: it waits as it did; the decision's: it runs the approval
    assert (m.strategy, m.advisor_hold) == (
        ("cold", rid) if half == "migration" else ("warm", None)
    )
    again = api.post(url, json={})
    assert again.status_code == 200, again.text
    m = api.store.get("migration", mid, Migration)
    assert (m.strategy, m.advisor_hold, _decision_events(api)) == (
        "warm",
        None,
        ["decision.approved"],
    )


@pytest.mark.parametrize("half", ["plan", "migration", "recommendation"])
def test_an_override_cut_short_by_a_failed_write_is_completed_by_the_retry(api, refuse_write, half):
    """SDD §14.6 (review ST-07): an override writes its plan entry first, then the migration,
    then the decision and its event. Whichever write fails, the person's retry completes it - an
    override that chooses the option the plan already sets goes on - with one decision.overridden
    event."""
    mid, rid = _held_for_a_decision(api)
    url = f"/api/v1/advisor/recommendations/{rid}/override"
    body = {"option": "cold", "reason": "Rehearsed the cold path twice"}
    refused = refuse_write(api.store, "UPDATE documents", half)
    with pytest.raises(RuntimeError, match="briefly unavailable"):
        api.post(url, json=body)
    assert refused == [f"UPDATE documents {half}"]
    pinned = api.store.get("plan", "plan-1", Plan).strategy_overrides.get(f"src-{mid}")
    rec = api.store.get("recommendation", rid, AdvisorRecommendation)
    assert (pinned, rec.decision.status) == (
        None if half == "plan" else "cold",
        DecisionStatus.pending_approval,
    )
    again = api.post(url, json=body)
    assert again.status_code == 200, again.text
    m = api.store.get("migration", mid, Migration)
    assert (m.strategy, m.advisor_hold, _decision_events(api)) == (
        "cold",
        None,
        ["decision.overridden"],
    )


def test_a_legacy_entry_cannot_be_approved(api):
    response = api.post(
        "/api/v1/advisor/recommendations/rec-legacy-mig-0000000001-0/approve", json={}
    )
    assert response.status_code == 409


def test_the_requester_of_a_decision_is_refused_by_the_api(api):
    """ADV-17: the validating admin cannot approve or override their own decision (409)."""
    api.store.put("plan", make_plan(id="plan-1", source_provider_id="src-osp"))
    api.store.put(
        "advisor_policy", pol("pol-finance", "always_require_approval", providers=["src-osp"])
    )
    api.store.put("migration", held_migration("mig-0000000001"))
    api.store.put("recommendation", rec_for("mig-0000000001", requested_by="admin-user"))
    base = "/api/v1/advisor/recommendations/rec-000001aa0001"
    for action, body in (
        ("approve", {}),
        ("override", {"option": "cold", "reason": "Rehearsed the cold path twice"}),
    ):
        refused = api.post(f"{base}/{action}", json=body)
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["message"] == (
            "You requested this decision. Another approver must decide it."
        )
