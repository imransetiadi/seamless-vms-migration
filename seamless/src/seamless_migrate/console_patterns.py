"""The console check's success patterns, matched in a process of their own (SDD §7.5; pentest
2026-11-28).

A plan's ``verification.console_success_patterns`` are regular expressions an operator writes.
``re`` runs one search as a single call that holds the interpreter: a pattern with catastrophic
backtracking - ``(a+)+$`` over a line of ``a`` - kept the event loop, and with it the API, the
event stream and the orchestrator's tick, for as long as it ran, in a cutover's downtime window.
No thread ends such a search, so it runs in a child process the control plane stops at a
deadline; a search that reached it counts as no match and says so.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass

#: how long the patterns of one console check may take together (seconds)
PATTERN_TIMEOUT_S = 5.0
#: what the child process runs: this module's ``main``
_CHILD = "from seamless_migrate.console_patterns import main; main()"


@dataclass(frozen=True)
class PatternMatch:
    """The first pattern found in the text, if any; ``timed_out`` when the deadline ended it."""

    pattern: str | None
    timed_out: bool = False


def pattern_match(patterns: list[str], text: str) -> str | None:
    """The first of ``patterns`` found in ``text``; an invalid one is a plain substring."""
    for pattern in patterns:
        try:
            if re.search(pattern, text):
                return pattern
        except re.error:
            if pattern in text:
                return pattern
    return None


def main() -> None:
    """The child process: ``{"patterns": [...], "text": ...}`` on stdin, the index of the first
    pattern found - or null - on stdout."""
    request = json.load(sys.stdin)
    patterns = [str(p) for p in request["patterns"]]
    found = pattern_match(patterns, str(request["text"]))
    json.dump({"index": None if found is None else patterns.index(found)}, sys.stdout)


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


async def match_bounded(
    patterns: list[str], text: str, timeout_s: float | None = None
) -> PatternMatch:
    """``pattern_match`` in a child process, stopped after ``timeout_s`` (PATTERN_TIMEOUT_S).

    A child that cannot be started or answers nothing readable counts as no match: the console
    check fails and the migration's verification says why, never the control plane with it."""
    if not patterns:
        return PatternMatch(None)
    deadline = PATTERN_TIMEOUT_S if timeout_s is None else timeout_s
    payload = json.dumps({"patterns": patterns, "text": text}).encode("utf-8")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _CHILD,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return PatternMatch(None)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(payload), deadline)
    except TimeoutError:
        return PatternMatch(None, timed_out=True)
    finally:
        await _stop(proc)
    try:
        index = json.loads(out)["index"]
    except (ValueError, KeyError, TypeError):
        return PatternMatch(None)
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(patterns):
        return PatternMatch(patterns[index])
    return PatternMatch(None)
