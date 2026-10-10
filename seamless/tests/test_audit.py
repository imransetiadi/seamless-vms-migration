"""Audit trail and input snapshots (SDD §14.5, ADV-05)."""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from seamless_migrate.ai.snapshots import canonical_json, snapshot_of, store_snapshot
from seamless_migrate.api.app import create_app
from seamless_migrate.domain.models import AdvisorSnapshot, Event
from seamless_migrate.events import AUDIT_KINDS, PERSISTED_KINDS
from seamless_migrate.store import AsyncStore, Store
from tests.api_support import Api, api_settings

T0 = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
#: An advisor input with synthetic secrets in it (none is real).
INPUT = {
    "tool": "jev_decide",
    "arguments": {
        "decision": "Which migration strategy should be used for VM web-01?",
        "evidence": "disk vdb volume_type=ceph-ssd; password=FakePass-Alpha1",  # gitleaks:allow
        "candidates": [{"id": "cold", "description": "Stop, copy, boot"}],
        "escalate_on_contradiction": True,
    },
    "scope": ["cold", "warm"],
    "deterministic": "cold",
}


def sha256_of(payload) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def test_the_audit_kinds_are_the_persisted_kinds_sdd_lists():
    assert AUDIT_KINDS == {
        "recommendation.created",
        "decision.auto_applied",
        "decision.pending_approval",
        "decision.approved",
        "decision.overridden",
        "decision.below_threshold",
        "decision.fallback",
    }
    assert AUDIT_KINDS <= PERSISTED_KINDS


def test_a_snapshot_is_redacted_then_named_by_the_hash_of_its_content():
    snapshot = snapshot_of(INPUT, now=T0)
    assert "FakePass" not in canonical_json(snapshot.payload)
    assert snapshot.payload["arguments"]["evidence"].endswith("password=[REDACTED]")
    assert snapshot.sha256 == sha256_of(snapshot.payload)
    assert len(snapshot.sha256) == 64
    # canonical: key order and spacing do not change the hash
    reordered = dict(reversed(list(json.loads(json.dumps(INPUT)).items())))
    assert list(reordered) != list(INPUT)
    assert snapshot_of(reordered, now=T0).sha256 == snapshot.sha256


async def test_a_snapshot_is_written_once(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'snap.db'}")
    store.create_schema()
    db = AsyncStore(store)
    first = await store_snapshot(db, INPUT, now=T0)
    again = await store_snapshot(db, INPUT, now=datetime(2026, 10, 11, tzinfo=UTC))
    assert first == again
    stored, version = store.get_versioned("advisor_snapshot", first, AdvisorSnapshot)
    assert (version, stored.created_at) == (1, T0)
    store.dispose()


@pytest.fixture
def api(tmp_path):
    settings, tokens = api_settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'audit.db'}")
    store.create_schema()
    store.put("advisor_snapshot", snapshot_of(INPUT, now=T0))
    for kind in ("migration.phase", "recommendation.created", "plan.updated", "decision.fallback"):
        store.append_event(Event(kind=kind, actor="system:advisor", data={"target_id": "rec-1"}))
    with TestClient(create_app(settings, store)) as client:
        yield Api(client, store, settings, tokens)
    store.dispose()


def test_anyone_can_recompute_a_snapshot_hash_from_the_api(api):
    sha = snapshot_of(INPUT, now=T0).sha256
    body = api.get(f"/api/v1/advisor/snapshots/{sha}").json()
    assert body["sha256"] == sha
    assert sha256_of(body["payload"]) == sha
    assert api.get(f"/api/v1/advisor/snapshots/{'0' * 64}").status_code == 404
    assert api.get("/api/v1/advisor/snapshots/not-a-hash").status_code == 422
    assert api.get(f"/api/v1/advisor/snapshots/{sha.upper()}").status_code == 422
    assert api.get(f"/api/v1/advisor/snapshots/{sha}", role=None).status_code == 401


def test_the_audit_filter_returns_only_audit_events(api):
    everything = api.get("/api/v1/events").json()
    audit = api.get("/api/v1/events", params={"audit": "true"}).json()
    assert len(everything) > len(audit)
    assert [e["kind"] for e in audit] == ["recommendation.created", "decision.fallback"]
    tail = api.get("/api/v1/events", params={"audit": "true", "tail": "true", "limit": 1}).json()
    assert [e["kind"] for e in tail] == ["decision.fallback"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/api/v1/events", "/api/v1/events/1", "/api/v1/events/stream"])
def test_no_route_changes_or_deletes_an_event(api, method, path):
    """ADV-05: the audit trail is append-only through the API."""
    before = api.kinds()
    response = api.client.request(method, path, headers=api.h("admin"))
    assert response.status_code in (404, 405), response.text
    assert api.kinds() == before


def test_the_api_declares_only_reads_on_the_events_paths(api):
    paths = api.client.get("/api/openapi.json").json()["paths"]
    events = {path: set(ops) for path, ops in paths.items() if path.startswith("/api/v1/events")}
    assert events and all(ops == {"get"} for ops in events.values()), events
