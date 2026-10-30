"""The orchestrator's elector and its life cycle under the lease (SDD §7.1, §8, §11, §25.3-§25.5;
plan task HA3).

Two replicas run in one test process, each with a store, a bus and an orchestrator of its own, on
one SQLite file and, with SEAMLESS_TEST_PG_URL, on PostgreSQL. Their leases last two seconds - the
renew deadline 1.33 s, the last third the margin; the two servers', taken over after a release
alone, production's thirty -, a deposed replica's pause three tries of a tenth
of a second, and they expire by the database's clock, which no test fakes. A playbook here is a real
process group: a shell in a session of its own with a `sleep` child. Replicas are seamless-a to
seamless-c, their addresses in 192.0.2.0/24 (RFC 5737).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from seamless_migrate import lease as lease_module
from seamless_migrate.api.app import create_app
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import Phase, Strategy
from seamless_migrate.domain.models import Provider
from seamless_migrate.executors.base import StepName, outlive_cancel
from seamless_migrate.lease import Lease, LeaseRow
from seamless_migrate.store import Fenced, Store, StoreError
from tests.factories import make_migration, make_plan, make_provider, make_vm
from tests.orch_support import (
    DST_PROVIDER,
    SHORT_LEASE_S,
    SHORT_RETRY_S,
    SRC_PROVIDER,
    Harness,
    Monotonic,
    ScriptedExecutor,
    build_replica,
    lead,
    make_settings,
    until,
)


@pytest.fixture
async def cluster(any_store: Store, tmp_path) -> Any:
    """``cluster(holder, **kw)``: a replica on ``any_store``'s database and its leadership (not
    started); ``harness`` in ``kw`` goes to its orchestrator. Every one is stopped at the end."""
    made: list[tuple[Harness, Any]] = []

    def replica(holder: str, **kw: Any) -> tuple[Harness, Any]:
        h = build_replica(tmp_path, any_store.url, [], **kw.pop("harness", {}))
        leadership = lead(h, holder, **kw)
        made.append((h, leadership))
        return h, leadership

    yield replica
    for h, leadership in made:
        await leadership.stop()
        h.store.dispose()


def lease_now(store: Store) -> LeaseRow:
    """The lease row as the database reads it now, by an observer that never campaigns."""
    row = Lease(store.engine, "observer", "192.0.2.99:8080", 30.0).read()
    assert row is not None
    return row


def acquired(store: Store) -> list[dict[str, Any]]:
    """The data of every leader.acquired, in order."""
    return [e.data for e in store.events(limit=1000) if e.kind == "leader.acquired"]


def expired_now(store: Store) -> bool:
    """Whether the lease has expired by the database's clock, as an observer reads it now; False
    while nobody took it."""
    row = Lease(store.engine, "observer", "192.0.2.99:8080", 30.0).read()
    return row is not None and row.expired


def database_clock(store: Store) -> datetime:
    """The database's own clock, read in SQL of this test's - not the lease module's."""
    with store.engine.connect() as conn:
        if conn.dialect.name == "postgresql":
            return conn.execute(sa.text("SELECT clock_timestamp()")).scalar_one()
        text = conn.execute(sa.text("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')")).scalar_one()
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


async def stall_the_tick_loop(h: Harness, clock: Monotonic) -> Callable[[], None]:
    """Hang the replica's next tick and move its orchestrator's clock 31 s on - no tick for
    max(5 × tick_s, 30 s) -: its leader renews no more, and its renew deadline deposes it (SDD §8,
    §12). The hung tick ends with the orchestrator's stop; the call returned gives the ticks after
    their own work back."""
    await until(lambda: h.orch.health()["ticks"] > 0, 5, "a tick")
    tick, hung = h.orch.tick, asyncio.Event()

    async def hanging_tick() -> None:
        hung.set()
        await asyncio.Event().wait()

    h.orch.tick = hanging_tick
    await until(hung.is_set, 5, "the hung tick")
    clock.advance(31)
    assert not h.orch.health()["healthy"]

    def unstall() -> None:
        h.orch.tick = tick

    return unstall


class CutOff:
    """A replica cut off from its database (SDD §25.5): its renewals and its campaigns fail, each
    try's monotonic time noted, until ``reconnect()``."""

    def __init__(self, leadership: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.lease = leadership.elector.lease
        self.monkeypatch = monkeypatch
        self.renew, self.campaign = self.lease.renew, self.lease.campaign
        self.tried: list[float] = []
        monkeypatch.setattr(self.lease, "renew", self.unreachable)
        monkeypatch.setattr(self.lease, "campaign", self.unreachable)

    def unreachable(self, *args: Any, **kwargs: Any) -> Any:
        self.tried.append(time.monotonic())
        raise sa.exc.OperationalError("leases", {}, ConnectionError("the database is out of reach"))

    def reconnect(self) -> None:
        self.monkeypatch.setattr(self.lease, "renew", self.renew)
        self.monkeypatch.setattr(self.lease, "campaign", self.campaign)


def cut_off(leadership: Any, monkeypatch: pytest.MonkeyPatch) -> CutOff:
    return CutOff(leadership, monkeypatch)


@contextlib.contextmanager
def a_playbook() -> Iterator[tuple[subprocess.Popen[str], int]]:
    """A process group as a playbook leads one: a shell in a session of its own and its `sleep`
    child. Yields the shell and the child's pid; both are gone at the end, whatever happened."""
    shell = subprocess.Popen(
        ["sh", "-c", "sleep 60 & echo $!; wait"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert shell.stdout is not None
    child = int(shell.stdout.readline())
    try:
        yield shell, child
    finally:
        # gone, or killed already: macOS answers EPERM for a group whose members are all zombies,
        # as the reparented `sleep` is until launchd reaps it
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(shell.pid, signal.SIGKILL)
        shell.wait(10)
        shell.stdout.close()


def gone(pid: int) -> bool:
    """Whether no process ``pid`` exists any more."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def server_settings(tmp_path, url: str) -> Settings:
    """A server's settings on ``url`` with production's lease of thirty seconds (SDD §15.1): its
    leader is taken over after a release, never by expiry, and on a two-second lease a renewal a
    loaded runner delayed past the renew deadline of 1.33 s would depose it (the relay tests' CI
    flakes of PR 175 and 176)."""
    return Settings(
        data_dir=tmp_path / "data",
        db_url=url,
        secrets_dir=tmp_path / "secrets",
        auth_disabled=True,
        dashboard_dir=None,
        tick_s=0.05,
        leader_lease_s=30.0,
    )


async def test_only_the_replica_that_leads_runs_the_orchestrator(any_store, tmp_path, monkeypatch):
    """SDD §8, §15, §25.3: two servers on one database, each through the API's lifespan. The first
    takes the lease before it serves and runs its orchestrator; the second serves at once as a
    follower, its orchestrator never started and its store at no epoch. While the leader renews,
    the follower never takes over; once the leader stopped and released its lease, the follower
    takes it at its next try and starts its orchestrator. At no moment do both run one."""
    monkeypatch.setattr(lease_module, "RETRY_S", SHORT_RETRY_S)
    apps = [create_app(server_settings(tmp_path / name, any_store.url)) for name in ("a", "b")]
    services = [app.state.services for app in apps]
    served = [asyncio.Event(), asyncio.Event()]
    stops = [asyncio.Event(), asyncio.Event()]
    both: list[float] = []

    async def serve(i: int) -> None:
        async with apps[i].router.lifespan_context(apps[i]):
            served[i].set()
            await stops[i].wait()

    async def watch() -> None:
        while True:
            if all(s.orchestrator.health()["running"] for s in services):
                both.append(time.monotonic())
            await asyncio.sleep(0.005)

    def running(i: int) -> bool:
        return bool(services[i].orchestrator.health()["running"])

    watcher = asyncio.create_task(watch())
    servers = [asyncio.create_task(serve(0))]
    try:
        await asyncio.wait_for(served[0].wait(), 30)
        assert running(0), "it took the lease and started its orchestrator before it served"
        assert services[0].store.epoch == 1
        servers.append(asyncio.create_task(serve(1)))
        await asyncio.wait_for(served[1].wait(), 30)
        await asyncio.sleep(1.0)  # the leader renews ten times, the follower tries ten times
        assert not running(1) and services[1].store.epoch is None
        row = lease_now(any_store)
        assert (row.holder, row.epoch, row.expired) == (services[0].leadership.holder, 1, False)
        with pytest.raises(Fenced):
            services[1].store.put("provider", make_provider(id="dst-follower"))
        with a_playbook() as (shell, _):  # a follower runs no playbook: its book is closed
            services[1].orchestrator.process_groups.add(shell.pid)
            await until(lambda: shell.poll() is not None, 5, "the follower's kill")

        stops[0].set()
        await asyncio.wait_for(servers[0], 30)
        await until(lambda: running(1), 30, "the follower's takeover")
        assert services[1].store.epoch == 2 and not running(0)
        row = lease_now(any_store)
        assert (row.holder, row.epoch) == (services[1].leadership.holder, 2)
    finally:
        for stop in stops:
            stop.set()
        await asyncio.gather(*servers, return_exceptions=True)
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        for s in services:
            s.store.dispose()
    assert both == [], "two orchestrators ran at once"
    assert [(a["epoch"], a["cause"]) for a in acquired(any_store)] == [
        (1, "first"),
        (2, "released"),
    ]


async def test_a_leader_that_cannot_renew_is_deposed_by_its_renew_deadline(
    cluster, any_store, monkeypatch, caplog
):
    """SDD §8, §25.3, §25.5: a leader whose renewals fail - its database out of its reach - is
    deposed when its renew deadline passes, before its lease expires: its store writes nothing
    more and its orchestrator stops. Each failed renewal and the deposition are logged with their
    reasons. The follower takes the lease once it expired, as `expired`."""
    a_h, a = cluster("seamless-a")
    _, b = cluster("seamless-b")
    await a.start()
    await b.start()
    assert (a.epoch, b.epoch) == (1, None) and a_h.orch.health()["running"]
    await asyncio.sleep(a.elector.renew_deadline_s + 0.3)
    assert a.leads(), "each renewal moved its deadline"
    with caplog.at_level(logging.INFO, logger="seamless_migrate.leader"):
        tried = cut_off(a, monkeypatch).tried
        cut = time.monotonic()
        await until(lambda: a.last_deposition is not None, 10, "the deposition")
        row = lease_now(any_store)
        deposed = a.last_deposition
        assert (row.epoch, row.expired) == (1, False), "deposed before its lease expired"
        assert "renew deadline" in deposed.reason
        assert deposed.epoch == 1
        assert deposed.at >= deposed.deadline, "not before its deadline"
        assert deposed.at - deposed.deadline < 0.5, "at its deadline, by the watchdog"
        assert deposed.deadline - a.elector.renew_deadline_s <= cut, "the last renewal's deadline"
        assert len(tried) >= 2, "it tried every retry period until its deadline"
        assert a_h.store.epoch is None and not a.leads()
        with pytest.raises(Fenced):
            a_h.store.put("provider", make_provider(id="dst-deposed"))
        await until(lambda: not a_h.orch.health()["running"], 10, "the deposed leader's stop")
        await until(lambda: b.epoch == 2, 10, "the follower's takeover")
    messages = [r.getMessage() for r in caplog.records if r.name == "seamless_migrate.leader"]
    assert any("could not renew" in m and "the database is out of reach" in m for m in messages)
    assert any("deposed" in m and "renew deadline" in m for m in messages)
    assert [(d["epoch"], d["cause"]) for d in acquired(any_store)] == [(1, "first"), (2, "expired")]


async def test_the_watchdog_deposes_a_leader_whose_renewal_hangs(cluster, any_store, monkeypatch):
    """SDD §8, §25.3: a renewal that never returns - a connection cut in mid-statement - holds the
    elector's thread, never the watchdog: a thread of its own that calls no database deposes the
    leader when its deadline passes, while the renewal still hangs. A renewal that returns after
    the deposition renews nothing of the deposed leader's term."""
    h, a = cluster("seamless-a")
    await a.start()
    assert a.epoch == 1
    entered, hang = threading.Event(), threading.Event()
    renew = a.elector.lease.renew

    def hanging(epoch: int, **kwargs: Any) -> bool:
        entered.set()
        hang.wait(30)
        return renew(epoch, **kwargs)

    monkeypatch.setattr(a.elector.lease, "renew", hanging)
    try:
        await until(entered.is_set, 5, "a renewal")
        await until(lambda: a.last_deposition is not None, 10, "the deposition")
        assert not hang.is_set(), "deposed while its renewal hung"
        deposed = a.last_deposition
        assert "renew deadline" in deposed.reason and deposed.at >= deposed.deadline
        assert not lease_now(any_store).expired, "before its lease expired"
        await until(lambda: not h.orch.health()["running"], 10, "its orchestrator's stop")
    finally:
        hang.set()
    await asyncio.sleep(SHORT_RETRY_S * 3)  # the hung renewal returns and renews the row
    assert not a.leads() and a.epoch is None and h.store.epoch is None


async def test_a_deposed_leader_kills_its_playbooks_before_its_lease_expires(
    cluster, any_store, monkeypatch
):
    """SDD §7.1, §8, §25.4 layer 3: the process group of a playbook the leader runs - a shell in a
    session of its own and its `sleep` child - is killed with SIGKILL when the watchdog deposes the
    leader: after its renew deadline and before its lease expires, in the margin the deadline
    leaves. Its book of groups is closed then: a group registered after the kill dies at once."""
    h, a = cluster("seamless-a")
    await a.start()
    groups = h.orch.process_groups
    with a_playbook() as (shell, child):
        groups.add(shell.pid)
        cut_off(a, monkeypatch)
        await asyncio.sleep(a.elector.renew_deadline_s / 3)
        assert shell.poll() is None, "it runs while the leader's deadline has not passed"
        await until(lambda: shell.poll() is not None, 10, "the kill")
        row = lease_now(any_store)
        assert (row.epoch, row.expired) == (1, False), "killed before the lease expired"
        assert shell.returncode == -signal.SIGKILL
        deposed = a.last_deposition
        assert deposed is not None and deposed.at >= deposed.deadline
        await until(lambda: gone(child), 10, "the playbook's child")
    with a_playbook() as (late, _):
        groups.add(late.pid)
        await until(lambda: late.poll() is not None, 5, "the kill of a group registered after")
        assert late.returncode == -signal.SIGKILL


async def test_a_deposition_does_not_wait_for_a_blocked_event_loop(cluster, any_store, monkeypatch):
    """SDD §8, §25.4 layer 3: while the event loop is blocked - the process held by one long call -,
    the watchdog still deposes the leader at its deadline and kills its playbooks from its own
    thread; the orchestrator's stop, scheduled on the loop, runs once the loop is free again."""
    h, a = cluster("seamless-a")
    await a.start()
    with a_playbook() as (shell, _):
        h.orch.process_groups.add(shell.pid)
        cut_off(a, monkeypatch)
        # one long call holds the loop: no await until the kill came, or 10 s went by
        blocked_until = time.monotonic() + 10
        while shell.poll() is None and time.monotonic() < blocked_until:
            time.sleep(0.01)  # noqa: ASYNC251 - the loop is blocked on purpose
        killed = shell.poll() is not None
        still_running = h.orch.health()["running"]
        row = lease_now(any_store)
    assert killed, "the playbook was not killed while the event loop was blocked"
    assert still_running, "the stop waits for the loop: the kill did not"
    assert not row.expired
    await until(lambda: not h.orch.health()["running"], 10, "the stop once the loop is free")


async def test_a_leader_whose_tick_loop_hangs_lets_its_lease_go(cluster, any_store):
    """SDD §8, §12, §25.3: a leader renews only while its tick loop is healthy. When a tick hangs -
    no tick for max(5 × tick_s, 30 s) by the orchestrator's clock -, it renews no more: its lease
    lapses, its deadline deposes it, and the follower takes the lease as `expired`; the hung tick
    ends with the deposed orchestrator's stop."""
    clock = Monotonic()
    a_h, a = cluster("seamless-a", harness={"monotonic": clock})
    _, b = cluster("seamless-b")
    await a.start()
    await b.start()
    await until(lambda: a_h.orch.health()["ticks"] > 0, 5, "a tick")
    hung, ended = asyncio.Event(), asyncio.Event()

    async def hanging_tick() -> None:
        hung.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()

    a_h.orch.tick = hanging_tick
    await until(hung.is_set, 5, "the hung tick")
    assert a_h.orch.health()["healthy"]
    clock.advance(31)  # no tick for 31 s by the orchestrator's clock
    assert not a_h.orch.health()["healthy"]
    await asyncio.sleep(SHORT_RETRY_S * 3)  # a renewal already sent lands
    renewed = lease_now(any_store).renewed_at
    await asyncio.sleep(0.5)
    assert lease_now(any_store).renewed_at == renewed, "it renewed no more"
    await until(lambda: a.last_deposition is not None and ended.is_set(), 10, "the deposition")
    assert "renew deadline" in a.last_deposition.reason
    await until(lambda: lease_now(any_store).epoch == 2, 10, "a takeover of the lapsed lease")
    taken = acquired(any_store)[-1]
    assert (taken["epoch"], taken["cause"], taken["previous_epoch"]) == (2, "expired", 1)


async def test_a_leader_whose_write_is_fenced_is_deposed(cluster, any_store, monkeypatch):
    """SDD §8, §11, §25.4: a leader whose lease another replica took while it did not know - as a
    process paused past the margin; here a renew deadline set past its lease - is deposed by its
    first write the fence refuses, at once and whatever the code around the write catches: its
    playbooks are killed and its orchestrator stops, long before its deadline."""
    a_h, a = cluster("seamless-a", lease_s=1.0, renew_deadline_s=30.0)
    _, b = cluster("seamless-b")
    await a.start()
    await b.start()
    cut_off(a, monkeypatch)
    await until(lambda: b.epoch == 2, 10, "the takeover")
    assert a.leads() and a.epoch == 1, "it does not know it lost the lease"
    with a_playbook() as (shell, _):
        a_h.orch.process_groups.add(shell.pid)
        with contextlib.suppress(StoreError):  # the code around the write catches it
            await asyncio.to_thread(a_h.store.put, "provider", make_provider(id="dst-late"))
        assert not a.leads() and a_h.store.epoch is None
        deposed = a.last_deposition
        assert "fence" in deposed.reason and "epoch is 1, the lease's is 2" in deposed.reason
        assert deposed.deadline - deposed.at > 25, "long before its deadline"
        await until(lambda: shell.poll() is not None, 5, "the kill")
    await until(lambda: not a_h.orch.health()["running"], 10, "the stop")
    assert lease_now(any_store).epoch == 2
    assert any_store.list("provider", Provider) == [], "the refused write wrote nothing"


async def test_a_renewal_that_renews_nothing_deposes_at_once(cluster, any_store, monkeypatch):
    """SDD §8, §11, §25.3: a renewal that updates no row - another replica took the lease while
    this one could not renew - deposes the leader at once, long before its deadline: its
    playbooks are killed and its orchestrator stops."""
    a_h, a = cluster("seamless-a", lease_s=1.0, renew_deadline_s=30.0)
    _, b = cluster("seamless-b")
    await a.start()
    await b.start()
    cut = cut_off(a, monkeypatch)
    await until(lambda: b.epoch == 2, 10, "the takeover")
    assert a.leads(), "it does not know it lost the lease"
    with a_playbook() as (shell, _):
        a_h.orch.process_groups.add(shell.pid)
        cut.reconnect()  # its next renewal reaches the database: it renews nothing
        await until(lambda: a.last_deposition is not None, 5, "the deposition")
        deposed = a.last_deposition
        assert "renewed nothing" in deposed.reason and deposed.deadline - deposed.at > 25
        await until(lambda: shell.poll() is not None, 5, "the kill")
    await until(lambda: not a_h.orch.health()["running"], 10, "the stop")
    assert lease_now(any_store).epoch == 2


def _stage_a_running_pass(store: Store) -> str:
    """The providers, a running plan and its migration in precopy: what a leader resumes at its
    takeover and runs a pass of (SDD §8). Its id."""
    store.put("provider", SRC_PROVIDER)
    store.put("provider", DST_PROVIDER)
    plan = make_plan(id="plan-0000beef", status="running")
    store.put("plan", plan)
    migration = make_migration(
        id="mig-00000000c1",
        plan_id=plan.id,
        vm=make_vm(source_id="vm-1", name="web-01"),
        strategy=Strategy.warm,
        phase=Phase.precopy,
    )
    store.put("migration", migration, expected_version=0)
    return migration.id


async def test_a_stopping_leader_releases_its_lease_once_its_steps_ended(
    cluster, any_store, tmp_path, monkeypatch
):
    """SDD §8, §11, §25.3: a leader that stops stops its orchestrator first - its running pass
    cancelled and ended, its lease renewed meanwhile, past its renew deadline and whatever the
    stopping tick loop's health -, and only then releases its lease: expires_at the database's
    now() and leader.released in one transaction. The follower takes the lease at its next try,
    as `released`."""
    order: list[str] = []
    started = asyncio.Event()
    # the playbook takes longer to stop than the renew deadline: only renewals keep the lease
    stopping_s = SHORT_LEASE_S * 2 / 3 + 0.5

    async def pass_until_cancelled(ctx: Any) -> Any:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await outlive_cancel(asyncio.sleep(stopping_s))
            order.append("step ended")

    async def pass_that_waits(ctx: Any) -> Any:
        await asyncio.Event().wait()

    settings = make_settings(tmp_path)
    leader = ScriptedExecutor(settings, hooks={StepName.PRECOPY: pass_until_cancelled})
    follower = ScriptedExecutor(settings, hooks={StepName.PRECOPY: pass_that_waits})
    mid = _stage_a_running_pass(any_store)
    a_h, a = cluster("seamless-a", harness={"executor": leader, "settings": settings})
    _, b = cluster("seamless-b", harness={"executor": follower, "settings": settings})
    release = a.elector.lease.release

    def releasing(epoch: int, **kw: Any) -> bool:
        order.append("released")
        return release(epoch, **kw)

    monkeypatch.setattr(a.elector.lease, "release", releasing)
    await a.start()
    await b.start()
    await asyncio.wait_for(started.wait(), 10)
    assert leader.calls == [(mid, StepName.PRECOPY)]
    await a.stop()
    assert order == ["step ended", "released"]
    assert a.last_deposition is None, "renewed through its stop"
    row = lease_now(any_store)
    assert (row.epoch, row.expired) == (1, True)
    [released] = [e for e in any_store.events(limit=100) if e.kind == "leader.released"]
    assert released.data == {"holder": "seamless-a", "epoch": 1}
    await until(lambda: b.epoch == 2, 5, "the follower's takeover")
    assert acquired(any_store)[-1]["cause"] == "released"


async def test_a_deposed_replica_lets_a_healthy_follower_take_the_lease_first(
    cluster, any_store, monkeypatch
):
    """SDD §8, §11, §25.3 (the owner's decision of 2026-10-29): a replica deposed by its renew
    deadline - its tick loop stalled - takes its own lapsed lease only once it has been expired for
    the pause, three retry periods by the database's clock, so a healthy follower takes it first,
    as `expired`. The follower's first try after the expiry here comes just after the deposed
    replica's, whose tries run in step with its last renewal: the phase in which, before the pause,
    the deposed replica took its lease again. That try takes nothing, and past its pause the deposed
    replica follows the new leader."""
    clock = Monotonic()
    a_h, a = cluster("seamless-a", harness={"monotonic": clock})
    _, b = cluster("seamless-b")
    await a.start()
    await b.start()
    assert (a.epoch, b.epoch) == (1, None)
    tried = threading.Event()  # the deposed replica tried the lease once it expired
    after_expiry: list[int | None] = []
    deposed_campaign, follower_campaign = a.elector.lease.campaign, b.elector.lease.campaign

    def deposed_tries(**kw: Any) -> int | None:
        expired = expired_now(any_store)
        epoch = deposed_campaign(**kw)
        if expired or epoch is not None:
            after_expiry.append(epoch)
            tried.set()
        return epoch

    def follower_tries(**kw: Any) -> int | None:
        if a.last_deposition is not None:  # its first try after the expiry: just after A's
            tried.wait(10)
        return follower_campaign(**kw)

    monkeypatch.setattr(a.elector.lease, "campaign", deposed_tries)
    monkeypatch.setattr(b.elector.lease, "campaign", follower_tries)
    await stall_the_tick_loop(a_h, clock)
    await until(lambda: a.last_deposition is not None, 10, "the deposition")
    assert "renew deadline" in a.last_deposition.reason
    await until(lambda: lease_now(any_store).epoch == 2, 10, "a takeover of the lapsed lease")
    row = lease_now(any_store)
    assert (row.holder, row.epoch) == ("seamless-b", 2), "the deposed replica took its lease again"
    assert after_expiry and set(after_expiry) == {None}, "its tries after the expiry took nothing"
    taken = acquired(any_store)[-1]
    assert (taken["holder"], taken["cause"], taken["previous_holder"]) == (
        "seamless-b",
        "expired",
        "seamless-a",
    )
    await asyncio.sleep(SHORT_RETRY_S * 6)  # past its pause, it tries the new leader's lease
    assert a.epoch is None and lease_now(any_store).holder == "seamless-b"


async def test_a_lone_deposed_replica_takes_its_lease_again_after_the_pause(
    cluster, any_store, monkeypatch
):
    """SDD §8, §11, §25.5 (the owner's decision of 2026-10-29): a deposed replica no follower
    relieves - its tick loop stalled, then well again - campaigns through its pause, each try
    taking nothing while its lapsed lease has been expired for less than three retry periods by the
    database's clock, and takes it again at its first try after: it leads again, as `expired` after
    itself, and its orchestrator ticks again."""
    clock = Monotonic()
    h, a = cluster("seamless-a", harness={"monotonic": clock})
    await a.start()
    tries: list[tuple[datetime, int | None]] = []
    campaign = a.elector.lease.campaign

    def noted(**kw: Any) -> int | None:
        at = database_clock(any_store)  # before the try's own reads of the database's clock
        epoch = campaign(**kw)
        tries.append((at, epoch))
        return epoch

    monkeypatch.setattr(a.elector.lease, "campaign", noted)
    unstall = await stall_the_tick_loop(h, clock)
    await until(lambda: a.last_deposition is not None, 10, "the deposition")
    unstall()
    lapsed = lease_now(any_store)
    assert (lapsed.holder, lapsed.epoch) == ("seamless-a", 1)
    await until(lambda: a.epoch == 2, 10, "its second term")
    taken = lease_now(any_store)
    pause = timedelta(seconds=3 * SHORT_RETRY_S)
    assert (taken.holder, taken.epoch) == ("seamless-a", 2)
    assert taken.acquired_at - lapsed.expires_at >= pause, "not before the pause"
    held_back = [at for at, epoch in tries if epoch is None]
    assert any(at >= lapsed.expires_at for at in held_back), "it tried its expired lease"
    assert all(at < lapsed.expires_at + pause for at in held_back), "a try after the pause took it"
    assert tries[-1][1] == 2
    assert acquired(any_store)[-1] == {
        "holder": "seamless-a",
        "epoch": 2,
        "previous_holder": "seamless-a",
        "previous_epoch": 1,
        "cause": "expired",
        "resumes": 0,
    }
    await until(lambda: h.orch.health()["ticks"] > 0, 10, "its orchestrator's ticks")
    assert h.orch.health()["healthy"]


async def test_a_replica_that_released_its_lease_or_never_led_keeps_todays_timing(
    cluster, any_store, monkeypatch
):
    """SDD §8, §11, §25.3 (the owner's decision of 2026-10-29): only a deposition pauses a replica,
    and only before its own lapsed lease. The first campaign at the start takes the lease nobody
    holds at once; the lease an orderly stop released, the follower takes at its next try; and the
    lease that lapsed under its leader cut off from the database, a replica that never led takes at
    its first try after the expiry - no try of theirs that began with the lease expired takes
    nothing."""
    _, a = cluster("seamless-a")
    _, b = cluster("seamless-b")
    _, c = cluster("seamless-c")
    await a.start()
    assert a.epoch == 1, "its first try at the start took the lease"
    held_back: list[str] = []

    def at_once(leadership: Any) -> None:
        campaign = leadership.elector.lease.campaign

        def tries(**kw: Any) -> int | None:
            expired = expired_now(any_store)
            epoch = campaign(**kw)
            if expired and epoch is None:
                held_back.append(leadership.holder)
            return epoch

        monkeypatch.setattr(leadership.elector.lease, "campaign", tries)

    at_once(b)
    await b.start()
    await a.stop()  # it releases its lease
    await until(lambda: b.epoch == 2, 5, "the takeover of the released lease")
    at_once(c)
    await c.start()
    assert c.epoch is None
    cut_off(b, monkeypatch)
    await until(lambda: b.last_deposition is not None, 10, "the deposition")
    await until(lambda: c.epoch == 3, 10, "the takeover of the lapsed lease")
    assert held_back == []
    assert [(d["holder"], d["epoch"], d["cause"]) for d in acquired(any_store)] == [
        ("seamless-a", 1, "first"),
        ("seamless-b", 2, "released"),
        ("seamless-c", 3, "expired"),
    ]


async def test_a_deposed_replica_campaigns_again_and_can_lead_again(
    cluster, any_store, monkeypatch
):
    """SDD §8, §25.3: a deposed replica follows. Once its orchestrator's stop ended it campaigns
    again, and once its old lease has been expired for its pause it takes the next epoch - recorded
    as `expired`, itself the previous holder -, starts its orchestrator again as a new process would
    and reopens its book of process groups."""
    h, a = cluster("seamless-a")
    order: list[str] = []
    start, stop = h.orch.start, h.orch.stop
    campaign = a.elector.lease.campaign

    async def starting() -> None:
        order.append("start")
        await start()

    async def stopping() -> None:
        await stop()
        order.append("stopped")

    def campaigning(**kw: Any) -> int | None:
        epoch = campaign(**kw)
        if epoch is not None:
            order.append(f"took {epoch}")
        return epoch

    h.orch.start, h.orch.stop = starting, stopping
    monkeypatch.setattr(a.elector.lease, "campaign", campaigning)
    await a.start()
    assert order == ["took 1", "start"]
    cut = cut_off(a, monkeypatch)
    await until(lambda: a.last_deposition is not None, 10, "the deposition")
    cut.reconnect()  # the database is back
    await until(lambda: a.epoch == 2 and h.orch.health()["running"], 10, "the second term")
    assert order == ["took 1", "start", "stopped", "took 2", "start"]
    assert acquired(any_store)[-1] == {
        "holder": "seamless-a",
        "epoch": 2,
        "previous_holder": "seamless-a",
        "previous_epoch": 1,
        "cause": "expired",
        "resumes": 0,
    }
    with a_playbook() as (shell, _):
        h.orch.process_groups.add(shell.pid)
        await asyncio.sleep(0.3)
        assert shell.poll() is None, "its book of process groups is open again"
        h.orch.process_groups.discard(shell.pid)
