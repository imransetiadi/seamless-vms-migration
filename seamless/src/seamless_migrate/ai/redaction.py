"""The one redaction layer (SDD §13.4, ADV-06).

Everything that leaves the control plane for an AI service passes through it: ``JevClient.call``
redacts every tool argument and ``MemoryClient`` every request body, so no caller can forget it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
#: how far past a cut ``redact_cut`` redacts, so it still sees the end of a secret the cut splits
CUT_MARGIN = 16384
_KEYWORDS = (
    r"password|passwd|pwd|pass|secret|client[_-]?secret|token|auth[_-]?token|x-auth-token|"
    r"api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|credentials?"
)
#: A credential key may carry a prefix joined by ``_``/``-``/``.`` (``OS_PASSWORD``,
#: ``vcenter_password``, ``ansible_become_pass``, ``AWS_SECRET_ACCESS_KEY``, ``auth.password``).
_KEY = r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_.-])*(?:" + _KEYWORDS + r")(?![A-Za-z0-9])"
#: A mapping key whose whole value is a credential: a credential key, or a clouds.yaml ``auth``.
_CREDENTIAL_KEY = re.compile(r"(?i)" + _KEY + r"|auth")


def _auth_block(match: re.Match[str]) -> str:
    end = "\n" if match.group(0).endswith("\n") else ""
    return f"{match.group(1)}auth: {REDACTED}{end}"


_PATTERNS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    # PEM blocks (keys, certificates)
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.S),
        "[REDACTED PEM]",
    ),
    # the auth section of a clouds.yaml: block style (every line indented under "auth:") ...
    (re.compile(r"(?m)^([ \t]*)auth:[ \t]*\n(?:\1[ \t]+\S[^\n]*(?:\n|$))+"), _auth_block),
    # ... and JSON or YAML flow style ("auth": {...})
    (re.compile(r"(?i)(?<![\w.-])([\"']?auth[\"']?\s*:\s*)\{[^{}]*\}"), r"\1" + REDACTED),
    # credentials in URLs and connection strings: scheme://user:pass@host (a pass with "@" too)
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/]*@"), r"\1[REDACTED]@"),
    # Authorization headers (any scheme)
    (re.compile(r"(?i)\b(authorization)\s*[:=]\s*(?:\w+\s+)?[^\s,;'\"]+"), r"\1: " + REDACTED),
    # bare bearer tokens
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + REDACTED),
    # key=value / key: value / "key": "value" (the key possibly prefixed, e.g. OS_PASSWORD), which
    # also covers X-Auth-Token / X-Subject-Token headers and Password=...; in connection strings
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
    # API key shapes: AWS access key ids, GitHub tokens, sk- keys, Slack tokens
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}|\bgithub_pat_[A-Za-z0-9_]{22,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), REDACTED),
)


def redact(text: str, redact_names: Sequence[str] | None = None) -> str:
    """Remove secrets (and optionally VM names) before text leaves the control plane."""
    out = str(text)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return replace_names(out, {name: "[vm]" for name in redact_names or []})


def replace_names(text: str, replacements: dict[str, str]) -> str:
    r"""Replace each name by its replacement wherever it stands as a word (SDD §13.4): between
    characters that are not letters or digits ([^\W_]), so web-01-data, web-01_root and data-web-01
    lose web-01 while web-010 (another VM) keeps it. One pass over every name - the longest first
    where two start at the same place -, so what a replacement wrote is never searched again, and
    the text is the same in every process, whatever its string hash seed (review RV22-04)."""
    names = sorted({n for n in replacements if n}, key=lambda n: (-len(n), n))
    if not names:
        return str(text)
    words = re.compile(rf"(?<![^\W_])(?:{'|'.join(map(re.escape, names))})(?![^\W_])")
    return words.sub(lambda m: replacements[m.group(0)], str(text))


def redact_cut(text: str, limit: int, redact_names: Sequence[str] | None = None) -> str:
    """``redact`` the first ``limit`` + CUT_MARGIN characters, then cut to ``limit`` (SDD §13.4).

    Cutting first could leave a PEM block without its END line or a URL without the ``@`` of its
    credentials, which ``redact`` no longer recognizes; the margin bounds the work whatever the
    text's length."""
    return redact(str(text)[: limit + CUT_MARGIN], redact_names)[:limit]


def redact_tail(text: str, limit: int, redact_names: Sequence[str] | None = None) -> str:
    """``redact`` the last ``limit`` + CUT_MARGIN characters, then keep the last ``limit``
    (SDD §13.4): keeping the end first could drop the BEGIN line of a PEM block it ends in."""
    return redact(str(text)[-(limit + CUT_MARGIN) :], redact_names)[-limit:]


def redact_payload(value: Any, redact_names: Sequence[str] | None = None) -> Any:
    """``redact`` every string of a JSON-like value; a credential key's whole value is removed."""
    if isinstance(value, str):
        return redact(value, redact_names)
    if isinstance(value, Mapping):
        return {
            key: REDACTED
            if isinstance(key, str) and _CREDENTIAL_KEY.fullmatch(key)
            else redact_payload(item, redact_names)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_payload(item, redact_names) for item in value]
    return value
