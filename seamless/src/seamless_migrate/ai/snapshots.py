"""Audit trail and input snapshots (SDD §14.5, ADV-05).

An advisor input passes the redaction layer and is stored once, named by the SHA-256 of its
canonical JSON, so anyone can recompute the hash. Each recommendation is announced by
``recommendation.created`` and each status its decision takes by exactly one ``decision.<status>``
event in the append-only events table.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from ..domain.enums import DecisionStatus
from ..domain.models import AdvisorRecommendation, AdvisorSnapshot, Decision
from ..store import AsyncStore, ConflictError, NotFound
from .redaction import redact_payload

DECISION_EVENTS: dict[DecisionStatus, str] = {
    DecisionStatus.auto_applied: "decision.auto_applied",
    DecisionStatus.pending_approval: "decision.pending_approval",
    DecisionStatus.approved: "decision.approved",
    DecisionStatus.overridden: "decision.overridden",
    DecisionStatus.below_threshold: "decision.below_threshold",
    DecisionStatus.fallback_rules: "decision.fallback",
}


def canonical_json(payload: Any) -> str:
    """The form the snapshot hash is taken over (SDD §14.5)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_of(payload: Any, *, now: datetime) -> AdvisorSnapshot:
    """``payload`` redacted and named by the SHA-256 of its canonical JSON."""
    redacted = json.loads(canonical_json(redact_payload(payload)))
    digest = hashlib.sha256(canonical_json(redacted).encode("utf-8")).hexdigest()
    return AdvisorSnapshot(sha256=digest, created_at=now, payload=redacted)


async def store_snapshot(db: AsyncStore, payload: Any, *, now: datetime) -> str:
    """Store the snapshot of ``payload`` unless it is stored already; return its SHA-256."""
    snapshot = snapshot_of(payload, now=now)
    try:
        await db.get("advisor_snapshot", snapshot.sha256, AdvisorSnapshot)
    except NotFound:
        try:
            await db.put("advisor_snapshot", snapshot, expected_version=0)
        except ConflictError:
            pass  # stored meanwhile: the same content under the same name
    return snapshot.sha256


def _state(decision: Decision) -> dict[str, Any]:
    return {
        "status": str(decision.status),
        "final_option": decision.final_option,
        "reason_code": str(decision.reason_code),
        "policy_id": decision.policy_id,
        "threshold_used": decision.threshold_used,
    }


def created_event(rec: AdvisorRecommendation) -> tuple[str, str, str, dict[str, Any]]:
    """``(kind, message, actor, data)`` of ``recommendation.created``."""
    option, confidence = rec.recommendation.option, rec.recommendation.confidence
    shown = f"{confidence:.0%}" if confidence is not None else "no confidence"
    return (
        "recommendation.created",
        f"{rec.subject.name}: recommended {option or 'nothing'} ({shown})",
        "system:advisor",
        {
            "target_id": rec.id,
            "model_version": rec.model.version,
            "decision_type": str(rec.decision_type),
            "subject": rec.subject.model_dump(mode="json"),
            "option": option,
            "confidence": confidence,
            "snapshot_sha256": rec.input_snapshot_sha256,
        },
    )


def decision_event(
    rec: AdvisorRecommendation, before: Decision | None = None
) -> tuple[str, str, str, dict[str, Any]]:
    """``(kind, message, actor, data)`` of the event for the status ``rec.decision`` took."""
    decision = rec.decision
    actor = f"{decision.actor.kind}:{decision.actor.id}"
    return (
        DECISION_EVENTS[decision.status],
        f"{rec.subject.name}: decision {decision.final_option or 'pending'} "
        f"({decision.status}) by {actor}",
        actor,
        {
            "target_id": rec.id,
            "model_version": rec.model.version,
            "before": _state(before) if before is not None else None,
            "after": _state(decision),
        },
    )
