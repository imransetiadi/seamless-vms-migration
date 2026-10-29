"""The demo's senders (SDD §7.4, §8.7, §24.9, plan task NT10): in demo mode every channel records
instead of sending (§24.11 decision 11). A recording sender renders its kind's format as the real
sender renders it - the email as the relay would receive it, the Slack payload as JSON, the
Standard Webhooks body a webhook signs - and answers sent, so the notifier records the message as
notification.sent (§24.7). It opens no connection and needs no secret, so the notifier reads none
for it: nothing leaves the process, whatever a channel names."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

from ..config import Settings
from ..domain.models import NotificationChannel
from .email import render_email
from .messages import Message, slack_payload
from .notifier import SendResult
from .webhook import webhook_body


class RecordingSender:
    """One per channel kind in demo mode (SDD §7.4): it renders the message, and sends nothing."""

    #: the notifier hands this sender no secret and reads none for it (SDD §8.7)
    reads_secret = False

    def __init__(self, kind: Literal["email", "slack", "webhook"], settings: Settings) -> None:
        self.kind = kind
        self.settings = settings

    def render(self, channel: NotificationChannel, message: Message, key: str) -> bytes:
        """The bytes the kind's real sender would send (SDD §24.5): the email as the relay would
        receive it, the Slack payload as httpx encodes a JSON body, the webhook's signed body."""
        if self.kind == "email":
            return render_email(self.settings, channel, message, key).as_bytes()
        if self.kind == "slack":
            payload = slack_payload(message)
            return json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        return webhook_body(message)

    async def send(
        self,
        channel: NotificationChannel,
        message: Message,
        key: str,
        secret: Mapping[str, str] | None,
    ) -> SendResult:
        self.render(channel, message, key)
        return SendResult("sent")
