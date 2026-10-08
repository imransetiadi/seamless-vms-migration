"""Jev MCP client (SDD §14.1) on the MCP Python SDK 2.x.

``stdio`` mode launches ``SEAMLESS_JEV_COMMAND`` with a whitelisted environment; ``http`` mode
connects to ``SEAMLESS_JEV_URL`` (streamable HTTP) with a bearer token; ``off`` never spawns
anything. Every call is bounded by ``SEAMLESS_JEV_TIMEOUT_S``; three consecutive failures open a
circuit breaker for 300 s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from ..config import Settings
from .memory import redact

log = logging.getLogger(__name__)

JEV_ENV_VARS = (
    "TYPESAFE_API_KEY",
    "OPENROUTER_API_KEY",
    "JEV_PROVIDER",
    "JEV_API_KEY",
    "JEV_API_BASE_URL",
    "JEV_MCP_MODEL",
)
BREAKER_THRESHOLD = 3
BREAKER_OPEN_S = 300.0

SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]


class JevUnavailable(Exception):
    """Jev is off, unreachable, slow, or answered with an error/invalid payload."""


def stdio_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The only variables the stdio child receives: PATH, HOME and the Jev provider keys."""
    source = os.environ if environ is None else environ
    return {k: source[k] for k in ("PATH", "HOME", *JEV_ENV_VARS) if source.get(k)}


def parse_result(result: Any) -> dict[str, Any]:
    """Parse ``content[0].text`` of a ``CallToolResult`` as a JSON object."""
    content = getattr(result, "content", None) or []
    text = getattr(content[0], "text", None) if content else None
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise JevUnavailable(f"tool error: {redact(str(text or ''))[:200]}")
    if not isinstance(text, str):
        raise JevUnavailable("invalid response: no text content")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise JevUnavailable("invalid response: not JSON") from None
    if not isinstance(payload, dict):
        raise JevUnavailable("invalid response: not a JSON object")
    return payload


class JevClient:
    def __init__(
        self,
        settings: Settings,
        session_factory: SessionFactory | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.mode = settings.jev_mode
        self._factory = session_factory or self._default_session
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0
        self.last_error: str | None = None
        #: (tool, outcome) -> count, exported as seamless_advisor_calls_total
        self.calls: Counter[tuple[str, str]] = Counter()

    # -- state -------------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def breaker_open(self) -> bool:
        return self._clock() < self._open_until

    @property
    def available(self) -> bool:
        return self.enabled and not self.breaker_open

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "available": self.available, "last_error": self.last_error}

    # -- transport ---------------------------------------------------------------------------
    def stdio_parameters(self, environ: Mapping[str, str] | None = None) -> Any:
        try:
            from mcp import StdioServerParameters
        except ImportError:
            raise JevUnavailable(
                "the 'mcp' package is not installed; install seamless-migrate[jev]"
            ) from None
        argv = shlex.split(self.settings.jev_command)
        if not argv:
            raise JevUnavailable("SEAMLESS_JEV_COMMAND is empty")
        return StdioServerParameters(command=argv[0], args=argv[1:], env=stdio_env(environ))

    @asynccontextmanager
    async def _default_session(self) -> AsyncIterator[Any]:
        try:
            from mcp import ClientSession
        except ImportError:
            raise JevUnavailable(
                "the 'mcp' package is not installed; install seamless-migrate[jev]"
            ) from None
        if self.mode == "stdio":
            from mcp.client.stdio import stdio_client

            async with stdio_client(self.stdio_parameters()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        elif self.mode == "http":
            if not self.settings.jev_url:
                raise JevUnavailable("SEAMLESS_JEV_URL is not set")
            import httpx2
            from mcp.client.streamable_http import streamable_http_client

            headers = {}
            if self.settings.jev_token:
                headers["Authorization"] = f"Bearer {self.settings.jev_token}"
            timeout = httpx2.Timeout(self.settings.jev_timeout_s, read=self.settings.jev_timeout_s)
            async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http:
                async with streamable_http_client(self.settings.jev_url, http_client=http) as (
                    read,
                    write,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
        else:
            raise JevUnavailable("Jev is disabled (SEAMLESS_JEV_MODE=off)")

    # -- calls -------------------------------------------------------------------------------
    async def _call_once(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self._factory() as session:
            result = await session.call_tool(
                tool, arguments, read_timeout_seconds=self.settings.jev_timeout_s
            )
        return parse_result(result)

    def _record_failure(self, tool: str, message: str) -> None:
        self._failures += 1
        self.last_error = message
        self.calls[(tool, "error")] += 1
        if self._failures >= BREAKER_THRESHOLD:
            self._open_until = self._clock() + BREAKER_OPEN_S
            log.warning("Jev circuit breaker open for %.0f s after: %s", BREAKER_OPEN_S, message)

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise JevUnavailable("Jev is disabled (SEAMLESS_JEV_MODE=off)")
        if self.breaker_open:
            self.calls[(tool, "circuit_open")] += 1
            raise JevUnavailable("circuit breaker open after repeated Jev failures")
        try:
            payload = await asyncio.wait_for(
                self._call_once(tool, arguments), timeout=self.settings.jev_timeout_s
            )
        except TimeoutError:
            message = f"{tool} timed out after {self.settings.jev_timeout_s:g} s"
            self._record_failure(tool, message)
            raise JevUnavailable(message) from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # transport errors, tool errors, invalid payloads
            message = redact(f"{tool}: {exc}")[:300]
            self._record_failure(tool, message)
            if isinstance(exc, JevUnavailable):
                raise
            raise JevUnavailable(message) from exc
        self._failures = 0
        self.last_error = None
        self.calls[(tool, "ok")] += 1
        return payload

    async def decide(
        self,
        decision: str,
        evidence: str,
        priorities: str,
        candidates: list[dict[str, str]],
        requirements: list[str],
        escalate_on_contradiction: bool = True,
    ) -> dict[str, Any]:
        return await self.call(
            "jev_decide",
            {
                "decision": decision,
                "evidence": evidence,
                "priorities": priorities,
                "candidates": candidates,
                "requirements": requirements,
                "escalate_on_contradiction": escalate_on_contradiction,
            },
        )

    async def classify(
        self, items: list[dict[str, str]], classes: list[dict[str, str]], purpose: str
    ) -> dict[str, Any]:
        return await self.call(
            "jev_classify", {"items": items, "classes": classes, "purpose": purpose}
        )

    async def verify(self, claims: list[str], evidence: list[dict[str, str]]) -> dict[str, Any]:
        return await self.call("jev_verify", {"claims": claims, "evidence": evidence})

    async def screen(self, text: str, purpose: str) -> dict[str, Any]:
        return await self.call("jev_screen", {"text": text, "purpose": purpose})
