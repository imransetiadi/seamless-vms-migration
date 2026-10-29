"""The webhook sender (SDD §8.7, §13.3, §24.3, §24.5, §24.6, plan task NT8): one POST of the
Standard Webhooks document through ``httpx``'s asynchronous client, 15 s, no redirect followed,
signed with the channel's ``signing_key``; a new timestamp and signature for each try under the
same ``webhook-id``. The key never leaves the control plane: only the signature travels, and no
reason, log line or exception of this module carries it."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from ..config import Settings
from ..domain.models import NotificationChannel, utcnow
from .messages import Message, webhook_document
from .notifier import SendResult

#: an HTTP request has this long (SDD §24.6)
HTTP_TIMEOUT_S = 15.0
#: the signing key's prefix (SDD §24.3)
SIGNING_KEY_PREFIX = "whsec_"
#: the statuses tried again (SDD §24.6), beside every 5xx
RETRIED_STATUSES = frozenset({408, 429})


def signing_key_bytes(secret: Mapping[str, str] | None) -> bytes | None:
    """The key the secret's ``signing_key`` encodes after ``whsec_`` (SDD §24.3); None when the
    secret is missing, holds no key, or holds one the sender cannot read."""
    value = secret.get("signing_key") if secret else None
    if not isinstance(value, str) or not value.startswith(SIGNING_KEY_PREFIX):
        return None
    try:
        raw = base64.b64decode(value[len(SIGNING_KEY_PREFIX) :], validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw or None


def webhook_body(message: Message) -> bytes:
    """The body the sender signs and POSTs (SDD §8.7, §24.5): the Standard Webhooks document as
    compact JSON in UTF-8 - the bytes the signature covers, and what the demo's recording sender
    renders (§7.4)."""
    return json.dumps(webhook_document(message), separators=(",", ":")).encode("utf-8")


def sign(key: bytes, msg_id: str, timestamp: str, body: bytes) -> str:
    """Standard Webhooks' signature: ``v1,`` and the base64 HMAC-SHA256 of
    ``<id>.<timestamp>.<body>`` (SDD §24.3)."""
    digest = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def retry_after_s(answer: httpx.Response, now: datetime) -> float | None:
    """The answer's ``Retry-After`` in seconds (RFC 9110: a delay or an HTTP-date); None when
    there is none or the sender cannot read it."""
    value = answer.headers.get("Retry-After")
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - now).total_seconds(), 0.0)


class WebhookSender:
    """One per server: the notifier's webhook channels send through it."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
        timeout_s: float = HTTP_TIMEOUT_S,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._now = now or utcnow
        self.timeout_s = timeout_s

    async def send(
        self,
        channel: NotificationChannel,
        message: Message,
        key: str,
        secret: Mapping[str, str] | None,
    ) -> SendResult:
        signing_key = signing_key_bytes(secret)
        if signing_key is None:
            return SendResult("failed", "no_secret")
        body = webhook_body(message)
        now = self._now()
        timestamp = str(int(now.timestamp()))
        headers = {
            "Content-Type": "application/json",
            "webhook-id": key,
            "webhook-timestamp": timestamp,
            "webhook-signature": sign(signing_key, key, timestamp, body),
        }
        try:
            async with httpx.AsyncClient(
                transport=self.transport, follow_redirects=False, timeout=self.timeout_s
            ) as client:
                answer = await client.post(str(channel.url), content=body, headers=headers)
        except httpx.HTTPError as exc:
            # a timeout, a connection or TLS error: its kind, never its text (§13.3)
            return SendResult("retry", type(exc).__name__)
        status = answer.status_code
        if 200 <= status < 300:
            return SendResult("sent")
        if 300 <= status < 400:
            return SendResult("failed", "redirect")  # no redirect is followed (§24.6)
        if status >= 500 or status in RETRIED_STATUSES:
            return SendResult("retry", str(status), retry_after_s(answer, now))
        return SendResult("failed", str(status))  # 410 and any other 4xx: final
