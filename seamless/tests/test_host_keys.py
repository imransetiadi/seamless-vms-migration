"""The conversion hosts' keys (SDD §10 "Host key pins", §26.3, §26.4; plan task ST2): the console
parser and the known_hosts writer of ``seamless_migrate.security.host_keys``.

Every console here is synthetic - cloud-init's output as an image prints it, its addresses in
RFC 5737's 192.0.2.0/24 -, and every key is made by the test: from a seed (``tests.key_support``),
or with ssh-keygen where ssh itself reads the file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from seamless_migrate.domain.models import HostKeyPin, utcnow
from seamless_migrate.security.host_keys import (
    SIDE_ALIASES,
    VMWARE_ALIAS,
    KnownHost,
    console_keys,
    write_known_hosts,
)
from tests.key_support import (
    bare,
    ecdsa_key,
    fingerprint,
    fingerprint_of,
    other_key,
    public_key,
    rsa_key,
)

SSH_KEYGEN = shutil.which("ssh-keygen")
SOURCE = SIDE_ALIASES["source"]
DESTINATION = SIDE_ALIASES["destination"]

BEGIN = "-----BEGIN SSH HOST KEY KEYS-----"
END = "-----END SSH HOST KEY KEYS-----"
BOOT = "\n".join(
    [
        "[    0.000000] Linux version 5.14.0-427.13.1.el9_4.x86_64 (mockbuild@example.org)",
        "[   11.205334] cloud-init[812]: Cloud-init v. 23.4-7.el9_4 running 'init' at Thu, 23 Oct",
        "ci-info: | eth0 | True | 192.0.2.15 | 255.255.255.0 | global | fa:16:3e:00:00:01 |",
        "ci-info: no authorized SSH keys fingerprints found for user cloud-user.",
    ]
)
LOGIN = "\n".join(
    [
        "[   24.912007] cloud-init[1203]: Cloud-init v. 23.4-7.el9_4 finished at Thu, 23 Oct",
        "",
        "Red Hat Enterprise Linux 9.4 (Plow)",
        "conv-src login: ",
    ]
)


def _keys_block(*keys: str) -> str:
    """cloud-init's keys block: its keys as the host's .pub files hold them, unprefixed."""
    return "\n".join([BEGIN, *keys, END])


def _fingerprints_block(*lines: str) -> str:
    """cloud-init's fingerprints block, which it writes through logger: each line prefixed."""
    tag = "<14>Oct 23 08:00:01 cloud-init: "
    body = [
        "#" * 61,
        "-----BEGIN SSH HOST KEY FINGERPRINTS-----",
        *lines,
        "-----END SSH HOST KEY FINGERPRINTS-----",
        "#" * 61,
    ]
    return "\n".join(tag + line for line in body)


def _console(*parts: str) -> str:
    return "\r\n".join([BOOT, *parts, LOGIN])


def _pin(server_id: str, *keys: str) -> HostKeyPin:
    return HostKeyPin(
        server_id=server_id, keys=[bare(k) for k in keys], source="console", pinned_at=utcnow()
    )


def _matched(path: Path, alias: str) -> list[str]:
    """The keys of the entries OpenSSH matches ``alias`` against: each ``|1|salt|hash`` entry
    whose hash is the HMAC-SHA1 of the alias keyed with its salt (OpenSSH's hostfile.c)."""
    keys = []
    for line in path.read_text(encoding="utf-8").splitlines():
        marker, key_type, key = line.split()[:3]
        _, version, salt, digest = marker.split("|")
        assert version == "1", line
        mac = hmac.new(base64.b64decode(salt), alias.encode(), hashlib.sha1).digest()
        if hmac.compare_digest(base64.b64encode(mac).decode(), digest):
            keys.append(f"{key_type} {key}")
    return keys


def _made_key(directory: Path, kind: str, name: str) -> str:
    """A host key pair made with ssh-keygen, as a host's ``ssh_host_<kind>_key.pub`` holds it."""
    path = directory / f"ssh_host_{kind}_key_{name}"
    size = ["-b", "2048"] if kind == "rsa" else []
    subprocess.run(
        [SSH_KEYGEN, "-q", "-t", kind, *size, "-N", "", "-C", f"root@{name}", "-f", str(path)],
        check=True,
        capture_output=True,
    )
    return Path(f"{path}.pub").read_text(encoding="utf-8").strip()


def test_the_console_keys_block_is_pinned_and_nothing_else():
    """SDD §10, §26.3: the keys of cloud-init's block, in its order and without their comments -
    the host's name is the guest's text -, and nothing else of the console: a key line before or
    after the block, a fingerprint, a kernel line inside it, a key of another type, a cut key."""
    ed25519 = public_key(1, "root@conv-src")
    ecdsa = ecdsa_key(2, "root@conv-src")
    rsa = rsa_key(3, "root@conv-src")
    stray = public_key(9, "ops@bastion")
    console = _console(
        stray,
        f"ci-info: | ssh-ed25519 | {stray.split()[1]} | - | ops@bastion |",
        _fingerprints_block(f"256 {fingerprint(1)} root@conv-src (ED25519)"),
        BEGIN,
        ecdsa,
        "[   21.004211] random: crng init done",
        other_key("ssh-dss", 4, "root@conv-src"),
        ed25519[:40],
        ed25519,
        rsa,
        END,
        public_key(8, "root@conv-src"),
    )
    assert console_keys(console) == [bare(ecdsa), bare(ed25519), bare(rsa)]
    # a block whose lines the console prefixes, as an image that writes it through logger does
    prefixed = "\n".join(f"cloud-init[1203]: {line}" for line in _keys_block(ed25519).splitlines())
    assert console_keys(_console(prefixed)) == [bare(ed25519)]


def test_a_console_without_the_block_pins_nothing():
    """SDD §10, §26.3: no keys block, no key - whatever key lines the console holds elsewhere,
    and a block it cut (no END) or one whose BEGIN it lost."""
    key = public_key(1, "root@conv-src")
    for console in (
        None,
        "",
        _console(),
        _console(key),
        _console(BEGIN, key),
        _console(key, END),
        _console(_fingerprints_block(f"256 {fingerprint(1)} root@conv-src (ED25519)")),
    ):
        assert console_keys(console) == [], console


def test_two_console_blocks_that_disagree_pin_nothing():
    """SDD §10, §26.3: a block printed again with the same keys (a console the instance's reboot
    appended to) pins them; two blocks with other keys pin nothing, whichever came first."""
    a, b, c = public_key(1, "root@conv-src"), public_key(2, "root@conv-src"), ecdsa_key(3)
    assert console_keys(_console(_keys_block(a, c), "reboot", _keys_block(a, c))) == [
        bare(a),
        bare(c),
    ]
    for first, second in (((a, c), (b, c)), ((a, c), (a,)), ((), (a,)), ((b,), (a,))):
        assert console_keys(_console(_keys_block(*first), _keys_block(*second))) == []


def test_a_fingerprints_only_console_pins_a_read_key_only_when_its_fingerprint_was_printed():
    """SDD §10, §26.3: an image that prints the fingerprints alone pins a key read from the host
    only when the console's fingerprint block printed its SHA256 fingerprint - one printed
    anywhere else counts for nothing, and fingerprint blocks that disagree pin nothing."""
    printed = public_key(1, "root@conv-src")
    unprinted = ecdsa_key(2, "root@conv-src")
    block = _fingerprints_block(
        f"256 {fingerprint(1)} root@conv-src (ED25519)",
        f"3072 SHA256:{'Q' * 43} root@conv-src (RSA)",
    )
    console = _console(block, f"a fingerprint outside the block: {fingerprint_of(unprinted)}")
    assert console_keys(console, read_keys=[printed, unprinted]) == [bare(printed)]
    assert console_keys(console, read_keys=[unprinted]) == []
    assert console_keys(console) == []
    # an empty keys block is an image that prints the fingerprints alone
    assert console_keys(_console(block, _keys_block()), read_keys=[unprinted, printed]) == [
        bare(printed)
    ]
    # a keys block that holds keys decides: the keys read from the host count for nothing
    held = public_key(5, "root@conv-src")
    assert console_keys(_console(block, _keys_block(held)), read_keys=[printed]) == [bare(held)]
    other = _fingerprints_block(f"256 {fingerprint_of(unprinted)} root@conv-src (ECDSA)")
    assert console_keys(_console(block, other), read_keys=[printed, unprinted]) == []


def test_the_parser_logs_no_line_of_the_console(caplog, capsys):
    """SDD §10, §13.4, §26.3: the console is the guest's text - the parser writes nothing of it,
    whatever it finds: a block, none, two that disagree, fingerprints alone, junk."""
    caplog.set_level(logging.DEBUG)
    key = public_key(1, "root@conv-src")
    consoles = [
        _console(_keys_block(key)),
        _console(key),
        _console(_keys_block(key), _keys_block(public_key(2))),
        _console(_fingerprints_block(f"256 {fingerprint(1)} root@conv-src (ED25519)")),
        _console(BEGIN, "\x1b[31mnot a key\x1b[0m", "\x00�", END),
    ]
    for console in consoles:
        console_keys(console, read_keys=[key, "no key at all"])
    assert caplog.records == []
    out = capsys.readouterr()
    assert (out.out, out.err) == ("", "")


@pytest.mark.skipif(SSH_KEYGEN is None, reason="ssh-keygen is not installed")
def test_known_hosts_holds_one_hashed_entry_per_key_under_its_side_alias(tmp_path):
    """SDD §10, §26.4: one entry per key, hashed as HashKnownHosts writes one - ssh-keygen -F
    finds each host's keys under its alias, and only those -, a new salt each, no alias in clear,
    the file 0600; a key declared twice is one entry."""
    made = tmp_path / "made"
    made.mkdir()
    source = [_made_key(made, "ed25519", "conv-src"), _made_key(made, "ecdsa", "conv-src")]
    destination = _made_key(made, "rsa", "conv-dst")
    vmware = _made_key(made, "ed25519", "conv-vmw")
    path = tmp_path / "run" / "known_hosts"
    path.parent.mkdir()
    write_known_hosts(
        path,
        [
            KnownHost(SOURCE, "srv-src", pin=_pin("srv-src", *source)),
            KnownHost(DESTINATION, "srv-dst", declared=[destination, destination]),
            KnownHost(VMWARE_ALIAS, "srv-vmw", declared=[vmware]),
        ],
    )
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 4 and all(line.startswith("|1|") for line in lines), text
    assert len({line.split("|")[2] for line in lines}) == 4  # a new salt for each entry
    assert not any(alias in text for alias in (SOURCE, DESTINATION, VMWARE_ALIAS, "conv-"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for alias, keys in ((SOURCE, source), (DESTINATION, [destination]), (VMWARE_ALIAS, [vmware])):
        found = subprocess.run(
            [SSH_KEYGEN, "-F", alias, "-f", str(path)], capture_output=True, text=True
        )
        entries = [line for line in found.stdout.splitlines() if line and line[0] != "#"]
        assert found.returncode == 0, found.stderr
        assert sorted(" ".join(entry.split()[1:3]) for entry in entries) == sorted(map(bare, keys))
    unknown = subprocess.run(
        [SSH_KEYGEN, "-F", "os_migrate_conv_other", "-f", str(path)], capture_output=True, text=True
    )
    assert unknown.returncode == 1 and unknown.stdout == ""


def test_declared_keys_win_over_the_pin(tmp_path):
    """SDD §10, §26.3: a host with declared keys is checked against them alone, its pin of the same
    server aside; a host without declared keys against its pin."""
    pinned = public_key(1, "root@conv-src")
    declared = [ecdsa_key(2, "set by the operator"), public_key(3)]
    path = tmp_path / "known_hosts"
    write_known_hosts(
        path,
        [
            KnownHost(SOURCE, "srv-src", declared=declared, pin=_pin("srv-src", pinned)),
            KnownHost(DESTINATION, "srv-dst", pin=_pin("srv-dst", pinned)),
        ],
    )
    assert _matched(path, SOURCE) == [bare(key) for key in declared]
    assert _matched(path, DESTINATION) == [bare(pinned)]


def test_a_pin_of_another_server_is_not_used(tmp_path):
    """SDD §10, §26.4: a pin belongs to its server - a host that resolves to another server, or to
    none, gets no entry from it (ssh then refuses the host), while its declared keys still count."""
    pin = _pin("srv-old", public_key(1, "root@conv-src"))
    declared = public_key(2)
    path = tmp_path / "known_hosts"
    write_known_hosts(
        path, [KnownHost(SOURCE, "srv-new", pin=pin), KnownHost(DESTINATION, None, pin=pin)]
    )
    assert path.read_text(encoding="utf-8") == ""
    write_known_hosts(path, [KnownHost(SOURCE, "srv-new", declared=[declared], pin=pin)])
    assert _matched(path, SOURCE) == [bare(declared)]
    write_known_hosts(path, [KnownHost(SOURCE, "srv-old", pin=pin)])
    assert _matched(path, SOURCE) == pin.keys
