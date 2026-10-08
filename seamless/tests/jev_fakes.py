"""In-process stand-ins for an MCP session talking to jev-mcp."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(tool: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"jev_{tool}_response.json").read_text())


def tool_result(payload: Any, is_error: bool = False) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=is_error)


class FakeSession:
    def __init__(self, responder: Callable[[str, dict], Any]) -> None:
        self.responder = responder
        self.calls: list[tuple[str, dict, float | None]] = []

    async def call_tool(self, name: str, arguments: dict, read_timeout_seconds=None):
        self.calls.append((name, arguments, read_timeout_seconds))
        result = self.responder(name, arguments)
        if isinstance(result, Exception):
            raise result
        return result if hasattr(result, "content") else tool_result(result)


def session_factory(responder: Callable[[str, dict], Any]):
    """Return ``(factory, session)``; every call opens the same fake session."""
    session = FakeSession(responder)

    @asynccontextmanager
    async def factory():
        yield session

    return factory, session


def fixture_responder(overrides: dict[str, Any] | None = None) -> Callable[[str, dict], Any]:
    overrides = overrides or {}

    def respond(name: str, arguments: dict) -> Any:
        tool = name.removeprefix("jev_")
        if tool in overrides:
            value = overrides[tool]
            return value(arguments) if callable(value) else value
        return load_fixture(tool)

    return respond
