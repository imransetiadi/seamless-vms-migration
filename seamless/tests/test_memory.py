import json

import httpx
import pytest

from seamless_migrate.ai.memory import MemoryClient, MemoryHit, parse_hits, redact
from seamless_migrate.config import Settings


def test_redact_removes_secrets():
    text = (
        "login failed password=hunter2 for admin; token: abc123def; "
        'json {"password": "s3cr3t", "api_key": "AKIA-xyz"} '
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl "
        "curl -H 'X-Auth-Token: gAAAAABlongfernettokenvalue1234567890' "  # gitleaks:allow
        "db postgresql+psycopg://seamless:pgpass@db.internal:5432/seamless "
        "key -----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAK\n-----END RSA PRIVATE KEY----- end "
        "api token smg_abcdefghijklmnopqrstuvwxyz012345"
    )
    out = redact(text)
    for secret in (
        "hunter2",
        "abc123def",
        "s3cr3t",
        "AKIA-xyz",
        "eyJhbGciOiJIUzI1NiJ9",
        "gAAAAABlongfernettokenvalue",
        "pgpass",
        "MIIEow",
        "smg_abcdefghijklmnopqrstuvwxyz012345",
    ):
        assert secret not in out, secret
    assert "db.internal:5432/seamless" in out  # the non-secret part of the URL survives
    assert "login failed" in out and "end" in out
    assert redact("Bearer abcdef0123456789") == "Bearer [REDACTED]"


def test_redact_names_optional():
    text = "web-01 failed; web-010 is fine; db-02 too"
    assert redact(text) == text
    out = redact(text, ["web-01", "db-02"])
    assert "web-01 " not in out and "db-02" not in out and "web-010" in out


def captured(handler_result, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = handler_result(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


async def test_memory_remember_payload_and_auth_header():
    seen: list[httpx.Request] = []
    transport = captured(lambda r: (201, {"success": True}), seen)
    client = MemoryClient("http://memory:3111", "mem-secret", "seamless-test", transport=transport)
    ok = await client.remember(
        "cutover of web-01 failed: password=hunter2",
        "bug",
        ["seamless", "warm", "cutover"],
        files=["seamless/src/x.py"],
    )
    assert ok is True
    [req] = seen
    assert req.method == "POST" and str(req.url) == "http://memory:3111/agentmemory/remember"
    assert req.headers["authorization"] == "Bearer mem-secret"
    body = json.loads(req.content)
    assert body["type"] == "bug" and body["project"] == "seamless-test"
    assert body["concepts"] == ["seamless", "warm", "cutover"]
    assert body["files"] == ["seamless/src/x.py"]
    assert "hunter2" not in body["content"] and "web-01" in body["content"]

    seen.clear()
    no_secret = MemoryClient(
        "http://memory:3111", None, "p", transport=transport, redact_names=True
    )
    await no_secret.remember("web-01 broke", "fact", [], names=["web-01"])
    assert "authorization" not in seen[0].headers
    assert "web-01" not in json.loads(seen[0].content)["content"]


async def test_memory_search_tolerant_parsing():
    seen: list[httpx.Request] = []
    payload = {
        "mode": "compact",
        "results": [
            {"obsId": "o1", "title": "Warm cutover timed out on conv host", "score": 0.82},
            {
                "content": "Raise os_migrate_timeout for >500G volumes",
                "title": "timeouts",
                "score": "0.5",
            },
        ],
    }
    client = MemoryClient(
        "http://memory:3111/", None, "proj", transport=captured(lambda r: (200, payload), seen)
    )
    hits = await client.search("warm cutover Timeout", limit=5)
    assert hits == [
        MemoryHit(
            title="Warm cutover timed out on conv host",
            content="Warm cutover timed out on conv host",
            score=0.82,
        ),
        MemoryHit(
            title="timeouts", content="Raise os_migrate_timeout for >500G volumes", score=0.5
        ),
    ]
    body = json.loads(seen[0].content)
    assert body == {"query": "warm cutover Timeout", "limit": 5, "project": "proj"}
    assert str(seen[0].url) == "http://memory:3111/agentmemory/smart-search"

    assert parse_hits({"memories": [{"narrative": "n1"}]}, 5) == [
        MemoryHit(title="n1", content="n1", score=None)
    ]
    assert parse_hits({"items": ["plain text"]}, 5)[0].content == "plain text"
    assert parse_hits({"results": [{"observation": {"title": "t", "narrative": "deep"}}]}, 5) == [
        MemoryHit(title="t", content="deep", score=None)
    ]
    assert parse_hits([{"content": "a"}, {"content": "b"}], 1) == [
        MemoryHit(title="a", content="a", score=None)
    ]
    assert parse_hits({"unexpected": True}, 5) == []
    assert parse_hits("garbage", 5) == []


@pytest.mark.parametrize("mode", ["http_error", "connect_error", "bad_json"])
async def test_memory_failures_swallowed(mode):
    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "connect_error":
            raise httpx.ConnectError("refused", request=request)
        if mode == "bad_json":
            return httpx.Response(200, content=b"<html>")
        return httpx.Response(500, json={"error": "boom"})

    client = MemoryClient("http://memory:3111", None, "p", transport=httpx.MockTransport(handler))
    assert await client.remember("x", "fact", []) is False
    assert await client.search("x") == []
    assert await client.health() is False
    assert client.last_error


async def test_memory_health_and_from_settings():
    client = MemoryClient(
        "http://memory:3111",
        None,
        "p",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok"})),
    )
    assert await client.health() is True and client.available is True
    assert MemoryClient.from_settings(Settings()) is None
    built = MemoryClient.from_settings(
        Settings(memory_url="http://localhost:3111", memory_project="x", memory_redact_names=True)
    )
    assert built.project == "x" and built.redact_names is True
