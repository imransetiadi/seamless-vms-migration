"""Provider management from the dashboard (SDD §4.2, §12, §13.3): PATCH, write-only credentials
and the conversion-host key in the platform secret store, distributions, secret resolution, and a
provider's credentials per Keystone project (§27.4)."""

import base64
import contextlib
import errno
import hashlib
import inspect
import json
import logging
import os
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from seamless_migrate.api import routes_providers
from seamless_migrate.api.app import create_app
from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import Phase, PlanStatus, Role
from seamless_migrate.domain.models import HostKeyPin, Plan, Provider
from seamless_migrate.security import secret_store as secret_store_module
from seamless_migrate.security.secret_store import (
    FileSecretStore,
    KubernetesSecretStore,
    SecretStoreError,
    kubernetes_name,
)
from seamless_migrate.security.secrets import (
    SecretNotFound,
    openstack_cloud_entry,
    read_secret,
    resolve,
    resolve_private_key,
    valid_secret_name,
)
from seamless_migrate.store import Store
from tests.api_support import DESTINATION, SOURCE, Api, api_settings
from tests.factories import make_migration, make_plan, make_provider
from tests.key_support import bare, ecdsa_key, other_key, public_key, rsa_key

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


@pytest.mark.parametrize(
    "name",
    ["Provider-dst-rhoso", "PROVIDER-DST-RHOSO", "provider_dst-rhoso", "provider.dst.rhoso"],
)
def test_a_provider_may_not_name_another_providers_secret_in_another_spelling(api, name):
    """SDD §13.3 (audit AP-04): the readers fold a secret's name - a case-insensitive filesystem
    (a host run on macOS or Windows) opens provider-dst-rhoso for Provider-dst-rhoso, and the
    SEAMLESS_SECRET_ variables upper-case it and turn every other character into _ - so the guard
    folds it too: a spelling of another provider's secret is refused like the name itself."""
    taken = api.post(
        "/api/v1/providers", json={**VCENTER, "id": "vc-x", "credentials_secret": name}
    )
    assert taken.status_code == 422, taken.text
    assert "another provider" in taken.json()["error"]["message"]
    host = {"manage": False, "address": "10.0.0.5", "ssh_key_secret": name + "-ssh"}
    assert patch(api, "/api/v1/providers/src-osp", {"conversion_host": host}).status_code == 422


def test_only_the_exact_name_of_a_providers_own_secret_is_its_own(api):
    """SDD §13.3 (audit AP-04): a provider names its own provider-* secret exactly; another
    spelling of it is no secret the store writes, and names that only start alike are free."""
    own = api.post(
        "/api/v1/providers", json={**VCENTER, "id": "vc-y", "credentials_secret": "Provider-vc-y"}
    )
    assert own.status_code == 422, own.text
    for free in ("providers-dc1", "providerless", "my-provider-dc1"):
        made = api.post(
            "/api/v1/providers",
            json={**VCENTER, "id": f"vc-{free[:9].strip('-')}", "credentials_secret": free},
        )
        assert made.status_code == 201, (free, made.text)


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


#: entries ssh_host_keys refuses, each with what the 422 says of it (SDD §4.2, §12, §26.3)
NO_PUBLIC_KEY = {
    "options": (f'restrict,command="/bin/true" {public_key(7)}', "options or a host name"),
    "host name": (f"conv-src.example.org,192.0.2.10 {public_key(7)}", "options or a host name"),
    "private key": (KEY, "a private key"),
    "one-line private key": (
        "-----BEGIN RSA PRIVATE KEY----- MIIEpAIBAAKCAQEA",  # gitleaks:allow (a header, no key)
        "a private key",
    ),
    "ssh-dss": (other_key("ssh-dss", 7), "ssh-dss is not an ed25519, ECDSA or RSA key"),
    "security key": (
        other_key("sk-ssh-ed25519@openssh.com", 7),
        "sk-ssh-ed25519@openssh.com is not an ed25519, ECDSA or RSA key",
    ),
    "certificate": (
        other_key("ssh-ed25519-cert-v01@openssh.com", 7),
        "ssh-ed25519-cert-v01@openssh.com is not an ed25519, ECDSA or RSA key",
    ),
    "another type's key": (f"ssh-rsa {public_key(7).split()[1]}", "no ssh-rsa key"),
    "a cut key": (public_key(7)[:40], "no ssh-ed25519 key"),
    "two lines": (f"{public_key(7)}\n{public_key(8)}", "more than one line"),
    "empty": ("  ", "empty"),
    "no key": ("hello world", "no OpenSSH public key"),
}


@pytest.mark.parametrize("case", sorted(NO_PUBLIC_KEY))
def test_a_declared_host_key_that_is_no_public_key_is_refused(api, case):
    """SDD §4.2, §12, §26.3: a conversion host's declared keys are OpenSSH one-line public keys,
    ed25519, ECDSA or RSA. Anything else is refused with 422 naming its line and why - on POST and
    PATCH alike - and never quoted: the line may be a private key."""
    entry, why = NO_PUBLIC_KEY[case]
    keys = [public_key(5, "root@conv-x"), entry]
    host = {"name": "conv-x", "ssh_host_keys": keys}
    created = api.post("/api/v1/providers", json={**SOURCE, "id": "src-x", "conversion_host": host})
    patched = patch(api, "/api/v1/providers/src-osp", {"conversion_host": host})
    for res in (created, patched):
        assert res.status_code == 422, res.text
        message = res.json()["error"]["message"]
        assert message.startswith("conversion_host.ssh_host_keys: line 2: "), message
        assert why in message, message
        # no part of the line but a type the message names: a base64 field, a private key's body
        quoted = [word for word in entry.split() if len(word) >= 12 and word not in why]
        assert [word for word in quoted if word in res.text] == []
    assert api.get("/api/v1/providers/src-x").status_code == 404
    stored = api.get("/api/v1/providers/src-osp").json()["conversion_host"]
    assert stored["ssh_host_keys"] == []
    # each type it takes, a comment or none, stored as declared
    good = [
        public_key(1, "root@conv-x"),
        ecdsa_key(2),
        ecdsa_key(3, "root@conv-x", curve="nistp384"),
        ecdsa_key(4, curve="nistp521"),
        rsa_key(5, "root@conv-x"),
    ]
    res = patch(
        api, "/api/v1/providers/src-osp", {"conversion_host": {**host, "ssh_host_keys": good}}
    )
    assert res.status_code == 200, res.text
    assert res.json()["conversion_host"]["ssh_host_keys"] == good


def test_the_pin_is_returned_and_cannot_be_written(api):
    """SDD §4.2, §12, §26.4: the conversion host's pin is the server's - GET returns it, PATCH
    refuses it as it refuses every server-owned field, POST ignores it -, and an edit of what the
    operator owns, its conversion host's declared keys or its key, keeps it: it is its server's."""
    pin = {
        "server_id": "srv-conv-src",
        "keys": [bare(public_key(1, "root@conv-src"))],
        "source": "console",
        "pinned_at": "2026-10-23T08:00:00Z",
    }
    stored = api.store.get("provider", "src-osp", Provider)
    api.store.put("provider", stored.model_copy(update={"conversion_host_pin": HostKeyPin(**pin)}))
    assert api.get("/api/v1/providers/src-osp").json()["conversion_host_pin"] == pin
    listed = {p["id"]: p["conversion_host_pin"] for p in api.get("/api/v1/providers").json()}
    assert listed == {"src-osp": pin, "dst-rhoso": None}
    forged = {**pin, "server_id": "srv-elsewhere", "keys": [bare(public_key(2))]}
    for value in (forged, None):
        refused = patch(api, "/api/v1/providers/src-osp", {"conversion_host_pin": value})
        assert refused.status_code == 422, refused.text
        assert "fields cannot be changed: conversion_host_pin" in refused.text
    declared = {"name": "conv-src", "ssh_host_keys": [public_key(3, "root@conv-src")]}
    edited = patch(api, "/api/v1/providers/src-osp", {"name": "x", "conversion_host": declared})
    assert edited.status_code == 200 and edited.json()["conversion_host_pin"] == pin
    keyed = put(api, "/api/v1/providers/src-osp/conversion-key", {"private_key": KEY})
    assert keyed.status_code == 200 and keyed.json()["conversion_host_pin"] == pin
    assert api.get("/api/v1/providers/src-osp").json()["conversion_host_pin"] == pin
    created = api.post(
        "/api/v1/providers", json={**SOURCE, "id": "src-two", "conversion_host_pin": forged}
    )
    assert created.status_code == 201 and created.json()["conversion_host_pin"] is None
    assert api.get("/api/v1/providers/src-two").json()["conversion_host_pin"] is None


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


def test_a_provider_deletion_the_store_cannot_finish_is_audited_naming_what_it_left(api):
    """SDD §12, §13.2 (audit AA-03): the files store let an OSError other than "not found" out of
    a deletion - a root remounted read-only, another owner's mount -, so DELETE /providers/{id}
    answered 500 after the provider was gone, wrote no provider.deleted, and stopped before the
    conversion key. Each secret is tried, the deletion is audited, and the event names what the
    store left."""
    assert api.post("/api/v1/providers", json=VCENTER).status_code == 201
    put(api, "/api/v1/providers/vc-dc2/credentials", {"username": "u", "password": "p"})
    put(api, "/api/v1/providers/vc-dc2/conversion-key", {"private_key": KEY})
    root = api.settings.secrets_dir
    os.chmod(root, 0o500)  # the store's root turned read-only
    try:
        res = api.client.delete("/api/v1/providers/vc-dc2", headers=api.h(Role.admin))
    finally:
        os.chmod(root, 0o700)
    assert res.status_code == 204, res.text
    assert api.get("/api/v1/providers/vc-dc2").status_code == 404
    [deleted] = [e for e in api.store.events() if e.kind == "provider.deleted"]
    assert deleted.data == {
        "provider_id": "vc-dc2",
        # the Ceph key was never written, so its "not found" is no secret left (SDD §13.3)
        "secrets_left": ["provider-vc-dc2", "provider-vc-dc2-ssh"],
    }


def _unbuildable_store(settings):
    """A Kubernetes store without its ServiceAccount: it cannot be built (SDD §13.3)."""
    raise SecretStoreError("no ServiceAccount namespace at /var/run/secrets/kubernetes.io")


@pytest.mark.parametrize("failure", ["unsearchable_root", "unbuildable_store"])
def test_a_provider_deletion_whose_store_cannot_be_searched_or_built_is_audited_naming_what_it_left(
    api, monkeypatch, failure
):
    """SDD §12, §13.3 (review RV52-X2): a root the process may not search (mode 000, a changed
    UID) raised PermissionError from the store's "already gone?" check, outside its guard, and a
    store that could not be built raised before the guarded loop - either way 500 after the
    provider was gone, no provider.deleted, and the secrets left unnamed."""
    assert api.post("/api/v1/providers", json=VCENTER).status_code == 201
    put(api, "/api/v1/providers/vc-dc2/credentials", {"username": "u", "password": "p"})
    put(api, "/api/v1/providers/vc-dc2/conversion-key", {"private_key": KEY})
    root = api.settings.secrets_dir
    if failure == "unbuildable_store":
        monkeypatch.setattr(routes_providers, "secret_store", _unbuildable_store)
    elif os.geteuid() == 0:
        pytest.skip("root searches a directory of mode 000")
    else:
        os.chmod(root, 0o000)
    try:
        res = api.client.delete("/api/v1/providers/vc-dc2", headers=api.h(Role.admin))
    finally:
        os.chmod(root, 0o700)
    assert res.status_code == 204, res.text
    assert api.get("/api/v1/providers/vc-dc2").status_code == 404
    [deleted] = [e for e in api.store.events() if e.kind == "provider.deleted"]
    assert deleted.data == {
        "provider_id": "vc-dc2",
        "secrets_left": ["provider-vc-dc2", "provider-vc-dc2-ssh", "provider-vc-dc2.ceph"],
    }


def test_a_secret_store_that_cannot_be_built_answers_503_to_a_credentials_write(api, monkeypatch):
    """SDD §13.3 (review RV52-X2): a store the control plane cannot build is unavailable, as one
    it cannot write - 503, never a 500."""
    assert api.post("/api/v1/providers", json=VCENTER).status_code == 201
    monkeypatch.setattr(routes_providers, "secret_store", _unbuildable_store)
    res = api.client.put(
        "/api/v1/providers/vc-dc2/credentials",
        json={"username": "u", "password": "p"},
        headers=api.h(Role.admin),
    )
    assert res.status_code == 503, res.text
    assert "ServiceAccount" in res.json()["error"]["message"]


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


def test_two_writes_of_one_secret_at_once_leave_one_secret_whole(tmp_path, monkeypatch):
    """SDD §13.3 (audit AA-02): two writes of one secret - a password rotation and a switch to an
    application credential, saved at once - each ran its stale-key pass before the other wrote:
    both answered and the secret held the keys of both, the replaced password stored beside the
    credential the connection then signs in with. A secret is written under a lock of its name:
    the last write's secret stands whole."""
    import tempfile as real_tempfile
    import threading

    met = threading.Barrier(2)
    first = threading.local()

    class MeetFirst:
        """Each writer waits for the other once, past its stale-key pass: unlocked, both meet there
        and write over each other; locked, the second waits for the lock and the first goes on."""

        @staticmethod
        def mkstemp(*args, **kwargs):
            if not getattr(first, "met", False):
                first.met = True
                with contextlib.suppress(threading.BrokenBarrierError):
                    met.wait(timeout=0.5)
            return real_tempfile.mkstemp(*args, **kwargs)

    monkeypatch.setattr(secret_store_module, "tempfile", MeetFirst)
    store = FileSecretStore(tmp_path)
    password = {"username": "ops", "password": "Old-Password-1", "project_name": "finance"}
    app_cred = {"application_credential_id": "ac-1", "application_credential_secret": "s3cr3t"}
    writers = [
        threading.Thread(target=store.write, args=("provider-x", values, {}))
        for values in (password, app_cred)
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(10)
    assert store.read("provider-x") in (password, app_cred)
    assert not list((tmp_path / "provider-x").glob(".tmp-*"))


def test_a_deletion_of_a_secret_waits_for_its_write(tmp_path, monkeypatch):
    """SDD §13.3 (audit AA-02): a deletion takes the secret's lock too - one landing while a write
    is under way would take the directory from under it, and the write would fail halfway, its
    keys partly written. It waits, then deletes the whole secret."""
    import tempfile as real_tempfile
    import threading
    import time

    entered, go = threading.Event(), threading.Event()

    class Pause:
        @staticmethod
        def mkstemp(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                go.wait(timeout=5)
            return real_tempfile.mkstemp(*args, **kwargs)

    store = FileSecretStore(tmp_path)
    store.write("provider-x", {"username": "old"}, {})
    monkeypatch.setattr(secret_store_module, "tempfile", Pause)
    errors: list[BaseException] = []

    def write():
        try:
            store.write("provider-x", {"username": "u", "password": "p"}, {})
        except BaseException as exc:  # noqa: BLE001 - what the write met
            errors.append(exc)

    writer = threading.Thread(target=write)
    writer.start()
    assert entered.wait(5)
    deleter = threading.Thread(target=store.delete, args=("provider-x",))
    deleter.start()
    time.sleep(0.2)  # unlocked, the deletion runs now, under the paused write
    go.set()
    writer.join(10)
    deleter.join(10)
    assert errors == []
    assert store.read("provider-x") is None


def test_a_secret_the_file_store_cannot_remove_is_a_store_error(tmp_path):
    """SDD §13.3 (audit AA-03): a directory the store cannot change answers as a write it cannot
    make - a SecretStoreError naming the store, never a bare OSError (its keys may be gone, the
    directory left) -, a deletion once it can finishes, and one already gone is no error."""
    store = FileSecretStore(tmp_path)
    store.write("provider-x", {"username": "u"}, {})
    os.chmod(tmp_path, 0o500)
    try:
        with pytest.raises(SecretStoreError, match=f"secret store at {tmp_path}"):
            store.delete("provider-x")
    finally:
        os.chmod(tmp_path, 0o700)
    assert (tmp_path / "provider-x").is_dir()
    store.delete("provider-x")
    store.delete("provider-x")
    assert store.read("provider-x") is None
    # nothing to delete in a store not written yet: no error, and nothing created for it
    FileSecretStore(tmp_path / "none").delete("provider-x")
    assert not (tmp_path / "none").exists()


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


def test_the_kubernetes_secret_store_always_checks_the_api_servers_certificate(monkeypatch):
    """SDD §13.3, §17 (CodeQL py/request-without-cert-validation): the store checks the API
    server's certificate against the ServiceAccount's CA, or a CA file it is given - no parameter
    turns the check off."""
    import httpx

    from seamless_migrate.security import secret_store

    seen: list = []
    real = httpx.Client

    def client(**kwargs):
        seen.append(kwargs.get("verify"))
        return real(base_url=kwargs["base_url"])

    monkeypatch.setattr(secret_store.httpx, "Client", client)
    given = secret_store.KubernetesSecretStore("ns", token="t", ca_cert="/etc/pki/api-ca.crt")
    default = secret_store.KubernetesSecretStore("ns", token="t")
    given._client().close()
    default._client().close()
    assert seen == ["/etc/pki/api-ca.crt", str(secret_store.SA_DIR / "ca.crt")]
    # no parameter can turn the check off: the store takes a CA file, never a verify switch
    parameters = inspect.signature(secret_store.KubernetesSecretStore).parameters
    assert "ca_cert" in parameters and "verify" not in parameters


# --------------------------------------------------------------------------------------------
# the source cluster's Ceph access and its key (SDD §4.2, §12, §13.3, §28.4; plan task CE6)

CEPH = {
    "fsid": "0b2a0b6c-5e8d-4c21-9f3e-1c0f3b1a2d3e",
    "monitors": ["mon1.ceph.example", "mon2.ceph.example:6789"],
    "client": "seamless",
}
#: a CephX key's shape - 28 bytes, base64 - with no key material (synthetic)
CEPH_KEY = "AQFakeCephKeyForTestsOnlyNotRealAtAll0=="  # gitleaks:allow


def test_a_source_provider_records_its_ceph_access(api):
    """SDD §4.2, §28.4: an OpenStack source declares its cluster's fsid, monitors and user; the key
    is not among them, and key_updated_at starts null."""
    res = patch(api, "/api/v1/providers/src-osp", {"ceph": CEPH})
    assert res.status_code == 200, res.text
    assert res.json()["ceph"] == {**CEPH, "key_updated_at": None}
    assert api.get("/api/v1/providers/src-osp").json()["ceph"] == {**CEPH, "key_updated_at": None}
    created = api.post("/api/v1/providers", json={**SOURCE, "id": "src-two", "ceph": CEPH})
    assert created.status_code == 201, created.text
    assert created.json()["ceph"]["key_updated_at"] is None
    # the client defaults to seamless; the shapes of §4.2 are checked
    short = patch(
        api, "/api/v1/providers/src-osp", {"ceph": {k: v for k, v in CEPH.items() if k != "client"}}
    )
    assert short.status_code == 200 and short.json()["ceph"]["client"] == "seamless"
    for bad, word in (
        ({**CEPH, "fsid": "not-a-uuid"}, "fsid"),
        ({**CEPH, "monitors": []}, "monitors"),
        ({**CEPH, "monitors": [f"mon{i}.example" for i in range(10)]}, "monitors"),
        ({**CEPH, "monitors": ["mon1.example", "not a host"]}, "monitors"),
        ({**CEPH, "client": "client.seamless"}, "client"),
        ({**CEPH, "client": ""}, "client"),
        ({**CEPH, "key": CEPH_KEY}, "key"),
    ):
        res = patch(api, "/api/v1/providers/src-osp", {"ceph": bad})
        assert res.status_code == 422 and word in res.json()["error"]["message"], (bad, res.text)


def test_ceph_access_is_refused_on_vmware_and_on_a_destination(api):
    """SDD §4.2, §28.4: the extents are the source cluster's; a vCenter has no Ceph, and a
    destination's Ceph is never read."""
    res = api.post("/api/v1/providers", json={**VCENTER, "ceph": CEPH})
    assert res.status_code == 422 and "ceph" in res.json()["error"]["message"], res.text
    res = patch(api, "/api/v1/providers/dst-rhoso", {"ceph": CEPH})
    assert res.status_code == 422 and "ceph" in res.json()["error"]["message"], res.text
    assert api.get("/api/v1/providers/dst-rhoso").json()["ceph"] is None


def test_the_ceph_key_is_write_only(api):
    """SDD §12, §13.3: PUT …/ceph-key takes the key `ceph auth get-key` prints, stores it in the
    secret store and records the time alone; no response, listing or event carries it."""
    before = put(api, "/api/v1/providers/src-osp/ceph-key", {"key": CEPH_KEY})
    assert before.status_code == 422 and "ceph" in before.json()["error"]["message"], before.text
    assert patch(api, "/api/v1/providers/src-osp", {"ceph": CEPH}).status_code == 200
    bad = put(api, "/api/v1/providers/src-osp/ceph-key", {"key": "not base64 at all!!"})
    assert bad.status_code == 422 and "base64" in bad.json()["error"]["message"], bad.text
    res = put(api, "/api/v1/providers/src-osp/ceph-key", {"key": CEPH_KEY})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ceph"]["key_updated_at"] is not None and CEPH_KEY not in res.text
    assert body["ceph"]["fsid"] == CEPH["fsid"]  # the rest of the access is kept
    assert body["status"] == "unknown" and body["capabilities"] == {}  # reset as by PATCH
    assert CEPH_KEY not in api.get("/api/v1/providers").text
    assert CEPH_KEY not in api.get("/api/v1/providers/src-osp").text
    assert read_secret("provider-src-osp.ceph", api.settings) == {"key": CEPH_KEY}
    missing = put(api, "/api/v1/providers/nope/ceph-key", {"key": CEPH_KEY})
    assert missing.status_code == 404


def test_the_ceph_key_is_stored_under_the_providers_ceph_secret_name(api):
    """SDD §13.3: the secret is provider-{id}.ceph, key `key`, a 0600 file in the file store; a
    second PUT replaces it, so one secret holds the key."""
    from seamless_migrate.security.secret_store import ceph_key_secret_name

    assert ceph_key_secret_name("src-osp") == "provider-src-osp.ceph"
    assert patch(api, "/api/v1/providers/src-osp", {"ceph": CEPH}).status_code == 200
    assert put(api, "/api/v1/providers/src-osp/ceph-key", {"key": CEPH_KEY}).status_code == 200
    path = api.settings.secrets_dir / "provider-src-osp.ceph" / "key"
    assert path.read_text(encoding="utf-8").strip() == CEPH_KEY
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    other = "AQFakeCephKeyForTestsOnlyNotRealAtAll1=="  # gitleaks:allow
    assert put(api, "/api/v1/providers/src-osp/ceph-key", {"key": other}).status_code == 200
    assert read_secret("provider-src-osp.ceph", api.settings) == {"key": other}
    # one secret (the store's own .lock file beside it is not one)
    names = (p.name for p in api.settings.secrets_dir.iterdir() if not p.name.startswith("."))
    assert sorted(name for name in names if "ceph" in name) == ["provider-src-osp.ceph"]


def test_an_id_that_would_read_another_providers_ceph_secret_is_refused(api):
    """SDD §13.3 (audit AP-04): the SEAMLESS_SECRET_ variables read `.` and `-` alike, so
    'src-osp-ceph' would read src-osp's Ceph key as its credentials: refused both ways, as -ssh."""
    clash = api.post("/api/v1/providers", json={**VCENTER, "id": "src-osp-ceph"})
    assert clash.status_code == 409, clash.text
    assert "src-osp" in clash.json()["error"]["message"]
    assert api.post("/api/v1/providers", json={**VCENTER, "id": "vc-b-ceph"}).status_code == 201
    assert api.post("/api/v1/providers", json={**VCENTER, "id": "vc-b"}).status_code == 409
    # ids that only look alike are fine
    assert api.post("/api/v1/providers", json={**VCENTER, "id": "src-osp-cephx"}).status_code == 201


def test_removing_ceph_access_deletes_its_key(api):
    """SDD §13.3: `ceph` set to null removes the secret with it - a key without an access to use
    it would stay in the store for good."""
    assert patch(api, "/api/v1/providers/src-osp", {"ceph": CEPH}).status_code == 200
    assert put(api, "/api/v1/providers/src-osp/ceph-key", {"key": CEPH_KEY}).status_code == 200
    assert read_secret("provider-src-osp.ceph", api.settings) == {"key": CEPH_KEY}
    res = patch(api, "/api/v1/providers/src-osp", {"ceph": None})
    assert res.status_code == 200 and res.json()["ceph"] is None, res.text
    assert read_secret("provider-src-osp.ceph", api.settings) == {}
    # setting the access again starts without a key
    res = patch(api, "/api/v1/providers/src-osp", {"ceph": CEPH})
    assert res.status_code == 200 and res.json()["ceph"]["key_updated_at"] is None


def test_deleting_a_provider_deletes_its_ceph_key(api):
    """SDD §13.3: the secrets the store manages for a provider go with it."""
    assert (
        api.post("/api/v1/providers", json={**SOURCE, "id": "src-two", "ceph": CEPH}).status_code
        == 201
    )
    assert put(api, "/api/v1/providers/src-two/ceph-key", {"key": CEPH_KEY}).status_code == 200
    assert read_secret("provider-src-two.ceph", api.settings) == {"key": CEPH_KEY}
    assert (
        api.client.delete("/api/v1/providers/src-two", headers=api.h(Role.admin)).status_code == 204
    )
    assert read_secret("provider-src-two.ceph", api.settings) == {}


def test_the_ceph_key_is_audited_by_name_never_by_value(api, caplog):
    """SDD §4.3, §13.3, §28.8: provider.credentials_updated names the secret and the key's name;
    no event and no log line carries the key."""
    assert patch(api, "/api/v1/providers/src-osp", {"ceph": CEPH}).status_code == 200
    with caplog.at_level(logging.DEBUG):
        assert put(api, "/api/v1/providers/src-osp/ceph-key", {"key": CEPH_KEY}).status_code == 200
    events = api.store.events(kinds=["provider.credentials_updated"])
    assert events[-1].data == {
        "provider_id": "src-osp",
        "secret": "provider-src-osp.ceph",
        "keys": ["key"],
    }
    assert events[-1].actor == "admin-user"
    everything = str([e.model_dump() for e in api.store.events(since_seq=0, limit=100000)])
    assert CEPH_KEY not in everything and CEPH_KEY not in caplog.text


def test_the_ceph_key_time_is_server_owned(api):
    """SDD §4.2: key_updated_at is set by PUT …/ceph-key alone; POST and PATCH ignore what a
    client sends there, and a PATCH of the access keeps the time the key was written."""
    stamped = {**CEPH, "key_updated_at": "2020-01-01T00:00:00Z"}
    created = api.post("/api/v1/providers", json={**SOURCE, "id": "src-two", "ceph": stamped})
    assert created.status_code == 201 and created.json()["ceph"]["key_updated_at"] is None
    assert (
        patch(api, "/api/v1/providers/src-osp", {"ceph": stamped}).json()["ceph"]["key_updated_at"]
        is None
    )
    written = put(api, "/api/v1/providers/src-osp/ceph-key", {"key": CEPH_KEY}).json()["ceph"][
        "key_updated_at"
    ]
    assert written is not None
    moved = patch(
        api,
        "/api/v1/providers/src-osp",
        {"ceph": {**CEPH, "monitors": ["mon3.example"], "key_updated_at": "2020-01-01T00:00:00Z"}},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["ceph"]["monitors"] == ["mon3.example"]
    assert moved.json()["ceph"]["key_updated_at"] == written


# --------------------------------------------------------------------------------------------
# a provider's credentials per Keystone project (SDD §4.2, §4.3, §12, §13.3, §27.4; plan task TC2).
# Synthetic: project ids of 32 hex digits and application credentials are generated here. No
# assertion holds a credential in its expression, and secret values are compared into a bool
# first, so a failing test's message never prints one (pytest explains a call by its arguments).

#: the keys a project credential's secret holds (SDD §13.3)
APP_CREDENTIAL_KEYS = ["application_credential_id", "application_credential_secret"]


def _project_id() -> str:
    """A Keystone project's id as Keystone writes one: 32 hex digits, made up for the test."""
    return secrets.token_hex(16)


def _app_credential() -> dict[str, str]:
    """An application credential's id and secret, generated for the test."""
    return {
        "application_credential_id": secrets.token_hex(16),
        "application_credential_secret": secrets.token_urlsafe(32),
    }


def _project_secret(provider_id: str, project_id: str) -> str:
    """The name SDD §13.3 gives a project credential's secret, computed here on its own."""
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
    return f"provider-{provider_id}.project-{digest}"


def _project_path(provider_id: str, project_id: str) -> str:
    return f"/api/v1/providers/{provider_id}/projects/{project_id}/credentials"


def _write(api, provider_id: str, project_id: str) -> int:
    """Writes a generated credential for the project; the answer's status alone."""
    return put(api, _project_path(provider_id, project_id), _app_credential()).status_code


def _delete(api, path: str):
    return api.client.delete(path, headers=api.h(Role.admin))


def _leaks(values, **texts) -> list[str]:
    """The names of the texts that hold any of ``values``: what a failing assertion may print."""
    return sorted(name for name, text in texts.items() if any(v in text for v in values))


def test_a_project_credential_is_write_only(api, caplog):
    """SDD §12, §13.3, §27.4: PUT …/projects/{project_id}/credentials stores an application
    credential in the secret store, 0600 in a 0700 directory, and the provider records its secret's
    name and the time alone; no answer, listing, event or log line carries its id or its secret."""
    tenant_a, credential = _project_id(), _app_credential()
    with caplog.at_level(logging.DEBUG):
        res = put(api, _project_path("src-osp", tenant_a), credential)
    assert res.status_code == 200, res.text
    entry = res.json()["project_credentials"][tenant_a]
    assert entry["secret"] == _project_secret("src-osp", tenant_a)
    assert entry["updated_at"] is not None
    directory = api.settings.secrets_dir / entry["secret"]
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert sorted(p.name for p in directory.iterdir()) == APP_CREDENTIAL_KEYS
    assert {stat.S_IMODE(p.stat().st_mode) for p in directory.iterdir()} == {0o600}
    stored = read_secret(entry["secret"], api.settings) == credential
    assert stored
    assert api.get("/api/v1/providers/src-osp").json()["project_credentials"] == {tenant_a: entry}
    events = str([e.model_dump() for e in api.store.events(since_seq=0, limit=100000)])
    leaks = _leaks(
        credential.values(),
        answer=res.text,
        provider=api.get("/api/v1/providers/src-osp").text,
        listing=api.get("/api/v1/providers").text,
        events=events,
        log=caplog.text,
    )
    assert leaks == []
    # a viewer sees the record, never a value; an operator may not write one
    viewed = api.get("/api/v1/providers/src-osp", Role.viewer)
    assert viewed.json()["project_credentials"] == {tenant_a: entry}
    res = put(api, _project_path("src-osp", tenant_a), credential, Role.operator)
    assert res.status_code == 403


def test_a_project_credential_is_an_application_credential(api):
    """SDD §12, §27.4, §27.8 (owner decision 3): the body is an application credential's id and
    secret alone - a password signs in to every project its user has a role in, and an auth_url
    would send the secret elsewhere - neither blank, for a project id of 1-64 letters, digits, - or
    _ (422 otherwise), judged before the provider is looked up; nothing is stored or recorded."""
    tenant_a, credential = _project_id(), _app_credential()
    path = _project_path("src-osp", tenant_a)
    password = {"username": "svc-tenant-a", "password": secrets.token_urlsafe(16)}
    for body, needle in (
        ({**password, "project_name": "tenant-a"}, "password"),
        ({**credential, **password}, "username"),
        ({**credential, "auth_url": "https://keystone.elsewhere.example/v3"}, "auth_url"),
        ({**credential, "interface": "internal"}, "interface"),
        ({"application_credential_id": "ac-only"}, "application_credential_secret"),
        ({"application_credential_secret": "secret-only"}, "application_credential_id"),
        ({**credential, "application_credential_secret": "   "}, "application_credential_secret"),
        ({**credential, "application_credential_id": " "}, "application_credential_id"),
    ):
        res = put(api, path, body)
        refused = res.status_code == 422 and needle in res.json()["error"]["message"]
        assert refused, (needle, res.status_code)
        # the body alone is judged first: a provider it does not know answers the same
        res = put(api, _project_path("nope-1", tenant_a), body)
        assert res.status_code == 422, needle
    for bad in ("a" * 65, "tenant.a", "tenant%20a", "tenant%0A"):
        res = put(api, _project_path("src-osp", bad), credential)
        refused = res.status_code == 422 and "project_id" in res.json()["error"]["message"]
        assert refused, (bad, res.status_code)
        res = put(api, _project_path("nope-1", bad), credential)
        assert res.status_code == 422, bad
        assert _delete(api, _project_path("src-osp", bad)).status_code == 422, bad
    res = put(api, _project_path("nope-1", tenant_a), credential)
    assert res.status_code == 404
    assert api.get("/api/v1/providers/src-osp").json()["project_credentials"] == {}
    assert read_secret(_project_secret("src-osp", tenant_a), api.settings) == {}
    assert "provider.credentials_updated" not in api.kinds()
    # the model keys its entries by the same rule (SDD §4.2)
    entry = {"secret": _project_secret("src-osp", "x" * 65), "updated_at": "2026-10-29T08:00:00Z"}
    with pytest.raises(ValidationError, match="project_credentials"):
        Provider.model_validate({**SOURCE, "project_credentials": {"x" * 65: entry}})
    longest = {"x" * 64: {**entry, "secret": _project_secret("src-osp", "x" * 64)}}
    held = Provider.model_validate({**SOURCE, "project_credentials": longest}).project_credentials
    assert set(held) == set(longest)
    # its two keys alone are stored, the id stripped as the provider's own credential's is - the
    # file itself: the files store strips what it reads, a Kubernetes Secret is read as written
    padded = {
        **credential,
        "application_credential_id": f"  {credential['application_credential_id']}\n",
    }
    res = put(api, path, padded)
    assert res.status_code == 200, res.text
    stored = read_secret(_project_secret("src-osp", tenant_a), api.settings) == credential
    assert stored
    written = api.settings.secrets_dir / _project_secret("src-osp", tenant_a)
    stripped = (written / "application_credential_id").read_text(encoding="utf-8") == (
        credential["application_credential_id"]
    )
    assert stripped
    # the widest id: 64 letters of either case, digits, - and _
    widest = "Tenant_A-" + "0" * 55
    res = put(api, _project_path("src-osp", widest), _app_credential())
    assert res.status_code == 200, res.text
    assert widest in res.json()["project_credentials"]


def test_a_project_credential_is_stored_under_its_providers_project_secret_name(api):
    """SDD §13.3, §27.4: the secret is provider-{id}.project-<h>, <h> the first 16 hex digits of the
    SHA-256 of the project id - lower case and at most 97 characters, a name both stores hold - one
    per project, which the project's next write replaces whole."""
    from seamless_migrate.security.secret_store import project_secret_name

    tenant_a, tenant_b = _project_id(), _project_id()
    name_a, name_b = _project_secret("src-osp", tenant_a), _project_secret("src-osp", tenant_b)
    assert project_secret_name("src-osp", tenant_a) == name_a
    longest = project_secret_name("p" * 63, "X" * 64)
    assert len(longest) == 97 and kubernetes_name(longest) and valid_secret_name(longest)
    first, second, other = _app_credential(), _app_credential(), _app_credential()
    for project, credential in ((tenant_a, first), (tenant_b, other)):
        res = put(api, _project_path("src-osp", project), credential)
        assert res.status_code == 200, res.text
    names = sorted(
        p.name for p in api.settings.secrets_dir.iterdir() if ".project-" in p.name and p.is_dir()
    )
    assert names == sorted([name_a, name_b])
    res = put(api, _project_path("src-osp", tenant_a), second)
    assert res.status_code == 200, res.text
    replaced = read_secret(name_a, api.settings) == second
    kept = read_secret(name_b, api.settings) == other
    assert replaced and kept
    recorded = api.get("/api/v1/providers/src-osp").json()["project_credentials"]
    assert {project: entry["secret"] for project, entry in recorded.items()} == {
        tenant_a: name_a,
        tenant_b: name_b,
    }


def test_a_project_credential_whose_secret_name_another_project_holds_is_refused(api):
    """SDD §13.3, §27.4: an entry records its project id, so a write whose secret's name another
    project's entry holds - two project ids whose SHA-256 begin with the same 64 bits - is refused
    (409) and never written over. No such pair can be found here: the other project's entry is
    stored holding this project's name, as a collision would leave it."""
    from seamless_migrate.domain.models import ProjectCredential, utcnow

    tenant_a, tenant_b = _project_id(), _project_id()
    name = _project_secret("src-osp", tenant_a)
    held = _app_credential()
    FileSecretStore(api.settings.secrets_dir).write(name, held, {})
    stored = api.store.get("provider", "src-osp", Provider)
    holder = {tenant_b: ProjectCredential(secret=name, updated_at=utcnow())}
    api.store.put("provider", stored.model_copy(update={"project_credentials": holder}))
    res = put(api, _project_path("src-osp", tenant_a), _app_credential())
    assert res.status_code == 409, res.text
    message = res.json()["error"]["message"]
    assert tenant_b in message and name in message
    untouched = read_secret(name, api.settings) == held
    assert untouched
    assert set(api.get("/api/v1/providers/src-osp").json()["project_credentials"]) == {tenant_b}
    assert "provider.credentials_updated" not in api.kinds()


def test_a_vmware_provider_takes_no_project_credential(api):
    """SDD §4.2, §27.9: vCenter has no Keystone projects, so a vmware provider holds no project
    credential: a write is refused (422) and nothing is stored or recorded."""
    assert api.post("/api/v1/providers", json=VCENTER).status_code == 201
    tenant_a = _project_id()
    res = put(api, _project_path("vc-dc2", tenant_a), _app_credential())
    assert res.status_code == 422 and "vmware" in res.json()["error"]["message"], res.text
    assert read_secret(_project_secret("vc-dc2", tenant_a), api.settings) == {}
    assert api.get("/api/v1/providers/vc-dc2").json()["project_credentials"] == {}
    assert _delete(api, _project_path("vc-dc2", tenant_a)).status_code == 404
    assert "provider.credentials_updated" not in api.kinds()


def test_an_id_that_would_read_another_providers_project_secret_is_refused(api):
    """SDD §13.3 (audit AP-04): the SEAMLESS_SECRET_ variables read `.` and `-` alike, so a provider
    'src-osp-project-<16 hex>' would read a project secret of src-osp as its credentials: refused
    both ways, as -ssh and -ceph are."""
    suffix = "-project-" + secrets.token_hex(8)
    clash = api.post("/api/v1/providers", json={**VCENTER, "id": "src-osp" + suffix})
    assert clash.status_code == 409, clash.text
    assert "src-osp" in clash.json()["error"]["message"]
    assert api.post("/api/v1/providers", json={**VCENTER, "id": "vc-c" + suffix}).status_code == 201
    refused = api.post("/api/v1/providers", json={**VCENTER, "id": "vc-c"})
    assert refused.status_code == 409 and "vc-c" + suffix in refused.json()["error"]["message"]
    # ids that only look alike are free: 15 or 17 hex digits, a letter no hex digit, no such prefix
    for free in (
        "src-osp-project-" + "a" * 15,
        "src-osp-project-" + "a" * 17,
        "src-osp-project-" + "a" * 15 + "g",
        "src-osp-projects-" + "a" * 16,
        "vc-nope" + suffix,
    ):
        made = api.post("/api/v1/providers", json={**VCENTER, "id": free})
        assert made.status_code == 201, (free, made.text)


def _project_check(name: str, checked_at: str) -> dict:
    """A project's entry of the check report as plan task TC3 writes it (SDD §27.7)."""
    return {"status": "ok", "message": None, "project_name": name, "checked_at": checked_at}


def test_a_project_credential_write_drops_only_its_projects_check(api, monkeypatch):
    """SDD §12, §27.4, §27.7: a project credential's write drops that project's entry of the check
    report, which tested the credential it replaces; the provider's status, the rest of its report
    and the other projects' entries stay. A check that lands while the write is recorded keeps its
    report, less that project's entry."""
    tenant_a, tenant_b = _project_id(), _project_id()
    report = {
        "release_range": ["2023.1", "2026.2"],
        "projects": {
            tenant_a: _project_check("tenant-a", "2026-10-29T08:00:00Z"),
            tenant_b: _project_check("tenant-b", "2026-10-29T08:00:00Z"),
        },
    }
    stored = api.store.get("provider", "src-osp", Provider)
    api.store.put(
        "provider",
        stored.model_copy(
            update={
                "status": "degraded",
                "status_message": "one pool unreadable",
                "capabilities": report,
                "last_checked_at": datetime(2026, 10, 29, 8, 0, tzinfo=UTC),
            }
        ),
    )
    res = put(api, _project_path("src-osp", tenant_a), _app_credential())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["capabilities"] == {
        "release_range": ["2023.1", "2026.2"],
        "projects": {tenant_b: _project_check("tenant-b", "2026-10-29T08:00:00Z")},
    }
    assert (body["status"], body["status_message"], body["last_checked_at"]) == (
        "degraded",
        "one pool unreadable",
        "2026-10-29T08:00:00Z",
    )
    # a check that lands between the record's read and its write: its report stays, less the
    # entry of the project whose credential the write replaced
    from seamless_migrate.store import AsyncStore

    db_put = AsyncStore.put
    raced: list[int] = []
    fresh_report = {
        "projects": {
            tenant_a: _project_check("tenant-a", "2026-10-29T09:00:00Z"),
            tenant_b: _project_check("tenant-b", "2026-10-29T09:00:00Z"),
        }
    }

    async def put_after_a_check(self, kind, model, expected_version=None):
        if kind == "provider" and expected_version is not None and not raced:
            raced.append(expected_version)
            fresh = api.store.get("provider", model.id, Provider)
            api.store.put(
                "provider", fresh.model_copy(update={"status": "ok", "capabilities": fresh_report})
            )
        return await db_put(self, kind, model, expected_version=expected_version)

    monkeypatch.setattr(AsyncStore, "put", put_after_a_check)
    res = put(api, _project_path("src-osp", tenant_a), _app_credential())
    assert res.status_code == 200 and raced, res.text
    assert res.json()["status"] == "ok"
    assert res.json()["capabilities"] == {
        "projects": {tenant_b: _project_check("tenant-b", "2026-10-29T09:00:00Z")}
    }
    assert set(res.json()["project_credentials"]) == {tenant_a}


def test_a_project_credential_a_plan_needs_cannot_be_deleted(api):
    """SDD §12, §27.4: DELETE …/projects/{project_id}/credentials answers 409 while a plan needs the
    project, judged as DELETE /providers/{id} judges a provider - a plan that names the project on
    the provider's side (its source_project or destination_project, §4.2, plan task TC4) and has
    not completed, or completed with a VM not finalized - each named; a plan that names the same id
    on its other provider, or no project, does not. Then the secret, the entry and the project's
    entry of the check report go, and an unknown project is a 404."""
    tenant_a, tenant_b, tenant_c = _project_id(), _project_id(), _project_id()
    for provider_id, project in (
        ("src-osp", tenant_a),
        ("dst-rhoso", tenant_b),
        ("src-osp", tenant_c),
    ):
        assert _write(api, provider_id, project) == 200

    def store_plan(status, **projects):
        plan = make_plan(status=status, **projects)
        api.store.put("plan", plan)
        return plan

    moving_a = store_plan(PlanStatus.draft, source_project=tenant_a)
    elsewhere_a = store_plan(PlanStatus.draft, destination_project=tenant_a)
    no_project = store_plan(PlanStatus.validated)
    into_b = store_plan(PlanStatus.failed, destination_project=tenant_b)
    elsewhere_b = store_plan(PlanStatus.draft, source_project=tenant_b)
    refused = _delete(api, _project_path("src-osp", tenant_a))
    assert refused.status_code == 409, refused.text
    message = refused.json()["error"]["message"]
    assert moving_a.id in message and "draft" in message
    assert elsewhere_a.id not in message and no_project.id not in message
    assert into_b.id not in message
    # a failed plan can start again: it still needs its destination project
    refused = _delete(api, _project_path("dst-rhoso", tenant_b))
    assert refused.status_code == 409 and into_b.id in refused.json()["error"]["message"]
    assert elsewhere_b.id not in refused.json()["error"]["message"]
    # a cancelled plan needs it no more; the plans that name it elsewhere, or none, never did
    api.store.put("plan", moving_a.model_copy(update={"status": PlanStatus.cancelled}))
    stored = api.store.get("provider", "src-osp", Provider)
    report = {"projects": {tenant_a: _project_check("tenant-a", "2026-10-29T08:00:00Z")}}
    api.store.put("provider", stored.model_copy(update={"capabilities": report}))
    assert _delete(api, _project_path("src-osp", tenant_a)).status_code == 204
    after = api.get("/api/v1/providers/src-osp").json()
    assert set(after["project_credentials"]) == {tenant_c}
    assert after["capabilities"] == {"projects": {}}
    assert read_secret(_project_secret("src-osp", tenant_a), api.settings) == {}
    assert _delete(api, _project_path("src-osp", tenant_a)).status_code == 404
    # a completed plan needs it while a VM is not finalized
    done_c = store_plan(PlanStatus.completed, source_project=tenant_c)
    migration = make_migration(plan_id=done_c.id, phase=Phase.completed)
    api.store.put("migration", migration)
    refused = _delete(api, _project_path("src-osp", tenant_c))
    assert refused.status_code == 409, refused.text
    assert f"{done_c.id} (" in refused.json()["error"]["message"]
    assert "1 VM not finalized" in refused.json()["error"]["message"]
    api.store.put("migration", migration.model_copy(update={"phase": Phase.finalized}))
    assert _delete(api, _project_path("src-osp", tenant_c)).status_code == 204
    assert read_secret(_project_secret("src-osp", tenant_c), api.settings) == {}


def test_a_project_credential_the_store_cannot_delete_answers_503_and_stays(api, monkeypatch):
    """SDD §12, §13.3: a deletion deletes the secret first, so a store that cannot answers 503 and
    the entry stays - nothing the store may still hold goes unrecorded - and no deletion is
    audited; sent again once the store can, it goes through."""
    tenant_a = _project_id()
    assert _write(api, "src-osp", tenant_a) == 200
    monkeypatch.setattr(routes_providers, "secret_store", _unbuildable_store)
    res = _delete(api, _project_path("src-osp", tenant_a))
    assert res.status_code == 503 and res.json()["error"]["code"] == "unavailable", res.text
    assert "ServiceAccount" in res.json()["error"]["message"]
    assert set(api.get("/api/v1/providers/src-osp").json()["project_credentials"]) == {tenant_a}
    kept = read_secret(_project_secret("src-osp", tenant_a), api.settings) != {}
    assert kept
    deletions = api.store.events(kinds=["provider.credentials_updated"])
    assert [e.data for e in deletions if e.data.get("deleted")] == []
    monkeypatch.undo()
    assert _delete(api, _project_path("src-osp", tenant_a)).status_code == 204
    assert read_secret(_project_secret("src-osp", tenant_a), api.settings) == {}


def test_deleting_a_provider_deletes_its_project_credentials(api):
    """SDD §12, §13.3: DELETE /providers/{id} deletes its project credentials' secrets with its
    other managed secrets - each one tried, one the store cannot remove named in provider.deleted
    (audit AA-03)."""
    assert api.post("/api/v1/providers", json={**SOURCE, "id": "src-two"}).status_code == 201
    tenants = [_project_id(), _project_id()]
    for tenant in tenants:
        assert _write(api, "src-two", tenant) == 200
    names = [_project_secret("src-two", tenant) for tenant in tenants]
    assert [name for name in names if not read_secret(name, api.settings)] == []
    assert _delete(api, "/api/v1/providers/src-two").status_code == 204
    assert [name for name in names if read_secret(name, api.settings)] == []
    [deleted] = [e for e in api.store.events() if e.kind == "provider.deleted"]
    assert deleted.data == {"provider_id": "src-two"}
    if os.geteuid() == 0:
        return  # root writes a directory of mode 0500
    assert api.post("/api/v1/providers", json={**SOURCE, "id": "src-three"}).status_code == 201
    tenant = _project_id()
    assert _write(api, "src-three", tenant) == 200
    root = api.settings.secrets_dir
    os.chmod(root, 0o500)  # the store's root turned read-only
    try:
        res = _delete(api, "/api/v1/providers/src-three")
    finally:
        os.chmod(root, 0o700)
    assert res.status_code == 204, res.text
    last = [e for e in api.store.events() if e.kind == "provider.deleted"][-1]
    assert last.data == {
        "provider_id": "src-three",
        "secrets_left": [_project_secret("src-three", tenant)],
    }


def test_a_project_credential_is_audited_by_project_never_by_value(api, caplog):
    """SDD §4.3, §27.8: provider.credentials_updated names the provider, the project, the secret and
    its keys' names - a deletion `deleted` true instead of key names -, its actor the Seamless
    principal; no event and no log line carries the credential's id or its secret."""
    tenant_a, credential = _project_id(), _app_credential()
    name = _project_secret("src-osp", tenant_a)
    with caplog.at_level(logging.DEBUG):
        res = put(api, _project_path("src-osp", tenant_a), credential)
        assert res.status_code == 200, res.text
        assert _delete(api, _project_path("src-osp", tenant_a)).status_code == 204
    written, deleted = api.store.events(kinds=["provider.credentials_updated"])
    assert written.data == {
        "provider_id": "src-osp",
        "project_id": tenant_a,
        "secret": name,
        "keys": APP_CREDENTIAL_KEYS,
    }
    assert deleted.data == {
        "provider_id": "src-osp",
        "project_id": tenant_a,
        "secret": name,
        "deleted": True,
    }
    assert written.actor == deleted.actor == "admin-user"
    assert tenant_a in written.message and tenant_a in deleted.message
    events = str([e.model_dump() for e in api.store.events(since_seq=0, limit=100000)])
    leaks = _leaks(credential.values(), events=events, log=caplog.text)
    assert leaks == []


def test_project_credentials_are_server_owned(api):
    """SDD §4.2, §12, §27.4: project_credentials is the server's - POST ignores it, PATCH refuses
    it (422) - and what changes the rest of the provider keeps it: an edit, the provider's own
    credentials and a check."""
    tenant_a = _project_id()
    forged = {
        tenant_a: {
            "secret": _project_secret("src-two", tenant_a),
            "updated_at": "2026-10-23T08:00:00Z",
        }
    }
    created = api.post(
        "/api/v1/providers", json={**SOURCE, "id": "src-two", "project_credentials": forged}
    )
    assert created.status_code == 201, created.text
    assert created.json()["project_credentials"] == {}
    assert api.get("/api/v1/providers/src-two").json()["project_credentials"] == {}
    for value in (forged, {}):
        refused = patch(api, "/api/v1/providers/src-osp", {"project_credentials": value})
        assert refused.status_code == 422, refused.text
        assert "fields cannot be changed: project_credentials" in refused.text
    assert _write(api, "src-osp", tenant_a) == 200
    recorded = api.get("/api/v1/providers/src-osp").json()["project_credentials"]
    assert set(recorded) == {tenant_a}
    renamed = patch(api, "/api/v1/providers/src-osp", {"name": "RHOSP 17.1 (renamed)"})
    assert renamed.status_code == 200 and renamed.json()["project_credentials"] == recorded
    own = {
        "username": "svc-migrate",
        "password": secrets.token_urlsafe(16),
        "project_name": "admin",
    }
    rewritten = put(api, "/api/v1/providers/src-osp/credentials", own)
    assert rewritten.status_code == 200 and rewritten.json()["project_credentials"] == recorded
    checked = api.post("/api/v1/providers/src-osp/check", Role.operator)
    assert checked.status_code == 200 and checked.json()["project_credentials"] == recorded
