from __future__ import absolute_import, division, print_function

__metaclass__ = type

import hashlib
import io
import json
import os
import random
import subprocess
import sys

import pytest

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import blocksync

# The CLI is exercised exactly as it runs on conversion hosts: as a script.
BLOCKSYNC = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        os.pardir,
        os.pardir,
        "plugins",
        "module_utils",
        "blocksync.py",
    )
)
# Allows running the CLI tests against an older interpreter (3.6+ support).
PYTHON = os.environ.get("BLOCKSYNC_TEST_PYTHON", sys.executable)
CHUNK = 65536
MIB = 1024 * 1024
SUMMARY_KEYS = {
    "ok",
    "chunks",
    "chunks_changed",
    "bytes_scanned",
    "bytes_changed",
    "bytes_transferred",
    "duration_s",
}


def rand_bytes(size, seed):
    if size == 0:
        return b""
    return random.Random(seed).getrandbits(8 * size).to_bytes(size, "little")


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)
    return str(path)


def read(path):
    with open(path, "rb") as f:
        return f.read()


def zero_file(path, size):
    with open(path, "wb") as f:
        f.truncate(size)
    return str(path)


def sender_cmd(src, chunk_size=CHUNK, workers=2):
    cmd = [PYTHON, BLOCKSYNC, "send", "--device", src, "--workers", str(workers)]
    if chunk_size is not None:
        cmd += ["--chunk-size", str(chunk_size)]
    return cmd


def receive(
    dst,
    src,
    chunk_size=CHUNK,
    workers=2,
    sender_chunk_size="same",
    assume_zero=False,
    sender_prefix=(),
):
    if sender_chunk_size == "same":
        sender_chunk_size = chunk_size
    cmd = [
        PYTHON,
        BLOCKSYNC,
        "receive",
        "--device",
        dst,
        "--workers",
        str(workers),
        "--progress-interval",
        "0",
    ]
    if chunk_size is not None:
        cmd += ["--chunk-size", str(chunk_size)]
    if assume_zero:
        cmd.append("--assume-zero")
    cmd.append("--")
    cmd += list(sender_prefix) + sender_cmd(src, sender_chunk_size, workers)
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False
    )
    lines = proc.stdout.decode("utf-8").strip().splitlines()
    summary = json.loads(lines[-1]) if lines else None
    return proc.returncode, summary, proc.stderr.decode("utf-8", "replace")


def progress_events(stderr):
    events = []
    for line in stderr.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "progress":
            events.append(event)
    return events


def test_identical_devices_transfer_nothing(tmp_path):
    data = rand_bytes(16 * CHUNK, 1)
    src = write(tmp_path / "src", data)
    dst = write(tmp_path / "dst", data)

    rc, summary, err = receive(dst, src)

    assert rc == 0, err
    assert summary["ok"] is True
    assert summary["chunks"] == 16
    assert summary["chunks_changed"] == 0
    assert summary["bytes_changed"] == 0
    assert summary["bytes_transferred"] == 0
    assert summary["bytes_scanned"] == 16 * CHUNK
    assert read(dst) == data


def test_changed_chunks_only_are_sent(tmp_path):
    data = rand_bytes(16 * CHUNK, 2)
    stale = bytearray(data)
    for index in (1, 7, 15):
        stale[index * CHUNK + 10] ^= 0xFF
    src = write(tmp_path / "src", data)
    dst = write(tmp_path / "dst", bytes(stale))

    rc, summary, err = receive(dst, src)

    assert rc == 0, err
    assert summary["chunks"] == 16
    assert summary["chunks_changed"] == 3
    assert summary["bytes_changed"] == 3 * CHUNK
    assert summary["bytes_transferred"] == 3 * CHUNK
    assert read(dst) == data


def test_zero_chunks_use_zero_frames(tmp_path):
    data = bytearray(rand_bytes(16 * CHUNK, 3))
    data[4 * CHUNK:8 * CHUNK] = bytes(4 * CHUNK)  # zeroed region at the source
    stale = bytearray(data)
    stale[4 * CHUNK:8 * CHUNK] = rand_bytes(4 * CHUNK, 4)  # dirty at the destination
    stale[9 * CHUNK] ^= 0x01  # plus one ordinary data change
    src = write(tmp_path / "src", bytes(data))
    dst = write(tmp_path / "dst", bytes(stale))

    rc, summary, err = receive(dst, src)

    assert rc == 0, err
    assert summary["chunks_changed"] == 5
    assert summary["bytes_changed"] == 5 * CHUNK
    # Zero frames carry no payload: only the data chunk counts as transferred.
    assert summary["bytes_transferred"] == CHUNK
    assert read(dst) == bytes(data)


def test_last_partial_chunk(tmp_path):
    size = 10 * MIB + 123
    data = rand_bytes(size, 5)
    src = write(tmp_path / "src", data)
    dst = zero_file(tmp_path / "dst", size)

    # Default chunk size (4 MiB): 4 MiB + 4 MiB + (2 MiB + 123 B).
    rc, summary, err = receive(dst, src, chunk_size=None, sender_chunk_size=None)

    assert rc == 0, err
    assert summary["chunks"] == 3
    assert summary["chunks_changed"] == 3
    assert summary["bytes_transferred"] == size
    assert read(dst) == data

    rc, summary, err = receive(dst, src, chunk_size=None, sender_chunk_size=None)
    assert rc == 0, err
    assert summary["chunks_changed"] == 0


def test_dest_larger_than_source_ok(tmp_path):
    data = rand_bytes(8 * CHUNK + 100, 6)
    original = rand_bytes(12 * CHUNK, 7)
    src = write(tmp_path / "src", data)
    dst = write(tmp_path / "dst", original)

    rc, summary, err = receive(dst, src)

    assert rc == 0, err
    assert summary["chunks"] == 9
    assert summary["bytes_scanned"] == len(data)
    result = read(dst)
    assert len(result) == len(original)
    assert result[:len(data)] == data
    assert result[len(data):] == original[len(data):]


def test_dest_smaller_exits_4(tmp_path):
    src = write(tmp_path / "src", rand_bytes(8 * CHUNK, 8))
    original = rand_bytes(4 * CHUNK, 9)
    dst = write(tmp_path / "dst", original)

    rc, summary, err = receive(dst, src)

    assert rc == 4, err
    assert summary["ok"] is False
    assert read(dst) == original


def test_chunk_size_mismatch_exits_3(tmp_path):
    src = write(tmp_path / "src", rand_bytes(8 * CHUNK, 10))
    original = rand_bytes(8 * CHUNK, 11)
    dst = write(tmp_path / "dst", original)

    rc, summary, err = receive(dst, src, chunk_size=CHUNK, sender_chunk_size=2 * CHUNK)

    assert rc == 3, err
    assert summary["ok"] is False
    assert read(dst) == original


def test_assume_zero_full_copy(tmp_path):
    data = bytearray(rand_bytes(16 * CHUNK, 12))
    zero_chunks = (0, 3, 5, 10)
    for index in zero_chunks:
        data[index * CHUNK:(index + 1) * CHUNK] = bytes(CHUNK)
    src = write(tmp_path / "src", bytes(data))
    dst = zero_file(tmp_path / "dst", len(data))

    rc, summary, err = receive(dst, src, assume_zero=True)

    assert rc == 0, err
    assert summary["chunks"] == 16
    assert summary["chunks_changed"] == 16 - len(zero_chunks)
    assert summary["bytes_transferred"] == (16 - len(zero_chunks)) * CHUNK
    assert read(dst) == bytes(data)


def test_workers_1_and_4_equivalent(tmp_path):
    data = bytearray(rand_bytes(32 * CHUNK + 4321, 13))
    data[8 * CHUNK:10 * CHUNK] = bytes(2 * CHUNK)
    stale = bytearray(rand_bytes(len(data), 14))
    stale[20 * CHUNK:24 * CHUNK] = data[20 * CHUNK:24 * CHUNK]
    src = write(tmp_path / "src", bytes(data))
    dst1 = write(tmp_path / "dst1", bytes(stale))
    dst4 = write(tmp_path / "dst4", bytes(stale))

    rc1, summary1, err1 = receive(dst1, src, workers=1)
    rc4, summary4, err4 = receive(dst4, src, workers=4)

    assert rc1 == 0, err1
    assert rc4 == 0, err4
    summary1.pop("duration_s")
    summary4.pop("duration_s")
    assert summary1 == summary4
    assert read(dst1) == read(dst4) == bytes(data)


def test_corrupted_frame_detected_exits_3(tmp_path):
    # The wrapper sits between the sender and the receiver and flips one
    # byte inside the payload of the first data frame:
    # HELLO (17 B) + frame header (13 B) + 100 B into the payload.
    wrapper = tmp_path / "flip.py"
    wrapper.write_text(
        "import os, subprocess, sys\n"
        "flip_at = int(sys.argv[1])\n"
        "proc = subprocess.Popen(sys.argv[2:], stdout=subprocess.PIPE)\n"
        "out = sys.stdout.buffer\n"
        "pos = 0\n"
        "while True:\n"
        "    buf = os.read(proc.stdout.fileno(), 65536)\n"
        "    if not buf:\n"
        "        break\n"
        "    if pos <= flip_at < pos + len(buf):\n"
        "        buf = bytearray(buf)\n"
        "        buf[flip_at - pos] ^= 0xFF\n"
        "        buf = bytes(buf)\n"
        "    pos += len(buf)\n"
        "    out.write(buf)\n"
        "    out.flush()\n"
        "sys.exit(proc.wait())\n"
    )
    src = write(tmp_path / "src", rand_bytes(8 * CHUNK, 15))
    dst = zero_file(tmp_path / "dst", 8 * CHUNK)

    rc, summary, err = receive(
        dst, src, sender_prefix=(PYTHON, str(wrapper), str(17 + 13 + 100))
    )

    assert rc == 3, err
    assert summary["ok"] is False
    assert "manifest" in summary["error"]


def test_progress_lines_and_summary_format(tmp_path):
    data = rand_bytes(16 * CHUNK + 7, 16)
    src = write(tmp_path / "src", data)
    dst = zero_file(tmp_path / "dst", len(data))

    rc, summary, err = receive(dst, src)

    assert rc == 0, err
    assert set(summary) == SUMMARY_KEYS
    assert isinstance(summary["duration_s"], float)
    events = progress_events(err)
    assert events, err
    for event in events:
        assert set(event) == {"event", "bytes_done", "bytes_total", "pct"}
        assert event["bytes_total"] == len(data)
        assert 0 <= event["bytes_done"] <= len(data)
    done = [event["bytes_done"] for event in events]
    assert done == sorted(done)
    assert events[-1]["bytes_done"] == len(data)
    assert events[-1]["pct"] == 100.0


def test_empty_source_device(tmp_path):
    src = write(tmp_path / "src", b"")
    dst = write(tmp_path / "dst", b"keep")

    rc, summary, err = receive(dst, src)

    assert rc == 0, err
    assert summary["chunks"] == 0
    assert summary["bytes_scanned"] == 0
    assert read(dst) == b"keep"


def test_receive_without_sender_command_exits_2(tmp_path):
    dst = zero_file(tmp_path / "dst", CHUNK)
    proc = subprocess.run(
        [PYTHON, BLOCKSYNC, "receive", "--device", dst],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 2


def test_unreadable_source_device_exits_2(tmp_path):
    dst = zero_file(tmp_path / "dst", CHUNK)

    rc, summary, err = receive(dst, str(tmp_path / "missing"))

    assert rc == 2, err
    assert summary["ok"] is False


def test_hash_prints_manifest_digest_and_size(tmp_path):
    data = rand_bytes(5 * CHUNK + 99, 17)
    src = write(tmp_path / "src", data)

    proc = subprocess.run(
        [PYTHON, BLOCKSYNC, "hash", "--device", src, "--chunk-size", str(CHUNK)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    digests = [
        hashlib.blake2b(data[i:i + CHUNK], digest_size=16).digest()
        for i in range(0, len(data), CHUNK)
    ]
    expected = hashlib.blake2b(b"".join(digests), digest_size=16).hexdigest()
    assert proc.stdout.decode("utf-8").split() == [expected, str(len(data))]


def test_help_works():
    proc = subprocess.run(
        [PYTHON, BLOCKSYNC, "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    out = proc.stdout.decode("utf-8")
    for command in ("send", "receive", "hash"):
        assert command in out


def test_digest_helpers(tmp_path):
    data = rand_bytes(3 * CHUNK, 18)
    assert blocksync.DEFAULT_CHUNK_SIZE == 4194304
    assert blocksync.chunk_digest(data) == hashlib.blake2b(data, digest_size=16).digest()
    assert blocksync.zero_digest(123) == blocksync.chunk_digest(bytes(123))
    digests = [blocksync.chunk_digest(data[i:i + CHUNK]) for i in range(0, len(data), CHUNK)]
    assert blocksync.manifest_digest(iter(digests)) == hashlib.blake2b(
        b"".join(digests), digest_size=16
    ).digest()
    assert blocksync.device_size(write(tmp_path / "f", data)) == len(data)


def test_run_sender_in_process_against_zero_manifest(tmp_path):
    data = bytearray(rand_bytes(4 * CHUNK + 10, 19))
    data[CHUNK:2 * CHUNK] = bytes(CHUNK)
    src = write(tmp_path / "src", bytes(data))
    lengths = [CHUNK, CHUNK, CHUNK, CHUNK, 10]
    inp = io.BytesIO(b"".join(blocksync.zero_digest(n) for n in lengths))
    out = io.BytesIO()

    stats = blocksync.run_sender(src, CHUNK, 2, inp, out)

    assert stats.chunks == 5
    assert stats.chunks_changed == 4
    assert stats.bytes_transferred == 3 * CHUNK + 10
    stream = out.getvalue()
    assert stream[:4] == b"SMBS"
    assert stream[4] == 1
    assert int.from_bytes(stream[5:9], "big") == CHUNK
    assert int.from_bytes(stream[9:17], "big") == len(data)
    assert stream[17:18] == b"D"
    assert stream[-41:-40] == b"E"
    assert stream[-16:] == blocksync.manifest_digest(
        blocksync.chunk_digest(bytes(data[i:i + CHUNK])) for i in range(0, len(data), CHUNK)
    )


def test_run_receiver_in_process(tmp_path):
    data = rand_bytes(6 * CHUNK + 1, 20)
    src = write(tmp_path / "src", data)
    dst = zero_file(tmp_path / "dst", len(data))
    seen = []

    summary = blocksync.run_receiver(
        dst,
        CHUNK,
        2,
        sender_cmd(src),
        progress_cb=lambda done, total: seen.append((done, total)),
    )

    assert summary.ok is True
    assert summary.chunks == 7
    assert summary.chunks_changed == 7
    assert summary.bytes_transferred == len(data)
    assert set(summary.to_dict()) == SUMMARY_KEYS
    assert seen and seen[-1] == (len(data), len(data))
    assert read(dst) == data


def test_run_receiver_raises_with_exit_code_on_small_destination(tmp_path):
    src = write(tmp_path / "src", rand_bytes(4 * CHUNK, 21))
    dst = zero_file(tmp_path / "dst", CHUNK)

    with pytest.raises(blocksync.BlocksyncError) as excinfo:
        blocksync.run_receiver(dst, CHUNK, 1, sender_cmd(src))

    assert excinfo.value.exit_code == 4


# --- Fix round 1: bounded cleanup, process-group kill, error mapping, FIPS ---

import shlex  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


class Boom(Exception):
    pass


def run_receiver_bounded(timeout, *args, **kwargs):
    """run_receiver in a daemon thread: a hang is reported, not inherited."""
    outcome = {}

    def target():
        try:
            outcome["summary"] = blocksync.run_receiver(*args, **kwargs)
        except BaseException as err:  # pylint: disable=broad-except
            outcome["error"] = err

    thread = threading.Thread(target=target)
    thread.daemon = True
    started = time.monotonic()
    thread.start()
    thread.join(timeout)
    outcome["elapsed"] = time.monotonic() - started
    outcome["hung"] = thread.is_alive()
    return outcome


@pytest.fixture
def reap(tmp_path):
    """Kill whatever a test left running that mentions its tmp_path."""
    yield
    subprocess.run(["pkill", "-9", "-f", str(tmp_path)], check=False)


def shell_sender(script, src, chunk_size=CHUNK):
    send = " ".join(
        shlex.quote(word)
        for word in (PYTHON, BLOCKSYNC, "send", "--device", src,
                     "--chunk-size", str(chunk_size), "--workers", "2")
    )
    return ["/bin/sh", "-c", script.replace("SEND", send)]


def test_abort_does_not_hang_when_the_sender_outlives_its_parent(tmp_path, reap):
    # More chunks than digests fit in a pipe: the digest thread blocks in a
    # write while the shell (the direct child) survives the Python sender.
    chunk = 4096
    size = 5000 * chunk
    src = write(tmp_path / "src", rand_bytes(size, 30))
    dst = zero_file(tmp_path / "dst", size)
    calls = []

    def progress(done, total):
        calls.append(done)
        if len(calls) > 50:
            time.sleep(1.0)  # meanwhile the digest thread fills the pipe and blocks
            raise Boom("progress callback failed")

    outcome = run_receiver_bounded(
        60, dst, chunk, 2, shell_sender("SEND; true", src, chunk),
        progress_cb=progress, progress_interval=0,
    )

    assert not outcome["hung"], "run_receiver did not return after the abort"
    assert isinstance(outcome.get("error"), Boom)


def test_verified_transfer_is_not_held_up_by_a_stderr_holder(tmp_path, reap, monkeypatch):
    monkeypatch.setattr(blocksync, "_STREAM_GRACE", 0.5, raising=False)
    monkeypatch.setattr(blocksync, "_KILL_GRACE", 1.0, raising=False)
    data = rand_bytes(8 * CHUNK, 31)
    src = write(tmp_path / "src", data)
    dst = zero_file(tmp_path / "dst", len(data))
    # A descendant in the sender's process group keeps only stderr open.
    sender = shell_sender("sleep 25 </dev/null >/dev/null & exec SEND", src)

    outcome = run_receiver_bounded(20, dst, CHUNK, 2, sender)

    assert not outcome["hung"]
    assert outcome["summary"].ok is True, outcome
    assert outcome["elapsed"] < 10
    assert read(dst) == data


def test_trailing_eof_check_is_bounded(tmp_path, reap, monkeypatch):
    monkeypatch.setattr(blocksync, "_STREAM_GRACE", 0.5, raising=False)
    monkeypatch.setattr(blocksync, "_KILL_GRACE", 1.0, raising=False)
    data = rand_bytes(8 * CHUNK, 32)
    src = write(tmp_path / "src", data)
    dst = zero_file(tmp_path / "dst", len(data))
    # A descendant keeps the frame stream (stdout) open after the end frame.
    sender = shell_sender("sleep 25 </dev/null 2>/dev/null & exec SEND", src)

    outcome = run_receiver_bounded(20, dst, CHUNK, 2, sender)

    assert not outcome["hung"]
    assert outcome["summary"].ok is True, outcome
    assert outcome["elapsed"] < 10


def test_stream_holder_outside_the_process_group_is_left_behind(tmp_path, reap, monkeypatch):
    monkeypatch.setattr(blocksync, "_STREAM_GRACE", 0.5, raising=False)
    monkeypatch.setattr(blocksync, "_KILL_GRACE", 1.0, raising=False)
    data = rand_bytes(8 * CHUNK, 33)
    src = write(tmp_path / "src", data)
    dst = zero_file(tmp_path / "dst", len(data))
    # Like a daemonised ssh ControlPersist master: a new session (beyond the
    # reach of the group kill) that keeps stderr open.
    holder = "%s -c 'import os, time; os.setsid(); time.sleep(15)' %s" % (
        shlex.quote(PYTHON), shlex.quote(str(tmp_path)),
    )
    sender = shell_sender(holder + " </dev/null >/dev/null & exec SEND", src)

    outcome = run_receiver_bounded(12, dst, CHUNK, 2, sender)

    assert not outcome["hung"]
    assert outcome["summary"].ok is True, outcome
    assert outcome["elapsed"] < 8
    assert read(dst) == data


def test_progress_callback_error_in_the_stderr_pump_is_raised(tmp_path, reap):
    data = rand_bytes(64 * CHUNK, 34)
    src = write(tmp_path / "src", data)
    dst = zero_file(tmp_path / "dst", len(data))
    caller = {}

    def progress(done, total):
        if threading.get_ident() != caller["ident"]:
            raise Boom("callback failed in the stderr pump")

    def call():
        caller["ident"] = threading.get_ident()
        return blocksync.run_receiver(
            dst, CHUNK, 2, sender_cmd(src), progress_cb=progress, progress_interval=0
        )

    outcome = {}

    def target():
        try:
            outcome["summary"] = call()
        except BaseException as err:  # pylint: disable=broad-except
            outcome["error"] = err

    thread = threading.Thread(target=target)
    thread.daemon = True
    thread.start()
    thread.join(30)

    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), Boom)


def test_sender_killed_by_a_signal_mid_stream_exits_2(tmp_path, reap):
    src = write(tmp_path / "src", rand_bytes(64 * CHUNK, 35))
    original = rand_bytes(64 * CHUNK, 36)
    dst = write(tmp_path / "dst", original)
    # Forwards part of the frame stream unbuffered, then dies of SIGKILL
    # (like the OOM killer would).
    killer = tmp_path / "die_midstream.py"
    killer.write_text(
        "import os, signal, subprocess, sys\n"
        "limit = int(sys.argv[1])\n"
        "proc = subprocess.Popen(sys.argv[2:], stdout=subprocess.PIPE)\n"
        "sent = 0\n"
        "while sent < limit:\n"
        "    buf = os.read(proc.stdout.fileno(), min(65536, limit - sent))\n"
        "    if not buf:\n"
        "        break\n"
        "    os.write(1, buf)\n"
        "    sent += len(buf)\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )

    rc, summary, err = receive(
        dst, src, sender_prefix=(PYTHON, str(killer), "200000")
    )

    assert rc == 2, err
    assert summary["ok"] is False
    assert "killed by signal 9" in summary["error"]


def test_broken_pipe_after_the_end_frame_is_a_protocol_error(tmp_path):
    session = blocksync._ReceiveSession(0, CHUNK, 1, ["true"], False, None, 0)
    session.digest_thread = threading.Thread(target=lambda: None)
    session.digest_thread.start()
    session.digest_error = BrokenPipeError(32, "Broken pipe")

    with pytest.raises(blocksync.ProtocolError):
        session._check_digest_thread()


def test_assume_zero_with_a_larger_destination(tmp_path):
    data = bytearray(rand_bytes(8 * CHUNK + 5, 37))
    data[2 * CHUNK:3 * CHUNK] = bytes(CHUNK)
    tail = rand_bytes(3 * CHUNK, 38)
    src = write(tmp_path / "src", bytes(data))
    dst = write(tmp_path / "dst", bytes(len(data)) + tail)

    rc, summary, err = receive(dst, src, assume_zero=True)

    assert rc == 0, err
    assert summary["chunks"] == 9 and summary["chunks_changed"] == 8
    result = read(dst)
    assert result[:len(data)] == bytes(data)
    assert result[len(data):] == tail


def test_pwrite_returning_zero_raises(tmp_path, monkeypatch):
    fd = os.open(str(tmp_path / "f"), os.O_RDWR | os.O_CREAT, 0o600)
    monkeypatch.setattr(blocksync.os, "pwrite", lambda fd, data, offset: 0)
    outcome = {}

    def target():
        try:
            blocksync._pwrite_full(fd, b"data", 0)
        except BaseException as err:  # pylint: disable=broad-except
            outcome["error"] = err

    thread = threading.Thread(target=target)
    thread.daemon = True
    thread.start()
    thread.join(10)
    os.close(fd)

    assert not thread.is_alive(), "_pwrite_full spins when pwrite returns 0"
    assert isinstance(outcome.get("error"), blocksync.BlocksyncError)


def test_digests_are_fips_tolerant(monkeypatch):
    data = rand_bytes(3 * CHUNK + 1, 39)
    expected_chunk = hashlib.blake2b(data, digest_size=16).digest()
    expected_zero = hashlib.blake2b(bytes(1000), digest_size=16).digest()
    expected_manifest = hashlib.blake2b(expected_chunk * 3, digest_size=16).digest()
    real = hashlib.blake2b
    seen = []

    def without_kwarg(*args, **kwargs):  # Python < 3.9 without RHEL's patch
        seen.append(kwargs)
        if "usedforsecurity" in kwargs:
            raise TypeError("'usedforsecurity' is an invalid keyword argument")
        return real(*args, **kwargs)

    def fips(*args, **kwargs):  # FIPS mode: BLAKE2 only when not for security
        seen.append(kwargs)
        if kwargs.get("usedforsecurity", True):
            raise ValueError("[digital envelope routines] unsupported")
        return real(*args, **kwargs)

    for fake_blake2b in (without_kwarg, fips):
        monkeypatch.setattr(blocksync.hashlib, "blake2b", fake_blake2b)
        monkeypatch.setattr(blocksync, "_BLAKE2B_KWARGS", None, raising=False)
        monkeypatch.setattr(blocksync, "_ZERO_DIGESTS", {})
        assert blocksync.chunk_digest(data) == expected_chunk
        assert blocksync.zero_digest(1000) == expected_zero
        assert blocksync.manifest_digest([expected_chunk] * 3) == expected_manifest
    assert any("usedforsecurity" not in kwargs for kwargs in seen)
    assert any(kwargs.get("usedforsecurity") is False for kwargs in seen)


def _mutate(data, rng, pattern, chunk_size):
    """Return a mutated copy of ``data``: random extents, whole chunks, or sparse zero runs."""
    out = bytearray(data)
    size = len(out)
    if size == 0:
        return bytes(out)
    if pattern == "extents":
        for _ in range(rng.randint(1, 8)):
            start = rng.randrange(size)
            length = min(size - start, rng.randint(1, max(1, size // 4)))
            out[start : start + length] = rand_bytes(length, rng.randrange(1 << 30))
    elif pattern == "chunks":
        chunks = max(1, (size + chunk_size - 1) // chunk_size)
        for index in rng.sample(range(chunks), k=max(1, chunks // 3)):
            start = index * chunk_size
            length = min(chunk_size, size - start)
            out[start : start + length] = rand_bytes(length, rng.randrange(1 << 30))
    elif pattern == "zero-runs":
        for _ in range(rng.randint(1, 4)):
            start = rng.randrange(size)
            length = min(size - start, rng.randint(1, max(1, size // 3)))
            out[start : start + length] = b"\0" * length
    elif pattern == "tail":
        length = min(size, rng.randint(1, 4097))
        out[size - length :] = rand_bytes(length, 7)  # same length: the device size is unchanged
    assert len(out) == size
    return bytes(out)


@pytest.mark.parametrize("seed", range(12))
def test_randomized_engine_fuzz(tmp_path, seed):
    """QASuite D-02: random sizes (0 and 1 byte included), chunk sizes, mutation patterns and
    zero ratios; sender and receiver run as subprocesses; the destination must end
    byte-identical to the source on every iteration, with a self-consistent summary."""
    rng = random.Random(1000 + seed)
    chunk_size = rng.choice([4096, 65536, 131072, 1 * MIB, 4 * MIB])
    size = rng.choice([0, 1, chunk_size - 1, chunk_size, chunk_size + 1, rng.randint(0, 6 * MIB)])
    workers = rng.choice([1, 2, 4])
    zero_ratio = rng.choice([0.0, 0.3, 0.9])
    # the source: random data with zero runs sprinkled in according to the zero ratio
    source = bytearray(rand_bytes(size, seed))
    if size and zero_ratio:
        for _ in range(int(zero_ratio * 6)):
            start = rng.randrange(size)
            length = min(size - start, rng.randint(1, max(1, size // 2)))
            source[start : start + length] = b"\0" * length
    source = bytes(source)
    pattern = rng.choice(["extents", "chunks", "zero-runs", "tail", "identical"])
    destination = source if pattern == "identical" else _mutate(source, rng, pattern, chunk_size)
    src = write(tmp_path / "src", source)
    dst = write(tmp_path / "dst", destination)

    rc, summary, err = receive(dst, src, chunk_size=chunk_size, workers=workers)

    assert rc == 0, err
    assert read(dst) == source, "destination differs from the source (seed %d)" % seed
    expected_chunks = (size + chunk_size - 1) // chunk_size
    assert summary["ok"] is True and summary["chunks"] == expected_chunks
    assert summary["bytes_scanned"] == size
    assert 0 <= summary["chunks_changed"] <= expected_chunks
    assert summary["bytes_transferred"] <= summary["bytes_changed"] <= size
    if pattern == "identical":
        assert summary["chunks_changed"] == 0 and summary["bytes_transferred"] == 0

    # a second pass over now-identical devices moves nothing and leaves the data untouched
    rc, again, err = receive(dst, src, chunk_size=chunk_size, workers=workers)
    assert rc == 0, err
    assert again["chunks_changed"] == 0 and again["bytes_transferred"] == 0
    assert read(dst) == source


def test_hash_chunk_zero_fast_path_matches_hashing(tmp_path):
    """All-zero chunks take the cached zero digest; a chunk whose first 4 KiB are zero but
    which has data later is hashed normally (the prefix check is only a cheap filter)."""
    chunk = 64 * 1024
    zero = b"\0" * chunk
    late = b"\0" * 8192 + b"\x01" + b"\0" * (chunk - 8193)
    early = b"\x01" + b"\0" * (chunk - 1)
    tail = b"\0" * 100  # a short last chunk, all zero
    path = write(tmp_path / "dev", zero + late + early + tail)
    fd = os.open(path, os.O_RDONLY)
    try:
        size = 3 * chunk + 100
        chunks = [blocksync._hash_chunk(fd, i, chunk, size, False) for i in range(4)]
    finally:
        os.close(fd)
    assert chunks[0].digest == blocksync.zero_digest(chunk) == blocksync.chunk_digest(zero)
    assert chunks[1].digest == blocksync.chunk_digest(late) != blocksync.zero_digest(chunk)
    assert chunks[2].digest == blocksync.chunk_digest(early)
    assert chunks[3].length == 100 and chunks[3].digest == blocksync.zero_digest(100)
