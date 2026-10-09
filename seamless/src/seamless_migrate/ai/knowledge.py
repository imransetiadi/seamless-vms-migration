"""Operational memory: lessons and similar-incident recall via agentmemory (SDD §14.3).

All memory failures are logged and swallowed — they never fail a migration. Guest console output
is never sent to agentmemory.
"""

from __future__ import annotations

import logging

from ..domain.models import AdvisorNote, Migration
from ..events import EventBus, emit
from ..store import Store
from .memory import MemoryClient, redact

log = logging.getLogger(__name__)
GIB = 2**30
MAX_HITS = 3
#: Longest memory hit content copied into a note/event (hits are shown, never prompted).
HIT_CONTENT_CHARS = 1000


def size_bucket(disk_bytes: int) -> str:
    gib = disk_bytes / GIB
    if gib < 50:
        return "<50G"
    if gib < 200:
        return "50-200G"
    if gib <= 500:
        return "200-500G"
    return ">500G"


class KnowledgeService:
    def __init__(self, memory: MemoryClient | None, store: Store | None, bus: EventBus) -> None:
        self.memory = memory
        self.store = store
        self.bus = bus

    async def _remember(
        self, migration: Migration, content: str, type_: str, concepts: list[str]
    ) -> None:
        if self.memory is None:
            return
        try:
            saved = await self.memory.remember(content, type_, concepts, names=[migration.vm.name])
            if saved:
                await emit(
                    self.store,
                    self.bus,
                    "memory.lesson_saved",
                    f"Saved a {type_} lesson for {migration.vm.name}",
                    plan_id=migration.plan_id,
                    migration_id=migration.id,
                    data={"type": type_, "concepts": concepts},
                )
        except Exception:
            log.warning("saving a lesson failed", exc_info=True)

    async def on_failure(
        self, migration: Migration, step: str, error: BaseException
    ) -> AdvisorNote | None:
        """Look up similar incidents (attached as a note) and record the failure as a lesson."""
        if self.memory is None:
            return None
        strategy = str(migration.strategy)
        error_class = type(error).__name__
        message = redact(str(error))
        note: AdvisorNote | None = None
        try:
            query = f"{strategy} {step} {error_class}: {message[:200]}"
            hits = await self.memory.search(query, limit=MAX_HITS, names=[migration.vm.name])
            if hits:
                note = AdvisorNote(
                    kind="similar_incidents",
                    source="memory",
                    summary=f"{len(hits[:MAX_HITS])} similar past incident(s) found",
                    data={
                        "query": query,
                        "hits": [
                            {
                                "title": h.title[:200],
                                "content": h.content[:HIT_CONTENT_CHARS],
                                "score": h.score,
                            }
                            for h in hits[:MAX_HITS]
                        ],
                    },
                )
                await emit(
                    self.store,
                    self.bus,
                    "advisor.similar_incidents",
                    note.summary,
                    plan_id=migration.plan_id,
                    migration_id=migration.id,
                    data=note.data,
                )
        except Exception:
            log.warning("similar-incident lookup failed", exc_info=True)
            note = None
        bucket = size_bucket(migration.vm.disk_bytes)
        await self._remember(
            migration,
            f"Seamless migration failure: strategy {strategy}, step {step}, guest "
            f"{migration.vm.os_type or 'unknown'}, disks {bucket}, attempt "
            f"{migration.attempts + 1}: {error_class}: {message[:500]}",
            "bug",
            ["seamless", strategy, step],
        )
        return note

    async def on_completed(self, migration: Migration) -> None:
        est = migration.estimate
        # the last number counts every pass, also those dropped from the list (SDD §5.4)
        passes = migration.sync_passes[-1].number if migration.sync_passes else 0
        final_delta = migration.sync_passes[-1].bytes_changed if migration.sync_passes else 0
        estimated = f"{est.downtime_s:.0f} s" if est is not None else "unknown"
        actual = (
            f"{migration.actual_downtime_s:.0f} s"
            if migration.actual_downtime_s is not None
            else "unknown"
        )
        bucket = size_bucket(migration.vm.disk_bytes)
        await self._remember(
            migration,
            f"Completed migration profile: guest {migration.vm.os_type or 'unknown'}, disks "
            f"{bucket}, strategy {migration.strategy}; {passes} sync pass(es), final delta "
            f"{final_delta} bytes; estimated downtime {estimated}, actual downtime {actual}.",
            "fact",
            ["seamless", str(migration.strategy), "completed", bucket],
        )

    async def on_rolled_back(self, migration: Migration, reason: str) -> None:
        actual = (
            f"{migration.actual_downtime_s:.0f} s"
            if migration.actual_downtime_s is not None
            else "unknown"
        )
        await self._remember(
            migration,
            f"Rolled back a {migration.strategy} migration (guest "
            f"{migration.vm.os_type or 'unknown'}, disks {size_bucket(migration.vm.disk_bytes)}) "
            f"after: {redact(reason)[:300]}; downtime {actual}; attempts {migration.attempts}.",
            "workflow",
            ["seamless", str(migration.strategy), "rollback"],
        )
