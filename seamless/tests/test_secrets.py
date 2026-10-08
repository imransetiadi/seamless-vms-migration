import os
import stat

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.security.secrets import (
    SecretNotFound,
    load_cloud_auth,
    resolve,
    secret_files,
    write_secret_file,
)


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
