"""agentmemory REST client and the shared redaction helper (SDD §13.4, §14.3)."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

REDACTED = "[REDACTED]"
_KEYWORDS = (
    r"password|passwd|pwd|pass|secret|client[_-]?secret|token|auth[_-]?token|x-auth-token|"
    r"api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|credentials?"
)
#: A credential key may carry a prefix joined by ``_``/``-``/``.`` (``OS_PASSWORD``,
#: ``vcenter_password``, ``ansible_become_pass``, ``AWS_SECRET_ACCESS_KEY``, ``auth.password``).
_KEY = r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_.-])*(?:" + _KEYWORDS + r")(?![A-Za-z0-9])"
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM blocks (keys, certificates)
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.S),
        "[REDACTED PEM]",
    ),
    # credentials embedded in URLs: scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"), r"\1[REDACTED]@"),
    # Authorization headers (any scheme)
    (re.compile(r"(?i)\b(authorization)\s*[:=]\s*(?:\w+\s+)?[^\s,;'\"]+"), r"\1: " + REDACTED),
    # bare bearer tokens
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + REDACTED),
    # key=value / key: value / "key": "value" (the key possibly prefixed, e.g. OS_PASSWORD)
    (
        re.compile(r"(?i)([\"']?" + _KEY + r"[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"),
        r"\1" + REDACTED,
    ),
    # CLI form: --os-password hunter2 / --password=hunter2 / -p hunter2 is too ambiguous
    (
        re.compile(r"(?i)(--[a-z0-9-]*(?:password|passwd|secret|token|api-key|key)\s+)([^\s]+)"),
        r"\1" + REDACTED,
    ),
    # well-known token shapes: seamless API tokens, Keystone fernet tokens, JWTs
    (re.compile(r"\bsmg_[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bgAAAAA[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
)


def redact(text: str, redact_names: Sequence[str] | None = None) -> str:
    """Remove secrets (and optionally VM names) before text leaves the control plane."""
    out = str(text)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    for name in sorted({n for n in (redact_names or []) if n}, key=len, reverse=True):
        out = re.sub(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", "[vm]", out)
    return out


@dataclass(frozen=True)
class MemoryHit:
    title: str
    content: str
    score: float | None = None


def _score(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _hit(item: Any) -> MemoryHit | None:
    if isinstance(item, str):
        return MemoryHit(title=item[:80], content=item)
    if not isinstance(item, dict):
        return None
    sources = [item]
    for nested in ("observation", "memory", "lesson"):
        if isinstance(item.get(nested), dict):
            sources.append(item[nested])
    content = next(
        (str(s[k]) for s in sources for k in ("content", "narrative", "title") if s.get(k)), ""
    )
    if not content:
        return None
    title = next((str(s["title"]) for s in sources if s.get("title")), content[:80])
    return MemoryHit(title=title, content=content, score=_score(item.get("score")))


def parse_hits(data: Any, limit: int) -> list[MemoryHit]:
    """Tolerantly extract hits from ``results|memories|items`` (or a bare list)."""
    items: Any = data
    if isinstance(data, dict):
        items = next(
            (data[k] for k in ("results", "memories", "items") if isinstance(data.get(k), list)),
            [],
        )
    if not isinstance(items, list):
        return []
    hits = [h for h in (_hit(i) for i in items) if h is not None]
    return hits[: max(0, limit)]


class MemoryClient:
    """Async agentmemory client; every failure is logged and swallowed."""

    def __init__(
        self,
        url: str,
        secret: str | None,
        project: str,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        redact_names: bool = False,
        timeout_s: float = 5.0,
    ) -> None:
        self.url = url.rstrip("/")
        self._secret = secret
        self.project = project
        self.redact_names = redact_names
        self._transport = transport
        self._timeout = timeout_s
        self.last_error: str | None = None
        self.available: bool | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> MemoryClient | None:
        if not settings.memory_url:
            return None
        return cls(
            settings.memory_url,
            settings.memory_secret,
            settings.memory_project,
            redact_names=settings.memory_redact_names,
        )

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "available": bool(self.available), "last_error": self.last_error}

    def _client(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self._secret}"} if self._secret else {}
        return httpx.AsyncClient(
            base_url=self.url, headers=headers, timeout=self._timeout, transport=self._transport
        )

    def _clean(self, text: str, names: Sequence[str] | None) -> str:
        return redact(text, names if self.redact_names else None)

    def _failed(self, what: str, exc: Exception) -> None:
        self.available = False
        self.last_error = redact(f"{what}: {type(exc).__name__}: {exc}")[:300]
        log.warning("agentmemory %s failed: %s", what, self.last_error)

    def _ok(self) -> None:
        self.available = True
        self.last_error = None

    async def remember(
        self,
        content: str,
        type: str,
        concepts: list[str],
        files: list[str] | None = None,
        *,
        names: Sequence[str] | None = None,
    ) -> bool:
        body = {
            "content": self._clean(content, names),
            "type": type,
            "concepts": [self._clean(c, names) for c in concepts],
            "files": list(files or []),
            "project": self.project,
        }
        try:
            async with self._client() as client:
                response = await client.post("/agentmemory/remember", json=body)
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, dict) or data.get("success") is False:
                raise ValueError(f"unexpected answer: {str(data)[:120]}")
        except Exception as exc:
            self._failed("remember", exc)
            return False
        self._ok()
        return True

    async def search(
        self, query: str, limit: int = 5, *, names: Sequence[str] | None = None
    ) -> list[MemoryHit]:
        body = {"query": self._clean(query, names), "limit": limit, "project": self.project}
        try:
            async with self._client() as client:
                response = await client.post("/agentmemory/smart-search", json=body)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            self._failed("search", exc)
            return []
        self._ok()
        return parse_hits(data, limit)

    async def health(self) -> bool:
        try:
            async with self._client() as client:
                response = await client.get("/agentmemory/livez")
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, dict) or data.get("status", "ok") != "ok":
                raise ValueError(f"unexpected answer: {str(data)[:120]}")
        except Exception as exc:
            self._failed("health", exc)
            return False
        self._ok()
        return True
