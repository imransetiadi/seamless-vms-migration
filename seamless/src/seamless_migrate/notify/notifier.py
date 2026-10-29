"""The notifier (SDD §8.7, §24.6, §24.7, plan task NT5): one task per channel that reads the
store after the channel's cursor, routes each event through the channel's subscriptions, sends
what matches one message at a time through the kind's sender, and records each message's
outcome with the cursor in one store transaction. Never in a migration's path."""

from __future__ import annotations

import asyncio
import logging
import random
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from ..config import Settings
from ..domain.enums import NotifyTopic, NotifyTrigger
from ..domain.models import (
    Event,
    Migration,
    NotificationChannel,
    NotificationCursor,
    NotificationSubscription,
    Plan,
    utcnow,
)
from ..events import EventBus, emit
from ..security.secret_store import SecretStoreError, secret_store
from ..store import ConflictError, NotFound, Store
from .messages import Message, build_message, triggers_for

log = logging.getLogger(__name__)

#: the kinds the notifier itself writes or the catalog writes: never notified (SDD §24.7)
NEVER_NOTIFIED = ("notification.", "channel.", "subscription.")


@dataclass(frozen=True)
class SendResult:
    """What a sender answers (SDD §8.7): final at once, or to be tried again."""

    outcome: Literal["sent", "failed", "retry"]
    reason: str | None = None
    #: a Retry-After the answer carried, in seconds
    retry_after_s: float | None = None
    #: an email's refused recipients
    refused: int = 0


class Sender(Protocol):
    async def send(
        self,
        channel: NotificationChannel,
        message: Message,
        key: str,
        secret: Mapping[str, str] | None,
    ) -> SendResult: ...


@dataclass(frozen=True)
class Timing:
    """The notifier's clocks (SDD §24.6); the tests shorten them."""

    tick_s: float = 1.0
    backoff_s: tuple[float, ...] = (10.0, 20.0, 40.0, 80.0)
    jitter: float = 0.2
    retry_after_max_s: float = 300.0
    pace_s: float = 1.0
    expiry_s: float = 3600.0
    cursor_write_s: float = 60.0
    restart_s: float = 5.0
    page: int = 500

    @property
    def tries(self) -> int:
        return len(self.backoff_s) + 1


@dataclass
class _Expired:
    count: int = 0
    first_seq: int = 0
    last_seq: int = 0
    plan_id: str | None = None


class Notifier:
    """One notifier per server (SDD §8.7); ``senders`` by channel kind."""

    def __init__(
        self,
        store: Store,
        bus: EventBus,
        settings: Settings,
        senders: Mapping[str, Sender] | None = None,
        *,
        timing: Timing | None = None,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.store = store
        self.bus = bus
        self.settings = settings
        self.senders: dict[str, Sender] = dict(senders or {})
        self.timing = timing or Timing()
        self._now = now
        self._sleep = sleep
        #: (kind, outcome) -> messages recorded (SDD §18)
        self.counts: Counter[tuple[str, str]] = Counter()
        self._main: asyncio.Task[None] | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._stopping = False

    # ------------------------------------------------------------------ the life cycle

    async def start(self) -> None:
        if self._main is None:
            self._stopping = False
            self._main = asyncio.create_task(self._run(), name="notifier")

    async def stop(self) -> None:
        self._stopping = True
        tasks = [*self._tasks.values(), *([self._main] if self._main else [])]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: S110 - stopping
                pass
        self._tasks.clear()
        self._main = None

    def wake(self) -> None:
        """A tick now: the catalog or an event changed."""
        self._wake.set()

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._keep_channel_tasks()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("the notifier could not read the channel catalog; trying again")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.timing.tick_s)
            except TimeoutError:
                pass

    async def _keep_channel_tasks(self) -> None:
        channels = await asyncio.to_thread(
            self.store.list, "notification_channel", NotificationChannel
        )
        names = {c.name for c in channels}
        for name, task in list(self._tasks.items()):
            if name not in names or task.done():
                if not task.done():
                    task.cancel()
                del self._tasks[name]
        for name in names - set(self._tasks):
            self._tasks[name] = asyncio.create_task(self._channel_loop(name), name=f"notify:{name}")

    async def _channel_loop(self, name: str) -> None:
        last_cursor_write = 0.0
        while not self._stopping:
            try:
                last_cursor_write = await self._advance(name, last_cursor_write)
            except asyncio.CancelledError:
                raise
            except NotFound:
                return  # the channel went
            except Exception:
                log.exception(
                    "channel %s: the notifier failed and starts again from the cursor", name
                )
                await self._sleep(self.timing.restart_s)
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), self.timing.tick_s)
            except TimeoutError:
                pass

    # -------------------------------------------------------------------- one channel

    async def _cursor(self, channel: NotificationChannel) -> tuple[NotificationCursor, int]:
        try:
            return await asyncio.to_thread(
                self.store.get_versioned, "notification_cursor", channel.name, NotificationCursor
            )
        except NotFound:
            pass
        # a new channel starts at its own channel.created event, never at the history (§24.6)
        created = await asyncio.to_thread(
            self.store.events, 0, None, None, 100000, ["channel.created"]
        )
        start = max(
            (e.seq for e in created if e.data.get("name") == channel.name),
            default=await asyncio.to_thread(self.store.max_seq),
        )
        cursor = NotificationCursor(channel=channel.name, seq=start)
        try:
            version = await asyncio.to_thread(self.store.put, "notification_cursor", cursor, 0)
        except ConflictError:
            return await asyncio.to_thread(
                self.store.get_versioned, "notification_cursor", channel.name, NotificationCursor
            )
        return cursor, version

    async def _advance(self, name: str, last_cursor_write: float) -> float:
        """One page of the channel's events; returns when the cursor was last written."""
        channel = await asyncio.to_thread(
            self.store.get, "notification_channel", name, NotificationChannel
        )
        cursor, version = await self._cursor(channel)
        events = await asyncio.to_thread(
            self.store.events, cursor.seq, None, None, self.timing.page
        )
        if not events:
            return last_cursor_write
        subs = await asyncio.to_thread(
            self.store.list, "notification_subscription", NotificationSubscription
        )
        subs = [s for s in subs if s.channel == name]
        expired = _Expired()
        written = False
        for event in events:
            messages = []
            if channel.enabled and not event.kind.startswith(NEVER_NOTIFIED):
                messages = await self._messages_for(channel, subs, event)
            if not messages:
                cursor.seq = event.seq
                continue
            if self._expired(event):
                expired.count += 1
                expired.first_seq = expired.first_seq or event.seq
                expired.last_seq = event.seq
                expired.plan_id = event.plan_id
                cursor.seq = event.seq
                continue
            if expired.count:
                version = await self._record_expired(channel, cursor, version, expired)
                expired = _Expired()
            for i, message in enumerate(messages):
                key = f"{name}_{event.seq}" + (f"_{i + 1}" if i else "")
                result, tries = await self._deliver(channel, message, key)
                cursor.seq = event.seq
                version = await self._record(
                    channel, cursor, version, event, message, key, result, tries
                )
                written = True
                await self._sleep(self.timing.pace_s)  # one message a second per channel
        if expired.count:
            version = await self._record_expired(channel, cursor, version, expired)
            written = True
        if written:
            return asyncio.get_running_loop().time()
        now = asyncio.get_running_loop().time()
        if now - last_cursor_write >= self.timing.cursor_write_s:
            await asyncio.to_thread(self.store.put, "notification_cursor", cursor, version)
            return now
        return last_cursor_write

    async def _messages_for(
        self, channel: NotificationChannel, subs: Sequence[NotificationSubscription], event: Event
    ) -> list[Message]:
        topics: set[NotifyTopic] = set()
        for sub in subs:
            if sub.plan_id is None or sub.plan_id == event.plan_id:
                topics.update(sub.topics)
        if not topics or event.plan_id is None:
            return []
        try:
            plan = await asyncio.to_thread(self.store.get, "plan", event.plan_id, Plan)
        except NotFound:
            return []
        migration = None
        if event.migration_id is not None:
            try:
                migration = await asyncio.to_thread(
                    self.store.get, "migration", event.migration_id, Migration
                )
            except NotFound:
                return []
        migrations: list[Migration] = []
        if event.kind in ("wave.started", "plan.started"):
            migrations = await asyncio.to_thread(
                self.store.list, "migration", Migration, plan_id=plan.id
            )
        triggers = triggers_for(event, plan, migration, migrations)
        return [
            build_message(
                trigger,
                event,
                plan,
                migration,
                migrations,
                redact_names=channel.redact_names,
                public_url=self.settings.public_url,
            )
            for trigger in triggers
            if trigger.topic in topics
        ]

    def _expired(self, event: Event) -> bool:
        return (self._now() - event.ts).total_seconds() > self.timing.expiry_s

    async def _secret(self, channel: NotificationChannel) -> Mapping[str, str] | None:
        """The channel's secret, read each time a message is sent (§24.3)."""
        if channel.kind == "email":
            return None
        try:
            return await asyncio.to_thread(secret_store(self.settings).read, channel.secret_name)
        except SecretStoreError as exc:
            log.warning("channel %s: its secret could not be read: %s", channel.name, exc)
            return None

    async def _deliver(
        self, channel: NotificationChannel, message: Message, key: str
    ) -> tuple[SendResult, int]:
        """Up to five tries with backoff and jitter, a Retry-After honoured (§24.6)."""
        sender = self.senders.get(channel.kind)
        if sender is None:
            return SendResult("failed", "no_sender"), 0
        tries = 0
        result = SendResult("failed", "not_tried")
        for attempt in range(self.timing.tries):
            tries += 1
            secret = await self._secret(channel)
            try:
                result = await sender.send(channel, message, key, secret)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = SendResult("retry", type(exc).__name__)
            if result.outcome != "retry" or attempt == self.timing.tries - 1:
                break
            delay = self.timing.backoff_s[attempt]
            delay *= 1 + random.uniform(-self.timing.jitter, self.timing.jitter)  # noqa: S311
            if result.retry_after_s is not None:
                delay = min(max(result.retry_after_s, 0.0), self.timing.retry_after_max_s)
            await self._sleep(delay)
        return result, tries

    def _outcome_event(
        self,
        channel: NotificationChannel,
        message: Message | None,
        *,
        trigger: str,
        event: Event | None,
        key: str,
        result: SendResult,
        tries: int,
        extra: dict[str, Any] | None = None,
    ) -> Event:
        outcome = "sent" if result.outcome == "sent" else "failed"
        title = message.title if message is not None else "expired"
        data: dict[str, Any] = {
            "channel": channel.name,
            "kind": channel.kind,
            "trigger": trigger,
            "event_seq": event.seq if event is not None else None,
            "key": key,
            "tries": tries,
            "reason": result.reason if outcome == "failed" else None,
            "refused": result.refused,
            **(extra or {}),
        }
        self.counts[(channel.kind, outcome)] += 1
        return Event(
            kind=f"notification.{outcome}",
            plan_id=event.plan_id if event is not None else None,
            migration_id=event.migration_id if event is not None else None,
            actor="system",
            message=f"{title} -> {channel.name}",
            data=data,
        )

    async def _record(
        self,
        channel: NotificationChannel,
        cursor: NotificationCursor,
        version: int,
        event: Event,
        message: Message,
        key: str,
        result: SendResult,
        tries: int,
    ) -> int:
        """The outcome's event and the cursor past the event in one transaction (§11, §24.6)."""
        outcome = self._outcome_event(
            channel,
            message,
            trigger=message.trigger_name,
            event=event,
            key=key,
            result=result,
            tries=tries,
        )
        cursor.last_outcome = "sent" if result.outcome == "sent" else "failed"
        cursor.last_outcome_at = self._now()
        cursor.last_reason = outcome.data["reason"]
        cursor.failures_in_a_row = (
            0 if cursor.last_outcome == "sent" else cursor.failures_in_a_row + 1
        )
        version, _ = await asyncio.to_thread(
            self.store.put_with_event, "notification_cursor", cursor, outcome, version
        )
        await self.bus.catch_up()  # the streams read the outcome from the store (§12)
        return version

    async def _record_expired(
        self,
        channel: NotificationChannel,
        cursor: NotificationCursor,
        version: int,
        expired: _Expired,
    ) -> int:
        """A run of expired messages is one notification.failed (§24.6)."""
        outcome = self._outcome_event(
            channel,
            None,
            trigger="expired",
            event=None,
            key=f"{channel.name}_{expired.first_seq}",
            result=SendResult("failed", "expired"),
            tries=0,
            extra={
                "expired": expired.count,
                "first_seq": expired.first_seq,
                "last_seq": expired.last_seq,
            },
        )
        outcome.plan_id = expired.plan_id
        outcome.message = f"{expired.count} message(s) expired -> {channel.name}"
        cursor.last_outcome, cursor.last_reason = "failed", "expired"
        cursor.last_outcome_at = self._now()
        cursor.failures_in_a_row += 1
        version, _ = await asyncio.to_thread(
            self.store.put_with_event, "notification_cursor", cursor, outcome, version
        )
        await self.bus.catch_up()
        return version

    # ------------------------------------------------------------------- the test route

    async def send_test(self, channel: NotificationChannel) -> dict[str, Any]:
        """A fixed message through the channel at once, one try (§8.7, §24.3), recorded as a
        delivery with the trigger ``test``; the cursor does not move."""
        message = Message(
            trigger="test",
            title="Seamless Migrate test message",
            lines=[f"Channel {channel.name} ({channel.kind}) is reachable from the control plane."],
            plan_id="-",
            plan_name="test",
            event_seq=0,
            event_kind="test",
            event_ts=self._now(),
        )
        sender = self.senders.get(channel.kind)
        key = f"{channel.name}_test_{int(self._now().timestamp())}"
        if sender is None:
            result = SendResult("failed", "no_sender")
        else:
            try:
                result = await sender.send(channel, message, key, await self._secret(channel))
            except Exception as exc:
                result = SendResult("failed", type(exc).__name__)
        outcome = self._outcome_event(
            channel, message, trigger="test", event=None, key=key, result=result, tries=1
        )
        await emit(
            self.store,
            self.bus,
            outcome.kind,
            outcome.message,
            actor="system",
            data=outcome.data,
        )
        return {
            "outcome": "sent" if result.outcome == "sent" else "failed",
            "reason": result.reason if result.outcome != "sent" else None,
        }


def senders_for(settings: Settings) -> dict[str, Sender]:
    """The senders the server runs with (SDD §8.7): the email sender (plan task NT6) and the Slack
    sender (NT7); the webhook sender follows with NT8, and until then its kind fails with
    ``no_sender``."""
    from .email import EmailSender  # the senders import SendResult from here
    from .slack import SlackSender

    return {"email": EmailSender(settings), "slack": SlackSender(settings)}


__all__ = ["NotifyTrigger", "Notifier", "SendResult", "Sender", "Timing", "senders_for"]
