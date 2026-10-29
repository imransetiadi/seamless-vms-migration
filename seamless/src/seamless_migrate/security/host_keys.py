"""The conversion hosts' SSH host keys (SDD §4.2, §10 "Host key pins", §26.3, §26.4).

* :func:`public_key_problem` judges a key the operator declares (``ssh_host_keys``): OpenSSH's
  one-line form, ed25519, ECDSA or RSA.
* :func:`console_keys` reads a conversion host's console: the keys of cloud-init's keys block
  alone, or, where the image prints the fingerprints alone, the keys read from the host whose
  fingerprint it printed.
* :func:`write_known_hosts` writes a run's known_hosts: one hashed entry per key under the host's
  alias, its declared keys over its pin, never the pin of another server.

The console is the guest's text: nothing here logs, keeps or sends a line of it (SDD §13.4,
§26.3) - a pin holds the keys alone, without their comments.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import os
import re
import secrets
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

#: the types a conversion host's key may have: ed25519, ECDSA or RSA (SDD §4.2, §26.3)
KEY_TYPES = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)
#: the size of each curve's point, uncompressed: 0x04, then its two coordinates (RFC 5656 §3.1)
_POINT_BYTES = {"nistp256": 65, "nistp384": 97, "nistp521": 133}
#: the alias each side's conversion host is checked under - its inventory name -, and a VMware
#: destination's host's, the name of its inventory group (SDD §26.4)
SIDE_ALIASES = {"source": "os_migrate_conv_src", "destination": "os_migrate_conv_dst"}
VMWARE_ALIAS = "conversion_host"

#: cloud-init's blocks (its keys-to-console module, SDD §26.3)
_KEYS = ("-----BEGIN SSH HOST KEY KEYS-----", "-----END SSH HOST KEY KEYS-----")
_PRINTS = ("-----BEGIN SSH HOST KEY FINGERPRINTS-----", "-----END SSH HOST KEY FINGERPRINTS-----")
#: a key in a line of the keys block, whatever a console prefixes to it: its type, its base64 field
_KEY_IN_LINE = re.compile(
    r"(?:^|\s)(" + "|".join(map(re.escape, KEY_TYPES)) + r")\s+([A-Za-z0-9+/]+={0,2})(?=\s|$)"
)
#: a SHA256 fingerprint as ssh-keygen -l prints one: 32 bytes, base64 without its padding
_FINGERPRINT = re.compile(r"(?<![A-Za-z0-9+/])SHA256:[A-Za-z0-9+/]{43}(?![A-Za-z0-9+/=])")
#: an OpenSSH key type's name, which a refusal may name: never any other part of a line
_TYPE_NAME = re.compile(r"(?:ssh|sk|ecdsa|x509v3|rsa)-[a-z0-9@.-]{1,60}")
#: HashKnownHosts' salt: as long as the HMAC-SHA1 it keys (OpenSSH's hostfile.c)
_SALT_BYTES = 20


def _strings(blob: bytes) -> list[bytes] | None:
    """The SSH wire strings ``blob`` is made of (RFC 4251 §5), None when it is not made of them."""
    parts, at = [], 0
    while at < len(blob):
        if at + 4 > len(blob):
            return None
        size = int.from_bytes(blob[at : at + 4], "big")
        at += 4
        if at + size > len(blob):
            return None
        parts.append(blob[at : at + size])
        at += size
    return parts


def _holds_key(kind: str, field: str) -> bool:
    """Whether a base64 field holds a public key of ``kind``: the type it names first, then its
    parts - an ed25519 key's 32 bytes, an RSA key's exponent and modulus, an ECDSA key's curve and
    point (RFC 8709, RFC 4253 §6.6, RFC 5656 §3.1)."""
    try:
        blob = base64.b64decode(field, validate=True)
    except (binascii.Error, ValueError):
        return False
    parts = _strings(blob)
    if not parts or parts[0] != kind.encode():
        return False
    if kind == "ssh-ed25519":
        return len(parts) == 2 and len(parts[1]) == 32
    if kind == "ssh-rsa":
        return len(parts) == 3 and all(parts[1:])
    curve = kind.removeprefix("ecdsa-sha2-")
    return (
        len(parts) == 3
        and parts[1] == curve.encode()
        and len(parts[2]) == _POINT_BYTES[curve]
        and parts[2][:1] == b"\x04"
    )


def public_key_problem(line: str) -> str | None:
    """Why ``line`` is no key a conversion host's key may be (SDD §4.2): None for one -
    ``<type> <base64> [comment]``, of a type of KEY_TYPES, its base64 field holding a key of that
    type. The reason never quotes the line, which may be a private key; it names a type alone."""
    text = line.strip()
    if not text:
        return "empty: no public key"
    if "PRIVATE KEY" in text or text.startswith(("-----BEGIN", "PuTTY-User-Key-File")):
        return "a private key: declare its public key"
    if "\n" in text or "\r" in text:
        return "more than one line: one key per entry"
    fields = text.split()
    kind = fields[0]
    if kind in KEY_TYPES:
        if len(fields) < 2 or not _holds_key(kind, fields[1]):
            return f"its base64 field holds no {kind} key"
        return None
    if any(field in KEY_TYPES for field in fields[1:]):
        return "options or a host name before the key type: declare the key alone"
    if _TYPE_NAME.fullmatch(kind):
        return f"{kind} is not an ed25519, ECDSA or RSA key"
    return "no OpenSSH public key"


def declared_key_problems(lines: Sequence[str]) -> list[str]:
    """``line <n>: <why>`` for each of ``lines`` that is no key a conversion host's key may be,
    ``n`` counting from 1 (SDD §4.2, §12)."""
    return [
        f"line {number}: {why}"
        for number, line in enumerate(lines, 1)
        if (why := public_key_problem(line)) is not None
    ]


def bare_key(line: str) -> str:
    """A public key line without its comment: its type and its base64 field."""
    return " ".join(line.split()[:2])


def fingerprint(key: str) -> str:
    """A public key's SHA256 fingerprint, as ``ssh-keygen -l`` prints it (SDD §4.3)."""
    blob = base64.b64decode(key.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _blocks(console: str) -> tuple[list[list[str]], list[list[str]]]:
    """The console's complete keys blocks, each its keys in order, and its complete fingerprint
    blocks, each its fingerprints: a block's BEGIN without its END is none, and a BEGIN within a
    block starts it again."""
    keys: list[list[str]] = []
    prints: list[list[str]] = []
    kind: str | None = None
    found: list[str] = []
    for line in console.splitlines():
        if _KEYS[0] in line or _PRINTS[0] in line:
            kind, found = ("keys" if _KEYS[0] in line else "prints"), []
        elif kind == "keys" and _KEYS[1] in line:
            keys.append(found)
            kind = None
        elif kind == "prints" and _PRINTS[1] in line:
            prints.append(found)
            kind = None
        elif kind == "keys":
            match = _KEY_IN_LINE.search(line)
            if match is not None and _holds_key(*match.groups()):
                found.append(" ".join(match.groups()))
        elif kind == "prints":
            found.extend(_FINGERPRINT.findall(line))
    return keys, prints


def _agreed(blocks: list[list[str]]) -> tuple[str, ...] | None:
    """What the blocks hold, in the first one's order; None when two of them disagree."""
    if not blocks:
        return ()
    first = tuple(dict.fromkeys(blocks[0]))
    if any(set(block) != set(first) for block in blocks[1:]):
        return None
    return first


def console_keys(console: str | None, read_keys: Iterable[str] = ()) -> list[str]:
    """The keys a conversion host's console pins, each without its comment (SDD §10, §26.3).

    Its keys block's keys, and nothing else of it; none when two blocks disagree. A console
    without the block, or whose blocks hold no key - an image that prints the fingerprints
    alone - pins each of ``read_keys``, keys read from the host, whose SHA256 fingerprint its
    fingerprint block printed; none when two of those disagree. Nothing is logged."""
    key_blocks, print_blocks = _blocks(console or "")
    keys = _agreed(key_blocks)
    if keys is None:
        return []
    if keys:
        return list(keys)
    printed = _agreed(print_blocks)
    if not printed:
        return []
    read = (bare_key(line) for line in read_keys if public_key_problem(line) is None)
    return [key for key in dict.fromkeys(read) if fingerprint(key) in printed]


class Pin(Protocol):
    """What the writer reads of a provider's pin, ``domain.models.HostKeyPin`` (SDD §4.2): the
    domain model reads this module to judge its declared keys, so this one names no model."""

    @property
    def server_id(self) -> str:
        """The Nova server whose keys they are: the pin applies to it alone."""

    @property
    def keys(self) -> Sequence[str]:
        """Its public keys, each ``<type> <base64>``."""


@dataclass(frozen=True)
class KnownHost:
    """A conversion host as a run's known_hosts holds it (SDD §10, §26.4)."""

    #: what ssh checks its key under (HostKeyAlias): SIDE_ALIASES' name for its side, VMWARE_ALIAS
    alias: str
    #: the Nova server the host resolves to now; None when it resolves to none
    server_id: str | None
    #: its declared keys, ``conversion_host.ssh_host_keys``
    declared: Sequence[str] = ()
    #: the pin of the credential the run reaches it with: its provider's ``conversion_host_pin``,
    #: or a project credential's (``credentials.host_pin``, SDD §26.4)
    pin: Pin | None = None


def trusted_keys(host: KnownHost) -> list[str]:
    """The keys ``host`` is checked against, without comments: its declared keys when it has
    some, else its pin's when the pin is its server's - never the pin of another server, nor a
    pin when the host resolves to no server (SDD §10, §26.4)."""
    keys: Sequence[str] = ()
    pin = host.pin
    if host.declared:
        keys = host.declared
    elif pin is not None and host.server_id is not None and pin.server_id == host.server_id:
        keys = pin.keys
    return list(dict.fromkeys(bare_key(key) for key in keys if public_key_problem(key) is None))


def hashed_host(alias: str, salt: bytes) -> str:
    """``alias`` as HashKnownHosts writes a host: ``|1|<salt>|<HMAC-SHA1 of it keyed with the
    salt>``, both base64 (OpenSSH's hostfile.c)."""
    digest = hmac.new(salt, alias.encode(), hashlib.sha1).digest()
    return f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(digest).decode()}"


def write_known_hosts(path: Path, hosts: Iterable[KnownHost]) -> dict[str, list[str]]:
    """Write ``hosts``' known_hosts at ``path``, 0600, replacing the file whole: one hashed entry
    per key of :func:`trusted_keys` under the host's alias, a new salt each (SDD §10, §26.4).
    Returns the keys written per alias - an alias with none is a host ssh refuses (§26.5)."""
    written: dict[str, list[str]] = {}
    for host in hosts:
        keys = written.setdefault(host.alias, [])
        keys.extend(key for key in trusted_keys(host) if key not in keys)
    text = "".join(
        f"{hashed_host(alias, secrets.token_bytes(_SALT_BYTES))} {key}\n"
        for alias, keys in written.items()
        for key in keys
    )
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".known_hosts.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:  # mkstemp's file is 0600
            out.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return written
