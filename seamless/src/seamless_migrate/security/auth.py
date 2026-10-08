"""Static bearer tokens and role checks (SDD §13.1, §13.2).

Only SHA-256 hashes of tokens are stored (``SEAMLESS_TOKENS_FILE``:
``tokens: [{name, role, sha256}]``); comparison uses ``hmac.compare_digest``.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..domain.enums import Role

TOKEN_PREFIX = "smg_"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
LOOPBACK_NAMES = frozenset({"localhost"})


class AuthConfigError(ValueError):
    """The tokens file is missing or malformed."""


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role


@dataclass(frozen=True)
class TokenEntry:
    name: str
    role: Role
    sha256: str


ANONYMOUS_ADMIN = Principal("anonymous", Role.admin)


def generate_token() -> str:
    """A new API token: ``smg_`` + 32 random URL-safe bytes."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_loopback(host: str) -> bool:
    if not host:
        return False
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def auth_disabled_allowed(host: str) -> bool:
    """``SEAMLESS_AUTH_DISABLED`` is only acceptable on a loopback bind address."""
    return is_loopback(host)


class TokenStore:
    def __init__(self, entries: list[TokenEntry] | None = None) -> None:
        self.entries = list(entries or [])

    def __len__(self) -> int:
        return len(self.entries)

    @classmethod
    def from_file(cls, path: str | Path) -> TokenStore:
        try:
            document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise AuthConfigError(f"cannot read tokens file {path}: {exc.strerror}") from None
        except yaml.YAMLError as exc:
            raise AuthConfigError(f"tokens file {path} is not valid YAML: {exc}") from None
        items = document.get("tokens") if isinstance(document, dict) else None
        if not isinstance(items, list):
            raise AuthConfigError("tokens file must contain a 'tokens' list")
        entries = []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not item.get("name"):
                raise AuthConfigError(f"token #{index} has no name")
            try:
                role = Role(str(item.get("role")))
            except ValueError:
                raise AuthConfigError(f"token {item['name']!r} has an unknown role") from None
            digest = str(item.get("sha256") or "").lower()
            if not _SHA256.match(digest):
                raise AuthConfigError(f"token {item['name']!r} needs a hex sha256 digest")
            entries.append(TokenEntry(name=str(item["name"]), role=role, sha256=digest))
        return cls(entries)

    def authenticate(self, token: str) -> Principal | None:
        if not token:
            return None
        digest = hash_token(token)
        match: TokenEntry | None = None
        for entry in self.entries:  # no early exit: constant work per request
            if hmac.compare_digest(entry.sha256, digest):
                match = entry
        return Principal(match.name, match.role) if match else None

    @staticmethod
    def yaml_entry(name: str, role: Role | str, token: str) -> str:
        return yaml.safe_dump(
            {"tokens": [{"name": name, "role": str(Role(role)), "sha256": hash_token(token)}]},
            sort_keys=False,
        )
