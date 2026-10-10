"""The recommendations listing (SDD §12, §14.4).

Stored recommendations and the strategy notes of earlier versions, which are adapted into
``legacy`` entries when they are read - nothing is copied or rewritten, so the number of legacy
entries always equals the number of strategy notes.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..domain.enums import DecisionStatus, DecisionType, ReasonCode
from ..domain.models import (
    AdvisorActor,
    AdvisorModelInfo,
    AdvisorNote,
    AdvisorRecommendation,
    AdvisorSubject,
    Decision,
    Migration,
    RecommendedOption,
)

LEGACY_PREFIX = "rec-legacy-"


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _legacy(migration: Migration, index: int, note: AdvisorNote) -> AdvisorRecommendation:
    data = note.data
    option = _text(data.get("selected"))
    probabilities = data.get("probabilities")
    distribution = (
        {k: float(v) for k, v in probabilities.items() if isinstance(v, int | float)}
        if isinstance(probabilities, dict)
        else {}
    )
    if data.get("applied") is True and option is not None:
        status, code, final = (
            DecisionStatus.auto_applied,
            ReasonCode.ABOVE_THRESHOLD_AUTO_APPLY,
            option,
        )
    elif note.source == "rules":
        status, code, final = (
            DecisionStatus.fallback_rules,
            ReasonCode.ADVISOR_UNAVAILABLE_FALLBACK,
            _text(data.get("deterministic")),
        )
    else:
        status, code, final = (
            DecisionStatus.below_threshold,
            ReasonCode.BELOW_THRESHOLD_DEFAULT_KEPT,
            _text(data.get("deterministic")),
        )
    return AdvisorRecommendation(
        id=f"{LEGACY_PREFIX}{migration.id}-{index}",
        created_at=note.created_at,
        decision_type=DecisionType.migration_strategy,
        subject=AdvisorSubject(kind="vm", id=migration.vm.source_id, name=migration.vm.name),
        plan_id=migration.plan_id,
        migration_id=migration.id,
        model=AdvisorModelInfo(
            name="jev" if note.source == "jev" else "rules",
            version="unknown" if note.source == "jev" else "rules",
        ),
        recommendation=RecommendedOption(
            option=option, confidence=note.confidence, distribution=distribution
        ),
        decision=Decision(
            final_option=final,
            status=status,
            reason_code=code,
            actor=AdvisorActor(kind="system", id="legacy"),
            decided_at=note.created_at,
        ),
        legacy=True,
        legacy_text=note.summary,
    )


def legacy_recommendations(migration: Migration) -> list[AdvisorRecommendation]:
    """The migration's strategy notes as legacy recommendations, in their order."""
    notes = [n for n in migration.advisor_notes if n.kind == "strategy"]
    return [_legacy(migration, index, note) for index, note in enumerate(notes)]


def legacy_by_id(rec_id: str, migration: Migration) -> AdvisorRecommendation | None:
    """The legacy entry ``rec-legacy-<migration id>-<index>`` of ``migration``, if it exists."""
    _, _, index = rec_id.removeprefix(LEGACY_PREFIX).rpartition("-")
    entries = legacy_recommendations(migration)
    if not index.isdigit() or int(index) >= len(entries):
        return None
    return entries[int(index)]


def listing(
    stored: Iterable[AdvisorRecommendation],
    migrations: Iterable[Migration],
    *,
    migration_id: str | None = None,
    plan_id: str | None = None,
    decision_type: DecisionType | None = None,
) -> list[AdvisorRecommendation]:
    """Stored and legacy recommendations matching the filters, newest first."""
    legacy = [r for m in migrations for r in legacy_recommendations(m)]
    items = [
        r
        for r in [*stored, *legacy]
        if (migration_id is None or r.migration_id == migration_id)
        and (plan_id is None or r.plan_id == plan_id)
        and (decision_type is None or r.decision_type == decision_type)
    ]
    return sorted(items, key=lambda r: (r.created_at, r.id), reverse=True)
