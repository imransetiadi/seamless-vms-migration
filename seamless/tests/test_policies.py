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


#: what a decision writes besides its documents (SDD §14.5, §14.6)
DECISION_EVENTS = ("plan.updated", "decision.approved", "decision.overridden", "migration.action")


def _events_of(api, *kinds):
    return [e.kind for e in api.store.events() if e.kind in kinds]


@pytest.mark.parametrize(
    ("write", "row"),
    [
        ("UPDATE documents", "migration"),
        ("UPDATE documents", "recommendation"),
        ("INSERT INTO events", "decision.approved"),
        ("INSERT INTO events", "migration.action"),
    ],
)
def test_an_approval_a_failed_write_cuts_short_leaves_nothing_and_the_retry_makes_it(
    api, refuse_write, write, row
):
    """SDD §14.6 (reviews ST-07, RV48-01, RV48-02, RV48-13): an approval's writes came one after
    another - a failure after the migration's left it released, which a running plan's tick
    starts on a strategy no decision recorded, and one after the decision's left the approval
    unaudited for good, every retry refused as "already approved". They are one transaction: a
    write refused leaves none of them, and the approver's retry makes the whole approval, with
    one event of each kind."""
    mid, rid = _held_for_a_decision(api)
    url = f"/api/v1/advisor/recommendations/{rid}/approve"
    refused = refuse_write(api.store, write, row)
    with pytest.raises(RuntimeError, match="briefly unavailable"):
        api.post(url, json={})
    assert refused == [f"{write} {row}"]
    rec = api.store.get("recommendation", rid, AdvisorRecommendation)
    m = api.store.get("migration", mid, Migration)
    assert (rec.decision.status, m.strategy, m.advisor_hold, _events_of(api, *DECISION_EVENTS)) == (
        DecisionStatus.pending_approval,
        "cold",
        rid,
        [],
    )
    again = api.post(url, json={})
    assert again.status_code == 200, again.text
    m = api.store.get("migration", mid, Migration)
    assert (m.strategy, m.advisor_hold, _events_of(api, *DECISION_EVENTS)) == (
        "warm",
        None,
        ["decision.approved", "migration.action"],
    )


@pytest.mark.parametrize(
    ("write", "row"),
    [
        ("UPDATE documents", "plan"),
        ("UPDATE documents", "migration"),
        ("UPDATE documents", "recommendation"),
        ("INSERT INTO events", "plan.updated"),
        ("INSERT INTO events", "decision.overridden"),
    ],
)
def test_an_override_a_failed_write_cuts_short_leaves_nothing_and_the_retry_makes_it(
    api, refuse_write, write, row
):
    """SDD §14.6 (reviews ST-07, RV48-03): an override's plan entry, its migration, its decision
    and their events are one transaction - a write refused leaves none of them, so no plan pins
    an option no decision records -, and the person's retry makes the whole override, with one
    event of each kind."""
    mid, rid = _held_for_a_decision(api)
    url = f"/api/v1/advisor/recommendations/{rid}/override"
    body = {"option": "cold", "reason": "Rehearsed the cold path twice"}
    refused = refuse_write(api.store, write, row)
    with pytest.raises(RuntimeError, match="briefly unavailable"):
        api.post(url, json=body)
    assert refused == [f"{write} {row}"]
    pinned = api.store.get("plan", "plan-1", Plan).strategy_overrides
    rec = api.store.get("recommendation", rid, AdvisorRecommendation)
    m = api.store.get("migration", mid, Migration)
    assert (pinned, rec.decision.status, m.advisor_hold, _events_of(api, *DECISION_EVENTS)) == (
        {},
        DecisionStatus.pending_approval,
        rid,
        [],
    )
    again = api.post(url, json=body)
    assert again.status_code == 200, again.text
    pinned = api.store.get("plan", "plan-1", Plan).strategy_overrides
    m = api.store.get("migration", mid, Migration)
    assert (pinned, m.strategy, m.advisor_hold, _events_of(api, *DECISION_EVENTS)) == (
        {f"src-{mid}": "cold"},
        "cold",
        None,
        ["plan.updated", "decision.overridden"],
    )


def test_an_override_already_made_is_not_made_again(api):
    """SDD §14.6 (review RV48-03): an override choosing the option the plan sets was let through as
    the retry of one cut off after its plan entry - and so an override already made was made
    again, a second decision.overridden rewriting who decided. No failure leaves a plan entry
    without its decision now: a second override is refused as every decision is while the plan
    sets the VM's strategy."""
    mid, rid = _held_for_a_decision(api)
    url = f"/api/v1/advisor/recommendations/{rid}/override"
    body = {"option": "cold", "reason": "Rehearsed the cold path twice"}
    assert api.post(url, json=body).status_code == 200
    again = api.post(url, json=body)
    assert (again.status_code, again.json()["error"]["message"]) == (
        409,
        "The plan sets the strategy of web-01 (cold): change it in the plan.",
    )
    rec = api.store.get("recommendation", rid, AdvisorRecommendation)
    assert (rec.decision.actor.id, _events_of(api, "decision.overridden")) == (
        "admin-user",
        ["decision.overridden"],
    )


@pytest.mark.parametrize("action", ["approve", "override"])
def test_no_decision_while_the_plan_sets_the_option_the_advisor_recommends(api, action):
    """SDD §14.6 (reviews A4, RV48-11): a person set the strategy in the plan, which a validation
    honours without a recommendation - the newest one no longer stands, whichever option the plan
    sets: an approve of the option it pins is refused, as an override to it."""
    mid, rid = _held_for_a_decision(api)
    plan = api.store.get("plan", "plan-1", Plan)
    plan.strategy_overrides[f"src-{mid}"] = "warm"  # what the advisor recommends
    api.store.put("plan", plan)
    body = {} if action == "approve" else {"option": "warm", "reason": "Rehearsed the warm path"}
    res = api.post(f"/api/v1/advisor/recommendations/{rid}/{action}", json=body)
    assert (res.status_code, res.json()["error"]["message"]) == (
        409,
        "The plan sets the strategy of web-01 (warm): change it in the plan.",
    )
    assert _events_of(api, *DECISION_EVENTS) == []


def test_a_decision_another_writer_moved_the_plan_under_is_made_again_keeping_both(api, land_first):
    """SDD §8, §14.6: plans are written by several drivers and the CLI. A plan moved between a
    decision's read and its transaction fails the versioned write, which leaves nothing; the
    decision is read again and made again - the other writer's change kept, one event of each
    kind."""
    mid, rid = _held_for_a_decision(api)

    def rename():
        plan, version = api.store.get_versioned("plan", "plan-1", Plan)
        renamed = plan.model_copy(update={"name": "renamed meanwhile"})
        return api.store.put("plan", renamed, expected_version=version)

    landed = land_first(api.store, "UPDATE documents", "plan", rename)
    body = {"option": "cold", "reason": "Rehearsed the cold path twice"}
    res = api.post(f"/api/v1/advisor/recommendations/{rid}/override", json=body)
    assert res.status_code == 200, res.text
    plan = api.store.get("plan", "plan-1", Plan)
    assert (landed, plan.name, plan.strategy_overrides) == (
        [2],
        "renamed meanwhile",
        {f"src-{mid}": "cold"},
    )
    assert _events_of(api, *DECISION_EVENTS) == ["plan.updated", "decision.overridden"]


def test_a_policy_replaced_during_its_delete_is_not_deleted_unseen(api, land_first):
    """SDD §12, §14.5 (review ST-05): DELETE /advisor/policies/{id} deletes the version it read: a
    replace landing between its read and its delete answers 409 and the replaced policy stays.
    Deleted by key, it was removed unseen and policy.deleted named the older version as before."""
    created = api.post("/api/v1/advisor/policies", json=SPEC)
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    stored, version = api.store.get_versioned("advisor_policy", pid, AdvisorPolicy)
    replaced = stored.model_copy(update={"threshold": 0.95})
    land_first(
        api.store,
        "DELETE FROM documents",
        "advisor_policy",
        lambda: api.store.put("advisor_policy", replaced, expected_version=version),
    )
    url = f"/api/v1/advisor/policies/{pid}"
    res = api.client.delete(url, headers=api.h(Role.admin))
    assert res.status_code == 409, res.text
    assert res.json()["error"]["message"] == f"policy {pid} changed meanwhile; try again"
    assert api.store.get("advisor_policy", pid, AdvisorPolicy).threshold == 0.95
    assert api.store.events(kinds=["policy.deleted"]) == []
    # the admin deletes what is there now, and the audit event names it
    assert api.client.delete(url, headers=api.h(Role.admin)).status_code == 204
    [deleted] = api.store.events(kinds=["policy.deleted"])
    assert deleted.data["before"]["threshold"] == 0.95


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
