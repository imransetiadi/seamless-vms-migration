"""The orchestrator driving the real storage-handover executor over the fake clouds of
test_executor_handover (SDD §7.3, §8): what shows only when both run together (audit EX-17)."""

from __future__ import annotations

import asyncio
import time

import pytest

from seamless_migrate.domain.enums import Phase, PlanStatus
from seamless_migrate.executors.handover import HandoverExecutor
from seamless_migrate.orchestrator import NotAllowed
from tests.orch_support import build_harness, make_settings
from tests.test_executor_handover import HTTPError, held_manage, sdk_error, setup, shared_pool

P = Phase


def handover_harness(tmp_path, store, auto_rollback=True, **settings_kw):
    """A running plan that cuts its one VM over by itself, by storage handover between clouds that
    share one Ceph pool."""
    _, calls, clouds, plan, mig, _ = setup(tmp_path / "clouds")
    verification = plan.verification.model_copy(update={"auto_rollback": auto_rollback})
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
                "verification": verification,
            }
        ),
    )
    store.put("migration", mig.model_copy(update={"phase": P.ready}))
    return h, calls, clouds, mig


def shutoff_never_confirmed(clouds, delay=None):
    """Nova accepts the stop; the wait for SHUTOFF then meets nova-api answering 503 (a restart),
    on every attempt - or, with ``delay``, ends only after that many seconds (a slow guest)."""
    wait = clouds["src"].compute.wait_for_server

    def wait_for_server(server, status="ACTIVE", **kw):
        if status == "SHUTOFF":
            if delay is None:
                raise HTTPError("HTTP 503 Service Unavailable: nova-api is restarting")
            time.sleep(delay)
        return wait(server, status=status, **kw)

    clouds["src"].compute.wait_for_server = wait_for_server


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


async def test_a_handover_whose_stop_was_accepted_but_never_confirmed_is_rolled_back(
    tmp_path, store
):
    """SDD §7.3 step 1, §8: Nova accepted the stop, then the wait for SHUTOFF failed through the
    step's retries. The clock opened at the stop's request, so the plan's automatic rollback starts
    the source again instead of leaving it stopped in failed (audit EX-02)."""
    h, calls, clouds, mig = handover_harness(tmp_path, store)
    shutoff_never_confirmed(clouds)
    await h.orch.start()
    try:
        final = await h.wait_phase(mig.id, P.rolled_back, P.failed)
        assert h.history(final)[-3:] == [P.failed, P.rolling_back, P.rolled_back], final.error
        assert ("src", "start_server", "srv-1") in calls
    finally:
        await h.orch.stop()


async def test_a_failed_handover_whose_stop_was_sent_is_not_cancelled(tmp_path, store):
    """SDD §5.1: without the automatic rollback the failed handover keeps its clock open, so a
    cancel - which would leave the source stopped with nothing to restart it - is refused, and a
    rollback starts the source (audit EX-02)."""
    h, calls, clouds, mig = handover_harness(tmp_path, store, auto_rollback=False)
    shutoff_never_confirmed(clouds)
    await h.orch.start()
    try:
        failed = await h.wait_phase(mig.id, P.failed)
        assert failed.downtime_started_at is not None
        with pytest.raises(NotAllowed):
            await h.orch.cancel(mig.id, "rina", "give up on this VM")
        await h.orch.rollback(mig.id, "rina", "start the source again")
        final = await h.wait_phase(mig.id, P.rolled_back, P.failed)
        assert final.phase == P.rolled_back, final.error
        assert ("src", "start_server", "srv-1") in calls
    finally:
        await h.orch.stop()


async def test_a_handover_cut_off_while_its_stop_waits_is_rolled_back(tmp_path, store):
    """SDD §7.3 step 1, §15.1: the step timeout cuts the cutover off while the stop it sent waits
    for a slow guest. The clock opens at the stop's request, so the automatic rollback starts the
    source again (audit EX-02)."""
    h, calls, clouds, mig = handover_harness(
        tmp_path, store, step_timeout_s=1.0, max_step_retries=0
    )
    shutoff_never_confirmed(clouds, delay=2.0)
    await h.orch.start()
    try:
        final = await h.wait_phase(mig.id, P.rolled_back, P.failed, timeout=15)
        assert h.history(final)[-3:] == [P.failed, P.rolling_back, P.rolled_back], final.error
        assert ("src", "start_server", "srv-1") in calls
    finally:
        await h.orch.stop()


async def test_a_rollback_resumed_after_it_archived_its_journal_still_reports_the_recreated_vm(
    tmp_path, store, refuse_write
):
    """SDD §7.3, §8: the rollback recreated the source server - a new id - and archived its
    journal, then the migration's save after the plan's rewrite failed (a store error: the driver
    is relaunched). The resumed rollback reports the recreated VM again from the archived journal,
    so the migration and its plan name the same, existing server (audit EX-04)."""
    h, calls, clouds, mig = handover_harness(tmp_path, store)
    clouds["dst"].crash_on = "create_server"  # step 8 fails once the source server was deleted
    create = clouds["src"].compute.create_server
    refused = []

    def recreate(**params):
        # the next save of the migration - the one after the plan's rewrite - fails
        refused.append(refuse_write(store, "UPDATE documents", "migration"))
        return create(**params)

    clouds["src"].compute.create_server = recreate
    await h.orch.start()
    try:
        final = await h.wait_phase(mig.id, P.rolled_back, P.failed, timeout=20)
        assert refused and refused[0], "the save after the plan's rewrite was refused"
        plan = await h.plan(mig.plan_id)
        assert final.phase == P.rolled_back, final.error
        assert final.vm.source_id == plan.vm_ids[0] == "src-new-1", (final.vm, plan.vm_ids)
    finally:
        await h.orch.stop()


async def test_a_handover_retries_a_manage_cinder_answered_503_and_completes(tmp_path, store):
    """SDD §7.3, §8: after the source server was deleted, Cinder answers one manage with 503 - the
    SDK's own HttpException. The step is retried from its journal and the handover completes,
    instead of rolling the VM back for a blip (audit EX-12)."""
    h, calls, clouds, mig = handover_harness(tmp_path, store)
    post = clouds["dst"].block_storage.post
    blips = []

    def blip(url, json=None, **kw):
        if url == "/manageable_volumes" and not blips:
            blips.append(url)
            raise sdk_error(503, "Service Unavailable")
        return post(url, json=json, **kw)

    clouds["dst"].block_storage.post = blip
    await h.orch.start()
    try:
        final = await h.wait_phase(mig.id, P.completed, P.failed, P.rolled_back, timeout=20)
        assert blips and final.phase == P.completed, (h.history(final), final.error)
        assert P.rolling_back not in h.history(final)
    finally:
        await h.orch.stop()
