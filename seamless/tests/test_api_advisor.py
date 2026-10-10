"""GET /advisor/recommendations: stored and legacy recommendations (SDD §12, §14.4, ADV-01)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.domain.enums import DecisionStatus, ReasonCode, Role
from seamless_migrate.domain.models import (
    AdvisorActor,
    AdvisorModelInfo,
    AdvisorNote,
    AdvisorRecommendation,
    AdvisorSubject,
    Decision,
    Migration,
    RecommendedOption,
)
from seamless_migrate.store import Store
from tests.api_support import Api, api_settings
from tests.factories import make_migration, make_vm

T0 = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
LEGACY_TEXT = "Jev suggested warm (confidence 0.57, status ok); below the bar, kept cold"


def legacy_migration(mid: str, plan_id: str, name: str):
    return make_migration(
        id=mid,
        plan_id=plan_id,
        vm=make_vm(name=name, source_id=f"src-{mid}"),
        advisor_notes=[
            AdvisorNote(
                kind="strategy",
                source="jev",
                summary=LEGACY_TEXT,
                confidence=0.57,
                data={
                    "deterministic": "cold",
                    "selected": "warm",
                    "applied": False,
                    "probabilities": {"warm": 0.75, "cold": 0.2, "none": 0.05},
                },
                created_at=T0,
            ),
            # not a strategy decision: never listed as a recommendation
            AdvisorNote(kind="classification", source="rules", summary="tiered", created_at=T0),
        ],
    )


def stored(mid: str, plan_id: str, name: str, at: datetime, option="warm", confidence=0.9):
    return AdvisorRecommendation(
        created_at=at,
        decision_type="migration_strategy",
        subject=AdvisorSubject(kind="vm", id=f"src-{mid}", name=name),
        plan_id=plan_id,
        migration_id=mid,
        provider_id="src-osp",
        model=AdvisorModelInfo(name="jev", version="jev-1.13.0"),
        recommendation=RecommendedOption(
            option=option, confidence=confidence, distribution={option: confidence}
        ),
        decision=Decision(
            final_option=option,
            status=DecisionStatus.auto_applied,
            reason_code=ReasonCode.ABOVE_THRESHOLD_AUTO_APPLY,
            actor=AdvisorActor(kind="policy", id="global-default"),
            policy_id="global-default",
            threshold_used=0.6,
            decided_at=at,
        ),
    )


@pytest.fixture
def api(tmp_path):
    settings, tokens = api_settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'advisor.db'}")
    store.create_schema()
    store.put("migration", legacy_migration("mig-a", "plan-1", "web-02"))
    store.put("migration", legacy_migration("mig-b", "plan-2", "db-01"))
    store.put("recommendation", stored("mig-a", "plan-1", "web-02", T0 + timedelta(hours=2)))
    store.put(
        "recommendation", stored("mig-b", "plan-2", "db-01", T0 + timedelta(hours=1), "cold", 0.8)
    )
    with TestClient(create_app(settings, store)) as client:
        yield Api(client, store, settings, tokens)
    store.dispose()


def test_lists_stored_and_legacy_recommendations_newest_first(api):
    page = api.get("/api/v1/advisor/recommendations")
    assert page.status_code == 200, page.text
    body = page.json()
    assert (body["total"], body["offset"], body["limit"]) == (4, 0, 50)
    items = body["items"]
    assert [i["legacy"] for i in items] == [False, False, True, True]
    assert [i["subject"]["name"] for i in items[:2]] == ["web-02", "db-01"]
    legacy = next(i for i in items if i["legacy"] and i["migration_id"] == "mig-a")
    assert legacy["id"] == "rec-legacy-mig-a-0"
    assert legacy["legacy_text"] == LEGACY_TEXT
    assert legacy["decision_type"] == "migration_strategy"
    assert legacy["recommendation"] == {
        "option": "warm",
        "confidence": 0.57,
        "distribution": {"warm": 0.75, "cold": 0.2, "none": 0.05},
    }
    assert (legacy["decision"]["status"], legacy["decision"]["final_option"]) == (
        "below_threshold",
        "cold",
    )
    assert legacy["decision"]["actor"] == {"kind": "system", "id": "legacy"}


@pytest.mark.parametrize(
    ("source", "data", "status", "final"),
    [
        (
            "jev",
            {"selected": "warm", "applied": True, "deterministic": "cold"},
            "auto_applied",
            "warm",
        ),
        (
            "rules",
            {"selected": None, "applied": False, "deterministic": "cold"},
            "fallback_rules",
            "cold",
        ),
        (
            "jev",
            {"selected": "warm", "applied": False, "deterministic": "cold"},
            "below_threshold",
            "cold",
        ),
    ],
)
def test_a_legacy_note_is_read_as_the_decision_it_records(api, source, data, status, final):
    m = legacy_migration("mig-c", "plan-3", "app-03")
    m.advisor_notes = [
        AdvisorNote(kind="strategy", source=source, summary="old note", data=data, created_at=T0)
    ]
    api.store.put("migration", m)
    entry = api.get("/api/v1/advisor/recommendations/rec-legacy-mig-c-0").json()
    assert (entry["decision"]["status"], entry["decision"]["final_option"]) == (status, final)
    assert entry["model"]["name"] == source


def test_the_legacy_entries_match_the_strategy_notes_one_for_one(api):
    notes = sum(
        1
        for doc in api.store.list("migration", Migration)
        for n in doc.advisor_notes
        if n.kind == "strategy"
    )
    items = api.get("/api/v1/advisor/recommendations", params={"limit": 200}).json()["items"]
    assert sum(1 for i in items if i["legacy"]) == notes == 2


def test_filters_and_pages_the_recommendations(api):
    by_migration = api.get(
        "/api/v1/advisor/recommendations", params={"migration_id": "mig-a"}
    ).json()
    assert by_migration["total"] == 2 and {i["migration_id"] for i in by_migration["items"]} == {
        "mig-a"
    }
    by_plan = api.get("/api/v1/advisor/recommendations", params={"plan_id": "plan-2"}).json()
    assert by_plan["total"] == 2 and {i["plan_id"] for i in by_plan["items"]} == {"plan-2"}
    none = api.get(
        "/api/v1/advisor/recommendations", params={"decision_type": "workload_tier"}
    ).json()
    assert none["total"] == 0 and none["items"] == []
    page = api.get("/api/v1/advisor/recommendations", params={"limit": 1, "offset": 1}).json()
    assert (page["total"], page["offset"], page["limit"], len(page["items"])) == (4, 1, 1, 1)
    assert page["items"][0]["subject"]["name"] == "db-01"
    for bad in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"decision_type": "nonsense"}):
        assert api.get("/api/v1/advisor/recommendations", params=bad).status_code == 422, bad


def test_gets_a_recommendation_by_id_stored_or_legacy(api):
    first = api.get("/api/v1/advisor/recommendations").json()["items"][0]
    got = api.get(f"/api/v1/advisor/recommendations/{first['id']}")
    assert got.status_code == 200 and got.json() == first
    legacy = api.get("/api/v1/advisor/recommendations/rec-legacy-mig-b-0")
    assert legacy.status_code == 200 and legacy.json()["legacy"] is True
    assert api.get("/api/v1/advisor/recommendations/rec-nope").status_code == 404
    assert api.get("/api/v1/advisor/recommendations/rec-legacy-mig-b-7").status_code == 404


def test_recommendations_need_a_token_and_any_role(api):
    assert api.get("/api/v1/advisor/recommendations", role=None).status_code == 401
    assert api.get("/api/v1/advisor/recommendations", role=Role.viewer).status_code == 200
