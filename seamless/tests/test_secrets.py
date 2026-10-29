import os
import stat

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.security.secret_store import FileSecretStore, SecretStoreError
from seamless_migrate.security.secrets import (
    SecretNotFound,
    load_cloud_auth,
    resolve,
    resolve_private_key,
    secret_files,
    write_secret_file,
)

#: a directory the process may not search: root searches every one
_UNREADABLE = pytest.mark.skipif(os.geteuid() == 0, reason="root reads a directory of mode 000")


def test_secret_resolution_file_then_env(tmp_path):
    settings = Settings(secrets_dir=tmp_path)
    env = {
        "SEAMLESS_SECRET_VCENTER_DC2_USERNAME": "env-user",
        "SEAMLESS_SECRET_VCENTER_DC2_PASSWORD": "env-pass",
        "SEAMLESS_SECRET_VCENTER_DC2_DATACENTER": "DC2",
    }
    # no files: environment variables (name upper-cased, '-' -> '_')
    assert resolve("vcenter-dc2", settings, env=env) == {
        "username": "env-user",
        "password": "env-pass",
        "datacenter": "DC2",
    }
    # files win over the environment
    secret = tmp_path / "vcenter-dc2"
    secret.mkdir()
    (secret / "username").write_text("file-user\n")
    (secret / "password").write_text("file-pass")
    (secret / "..data").mkdir()  # Kubernetes secret-volume internals are ignored
    assert resolve("vcenter-dc2", settings, env=env) == {
        "username": "file-user",
        "password": "file-pass",
    }
    # incomplete files fall back to a complete environment entry
    (secret / "password").unlink()
    assert resolve("vcenter-dc2", settings, env=env)["username"] == "env-user"
    with pytest.raises(SecretNotFound):
        resolve("vcenter-dc2", settings, env={})
    for bad in ("../etc", "", "a/b", ".."):
        with pytest.raises(SecretNotFound):
            resolve(bad, settings, env=env)


def test_load_cloud_auth(tmp_path):
    clouds = tmp_path / "clouds.yaml"
    clouds.write_text(
        "clouds:\n"
        "  src:\n    auth: {auth_url: 'https://k/v3', username: u, password: p}\n"
        "    region_name: regionOne\n"
        "  broken:\n    region_name: x\n"
    )
    settings = Settings(clouds_yaml=clouds)
    entry = load_cloud_auth("src", settings)
    assert entry["auth"]["username"] == "u" and entry["region_name"] == "regionOne"
    with pytest.raises(SecretNotFound):
        load_cloud_auth("missing", settings)
    with pytest.raises(SecretNotFound):
        load_cloud_auth("broken", settings)
    with pytest.raises(SecretNotFound):
        load_cloud_auth("src", Settings(clouds_yaml=None))


def test_write_secret_file_is_0600_and_removed(tmp_path):
    old = os.umask(0)
    try:
        path = write_secret_file(tmp_path / "run" / "secrets.yml", "k: v\n")
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    other = tmp_path / "clouds.yaml"
    other.write_text("x")
    with pytest.raises(RuntimeError), secret_files(path, other):
        raise RuntimeError("playbook crashed")
    assert not path.exists() and not other.exists()


def test_secret_name_rejects_path_traversal(tmp_path):
    """Security.md R-03 / QASuite S-08: a credentials_secret is a name, never a path."""
    settings = Settings(secrets_dir=tmp_path)
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "username").write_text("u")
    (tmp_path / "ok" / "password").write_text("p")
    assert resolve("ok", settings, env={})["username"] == "u"
    for bad in ("../../etc/passwd", "..", ".", "a/b", "/etc/shadow", "", "x" * 129, "-leading"):
        with pytest.raises(SecretNotFound):
            resolve(bad, settings, env={})


@_UNREADABLE
@pytest.mark.parametrize("unreadable", ["secret", "root"])
def test_a_secrets_directory_this_process_may_not_read_falls_back_to_the_variables(
    tmp_path, unreadable
):
    """SDD §13.3 (PR #58 review): the readers raised a bare PermissionError at a secrets
    directory this process may not search - mode 000, another UID's 0700 directory - so the
    SEAMLESS_SECRET_* variables were never read, and the providers' and steps' SecretNotFound
    handling never saw the error. Such a directory holds nothing usable: the variables come next,
    and a secret found nowhere is SecretNotFound."""
    settings = Settings(secrets_dir=tmp_path / "secrets")
    secret = tmp_path / "secrets" / "provider-x"
    secret.mkdir(parents=True)
    (secret / "username").write_text("file-user")
    (secret / "password").write_text("file-pass")
    blocked = secret if unreadable == "secret" else tmp_path / "secrets"
    env = {
        "SEAMLESS_SECRET_PROVIDER_X_USERNAME": "env-user",
        "SEAMLESS_SECRET_PROVIDER_X_PASSWORD": "env-pass",
        "SEAMLESS_SECRET_PROVIDER_X_PRIVATE_KEY": "env-key",
    }
    os.chmod(blocked, 0)
    try:
        assert resolve("provider-x", settings, env=env) == {
            "username": "env-user",
            "password": "env-pass",
            "private_key": "env-key",
        }
        assert resolve_private_key("provider-x", settings, env=env) == "env-key\n"
        with pytest.raises(SecretNotFound):
            resolve("provider-x", settings, env={})
    finally:
        os.chmod(blocked, 0o700)


@_UNREADABLE
def test_a_file_store_this_process_may_not_read_or_search_answers_as_unavailable(tmp_path):
    """SDD §13.3 (PR #58 review): FileSecretStore.read raised a bare PermissionError, and on
    Python 3.14 delete read an unsearchable root as "already gone" - Path.exists() no longer
    raises there - and left the secret on disk unreported. Both are a store it cannot reach."""
    store = FileSecretStore(tmp_path / "secrets")
    store.write("provider-x", {"password": "p"}, {})
    os.chmod(tmp_path / "secrets", 0)
    try:
        with pytest.raises(SecretStoreError):
            store.read("provider-x")
        with pytest.raises(SecretStoreError):
            store.delete("provider-x")
    finally:
        os.chmod(tmp_path / "secrets", 0o700)
    assert store.read("provider-x") == {"password": "p"}


def test_a_project_credential_signs_in_at_its_providers_auth_url(tmp_path):
    """SDD §13.3, §27.4: a project credential's connection entry is an application credential's
    that signs in at the auth URL of the provider's own credential - its clouds.yaml entry's, or
    its secret's auth_url - else at the provider's endpoint. It takes no auth_url of its own: a key
    its secret holds beside the two is never read, so the secret is never sent to another endpoint.
    The entry's other keys are the provider credential's: the same cloud, reached the same way."""
    import secrets as tokens

    from seamless_migrate.domain.models import ProjectCredential, utcnow
    from seamless_migrate.security.secret_store import project_secret_name
    from seamless_migrate.security.secrets import openstack_project_cloud_entry
    from tests.factories import make_provider

    clouds = tmp_path / "clouds.yaml"
    clouds.write_text(
        "clouds:\n  src:\n"
        "    auth: {auth_url: 'https://keystone.internal.example:5000/v3',\n"
        "           username: u, password: p}\n"
        "    region_name: regionTwo\n    interface: internal\n"
    )
    settings = Settings(secrets_dir=tmp_path / "secrets", clouds_yaml=clouds)
    store = FileSecretStore(tmp_path / "secrets")
    tenant_a = tokens.token_hex(16)
    credential = {
        "application_credential_id": tokens.token_hex(16),
        "application_credential_secret": tokens.token_urlsafe(32),
    }
    name = project_secret_name("src-osp", tenant_a)
    # a key the route never writes, as an operator could leave one: never read
    store.write(name, {**credential, "auth_url": "https://keystone.elsewhere.example/v3"}, {})
    holding = {tenant_a: ProjectCredential(secret=name, updated_at=utcnow())}

    by_cloud = make_provider(cloud="src", project_credentials=holding)
    entry = openstack_project_cloud_entry(by_cloud, tenant_a, settings)
    assert entry["auth_type"] == "v3applicationcredential"
    keys = sorted(entry["auth"])  # never in an assertion's expression: it would print the values
    assert keys == ["application_credential_id", "application_credential_secret", "auth_url"]
    assert entry["auth"]["auth_url"] == "https://keystone.internal.example:5000/v3"
    signs_in_as_the_project = all(entry["auth"][key] == value for key, value in credential.items())
    assert signs_in_as_the_project
    assert (entry["region_name"], entry["interface"]) == ("regionTwo", "internal")

    # the provider's own secret: its auth_url and interface
    store.write(
        "provider-src-osp",
        {
            "application_credential_id": tokens.token_hex(16),
            "application_credential_secret": tokens.token_urlsafe(32),
            "auth_url": "https://keystone.dc1.example:13000/v3",
            "interface": "admin",
            "region_name": "regionThree",
        },
        {},
    )
    by_secret = make_provider(
        cloud=None, credentials_secret="provider-src-osp", project_credentials=holding
    )
    entry = openstack_project_cloud_entry(by_secret, tenant_a, settings)
    assert entry["auth"]["auth_url"] == "https://keystone.dc1.example:13000/v3"
    assert entry["interface"] == "admin" and entry["auth_type"] == "v3applicationcredential"
    assert entry["region_name"] == "regionThree"
    signs_in_as_the_project = all(entry["auth"][key] == value for key, value in credential.items())
    assert signs_in_as_the_project
    # its own secret without an auth_url, or no credential of its own: the provider's endpoint
    store.write(
        "provider-src-osp",
        {"username": "svc-migrate", "password": tokens.token_urlsafe(16), "project_name": "admin"},
        {},
    )
    entry = openstack_project_cloud_entry(by_secret, tenant_a, settings)
    assert entry["auth"]["auth_url"] == by_secret.endpoint
    keys = sorted(entry["auth"])
    assert keys == ["application_credential_id", "application_credential_secret", "auth_url"]
    bare = make_provider(cloud=None, credentials_secret=None, project_credentials=holding)
    entry = openstack_project_cloud_entry(bare, tenant_a, settings)
    assert entry["auth"]["auth_url"] == bare.endpoint
    # a cloud named while no clouds.yaml is mounted: none read, the endpoint
    unmounted = Settings(secrets_dir=tmp_path / "secrets", clouds_yaml=None)
    entry = openstack_project_cloud_entry(by_cloud, tenant_a, unmounted)
    assert entry["auth"]["auth_url"] == by_cloud.endpoint and "interface" not in entry

    # a project it holds no credential for, or a secret without its two keys, is SecretNotFound
    with pytest.raises(SecretNotFound, match="no credential for project"):
        openstack_project_cloud_entry(by_cloud, tokens.token_hex(16), settings)
    store.write(name, {"application_credential_id": credential["application_credential_id"]}, {})
    with pytest.raises(SecretNotFound, match="application_credential_secret") as caught:
        openstack_project_cloud_entry(by_cloud, tenant_a, settings)
    named = any(value in str(caught.value) for value in credential.values())
    assert not named
