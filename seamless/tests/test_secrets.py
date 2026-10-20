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
