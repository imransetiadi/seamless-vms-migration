"""The orchestrator's elector and its life cycle under the lease (SDD §8, §11, §25.3-§25.5; plan
task HA3).

A server replica's elector tries the lease while its replica follows and renews it while it leads,
each every retry period, in a thread of its own on an engine of its own, so neither a busy event
loop nor a busy pool delays them. It notes its monotonic clock before it sends a campaign or a
renewal: one that takes or renews the lease sets the leader's deadline that far plus the renew
deadline, two thirds of the lease. It renews only while the tick loop is healthy, or while its
replica stops. A watchdog, a thread of its own that calls no database, deposes the leader when its
deadline passes; so do a renewal that renewed nothing, a write the fence refused, the check before
an action on a cloud that read another epoch, and a takeover whose start failed. A deposition sets
the store's epoch to None - the replica writes nothing more -, kills its playbooks' process groups
from the deposing thread and schedules the stop of its orchestrator on the event loop; the replica
campaigns again once that stop ended, and takes its own lapsed lease only once it has been expired
for the pause (``lease.PAUSE_PERIODS`` retry periods), so a follower takes it first (the owner's
decision of 2026-10-29).

:class:`Leadership` is the event loop's side: a takeover runs the start hooks - the demo seed with
its restore of the fake clouds -, the orchestrator's ``start()`` and the notifier; a deposition
stops them; the server's stop stops them, then releases the lease with ``leader.released`` in one
transaction.

Each renewal writes the orchestrator's health into the lease row, and each retry period the tick
loop is unhealthy the leader writes it alone, renewing nothing, so every replica reports the
leader's orchestrator; a follower is ready while its campaign tries the lease (SDD §12, §25.6; plan
task HA4).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from . import lease as lease_module
from .ai.redaction import redact_cut
from .domain import fsm
from .domain.models import Event
from .events import new_event
from .executors.base import ProcessGroups
from .lease import Lease, LeaseRow, Taken, lease_engine, read_lease
from .store import Fenced, Store, documents, events, write_event

log = logging.getLogger(__name__)

#: the renew deadline's share of the lease (SDD §8, §25.3): 20 s of 30, so a leader's deadline
#: comes a third of the lease before the expiry the database set
RENEW_SHARE = 2 / 3
#: how long a server's start waits for its elector's first try (SDD §8, §15)
FIRST_TRY_S = 30.0
#: how long a stop waits for each of the elector's threads to end; one held up in a database call
#: past it is a daemon, left to end by itself
JOIN_S = 5.0
#: a follower's campaign is alive while it tried the lease within max(this many retry periods,
#: CAMPAIGN_STALE_S) - the tick's rule (SDD §12)
CAMPAIGN_STALE_PERIODS = 5
CAMPAIGN_STALE_S = 30.0


@dataclass(frozen=True)
class Deposition:
    """Why and when a leader was deposed (SDD §8): ``at`` and ``deadline`` on the elector's
    monotonic clock."""

    epoch: int
    reason: str
    at: float
    deadline: float


def resumable_migrations(conn: sa.Connection) -> int:
    """How many migrations a takeover drives again: those in a resumable phase (SDD §4.3, §8)."""
    phase = documents.c.data["phase"].as_string()
    query = (
        sa.select(sa.func.count())
        .select_from(documents)
        .where(
            documents.c.kind == "migration",
            phase.in_(sorted(str(p) for p in fsm.RESUMABLE_PHASES)),
        )
    )
    return int(conn.execute(query).scalar_one())


def released_by(conn: sa.Connection, holder: str | None, epoch: int | None) -> bool:
    """Whether the newest leader.released names ``holder`` and ``epoch``: the lease taken over was
    released, not lapsed (SDD §11)."""
    data = conn.execute(
        sa.select(events.c.data)
        .where(events.c.kind == "leader.released")
        .order_by(events.c.seq.desc())
        .limit(1)
    ).scalar()
    return isinstance(data, dict) and (data.get("holder"), data.get("epoch")) == (holder, epoch)


def acquired_event(holder: str, taken: Taken, cause: str, resumes: int) -> Event:
    """leader.acquired (SDD §4.3)."""
    if taken.previous_epoch is None:
        why = "nobody held the lease"
    elif cause == "released":
        why = f"{taken.previous_holder} released epoch {taken.previous_epoch}"
    else:
        why = f"the lease of {taken.previous_holder} at epoch {taken.previous_epoch} expired"
    noun = "migration" if resumes == 1 else "migrations"
    return new_event(
        "leader.acquired",
        f"{holder} leads the orchestrator with epoch {taken.epoch} ({why}); "
        f"it resumes {resumes} {noun}",
        data={
            "holder": holder,
            "epoch": taken.epoch,
            "previous_holder": taken.previous_holder,
            "previous_epoch": taken.previous_epoch,
            "cause": cause,
            "resumes": resumes,
        },
    )


def released_event(holder: str, epoch: int) -> Event:
    """leader.released (SDD §4.3)."""
    return new_event(
        "leader.released",
        f"{holder} released the orchestrator lease of epoch {epoch}",
        data={"holder": holder, "epoch": epoch},
    )


def _why(exc: BaseException) -> str:
    """A failure in words for one log line: the driver's own error where the database's driver
    raised it, its class and its text on one line, redacted and cut."""
    cause = exc.orig if isinstance(exc, sa.exc.DBAPIError) and exc.orig is not None else exc
    text = " ".join(str(cause).split())
    return f"{type(cause).__name__}: {redact_cut(text, 300)}" if text else type(cause).__name__


class Elector:
    """One replica's campaign and renewals (SDD §8, §25.3), in threads of its own.

    ``on_acquired(epoch, event)`` runs in the elector's thread once it took the lease - ``event``
    the leader.acquired its takeover wrote -, and ``on_deposed(epoch, reason)`` in the deposing
    thread once its kill is sent. ``report()`` is the orchestrator's health, which the leader writes
    into the lease row (SDD §12)."""

    def __init__(
        self,
        lease: Lease,
        store: Store,
        groups: ProcessGroups,
        *,
        healthy: Callable[[], bool] = lambda: True,
        report: Callable[[], dict[str, Any] | None] = lambda: None,
        renew_deadline_s: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.lease = lease
        self.store = store
        self.groups = groups
        self.healthy = healthy
        self.report = report
        self.renew_deadline_s = (
            lease.lease_s * RENEW_SHARE if renew_deadline_s is None else float(renew_deadline_s)
        )
        self._monotonic = monotonic
        #: guards the term - the epoch led with and its deadline - and the stop's flags
        self._cond = threading.Condition()
        self._epoch: int | None = None
        self._deadline = 0.0
        self._stopping = False
        self._closed = False
        #: set while no orchestrator of an earlier term is still stopping: only then a campaign
        self._idle = threading.Event()
        self._idle.set()
        self._first_try = threading.Event()
        self._threads: list[threading.Thread] = []
        #: the campaign's and the renewals' thread, and when its last try of the lease ended
        self._runner: threading.Thread | None = None
        self._tried_at: float | None = None
        self._unhealthy_said = False
        self._following_said = False
        self.on_acquired: Callable[[int, Event | None], None] = lambda epoch, event: None
        self.on_deposed: Callable[[int, str], None] = lambda epoch, reason: None
        #: the last deposition, for the logs' reader and the tests
        self.last_deposition: Deposition | None = None

    # -- what the replica asks ----------------------------------------------------------------
    @property
    def epoch(self) -> int | None:
        """The epoch this replica leads with; None while it follows."""
        with self._cond:
            return self._epoch

    def leads(self) -> bool:
        """Whether this replica leads: it took the lease and its deadline has not passed (§8)."""
        with self._cond:
            return self._epoch is not None and self._monotonic() < self._deadline

    def campaigning(self) -> bool:
        """Whether the campaign is alive, which a follower's readiness judges (SDD §12): its thread
        runs and its last try of the lease - taken, held by another or failed - ended within
        max(5 retry periods, 30 s)."""
        runner = self._runner
        with self._cond:
            tried = self._tried_at
        stale = max(CAMPAIGN_STALE_PERIODS * self.lease.retry_s, CAMPAIGN_STALE_S)
        return (
            runner is not None
            and runner.is_alive()
            and tried is not None
            and self._monotonic() - tried <= stale
        )

    # -- the threads ----------------------------------------------------------------------------
    def start(self) -> None:
        """Start the campaign's thread and the watchdog's."""
        for name, target in (("seamless-elector", self._run), ("seamless-watchdog", self._watch)):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
            if name == "seamless-elector":
                self._runner = thread

    def wait_first_try(self, timeout: float) -> bool:
        """Until the first campaign was tried: whether it was within ``timeout``."""
        return self._first_try.wait(timeout)

    def stopping(self) -> None:
        """An orderly stop begins (SDD §8): no campaign from now, and renewals whatever the tick
        loop's health, until the release."""
        with self._cond:
            self._stopping = True

    def idle(self) -> None:
        """The orchestrator of a term that ended stopped: the replica may campaign again."""
        self._idle.set()

    def close(self, timeout: float = JOIN_S) -> None:
        """End the threads: a renewal or a campaign under way ends first, each within the retry
        period; one held up past ``timeout`` is left to end by itself."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout)
        self._threads = [thread for thread in self._threads if thread.is_alive()]

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._closed:
                    return
                epoch, stopping = self._epoch, self._stopping
            tried = False
            try:
                if epoch is not None:
                    tried = True
                    self._renew(epoch, stopping)
                elif not stopping and self._idle.is_set():
                    tried = True
                    self._campaign()
            except Exception as exc:  # noqa: BLE001 - the thread goes on; its next try may work
                log.warning("the elector of the orchestrator lease failed: %s", _why(exc))
            finally:
                if tried:
                    with self._cond:
                        self._tried_at = self._monotonic()
                self._first_try.set()
            with self._cond:
                self._cond.wait_for(lambda: self._closed, timeout=self.lease.retry_s)

    def _campaign(self) -> None:
        noted = self._monotonic()  # before it sends: the expiry the database sets comes after
        written: list[Event] = []

        def record(conn: sa.Connection, taken: Taken) -> None:
            written.append(write_event(conn, self._acquired_event(conn, taken)))

        epoch = self.lease.campaign(record=record)
        if epoch is None:
            if not self._following_said:
                self._following_said = True
                held = None
                with contextlib.suppress(Exception):  # a log line's read: best effort
                    held = self.lease.read()
                log.info(
                    "%s follows: %s holds the orchestrator lease at epoch %s",
                    self.lease.holder,
                    held.holder if held else "another replica",
                    held.epoch if held else "unknown",
                )
            return
        with self._cond:
            self._epoch = epoch
            self._deadline = noted + self.renew_deadline_s
            self.store.epoch = epoch
            self._idle.clear()
            self._cond.notify_all()
        self._following_said = False
        self.groups.open()
        event = written[0] if written else None
        log.info(
            "%s took the orchestrator lease with epoch %d (%s)",
            self.lease.holder,
            epoch,
            event.data["cause"] if event is not None else "taken",
        )
        self.on_acquired(epoch, event)

    def _acquired_event(self, conn: sa.Connection, taken: Taken) -> Event:
        """The takeover's leader.acquired, read in its transaction (SDD §11)."""
        if taken.previous_epoch is None:
            cause = "first"
        elif released_by(conn, taken.previous_holder, taken.previous_epoch):
            cause = "released"
        else:
            cause = "expired"
        return acquired_event(self.lease.holder, taken, cause, resumable_migrations(conn))

    def _healthy(self) -> bool:
        try:
            return bool(self.healthy())
        except Exception as exc:  # noqa: BLE001 - a health that cannot be read is no health
            log.warning("the tick loop's health could not be read: %s", _why(exc))
            return False

    def _report(self) -> dict[str, Any] | None:
        try:
            return self.report()
        except Exception as exc:  # noqa: BLE001 - a health that cannot be read is not written
            log.warning("the orchestrator's health could not be read: %s", _why(exc))
            return None

    def _write_health(self, epoch: int, health: dict[str, Any] | None) -> None:
        """While the tick loop is unhealthy: the orchestrator's health alone, renewing nothing, so
        every replica reports the stall (SDD §12); a write that finds the lease taken deposes."""
        if health is None:
            return
        try:
            written = self.lease.write_health(epoch, health)
        except Exception as exc:  # noqa: BLE001 - written again at the next period
            log.debug("could not write the orchestrator's health at epoch %d: %s", epoch, _why(exc))
            return
        if not written:
            self.depose(f"the lease was taken: its health at epoch {epoch} wrote nothing", epoch)

    def _renew(self, epoch: int, stopping: bool) -> None:
        health = self._report()
        if not stopping and not self._healthy():
            if not self._unhealthy_said:
                log.warning(
                    "the orchestrator's tick loop is not healthy: the lease of epoch %d is not "
                    "renewed and lapses",
                    epoch,
                )
                self._unhealthy_said = True
            self._write_health(epoch, health)
            return
        self._unhealthy_said = False
        noted = self._monotonic()  # before it sends: the expiry the database sets comes after
        try:
            renewed = self.lease.renew(epoch, health=health)
        except Exception as exc:  # noqa: BLE001 - tried again at the next period
            log.warning("could not renew the orchestrator lease of epoch %d: %s", epoch, _why(exc))
            return
        if not renewed:
            self.depose(f"the lease was taken: its renewal of epoch {epoch} renewed nothing", epoch)
            return
        with self._cond:
            if self._epoch == epoch:
                self._deadline = max(self._deadline, noted + self.renew_deadline_s)

    def _watch(self) -> None:
        """The watchdog: calls no database, deposes the leader once its deadline passed."""
        while True:
            with self._cond:
                if self._closed:
                    return
                if self._epoch is None:
                    self._cond.wait()
                    continue
                epoch, left = self._epoch, self._deadline - self._monotonic()
                if left > 0:
                    self._cond.wait(left)
                    continue
            self.depose(
                f"its renew deadline passed: no renewal for {self.renew_deadline_s:g} s", epoch
            )

    # -- the end of a term --------------------------------------------------------------------
    def depose(self, reason: str, epoch: int | None = None) -> bool:
        """Depose the leader - of ``epoch`` when given - from any thread (SDD §8): its writes are
        refused from now, its playbooks' process groups killed from this thread, never waiting for
        the event loop, then its orchestrator's stop scheduled. False when it did not lead."""
        with self._cond:
            held = self._epoch
            if held is None or (epoch is not None and held != epoch):
                return False
            self.last_deposition = Deposition(held, reason, self._monotonic(), self._deadline)
            self._epoch = None
            self.store.epoch = None
            self._cond.notify_all()
        killed = self.groups.kill()
        log.warning(
            "%s was deposed from the orchestrator lease of epoch %d: %s; %d playbook process "
            "group(s) killed",
            self.lease.holder,
            held,
            reason,
            len(killed),
        )
        self.on_deposed(held, reason)
        return True

    def release(self) -> Event | None:
        """After the orchestrator stopped and the threads ended: expire the lease now and write
        leader.released in one transaction, where the lease is still this replica's (SDD §8, §11);
        the event as written, None when the replica no longer led or the release failed - the
        lease then lapses by itself."""
        with self._cond:
            epoch = self._epoch
            self._epoch = None
            self.store.epoch = None
            self._cond.notify_all()
        if epoch is None:
            return None
        written: list[Event] = []

        def record(conn: sa.Connection) -> None:
            written.append(write_event(conn, released_event(self.lease.holder, epoch)))

        try:
            released = self.lease.release(epoch, record=record)
        except Exception as exc:  # noqa: BLE001 - the stop goes on; the lease lapses by itself
            log.warning(
                "could not release the orchestrator lease of epoch %d (%s); it lapses by itself",
                epoch,
                _why(exc),
            )
            return None
        if not released:
            log.warning("the orchestrator lease of epoch %d was no longer this replica's", epoch)
            return None
        log.info("%s released the orchestrator lease of epoch %d", self.lease.holder, epoch)
        return written[0] if written else None


class Leadership:
    """A server replica's orchestrator under the lease (SDD §8, §25.3-§25.5).

    Built beside the orchestrator: it hands the store its fence's refusals, the orchestrator itself
    as the leadership its check asks, and closes the book of process groups while the replica
    follows. ``start()`` starts the elector and waits for its first try; ``stop()`` is the
    server's stop."""

    def __init__(
        self,
        store: Store,
        orchestrator: Any,
        *,
        holder: str,
        address: str,
        lease_s: float,
        retry_s: float | None = None,
        renew_deadline_s: float | None = None,
        start_hooks: Sequence[Callable[[], Awaitable[None]]] = (),
        notifier: Any = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.orchestrator = orchestrator
        self.notifier = notifier
        self._start_hooks = list(start_hooks)
        retry = lease_module.RETRY_S if retry_s is None else float(retry_s)
        self.engine = lease_engine(store.url, retry)
        self.elector = Elector(
            Lease(self.engine, holder, address, lease_s, retry_s=retry),
            store,
            orchestrator.process_groups,
            healthy=self._healthy,
            report=self._report,
            renew_deadline_s=renew_deadline_s,
            monotonic=monotonic,
        )
        self.elector.on_acquired = self._acquired
        self.elector.on_deposed = self._deposed
        self._loop: asyncio.AbstractEventLoop | None = None
        self._transitions: asyncio.Lock | None = None
        self._pending: set[concurrent.futures.Future[None]] = set()
        self._in_office = False
        self._closing = False
        self._stopped = False
        store.on_fenced = self._fenced
        orchestrator.leadership = self
        orchestrator.process_groups.kill()  # closed: a follower runs no playbook (SDD §7.1)

    @property
    def holder(self) -> str:
        return self.elector.lease.holder

    @property
    def epoch(self) -> int | None:
        return self.elector.epoch

    @property
    def last_deposition(self) -> Deposition | None:
        return self.elector.last_deposition

    def leads(self) -> bool:
        return self.elector.leads()

    def campaigning(self) -> bool:
        """Whether this replica's campaign is alive: a follower's readiness (SDD §12)."""
        return self.elector.campaigning()

    def row(self) -> LeaseRow | None:
        """The lease row as the store's engine reads it now - not the elector's, whose one
        connection is its own (SDD §8): what /health and the relay read (§12)."""
        return read_lease(self.store.engine)

    def depose(self, reason: str) -> None:
        self.elector.depose(reason)

    def _fenced(self, refused: Fenced) -> None:
        self.elector.depose(f"the fence refused a write: {refused}")

    def _healthy(self) -> bool:
        return bool(self.orchestrator.health()["healthy"])

    def _report(self) -> dict[str, Any]:
        return dict(self.orchestrator.health())

    # -- the server's life cycle --------------------------------------------------------------
    async def start(self) -> None:
        """Start the elector and wait for its first try (SDD §8, §15): a replica that takes the
        lease runs its takeover before it serves; one that finds it held serves at once."""
        self._loop = asyncio.get_running_loop()
        self._transitions = asyncio.Lock()
        self.elector.start()
        await asyncio.to_thread(self.elector.wait_first_try, FIRST_TRY_S)
        await self._settle()

    async def stop(self) -> None:
        """The server's stop (SDD §8): the notifier and the orchestrator stop - the lease renewed
        meanwhile -, then the elector's threads end, then the lease is released with
        leader.released in one transaction, unless the replica no longer leads."""
        if self._stopped:
            return
        self._stopped = True
        self._closing = True
        self.elector.stopping()
        if self._transitions is not None:
            async with self._transitions:
                await self._stop_work()
        else:
            await self._stop_work()
        await asyncio.to_thread(self.elector.close)
        event = await asyncio.to_thread(self.elector.release)
        if event is not None:
            await self._publish(event)
        await self._settle()
        await asyncio.to_thread(self.engine.dispose)

    # -- the terms ------------------------------------------------------------------------------
    def _acquired(self, epoch: int, event: Event | None) -> None:
        self._schedule(self._take_office(epoch, event))

    def _deposed(self, epoch: int, reason: str) -> None:
        self._schedule(self._leave_office())

    def _schedule(self, work: Coroutine[Any, Any, None]) -> None:
        """Run ``work`` on the event loop, from whatever thread: a blocked loop runs it later."""
        loop = self._loop
        if loop is None or loop.is_closed():
            work.close()
            return
        try:
            future = asyncio.run_coroutine_threadsafe(work, loop)
        except RuntimeError:  # the loop is closing
            work.close()
            return
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)

    async def _settle(self) -> None:
        """Wait for the terms' starts and stops scheduled so far."""
        for future in list(self._pending):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wrap_future(future)

    async def _take_office(self, epoch: int, event: Event | None) -> None:
        """The takeover (SDD §8): the start hooks - the demo seed -, the orchestrator's start(),
        the notifier."""
        assert self._transitions is not None
        async with self._transitions:
            if event is not None:
                await self._publish(event)
            if self._closing or self.elector.epoch != epoch:
                return  # stopped or deposed meanwhile: its stop follows or ran
            self._in_office = True
            try:
                for hook in self._start_hooks:
                    await hook()
                await self.orchestrator.start()
                if self.notifier is not None:
                    await self.notifier.start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("the orchestrator's start at epoch %d failed", epoch)
                self.elector.depose(f"its start failed ({type(exc).__name__})", epoch)

    async def _leave_office(self) -> None:
        """A deposition's stop (SDD §8): the notifier and the orchestrator; then the replica may
        campaign again."""
        assert self._transitions is not None
        async with self._transitions:
            try:
                await self._stop_work()
            finally:
                self.elector.idle()

    async def _stop_work(self) -> None:
        if not self._in_office:
            return
        self._in_office = False
        if self.notifier is not None:
            await self.notifier.stop()
        await self.orchestrator.stop()

    async def _publish(self, event: Event) -> None:
        """A leader event its own transaction wrote, to this replica's streams (SDD §12)."""
        bus = self.orchestrator.bus
        async with bus.emit_lock:
            bus.publish(event, persist=True)


__all__ = [
    "RENEW_SHARE",
    "Deposition",
    "Elector",
    "Leadership",
    "acquired_event",
    "released_event",
]
