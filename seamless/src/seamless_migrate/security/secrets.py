"""Credential resolution and short-lived secret files (SDD §13.3).

Credentials never enter the database or the API. OpenStack credentials come from the mounted
``clouds.yaml`` (``SEAMLESS_CLOUDS_YAML``) by cloud name; other secrets (VMware) come from
``{SEAMLESS_SECRETS_DIR}/{name}/{key}`` files or ``SEAMLESS_SECRET_{NAME}_{KEY}`` variables.
Secret material handed to subprocesses is written to 0600 files that are removed after use.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

from ..config import Settings

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_KEY = re.compile(r"^[A-Za-z0-9_]+$")
REQUIRED_KEYS = ("username", "password")


class SecretNotFound(LookupError):
    """The named secret (or a required key of it) is not configured."""


def _env_prefix(name: str) -> str:
    return "SEAMLESS_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper() + "_"


def read_secret(
    name: str, settings: Settings, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """All keys of secret ``name`` (SDD §13.3 order): mounted files under ``secrets_dir``, then
    the Kubernetes store, then ``SEAMLESS_SECRET_{NAME}_{KEY}`` variables. ``{}`` when absent."""
    if not name or not _NAME.match(name) or name in (".", ".."):
        raise SecretNotFound(f"invalid secret name {name!r}")
    values: dict[str, str] = {}
    directory = Path(settings.secrets_dir) / name
    if directory.is_dir():
        for entry in sorted(directory.iterdir()):
            # Kubernetes secret volumes add hidden ..data symlinks; skip anything not key-like.
            if entry.name.startswith(".") or not _KEY.match(entry.name) or not entry.is_file():
                continue
            values[entry.name] = entry.read_text(encoding="utf-8").strip()
    if not values and settings.secret_store == "kubernetes":
        from .secret_store import KubernetesSecretStore, SecretStoreError

        try:
            values = KubernetesSecretStore(settings.k8s_namespace).read(name) or {}
        except SecretStoreError as exc:
            raise SecretNotFound(f"secret {name!r}: {exc}") from None
    if not values:
        source = os.environ if env is None else env
        prefix = _env_prefix(name)
        values = {
            key[len(prefix) :].lower(): value
            for key, value in source.items()
            if key.startswith(prefix) and value
        }
    return values


def resolve(name: str, settings: Settings, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the secret ``name`` as ``{key: value}`` (at least ``username`` and ``password``).

    Files under ``{secrets_dir}/{name}/`` win; the Kubernetes store and environment variables are
    the fallbacks (SDD §13.3).
    """
    values = read_secret(name, settings, env)
    if not all(values.get(k) for k in REQUIRED_KEYS):
        source = os.environ if env is None else env
        prefix = _env_prefix(name)
        from_env = {
            key[len(prefix) :].lower(): value
            for key, value in source.items()
            if key.startswith(prefix) and value
        }
        if all(from_env.get(k) for k in REQUIRED_KEYS):
            values = from_env
    missing = [k for k in REQUIRED_KEYS if not values.get(k)]
    if missing:
        raise SecretNotFound(f"secret {name!r} is missing {', '.join(missing)}")
    return values


def resolve_private_key(name: str, settings: Settings, env: Mapping[str, str] | None = None) -> str:
    """The SSH private key stored in secret ``name`` (``ConversionHostConfig.ssh_key_secret``).

    Read from ``{secrets_dir}/{name}/private_key`` or ``SEAMLESS_SECRET_{NAME}_PRIVATE_KEY``.
    """
    if not name or not _NAME.match(name) or name in (".", ".."):
        raise SecretNotFound(f"invalid secret name {name!r}")
    path = Path(settings.secrets_dir) / name / "private_key"
    if path.is_file():
        key = path.read_text(encoding="utf-8")
    else:
        key = read_secret(name, settings, env).get("private_key", "")
    if not key.strip():
        raise SecretNotFound(f"secret {name!r} has no private_key")
    return key.strip() + "\n"


def load_clouds(settings: Settings) -> dict[str, Any]:
    if settings.clouds_yaml is None:
        raise SecretNotFound("SEAMLESS_CLOUDS_YAML is not configured")
    path = Path(settings.clouds_yaml)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise SecretNotFound(f"cannot read clouds.yaml: {exc.strerror}") from None
    clouds = document.get("clouds") or {}
    if not isinstance(clouds, dict):
        raise SecretNotFound("clouds.yaml has no 'clouds' mapping")
    return clouds


def load_cloud_auth(cloud: str, settings: Settings) -> dict[str, Any]:
    """The ``clouds.yaml`` entry of ``cloud`` (``auth``, ``region_name``, ``auth_type``, …)."""
    clouds = load_clouds(settings)
    entry = clouds.get(cloud)
    if not isinstance(entry, dict) or not isinstance(entry.get("auth"), dict):
        raise SecretNotFound(f"cloud {cloud!r} with an 'auth' section is not in clouds.yaml")
    return dict(entry)


#: keys of an OpenStack secret written by PUT /providers/{id}/credentials (SDD §13.3)
_PASSWORD_AUTH = ("username", "password", "project_name", "user_domain_name", "project_domain_name")
_APP_CRED_AUTH = ("application_credential_id", "application_credential_secret")


def openstack_cloud_entry(provider: Any, settings: Settings) -> dict[str, Any]:
    """The connection entry of an OpenStack/RHOSO provider, in ``clouds.yaml`` shape.

    A provider with ``credentials_secret`` builds it from that secret (password or application
    credential auth; ``auth_url`` defaults to the provider endpoint); otherwise the ``clouds.yaml``
    entry named by ``cloud`` is used (SDD §13.3).
    """
    if provider.credentials_secret:
        values = read_secret(provider.credentials_secret, settings)
        if not values:
            raise SecretNotFound(f"secret {provider.credentials_secret!r} is not configured")
        auth: dict[str, Any] = {"auth_url": values.get("auth_url") or provider.endpoint}
        entry: dict[str, Any] = {"auth": auth}
        if values.get("application_credential_id"):
            for key in _APP_CRED_AUTH:
                if not values.get(key):
                    raise SecretNotFound(f"secret {provider.credentials_secret!r} is missing {key}")
                auth[key] = values[key]
            entry["auth_type"] = "v3applicationcredential"
        else:
            missing = [k for k in ("username", "password", "project_name") if not values.get(k)]
            if missing:
                raise SecretNotFound(
                    f"secret {provider.credentials_secret!r} is missing {', '.join(missing)}"
                )
            for key in _PASSWORD_AUTH:
                auth[key] = values.get(key) or "Default"
            entry["auth_type"] = "password"
        if values.get("interface"):
            entry["interface"] = values["interface"]
        if values.get("region_name"):
            entry["region_name"] = values["region_name"]
        return entry
    if not provider.cloud:
        raise SecretNotFound(
            f"{provider.id}: neither credentials nor a clouds.yaml cloud configured"
        )
    return load_cloud_auth(provider.cloud, settings)


def write_secret_file(path: Path, content: str) -> Path:
    """Create ``path`` with mode 0600 (never wider, even briefly) and write ``content``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(path, 0o600)
    return path


def remove_quietly(*paths: Path) -> None:
    for path in paths:
        with contextlib.suppress(FileNotFoundError):
            Path(path).unlink()


@contextlib.contextmanager
def secret_files(*paths: Path) -> Iterator[None]:
    """Guarantee removal of ``paths`` when the block exits (success, error or cancellation)."""
    try:
        yield
    finally:
        remove_quietly(*paths)
