"""The notification catalog, its secrets and the subscriptions (SDD §4.3, §12, §13.2, §13.3,
§24.3, §24.4, plan task NT3)."""

import base64
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.domain.enums import PlanStatus, Role
from seamless_migrate.domain.models import NotificationCursor, Plan
from seamless_migrate.store import Store
from tests.api_support import DESTINATION, SOURCE, Api, api_settings

CHANNELS = "/api/v1/notifications/channels"
SUBSCRIPTIONS = "/api/v1/notifications/subscriptions"
EMAIL = {
    "name": "app-owners",
    "kind": "email",
    "recipients": ["owners@example.com", "ops@example.org"],
    "description": "The owners' list",
}
SLACK = {"name": "ops-slack", "kind": "slack"}
WEBHOOK = {"name": "cmdb", "kind": "webhook", "url": "https://cmdb.example.com/hooks/seamless"}
SLACK_URL = "https://hooks.slack.com/services/T0000000/B0000000/notasecretxxxxxxxxxxxxxx"
SIGNING_KEY = "whsec_" + base64.b64encode(bytes(range(32))).decode()
PLAN = {
    "name": "Finance",
    "source_provider_id": "src-osp",
    "destination_provider_id": "dst-rhoso",
    "vm_ids": ["vm-1"],
}
OTHERS = (Role.viewer, Role.operator, Role.approver)


@pytest.fixture
def api(tmp_path):
    settings, tokens = api_settings(tmp_path, secrets_dir=tmp_path / "secrets")
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        api = Api(client, store, settings, tokens)
        assert api.post("/api/v1/providers", json=SOURCE).status_code == 201
        assert api.post("/api/v1/providers", json=DESTINATION).status_code == 201
        yield api
    store.dispose()


def patch(api, path, body, role=Role.admin):
    return api.client.patch(f"/api/v1{path}", headers=api.h(role), json=body)


def put(api, path, body, role=Role.admin):
    return api.client.put(f"/api/v1{path}", headers=api.h(role), json=body)


def delete(api, path, role=Role.admin):
    return api.client.delete(f"/api/v1{path}", headers=api.h(role))


def register(api, body):
    created = api.post(CHANNELS, json=body)
    assert created.status_code == 201, created.text
    return created.json()


def subscribe(api, body, role=Role.operator):
    return api.post(SUBSCRIPTIONS, role=role, json=body)


def make_plan(api):
    created = api.post("/api/v1/plans", role=Role.operator, json=PLAN)
    assert created.status_code == 201, created.text
    return created.json()["id"]


def message(response):
    return response.json()["error"]["message"]


def events(api, prefixes=("channel.", "subscription.")):
    return [e for e in api.store.events(since_seq=0, limit=100000) if e.kind.startswith(prefixes)]


def test_only_an_admin_changes_the_channel_catalog(api):
    """SDD §12, §13.2, §24.4: a channel decides where the installation's data goes, so only an
    admin registers, edits, enables, disables or deletes one, or writes its secret; every role
    reads the catalog by name. Each change is a channel.* event naming the channel and its kind."""
    for role in OTHERS:
        assert api.post(CHANNELS, role=role, json=SLACK).status_code == 403
    assert api.get(CHANNELS).json() == []
    created = register(api, WEBHOOK)
    assert {k: created[k] for k in ("name", "kind", "url", "recipients", "enabled")} == {
        "name": "cmdb",
        "kind": "webhook",
        "url": "https://cmdb.example.com/hooks/seamless",
        "recipients": [],
        "enabled": True,
    }
    assert created["redact_names"] is False and created["description"] is None
    assert created["created_at"] and created["updated_at"]
    assert created["recipient_count"] == 0 and created["url_host"] == "cmdb.example.com"
    assert created["cursor"] is None  # no event handled yet (SDD §24.6)
    register(api, EMAIL)
    register(api, SLACK)
    # every role reads the catalog, by name
    for role in (*OTHERS, Role.admin):
        listed = api.get(CHANNELS, role=role)
        assert listed.status_code == 200, listed.text
        assert [c["name"] for c in listed.json()] == ["app-owners", "cmdb", "ops-slack"]
    assert api.get(f"{CHANNELS}/ops-slack", role=Role.viewer).json()["kind"] == "slack"
    assert api.get(f"{CHANNELS}/nope").status_code == 404
    # the cursor comes with the channel once it exists
    api.store.put("notification_cursor", NotificationCursor(channel="cmdb", seq=7))
    assert api.get(f"{CHANNELS}/cmdb").json()["cursor"]["seq"] == 7
    # the same name twice is a conflict; the server-owned fields are the server's
    assert api.post(CHANNELS, json=WEBHOOK).status_code == 409
    for role in OTHERS:
        assert (
            patch(api, "/notifications/channels/cmdb", {"enabled": False}, role).status_code == 403
        )
        assert delete(api, "/notifications/channels/cmdb", role).status_code == 403
        assert put(api, "/notifications/channels/cmdb/secret", {}, role).status_code == 403
    edited = patch(
        api, "/notifications/channels/cmdb", {"enabled": False, "description": "The CMDB"}
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["enabled"], edited.json()["description"]) == (False, "The CMDB")
    assert edited.json()["created_at"] == created["created_at"]
    for body in (
        {"name": "other"},
        {"kind": "slack"},
        {"created_at": "2026-01-01T00:00:00Z"},
        {"colour": "blue"},
    ):
        refused = patch(api, "/notifications/channels/cmdb", body)
        assert refused.status_code == 422 and "cannot be changed" in message(refused), body
    # a refused edit stores nothing: the merged channel is checked as a new one is
    refused = patch(api, "/notifications/channels/cmdb", {"url": "http://cmdb.example.com/"})
    assert refused.status_code == 422 and "https" in message(refused)
    refused = patch(api, "/notifications/channels/ops-slack", {"recipients": ["a@example.com"]})
    assert refused.status_code == 422 and "recipients" in message(refused)
    assert api.get(f"{CHANNELS}/cmdb", role=Role.admin).json()["url"] == WEBHOOK["url"]
    # the registration's own refusals
    for body, why in (
        ({**EMAIL, "recipients": []}, "recipients"),
        ({**EMAIL, "name": "owners", "recipients": ["Owners <owners@example.com>"]}, "recipients"),
        ({**EMAIL, "name": "owners", "url": "https://mail.example.com/"}, "url"),
        ({**WEBHOOK, "name": "cmdb2", "url": "https://svc:pw@cmdb.example.com/"}, "user"),
        ({"name": "cmdb2", "kind": "webhook"}, "url"),
        ({"name": "Ops Slack", "kind": "slack"}, "name"),
        ({"name": "teams", "kind": "teams"}, "kind"),
        ({**SLACK, "name": "ops2", "url": SLACK_URL}, "url"),
        ({**SLACK, "name": "ops2", "colour": "blue"}, "colour"),
    ):
        refused = api.post(CHANNELS, json=body)
        assert refused.status_code == 422, (body, refused.text)
        assert why in message(refused), (body, message(refused))
    assert len(api.get(CHANNELS).json()) == 3
    assert delete(api, "/notifications/channels/cmdb").status_code == 204
    assert api.get(f"{CHANNELS}/cmdb").status_code == 404
    assert delete(api, "/notifications/channels/cmdb").status_code == 404
    assert [(e.kind, e.data, e.actor) for e in events(api)] == [
        ("channel.created", {"name": "cmdb", "kind": "webhook"}, "admin-user"),
        ("channel.created", {"name": "app-owners", "kind": "email"}, "admin-user"),
        ("channel.created", {"name": "ops-slack", "kind": "slack"}, "admin-user"),
        (
            "channel.updated",
            {"name": "cmdb", "kind": "webhook", "fields": ["description", "enabled"]},
            "admin-user",
        ),
        ("channel.deleted", {"name": "cmdb", "kind": "webhook"}, "admin-user"),
    ]


def test_an_operator_subscribes_a_channel_to_a_running_plan_and_its_status_stays(api):
    """SDD §12, §24.4: a subscription is not a field of the plan - an operator adds or removes one
    at any time, a running plan's included, and the plan's status and updated_at stay as they
    are; an unknown channel, plan or topic is refused (422), the same channel twice for one plan
    is a conflict (409)."""
    register(api, SLACK)
    register(api, EMAIL)
    plan_id = make_plan(api)
    stored = api.store.get("plan", plan_id, Plan)
    api.store.put("plan", stored.model_copy(update={"status": PlanStatus.running}))
    before = api.store.get("plan", plan_id, Plan)
    for role in (Role.viewer,):
        assert subscribe(api, {"channel": "ops-slack", "plan_id": plan_id}, role).status_code == 403
    created = subscribe(api, {"channel": "ops-slack", "plan_id": plan_id})
    assert created.status_code == 201, created.text
    sub = created.json()
    assert sub["id"].startswith("sub-") and len(sub["id"]) == 12
    assert sub["channel"] == "ops-slack" and sub["plan_id"] == plan_id
    assert sub["topics"] == ["approval", "downtime", "problem"]  # the defaults (SDD §24.4)
    assert sub["created_by"] == "operator-user" and sub["created_at"]
    chosen = subscribe(
        api, {"channel": "app-owners", "plan_id": plan_id, "topics": ["outcome", "downtime"]}
    )
    assert chosen.status_code == 201 and chosen.json()["topics"] == ["outcome", "downtime"]
    after = api.store.get("plan", plan_id, Plan)
    assert after.status == PlanStatus.running and after.updated_at == before.updated_at
    assert not [e for e in api.store.events(since_seq=0, limit=100000) if e.kind == "plan.updated"]
    # listed by plan and by channel, for every role
    assert [s["id"] for s in api.get(f"{SUBSCRIPTIONS}?plan_id={plan_id}").json()] == sorted(
        [sub["id"], chosen.json()["id"]]
    )
    assert [s["id"] for s in api.get(f"{SUBSCRIPTIONS}?channel=ops-slack").json()] == [sub["id"]]
    assert api.get(f"{SUBSCRIPTIONS}?plan_id=plan-nope").json() == []
    # the refusals
    twice = subscribe(api, {"channel": "ops-slack", "plan_id": plan_id})
    assert twice.status_code == 409 and sub["id"] in message(twice)
    for body, why in (
        ({"channel": "nope", "plan_id": plan_id}, "channel"),
        ({"channel": "ops-slack", "plan_id": "plan-nope"}, "plan_id"),
        ({"channel": "ops-slack", "plan_id": plan_id, "topics": []}, "topics"),
        ({"channel": "ops-slack", "plan_id": plan_id, "topics": ["digest"]}, "topics"),
        ({"channel": "ops-slack", "plan_id": plan_id, "topics": ["problem", "problem"]}, "topics"),
        ({"channel": "ops-slack"}, "plan_id"),
        ({"channel": "ops-slack", "plan_id": plan_id, "created_by": "me"}, "created_by"),
    ):
        refused = subscribe(api, body)
        assert refused.status_code == 422, (body, refused.text)
        assert why in message(refused), (body, message(refused))
    assert len(api.get(SUBSCRIPTIONS).json()) == 2
    # removed by an operator, at any time
    assert delete(api, f"/notifications/subscriptions/{sub['id']}", Role.viewer).status_code == 403
    assert (
        delete(api, f"/notifications/subscriptions/{sub['id']}", Role.operator).status_code == 204
    )
    assert (
        delete(api, f"/notifications/subscriptions/{sub['id']}", Role.operator).status_code == 404
    )
    assert api.store.get("plan", plan_id, Plan).status == PlanStatus.running
    kinds = [
        (e.kind, e.data["channel"], e.data["plan_id"], e.data["topics"], e.actor)
        for e in events(api, ("subscription.",))
    ]
    assert kinds == [
        (
            "subscription.created",
            "ops-slack",
            plan_id,
            ["approval", "downtime", "problem"],
            "operator-user",
        ),
        ("subscription.created", "app-owners", plan_id, ["outcome", "downtime"], "operator-user"),
        (
            "subscription.deleted",
            "ops-slack",
            plan_id,
            ["approval", "downtime", "problem"],
            "operator-user",
        ),
    ]
    assert all(e.plan_id == plan_id for e in events(api, ("subscription.",)))


def test_only_an_admin_subscribes_a_channel_to_every_plan(api):
    """SDD §13.2, §24.4: a subscription to every plan (plan_id null) is an admin's - an operator
    who asks for one, or removes one, gets 403 and an auth.denied naming the admin role, as for
    an approver-only plan field; one per channel, as for a plan."""
    register(api, SLACK)
    refused = subscribe(api, {"channel": "ops-slack", "plan_id": None})
    assert refused.status_code == 403, refused.text
    assert "admin" in message(refused)
    denied = [e for e in api.store.events(since_seq=0, limit=100000) if e.kind == "auth.denied"]
    assert len(denied) == 1 and denied[0].data["required_role"] == "admin"
    assert denied[0].actor == "operator-user" and denied[0].data["method"] == "POST"
    created = subscribe(api, {"channel": "ops-slack", "plan_id": None}, Role.admin)
    assert created.status_code == 201, created.text
    sub = created.json()
    assert sub["plan_id"] is None and sub["created_by"] == "admin-user"
    again = subscribe(api, {"channel": "ops-slack", "plan_id": None}, Role.admin)
    assert again.status_code == 409 and "every plan" in message(again)
    # a plan's subscription of the same channel is another one
    plan_id = make_plan(api)
    assert subscribe(api, {"channel": "ops-slack", "plan_id": plan_id}).status_code == 201
    assert len(api.get(f"{SUBSCRIPTIONS}?channel=ops-slack").json()) == 2
    assert (
        delete(api, f"/notifications/subscriptions/{sub['id']}", Role.operator).status_code == 403
    )
    assert (
        len([e for e in api.store.events(since_seq=0, limit=100000) if e.kind == "auth.denied"])
        == 2
    )
    assert delete(api, f"/notifications/subscriptions/{sub['id']}").status_code == 204
    created_event = next(
        e for e in events(api, ("subscription.",)) if e.kind == "subscription.created"
    )
    assert created_event.data["plan_id"] is None and created_event.plan_id is None


def test_a_subscribed_channel_cannot_be_deleted(api):
    """SDD §12, §24.3: a channel a subscription names cannot be deleted (409, each subscription
    named with its plan); once none names it, the deletion takes its cursor and its secret with
    it, and the event says when the store kept the secret."""
    register(api, SLACK)
    register(api, WEBHOOK)
    plan_id = make_plan(api)
    one = subscribe(api, {"channel": "ops-slack", "plan_id": plan_id}).json()
    every = subscribe(api, {"channel": "ops-slack", "plan_id": None}, Role.admin).json()
    refused = delete(api, "/notifications/channels/ops-slack")
    assert refused.status_code == 409, refused.text
    assert f"{one['id']} (plan {plan_id})" in message(refused)
    assert f"{every['id']} (every plan)" in message(refused)
    assert api.get(f"{CHANNELS}/ops-slack").status_code == 200
    assert (
        delete(api, f"/notifications/subscriptions/{one['id']}", Role.operator).status_code == 204
    )
    assert delete(api, "/notifications/channels/ops-slack").status_code == 409
    assert delete(api, f"/notifications/subscriptions/{every['id']}").status_code == 204
    # the secret and the cursor go with the channel
    assert (
        put(api, "/notifications/channels/ops-slack/secret", {"url": SLACK_URL}).status_code == 200
    )
    secret_dir = api.settings.secrets_dir / "notify-ops-slack"
    assert (secret_dir / "url").read_text() == SLACK_URL
    api.store.put("notification_cursor", NotificationCursor(channel="ops-slack", seq=3))
    assert delete(api, "/notifications/channels/ops-slack").status_code == 204
    assert not secret_dir.exists()
    assert api.store.list("notification_cursor", NotificationCursor) == []
    deleted = [e for e in events(api) if e.kind == "channel.deleted"]
    assert deleted[-1].data == {"name": "ops-slack", "kind": "slack"}
    # a secret the store cannot remove is named, and the channel still goes (audit AA-03)
    assert (
        put(api, "/notifications/channels/cmdb/secret", {"signing_key": SIGNING_KEY}).status_code
        == 200
    )
    kept = api.settings.secrets_dir / "notify-cmdb"
    kept.chmod(0o500)
    api.settings.secrets_dir.chmod(0o500)
    try:
        assert delete(api, "/notifications/channels/cmdb").status_code == 204
    finally:
        api.settings.secrets_dir.chmod(0o700)
        kept.chmod(0o700)
    assert api.get(f"{CHANNELS}/cmdb").status_code == 404
    deleted = [e for e in events(api) if e.kind == "channel.deleted"]
    assert deleted[-1].data == {"name": "cmdb", "kind": "webhook", "secret_left": "notify-cmdb"}


def test_a_channels_secret_is_write_only_and_named_after_it(api):
    """SDD §12, §13.3, §24.3: an admin writes a Slack channel's URL or a webhook's signing key
    through the secret route; the store holds it as notify-{name}, no route returns it, the
    event names the field alone, an email channel has none, and a store that cannot write
    answers 503."""
    register(api, SLACK)
    register(api, WEBHOOK)
    register(api, EMAIL)
    assert put(api, "/notifications/channels/nope/secret", {"url": SLACK_URL}).status_code == 404
    written = put(api, "/notifications/channels/ops-slack/secret", {"url": SLACK_URL})
    assert written.status_code == 200, written.text
    assert written.json()["name"] == "ops-slack" and written.json()["url"] is None
    assert SLACK_URL not in written.text
    assert (api.settings.secrets_dir / "notify-ops-slack" / "url").read_text() == SLACK_URL
    signed = put(api, "/notifications/channels/cmdb/secret", {"signing_key": SIGNING_KEY})
    assert signed.status_code == 200 and SIGNING_KEY not in signed.text
    assert (api.settings.secrets_dir / "notify-cmdb" / "signing_key").read_text() == SIGNING_KEY
    for path in (CHANNELS, f"{CHANNELS}/ops-slack", f"{CHANNELS}/cmdb"):
        text = api.get(path, role=Role.admin).text
        assert SLACK_URL not in text and SIGNING_KEY not in text and "hooks.slack.com" not in text
    # the key per kind, and nothing for an email channel
    for path, body, why in (
        ("/notifications/channels/ops-slack/secret", {"signing_key": SIGNING_KEY}, "url"),
        ("/notifications/channels/ops-slack/secret", {"url": SLACK_URL, "x": 1}, "url"),
        ("/notifications/channels/ops-slack/secret", {}, "url"),
        ("/notifications/channels/cmdb/secret", {"url": SLACK_URL}, "signing_key"),
        ("/notifications/channels/app-owners/secret", {"url": SLACK_URL}, "email"),
        ("/notifications/channels/app-owners/secret", {"password": "x"}, "email"),
    ):
        refused = put(api, path, body)
        assert refused.status_code == 422, (path, body, refused.text)
        assert why in message(refused), (path, body, message(refused))
    # a rotation replaces the value without a restart (SDD §24.3)
    rotated = SLACK_URL + "rotated"
    assert put(api, "/notifications/channels/ops-slack/secret", {"url": rotated}).status_code == 200
    assert (api.settings.secrets_dir / "notify-ops-slack" / "url").read_text() == rotated
    # a store that cannot write: 503, the event not written
    before = len(events(api))
    # a regular file where the store keeps the secret's directory: the write cannot be made
    shutil.rmtree(api.settings.secrets_dir / "notify-cmdb")
    (api.settings.secrets_dir / "notify-cmdb").write_text("")
    try:
        failed = put(api, "/notifications/channels/cmdb/secret", {"signing_key": SIGNING_KEY})
    finally:
        (api.settings.secrets_dir / "notify-cmdb").unlink()
    assert failed.status_code == 503, failed.text
    assert "secret store" in message(failed) and SIGNING_KEY not in failed.text
    assert len(events(api)) == before
    updated = [e for e in events(api) if e.kind == "channel.updated"]
    assert [e.data for e in updated] == [
        {"name": "ops-slack", "kind": "slack", "fields": ["secret"]},
        {"name": "cmdb", "kind": "webhook", "fields": ["secret"]},
        {"name": "ops-slack", "kind": "slack", "fields": ["secret"]},
    ]


def test_a_slack_secret_must_be_a_slack_incoming_webhook_url(api):
    """SDD §24.3: a Slack channel's secret is a URL under https://hooks.slack.com/services/
    without user information, and a webhook's signing key whsec_ and the base64 of 24 to 64
    bytes; anything else is refused (422) and stores nothing."""
    register(api, SLACK)
    register(api, WEBHOOK)
    for value in (
        "https://example.com/services/T000/B000/x",
        "http://hooks.slack.com/services/T000/B000/x",
        "https://hooks.slack.com/x",
        "https://user:pw@hooks.slack.com/services/T000/B000/x",
        "https://hooks.slack.com/services/T000/B000/x y",
        "https://hooks.slack.com/services/T000/B000/x\n",
        "https://hooks.slack.com.example.com/services/T000/B000/x",
        "",
        42,
        None,
    ):
        refused = put(api, "/notifications/channels/ops-slack/secret", {"url": value})
        assert refused.status_code == 422, (value, refused.text)
        assert "url" in message(refused)
    assert not (api.settings.secrets_dir / "notify-ops-slack").exists()
    assert (
        put(api, "/notifications/channels/ops-slack/secret", {"url": SLACK_URL}).status_code == 200
    )
    key = lambda n: "whsec_" + base64.b64encode(bytes(range(n))).decode()  # noqa: E731
    for value in (
        key(23),
        key(65),
        "whsec_not base64!",
        base64.b64encode(bytes(32)).decode(),
        "whsec_",
        "",
        7,
    ):
        refused = put(api, "/notifications/channels/cmdb/secret", {"signing_key": value})
        assert refused.status_code == 422, (value, refused.text)
        assert "signing_key" in message(refused)
    assert not (api.settings.secrets_dir / "notify-cmdb").exists()
    for value in (key(24), key(64)):
        assert (
            put(api, "/notifications/channels/cmdb/secret", {"signing_key": value}).status_code
            == 200
        )
        assert (api.settings.secrets_dir / "notify-cmdb" / "signing_key").read_text() == value


def test_recipients_and_webhook_urls_are_shown_in_full_to_an_admin_alone(api):
    """SDD §12, §24.3, §24.8: recipients are personal data and a webhook's URL may hold a token,
    so an admin reads them in full and any other role their number and the URL's host."""
    register(api, EMAIL)
    register(api, WEBHOOK)
    full = api.get(f"{CHANNELS}/app-owners", role=Role.admin).json()
    assert full["recipients"] == EMAIL["recipients"] and full["recipient_count"] == 2
    assert full["url"] is None and full["url_host"] is None
    hook = api.get(f"{CHANNELS}/cmdb", role=Role.admin).json()
    assert hook["url"] == WEBHOOK["url"] and hook["url_host"] == "cmdb.example.com"
    for role in OTHERS:
        shown = api.get(f"{CHANNELS}/app-owners", role=role).json()
        assert shown["recipients"] == [] and shown["recipient_count"] == 2, role
        shown = api.get(f"{CHANNELS}/cmdb", role=role).json()
        assert shown["url"] is None and shown["url_host"] == "cmdb.example.com", role
        listed = api.get(CHANNELS, role=role).text
        assert "owners@example.com" not in listed and "/hooks/seamless" not in listed, role
    assert "owners@example.com" in api.get(CHANNELS, role=Role.admin).text


def test_the_catalogs_events_carry_no_url_recipient_or_secret(api):
    """SDD §4.3, §24.7: the channel.* and subscription.* events carry the channel's name and
    kind, the fields set, the subscription's id, channel, plan and topics - never a URL, a
    recipient, a secret's name's value or the signing key."""
    register(api, EMAIL)
    register(api, WEBHOOK)
    register(api, SLACK)
    plan_id = make_plan(api)
    assert (
        put(api, "/notifications/channels/ops-slack/secret", {"url": SLACK_URL}).status_code == 200
    )
    assert (
        put(api, "/notifications/channels/cmdb/secret", {"signing_key": SIGNING_KEY}).status_code
        == 200
    )
    edited = patch(api, "/notifications/channels/app-owners", {"recipients": ["new@example.com"]})
    assert edited.status_code == 200
    sub = subscribe(api, {"channel": "cmdb", "plan_id": plan_id}).json()
    assert (
        delete(api, f"/notifications/subscriptions/{sub['id']}", Role.operator).status_code == 204
    )
    assert delete(api, "/notifications/channels/cmdb").status_code == 204
    seen = events(api)
    assert {e.kind for e in seen} == {
        "channel.created",
        "channel.updated",
        "channel.deleted",
        "subscription.created",
        "subscription.deleted",
    }
    for e in seen:
        text = e.message + json.dumps(e.data)
        for forbidden in (
            "example.com",
            "example.org",
            "owners@",
            "new@",
            "hooks.slack.com",
            SLACK_URL,
            "whsec_",
            "/hooks/seamless",
        ):
            assert forbidden not in text, (e.kind, text)
        assert set(e.data) <= {
            "name",
            "kind",
            "fields",
            "secret_left",
            "id",
            "channel",
            "plan_id",
            "topics",
        }


def test_a_notify_secret_name_is_refused_for_a_provider_a_hook_and_a_dns_target(api):
    """SDD §13.3, §24.3: names that fold to notify-* belong to the channels' secrets, so a
    provider's credentials_secret or conversion_host.ssh_key_secret, a hook's secret and a DNS
    target's tsig_secret that folds to one are refused (422): writing a channel's secret must
    never replace theirs, and a channel must never read theirs."""
    for name in ("notify-ops-slack", "Notify-x", "notify_x", "NOTIFY.x"):
        refused = patch(api, "/providers/dst-rhoso", {"credentials_secret": name})
        assert refused.status_code == 422, (name, refused.text)
        assert "notify-" in message(refused), (name, message(refused))
        refused = patch(
            api,
            "/providers/src-osp",
            {"conversion_host": {"ssh_key_secret": name}},
        )
        assert refused.status_code == 422 and "notify-" in message(refused), (name, refused.text)
        refused = api.post(
            "/api/v1/hooks",
            json={
                "name": "dns-swap",
                "kind": "webhook",
                "url": "https://h.example.org/",
                "secret": name,
            },
        )
        assert refused.status_code == 422 and "notify-" in message(refused), (name, refused.text)
        target = {
            "kind": "rfc2136",
            "zone": "example.com.",
            "record": "web",
            "server": "ns1",
            "tsig_secret": name,
        }
        refused = api.post(
            "/api/v1/plans",
            role=Role.operator,
            json={**PLAN, "network_cutover": {"dns_targets": {"vm-1": target}}},
        )
        assert refused.status_code == 422 and "notify-" in message(refused), (name, refused.text)
    assert api.get("/api/v1/hooks").json() == [] and api.get("/api/v1/plans").json() == []
    # an edit is refused alike; a name that merely contains it is not
    plan_id = make_plan(api)
    target = {
        "kind": "rfc2136",
        "zone": "example.com.",
        "record": "web",
        "server": "ns1",
        "tsig_secret": "notify-x",
    }
    refused = patch(
        api,
        f"/plans/{plan_id}",
        {"network_cutover": {"dns_targets": {"vm-1": target}}},
        Role.operator,
    )
    assert refused.status_code == 422 and "notify-" in message(refused)
    allowed = patch(api, "/providers/dst-rhoso", {"credentials_secret": "my-notify-creds"})
    assert allowed.status_code == 200, allowed.text
    assert (
        patch(
            api,
            f"/plans/{plan_id}",
            {
                "network_cutover": {
                    "dns_targets": {"vm-1": {**target, "tsig_secret": "tsig-notify"}}
                }
            },
            Role.operator,
        ).status_code
        == 200
    )
