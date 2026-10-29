"""The email sender against an SMTP relay in the test process (SDD §8.7, §24.3, §24.5, §24.10,
plan task NT6)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import NotifyTrigger
from seamless_migrate.domain.models import NotificationChannel
from seamless_migrate.notify.email import EmailSender
from seamless_migrate.notify.messages import Message
from seamless_migrate.store import Store
from tests.api_support import Api, api_settings
from tests.smtp_support import Recorder, Relay, make_certificate

CHANNEL = NotificationChannel(
    name="app-owners", kind="email", recipients=["owners@example.com", "ops@example.org"]
)
MESSAGE = Message(
    trigger=NotifyTrigger.downtime_started,
    title="web-01 is down for its cutover",
    lines=[
        "The source VM stopped at 2026-10-29T10:00:05Z. Estimated downtime 2 min 0 s; "
        "SLO 10 min 0 s."
    ],
    plan_id="plan-0000abcd",
    plan_name="Finance apps",
    event_seq=7,
    event_kind="migration.downtime_started",
    event_ts=datetime(2026, 10, 29, 10, 0, 5, tzinfo=UTC),
    migration_id="mig-0000000001",
    link="https://seamless.example.com/migrations/mig-0000000001",
)


@pytest.fixture
def certificate(tmp_path):
    return make_certificate(tmp_path)


def settings_for(tmp_path, relay, certificate, *, security="starttls", secret=None, ca=True):
    kw = {
        "data_dir": tmp_path / "data",
        "secrets_dir": tmp_path / "secrets",
        "smtp_host": "127.0.0.1",
        "smtp_port": relay.port,
        "smtp_from": "seamless@example.com",
        "smtp_security": security,
        "smtp_secret": secret,
    }
    if ca and certificate is not None:
        kw["smtp_ca_file"] = certificate[0]
    return Settings(**kw)


async def send(sender, secret=None, channel=CHANNEL):
    return await sender.send(channel, MESSAGE, "app-owners_7", secret)


async def test_email_goes_over_starttls_and_authenticates_after_it(tmp_path, certificate):
    """SDD §24.3: the session opens in plain text, upgrades with STARTTLS before anything else,
    and authenticates with the secret's username and password only then; the relay receives one
    message for the channel's recipients, all in To."""
    with Relay(certificate, mode="starttls", auth=("seamless", "relay-pw")) as relay:
        sender = EmailSender(settings_for(tmp_path, relay, certificate, secret="smtp-relay"))
        result = await send(sender, {"username": "seamless", "password": "relay-pw"})
        assert result.outcome == "sent" and result.refused == 0, result
        assert relay.logins == ["seamless"]  # the relay admits AUTH after TLS alone
        assert relay.handler.sessions_tls == [True]
        [mail] = relay.handler.messages
        assert mail["To"] == "owners@example.com, ops@example.org"
        assert mail["From"] == "seamless@example.com"
        assert mail["Subject"] == "[Seamless] web-01 is down for its cutover"
        assert relay.handler.recipients == [["owners@example.com", "ops@example.org"]]
        # a wrong password is a 535: final
        wrong = await send(sender, {"username": "seamless", "password": "nope"})
        assert wrong.outcome == "failed" and wrong.reason == "535"
        assert len(relay.handler.messages) == 1


async def test_email_goes_over_implicit_tls(tmp_path, certificate):
    """SDD §24.3: SEAMLESS_SMTP_SECURITY=tls connects with implicit TLS (RFC 8314's preference,
    port 465) and sends without a relay credential when none is set."""
    with Relay(certificate, mode="implicit") as relay:
        sender = EmailSender(settings_for(tmp_path, relay, certificate, security="tls"))
        result = await send(sender)
        assert result.outcome == "sent", result
        assert relay.handler.sessions_tls == [True] and relay.logins == []


async def test_a_relay_without_starttls_gets_nothing_in_clear_text(tmp_path, certificate):
    """SDD §24.3 (NFR-06): there is no plain-text mode - a relay that offers no STARTTLS fails the
    message with no_starttls, final, and the client never falls back: nothing but EHLO went
    in clear text and the relay received no message."""
    with Relay(None, mode="plain") as relay:
        sender = EmailSender(settings_for(tmp_path, relay, certificate))
        result = await send(sender, {"username": "seamless", "password": "relay-pw"})
        assert result.outcome == "failed" and result.reason == "no_starttls"
        assert relay.handler.messages == [] and relay.logins == []


async def test_a_relay_whose_certificate_fails_verification_gets_nothing(tmp_path, certificate):
    """SDD §24.3: the relay's certificate is verified against the system's CAs, or
    SEAMLESS_SMTP_CA_FILE; a certificate that fails is a TLS error - retried, and nothing is sent
    over it - on both paths."""
    for mode, security in (("starttls", "starttls"), ("implicit", "tls")):
        with Relay(certificate, mode=mode) as relay:
            sender = EmailSender(
                settings_for(tmp_path, relay, certificate, security=security, ca=False)
            )
            result = await send(sender)
            assert result.outcome == "retry" and result.reason == "tls_error", (mode, result)
            assert relay.handler.messages == []


async def test_an_smtp_4yz_reply_is_retried_and_a_5yz_reply_is_final(tmp_path, certificate):
    """SDD §24.6: a temporary SMTP reply (4yz) is retried, a permanent one (5yz) is final, each
    with its code as the reason."""
    with Relay(certificate, handler=Recorder(data_reply="451 4.3.0 try again later")) as relay:
        result = await send(EmailSender(settings_for(tmp_path, relay, certificate)))
        assert (result.outcome, result.reason) == ("retry", "451")
    with Relay(certificate, handler=Recorder(data_reply="552 5.3.4 message too big")) as relay:
        result = await send(EmailSender(settings_for(tmp_path, relay, certificate)))
        assert (result.outcome, result.reason) == ("failed", "552")
    # a relay that is not there: a connection error, retried
    with Relay(certificate) as relay:
        port = relay.port
    result = await send(EmailSender(settings_for(tmp_path, relay, certificate)))
    assert result.outcome == "retry" and result.reason, (port, result)


async def test_the_message_id_is_the_key_and_auto_submitted_is_set(tmp_path, certificate):
    """SDD §24.5, §24.6: Message-ID is <{key}@{the sender's domain}>, so a receiver that keeps the
    ids it saw drops a copy sent again after a restart; Auto-Submitted tells responders not to
    answer; the body is the lines and the ending."""
    with Relay(certificate) as relay:
        result = await send(EmailSender(settings_for(tmp_path, relay, certificate)))
        assert result.outcome == "sent"
        [mail] = relay.handler.messages
        assert mail["Message-ID"] == "<app-owners_7@example.com>"
        assert mail["Auto-Submitted"] == "auto-generated"
        body = mail.get_payload(decode=True).decode()
        assert body.startswith("The source VM stopped at 2026-10-29T10:00:05Z.")
        assert "Plan Finance apps (plan-0000abcd)" in body
        assert "migration mig-0000000001" in body
        assert "https://seamless.example.com/migrations/mig-0000000001" in body


async def test_a_partly_refused_message_counts_its_refused_recipients(tmp_path, certificate):
    """SDD §24.6: when the relay accepts some recipients and refuses others, the message counts
    as sent and the outcome gives the number refused, never their addresses; all refused is
    final."""
    with Relay(certificate, handler=Recorder(refuse=("ops@example.org",))) as relay:
        result = await send(EmailSender(settings_for(tmp_path, relay, certificate)))
        assert result.outcome == "sent" and result.refused == 1
        assert relay.handler.recipients == [["owners@example.com"]]
        assert "ops@example.org" not in (result.reason or "")
    with Relay(
        certificate, handler=Recorder(refuse=("owners@example.com", "ops@example.org"))
    ) as relay:
        result = await send(EmailSender(settings_for(tmp_path, relay, certificate)))
        assert result.outcome == "failed" and result.refused == 2 and result.reason == "550"


async def test_an_email_channel_without_a_relay_fails_with_no_relay(tmp_path):
    """SDD §24.3: while SEAMLESS_SMTP_HOST or SEAMLESS_SMTP_FROM is unset, an email channel's
    messages fail with no_relay, and the API refuses to register or turn a channel into an email
    one (422)."""
    sender = EmailSender(Settings(data_dir=tmp_path / "data", smtp_from="s@example.com"))
    result = await send(sender)
    assert result.outcome == "failed" and result.reason == "no_relay"
    settings, tokens = api_settings(tmp_path, secrets_dir=tmp_path / "secrets")
    assert settings.smtp_host is None
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        api = Api(client, store, settings, tokens)
        refused = api.post(
            "/api/v1/notifications/channels",
            json={"name": "owners", "kind": "email", "recipients": ["o@example.com"]},
        )
        assert refused.status_code == 422, refused.text
        assert "SEAMLESS_SMTP_HOST" in refused.json()["error"]["message"]
        assert (
            api.post(
                "/api/v1/notifications/channels", json={"name": "ops", "kind": "slack"}
            ).status_code
            == 201
        )
        assert api.get("/api/v1/notifications/channels").json()[0]["name"] == "ops"
    store.dispose()
