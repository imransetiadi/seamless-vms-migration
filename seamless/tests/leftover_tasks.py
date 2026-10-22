"""What an async test leaves behind (QASuite §14): the tasks still running when it ends.

pytest-asyncio closes a test's event loop by cancelling every task left and waiting for them all;
a task that never ends after its cancel hangs that close, and the run with it, with no test
named - CI's py3.11 control-plane job hung so at 55 % of the suite on PR #49 and PR #82, until
its 30-minute timeout. ``cancel_leftovers`` does what the close does first, bounded: it cancels the
tasks and waits ``grace_s`` for them, and names each one that did not end, with its stack."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Iterable


async def cancel_leftovers(tasks: Iterable[asyncio.Task], grace_s: float) -> str:
    """Cancel ``tasks`` and wait up to ``grace_s`` for them to end; the report of those that did
    not - each task's name and coroutine with its stack -, empty when every one ended."""
    left = [task for task in tasks if not task.done()]
    if not left:
        return ""
    for task in left:
        task.cancel()
    _, stuck = await asyncio.wait(left, timeout=grace_s)
    if not stuck:
        return ""
    lines = [f"{len(stuck)} task(s) did not end {grace_s:g} s after their cancel:"]
    for task in sorted(stuck, key=lambda t: t.get_name()):
        buffer = io.StringIO()
        task.print_stack(file=buffer)
        lines.append(f"- {task.get_name()} {task.get_coro()!r}\n{buffer.getvalue()}")
    return "\n".join(lines)
