"""Provider management from the dashboard (SDD §4.2, §12, §13.3): PATCH, write-only credentials
and the conversion-host key in the platform secret store, distributions, secret resolution."""

import base64
import errno
import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from seamless_migrate.api.app import create_app
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import PlanStatus, Role
from seamless_migrate.domain.models import Plan, Provider
from seamless_migrate.security import secret_store as secret_store_module
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
    # the 409 comes before the merged provider is validated, for a name that is not a string too:
    # the order the dashboard's mock keeps (review RV21-07)
    assert patch(api, "/api/v1/providers/src-osp", {"name": None}).status_code == 409
    api.store.put(
        "plan",
        api.store.get("plan", plan.id, Plan).model_copy(update={"status": PlanStatus.completed}),
        expected_version=1,
    )
    assert patch(api, "/api/v1/providers/src-osp", {"name": "x"}).status_code == 200


def test_credentials_are_recorded_however_often_a_check_races_the_record(api, monkeypatch):
    """SDD §13.3: once the secret is written the change is recorded - never a 409 that leaves a
    replaced secret unaudited, however many checks land between the route's read and its write."""
    from seamless_migrate.store import AsyncStore

    db_put = AsyncStore.put
    raced: list[int] = []

    async def put_after_a_check(self, kind, model, expected_version=None):
        if kind == "provider" and expected_version is not None and len(raced) < 8:
            raced.append(expected_version)
            provider = api.store.get("provider", model.id, Provider)
            api.store.put("provider", provider.model_copy(update={"status": "ok"}))
        return await db_put(self, kind, model, expected_version=expected_version)

    monkeypatch.setattr(AsyncStore, "put", put_after_a_check)
    res = put(
        api,
        "/api/v1/providers/src-osp/credentials",
        {"username": "svc-migrate", "password": "pw-1", "project_name": "finance"},
    )
    assert res.status_code == 200, res.text
    assert len(raced) == 8
    assert api.store.get("provider", "src-osp", Provider).credentials_updated_at is not None
    events = api.store.events(since_seq=0)
    assert [e.kind for e in events].count("provider.credentials_updated") == 1


def test_credentials_written_while_a_check_updates_the_provider_are_recorded(api, monkeypatch):
    """SDD §13.3: a provider check finishing while the credentials are written bumps the
    provider's version; the route retries its versioned write, so the change is recorded —
    credentials_updated_at and one provider.credentials_updated event — and the provider returns
    to unknown: that check tested the old credentials. A 409 here would leave a replaced secret
    unaudited."""
    write = FileSecretStore.write
    checks: list[str] = []

    def write_while_a_check_finishes(self, name, values, labels):
        write(self, name, values, labels)
        if not checks:
            checks.append(name)
            provider = api.store.get("provider", "src-osp", Provider)
            api.store.put("provider", provider.model_copy(update={"status": "ok"}))

    monkeypatch.setattr(FileSecretStore, "write", write_while_a_check_finishes)
    res = put(
        api,
        "/api/v1/providers/src-osp/credentials",
        {"username": "svc-migrate", "password": "pw-1", "project_name": "finance"},
    )
    assert res.status_code == 200, res.text
    stored = api.store.get("provider", "src-osp", Provider)
    assert (stored.credentials_secret, stored.status) == ("provider-src-osp", "unknown")
    assert stored.credentials_updated_at is not None
    recorded = [
        e for e in api.store.events(since_seq=0) if e.kind == "provider.credentials_updated"
    ]
    assert [e.data["secret"] for e in recorded] == ["provider-src-osp"]

    # a check that finishes between the route's read of the provider and its write: the
    # versioned write conflicts and is retried on the fresh copy
    from seamless_migrate.store import AsyncStore

    db_put = AsyncStore.put
    raced: list[int] = []

    async def put_after_a_check(self, kind, model, expected_version=None):
        if kind == "provider" and expected_version is not None and not raced:
            raced.append(expected_version)
            provider = api.store.get("provider", model.id, Provider)
            api.store.put("provider", provider.model_copy(update={"status": "ok"}))
        return await db_put(self, kind, model, expected_version=expected_version)

    monkeypatch.setattr(AsyncStore, "put", put_after_a_check)
    res = put(
        api,
        "/api/v1/providers/src-osp/credentials",
        {"username": "svc-migrate", "password": "pw-2", "project_name": "finance"},
    )
    assert res.status_code == 200, res.text
    assert (
        raced and (api.settings.secrets_dir / "provider-src-osp" / "password").read_text() == "pw-2"
    )
    monkeypatch.setattr(AsyncStore, "put", db_put)

    checks.clear()
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nkey-material\n-----END OPENSSH PRIVATE KEY-----"
    res = put(api, "/api/v1/providers/dst-rhoso/conversion-key", {"private_key": key})
    assert res.status_code == 200, res.text
    stored = api.store.get("provider", "dst-rhoso", Provider)
    assert stored.conversion_key_updated_at is not None
    assert stored.conversion_host is not None
    assert stored.conversion_host.ssh_key_secret == "provider-dst-rhoso-ssh"


def test_credentials_of_a_provider_that_keeps_changing_answer_503_naming_the_stored_secret(
    api, monkeypatch
):
    """Audit B-25 (SDD §13.3): a provider rewritten between every read and write of the record is
    retried RECORD_SECRET_ATTEMPTS times, then the route answers 503 unavailable naming the secret
    the store already holds, so the operator sends the credentials again - never a hung request."""
    from seamless_migrate.api import routes_providers
    from seamless_migrate.store import AsyncStore, ConflictError

    db_put = AsyncStore.put
    attempts: list[int] = []

    async def rewritten_meanwhile(self, kind, model, expected_version=None):
        if kind == "provider" and expected_version is not None:
            attempts.append(expected_version)
            raise ConflictError(f"provider {model.id!r} is at version 9, not {expected_version}")
        return await db_put(self, kind, model, expected_version=expected_version)

    monkeypatch.setattr(AsyncStore, "put", rewritten_meanwhile)
    monkeypatch.setattr(routes_providers, "RECORD_SECRET_ATTEMPTS", 3)
    res = put(
        api,
        "/api/v1/providers/src-osp/credentials",
        {"username": "svc-migrate", "password": "pw-1", "project_name": "finance"},
    )
    assert res.status_code == 503, res.text
    error = res.json()["error"]
    assert error["code"] == "unavailable"
    assert "provider-src-osp" in error["message"] and "again" in error["message"]
    assert len(attempts) == 3
    # the store holds the value; the provider does not record it yet, and nothing was audited
    assert (api.settings.secrets_dir / "provider-src-osp" / "password").read_text() == "pw-1"
    stored = api.store.get("provider", "src-osp", Provider)
    assert stored.credentials_updated_at is None and stored.credentials_secret is None
    kinds = [e.kind for e in api.store.events(since_seq=0)]
    assert "provider.credentials_updated" not in kinds


def test_credentials_of_a_provider_deleted_meanwhile_answer_404_and_leave_no_secret(
    api, monkeypatch
):
    """SDD §13.3: a provider deleted while its credentials are written answers 404, and the
    secret just written is removed again — no secret outlives its provider."""
    write = FileSecretStore.write

    def write_while_the_provider_is_deleted(self, name, values, labels):
        write(self, name, values, labels)
        api.store.delete("provider", "src-osp")

    monkeypatch.setattr(FileSecretStore, "write", write_while_the_provider_is_deleted)
    res = put(
        api,
        "/api/v1/providers/src-osp/credentials",
        {"username": "svc-migrate", "password": "pw-1", "project_name": "finance"},
    )
    assert res.status_code == 404, res.text
    assert not (api.settings.secrets_dir / "provider-src-osp").exists()


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


@pytest.mark.parametrize(
    "bad", ["../etc", "-starts-with-dash", "has space", "a" * 129, ".", "..", "trailing\n"]
)
def test_a_secret_name_the_reader_could_not_use_is_refused(api, bad):
    """SDD §13.3: credentials_secret and conversion_host.ssh_key_secret name a secret managed
    outside Seamless; a name security.secrets would refuse to read is refused when written."""
    created = api.post(
        "/api/v1/providers", json={**VCENTER, "id": "vc-named", "credentials_secret": bad}
    )
    assert created.status_code == 422, created.text
    assert "credentials_secret" in created.json()["error"]["message"]
    assert patch(api, "/api/v1/providers/src-osp", {"credentials_secret": bad}).status_code == 422
    host = {"manage": False, "address": "10.0.0.5", "ssh_key_secret": bad}
    refused = patch(api, "/api/v1/providers/src-osp", {"conversion_host": host})
    assert refused.status_code == 422 and "ssh_key_secret" in refused.json()["error"]["message"]


def test_a_provider_stored_with_an_odd_secret_name_stays_editable(api):
    """Only the fields a PATCH sets are checked: a provider stored before the rule stays usable."""
    stored = api.store.get("provider", "src-osp", Provider)
    api.store.put("provider", stored.model_copy(update={"credentials_secret": "old name"}))
    renamed = patch(api, "/api/v1/providers/src-osp", {"name": "RHOSP 17.1 (finance, renamed)"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["credentials_secret"] == "old name"
    assert (
        patch(api, "/api/v1/providers/src-osp", {"credentials_secret": "old name"}).status_code
        == 422
    )


def test_an_id_whose_secret_names_are_another_providers_is_refused(api):
    """SDD §13.3: provider-{id} and provider-{id}-ssh must stay apart: 'src-osp-ssh' would store its
    credentials over src-osp's conversion key, and deleting it would remove that key."""
    clash = api.post("/api/v1/providers", json={**VCENTER, "id": "src-osp-ssh"})
    assert clash.status_code == 409, clash.text
    assert "src-osp" in clash.json()["error"]["message"]
    # the other way round: 'vc-a-ssh' first, then 'vc-a'
    assert api.post("/api/v1/providers", json={**VCENTER, "id": "vc-a-ssh"}).status_code == 201
    assert api.post("/api/v1/providers", json={**VCENTER, "id": "vc-a"}).status_code == 409
    # ids that only look alike are fine
    assert api.post("/api/v1/providers", json={**VCENTER, "id": "src-osp-sshd"}).status_code == 201


def test_a_provider_may_not_name_another_providers_secret(api):
    """SDD §13.3: provider-* names belong to the providers the store writes them for; a check would
    send another provider's credentials to this provider's endpoint."""
    other = api.post(
        "/api/v1/providers",
        json={**VCENTER, "id": "vc-x", "credentials_secret": "provider-src-osp"},
    )
    assert other.status_code == 422 and "another provider" in other.json()["error"]["message"]
    own = api.post(
        "/api/v1/providers", json={**VCENTER, "id": "vc-y", "credentials_secret": "provider-vc-y"}
    )
    assert own.status_code == 201, own.text
    host = {"manage": False, "address": "10.0.0.5", "ssh_key_secret": "provider-dst-rhoso-ssh"}
    assert patch(api, "/api/v1/providers/src-osp", {"conversion_host": host}).status_code == 422
    host["ssh_key_secret"] = "provider-src-osp-ssh"
    assert patch(api, "/api/v1/providers/src-osp", {"conversion_host": host}).status_code == 200


def test_provider_events_name_the_secrets_it_reads(api):
    """SDD §13.3: the audit trail says which secret a provider reads, never a value."""
    created = api.post(
        "/api/v1/providers", json={**VCENTER, "id": "vc-z", "credentials_secret": "vcenter-dc9"}
    )
    assert created.status_code == 201, created.text
    host = {"manage": False, "address": "10.0.0.5", "ssh_key_secret": "conv-dc9-key"}
    assert patch(api, "/api/v1/providers/vc-z", {"conversion_host": host}).status_code == 200
    events = api.store.events(since_seq=0, limit=100000)
    made = next(
        e for e in events if e.kind == "provider.created" and e.data["provider_id"] == "vc-z"
    )
    assert made.data["credentials_secret"] == "vcenter-dc9"
    changed = [
        e for e in events if e.kind == "provider.updated" and e.data["provider_id"] == "vc-z"
    ]
    assert changed[-1].data["ssh_key_secret"] == "conv-dc9-key"


def test_a_secret_managed_outside_seamless_can_be_named(api):
    """SDD §13.3, §16: the dialog names an existing secret; nothing is written to the store."""
    created = api.post(
        "/api/v1/providers",
        json={**VCENTER, "id": "vc-named", "credentials_secret": "vcenter-dc2.prod_1"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["credentials_secret"] == "vcenter-dc2.prod_1"
    host = {"manage": False, "address": "10.0.0.5", "ssh_key_secret": "conv-key"}
    patched = patch(
        api, "/api/v1/providers/src-osp", {"conversion_host": host, "credentials_secret": "osp-src"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["conversion_host"]["ssh_key_secret"] == "conv-key"
    assert patched.json()["credentials_secret"] == "osp-src"


CREDENTIAL_BODY_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "credential_body_cases.json").read_text(encoding="utf-8")
)["cases"]
#: what a case's "[pem]" stands for: an OpenSSH key's header, no key material
PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "A" * 64  # gitleaks:allow (a header, filler)


@pytest.mark.parametrize(
    "case",
    CREDENTIAL_BODY_CASES,
    ids=[f"{c['route']} {c['provider']}: {c['note']}" for c in CREDENTIAL_BODY_CASES],
)
def test_a_credential_body_is_judged_before_the_provider_is_looked_up(api, case):
    """SDD §12, §13.3 (reviews RV23-03, RV24-06): PUT /providers/{id}/credentials and
    /conversion-key refuse what their body alone gets wrong (422) before they look the provider up
    (404), for a provider they do not know too, and strip as Python's str.strip - the cases the
    dashboard's mock answers the same way (credential_body_cases.json)."""
    provider = "src-osp" if case["provider"] == "known" else case["provider"]
    body = {
        k: v.replace("[pem]", PEM) if k == "private_key" and isinstance(v, str) else v
        for k, v in case["body"].items()
    }
    answer = put(api, f"/api/v1/providers/{provider}/{case['route']}", body)
    assert answer.status_code == case["status"], answer.text
    if case["message"] is not None:
        assert answer.json()["error"]["message"] == case["message"]


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


def test_an_unwritable_store_names_the_setting_to_change(tmp_path):
    """SDD §13.3: a host run keeps the default /var/run/secrets/seamless, which a user cannot write;
    the 503 the dashboard shows says which setting points the store at a directory it can write."""
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
        assert res.status_code == 503
        message = res.json()["error"]["message"]
        assert message.startswith(
            f"secret store: cannot write the secret store at {blocker / 'secrets'}: "
        )
        assert message.endswith("; set SEAMLESS_SECRETS_DIR to a directory this process can write")
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


def test_file_store_that_cannot_write_says_which_setting_to_change(tmp_path, monkeypatch):
    """SDD §13.3: a user cannot write the default directory on a host run (Permission denied)."""

    def denied(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(secret_store_module.tempfile, "mkstemp", denied)
    with pytest.raises(SecretStoreError) as caught:
        FileSecretStore(tmp_path / "secrets").write("provider-x", {"password": "p"}, {})
    assert str(caught.value) == (
        f"cannot write the secret store at {tmp_path / 'secrets'}: Permission denied; "
        "set SEAMLESS_SECRETS_DIR to a directory this process can write"
    )


def test_file_store_root_lists_no_secret_names_to_other_users(tmp_path, monkeypatch):
    """SDD §13.3: the store creates its root 0700 and tightens one it owns, so other local users
    cannot list the secret names; a root it does not own (a mount) keeps its mode."""
    fresh = tmp_path / "fresh" / "secrets"
    FileSecretStore(fresh).write("provider-x", {"username": "u"}, {})
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o700

    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o755)
    FileSecretStore(loose).write("provider-x", {"username": "u"}, {})
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    os.chmod(foreign, 0o755)
    monkeypatch.setattr(os, "geteuid", lambda: foreign.stat().st_uid + 1)
    FileSecretStore(foreign).write("provider-x", {"username": "u"}, {})
    assert stat.S_IMODE(foreign.stat().st_mode) == 0o755
    assert FileSecretStore(foreign).read("provider-x") == {"username": "u"}


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


def test_kubernetes_store_reads_only_its_own_or_labelled_secrets():
    """SDD §13.3: never the deployment's own Secrets (seamless-db, seamless-tokens, ...)."""

    def secret(labels, **data):
        encoded = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
        return {"metadata": {"labels": labels}, "data": encoded}

    secrets = {
        "seamless-db": secret(
            {"app.kubernetes.io/part-of": "seamless-migrate"}, username="pg", password="x"
        ),
        "seamless-ext": secret({"seamless.io/provider-secret": "true"}, username="u", password="p"),
        "seamless-own": secret(
            {"app.kubernetes.io/managed-by": "seamless-migrate"}, username="o", password="q"
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=secrets[name]) if name in secrets else httpx.Response(404)

    store = KubernetesSecretStore(
        "ns", base_url="https://api.test", token="t", transport=httpx.MockTransport(handler)
    )
    assert store.read("db") is None
    assert store.read("ext") == {"username": "u", "password": "p"}
    assert store.read("own") == {"username": "o", "password": "q"}


def test_a_name_the_kubernetes_store_cannot_hold_reads_the_variables(tmp_path, monkeypatch):
    """SDD §13.3: upper-case or '_' is a valid secret name but not a Kubernetes one: the
    variables are read instead of the read failing."""
    settings = Settings(secrets_dir=tmp_path, secret_store="kubernetes", k8s_namespace="ns")
    from seamless_migrate.security import secret_store as module

    asked: list[str] = []

    class FakeStore:
        def __init__(self, namespace):
            pass

        def read(self, name):
            asked.append(name)
            raise module.SecretStoreError(f"invalid secret name {name!r}")

    monkeypatch.setattr(module, "KubernetesSecretStore", FakeStore)
    env = {"SEAMLESS_SECRET_VCENTER_DC2_USERNAME": "u", "SEAMLESS_SECRET_VCENTER_DC2_PASSWORD": "p"}
    assert read_secret("Vcenter_DC2", settings, env) == {"username": "u", "password": "p"}
    assert asked == []


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
