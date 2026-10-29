"""The Slack sender against httpx's MockTransport, never a network (SDD §8.7, §24.3, §24.5, §24.6,
§24.8, plan task NT7)."""

import json
import logging
import time
from datetime import UTC, datetime

import httpx
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import NotifyTrigger, Role
from seamless_migrate.domain.models import NotificationChannel
from seamless_migrate.notify.messages import Message, slack_payload
from seamless_migrate.notify.notifier import SendResult, senders_for
from seamless_migrate.notify.slack import SlackSender
from seamless_migrate.security.secret_store import secret_store
from seamless_migrate.store import Store
from tests.api_support import Api, api_settings
from tests.test_notify import Lab, run

CHANNEL = NotificationChannel(name="ops-slack", kind="slack")
#: a made-up webhook path, too short for a secret scanner's pattern (the plan's rules)
SLACK_URL = "https://hooks.slack.com/services/T000/B000/notasecret"
SECRET = {"url": SLACK_URL}
MESSAGE = Message(
    trigger=NotifyTrigger.downtime_started,
    title="web-01 <!channel> & co is down for its cutover",
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


class Slack:
    """A fake of Slack's webhook endpoint: records every request, answers a script, then 200."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []
        self.times: list[float] = []

    def handler(self, request):
        self.requests.append(request)
        self.times.append(time.monotonic())
        answer = self.answers.pop(0) if self.answers else (200, "ok", {})
        if isinstance(answer, Exception):
            raise answer
        status, text, headers = answer
        return httpx.Response(status, text=text, headers=headers)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


def sender_for(tmp_path, slack):
    settings = Settings(data_dir=tmp_path / "data", secrets_dir=tmp_path / "secrets")
    return SlackSender(settings, transport=slack.transport)


async def send(sender, secret=SECRET, channel=CHANNEL):
    return await sender.send(channel, MESSAGE, "ops-slack_7", secret)


def leaked(text):
    return "hooks.slack.com" in text or "notasecret" in text or "T000" in text


async def test_slack_gets_text_and_blocks_and_nothing_unfurls(tmp_path):
    """SDD §8.7, §24.5, §24.6: one POST of the Slack payload as JSON to the secret's url - the text
    for the phone, the blocks for the client, every value escaped, nothing unfurled -, within 15 s
    and with no redirect followed: a 3xx is final, and the Location is never fetched; the server
    runs this sender for the slack kind."""
    slack = Slack((200, "ok", {}), (302, "", {"location": "https://elsewhere.example/x"}))
    sender = sender_for(tmp_path, slack)
    assert isinstance(senders_for(sender.settings)["slack"], SlackSender)
    result = await send(sender)
    assert result == SendResult("sent"), result
    [request] = slack.requests
    assert (request.method, str(request.url)) == ("POST", SLACK_URL)
    assert request.headers["content-type"] == "application/json"
    assert request.extensions["timeout"] == {k: 15.0 for k in ("connect", "read", "write", "pool")}
    body = json.loads(request.content)
    assert body == slack_payload(MESSAGE)
    assert body["text"] == "web-01 &lt;!channel&gt; &amp; co is down for its cutover"
    assert body["unfurl_links"] is False and body["unfurl_media"] is False
    assert [block["type"] for block in body["blocks"]] == ["header", "section", "context"]
    assert body["blocks"][0]["text"]["text"] == body["text"]
    assert "2026-10-29T10:00:05Z" in body["blocks"][1]["text"]["text"]
    context = " ".join(e["text"] for e in body["blocks"][2]["elements"])
    assert "plan-0000abcd" in context and "mig-0000000001" in context
    assert "https://seamless.example.com/migrations/mig-0000000001" in context
    assert "<!channel>" not in request.content.decode()
    # a redirect: final, and only the one request went
    moved = await send(sender)
    assert (moved.outcome, moved.reason) == ("failed", "302")
    assert len(slack.requests) == 2


async def test_a_slack_429_waits_its_retry_after(tmp_path):
    """SDD §24.6: a 429 is retried after the Retry-After Slack sends - seconds, or an HTTP date -,
    which the notifier waits before the next try; 408, 5xx, a timeout and a connection error
    are retried too, each with the status or the error's class as the reason."""
    slack = Slack(
        (429, "rate_limited", {"retry-after": "7"}),
        (429, "rate_limited", {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}),
        (429, "rate_limited", {}),
        (408, "", {}),
        (503, "service_unavailable", {}),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("[Errno 61] Connection refused"),
    )
    sender = sender_for(tmp_path, slack)
    waited = await send(sender)
    assert waited == SendResult("retry", "429", retry_after_s=7.0), waited
    dated = await send(sender)
    assert (dated.outcome, dated.reason, dated.retry_after_s) == ("retry", "429", 0.0)
    bare = await send(sender)
    assert (bare.outcome, bare.reason, bare.retry_after_s) == ("retry", "429", None)
    for reason in ("408", "503", "ReadTimeout", "ConnectError"):
        result = await send(sender)
        assert (result.outcome, result.reason) == ("retry", reason), result
    # through the notifier: the second try waits the Retry-After, then the message is sent
    lab = Lab(tmp_path / "lab")
    slack = Slack((429, "rate_limited", {"retry-after": "0.12"}))
    lab.notifier.senders["slack"] = SlackSender(lab.settings, transport=slack.transport)
    channel = lab.channel()
    secret_store(lab.settings).write(channel.secret_name, SECRET, {"seamless.io/channel": "x"})
    lab.subscribe("ops-slack")
    lab.downtime(1)

    async def body():
        [seen] = await lab.wait_outcomes(1)
        assert seen.kind == "notification.sent" and seen.data["tries"] == 2
        assert len(slack.requests) == 2
        first, second = slack.times
        assert second - first >= 0.12, (first, second)

    await run(lab, body)


async def test_a_revoked_slack_webhook_fails_without_a_retry(tmp_path):
    """SDD §24.6: Slack's final answers - invalid_payload, channel_not_found, channel_is_archived,
    no_service, a revoked webhook's among them - and any other 4xx fail at once with the answer's
    word as the reason; a missing secret, or one that is no Slack webhook, is no_secret and
    nothing is sent; through the notifier such a message is tried once."""
    finals = (
        (400, "invalid_payload"),
        (404, "channel_not_found"),
        (410, "channel_is_archived"),
        (404, "no_service"),
        (403, "action_prohibited"),
    )
    slack = Slack(*[(status, text, {}) for status, text in finals])
    sender = sender_for(tmp_path, slack)
    for status, text in finals:
        result = await send(sender)
        assert (result.outcome, result.reason) == ("failed", text), (status, result)
    assert len(slack.requests) == len(finals)
    # no secret, an empty one, or a url that is no Slack incoming webhook: nothing goes out
    for secret in (None, {}, {"url": ""}, {"url": "https://cmdb.example.com/hooks/seamless"}):
        result = await send(sender, secret)
        assert (result.outcome, result.reason) == ("failed", "no_secret"), secret
    assert len(slack.requests) == len(finals)
    # through the notifier: one try, the outcome recorded with Slack's word
    lab = Lab(tmp_path / "lab")
    slack = Slack((404, "no_service", {}))
    lab.notifier.senders["slack"] = SlackSender(lab.settings, transport=slack.transport)
    channel = lab.channel()
    secret_store(lab.settings).write(channel.secret_name, SECRET, {"seamless.io/channel": "x"})
    lab.subscribe("ops-slack")
    lab.downtime(1)

    async def body():
        [seen] = await lab.wait_outcomes(1)
        assert seen.kind == "notification.failed"
        assert (seen.data["tries"], seen.data["reason"]) == (1, "no_service")
        assert len(slack.requests) == 1

    await run(lab, body)


async def test_the_slack_url_reaches_no_event_no_log_line_and_no_answer(tmp_path, caplog):
    """SDD §24.3, §24.8: the URL is the credential and the request's address alone - an answer
    that echoes it, an error whose text names it, a redirect to it: no reason, event, log line,
    API answer or result holds it."""
    caplog.set_level(logging.DEBUG)
    echo = Slack(
        (400, f"invalid payload sent to {SLACK_URL}", {}),
        httpx.ConnectError(f"cannot reach {SLACK_URL}"),
        (500, f"<html>{SLACK_URL}</html>", {}),
        (301, "", {"location": SLACK_URL + "/moved"}),
    )
    sender = sender_for(tmp_path, echo)
    expectations = (
        ("failed", "400"),
        ("retry", "ConnectError"),
        ("retry", "500"),
        ("failed", "301"),
    )
    for expected in expectations:
        result = await send(sender)
        assert (result.outcome, result.reason) == expected, result
        assert not leaked(repr(result))
    # the test route: the answer and the recorded outcome name the status alone
    settings, tokens = api_settings(tmp_path / "api", secrets_dir=tmp_path / "api" / "secrets")
    store = Store(f"sqlite:///{tmp_path / 'api' / 'api.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        api = Api(client, store, settings, tokens)
        echo = Slack(
            (400, f"invalid payload sent to {SLACK_URL}", {}),
            httpx.ConnectError(f"cannot reach {SLACK_URL}"),
        )
        client.app.state.services.notifier.senders["slack"] = SlackSender(
            settings, transport=echo.transport
        )
        created = api.post("/api/v1/notifications/channels", json={"name": "ops", "kind": "slack"})
        assert created.status_code == 201, created.text
        written = api.client.put(
            "/api/v1/notifications/channels/ops/secret",
            headers=api.h(Role.admin),
            json={"url": SLACK_URL},
        )
        assert written.status_code == 200, written.text
        answers = [api.post("/api/v1/notifications/channels/ops/test") for _ in range(2)]
        assert [a.json() for a in answers] == [
            {"outcome": "failed", "reason": "400"},
            {"outcome": "failed", "reason": "ConnectError"},
        ]
        assert not any(leaked(a.text) for a in answers)
        recorded = store.events(since_seq=0, limit=100000)
        assert [e.kind for e in recorded if e.kind.startswith("notification.")] == [
            "notification.failed",
            "notification.failed",
        ]
        assert not any(leaked(e.model_dump_json()) for e in recorded)
        assert len(echo.requests) == 2
    store.dispose()
    assert not leaked(caplog.text)
