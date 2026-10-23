"""Synthetic SSH keys for the conversion key's tests (SDD §7.2 "Conversion key", issue #112).

A public key is built from a seed at run time - no key text lives in the repository -, and a
private key is a placeholder line: the control plane copies private keys and never reads them.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from pathlib import Path

from seamless_migrate.config import Settings


def public_key(seed: int, comment: str = "") -> str:
    """An ssh-ed25519 public key whose blob is the key type and 32 bytes of ``seed``."""
    kind = b"ssh-ed25519"
    blob = struct.pack(">I", len(kind)) + kind + struct.pack(">I", 32) + bytes([seed]) * 32
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}".strip()


def fingerprint(seed: int) -> str:
    """The seed's key's SHA256 fingerprint, as ``ssh-keygen -l`` prints it."""
    blob = base64.b64decode(public_key(seed).split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


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
