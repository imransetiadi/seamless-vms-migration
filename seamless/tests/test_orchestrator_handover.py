"""The orchestrator driving the real storage-handover executor over the fake clouds of
test_executor_handover (SDD §7.3, §8): what shows only when both run together (audit EX-17)."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from seamless_migrate.domain.enums import Phase, PlanStatus
from seamless_migrate.executors.handover import HandoverExecutor
from seamless_migrate.orchestrator import NotAllowed
from tests.orch_support import build_harness, make_settings
from tests.test_executor_handover import (
    HTTPError,
    held_manage,
    powering_off,
    sdk_error,
    setup,
    shared_pool,
    the_give_back_refused,
)

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
    tmp_path, store, monkeypatch
):
    """SDD §7.3, §15.1: the step timeout fires while Cinder's answer to the boot volume's manage at
    RHOSO is slow. The step ends - and the automatic rollback starts - only once that call
    returned and journaled the manage, so the rollback unmanages that volume at RHOSO, manages the
    renamed object back and the VM is rolled back in one go (audit EX-01)."""
    h, calls, clouds, mig = handover_harness(
        tmp_path, store, step_timeout_s=1.0, max_step_retries=0
    )
    # the 1 s ceiling is for the cutover this test cuts off. The step timeout bounds every step
    # attempt (SDD §15.1), so the rollback the test expects to succeed would run under it too - and
    # on a loaded runner it took longer ("rollback exceeded the step timeout of 1 s", a CI flake):
    # the rollback runs unbounded (0 = no bound), from its start on, after the cutover failed
    unbounded = h.orch.settings.model_copy(update={"step_timeout_s": 0.0})
    do_rollback = h.orch._do_rollback

    async def rollback_unbounded(m):
        monkeypatch.setattr(h.orch, "settings", unbounded)
        await do_rollback(m)

    monkeypatch.setattr(h.orch, "_do_rollback", rollback_unbounded)
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
    put = clouds["src"].compute.put
    refused = []

    def give_back(url, json=None, microversion=None, **kw):
        # past the clock's close: the next save of the migration - the one after the plan's
        # rewrite - fails
        if not refused and (json or {}).get("volumeAttachment", {}).get("delete_on_termination"):
            refused.append(refuse_write(store, "UPDATE documents", "migration"))
        return put(url, json=json, microversion=microversion, **kw)

    clouds["src"].compute.put = give_back
    await h.orch.start()
    try:
        final = await h.wait_phase(mig.id, P.rolled_back, P.failed, timeout=20)
        assert refused and refused[0], "the save after the plan's rewrite was refused"
        plan = await h.plan(mig.plan_id)
        assert final.phase == P.rolled_back, final.error
        assert final.vm.source_id == plan.vm_ids[0] == "src-new-1", (final.vm, plan.vm_ids)
    finally:
        await h.orch.stop()


async def test_a_store_error_closing_the_clock_in_a_handover_rollback_is_retried(
    tmp_path, store, refuse_write
):
    """SDD §5.2, §7.3 (review RV36B-03): the rollback closes the clock once the recreated source
    runs; the store refusing that save fails the attempt as transient - the retry resumes from the
    journal and closes the clock again -, and the rollback ends rolled_back."""
    h, calls, clouds, mig = handover_harness(tmp_path, store)
    clouds["dst"].crash_on = "create_server"  # step 8 fails once the source server was deleted
    create = clouds["src"].compute.create_server
    refused = []

    def recreate(**params):
        refused.append(refuse_write(store, "UPDATE documents", "migration"))  # the clock's close
        return create(**params)

    clouds["src"].compute.create_server = recreate
    await h.orch.start()
    try:
        final = await h.wait_phase(mig.id, P.rolled_back, P.failed, timeout=20)
    finally:
        await h.orch.stop()
    assert refused == [["UPDATE documents migration"]]
    assert final.phase == P.rolled_back, (h.history(final), final.error)
    assert final.vm.source_id == "src-new-1" and final.downtime_ended_at is not None


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


async def test_the_automatic_rollback_starts_a_source_nova_still_powered_off(tmp_path, store):
    """SDD §7.3 step 1, §8, §5.1: a stop Nova accepted meets a 503 during its wait, and the
    automatic rollback starts while Nova still powers the guest off - the server reads ACTIVE. The
    rollback waits for the power-off to end and starts the source, so rolled_back means the source
    runs (review RV26-01)."""
    h, calls, clouds, mig = handover_harness(tmp_path, store, max_step_retries=0)
    settled = powering_off(clouds["src"], settle_s=1.5)
    await h.orch.start()
    try:
        final = await h.wait_phase(mig.id, P.rolled_back, P.failed, timeout=20)
        assert await asyncio.to_thread(settled.wait, 10), "Nova never finished the power-off"
        assert final.phase == P.rolled_back, final.error
        assert clouds["src"].servers["srv-1"].status == "ACTIVE", h.history(final)
    finally:
        await h.orch.stop()


@pytest.mark.parametrize(
    ("cloud", "blipped"),
    [
        ("dst", lambda url, json: url == "/manageable_volumes"),  # step 6's manage
        ("src", lambda url, json: "os-unmanage" in (json or {})),  # step 5's unmanage
    ],
    ids=["destination", "source"],
)
async def test_a_handover_retry_after_a_rotation_signs_in_with_the_new_credentials(
    tmp_path, store, cloud, blipped
):
    """SDD §8, §13.3: a cutover attempt meets a 503 - on either cloud - and, while it waits for
    its retry, the admin rotates both providers' credentials (PUT .../credentials, the old
    password revoked). The retry reads the providers again and signs in with the new credentials,
    so the cutover completes instead of being refused 401 and rolled back (review RV26-07)."""
    from seamless_migrate.domain.models import utcnow
    from tests.orch_support import DST_PROVIDER, SRC_PROVIDER
    from tests.test_executor_handover import Credentialed

    h, calls, clouds, mig = handover_harness(tmp_path, store)
    revoked: set[str] = set()
    h.executor._conn_factory = lambda p: Credentialed(
        clouds[p.cloud], p.credentials_secret, revoked
    )
    for provider in (SRC_PROVIDER, DST_PROVIDER):
        store.put("provider", provider.model_copy(update={"credentials_secret": "v1"}))
    post = clouds[cloud].block_storage.post
    rotated: list[str] = []

    def blip_while_rotated(url, json=None, **kw):
        if blipped(url, json) and not rotated:
            # the call meets a 503; meanwhile the admin rotates both providers' credentials
            for provider in (SRC_PROVIDER, DST_PROVIDER):
                store.put(
                    "provider",
                    provider.model_copy(
                        update={"credentials_secret": "v2", "credentials_updated_at": utcnow()}
                    ),
                )
            revoked.add("v1")
            rotated.append(url)
            raise sdk_error(503, "Service Unavailable")
        return post(url, json=json, **kw)

    clouds[cloud].block_storage.post = blip_while_rotated
    await h.orch.start()
    try:
        final = await h.wait_phase(mig.id, P.completed, P.rolled_back, P.failed, timeout=20)
        assert rotated, "the cutover never made the call"
        assert final.phase == P.completed, (h.history(final), final.error)
    finally:
        await h.orch.stop()


async def _failed_after_its_rollback(h, mid, timeout=20.0):
    """The migration once its automatic rollback itself failed: failed, after rolling_back."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        m = await h.migration(mid)
        if m.phase == P.failed and P.rolling_back in h.history(m):
            return m
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError((m.phase, h.history(m), m.error))
        await asyncio.sleep(0.01)


async def test_a_retry_after_a_failed_handover_rollback_keeps_the_boot_volume(tmp_path, store):
    """SDD §7.3 step 3 (review RV33-01), end to end: the cutover fails at its delete (Nova 500),
    the automatic rollback gives delete_on_termination back and fails at its start (performed,
    its wait failed); the operator's Retry - the dashboard's "fix the cause and retry" - keeps the
    volumes again before it deletes the source: Nova deletes no source volume."""
    h, calls, clouds, mig = handover_harness(tmp_path, store, max_step_retries=0)
    src = clouds["src"]
    delete, wait = src.compute.delete_server, src.compute.wait_for_server
    refused, started, waits_failed = [], [], []

    def delete_once(s, ignore_missing=True):
        if not refused:
            refused.append(1)
            raise RuntimeError("nova: HTTP 500 Internal Server Error")
        return delete(s, ignore_missing=ignore_missing)

    def start_server(s):
        src.record("start_server", s.id)
        s.status = "ACTIVE"
        started.append(1)

    def wait_for_server(s, status="ACTIVE", **kw):
        if status == "ACTIVE" and started and not waits_failed:
            waits_failed.append(1)
            raise RuntimeError("nova: HTTP 500 Internal Server Error")
        return wait(s, status=status, **kw)

    src.compute.delete_server, src.compute.start_server = delete_once, start_server
    src.compute.wait_for_server = wait_for_server
    await h.orch.start()
    try:
        failed = await _failed_after_its_rollback(h, mig.id)
        assert "start the source server" in (failed.error or ""), failed.error
        assert src.servers["srv-1"].dot["vol-root"] is True  # given back
        await h.orch.retry(mig.id, "rina")
        final = await h.wait_phase(mig.id, P.completed, P.rolled_back, P.failed, timeout=20)
    finally:
        await h.orch.stop()
    assert src.deleted_by_nova == [], (final.phase, final.error)
    assert final.phase == P.completed, (h.history(final), final.error)


async def test_a_rollback_failing_after_the_recreate_leaves_the_clock_closed(tmp_path, store):
    """SDD §5.2, §7.3 rollback (review RV36B-03), end to end: step 8 fails at RHOSO after step 4
    deleted the source; the automatic rollback recreates the source while nova-api restarts and
    its give-back is refused on the attempt and both retries. The migration is failed with the
    clock closed - the recreated source runs -, and the operator's Roll back an hour later records
    no downtime for that hour."""
    h, calls, clouds, mig = handover_harness(tmp_path, store)
    src = clouds["src"]
    clouds["dst"].crash_on = "create_server"  # step 8 fails once the source server was deleted
    refusing = the_give_back_refused(src)
    await h.orch.start()
    try:
        failed = await _failed_after_its_rollback(h, mig.id)
        assert {sid: s.status for sid, s in src.servers.items()} == {"src-new-1": "ACTIVE"}
        assert failed.downtime_ended_at is not None and failed.actual_downtime_s is not None
        assert "restore delete_on_termination" in failed.error
        h.clock.advance(3600)  # an hour before the operator rolls back again
        refusing["on"] = False
        await h.orch.rollback(mig.id, "rina", "finish the rollback")
        final = await h.wait_phase(mig.id, P.rolled_back, P.failed, timeout=20)
    finally:
        await h.orch.stop()
    assert final.phase == P.rolled_back, (h.history(final), final.error)
    assert final.vm.source_id == "src-new-1"
    assert final.actual_downtime_s == failed.actual_downtime_s  # that hour is no downtime
    assert src.servers["src-new-1"].dot["src-vol-1"] is True


@pytest.mark.parametrize("path", ["the recreated source", "the source it started"])
async def test_a_step_timeout_in_the_rollbacks_power_on_leaves_no_clock_open(tmp_path, store, path):
    """SDD §5.2, §7.3 (review RV37A-01), end to end: the step timeout fires while the rollback waits
    for the source it recreated - or started - to run. The attempt still fails on the timeout, but
    the power-on, its journal mark and the clock's close ran as one: the source runs and the clock
    is closed."""
    h, calls, clouds, mig = handover_harness(
        tmp_path, store, step_timeout_s=1.0, max_step_retries=0
    )
    src = clouds["src"]
    wait = src.compute.wait_for_server
    started: list[str] = []
    if path == "the recreated source":
        clouds["dst"].crash_on = "create_server"  # step 8 fails once step 4 deleted the source
    else:
        src.crash_on = "delete_server"  # the cutover fails before step 4 deleted the source

        def start_server(s):
            src.record("start_server", s.id)
            s.status = "ACTIVE"
            started.append(s.id)

        src.compute.start_server = start_server

    def slow(s, status="ACTIVE", **kw):
        booting = str(s.id).startswith("src-new-") if path == "the recreated source" else started
        if status == "ACTIVE" and booting:
            time.sleep(2.0)  # boots past the step timeout; Nova reads it ACTIVE then
        return wait(s, status=status, **kw)

    src.compute.wait_for_server = slow
    await h.orch.start()
    try:
        failed = await _failed_after_its_rollback(h, mig.id, timeout=30)
    finally:
        await h.orch.stop()
    assert "exceeded the step timeout" in (failed.error or ""), failed.error
    running = sorted(sid for sid, s in src.servers.items() if s.status == "ACTIVE")
    assert running == (["src-new-1"] if path == "the recreated source" else ["srv-1"])
    assert failed.downtime_ended_at is not None, (failed.downtime_started_at, failed.error)


async def test_a_shutdown_in_the_recreate_counts_no_downtime_while_the_source_runs(tmp_path, store):
    """SDD §5.2, §7.3, §8 (review RV37A-01): a shutdown that lands while the rollback waits for the
    source it recreated waits out the call and the clock's close, so the hour before the control
    plane runs again is not downtime."""
    h, calls, clouds, mig = handover_harness(tmp_path, store)
    src = clouds["src"]
    clouds["dst"].crash_on = "create_server"
    wait = src.compute.wait_for_server
    waiting, slept = threading.Event(), []

    def slow(s, status="ACTIVE", **kw):
        if status == "ACTIVE" and str(s.id).startswith("src-new-") and not slept:
            slept.append(s.id)
            waiting.set()
            time.sleep(1.0)  # the shutdown lands here; Nova reads it ACTIVE when the wait ends
        return wait(s, status=status, **kw)

    src.compute.wait_for_server = slow
    await h.orch.start()
    try:
        assert await asyncio.to_thread(waiting.wait, 20)
    finally:
        await h.orch.stop()  # waits for the call in flight, then cancels the step
    stopped = await h.migration(mig.id)
    assert stopped.downtime_ended_at is not None  # closed before the stop ended
    h.clock.advance(3600)  # an hour before the control plane runs again
    await h.orch.start()
    try:
        final = await h.wait_phase(mig.id, P.rolled_back, P.failed, timeout=20)
    finally:
        await h.orch.stop()
    assert final.phase == P.rolled_back, (h.history(final), final.error)
    assert final.actual_downtime_s is not None and final.actual_downtime_s < 3600


async def test_a_retry_after_a_rollback_recreated_the_source_asks_for_a_roll_back(tmp_path, store):
    """SDD §7.3 (review RV36B-03), end to end: after a rollback that recreated the source failed at
    its give-back, the operator's Retry is refused before any call - the cutover can never
    complete - with no clock opened and no automatic rollback; the Roll back it names finishes,
    following the recreated source."""
    h, calls, clouds, mig = handover_harness(tmp_path, store)
    src = clouds["src"]
    clouds["dst"].crash_on = "create_server"
    refusing = the_give_back_refused(src)
    await h.orch.start()
    try:
        await _failed_after_its_rollback(h, mig.id)
        refusing["on"] = False
        before = len(calls)
        await h.orch.retry(mig.id, "rina")
        refused = await h.wait_phase(mig.id, P.failed, timeout=20)
        assert (
            "a rollback attempt recreated the source server src-new-1, which runs: roll back"
            in (refused.error)
        )
        assert calls[before:] == []  # no call, at the source or at RHOSO
        assert refused.downtime_started_at is None  # no outage opened while src-new-1 runs
        assert h.history(refused)[-2:] == [P.cutover, P.failed]  # no automatic rollback
        await h.orch.rollback(mig.id, "rina", "roll back to finish it")
        final = await h.wait_phase(mig.id, P.rolled_back, P.failed, timeout=20)
    finally:
        await h.orch.stop()
    assert final.phase == P.rolled_back, (h.history(final), final.error)
    assert final.vm.source_id == "src-new-1"
    assert "src-new-1" in (await h.plan(mig.plan_id)).vm_ids


async def test_a_retried_handover_cutover_that_finds_the_source_running_counts_a_new_outage(
    tmp_path, store
):
    """SDD §5.2, §7.3 step 1 (review RV33-04), end to end: the rollback's start Nova performed
    while its wait failed leaves the clock open though the source runs; when the operator retries
    an hour later, the cutover finds it running, closes that outage and counts a new one from its
    re-stop - the hour the source ran is no downtime."""
    h, calls, clouds, mig = handover_harness(tmp_path, store, max_step_retries=0)
    src = clouds["src"]
    stop, ports, wait = src.compute.stop_server, src.network.ports, src.compute.wait_for_server
    failing, started, waits_failed = [], [], []

    def stop_then_fail_the_definition(s):
        stop(s)
        if not started:
            failing.append(1)

    def neutron_once(**query):
        if failing:
            failing.clear()
            raise RuntimeError("neutron: HTTP 500 Internal Server Error")
        return ports(**query)

    def start_server(s):
        src.record("start_server", s.id)
        s.status = "ACTIVE"
        started.append(1)

    def wait_for_server(s, status="ACTIVE", **kw):
        if status == "ACTIVE" and started and not waits_failed:
            waits_failed.append(1)
            raise RuntimeError("nova: HTTP 500 Internal Server Error")
        return wait(s, status=status, **kw)

    src.compute.stop_server, src.network.ports = stop_then_fail_the_definition, neutron_once
    src.compute.start_server, src.compute.wait_for_server = start_server, wait_for_server
    await h.orch.start()
    try:
        failed = await _failed_after_its_rollback(h, mig.id)
        assert src.servers["srv-1"].status == "ACTIVE"  # the source runs again
        h.clock.advance(3600)  # for an hour, before the operator retries
        await h.orch.retry(mig.id, "rina")
        final = await h.wait_phase(mig.id, P.completed, P.rolled_back, P.failed, timeout=20)
    finally:
        await h.orch.stop()
    assert final.phase == P.completed, (h.history(final), final.error)
    assert final.downtime_started_at > failed.downtime_started_at  # a new outage
    assert final.actual_downtime_s is not None and 0 <= final.actual_downtime_s < 3600, (
        final.actual_downtime_s
    )


async def test_a_retry_after_a_first_stop_never_confirmed_counts_a_new_outage(tmp_path, store):
    """SDD §5.2, §7.3 step 1 (review RV34B-04), end to end: the first stop is accepted and its wait
    fails - only the stop sent is journaled, the clock opened at its request -; the automatic
    rollback finds no definition and starts the source, Nova performs the start while its wait
    fails: failed, the clock open, the source running. The operator's retry an hour later finds
    it running, closes that outage and counts a new one from its stop."""
    h, calls, clouds, mig = handover_harness(tmp_path, store, max_step_retries=0)
    src = clouds["src"]
    wait = src.compute.wait_for_server
    shutoff_failed, started, active_failed = [], [], []

    def start_server(s):
        src.record("start_server", s.id)
        s.status = "ACTIVE"
        started.append(1)

    def wait_for_server(s, status="ACTIVE", **kw):
        if status == "SHUTOFF" and not shutoff_failed:
            shutoff_failed.append(1)  # the first stop: accepted, its wait fails
            raise HTTPError("HTTP 500 Internal Server Error: nova-api")
        if status == "ACTIVE" and started and not active_failed:
            active_failed.append(1)  # the rollback's start: performed, its wait fails
            raise RuntimeError("nova: HTTP 500 Internal Server Error")
        return wait(s, status=status, **kw)

    src.compute.start_server, src.compute.wait_for_server = start_server, wait_for_server
    await h.orch.start()
    try:
        failed = await _failed_after_its_rollback(h, mig.id)
        assert failed.downtime_started_at is not None and failed.downtime_ended_at is None
        assert src.servers["srv-1"].status == "ACTIVE"  # the source runs again
        h.clock.advance(3600)  # for an hour, before the operator retries
        await h.orch.retry(mig.id, "rina")
        final = await h.wait_phase(mig.id, P.completed, P.rolled_back, P.failed, timeout=20)
    finally:
        await h.orch.stop()
    assert final.phase == P.completed, (h.history(final), final.error)
    assert final.downtime_started_at > failed.downtime_started_at  # a new outage
    assert final.actual_downtime_s is not None and 0 <= final.actual_downtime_s < 3600, (
        final.actual_downtime_s
    )
