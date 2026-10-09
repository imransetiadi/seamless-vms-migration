"""Provider management from the dashboard (SDD §4.2, §12, §13.3): PATCH, write-only credentials
and the conversion-host key in the platform secret store, distributions, secret resolution."""

import base64
import json
import logging
import stat
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import PlanStatus, Role
from seamless_migrate.domain.models import Plan, Provider
from seamless_migrate.security.secret_store import (
    FileSecretStore,
    KubernetesSecretStore,
    SecretStoreError,
)
from seamless_migrate.security.secrets import (
    SecretNotFound,
    openstack_cloud_entry,
    read_secret,
    resolve,
    resolve_private_key,
)
from seamless_migrate.store import Store
from tests.api_support import DESTINATION, SOURCE, Api, api_settings
from tests.factories import make_plan, make_provider

KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"  # gitleaks:allow (header only, no key material)
    + "b3BlbnNzaC1rZXktdjEAAAAA" * 4
    + "\n-----END OPENSSH PRIVATE KEY-----"
)
VCENTER = {
    "id": "vc-dc2",
    "name": "vCenter DC2",
    "kind": "vmware",
    "role": "source",
    "endpoint": "https://vcenter.dc2.example/sdk",
    "distribution": "vmware",
}


@pytest.fixture
def api(tmp_path):
    settings, tokens = api_settings(tmp_path, secrets_dir=tmp_path / "secrets")
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        a = Api(client, store, settings, tokens)
        assert (
            a.post("/api/v1/providers", json={**SOURCE, "distribution": "rhosp"}).status_code == 201
        )
        assert a.post("/api/v1/providers", json=DESTINATION).status_code == 201
        yield a
    store.dispose()


def patch(api, path, body, role=Role.admin):
    return api.client.patch(path, headers=api.h(role), json=body)


def put(api, path, body, role=Role.admin):
    return api.client.put(path, headers=api.h(role), json=body)


def test_distribution_must_match_the_kind(api):
    bad = api.post("/api/v1/providers", json={**VCENTER, "id": "vc-bad", "distribution": "kolla"})
    assert bad.status_code == 422 and "belongs to kind openstack" in bad.text
    ok = api.post("/api/v1/providers", json=VCENTER)
    assert ok.status_code == 201 and ok.json()["distribution"] == "vmware"


def test_patch_updates_editable_fields_and_resets_the_status(api):
    api.post("/api/v1/providers/src-osp/check", Role.operator)
    assert api.get("/api/v1/providers/src-osp").json()["status"] == "ok"
    res = patch(
        api,
        "/api/v1/providers/src-osp",
        {
            "name": "RHOSP 17.1 DC1",
            "region": "regionTwo",
            "verify_tls": False,
            "distribution": "kolla",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["name"], body["region"], body["verify_tls"], body["distribution"]) == (
        "RHOSP 17.1 DC1",
        "regionTwo",
        False,
        "kolla",
    )
    assert body["status"] == "unknown" and body["capabilities"] == {}
    updated = [e for e in api.store.events(since_seq=0) if e.kind == "provider.updated"]
    assert updated[-1].data == {
        "provider_id": "src-osp",
        "fields": ["distribution", "name", "region", "verify_tls"],
    }
    # immutable and unknown fields, invalid values, a distribution of another kind
    for body, code in (
        ({"kind": "vmware"}, 422),
        ({"role": "destination"}, 422),
        ({"status": "ok"}, 422),
        ({"credentials_updated_at": "2026-01-01T00:00:00Z"}, 422),
        ({"distribution": "vmware"}, 422),
        ({"verify_tls": "maybe"}, 422),
    ):
        assert patch(api, "/api/v1/providers/src-osp", body).status_code == code, body
    assert patch(api, "/api/v1/providers/nope-1", {"name": "x"}).status_code == 404
    assert patch(api, "/api/v1/providers/src-osp", {"name": "x"}, Role.approver).status_code == 403


def test_patch_is_refused_while_a_running_plan_uses_the_provider(api):
    plan = make_plan(source_provider_id="src-osp", destination_provider_id="dst-rhoso")
    api.store.put("plan", plan.model_copy(update={"status": PlanStatus.running}))
    res = patch(api, "/api/v1/providers/src-osp", {"name": "x"})
    assert res.status_code == 409 and plan.id in res.text
    api.store.put(
        "plan",
        api.store.get("plan", plan.id, Plan).model_copy(update={"status": PlanStatus.completed}),
        expected_version=1,
    )
    assert patch(api, "/api/v1/providers/src-osp", {"name": "x"}).status_code == 200


def test_openstack_credentials_are_write_only(api, caplog):
    secret = "s3cr3t-Keystone-pw"
    with caplog.at_level(logging.DEBUG):
        res = put(
            api,
            "/api/v1/providers/src-osp/credentials",
            {"username": "svc-migrate", "password": secret, "project_name": "finance"},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["credentials_secret"] == "provider-src-osp"
    assert body["credentials_updated_at"] is not None and body["status"] == "unknown"
    # the value is in the store (0600 in a 0700 directory) and nowhere else
    directory = api.settings.secrets_dir / "provider-src-osp"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "password").stat().st_mode) == 0o600
    assert (directory / "password").read_text() == secret
    assert secret not in res.text and secret not in caplog.text
    assert secret not in api.get("/api/v1/providers/src-osp").text
    for e in api.store.events(since_seq=0):
        assert secret not in e.message and secret not in json.dumps(e.data)
    event = [e for e in api.store.events(since_seq=0) if e.kind == "provider.credentials_updated"][
        -1
    ]
    assert event.data["keys"] == ["password", "project_name", "username"]
    # the connection is built from the secret, domains default to Default, auth_url to the endpoint
    entry = openstack_cloud_entry(Provider.model_validate(body), api.settings)
    assert entry["auth_type"] == "password"
    assert entry["auth"] == {
        "auth_url": SOURCE["endpoint"],
        "username": "svc-migrate",
        "password": secret,
        "project_name": "finance",
        "user_domain_name": "Default",
        "project_domain_name": "Default",
    }
    # replacing the credentials replaces the whole secret (no stale password left)
    res = put(
        api,
        "/api/v1/providers/src-osp/credentials",
        {
            "application_credential_id": "ac-1",
            "application_credential_secret": "ac-s",
            "interface": "internal",
        },
    )
    assert res.status_code == 200
    assert not (directory / "password").exists()
    entry = openstack_cloud_entry(Provider.model_validate(res.json()), api.settings)
    assert entry["auth_type"] == "v3applicationcredential" and entry["interface"] == "internal"
    assert entry["auth"]["application_credential_secret"] == "ac-s"


def test_credential_validation_per_kind(api):
    assert api.post("/api/v1/providers", json=VCENTER).status_code == 201
    for path, body, needle in (
        (
            "/api/v1/providers/src-osp/credentials",
            {"username": "u", "password": "p"},
            "project_name",
        ),
        (
            "/api/v1/providers/src-osp/credentials",
            {"application_credential_id": "x"},
            "application_credential_secret",
        ),
        (
            "/api/v1/providers/src-osp/credentials",
            {"username": "u", "password": "p", "project_name": "x", "datacenter": "d"},
            "datacenter",
        ),
        (
            "/api/v1/providers/vc-dc2/credentials",
            {"username": "u", "password": "p", "project_name": "x"},
            "project_name",
        ),
        ("/api/v1/providers/vc-dc2/credentials", {"username": "u"}, "password"),
        (
            "/api/v1/providers/vc-dc2/credentials",
            {"username": "u", "password": "p", "token": "t"},
            "token",
        ),
    ):
        res = put(api, path, body)
        assert res.status_code == 422 and needle in res.text, (body, res.text)
    ok = put(
        api,
        "/api/v1/providers/vc-dc2/credentials",
        {"username": "svc@vsphere.local", "password": "pw", "datacenter": "DC2"},
    )
    assert ok.status_code == 200
    assert resolve("provider-vc-dc2", api.settings) == {
        "username": "svc@vsphere.local",
        "password": "pw",
        "datacenter": "DC2",
    }


def test_conversion_key_is_write_only(api):
    bad = put(
        api, "/api/v1/providers/src-osp/conversion-key", {"private_key": "ssh-ed25519 " + "A" * 80}
    )
    assert bad.status_code == 422 and "private key" in bad.text
    res = put(api, "/api/v1/providers/src-osp/conversion-key", {"private_key": KEY})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["conversion_host"]["ssh_key_secret"] == "provider-src-osp-ssh"
    assert body["conversion_host"]["name"] == "conv-src"  # the rest of the host config is kept
    assert body["conversion_key_updated_at"] is not None and KEY not in res.text
    path = api.settings.secrets_dir / "provider-src-osp-ssh" / "private_key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert resolve_private_key("provider-src-osp-ssh", api.settings) == KEY + "\n"


def test_deleting_a_provider_deletes_its_managed_secrets(api):
    assert api.post("/api/v1/providers", json=VCENTER).status_code == 201
    put(api, "/api/v1/providers/vc-dc2/credentials", {"username": "u", "password": "p"})
    put(api, "/api/v1/providers/vc-dc2/conversion-key", {"private_key": KEY})
    root = api.settings.secrets_dir
    assert (root / "provider-vc-dc2").is_dir() and (root / "provider-vc-dc2-ssh").is_dir()
    assert (
        api.client.delete("/api/v1/providers/vc-dc2", headers=api.h(Role.admin)).status_code == 204
    )
    assert not (root / "provider-vc-dc2").exists() and not (root / "provider-vc-dc2-ssh").exists()


def test_an_unwritable_store_answers_503(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    settings, tokens = api_settings(tmp_path, secrets_dir=blocker / "secrets")
    store = Store(f"sqlite:///{tmp_path / 'ro.db'}")
    store.create_schema()
    with TestClient(create_app(settings, store)) as client:
        api = Api(client, store, settings, tokens)
        api.post("/api/v1/providers", json=SOURCE)
        res = put(
            api,
            "/api/v1/providers/src-osp/credentials",
            {"username": "u", "password": "p", "project_name": "x"},
        )
        assert res.status_code == 503 and res.json()["error"]["code"] == "unavailable"
        assert api.get("/api/v1/providers/src-osp").json()["credentials_secret"] is None
    store.dispose()


# -- stores and resolution ------------------------------------------------------------------------


def test_file_store_roundtrip(tmp_path):
    store = FileSecretStore(tmp_path)
    assert store.read("provider-x") is None
    store.write("provider-x", {"username": "u", "password": "p"}, {})
    assert store.read("provider-x") == {"username": "u", "password": "p"}
    store.write("provider-x", {"username": "v"}, {})
    assert store.read("provider-x") == {"username": "v"}  # replaced as a whole
    assert not list((tmp_path / "provider-x").glob(".tmp-*"))
    store.delete("provider-x")
    store.delete("provider-x")  # idempotent
    assert store.read("provider-x") is None
    for bad in ("../x", "X", ""):
        with pytest.raises(SecretStoreError):
            store.write(bad, {"a": "b"}, {})
    with pytest.raises(SecretStoreError):
        store.write("ok", {"bad key": "v"}, {})


def test_kubernetes_store_against_a_fake_api_server():
    secrets: dict[str, dict] = {}
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization", "")))
        name = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET":
            return (
                httpx.Response(200, json=secrets[name]) if name in secrets else httpx.Response(404)
            )
        if request.method == "PUT":
            if name not in secrets:
                return httpx.Response(404)
            secrets[name] = json.loads(request.content)
            return httpx.Response(200, json=secrets[name])
        if request.method == "POST":
            body = json.loads(request.content)
            secrets[body["metadata"]["name"]] = body
            return httpx.Response(201, json=body)
        if request.method == "DELETE":
            return httpx.Response(200 if secrets.pop(name, None) else 404)
        return httpx.Response(405)

    store = KubernetesSecretStore(
        "seamless-migrate",
        base_url="https://api.test",
        token="sa-token",
        transport=httpx.MockTransport(handler),
    )
    assert store.read("provider-x") is None
    store.write("provider-x", {"username": "u", "password": "p"}, {"seamless.io/provider": "x"})
    stored = secrets["seamless-provider-x"]
    assert stored["metadata"]["labels"] == {
        "app.kubernetes.io/managed-by": "seamless-migrate",
        "seamless.io/provider": "x",
    }
    assert base64.b64decode(stored["data"]["password"]) == b"p"
    assert store.read("provider-x") == {"username": "u", "password": "p"}
    store.write("provider-x", {"username": "v", "password": "q"}, {})  # replace (PUT)
    assert store.read("provider-x") == {"username": "v", "password": "q"}
    store.delete("provider-x")
    store.delete("provider-x")  # 404 is fine
    assert ("POST", "/api/v1/namespaces/seamless-migrate/secrets", "Bearer sa-token") in seen
    assert all(
        path.startswith("/api/v1/namespaces/seamless-migrate/secrets") for _, path, _ in seen
    )

    forbidden = KubernetesSecretStore(
        "ns",
        base_url="https://api.test",
        token="t",
        transport=httpx.MockTransport(lambda r: httpx.Response(403)),
    )
    with pytest.raises(SecretStoreError, match="may not GET secrets in ns"):
        forbidden.read("provider-x")


def test_read_secret_order_files_then_store_then_environment(tmp_path, monkeypatch):
    settings = Settings(secrets_dir=tmp_path, secret_store="kubernetes", k8s_namespace="ns")
    from seamless_migrate.security import secret_store as module

    calls: list[str] = []

    class FakeStore:
        def __init__(self, namespace):
            calls.append(namespace)

        def read(self, name):
            return (
                {"username": "store-user", "password": "store-pw"} if name == "in-store" else None
            )

    monkeypatch.setattr(module, "KubernetesSecretStore", FakeStore)
    (tmp_path / "mounted").mkdir()
    (tmp_path / "mounted" / "username").write_text("file-user")
    (tmp_path / "mounted" / "password").write_text("file-pw")
    env = {
        "SEAMLESS_SECRET_ENV_ONLY_USERNAME": "env-user",
        "SEAMLESS_SECRET_ENV_ONLY_PASSWORD": "env-pw",
    }
    assert read_secret("mounted", settings, env)["username"] == "file-user"
    assert read_secret("in-store", settings, env)["username"] == "store-user"
    assert read_secret("env-only", settings, env)["username"] == "env-user"
    assert read_secret("nothing", settings, env) == {}
    assert calls == ["ns", "ns", "ns"]  # the store is not asked when files exist


def test_openstack_entry_falls_back_to_clouds_yaml_and_reports_gaps(tmp_path):
    clouds = tmp_path / "clouds.yaml"
    clouds.write_text(
        "clouds:\n  src:\n    auth: {auth_url: 'https://k/v3', username: a, password: b}\n"
    )
    settings = Settings(secrets_dir=tmp_path / "s", clouds_yaml=clouds)
    entry = openstack_cloud_entry(make_provider(cloud="src"), settings)
    assert entry["auth"]["username"] == "a"
    with pytest.raises(SecretNotFound, match="not configured"):
        openstack_cloud_entry(make_provider(credentials_secret="provider-missing"), settings)
    with pytest.raises(SecretNotFound, match="neither credentials nor"):
        openstack_cloud_entry(make_provider(cloud=None, credentials_secret=None), settings)
    FileSecretStore(tmp_path / "s").write("provider-half", {"username": "u"}, {})
    with pytest.raises(SecretNotFound, match="password, project_name"):
        openstack_cloud_entry(make_provider(credentials_secret="provider-half"), settings)


def test_registry_reconnects_after_new_credentials(tmp_path):
    from seamless_migrate.providers.registry import ProviderRegistry

    registry = ProviderRegistry(Settings(data_dir=tmp_path))
    provider = make_provider(id="src-1")
    first = registry.get(provider)
    assert registry.get(provider) is first
    stamped = provider.model_copy(
        update={"credentials_updated_at": datetime(2026, 10, 9, tzinfo=UTC)}
    )
    assert registry.get(stamped) is not first
