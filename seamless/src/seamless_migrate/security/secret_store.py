"""Writable secret store for credentials entered in the dashboard (SDD §13.3).

Two backends, chosen by ``SEAMLESS_SECRET_STORE``:

* ``files`` — ``{SEAMLESS_SECRETS_DIR}/{name}/{key}``: 0700 directory, 0600 files, every key written
  to a temporary file in the same directory and renamed, so a reader never sees half a value.
* ``kubernetes`` — a ``Secret`` named ``seamless-{name}`` in the pod's namespace, created or
  replaced through the API server with the pod's ServiceAccount token. Reads go through the API
  too, so a credential takes effect without a pod restart.

Values never leave this module except to the code that connects to a cloud: callers get key *names*
back, and nothing here logs a value.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..config import Settings

_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{0,200}$")
_KEY = re.compile(r"^[A-Za-z0-9_]+$")
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MANAGED_BY = "seamless-migrate"
K8S_PREFIX = "seamless-"


class SecretStoreError(RuntimeError):
    """The store cannot be read or written (permissions, API server unreachable, …)."""


class SecretStore(Protocol):
    def read(self, name: str) -> dict[str, str] | None: ...
    def write(self, name: str, values: Mapping[str, str], labels: Mapping[str, str]) -> None: ...
    def delete(self, name: str) -> None: ...


def _check(name: str, values: Mapping[str, str] | None = None) -> None:
    if not _NAME.match(name):
        raise SecretStoreError(f"invalid secret name {name!r}")
    for key in values or {}:
        if not _KEY.match(key):
            raise SecretStoreError(f"invalid secret key {key!r}")


class FileSecretStore:
    """``{root}/{name}/{key}`` files (Compose, single host)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def read(self, name: str) -> dict[str, str] | None:
        _check(name)
        directory = self.root / name
        if not directory.is_dir():
            return None
        return {
            entry.name: entry.read_text(encoding="utf-8").strip()
            for entry in sorted(directory.iterdir())
            if not entry.name.startswith(".") and _KEY.match(entry.name) and entry.is_file()
        }

    def write(self, name: str, values: Mapping[str, str], labels: Mapping[str, str]) -> None:
        _check(name, values)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            directory = self.root / name
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
            for stale in directory.iterdir():  # a secret is replaced as a whole
                if stale.is_file() and stale.name not in values:
                    stale.unlink()
            for key, value in values.items():
                fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(value)
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, directory / key)
                except BaseException:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(tmp)
                    raise
        except OSError as exc:
            raise SecretStoreError(
                f"cannot write the secret store at {self.root}: {exc.strerror}"
            ) from None

    def delete(self, name: str) -> None:
        _check(name)
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(self.root / name)


class KubernetesSecretStore:
    """``Secret`` objects through the API server (OpenShift / Kubernetes)."""

    def __init__(
        self,
        namespace: str | None = None,
        *,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        verify: Any = None,
    ) -> None:
        self.namespace = namespace or self._read_sa("namespace")
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.base_url = base_url or f"https://{host}:{port}"
        self._token = token
        self._transport = transport
        self._verify = verify if verify is not None else str(SA_DIR / "ca.crt")

    @staticmethod
    def _read_sa(name: str) -> str:
        try:
            return (SA_DIR / name).read_text(encoding="utf-8").strip()
        except OSError:
            raise SecretStoreError(
                f"no ServiceAccount {name} at {SA_DIR} (automountServiceAccountToken and the "
                "Role of SDD §17 are required for SEAMLESS_SECRET_STORE=kubernetes)"
            ) from None

    def _client(self) -> httpx.Client:
        token = self._token or self._read_sa("token")  # re-read: projected tokens rotate
        return httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
            transport=self._transport,
            verify=self._verify if self._transport is None else True,
            follow_redirects=False,
        )

    def _path(self, name: str = "") -> str:
        base = f"/api/v1/namespaces/{self.namespace}/secrets"
        return f"{base}/{K8S_PREFIX}{name}" if name else base

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            with self._client() as client:
                response = client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SecretStoreError(f"Kubernetes API unreachable: {type(exc).__name__}") from None
        if response.status_code in (401, 403):
            raise SecretStoreError(
                f"the ServiceAccount may not {method} secrets in {self.namespace} "
                f"(HTTP {response.status_code}; see the Role of SDD §17)"
            )
        return response

    def read(self, name: str) -> dict[str, str] | None:
        _check(name)
        response = self._request("GET", self._path(name))
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SecretStoreError(f"reading secret failed: HTTP {response.status_code}")
        data = response.json().get("data") or {}
        return {k: base64.b64decode(v).decode("utf-8") for k, v in data.items()}

    def write(self, name: str, values: Mapping[str, str], labels: Mapping[str, str]) -> None:
        _check(name, values)
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": K8S_PREFIX + name,
                "labels": {"app.kubernetes.io/managed-by": MANAGED_BY, **labels},
            },
            "data": {
                k: base64.b64encode(v.encode("utf-8")).decode("ascii") for k, v in values.items()
            },
        }
        response = self._request("PUT", self._path(name), json=body)
        if response.status_code == 404:
            response = self._request("POST", self._path(), json=body)
        if response.status_code not in (200, 201):
            raise SecretStoreError(f"writing secret failed: HTTP {response.status_code}")

    def delete(self, name: str) -> None:
        _check(name)
        response = self._request("DELETE", self._path(name))
        if response.status_code not in (200, 202, 404):
            raise SecretStoreError(f"deleting secret failed: HTTP {response.status_code}")


def secret_store(settings: Settings) -> SecretStore:
    if settings.secret_store == "kubernetes":
        return KubernetesSecretStore(settings.k8s_namespace)
    return FileSecretStore(Path(settings.secrets_dir))


def credentials_secret_name(provider_id: str) -> str:
    return f"provider-{provider_id}"


def conversion_key_secret_name(provider_id: str) -> str:
    return f"provider-{provider_id}-ssh"
