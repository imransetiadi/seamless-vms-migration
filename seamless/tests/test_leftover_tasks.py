"""The tasks an async test leaves behind (tests/leftover_tasks.py, QASuite §14)."""

import asyncio

from tests.conftest import LEFTOVERS
from tests.leftover_tasks import cancel_leftovers


async def test_a_task_that_outlives_its_cancel_is_named_with_its_stack():
    """A task that ends when cancelled is no leftover; one that keeps running after its cancel is
    named, with its coroutine and the line it waits on, once the grace has passed."""
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # what hangs an event loop's close

    async def polite() -> None:
        await asyncio.sleep(3600)

    held = asyncio.create_task(stubborn(), name="stubborn-one")
    ended = asyncio.create_task(polite(), name="polite-one")
    await asyncio.sleep(0)
    report = await cancel_leftovers([held, ended], grace_s=0.1)
    release.set()
    await held
    assert ended.cancelled()
    assert report.startswith("1 task(s) did not end 0.1 s after their cancel:")
    assert "- stubborn-one <coroutine object" in report and "stubborn" in report
    assert "await release.wait()" in report
    assert "polite-one" not in report
    assert await cancel_leftovers([held, ended], grace_s=0.1) == ""


async def test_every_async_test_gets_the_leftover_check_first(request):
    """conftest gives each async test the check as its first fixture, so it is torn down last -
    right before the test's loop closes."""
    assert request.node.fixturenames[0] == LEFTOVERS


def test_a_sync_test_gets_no_leftover_check(request):
    assert LEFTOVERS not in request.node.fixturenames
