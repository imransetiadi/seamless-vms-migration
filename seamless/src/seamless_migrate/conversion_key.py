"""One conversion key per installation (SDD §7.2 "Conversion key", issue #112).

The managed conversion hosts are per provider and the ``os_migrate_conv`` keypair belongs to the
credential's user, so every OpenStack plan reaches them with one key, ``{data_dir}/conversion/
ssh.key``. Before a pre-staging deploys conversion hosts, :func:`resolve` reads that keypair on
each managed side and settles the key against it: it adopts the plan's key the clouds hold, lets
the deploy generate one, names the sides to re-key, or refuses with the runbook.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import shutil
import threading
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .config import Settings
from .domain.models import Provider
from .executors.base import PermanentStepError

log = logging.getLogger(__name__)

#: the collection's default keypair name, which the executor never changes (SDD §7.2)
KEYPAIR = "os_migrate_conv"
RUNBOOK = "docs/Operations.md §7.1"
#: a key's identity: its type and its blob, the base64 field decoded (SDD §7.2)
KeyId = tuple[str, bytes]

#: one resolution at a time in this process: two pre-stagings never adopt over each other
_SETTLING = threading.Lock()


def installation_key(settings: Settings) -> Path:
    """The installation's conversion key, ``{data_dir}/conversion/ssh.key`` (SDD §7.2)."""
    return Path(settings.data_dir) / "conversion" / "ssh.key"


def earlier_plan_key(settings: Settings, plan_id: str) -> Path:
    """The conversion key an earlier version made for the plan, one per plan (SDD §7.2)."""
    return Path(settings.data_dir) / "plans" / plan_id / "osm" / "conversion" / "ssh.key"


def run_key(settings: Settings, plan_id: str) -> Path:
    """The conversion key a run of the plan reaches the hosts with (SDD §7.2): the
    installation's - until it exists, the key an earlier version made for the plan, which its
    hosts accept, so a migration in flight across the upgrade keeps reaching them."""
    key = installation_key(settings)
    if not key.exists():
        own = earlier_plan_key(settings, plan_id)
        if own.exists():
            return own
    return key


def key_id(text: str | None) -> KeyId | None:
    """A public key's identity as OpenSSH reads one: its type and the blob its base64 field
    holds, never its comment; None for text that holds no key (SDD §7.2)."""
    fields = (text or "").split()
    if len(fields) < 2:
        return None
    kind = fields[0]
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    # the blob names its own type first, as an SSH string: a key of another type is no key
    size = int.from_bytes(blob[:4], "big")
    if len(blob) < 4 + size or blob[4 : 4 + size] != kind.encode():
        return None
    return kind, blob


def fingerprint(text: str | None) -> str:
    """The key's SHA256 fingerprint as ``ssh-keygen -l`` prints it."""
    identity = key_id(text)
    if identity is None:
        return "a text that is no public key"
    digest = base64.b64encode(hashlib.sha256(identity[1]).digest()).decode()
    return "SHA256:" + digest.rstrip("=")


async def resolve(
    settings: Settings,
    source: Provider,
    destination: Provider,
    client_of: Callable[[Provider], Any],
) -> dict[str, Path]:
    """Settle the installation's conversion key against the os_migrate_conv keypair of each side
    whose conversion host the deploy manages (SDD §7.2, issue #112): adopt the plan's key the
    keypairs hold, or leave the deploy to generate one, and return the sides to re-key, each
    with the earlier plan's private key its keypair holds. A refusal is a PermanentStepError
    naming the providers and the keys' fingerprints; a keypair read that fails, its provider
    error."""
    held: dict[str, tuple[Provider, str]] = {}
    for side, provider in (("src", source), ("dst", destination)):
        host = provider.conversion_host
        if host is None or not host.manage:
            continue  # the deploy leaves an operator's host and its keypair alone
        public_key = await client_of(provider).keypair_public_key(KEYPAIR)
        if public_key is not None:
            held[side] = (provider, public_key)
    return await asyncio.to_thread(_settle, settings, held)


def _settle(settings: Settings, held: dict[str, tuple[Provider, str]]) -> dict[str, Path]:
    with _SETTLING:
        target = installation_key(settings)
        plans = _plan_keys(settings)
        installed = _installed(target)
        if installed is None:
            adopted = _adoption(held, plans)
            if adopted is None or _adopt(adopted, target):
                return {}
            # another pre-staging's deploy generated the key meanwhile: the sides go by it
            installed = _installed(target)
            if installed is None:
                raise _incomplete(target)
        return _rekeys(held, installed, plans)


def _plan_keys(settings: Settings) -> dict[KeyId, Path]:
    """Each key an earlier version made for a plan, by its identity: the private key beside its
    ``.pub``, the first plan by id of each key (SDD §7.2)."""
    out: dict[KeyId, Path] = {}
    for pub in sorted((Path(settings.data_dir) / "plans").glob("*/osm/conversion/ssh.key.pub")):
        private = pub.with_suffix("")
        try:
            identity = key_id(pub.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("conversion key %s left out: %s", pub, exc)
            continue
        if identity is not None and private.is_file():
            out.setdefault(identity, private)
    return out


def _installed(target: Path) -> KeyId | None:
    """The installation key's identity, None when it does not exist yet."""
    if not target.exists():
        return None
    try:
        identity = key_id(Path(f"{target}.pub").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        identity = None
    if identity is None:
        raise _incomplete(target)
    return identity


def _incomplete(target: Path) -> PermanentStepError:
    return PermanentStepError(
        f"the installation's conversion key {target} has no readable public key beside it "
        f"({target}.pub): see {RUNBOOK}"
    )


def _adoption(held: dict[str, tuple[Provider, str]], plans: dict[KeyId, Path]) -> Path | None:
    """The plan's private key every side that holds the keypair holds, None when no side holds
    one; a key no plan's matches, or two sides that disagree, are refused (SDD §7.2)."""
    _refuse_unknown(held, plans)
    keys = {key_id(public_key) for _, public_key in held.values()}
    if len(keys) > 1:
        raise PermanentStepError(
            f"the {KEYPAIR} keypairs of {_named(held.values())} hold different keys, and this "
            f"installation has no conversion key yet to re-key them to: see {RUNBOOK}"
        )
    identity = keys.pop() if keys else None
    return None if identity is None else plans[identity]


def _rekeys(
    held: dict[str, tuple[Provider, str]], installed: KeyId, plans: dict[KeyId, Path]
) -> dict[str, Path]:
    """The sides whose keypair holds an earlier plan's key, each with that key (SDD §7.2)."""
    others = {side: entry for side, entry in held.items() if key_id(entry[1]) != installed}
    _refuse_unknown(others, plans)
    out: dict[str, Path] = {}
    for side, (_, public_key) in others.items():
        identity = key_id(public_key)
        assert identity is not None  # _refuse_unknown refused any other
        out[side] = plans[identity]
    return out


def _refuse_unknown(held: dict[str, tuple[Provider, str]], plans: dict[KeyId, Path]) -> None:
    unknown = [entry for entry in held.values() if key_id(entry[1]) not in plans]
    if unknown:
        many = len(unknown) > 1
        hosts = "their conversion hosts" if many else "its conversion host"
        raise PermanentStepError(
            f"the {KEYPAIR} keypair{'s' if many else ''} of {_named(unknown)} "
            f"hold{'' if many else 's'} a key that neither this installation's conversion key "
            f"nor any plan's matches, so {hosts} can be neither reached nor re-keyed: see "
            f"{RUNBOOK}"
        )


def _named(entries: Iterable[tuple[Provider, str]]) -> str:
    return " and ".join(f"{p.label} ({fingerprint(key)})" for p, key in entries)


def _adopt(private: Path, target: Path) -> bool:
    """Copy a plan's key pair to the installation's path - never move it -: the private key 0600
    in a 0700 directory, the public key with its own mode. Never over a key that exists: False
    when one does (SDD §7.2)."""
    content = private.read_bytes()
    directory = target.parent
    if not directory.exists():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)  # mkdir's mode passes through the umask
    staged = directory / f".adopting.{uuid.uuid4().hex}.pub"
    try:
        shutil.copy2(f"{private}.pub", staged)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(target, 0o600)
            os.replace(staged, f"{target}.pub")
        except BaseException:
            target.unlink(missing_ok=True)  # this call made it: no half a key stays behind
            raise
        return True
    finally:
        staged.unlink(missing_ok=True)
