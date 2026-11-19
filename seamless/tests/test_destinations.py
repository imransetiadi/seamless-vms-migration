"""The destination guard (SDD §13.5; pentest 2026-11-19 P3, P4.5): where the control plane may
connect on an admin's word."""

import httpx
import pytest
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import Role
from seamless_migrate.domain.models import Hook, NotificationChannel, utcnow
from seamless_migrate.hooks import HookRunner
from seamless_migrate.notify.messages import Message
from seamless_migrate.notify.webhook import WebhookSender
from seamless_migrate.security import destinations
from seamless_migrate.security.destinations import (
    DestinationNotAllowed,
    check_destination,
    check_stored,
)
from seamless_migrate.store import Store
from tests.api_support import DESTINATION, SOURCE, Api, api_settings

REFUSED = [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8000/",
    "https://localhost/",
    "https://kubernetes.default.svc/",
    "https://kubernetes.default.svc.cluster.local:443/api",
    "https://[::1]/",
    "https://[::ffff:127.0.0.1]/",
    "https://[::ffff:169.254.169.254]/",
    "https://2130706433/",  # 127.0.0.1 as one decimal number
    "https://0x7f.0.0.1/",
    "https://127.1/",
    "https://0.0.0.0/",
    "https://[fe80::1]/",
    "https://224.0.0.1/",
    "https://[64:ff9b::7f00:1]/",  # NAT64 of 127.0.0.1
]


def settings(**kw) -> Settings:
    return Settings(**kw)


@pytest.mark.parametrize("url", REFUSED)
def test_a_loopback_link_local_or_cluster_destination_is_refused_in_any_spelling(url):
    """SDD §13.5: never a loopback, link-local, unspecified, multicast or reserved address - the
    metadata service among them - whatever the spelling, nor a name of the cluster's own DNS;
    refused before anything is resolved, so as a route stores the URL too."""
    resolved = []
    for check in (check_destination, check_stored):
        with pytest.raises(DestinationNotAllowed) as refused:
            kw = {"resolver": lambda host: resolved.append(host) or ["8.8.8.8"]}
            check(url, settings(), **(kw if check is check_destination else {}))
        assert "destination not allowed" in str(refused.value)
        assert "meta-data" not in str(refused.value)  # the host alone, never the path
    assert resolved == []


def test_a_name_is_judged_by_every_address_it_resolves_to():
    """SDD §13.5: a public name that resolves to an internal address (nip.io, a rebinding record)
    is refused, as one of whose addresses a single one is internal; a name that does not resolve
    is refused too; a public address passes."""
    answers = {
        "127.0.0.1.nip.io": ["127.0.0.1"],
        "meta.example.org": ["169.254.169.254"],
        "mixed.example.org": ["203.0.113.7", "::1"],
        "v6.example.org": ["fe80::1%eth0"],
        "keystone.example.org": ["203.0.113.7", "2001:db8::7"],
        "gone.example.org": [],
    }

    def resolver(host):
        if host == "broken.example.org":
            raise OSError("no answer")
        return answers[host]

    for host in ("127.0.0.1.nip.io", "meta.example.org", "mixed.example.org", "v6.example.org"):
        with pytest.raises(DestinationNotAllowed, match="resolves to"):
            check_destination(f"https://{host}:13000/v3", settings(), resolver=resolver)
    for host in ("gone.example.org", "broken.example.org"):
        with pytest.raises(DestinationNotAllowed, match="does not resolve"):
            check_destination(f"https://{host}/", settings(), resolver=resolver)
    assert check_destination(
        "https://keystone.example.org:13000/v3", settings(), resolver=resolver
    ) == ["203.0.113.7", "2001:db8::7"]
    # a bare host, as a conversion host's address is given
    assert check_destination("203.0.113.9", settings()) == ["203.0.113.9"]


def test_the_deployments_networks_are_refused_and_private_ones_follow_the_allowlist():
    """SDD §13.5, §15.1: SEAMLESS_EGRESS_DENY_CIDRS names the cluster's pod and service networks,
    always refused. A private address is allowed - an on-premises cloud lives there - unless
    SEAMLESS_EGRESS_ALLOW_PRIVATE_CIDRS is set: then only inside one of its networks. A public
    address never needs the allowlist."""
    plain = settings()
    assert check_destination("https://10.20.0.5:5000/v3", plain) == ["10.20.0.5"]
    assert check_destination("https://192.168.7.5/", plain) == ["192.168.7.5"]
    cluster = Settings.from_env(
        {"SEAMLESS_EGRESS_DENY_CIDRS": "10.42.0.0/16, 10.43.0.0/16,fd00:42::/48"}
    )
    for url in ("https://10.42.0.12/", "https://10.43.0.1:443/", "https://[fd00:42::1]/"):
        with pytest.raises(DestinationNotAllowed, match="SEAMLESS_EGRESS_DENY_CIDRS"):
            check_destination(url, cluster)
    assert check_destination("https://10.20.0.5/", cluster) == ["10.20.0.5"]
    narrow = Settings.from_env({"SEAMLESS_EGRESS_ALLOW_PRIVATE_CIDRS": "10.20.0.0/16"})
    assert check_destination("https://10.20.0.5/", narrow) == ["10.20.0.5"]
    assert check_destination("https://203.0.113.7/", narrow) == ["203.0.113.7"]
    for url in ("https://10.21.0.5/", "https://192.168.7.5/", "https://100.64.0.1/"):
        with pytest.raises(DestinationNotAllowed, match="SEAMLESS_EGRESS_ALLOW_PRIVATE_CIDRS"):
            check_destination(url, narrow)
    for name in ("SEAMLESS_EGRESS_DENY_CIDRS", "SEAMLESS_EGRESS_ALLOW_PRIVATE_CIDRS"):
        with pytest.raises(ValueError):
            Settings.from_env({name: "10.0.0.0/8,not-a-network"})


@pytest.fixture
def api(tmp_path):
    cfg, tokens = api_settings(tmp_path, demo=False, secrets_dir=tmp_path / "secrets")
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    store.create_schema()
    with TestClient(create_app(cfg, store)) as client:
        yield Api(client, store, cfg, tokens)
    store.dispose()


def message(response):
    return response.json()["error"]["message"]


def test_a_provider_endpoint_the_control_plane_never_connects_to_is_refused(api):
    """SDD §12, §13.5 (pentest P3): POST and PATCH /providers refuse (422) an endpoint or a brought
    conversion host's address that is a loopback, link-local or cluster destination, and plain
    http:// unless allow_plain_http is set; a private address stays allowed."""
    for endpoint in (
        "http://169.254.169.254/",
        "http://127.0.0.1:8000/",
        "https://kubernetes.default.svc/",
    ):
        refused = api.post("/api/v1/providers", json={**SOURCE, "endpoint": endpoint})
        assert refused.status_code == 422, (endpoint, refused.text)
        assert "destination not allowed" in message(refused), message(refused)
    refused = api.post(
        "/api/v1/providers", json={**SOURCE, "endpoint": "http://keystone.src.example:5000/v3"}
    )
    assert refused.status_code == 422 and "allow_plain_http" in message(refused)
    lab = {**SOURCE, "endpoint": "http://10.20.0.5:5000/v3", "allow_plain_http": True}
    assert api.post("/api/v1/providers", json=lab).status_code == 201
    assert api.post("/api/v1/providers", json=DESTINATION).status_code == 201

    def patch(provider, body):
        return api.client.patch(
            f"/api/v1/providers/{provider}", headers=api.h(Role.admin), json=body
        )

    refused = patch("dst-rhoso", {"endpoint": "https://[::ffff:169.254.169.254]/v3"})
    assert refused.status_code == 422 and "destination not allowed" in message(refused)
    refused = patch("src-osp", {"allow_plain_http": False})
    assert refused.status_code == 422 and "allow_plain_http" in message(refused)
    host = {**SOURCE["conversion_host"], "manage": False, "address": "127.0.0.1"}
    refused = patch("src-osp", {"conversion_host": host})
    assert refused.status_code == 422 and "conversion_host.address" in message(refused)
    # a field the request does not set is not judged: a provider stored before stays editable
    assert patch("src-osp", {"name": "Lab"}).status_code == 200


def test_a_hook_or_channel_url_the_control_plane_never_connects_to_is_refused(api):
    """SDD §12, §13.5 (pentest P4.5): a webhook hook's and a webhook channel's URL is judged as
    it is stored."""
    for url in ("https://169.254.169.254/", "https://localhost/x", "https://10.0.0.1.svc/"):
        hook = {"name": "h1", "kind": "webhook", "url": url}
        refused = api.post("/api/v1/hooks", json=hook)
        assert refused.status_code == 422 and "destination not allowed" in message(refused), url
        channel = {"name": "c1", "kind": "webhook", "url": url}
        refused = api.post("/api/v1/notifications/channels", json=channel)
        assert refused.status_code == 422 and "destination not allowed" in message(refused), url
    hook = {"name": "h1", "kind": "webhook", "url": "https://hooks.example.org/x"}
    assert api.post("/api/v1/hooks", json=hook).status_code == 201
    refused = api.client.patch(
        "/api/v1/hooks/h1", headers=api.h(Role.admin), json={"url": "https://127.0.0.1/"}
    )
    assert refused.status_code == 422 and "destination not allowed" in message(refused)


async def test_a_hook_is_never_sent_to_an_internal_address(tmp_path, monkeypatch):
    """SDD §7.2, §13.5 (pentest P4.5, "not tested yet"): a webhook hook whose name resolves to an
    internal address when it runs fails with `destination not allowed`, and no request - no
    bearer token - leaves; so does a webhook channel's message, final."""
    secrets = tmp_path / "secrets"
    (secrets / "hook-inside").mkdir(parents=True)
    (secrets / "hook-inside" / "token").write_text("t0ken")
    cfg = Settings(data_dir=tmp_path / "data", secrets_dir=secrets)
    sent = []

    class Recording(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            sent.append(request)
            return httpx.Response(204)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", Recording)
    monkeypatch.setattr(destinations, "system_resolver", lambda host: ["10.42.0.9", "127.0.0.1"])
    hook = Hook(name="inside", kind="webhook", url="https://rebound.example.org/x")
    context = {"point": "post_create", "idempotency_key": "mig-1.1.post_create"}
    run = await HookRunner(cfg).run(hook, context, tmp_path / "run")
    assert (run.outcome, run.detail) == ("failed", "destination not allowed")
    channel = NotificationChannel(name="cmdb", kind="webhook", url="https://rebound.example.org/x")
    message_ = Message(
        trigger="test",
        title="t",
        lines=[],
        plan_id="plan-0000abcd",
        plan_name="Finance apps",
        event_seq=1,
        event_kind="migration.failed",
        event_ts=utcnow(),
    )
    result = await WebhookSender(cfg).send(
        channel, message_, "msg_1", {"signing_key": "whsec_" + "QUJD" * 8}
    )
    assert (result.outcome, result.reason) == ("failed", "destination_not_allowed")
    assert sent == []
