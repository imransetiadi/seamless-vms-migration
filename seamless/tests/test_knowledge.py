import json
from datetime import UTC, datetime, timedelta

import httpx

from seamless_migrate.ai.knowledge import KnowledgeService, size_bucket
from seamless_migrate.ai.memory import MemoryClient
from seamless_migrate.domain.enums import Phase, Strategy
from seamless_migrate.domain.models import Estimate
from seamless_migrate.events import EventBus
from tests.factories import make_disk, make_migration, make_vm


class PermanentStepError(Exception):
    """Stand-in for the executor error class (only its name matters here)."""


def memory_with(results, seen, fail=False):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content or b"{}")))
        if fail:
            raise httpx.ConnectError("down", request=request)
        if request.url.path.endswith("/smart-search"):
            return httpx.Response(200, json={"results": results})
        return httpx.Response(201, json={"success": True})

    return MemoryClient("http://m:3111", None, "proj", transport=httpx.MockTransport(handler))


async def test_knowledge_on_failure_attaches_hits(store):
    seen = []
    hits = [
        {"title": f"incident {i}", "content": f"fix {i}", "score": 0.9 - i / 10} for i in range(5)
    ]
    bus = EventBus(store)
    service = KnowledgeService(memory_with(hits, seen), store, bus)
    mig = make_migration(strategy=Strategy.warm, phase=Phase.failed)
    error = PermanentStepError("dst conversion host unreachable: password=hunter2 " + "x" * 400)
    note = await service.on_failure(mig, "cutover", error)

    assert note.kind == "similar_incidents" and note.source == "memory"
    assert [h["title"] for h in note.data["hits"]] == ["incident 0", "incident 1", "incident 2"]
    search_path, search_body = seen[0]
    assert search_path == "/agentmemory/smart-search"
    assert search_body["query"].startswith("warm cutover PermanentStepError: dst conversion")
    assert "hunter2" not in search_body["query"]
    assert len(search_body["query"]) < 260
    remember_path, remember_body = seen[1]
    assert remember_path == "/agentmemory/remember"
    assert remember_body["type"] == "bug"
    assert remember_body["concepts"] == ["seamless", "warm", "cutover"]
    kinds = [e.kind for e in store.events(since_seq=0)]
    assert kinds == ["advisor.similar_incidents", "memory.lesson_saved"]


async def test_knowledge_on_completed_and_rolled_back(store):
    seen = []
    service = KnowledgeService(memory_with([], seen), store, EventBus(store))
    start = datetime(2026, 10, 8, 1, tzinfo=UTC)
    mig = make_migration(
        strategy=Strategy.warm,
        phase=Phase.completed,
        vm=make_vm(os_type="rhel9", disks=[make_disk(size_gb=300)]),
        estimate=Estimate(
            strategy=Strategy.warm,
            eligible=True,
            precopy_s=10,
            passes=2,
            downtime_s=420.0,
            total_s=430.0,
            final_delta_bytes=2**30,
            meets_slo=False,
        ),
        downtime_started_at=start,
        downtime_ended_at=start + timedelta(seconds=380),
        actual_downtime_s=380.0,
    )
    await service.on_completed(mig)
    _, body = seen[-1]
    assert body["type"] == "fact"
    for fragment in (
        "rhel9",
        "200-500G",
        "warm",
        "estimated downtime 420 s",
        "actual downtime 380 s",
    ):
        assert fragment in body["content"]
    await service.on_rolled_back(mig, "verification failed: tcp:22")
    _, body = seen[-1]
    assert body["type"] == "workflow" and "verification failed" in body["content"]
    assert [e.kind for e in store.events(since_seq=0)] == ["memory.lesson_saved"] * 2


async def test_knowledge_swallows_memory_failures(store):
    seen = []
    service = KnowledgeService(memory_with([], seen, fail=True), store, EventBus(store))
    assert await service.on_failure(make_migration(), "precopy", RuntimeError("x")) is None
    await service.on_completed(make_migration())
    await service.on_rolled_back(make_migration(), "r")
    assert store.events(since_seq=0) == []
    disabled = KnowledgeService(None, store, EventBus(store))
    assert await disabled.on_failure(make_migration(), "cutover", RuntimeError("x")) is None


def test_size_bucket():
    assert [size_bucket(g * 2**30) for g in (10, 50, 199, 200, 500, 501)] == [
        "<50G",
        "50-200G",
        "50-200G",
        "200-500G",
        "200-500G",
        ">500G",
    ]
