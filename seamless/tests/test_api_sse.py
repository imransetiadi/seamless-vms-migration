"""SSE end-to-end against a real uvicorn server (TestClient buffers whole responses)."""

import asyncio
import json

import httpx
import pytest
import uvicorn

from seamless_migrate.api.app import create_app
from seamless_migrate.api.routes_events import format_sse
from seamless_migrate.domain.enums import Role
from seamless_migrate.domain.models import Event
from seamless_migrate.events import emit
from seamless_migrate.store import Store
from tests.api_support import api_settings


@pytest.fixture
async def server(tmp_path):
    settings, tokens = api_settings(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'sse.db'}")
    store.create_schema()
    app = create_app(settings, store, sse_heartbeat_s=0.2)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.01)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", tokens, app, store
    srv.should_exit = True
    await task
    store.dispose()


async def read_frames(response, count, timeout=5.0):
    frames: list[str] = []
    buffer = ""

    async def run():
        nonlocal buffer
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                frames.append(frame)
                if len(frames) >= count:
                    return

    await asyncio.wait_for(run(), timeout)
    return frames


def parse(frame: str) -> dict:
    fields = {}
    for line in frame.splitlines():
        key, _, value = line.partition(": ")
        fields[key] = value
    return fields


def test_format_sse():
    ev = Event(seq=7, kind="plan.created", message="m")
    frame = format_sse(ev)
    assert frame.startswith("id: 7\nevent: plan.created\ndata: {") and frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1])["seq"] == 7
    assert format_sse(Event(seq=0, kind="migration.progress")).startswith("id: 0\n")


async def test_sse_stream_resume_and_heartbeat(server):
    base, tokens, app, store = server
    bus = app.state.services.bus
    headers = {"Authorization": f"Bearer {tokens[Role.viewer]}"}
    first = await emit(store, bus, "plan.created", "one", plan_id="p")
    second = await emit(store, bus, "plan.updated", "two", plan_id="p")

    async with httpx.AsyncClient(timeout=10) as client:
        # resume after the first event: the second is replayed, then live events follow
        async with client.stream(
            "GET", f"{base}/api/v1/events/stream?since={first.seq}", headers=headers
        ) as res:
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("text/event-stream")
            frames_task = asyncio.ensure_future(read_frames(res, 4))
            await asyncio.sleep(0.1)
            live = await emit(store, bus, "plan.updated", "three", plan_id="p")
            await emit(store, bus, "migration.progress", "50%", migration_id="m", persist=False)
            frames = await frames_task
        events = [parse(f) for f in frames if not f.startswith(":")]
        assert [e["id"] for e in events][:3] == [str(second.seq), str(live.seq), "0"]
        assert [e["event"] for e in events][:3] == [
            "plan.updated",
            "plan.updated",
            "migration.progress",
        ]
        assert json.loads(events[0]["data"])["message"] == "two"

        # no since: live only, and a heartbeat comment arrives while idle
        async with client.stream("GET", f"{base}/api/v1/events/stream", headers=headers) as res:
            frames = await read_frames(res, 1)
            assert frames == [": heartbeat"]

        denied = await client.get(f"{base}/api/v1/events/stream")
        assert denied.status_code == 401


async def test_event_stream_is_never_compressed(server):
    """SDD §12: a client that accepts gzip gets compressed responses, but never a compressed event
    stream — a compressor would hold live events back until its buffer fills."""
    base, tokens, app, store = server
    bus = app.state.services.bus
    headers = {"Authorization": f"Bearer {tokens[Role.viewer]}", "Accept-Encoding": "gzip"}
    async with httpx.AsyncClient(timeout=10) as client:
        async with client.stream("GET", f"{base}/api/v1/events/stream", headers=headers) as res:
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("text/event-stream")
            assert "content-encoding" not in res.headers
            frames_task = asyncio.ensure_future(read_frames(res, 1))
            await asyncio.sleep(0.1)
            live = await emit(store, bus, "plan.updated", "live", plan_id="p")
            frames = await asyncio.wait_for(frames_task, timeout=3)
    assert parse(frames[0])["id"] == str(live.seq)
