"""The orchestrator driving the real storage-handover executor over the fake clouds of
test_executor_handover (SDD §7.3, §8): what shows only when both run together (audit EX-17)."""

from __future__ import annotations

import asyncio

from seamless_migrate.domain.enums import Phase, PlanStatus
from seamless_migrate.executors.handover import HandoverExecutor
from tests.orch_support import build_harness, make_settings
from tests.test_executor_handover import held_manage, setup, shared_pool

P = Phase


def handover_harness(tmp_path, store, **settings_kw):
    """A running plan that cuts its one VM over by itself, by storage handover between clouds that
    share one Ceph pool."""
    _, calls, clouds, plan, mig, _ = setup(tmp_path / "clouds")
    shared_pool(clouds)
    settings = make_settings(tmp_path, **settings_kw)
    executor = HandoverExecutor(settings, conn_factory=lambda p: clouds[p.cloud], poll_s=0)
    h = build_harness(tmp_path, store, [mig.vm], executor=executor, settings=settings)
    store.put(
        "plan",
        plan.model_copy(
            update={
                "status": PlanStatus.running,
                "require_approval": False,
                "auto_cutover": True,
                "vm_ids": [mig.vm.source_id],
            }
        ),
    )
    store.put("migration", mig.model_copy(update={"phase": P.ready}))
    return h, calls, clouds, mig


def assert_rolled_back(clouds):
    """The source runs again on its volumes, managed back; RHOSO keeps none of them."""
    assert clouds["dst"].volumes == {} and clouds["dst"].servers == {}
    assert [s.status for s in clouds["src"].servers.values()] == ["ACTIVE"]


async def test_a_handover_step_cut_off_by_the_step_timeout_ends_only_after_its_call_returned(
    tmp_path, store
):
    """SDD §7.3, §15.1: the step timeout fires while Cinder's answer to the boot volume's manage at
    RHOSO is slow. The step ends - and the automatic rollback starts - only once that call
    returned and journaled the manage, so the rollback unmanages that volume at RHOSO, manages the
    renamed object back and the VM is rolled back in one go (audit EX-01)."""
    h, calls, clouds, mig = handover_harness(
        tmp_path, store, step_timeout_s=1.0, max_step_retries=0
    )
    held, release = held_manage(clouds["dst"], "volume-vol-root")
    await h.orch.start()
    try:
        assert await asyncio.to_thread(held.wait, 10)
        seen = len(calls)
        await asyncio.sleep(2.0)  # well past the step timeout
        m = await h.migration(mig.id)
        assert m.phase == P.cutover, h.history(m)
        assert calls[seen:] == [], "the rollback ran beside the call"
        release.set()
        final = await h.wait_phase(mig.id, P.rolled_back, P.failed)
        assert final.phase == P.rolled_back, final.error
        assert h.history(final)[-3:] == [P.failed, P.rolling_back, P.rolled_back]
        assert_rolled_back(clouds)
    finally:
        release.set()
        await h.orch.stop()


async def test_a_rollback_requested_during_a_handover_call_starts_after_that_call_returned(
    tmp_path, store
):
    """SDD §5.1, §7.3: a rollback requested while the cutover waits for Cinder cancels the step;
    the rollback runs only once the call in flight returned, from the whole journal (audit
    EX-01)."""
    h, calls, clouds, mig = handover_harness(tmp_path, store)
    held, release = held_manage(clouds["dst"], "volume-vol-root")
    await h.orch.start()
    try:
        assert await asyncio.to_thread(held.wait, 10)
        seen = len(calls)
        await h.orch.rollback(mig.id, "rina", "the cutover takes too long")
        await asyncio.sleep(1.0)
        m = await h.migration(mig.id)
        assert m.phase == P.rolling_back, h.history(m)
        assert calls[seen:] == [], "the rollback ran beside the call"
        release.set()
        final = await h.wait_phase(mig.id, P.rolled_back, P.failed)
        assert final.phase == P.rolled_back, final.error
        assert_rolled_back(clouds)
    finally:
        release.set()
        await h.orch.stop()
