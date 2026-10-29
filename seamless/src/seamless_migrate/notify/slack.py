"""The Slack sender (SDD §8.7, §24.3, §24.5, §24.6, plan task NT7): one POST of the Slack payload
to the incoming webhook URL of the channel's secret through ``httpx``'s asynchronous client, 15 s,
no redirect followed. The URL is the request's address and nothing else: no reason, log line or
exception message carries it, and the answer's body is never kept."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from ..config import Settings
from ..domain.models import NotificationChannel
from .messages import Message, slack_payload
from .notifier import SendResult

log = logging.getLogger(__name__)

#: an HTTP request has this long (SDD §24.6)
HTTP_TIMEOUT_S = 15.0
#: where a Slack incoming webhook lives (SDD §24.3); another url is no secret of a Slack channel
SLACK_WEBHOOK_PREFIX = "https://hooks.slack.com/services/"
#: Slack's final answers are one word (invalid_payload, channel_not_found, channel_is_archived,
#: no_service, ...): kept as the reason; anything else - a proxy's page - names the status alone
_ONE_WORD = re.compile(r"[A-Za-z0-9_]{1,64}")
#: a 4xx tried again (SDD §24.6)
_RETRIED_4XX = (408, 429)


class _NoWebhookLine(logging.Filter):
    """httpx logs each request's method and URL at INFO: a Slack webhook's would be the credential
    in a log line (SDD §24.3, §24.8), so the record is dropped. The server's formatters redact
    every record besides (§13.4, ``cli.configure_logging``); this keeps the line out of any
    handler, a test's capture included."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "hooks.slack.com/services/" not in record.getMessage()
        except Exception:  # a record whose args do not format: nothing to read, let it be
            return True


_NO_WEBHOOK_LINE = _NoWebhookLine()


def _retry_after(answer: httpx.Response) -> float | None:
    """A Retry-After in seconds - as seconds, or as an HTTP date -, none when absent or unread."""
    value = answer.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(float(value.strip()), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


def _final_reason(answer: httpx.Response) -> str:
    """Slack's word, or the status (SDD §8.7)."""
    text = answer.text.strip()
    return text if _ONE_WORD.fullmatch(text) else str(answer.status_code)


class SlackSender:
    """One per server: the notifier's Slack channels send through it."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = HTTP_TIMEOUT_S,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.timeout_s = timeout_s
        logging.getLogger("httpx").addFilter(_NO_WEBHOOK_LINE)  # once: the same object is kept

    async def send(
        self,
        channel: NotificationChannel,
        message: Message,
        key: str,
        secret: Mapping[str, str] | None,
    ) -> SendResult:
        url = (secret or {}).get("url") or ""
        if not url.startswith(SLACK_WEBHOOK_PREFIX):
            return SendResult("failed", "no_secret")
        try:
            async with httpx.AsyncClient(
                transport=self.transport, follow_redirects=False, timeout=self.timeout_s
            ) as client:
                answer = await client.post(url, json=slack_payload(message))
        except httpx.HTTPError as exc:
            # the error's class alone: its text may name the address (SDD §24.8)
            return SendResult("retry", type(exc).__name__)
        status = answer.status_code
        if status == 200:
            return SendResult("sent")
        if status == 429:
            return SendResult("retry", "429", retry_after_s=_retry_after(answer))
        if status in _RETRIED_4XX or status >= 500:
            return SendResult("retry", str(status))
        reason = _final_reason(answer) if 400 <= status < 500 else str(status)
        log.warning("channel %s: Slack answered %s, final", channel.name, reason)
        return SendResult("failed", reason)
