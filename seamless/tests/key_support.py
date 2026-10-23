"""Synthetic SSH keys for the conversion key's and the host keys' tests (SDD §7.2 "Conversion key",
issue #112; §4.2, §10, §26.3).

A public key is built from a seed at run time - no key text lives in the repository -, and a
private key is a placeholder line: the control plane copies private keys and never reads them.
The keys are synthetic: each blob has its type's layout, which is all the control plane reads,
not a point or a modulus OpenSSH could use - a test that needs ssh's own reading makes its keys
with ssh-keygen.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
from pathlib import Path

from seamless_migrate.config import Settings


def _string(data: bytes) -> bytes:
    """An SSH wire string: its length, then its bytes (RFC 4251 §5)."""
    return struct.pack(">I", len(data)) + data


def _line(kind: str, blob: bytes, comment: str) -> str:
    return f"{kind} {base64.b64encode(blob).decode()} {comment}".strip()


def public_key(seed: int, comment: str = "") -> str:
    """An ssh-ed25519 public key whose blob is the key type and 32 bytes of ``seed``."""
    kind = b"ssh-ed25519"
    blob = struct.pack(">I", len(kind)) + kind + struct.pack(">I", 32) + bytes([seed]) * 32
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".strip()


def ecdsa_key(seed: int, comment: str = "", curve: str = "nistp256") -> str:
    """An ecdsa-sha2-<curve> public key: its type, its curve and an uncompressed point."""
    size = {"nistp256": 32, "nistp384": 48, "nistp521": 66}[curve]
    kind = f"ecdsa-sha2-{curve}"
    point = b"\x04" + bytes([seed]) * (2 * size)
    return _line(kind, _string(kind.encode()) + _string(curve.encode()) + _string(point), comment)


def rsa_key(seed: int, comment: str = "") -> str:
    """An ssh-rsa public key: the type, the exponent 65537 and a 2048-bit modulus of ``seed``."""
    modulus = b"\x00" + bytes([0x80 | seed]) * 256
    blob = _string(b"ssh-rsa") + _string(b"\x01\x00\x01") + _string(modulus)
    return _line("ssh-rsa", blob, comment)


def other_key(kind: str, seed: int, comment: str = "") -> str:
    """A key of a type the host keys refuse (ssh-dss, a security key, a certificate): its type
    and 32 bytes of ``seed``."""
    return _line(kind, _string(kind.encode()) + _string(bytes([seed]) * 32), comment)


def fingerprint(seed: int) -> str:
    """The seed's key's SHA256 fingerprint, as ``ssh-keygen -l`` prints it."""
    return fingerprint_of(public_key(seed))


def fingerprint_of(line: str) -> str:
    """A public key line's SHA256 fingerprint, as ``ssh-keygen -l`` prints it."""
    blob = base64.b64decode(line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def bare(line: str) -> str:
    """A public key line without its comment: its type and its base64 field."""
    return " ".join(line.split()[:2])


def known_keys(text: str, alias: str) -> list[str]:
    """The keys of a known_hosts file's ``text`` that OpenSSH matches ``alias`` against, in the
    file's order: each ``|1|salt|hash`` entry whose hash is the HMAC-SHA1 of the alias keyed with
    its salt, or an entry that names the alias (OpenSSH's hostfile.c)."""
    keys = []
    for line in text.splitlines():
        if not line.strip():
            continue
        names, key_type, key = line.split()[:3]
        if names.startswith("|1|"):
            salt, digest = (base64.b64decode(part) for part in names[3:].split("|"))
            matched = hmac.compare_digest(
                hmac.new(salt, alias.encode(), hashlib.sha1).digest(), digest
            )
        else:
            matched = alias in names.split(",")
        if matched:
            keys.append(f"{key_type} {key}")
    return keys


def write_pair(private: Path, seed: int, comment: str) -> Path:
    """A key pair as ``ssh-keygen`` leaves it: the private key 0600, its ``.pub`` 0644 beside it,
    in a 0700 directory."""
    private.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private.write_text(f"placeholder for the private key of seed {seed}\n", encoding="utf-8")
    private.chmod(0o600)
    pub = Path(f"{private}.pub")
    pub.write_text(public_key(seed, comment) + "\n", encoding="utf-8")
    pub.chmod(0o644)
    return private


def plan_key(settings: Settings, plan_id: str, seed: int) -> Path:
    """The conversion key a plan of an earlier version made: one per plan (SDD §7.2)."""
    path = Path(settings.data_dir) / "plans" / plan_id / "osm" / "conversion" / "ssh.key"
    return write_pair(path, seed, f"seamless@{plan_id}")


def installation_key(settings: Settings, seed: int | None = None) -> Path:
    """The installation's conversion key, written with ``seed``'s pair when one is given."""
    path = Path(settings.data_dir) / "conversion" / "ssh.key"
    return path if seed is None else write_pair(path, seed, "seamless@installation")
