"""The notifier (SDD §8.7, §24.6, §24.7, plan task NT5): cursors, routing, retries, pacing,
expiry, the dedupe key, the restart, and what is never notified - against fake senders."""

import asyncio
import contextlib
from collections.abc import Mapping
from datetime import timedelta

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import Phase, Strategy
from seamless_migrate.domain.models import (
    Event,
    NotificationChannel,
    NotificationCursor,
    NotificationSubscription,
    utcnow,
)
from seamless_migrate.events import EventBus, emit
from seamless_migrate.notify.notifier import Notifier, SendResult, Timing
from seamless_migrate.store import NotFound, Store
from tests.factories import make_migration, make_plan

FAST = Timing(
    tick_s=0.02,
    backoff_s=(0.03, 0.05, 0.08, 0.1),
    jitter=0.0,
    retry_after_max_s=0.2,
    pace_s=0.01,
    expiry_s=3600.0,
    cursor_write_s=0.05,
    restart_s=0.05,
)


class FakeSender:
    """Records every send; answers the scripted results, then ``sent``."""

    def __init__(self, *results: SendResult, hang: bool = False) -> None:
        self.results = list(results)
        self.hang = hang
        self.sent: list[tuple[str, str, str]] = []  # (channel, key, title)
        self.secrets: list[Mapping[str, str] | None] = []
        self.times: list[float] = []

    async def send(self, channel, message, key, secret):
        if self.hang:
            await asyncio.Event().wait()
        self.times.append(asyncio.get_running_loop().time())
        self.sent.append((channel.name, key, message.title))
        self.secrets.append(secret)
        return self.results.pop(0) if self.results else SendResult("sent")


class Lab:
    def __init__(self, tmp_path, timing=FAST, now=None):
        self.settings = Settings(data_dir=tmp_path / "data", secrets_dir=tmp_path / "secrets")
        self.store = Store(f"sqlite:///{tmp_path / 'n.db'}")
        self.store.create_schema()
        self.bus = EventBus(self.store)
        self.sender = FakeSender()
        self.notifier = Notifier(
            self.store,
            self.bus,
            self.settings,
            {"slack": self.sender, "webhook": self.sender},
            timing=timing,
            now=now or utcnow,
        )
        self.plan = make_plan(name="Finance apps")
        self.store.put("plan", self.plan)
        self.m = make_migration(plan_id=self.plan.id, strategy=Strategy.warm, phase=Phase.cutover)
        self.store.put("migration", self.m)

    def channel(self, name="ops-slack", kind="slack", **kw):
        channel = NotificationChannel(name=name, kind=kind, **kw)
        self.store.put("notification_channel", channel)
        self.store.append_event(
            Event(kind="channel.created", data={"name": name, "kind": kind}, actor="admin")
        )
        return channel

    def subscribe(self, channel, plan_id=None, topics=None, every=False):
        sub = NotificationSubscription(
            channel=channel,
            plan_id=None if every else (plan_id or self.plan.id),
            topics=topics or ["approval", "downtime", "problem"],
            created_by="op",
        )
        self.store.put("notification_subscription", sub)
        return sub

    def event(self, kind, ts=None, **data):
        e = Event(
            kind=kind,
            plan_id=self.plan.id,
            migration_id=self.m.id if kind.startswith("migration.") else None,
            message="x",
            data=data,
            ts=ts or utcnow(),
        )
        return self.store.append_event(e)

    def downtime(self, n=1, ts=None):
        return [self.event("migration.downtime_started", ts=ts) for _ in range(n)]

    def outcomes(self, channel=None):
        return [
            e
            for e in self.store.events(since_seq=0, limit=100000)
            if e.kind.startswith("notification.")
            and (channel is None or e.data["channel"] == channel)
        ]

    def cursor(self, name):
        return self.store.get("notification_cursor", name, NotificationCursor)

    async def wait_outcomes(self, n, channel=None, timeout=20.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            seen = self.outcomes(channel)
            if len(seen) >= n:
                return seen
            await asyncio.sleep(0.02)
        raise AssertionError(f"{n} outcome(s) expected, got {self.outcomes(channel)}")

    async def settle(self, seconds=0.15):
        await asyncio.sleep(seconds)


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


async def run(lab, body):
    await lab.notifier.start()
    try:
        return await body()
    finally:
        await lab.notifier.stop()
        lab.store.dispose()


async def test_a_channel_sends_in_event_order(lab):
    """SDD §24.6: a channel's messages go one at a time in seq order, each with the key
    <channel>_<seq>, and the outcome's event and the cursor are one write."""
    lab.channel()
    lab.subscribe("ops-slack")
    events = lab.downtime(3)

    async def body():
        seen = await lab.wait_outcomes(3)
        assert [e.kind for e in seen] == ["notification.sent"] * 3
        assert [k for _, k, _ in lab.sender.sent] == [f"ops-slack_{e.seq}" for e in events]
        assert [e.data["event_seq"] for e in seen] == [e.seq for e in events]
        assert seen[0].data["trigger"] == "downtime.started" and seen[0].data["tries"] == 1
        assert seen[0].plan_id == lab.plan.id and seen[0].migration_id == lab.m.id
        assert seen[0].message.startswith("web-01 is down for its cutover -> ops-slack")
        cursor = lab.cursor("ops-slack")
        assert cursor.seq == events[-1].seq and cursor.last_outcome == "sent"
        assert cursor.failures_in_a_row == 0 and cursor.last_outcome_at is not None
        assert lab.notifier.counts[("slack", "sent")] == 3

    await run(lab, body)


async def test_a_failing_channel_holds_back_no_other_channel(lab):
    """SDD §24.6: a channel that fails holds back its own messages, never another channel's."""
    slow = FakeSender(hang=True)
    lab.notifier.senders["webhook"] = slow
    lab.channel("cmdb", "webhook", url="https://cmdb.example.com/h")
    lab.channel()
    lab.subscribe("cmdb")
    lab.subscribe("ops-slack")
    lab.downtime(2)

    async def body():
        seen = await lab.wait_outcomes(2, "ops-slack")
        assert len(seen) == 2 and lab.outcomes("cmdb") == []

    await run(lab, body)


async def test_a_temporary_failure_is_retried_with_backoff_and_retry_after(lab):
    """SDD §24.6: a retryable answer is tried again, 5 tries at most, the backoff growing, a
    Retry-After honoured up to its cap; the outcome counts the tries."""
    lab.sender = FakeSender(
        SendResult("retry", "503"),
        SendResult("retry", "429", retry_after_s=0.12),
        SendResult("sent"),
    )
    lab.notifier.senders["slack"] = lab.sender
    lab.channel()
    lab.subscribe("ops-slack")
    lab.downtime(1)

    async def body():
        [seen] = await lab.wait_outcomes(1)
        assert seen.kind == "notification.sent" and seen.data["tries"] == 3
        gaps = [b - a for a, b in zip(lab.sender.times, lab.sender.times[1:], strict=False)]
        assert gaps[0] >= 0.03 and gaps[1] >= 0.12, gaps  # the backoff, then the Retry-After
        assert len({k for _, k, _ in lab.sender.sent}) == 1  # the same key every try

    await run(lab, body)
    # five retryable answers: failed after the fifth, with the last reason
    (lab.settings.data_dir.parent / "two").mkdir()
    lab2 = Lab(lab.settings.data_dir.parent / "two")
    lab2.sender = FakeSender(*[SendResult("retry", "timeout")] * 6)
    lab2.notifier.senders["slack"] = lab2.sender
    lab2.channel()
    lab2.subscribe("ops-slack")
    lab2.downtime(1)

    async def body2():
        [seen] = await lab2.wait_outcomes(1)
        assert seen.kind == "notification.failed"
        assert seen.data["tries"] == 5 and seen.data["reason"] == "timeout"
        assert lab2.cursor("ops-slack").failures_in_a_row == 1
        assert lab2.notifier.counts[("slack", "failed")] == 1

    await run(lab2, body2)


async def test_a_final_answer_is_not_retried(lab):
    """SDD §24.6: a final answer - another 4xx, a 3xx, an SMTP 5yz, a missing secret or relay -
    is recorded failed at once, and the channel goes on to the next message."""
    lab.sender = FakeSender(SendResult("failed", "404"), SendResult("sent"))
    lab.notifier.senders["slack"] = lab.sender
    lab.channel()
    lab.subscribe("ops-slack")
    lab.downtime(2)

    async def body():
        seen = await lab.wait_outcomes(2)
        assert [e.kind for e in seen] == ["notification.failed", "notification.sent"]
        assert seen[0].data["tries"] == 1 and seen[0].data["reason"] == "404"
        assert len(lab.sender.sent) == 2
        assert lab.cursor("ops-slack").last_outcome == "sent"

    await run(lab, body)
    # a kind without a sender fails with no_sender
    (lab.settings.data_dir.parent / "two").mkdir()
    lab2 = Lab(lab.settings.data_dir.parent / "two")
    lab2.channel("owners", "email", recipients=["o@example.com"])
    lab2.subscribe("owners")
    lab2.downtime(1)

    async def body2():
        [seen] = await lab2.wait_outcomes(1)
        assert seen.kind == "notification.failed" and seen.data["reason"] == "no_sender"

    await run(lab2, body2)


async def test_a_message_whose_event_is_an_hour_old_expires(lab):
    """SDD §24.6 (owner decision 9): a message whose event is more than an hour old when its turn
    comes is not sent; it is recorded failed with the reason expired."""
    lab.channel()
    lab.subscribe("ops-slack")
    old = utcnow() - timedelta(hours=1, minutes=1)
    lab.downtime(1, ts=old)
    fresh = lab.downtime(1)

    async def body():
        seen = await lab.wait_outcomes(2)
        assert [e.kind for e in seen] == ["notification.failed", "notification.sent"]
        assert seen[0].data["reason"] == "expired" and seen[0].data["expired"] == 1
        assert [k for _, k, _ in lab.sender.sent] == [f"ops-slack_{fresh[0].seq}"]

    await run(lab, body)


async def test_a_run_of_expired_messages_is_one_event(lab):
    """SDD §24.6: a run of expired messages is one notification.failed with their number and
    their first and last seq."""
    lab.channel()
    lab.subscribe("ops-slack")
    old = utcnow() - timedelta(hours=2)
    stale = lab.downtime(4, ts=old)

    async def body():
        [seen] = await lab.wait_outcomes(1)
        assert seen.kind == "notification.failed" and seen.data["reason"] == "expired"
        assert seen.data["expired"] == 4
        assert (seen.data["first_seq"], seen.data["last_seq"]) == (stale[0].seq, stale[-1].seq)
        assert lab.sender.sent == []
        assert lab.cursor("ops-slack").seq == stale[-1].seq

    await run(lab, body)


async def test_a_restart_resends_an_unrecorded_message_with_its_key(lab):
    """SDD §24.6: the cursor moves with the outcome's event; a notifier that dies between the
    send and the write - here the write fails once - sends again from the cursor after its
    restart, with the same key, and a recorded message is never sent twice."""
    lab.channel()
    lab.subscribe("ops-slack")
    first, second = lab.downtime(2)
    original = lab.store.put_with_event
    crashed = {"n": 0}

    def put_with_event(kind, model, event, expected_version):
        if kind == "notification_cursor" and model.seq == second.seq and not crashed["n"]:
            crashed["n"] += 1
            raise RuntimeError("the process died between the send and the write")
        return original(kind, model, event, expected_version)

    lab.store.put_with_event = put_with_event

    async def body():
        seen = await lab.wait_outcomes(2)
        assert [e.data["event_seq"] for e in seen] == [first.seq, second.seq]
        keys = [k for _, k, _ in lab.sender.sent]
        assert keys == [
            f"ops-slack_{first.seq}",
            f"ops-slack_{second.seq}",
            f"ops-slack_{second.seq}",
        ]
        assert crashed["n"] == 1

    await run(lab, body)


async def test_a_channel_two_subscriptions_match_sends_once(lab):
    """SDD §24.4: a plan's and an every-plan subscription of one channel match the same event:
    the channel sends it once, for the union of their topics."""
    lab.channel()
    lab.subscribe("ops-slack", topics=["downtime"])
    lab.subscribe("ops-slack", every=True, topics=["outcome"])
    lab.downtime(1)
    lab.event("migration.phase", **{"from": "verifying", "to": "completed", "reason": "ok"})
    lab.event(
        "migration.phase", **{"from": "cutover", "to": "failed", "reason": "boom"}
    )  # no topic

    async def body():
        await lab.wait_outcomes(2)
        await lab.settle()
        assert [e.data["trigger"] for e in lab.outcomes()] == [
            "downtime.started",
            "outcome.completed",
        ]
        assert len(lab.sender.sent) == 2

    await run(lab, body)


async def test_a_new_channel_starts_at_its_creation(lab):
    """SDD §24.6: a new channel's cursor starts at its own channel.created event, so it never
    receives the history."""
    lab.downtime(2)  # before the channel exists
    lab.channel()
    lab.subscribe("ops-slack")
    later = lab.downtime(1)

    async def body():
        [seen] = await lab.wait_outcomes(1)
        await lab.settle()
        assert [e.data["event_seq"] for e in lab.outcomes()] == [later[0].seq]

    await run(lab, body)


async def test_a_disabled_channel_queues_nothing(lab):
    """SDD §24.6: a disabled channel's cursor follows the events and nothing is queued for it;
    enabled again, it sends from there on."""
    lab.channel(enabled=False)
    lab.subscribe("ops-slack")
    skipped = lab.downtime(2)

    async def body():
        await lab.settle(0.3)
        assert lab.sender.sent == [] and lab.outcomes() == []
        assert lab.cursor("ops-slack").seq == skipped[-1].seq
        channel = lab.store.get("notification_channel", "ops-slack", NotificationChannel)
        lab.store.put("notification_channel", channel.model_copy(update={"enabled": True}))
        fresh = lab.downtime(1)
        [seen] = await lab.wait_outcomes(1)
        assert seen.data["event_seq"] == fresh[0].seq

    await run(lab, body)


async def test_notification_events_are_never_notified(lab):
    """SDD §24.7: neither the notification events nor the catalog's are ever notified, whatever
    the subscriptions say."""
    lab.channel()
    lab.subscribe(
        "ops-slack", every=True, topics=["approval", "downtime", "problem", "outcome", "progress"]
    )
    lab.downtime(1)

    async def body():
        await lab.wait_outcomes(1)
        lab.store.append_event(
            Event(
                kind="channel.updated",
                plan_id=lab.plan.id,
                data={"name": "ops-slack", "kind": "slack"},
            )
        )
        lab.store.append_event(
            Event(
                kind="subscription.created",
                plan_id=lab.plan.id,
                data={"id": "sub-1", "channel": "ops-slack"},
            )
        )
        await lab.settle(0.3)
        assert len(lab.outcomes()) == 1 and len(lab.sender.sent) == 1
        assert lab.cursor("ops-slack").seq == lab.store.max_seq()

    await run(lab, body)


async def test_the_clis_events_are_notified_by_the_server(lab):
    """SDD §24.6: the CLI sends nothing; the server notifies the CLI's events, which it reads from
    the store - an event written outside the bus reaches the channel."""
    lab.channel()
    lab.subscribe("ops-slack")

    async def body():
        await lab.settle()
        other = Store(f"sqlite:///{lab.settings.data_dir.parent / 'n.db'}")  # the CLI's store
        other.append_event(
            Event(
                kind="migration.downtime_started",
                plan_id=lab.plan.id,
                migration_id=lab.m.id,
                actor="cli",
            )
        )
        other.dispose()
        [seen] = await lab.wait_outcomes(1)
        assert seen.data["trigger"] == "downtime.started"

    await run(lab, body)


async def test_a_channel_sends_at_most_one_message_a_second(tmp_path):
    """SDD §24.6: a channel sends at most one message a second; a burst waits behind it."""
    lab = Lab(tmp_path, timing=Timing(**{**FAST.__dict__, "pace_s": 0.1}))
    lab.channel()
    lab.subscribe("ops-slack")
    lab.downtime(3)

    async def body():
        await lab.wait_outcomes(3)
        gaps = [b - a for a, b in zip(lab.sender.times, lab.sender.times[1:], strict=False)]
        assert all(gap >= 0.1 for gap in gaps), gaps

    await run(lab, body)


async def test_the_notifier_starts_again_after_an_unexpected_error(lab):
    """SDD §24.6: an unexpected error of the notifier's own is logged, and it starts again from
    the cursors after a backoff; the server never stops."""
    lab.channel()
    lab.subscribe("ops-slack")
    lab.downtime(1)
    calls = {"n": 0}
    original = lab.notifier._messages_for

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("a bug of the notifier's own")
        return await original(*args, **kwargs)

    lab.notifier._messages_for = flaky

    async def body():
        [seen] = await lab.wait_outcomes(1)
        assert seen.kind == "notification.sent" and calls["n"] >= 2
        assert lab.notifier._main is not None and not lab.notifier._main.done()

    await run(lab, body)


async def test_a_test_message_is_recorded_with_the_trigger_test(lab):
    """SDD §8.7: the test route's message is one try, recorded as a delivery with the trigger
    test, and the cursor does not move."""
    channel = lab.channel()
    lab.sender = FakeSender(SendResult("failed", "invalid_payload"))
    lab.notifier.senders["slack"] = lab.sender
    failed = await lab.notifier.send_test(channel)
    assert failed == {"outcome": "failed", "reason": "invalid_payload"}
    sent = await lab.notifier.send_test(channel)
    assert sent == {"outcome": "sent", "reason": None}
    seen = lab.outcomes()
    assert [e.kind for e in seen] == ["notification.failed", "notification.sent"]
    assert all(e.data["trigger"] == "test" and e.data["tries"] == 1 for e in seen)
    assert lab.sender.sent[0][2] == "Seamless Migrate test message"
    with pytest.raises(NotFound):
        lab.cursor("ops-slack")
    assert lab.notifier.counts == {("slack", "failed"): 1, ("slack", "sent"): 1}
    lab.store.dispose()


async def test_an_event_written_after_a_channel_is_read_waits_for_the_next_pass(lab):
    """SDD §24.6: a pass judges only the events written before it read the channel. A channel
    enabled while a pass is in flight - read disabled, then an event written - must not lose
    that event to the disabled reading: the next pass, which reads the channel enabled, sends it."""
    lab.channel(enabled=False)
    lab.subscribe("ops-slack")
    original_get = lab.store.get
    fresh: list = []

    def get(kind, id_, model_cls):
        found = original_get(kind, id_, model_cls)
        if kind == "notification_channel" and not fresh and not found.enabled:
            # the pass has read the channel disabled; now the admin enables it and an event lands
            lab.store.put("notification_channel", found.model_copy(update={"enabled": True}))
            fresh.extend(lab.downtime(1))
        return found

    lab.store.get = get

    async def body():
        [seen] = await lab.wait_outcomes(1)
        assert seen.kind == "notification.sent" and seen.data["event_seq"] == fresh[0].seq

    await run(lab, body)


@pytest.mark.parametrize(
    ("hours_old", "other_writes"),
    [(0, "while_it_is_written"), (2, "while_it_is_written"), (0, "before_its_write")],
    ids=["sent", "expired", "sent-after-a-writer-that-held-the-lock"],
)
async def test_an_outcome_event_is_timed_in_seq_order_with_the_others(lab, hours_old, other_writes):
    """SDD §4.2: an event's ts is taken under the lock that writes and numbers the events, so
    their order in time is their seq order - the Events page lists events by time. An outcome's
    event, a message's or a run of expired ones', was timed before that lock and written outside
    it: an event another writer wrote meanwhile - another channel's outcome - took the earlier
    seq and the later time, and the page listed the two out of seq order (plan task NT10: the
    demo's channels did it about once in fifteen messages). Taken under the lock, the time also
    follows an event a writer that held the lock wrote while the outcome was being built."""
    lab.channel()
    lab.subscribe("ops-slack")
    lab.downtime(1, ts=utcnow() - timedelta(hours=hours_old))
    loop = asyncio.get_running_loop()
    original = lab.store.put_with_event
    other = []
    sent = asyncio.Event()

    def put_with_event(kind, model, event, expected_version):
        if kind == "notification_cursor" and not other:
            # another writer emits while the outcome is being written: it waits for the lock
            # that write holds, or - nothing held - writes first
            other.append(
                asyncio.run_coroutine_threadsafe(
                    emit(lab.store, lab.bus, "plan.paused", "paused", plan_id=lab.plan.id), loop
                )
            )
            with contextlib.suppress(TimeoutError):
                other[0].result(timeout=0.5)
        return original(kind, model, event, expected_version)

    async def send(channel, message, key, secret):
        result = await FakeSender.send(lab.sender, channel, message, key, secret)
        sent.set()  # the notifier builds the outcome next, and runs on to its write
        return result

    if other_writes == "while_it_is_written":
        lab.store.put_with_event = put_with_event
    else:
        lab.sender.send = send

    async def body():
        if other_writes == "before_its_write":
            async with lab.bus.emit_lock:  # a writer holds the lock while the outcome is built
                await sent.wait()
                # written now, while the outcome waits for the lock: timed after its building
                paused = lab.store.append_event(
                    Event(kind="plan.paused", plan_id=lab.plan.id, message="paused")
                )
        [outcome] = await lab.wait_outcomes(1)
        if other_writes == "while_it_is_written":
            paused = await asyncio.wrap_future(other[0])
        assert outcome.kind == ("notification.failed" if hours_old else "notification.sent")
        assert (outcome.data["reason"] == "expired") == bool(hours_old)
        by_time = sorted([outcome, paused], key=lambda e: (e.ts, e.seq))
        assert [e.seq for e in by_time] == sorted([outcome.seq, paused.seq]), [
            (e.kind, e.seq, e.ts) for e in by_time
        ]

    await run(lab, body)
