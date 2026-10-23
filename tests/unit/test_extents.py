# Copyright: Seamless Migrate contributors
# Apache License 2.0 (see LICENSE)
"""The extent query of changed-extent tracking (Seamless Migrate SDD 6.4, 28.4, 28.5, 28.8; plan
task CE3).

``rbd diff`` between Cinder's base and current pass snapshots of a volume's RBD image, run on the
source conversion host through a real, pinned RemoteShell: an unprivileged sshd on 127.0.0.1
(local_sshd.py) whose sessions find a fake ``rbd`` first on their PATH. The fake records its
arguments, the files it was given and the directory holding them, then prints JSON, fails or hangs
as each test says. The Ceph configuration, keyring and key are made up here; no Ceph runs, and no
test skips: CI's runner has sshd.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import base64
import hashlib
import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import traceback

import pytest

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import extents
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import volume_common
from ansible_collections.os_migrate.os_migrate.tests.unit import local_sshd

M = 4 << 20  # blocksync's chunk, and RBD's default object: 4 MiB
GIB = 1 << 30
SIZE_GIB = 2
POOL = "volumes"
CLIENT = "seamless"
VOLUME_ID = "3f6c1a52-7d1e-4f0b-9a63-6c1d2e8b4a10"
NAME_ID = "9b2e7d40-15c8-4e3a-b1f6-0d7a5c3e9f21"  # another back-end object: a migration, a retype
BASE = "5d0f3b2a-8c41-4b6e-a7d9-2e1c6f4b8a30"  # the last completed pass's Cinder snapshot
CURRENT = "c8a4e1f7-2b39-4d5c-8e60-7f1a3b9d2c45"  # this pass's
SRC_ALIAS = volume_common.HOST_KEY_ALIASES["source"]
CONF = (
    "[global]\n"
    "fsid = 6f2a9c1e-0b7d-4c3a-9e58-1d4b7a2c6e90\n"
    "mon_host = 192.0.2.10:6789,192.0.2.11:6789\n"
)
KEYRING = '[client.%s]\n\tkey = %s\n\tcaps mon = "profile rbd"\n'

FAKE_RBD = r'''
# A fake rbd (tests/unit/test_extents.py): it records what it was given - its arguments, the digest
# and mode of each file --conf and --keyring name, and the directory holding them - then answers as
# plan.json beside it says: what it prints on its standard error and output, how long it sleeps
# before its output, whether it ignores SIGTERM, its exit status.
import hashlib
import json
import os
import signal
import stat
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ARGS = sys.argv[1:]


def option(flag):
    return ARGS[ARGS.index(flag) + 1] if flag in ARGS[:-1] else None


with open(os.path.join(HERE, "plan.json"), encoding="utf-8") as f:
    PLAN = json.load(f)
seen = {"argv": ARGS, "files": {}}
for flag in ("--conf", "--keyring"):
    path = option(flag)
    if path is None:
        continue
    try:
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        seen["files"][flag] = {
            "path": path, "sha256": digest, "mode": stat.S_IMODE(os.stat(path).st_mode)
        }
    except OSError as err:
        seen["files"][flag] = {"path": path, "error": str(err)}
if option("--conf") is not None:
    directory = os.path.dirname(option("--conf"))
    try:
        info = os.stat(directory)
        seen["directory"] = {
            "path": directory,
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "entries": sorted(os.listdir(directory)),
        }
    except OSError as err:
        seen["directory"] = {"path": directory, "error": str(err)}
with open(os.path.join(HERE, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps(seen) + "\n")
if PLAN.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
for stream in PLAN.get("print_keyring", ()):
    # as Ceph prints a key it cannot parse: "error setting modifier for [...] type=key val=<key>"
    with open(option("--keyring"), encoding="utf-8") as f:
        getattr(sys, stream).write(f.read())
sys.stderr.write(PLAN.get("stderr", ""))
sys.stderr.flush()
sys.stdout.flush()
time.sleep(PLAN.get("sleep", 0))
sys.stdout.write(PLAN.get("stdout", ""))
sys.exit(PLAN.get("status", 0))
'''


class PathSshd(local_sshd.LocalSshd):
    """LocalSshd whose sessions run with ``path`` as their PATH (sshd_config's SetEnv), so that
    the fake rbd comes first."""

    def __init__(self, path):
        self.session_path = path
        super(PathSshd, self).__init__()

    def _config(self, port, penalties):
        config = super(PathSshd, self)._config(port, penalties)
        with open(config, "a", encoding="ascii") as f:
            f.write("SetEnv PATH=%s\n" % self.session_path)
        return config


def shell_class(port):
    """RemoteShell reaching ``port`` of 127.0.0.1. A conversion host listens on 22, which the
    shell never names: the test adds ssh's Port option after the shell's own."""

    class Shell(volume_common.RemoteShell):
        def _default_options(self):
            return super(Shell, self)._default_options() + ["-o", "Port=%d" % port]

    return Shell


class ConversionHost:
    """The source conversion host of the tests: a real sshd on 127.0.0.1 whose sessions find the
    fake rbd first on their PATH; what the fake answers (``answer``) and saw (``calls``); the
    host's temporary directory, where each query makes its own (``extents.REMOTE_TMP``)."""

    def __init__(self, root):
        self.root = root
        self.tmp = None
        bin_dir = os.path.join(root, "bin")
        os.mkdir(bin_dir)
        fake = os.path.join(root, "fake_rbd.py")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE_RBD)
        rbd = os.path.join(bin_dir, "rbd")
        with open(rbd, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nexec %s %s "$@"\n' % (shlex.quote(sys.executable), shlex.quote(fake)))
        os.chmod(rbd, 0o755)
        self.sshd = PathSshd(bin_dir + ":/usr/local/bin:/usr/bin:/bin")
        self.shell_class = shell_class(self.sshd.port)

    def reset(self, tmp):
        """A test's start: a fresh temporary directory, no call seen, an answer of no extent."""
        os.mkdir(tmp)
        self.tmp = tmp
        self.answer(stdout="[]")
        with open(os.path.join(self.root, "calls.jsonl"), "w", encoding="utf-8"):
            pass

    def answer(self, **plan):
        """What the fake rbd does when it runs next: ``stdout``, ``stderr``, ``status``,
        ``sleep``, ``ignore_term``, ``print_keyring`` (the streams it prints the keyring on)."""
        with open(os.path.join(self.root, "plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f)

    def calls(self):
        """What each run of the fake rbd was given, in order."""
        with open(os.path.join(self.root, "calls.jsonl"), encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def leftovers(self):
        """What is left in the host's temporary directory."""
        return sorted(os.listdir(self.tmp))

    def stop(self):
        self.sshd.stop()


def write_0600(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(text)
    return path


class Run:
    """A run's Ceph files (SDD 28.4, plan task CE7): its ceph.conf and keyring, 0600, with a key
    made here as ``ceph auth get-key`` prints one, and its known_hosts, which pins the test sshd's
    key under the source alias."""

    def __init__(self, host, directory):
        os.mkdir(directory, 0o700)
        self.host = host
        self.key = base64.b64encode(b"\x01\x00" + os.urandom(26)).decode("ascii")
        self.conf = write_0600(os.path.join(directory, "ceph.conf"), CONF)
        self.keyring = write_0600(
            os.path.join(directory, "ceph.client.%s.keyring" % CLIENT), KEYRING % (CLIENT, self.key)
        )
        self.known_hosts = local_sshd.write_known_hosts(
            os.path.join(directory, "known_hosts"), [(SRC_ALIAS, self.host.sshd.host_keys["ed25519"])]
        )

    def shell(self, known_hosts=None, cls=None):
        """The module's pinned shell to the source conversion host."""
        return (cls or self.host.shell_class)(
            "127.0.0.1",
            self.host.sshd.user,
            self.host.sshd.client_key,
            known_hosts=known_hosts or self.known_hosts,
            side="source",
            server="src-conv",
        )

    def query(self, shell=None, **arguments):
        """query_extents of the test volume, ``arguments`` overriding."""
        given = dict(
            ceph_conf=self.conf,
            ceph_keyring=self.keyring,
            ceph_client=CLIENT,
            pool=POOL,
            volume_id=VOLUME_ID,
            name_id=NAME_ID,
            size_gib=SIZE_GIB,
            base_snapshot_id=BASE,
            snapshot_id=CURRENT,
        )
        given.update(arguments)
        return extents.query_extents(shell or self.shell(), **given)


@pytest.fixture(scope="module")
def conversion_host(tmp_path_factory):
    assert local_sshd.SSHD, "needs an sshd binary (openssh-server), as CI's runner has"
    host = ConversionHost(str(tmp_path_factory.mktemp("conversion-host")))
    try:
        yield host
    finally:
        host.stop()


@pytest.fixture
def host(conversion_host, tmp_path, monkeypatch):
    conversion_host.reset(str(tmp_path / "host-tmp"))
    monkeypatch.setattr(extents, "REMOTE_TMP", conversion_host.tmp)
    return conversion_host


@pytest.fixture
def run(host, tmp_path):
    return Run(host, str(tmp_path / "run"))


class CommandLines:
    """Every ssh and scp command line RemoteShell runs, recorded as it runs."""

    def __init__(self, monkeypatch):
        self.argvs = []
        for name in ("check_output", "call"):
            monkeypatch.setattr(
                volume_common.subprocess, name, self._recording(getattr(subprocess, name))
            )

    def _recording(self, run):
        def recorded(args, *rest, **kwargs):
            self.argvs.append([str(arg) for arg in args])
            return run(args, *rest, **kwargs)

        return recorded

    def remote(self):
        """The words of each ssh command line, as the host's shell splits them."""
        words = []
        for argv in self.argvs:
            if argv[0] == "ssh":
                target = next(i for i, word in enumerate(argv) if word.endswith("@127.0.0.1"))
                words.append(shlex.split(" ".join(argv[target + 1:])))
        return words


def rbd_json(*listed):
    """rbd diff's JSON of ``(offset, length[, exists])`` extents, ``exists`` as the string rbd
    prints ("true" unless given)."""
    return json.dumps(
        [
            {"offset": e[0], "length": e[1], "exists": "true" if e[2:] in ((), (True,)) else "false"}
            for e in listed
        ]
    )


def expected_argv(directory, name_id=NAME_ID):
    """rbd's arguments for the test volume, its Ceph files in ``directory``."""
    return [
        "--id", CLIENT,
        "--conf", directory + "/ceph.conf",
        "--keyring", directory + "/keyring",
        "diff",
        "--from-snap", "snapshot-" + BASE,
        "--whole-object",
        "--format", "json",
        "%s/volume-%s@snapshot-%s" % (POOL, name_id, CURRENT),
    ]


def sha256_of(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def assert_failed(result):
    """A query that failed: the reason, never an exception, with its time and no extents."""
    assert set(result) == {"extents", "reason", "query_s"}, result
    assert result["extents"] is None and result["reason"] == extents.QUERY_FAILED == "query_failed"
    assert isinstance(result["query_s"], float) and result["query_s"] >= 0, result


def warnings_of(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def wait_for(condition, seconds=20):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "waited %d s in vain" % seconds
        time.sleep(0.05)


def test_the_query_diffs_the_base_and_the_current_snapshot_of_the_volume_image(
    host, run, caplog, monkeypatch
):
    """SDD 6.4, 28.4 (plan task CE3): on the source conversion host, through the module's pinned
    RemoteShell, rbd diffs the volume's image from the base pass's Cinder snapshot to the current
    pass's - each named snapshot-<its Cinder id> -, whole objects, as JSON, as the run's Ceph user.
    The query makes its directory 0700, copies the Ceph files there, runs rbd in sh -c under
    timeout (300 s by default) and removes the directory; every word reaches the host's shell
    quoted, as one word. The extents rbd lists come back with the query's time. A base that is the
    current snapshot, whose diff lists nothing, fails the query before anything runs."""
    caplog.set_level(logging.DEBUG)
    lines = CommandLines(monkeypatch)
    host.answer(stdout=rbd_json((0, M), (8 * M, M)))

    result = run.query()

    [call] = host.calls()
    directory = call["directory"]["path"]
    assert call["argv"] == expected_argv(directory)
    assert result == {"extents": [[0, M], [8 * M, M]], "reason": None, "query_s": result["query_s"]}
    assert isinstance(result["query_s"], float) and 0 <= result["query_s"] < 60
    assert lines.remote() == [
        ["mkdir", "-m", "0700", "--", directory],
        ["sh", "-c", extents.HOST_SCRIPT, "sh", directory, "-k", "10", "300", "rbd"]
        + expected_argv(directory),
        ["rm", "-rf", "--", directory],
    ]
    copies = [argv[-2:] for argv in lines.argvs if argv[0] == "scp"]
    target = "%s@127.0.0.1:%s/" % (host.sshd.user, directory)
    assert copies == [[run.conf, target + "ceph.conf"], [run.keyring, target + "keyring"]]
    assert any(
        VOLUME_ID in message and "2 extents" in message
        for message in (r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    )
    assert host.leftovers() == []

    del lines.argvs[:]
    assert_failed(run.query(base_snapshot_id=CURRENT))
    assert lines.argvs == [] and len(host.calls()) == 1
    assert any("is the current snapshot" in message for message in warnings_of(caplog))


def test_the_image_is_named_by_the_volumes_name_id(host, run):
    """SDD 6.4, 28.4: Cinder names a volume's RBD image volume-<name_id>. A migration or a retype
    gives a volume another back-end object, so that its name_id is no longer its id: the image is
    the name_id's, and the volume's id reaches rbd nowhere. A volume whose name_id is its own id -
    Cinder shows none, and the caller passes the id - names that image."""
    result = run.query(volume_id=VOLUME_ID, name_id=NAME_ID)

    assert result["reason"] is None and result["extents"] == []
    [call] = host.calls()
    assert call["argv"][-1] == "%s/volume-%s@snapshot-%s" % (POOL, NAME_ID, CURRENT)
    assert not [word for word in call["argv"] if VOLUME_ID in word]

    run.query(volume_id=VOLUME_ID, name_id=VOLUME_ID)
    assert host.calls()[-1]["argv"][-1] == "%s/volume-%s@snapshot-%s" % (POOL, VOLUME_ID, CURRENT)


def test_rbd_diff_json_becomes_sorted_merged_extents(host, run):
    """SDD 6.4, 28.5 (plan tasks CE2, CE3): rbd's list, in whatever order, with extents that repeat,
    overlap, hold one another or touch, becomes [offset, length] pairs sorted by offset, those that
    overlap or touch merged - what blocksync's --extents takes; extents with a gap between them stay
    apart, an extent inside a chunk stays as rbd listed it (blocksync lists the chunk it touches),
    and an empty list is a query that found no change, not a failure."""
    listed = [
        (60 * M, M),
        (0, M),
        (16 * M, 2 * M),
        (M, M),
        (0, M),
        (17 * M, 2 * M),
        (40 * M, 3 * M),
        (41 * M, M),
        (70 * M + 512, 1024),
    ]
    merged = [[0, 2 * M], [16 * M, 3 * M], [40 * M, 3 * M], [60 * M, M], [70 * M + 512, 1024]]
    assert extents.parse_rbd_diff(rbd_json(*listed), SIZE_GIB * GIB) == merged
    assert extents.parse_rbd_diff("[]", SIZE_GIB * GIB) == []

    host.answer(stdout=rbd_json(*listed))
    assert run.query()["extents"] == merged
    host.answer(stdout="[]\n")
    result = run.query()
    assert (result["extents"], result["reason"]) == ([], None)


def test_zero_extents_are_kept_like_data_extents(host, run):
    """SDD 6.4, 28.4: an extent rbd lists with exists false - zeroed, discarded or trimmed since the
    base - is kept like a data extent and merged with its neighbours: the read pass reads it like
    the others, and blocksync sends its zero frame; dropped, the destination would keep bytes the
    guest zeroed. rbd prints exists as the string "true" or "false"; JSON's booleans read alike."""
    listed = json.dumps(
        [
            {"offset": 0, "length": M, "exists": "false"},
            {"offset": M, "length": M, "exists": "true"},
            {"offset": 8 * M, "length": M, "exists": False},
            {"offset": 16 * M, "length": M, "exists": "false"},
            {"offset": 17 * M, "length": M, "exists": True},
        ]
    )
    kept = [[0, 2 * M], [8 * M, M], [16 * M, 2 * M]]
    assert extents.parse_rbd_diff(listed, SIZE_GIB * GIB) == kept

    host.answer(stdout=listed)
    result = run.query()
    assert (result["extents"], result["reason"]) == (kept, None)


def test_an_extent_beyond_the_volume_fails_the_query(host, run):
    """SDD 6.4, 28.5: an extent that ends beyond the volume's size - Cinder's, in GiB - is none of
    this volume's (a resize the checks missed, another image): the query fails with query_failed and
    the disk scans in full. One that ends at the volume's last byte is the volume's."""
    size = SIZE_GIB * GIB
    assert extents.parse_rbd_diff(rbd_json((size - M, M)), size) == [[size - M, M]]
    for listed in ([(size - M, M + 1)], [(size, M)], [(0, M), (size + 8 * M, M)]):
        with pytest.raises(extents.ExtentQueryError, match="beyond the volume"):
            extents.parse_rbd_diff(rbd_json(*listed), size)

    host.answer(stdout=rbd_json((0, M), (size - M, 2 * M)))
    assert_failed(run.query())
    assert run.query(size_gib=SIZE_GIB + 1)["extents"] == [[0, M], [size - M, 2 * M]]
    assert host.leftovers() == []


def test_a_failed_or_timed_out_query_is_a_reason_never_an_error(host, run, caplog, monkeypatch):
    """SDD 6.4, 28.5 (owner decision 10): whatever fails - rbd's exit, rbd missing, output that is
    not rbd diff's JSON list, an extent rbd could not have listed, rbd running past the timeout on
    the host, no answer from the host at all, ssh, a name rbd could misread - the query returns the
    reason query_failed with its time, logs the cause as a warning and raises nothing: tracking never
    fails a pass. The one exception is no failure of the query: a refused host key (SDD 6.6, 26.5)
    propagates, and nothing reaches the host."""
    caplog.set_level(logging.DEBUG)
    for output in (
        "rbd: diff error",
        "",
        '{"extents": []}',
        "[[0, 4194304]]",
        '[{"offset": 0, "length": 4194304}]',
        '[{"offset": -4194304, "length": 4194304, "exists": "true"}]',
        '[{"offset": 0, "length": 0, "exists": "true"}]',
        '[{"offset": 0, "length": 4194304.0, "exists": "true"}]',
        '[{"offset": "0", "length": 4194304, "exists": "true"}]',
        '[{"offset": true, "length": 4194304, "exists": "true"}]',
        '[{"offset": 0, "length": 4194304, "exists": "yes"}]',
        '[{"offset": 0, "length": 4194304, "exists": 1}]',
    ):
        with pytest.raises(extents.ExtentQueryError):
            extents.parse_rbd_diff(output, SIZE_GIB * GIB)

    for answer, arguments, cause in (
        (dict(status=2), {}, "rbd exited with status 2"),
        (dict(status=127), {}, "rbd could not run on the conversion host (status 127)"),
        (dict(stdout="rbd: diff error\n"), {}, "rbd's output is not JSON"),
        (dict(stdout='{"extents": []}'), {}, "not a list of extents"),
        (dict(stdout='[{"offset": 0, "length": 0, "exists": "true"}]'), {}, "extent 0 is not"),
        (dict(sleep=60), dict(timeout=1), "rbd did not answer within 1 s"),
    ):
        caplog.clear()
        host.answer(**answer)
        result = run.query(**arguments)
        assert_failed(result)
        assert [m for m in warnings_of(caplog) if cause in m and "query_failed" in m], (cause, caplog.text)
        if "timeout" in arguments:
            assert result["query_s"] >= arguments["timeout"]

    # no answer at all: rbd ignores timeout's SIGTERM, and the module stops waiting first
    with monkeypatch.context() as patched:
        patched.setattr(extents, "QUERY_GRACE_S", 0)
        patched.setattr(extents, "KILL_AFTER_S", 1)
        caplog.clear()
        host.answer(sleep=60, ignore_term=True)
        result = run.query(timeout=1)
    assert_failed(result)
    assert result["query_s"] >= 1
    assert [m for m in warnings_of(caplog) if "no answer from the conversion host within 1 s" in m]

    # ssh fails: nothing listens where the host should be
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    caplog.clear()
    assert_failed(run.query(shell=run.shell(cls=shell_class(port))))
    assert [m for m in warnings_of(caplog) if "ssh to the conversion host failed (status 255)" in m]

    # a word rbd could misread, a snapshot diffed with itself, no timeout, no size: nothing runs
    lines = CommandLines(monkeypatch)
    for arguments, cause in (
        (dict(pool="volumes/other"), "pool"),
        (dict(pool="-volumes"), "pool"),
        (dict(name_id="%s; rm -rf /" % NAME_ID), "name_id"),
        (dict(name_id=None), "name_id"),
        (dict(base_snapshot_id="snap@x"), "snapshot"),
        (dict(snapshot_id=""), "snapshot"),
        (dict(ceph_client="seamless admin"), "Ceph client"),
        (dict(ceph_client="c" * 65), "Ceph client"),
        (dict(base_snapshot_id=CURRENT), "is the current snapshot"),
        (dict(timeout=0), "timeout"),
        (dict(timeout=-1), "timeout"),
        (dict(size_gib=0), "size"),
        (dict(size_gib=None), "size"),
        (dict(ceph_keyring=""), "keyring"),
        (dict(ceph_conf=None), "ceph.conf"),
    ):
        caplog.clear()
        calls = len(host.calls())
        assert_failed(run.query(**arguments))
        assert lines.argvs == [] and len(host.calls()) == calls, arguments
        assert [m for m in warnings_of(caplog) if cause in m], (arguments, caplog.text)

    # the one exception: a host whose key is not the pinned one is refused, at once
    other = local_sshd.keygen(os.path.join(os.path.dirname(run.conf), "other"))
    refusing = local_sshd.write_known_hosts(
        os.path.join(os.path.dirname(run.conf), "other_known_hosts"), [(SRC_ALIAS, other)]
    )
    with pytest.raises(volume_common.HostKeyRefused):
        run.query(shell=run.shell(known_hosts=refusing))
    assert [argv[0] for argv in lines.argvs] == ["ssh"]
    assert host.leftovers() == []


def test_the_query_reads_only_the_runs_ceph_conf_and_keyring(host, run, tmp_path):
    """SDD 6.4, 28.4, 28.8: rbd is given the run's ceph.conf and keyring alone - --conf and
    --keyring, copies of the files the caller names, byte for byte, in the query's directory, which
    holds nothing else - and no other configuration or key: no /etc/ceph, no -c, -k, --keyfile,
    --key, --name or --cluster. Another run's files are that run's."""
    run.query()
    other = Run(host, str(tmp_path / "other-run"))
    other.query()

    for each, call in zip((run, other), host.calls()):
        directory = call["directory"]["path"]
        assert os.path.dirname(directory) == host.tmp
        assert call["files"] == {
            "--conf": {"path": directory + "/ceph.conf", "sha256": sha256_of(each.conf), "mode": 0o600},
            "--keyring": {
                "path": directory + "/keyring", "sha256": sha256_of(each.keyring), "mode": 0o600
            },
        }
        assert call["directory"]["entries"] == ["ceph.conf", "keyring"]
        assert [word for word in call["argv"] if word.startswith("-")] == [
            "--id", "--conf", "--keyring", "--from-snap", "--whole-object", "--format"
        ]
        assert not [word for word in call["argv"] if "/etc/ceph" in word]
    assert sha256_of(run.keyring) != sha256_of(other.keyring)


def test_the_ceph_files_live_in_a_0700_directory_removed_after_the_query(host, run):
    """SDD 6.4, 28.4, 28.8: each query copies the Ceph files into a fresh directory of the host's
    temporary directory, seamless-extents-<random>, 0700 and the SSH user's, the files readable by
    that user alone, and removes it once rbd answered: two queries, two directories, neither left."""
    host.answer(stdout=rbd_json((0, M)))

    results = [run.query(), run.query()]

    assert [r["extents"] for r in results] == [[[0, M]]] * 2
    directories = [call["directory"] for call in host.calls()]
    assert len({d["path"] for d in directories}) == 2
    for directory in directories:
        assert os.path.dirname(directory["path"]) == host.tmp
        assert os.path.basename(directory["path"]).startswith("seamless-extents-")
        assert (directory["mode"], directory["uid"]) == (0o700, os.getuid())
        assert not os.path.exists(directory["path"])
    for call in host.calls():
        assert [f["mode"] & 0o077 for f in call["files"].values()] == [0, 0]
    assert host.leftovers() == []


def test_the_ceph_files_are_removed_when_the_query_fails(host, run, monkeypatch):
    """SDD 6.4, 28.4, 28.8: the directory holding the Ceph files goes whatever becomes of the query:
    rbd failing, output that is not rbd diff's JSON, an extent beyond the volume, rbd killed at the
    timeout; no answer at all, where the module removes it before the host's shell can; a copy that
    failed, where rbd never ran; and a module killed during the query - a cancel, a step timeout -,
    where the host's shell removes it as rbd ends."""
    size = SIZE_GIB * GIB
    for answer, arguments in (
        (dict(status=1), {}),
        (dict(stdout="[{"), {}),
        (dict(stdout=rbd_json((size - M, 2 * M))), {}),
        (dict(sleep=60), dict(timeout=1)),
    ):
        host.answer(**answer)
        calls = len(host.calls())
        assert_failed(run.query(**arguments))
        [call] = host.calls()[calls:]
        assert not os.path.exists(call["directory"]["path"]), answer
        assert host.leftovers() == [], answer

    monkeypatch.setattr(extents, "QUERY_GRACE_S", 0)
    monkeypatch.setattr(extents, "KILL_AFTER_S", 1)
    host.answer(sleep=60, ignore_term=True)
    assert_failed(run.query(timeout=1))
    assert host.leftovers() == []  # the module's removal: rbd still runs on the host

    calls = len(host.calls())
    assert_failed(run.query(ceph_keyring=run.keyring + ".missing"))
    assert len(host.calls()) == calls and host.leftovers() == []

    host.answer(sleep=3, stdout="[]")
    directory = os.path.join(host.tmp, "seamless-extents-killed")
    os.mkdir(directory, 0o700)
    shutil.copy(run.conf, os.path.join(directory, "ceph.conf"))
    shutil.copy(run.keyring, os.path.join(directory, "keyring"))
    command = extents.host_command(
        directory,
        30,
        extents.diff_command(
            directory,
            ceph_client=CLIENT,
            pool=POOL,
            name_id=NAME_ID,
            base_snapshot_id=BASE,
            snapshot_id=CURRENT,
        ),
    )
    process = run.shell().cmd_sub(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        wait_for(lambda: len(host.calls()) > calls)
    finally:
        process.kill()
        process.wait()
    assert os.path.isdir(directory)  # rbd runs on, its module and ssh gone
    wait_for(lambda: not os.path.exists(directory))
    assert host.leftovers() == []


def test_no_log_line_or_result_holds_the_key(host, run, caplog, capfd, monkeypatch, tmp_path):
    """SDD 6.4, 28.8: the Ceph key stays in the keyring files - the run's, and the query's copy until
    its removal - whatever the query meets: rbd printing the keyring on its standard error as Ceph
    prints a key it cannot parse ("error setting modifier ... val=<key>"), or on its standard
    output; a query that succeeds, one that fails, one that times out; a refused host key. No log
    line at any level, no result, no exception, no ssh or scp command line, and nothing the module
    wrote to its standard output or error holds the key or the keyring's key line."""
    caplog.set_level(logging.DEBUG)
    lines = CommandLines(monkeypatch)
    results = []
    for answer, arguments in (
        (dict(stdout=rbd_json((0, M)), print_keyring=["stderr"]), {}),
        (dict(status=1, print_keyring=["stderr"]), {}),
        (dict(print_keyring=["stdout", "stderr"]), {}),
        (dict(sleep=60, print_keyring=["stderr"]), dict(timeout=1)),
    ):
        host.answer(**answer)
        results.append(run.query(**arguments))
    other = local_sshd.keygen(str(tmp_path / "other"))
    refusing = local_sshd.write_known_hosts(str(tmp_path / "refusing"), [(SRC_ALIAS, other)])
    with pytest.raises(volume_common.HostKeyRefused) as refused:
        run.query(shell=run.shell(known_hosts=refusing))
    out, err = capfd.readouterr()
    told = "".join(
        traceback.format_exception(type(refused.value), refused.value, refused.value.__traceback__)
    )
    texts = {
        "the results": json.dumps(results),
        "the refusal": told,
        "the module's standard output": out,
        "the module's standard error": err,
    }
    texts.update(("log line %d" % i, r.getMessage()) for i, r in enumerate(caplog.records))
    texts.update(("command line %d" % i, " ".join(argv)) for i, argv in enumerate(lines.argvs))
    caplog.clear()  # a failure's report prints no captured line, as no assertion prints a text

    assert sorted(name for name, text in texts.items() if run.key in text or "key = " in text) == []
    assert [r["reason"] for r in results] == [None, "query_failed", "query_failed", "query_failed"]
    assert len(host.calls()) == 4 and host.leftovers() == []
