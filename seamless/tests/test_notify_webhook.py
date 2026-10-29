"""The webhook sender against an httpx MockTransport in the test process (SDD §8.7, §13.3,
§24.3, §24.5, §24.6, plan task NT8). No request leaves the process; the signing key is made here."""

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import NotifyTrigger
from seamless_migrate.domain.models import NotificationChannel
from seamless_migrate.notify.messages import Message, webhook_document
from seamless_migrate.notify.notifier import senders_for
from seamless_migrate.notify.webhook import WebhookSender

URL = "https://cmdb.example.com/hooks/seamless"
CHANNEL = NotificationChannel(name="cmdb", kind="webhook", url=URL)
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
    vm_name="web-01",
    strategy="warm",
    phase="cutover",
    estimate_s=120.0,
    slo_s=600.0,
    link="https://seamless.example.com/migrations/mig-0000000001",
)
KEY = "cmdb_7"
NOW = datetime(2026, 10, 29, 10, 0, 9, tzinfo=UTC)


def signing_key() -> str:
    """A key as an admin would make one (SDD §24.3): whsec_ and the base64 of 32 random bytes."""
    return "whsec_" + base64.b64encode(os.urandom(32)).decode()


def expected_signature(key: str, msg_id: str, timestamp: str, body: bytes) -> str:
    """Standard Webhooks' rule, written here a second time and independently of the sender:
    v1, and the base64 HMAC-SHA256 of ``<id>.<timestamp>.<body>`` keyed with the bytes after
    ``whsec_``."""
    raw = base64.b64decode(key[len("whsec_") :])
    digest = hmac.new(raw, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


class Receiver:
    """A receiver at the channel's URL: records every request, answers in turn, and keeps the
    last answer for every request after it."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


class Clock:
    """A clock the sender reads for each try; ``tick`` moves it on."""

    def __init__(self, start: datetime = NOW):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def sender_for(tmp_path, receiver: Receiver, clock: Clock | None = None) -> WebhookSender:
    settings = Settings(data_dir=tmp_path / "data", secrets_dir=tmp_path / "secrets")
    return WebhookSender(settings, transport=receiver.transport, now=clock or Clock())


async def send(sender: WebhookSender, secret, *, key: str = KEY):
    return await sender.send(CHANNEL, MESSAGE, key, secret)


async def test_a_webhook_is_signed_as_standard_webhooks_specify(tmp_path):
    """SDD §8.7, §24.3, §24.5: one POST to the channel's URL with the Standard Webhooks document as
    JSON, webhook-id the message's key, webhook-timestamp the try's Unix seconds, and
    webhook-signature v1, and the base64 HMAC-SHA256 of <id>.<timestamp>.<body> keyed with the
    bytes after whsec_; a second implementation of the rule verifies it in constant time; any 2xx
    is sent."""
    key = signing_key()
    receiver = Receiver(httpx.Response(200, text="ok"))
    sender = sender_for(tmp_path, receiver)
    result = await send(sender, {"signing_key": key})
    assert result.outcome == "sent" and result.reason is None, result
    [request] = receiver.requests
    assert (request.method, str(request.url)) == ("POST", URL)
    assert request.headers["content-type"] == "application/json"
    body = request.read()
    assert json.loads(body) == webhook_document(MESSAGE)
    assert json.loads(body)["type"] == "downtime.started"
    assert request.headers["webhook-id"] == KEY
    timestamp = request.headers["webhook-timestamp"]
    assert timestamp == str(int(NOW.timestamp())) == "1793268009"
    signature = request.headers["webhook-signature"]
    assert signature.startswith("v1,")
    assert hmac.compare_digest(signature, expected_signature(key, KEY, timestamp, body))
    # the comparison is not vacuous: another key, another id, another time or another body
    # each give another signature
    assert not hmac.compare_digest(
        signature, expected_signature(signing_key(), KEY, timestamp, body)
    )
    assert not hmac.compare_digest(signature, expected_signature(key, "cmdb_8", timestamp, body))
    assert not hmac.compare_digest(signature, expected_signature(key, KEY, "1793268010", body))
    assert not hmac.compare_digest(signature, expected_signature(key, KEY, timestamp, body + b" "))
    # the key holds no "." (SDD §24.6), and a 204 with no body is sent too
    assert "." not in KEY
    receiver.answers = [httpx.Response(204)]
    assert (await send(sender, {"signing_key": key})).outcome == "sent"


async def test_each_try_is_signed_anew_with_the_same_webhook_id(tmp_path):
    """SDD §8.7, §24.6: a 503, a 429, a 408, a timeout and a connection error are retried, with
    the answer's Retry-After; each try carries the same webhook-id with its own timestamp and a
    signature that verifies for that timestamp alone."""
    key = signing_key()
    clock = Clock()
    receiver = Receiver(
        httpx.Response(503, headers={"Retry-After": "30"}),
        httpx.Response(429, headers={"Retry-After": "Wed, 29 Oct 2026 10:01:39 GMT"}),
        httpx.Response(408),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("connection refused"),
        httpx.Response(500, headers={"Retry-After": "soon"}),
        httpx.Response(200),
    )
    sender = sender_for(tmp_path, receiver, clock)
    results = []
    for _ in range(7):
        results.append(await send(sender, {"signing_key": key}))
        clock.tick(10)
    assert [(r.outcome, r.reason, r.retry_after_s) for r in results] == [
        ("retry", "503", 30.0),
        ("retry", "429", 80.0),  # an HTTP-date, 80 s after the clock's 10:00:19
        ("retry", "408", None),
        ("retry", "ReadTimeout", None),
        ("retry", "ConnectError", None),
        ("retry", "500", None),  # a Retry-After the sender cannot read is none
        ("sent", None, None),
    ]
    assert len(receiver.requests) == 7
    assert {r.headers["webhook-id"] for r in receiver.requests} == {KEY}
    timestamps = [r.headers["webhook-timestamp"] for r in receiver.requests]
    assert timestamps == [str(int(NOW.timestamp()) + 10 * i) for i in range(7)]
    signatures = [r.headers["webhook-signature"] for r in receiver.requests]
    assert len(set(signatures)) == 7
    bodies = {r.read() for r in receiver.requests}
    assert len(bodies) == 1  # the same document each try: the timestamp is a header, not a field
    [body] = bodies
    for i in range(len(receiver.requests)):
        own = expected_signature(key, KEY, timestamps[i], body)
        assert hmac.compare_digest(signatures[i], own)
        for other in timestamps[:i] + timestamps[i + 1 :]:
            assert not hmac.compare_digest(signatures[i], expected_signature(key, KEY, other, body))


async def test_a_webhook_redirect_is_not_followed(tmp_path):
    """SDD §8.7, §24.6: a 3xx is final with the reason redirect, and the receiver named by
    Location gets no request - the signed document goes to the channel's URL alone."""
    key = signing_key()
    for status in (301, 302, 303, 307, 308):
        receiver = Receiver(
            httpx.Response(status, headers={"Location": "https://elsewhere.example.com/hooks"}),
            httpx.Response(200),
        )
        result = await send(sender_for(tmp_path, receiver), {"signing_key": key})
        assert (result.outcome, result.reason) == ("failed", "redirect"), status
        assert [str(r.url) for r in receiver.requests] == [URL]


async def test_a_410_from_a_webhook_is_final(tmp_path):
    """SDD §8.7, §24.6: a 410 - the receiver is gone - and any other 4xx but 408 and 429 are
    final, with the status as the reason, after one request."""
    key = signing_key()
    for status in (410, 400, 401, 403, 404, 413, 422):
        receiver = Receiver(httpx.Response(status, text="gone"), httpx.Response(200))
        result = await send(sender_for(tmp_path, receiver), {"signing_key": key})
        assert (result.outcome, result.reason) == ("failed", str(status)), status
        assert len(receiver.requests) == 1


async def test_the_signing_key_never_leaves_the_control_plane(tmp_path, caplog):
    """SDD §13.3, §24.3, §24.8: only the signature travels. The key appears in no header, no
    body, no SendResult reason, no log line and no exception's text - on a sent message, a
    retried one, a final one and a transport error alike; a missing or malformed key is failed
    with no_secret before any request."""
    key = signing_key()
    raw = key[len("whsec_") :]
    caplog.set_level(logging.DEBUG)
    receiver = Receiver(
        httpx.Response(200),
        httpx.Response(503, text=f"echo {key}"),  # a receiver may echo what it was sent
        httpx.Response(410),
        httpx.ConnectError("connection refused"),
        RuntimeError("the transport broke"),
    )
    sender = sender_for(tmp_path, receiver)
    results = []
    for _ in range(4):
        results.append(await send(sender, {"signing_key": key}))
    with pytest.raises(RuntimeError) as raised:
        await send(sender, {"signing_key": key})
    assert [r.outcome for r in results] == ["sent", "retry", "failed", "retry"]
    assert len(receiver.requests) == 5
    for request in receiver.requests:
        for name, value in request.headers.items():
            assert key not in value and raw not in value, name
        assert key.encode() not in request.read() and raw.encode() not in request.read()
    for result in results:
        assert key not in (result.reason or "") and raw not in (result.reason or "")
    assert key not in caplog.text and raw not in caplog.text
    assert key not in str(raised.value) and raw not in str(raised.value)
    # the secret missing, without the key, or malformed: nothing is sent
    before = len(receiver.requests)
    for secret in (
        None,
        {},
        {"url": "https://hooks.example.org/x"},
        {"signing_key": ""},
        {"signing_key": raw},  # no whsec_ prefix
        {"signing_key": "whsec_"},  # nothing after it
        {"signing_key": "whsec_not*base64"},
        {"signing_key": "whsec_AAAA*AAAA"},  # valid base64 only once the * is dropped
    ):
        result = await send(sender, secret)
        assert (result.outcome, result.reason) == ("failed", "no_secret"), secret
    assert len(receiver.requests) == before
    assert key not in caplog.text and raw not in caplog.text


def test_the_server_runs_a_webhook_sender(tmp_path):
    """SDD §8.7: senders_for registers the webhook sender for the webhook kind."""
    settings = Settings(data_dir=tmp_path / "data", secrets_dir=tmp_path / "secrets")
    assert isinstance(senders_for(settings)["webhook"], WebhookSender)
