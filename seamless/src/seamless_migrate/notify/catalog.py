"""The catalog's audit events (SDD §4.3, §24.7): what a change to a channel or a subscription
records, alike for the routes (plan task NT3) and the demo's seed (NT10). None carries a URL, a
recipient or a secret's value."""

from __future__ import annotations

from typing import Any

from ..domain.models import Event, NotificationChannel, NotificationSubscription
from ..events import new_event


def channel_event(kind: str, channel: NotificationChannel, actor: str, **data: Any) -> Event:
    """A channel.* event: the channel's name and kind, and what ``data`` adds - the fields an
    update set, a secret a deletion left."""
    verb = kind.split(".", 1)[1]
    return new_event(
        kind,
        f"notification channel {channel.name} {verb}",
        actor=actor,
        data={"name": channel.name, "kind": channel.kind, **data},
    )


def subscription_event(kind: str, sub: NotificationSubscription, actor: str) -> Event:
    """A subscription.* event: the subscription, its channel, its plan (null for every plan) and
    its topics."""
    verb = kind.split(".", 1)[1]
    where = "every plan" if sub.plan_id is None else f"plan {sub.plan_id}"
    return new_event(
        kind,
        f"subscription {sub.id} of channel {sub.channel} to {where} {verb}",
        plan_id=sub.plan_id,
        actor=actor,
        data={
            "id": sub.id,
            "channel": sub.channel,
            "plan_id": sub.plan_id,
            "topics": [str(topic) for topic in sub.topics],
        },
    )
