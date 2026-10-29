"""The live smoke of the notification senders (SDD §24.10, plan task NT12): a real Slack
workspace's incoming webhook, a real SMTP relay over STARTTLS and over implicit TLS, and a real
webhook receiver that verifies the Standard Webhooks signature with a library of its own.

Skipped unless enabled, as test_live_integrations.py's tests are. The secrets come from the
environment, never from the repository:

* ``SEAMLESS_LIVE_SLACK=1``: the webhook's URL is the channel ``live-slack``'s secret, given as
  §13.3's variable ``SEAMLESS_SECRET_NOTIFY_LIVE_SLACK_URL``.
* ``SEAMLESS_LIVE_SMTP=1``: the installation's relay variables (``SEAMLESS_SMTP_HOST``,
  ``SEAMLESS_SMTP_FROM``, and ``SEAMLESS_SMTP_SECRET`` or ``SEAMLESS_SMTP_CA_FILE`` when the relay
  needs them), a test mailbox in ``SEAMLESS_LIVE_SMTP_TO`` (comma-separated), and optionally
  ``SEAMLESS_LIVE_SMTP_STARTTLS_PORT`` (587), ``SEAMLESS_LIVE_SMTP_TLS_PORT`` (465) and
  ``SEAMLESS_LIVE_SMTP_TLS_HOST`` for a second relay that speaks implicit TLS.
* ``SEAMLESS_LIVE_WEBHOOK=1``: the receiver's URL in ``SEAMLESS_LIVE_WEBHOOK_URL``, and the
  signing key it verifies with as the channel ``live-webhook``'s secret, given as
  ``SEAMLESS_SECRET_NOTIFY_LIVE_WEBHOOK_SIGNING_KEY``. The receiver answers 2xx only for a
  signature its library verified.

Each test sends one message through the sender the notifier uses, with the secret read as the
notifier reads it, and asserts the answer. QASuite §13.1d records the results with the versions
and the open questions of §24.10.
"""

from __future__ import annotations

import os
import uuid

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import NotifyTrigger
from seamless_migrate.domain.models import NotificationChannel, utcnow
from seamless_migrate.notify.email import EmailSender
from seamless_migrate.notify.messages import Message
from seamless_migrate.notify.slack import SlackSender
from seamless_migrate.notify.webhook import WebhookSender
from seamless_migrate.security.secrets import read_secret

pytestmark = pytest.mark.live

LIVE_SLACK = os.environ.get("SEAMLESS_LIVE_SLACK") == "1"
LIVE_SMTP = os.environ.get("SEAMLESS_LIVE_SMTP") == "1"
LIVE_WEBHOOK = os.environ.get("SEAMLESS_LIVE_WEBHOOK") == "1"


def message() -> Message:
    """One fixed message of §24.5, about no real VM: the smoke's reader sees what it is."""
    return Message(
        trigger=NotifyTrigger.downtime_started,
        title="Seamless Migrate live smoke: livetest-vm is down for its cutover",
        lines=["A live smoke of the notification senders (QASuite §13.1d); no VM was stopped."],
        plan_id="plan-livetest",
        plan_name="Seamless live smoke",
        event_seq=1,
        event_kind="migration.downtime_started",
        event_ts=utcnow(),
        migration_id="mig-livetest00",
    )


def key(channel: str) -> str:
    """A dedupe key of its own per run (§24.6): no receiver drops the smoke as a copy."""
    return f"{channel}_{uuid.uuid4().hex[:12]}"


def channel_secret(channel: NotificationChannel, settings: Settings, needed: str) -> dict:
    secret = read_secret(channel.secret_name, settings)
    if not secret.get(needed):
        variable = "SEAMLESS_SECRET_" + channel.secret_name.replace("-", "_").upper()
        pytest.skip(f"{variable}_{needed.upper()} is not set")
    return secret


@pytest.mark.skipif(not LIVE_SLACK, reason="set SEAMLESS_LIVE_SLACK=1 to post to a Slack webhook")
async def test_live_slack_accepts_a_message():
    settings = Settings.from_env()
    channel = NotificationChannel(name="live-slack", kind="slack")
    secret = channel_secret(channel, settings, "url")
    result = await SlackSender(settings).send(channel, message(), key(channel.name), secret)
    assert result.outcome == "sent", result.reason


def smtp_settings(security: str, port_variable: str, default_port: int, host_variable: str = ""):
    settings = Settings.from_env()
    if not (settings.smtp_host and settings.smtp_from):
        pytest.skip("SEAMLESS_SMTP_HOST and SEAMLESS_SMTP_FROM are not set")
    recipients = [a.strip() for a in os.environ.get("SEAMLESS_LIVE_SMTP_TO", "").split(",")]
    recipients = [a for a in recipients if a]
    if not recipients:
        pytest.skip("SEAMLESS_LIVE_SMTP_TO is not set")
    host = (os.environ.get(host_variable) if host_variable else None) or settings.smtp_host
    port = int(os.environ.get(port_variable) or default_port)
    live = settings.model_copy(
        update={"smtp_security": security, "smtp_port": port, "smtp_host": host}
    )
    return live, NotificationChannel(name="live-mailbox", kind="email", recipients=recipients)


@pytest.mark.skipif(not LIVE_SMTP, reason="set SEAMLESS_LIVE_SMTP=1 to send through a relay")
async def test_live_smtp_delivers_over_starttls():
    settings, channel = smtp_settings("starttls", "SEAMLESS_LIVE_SMTP_STARTTLS_PORT", 587)
    result = await EmailSender(settings).send(channel, message(), key(channel.name), None)
    assert result.outcome == "sent", result.reason
    assert result.refused == 0


@pytest.mark.skipif(not LIVE_SMTP, reason="set SEAMLESS_LIVE_SMTP=1 to send through a relay")
async def test_live_smtp_delivers_over_implicit_tls():
    settings, channel = smtp_settings(
        "tls", "SEAMLESS_LIVE_SMTP_TLS_PORT", 465, "SEAMLESS_LIVE_SMTP_TLS_HOST"
    )
    result = await EmailSender(settings).send(channel, message(), key(channel.name), None)
    assert result.outcome == "sent", result.reason
    assert result.refused == 0


@pytest.mark.skipif(not LIVE_WEBHOOK, reason="set SEAMLESS_LIVE_WEBHOOK=1 to call a receiver")
async def test_live_webhook_receiver_verifies_the_signature():
    url = os.environ.get("SEAMLESS_LIVE_WEBHOOK_URL")
    if not url:
        pytest.skip("SEAMLESS_LIVE_WEBHOOK_URL is not set")
    settings = Settings.from_env()
    channel = NotificationChannel(name="live-webhook", kind="webhook", url=url)
    secret = channel_secret(channel, settings, "signing_key")
    result = await WebhookSender(settings).send(channel, message(), key(channel.name), secret)
    # the receiver answers 2xx only for a signature its Standard Webhooks library verified
    assert result.outcome == "sent", result.reason
