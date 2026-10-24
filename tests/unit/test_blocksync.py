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
        # the caller's own updates are slow (64 frames take about 3 s), so a send_progress line
        # is always ahead of them
        time.sleep(0.05)

    # The sender reports progress at most once a second: on a fast transfer its lines race the
    # frames and the pump's update can find nothing new to report. Lines ahead of anything
    # applied, written once the header is read, make the pump call back from its own thread.
    ahead = shlex.quote(json.dumps(
        {"event": "send_progress", "bytes_done": len(data), "bytes_total": len(data)}
    ))
    emit = "printf '%s\\n' " + ahead + " >&2"
    sender = shell_sender(
        "(sleep 0.5; %s; sleep 0.5; %s; sleep 0.5; %s) & exec SEND" % (emit, emit, emit), src
    )

    def call():
        caller["ident"] = threading.get_ident()
        return blocksync.run_receiver(
            dst, CHUNK, 2, sender, progress_cb=progress, progress_interval=0
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
        seen.append(dict(kwargs))
        if kwargs.pop("usedforsecurity", True):
            raise ValueError("[digital envelope routines] unsupported")
        return real(*args, **kwargs)  # the kwarg is the fake's: Python < 3.9 refuses it

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
        for extent in range(rng.randint(1, 8)):
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
        for zero_run in range(rng.randint(1, 4)):
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
        for zero_run in range(int(zero_ratio * 6)):
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


# --- protocol guards on the receiver ---------------------------------------------------------
# A scripted sender speaks protocol v1 and breaks it in exactly one place. Every violation must
# end in ProtocolError (exit 3); the ones caught before a write must leave the destination as it
# was (data integrity, QASuite D-series; test review COLL-5).

FAKE_SENDER = r'''
import struct, sys
sys.path.insert(0, sys.argv[2])
import blocksync as b
scenario, chunk, size = sys.argv[1], int(sys.argv[3]), int(sys.argv[4])
count = b.chunk_count(size, chunk)
out, inp = sys.stdout.buffer, sys.stdin.buffer
magic, version = b.PROTOCOL_MAGIC, b.PROTOCOL_VERSION
if scenario == "bad_magic":
    magic = b"XXXX"
if scenario == "bad_version":
    version = 3
out.write(b._HELLO.pack(magic, version, chunk, size)); out.flush()
if scenario in ("bad_magic", "bad_version"):
    sys.exit(0)
theirs = [b._read_exact(inp, b.DIGEST_SIZE) for _ in range(count)]
payload = b"\x5a" * chunk
def frame(offset, length, data=True):
    out.write((b.FRAME_DATA if data else b.FRAME_ZERO) + b._FRAME_HEADER.pack(offset, length))
    if data:
        out.write(payload[:length])
changed = transferred = 0
if scenario == "misaligned":
    frame(1, chunk)
elif scenario == "beyond":
    frame(count * chunk, chunk)
elif scenario == "wrong_length":
    frame(0, chunk - 1)
elif scenario == "unknown_type":
    out.write(b"Q" + b._FRAME_HEADER.pack(0, chunk))
elif scenario == "out_of_order":
    frame(chunk, chunk); frame(0, chunk)
elif scenario == "end_mismatch":
    changed = 1
elif scenario == "lying_manifest":
    pass
manifest = b.manifest_digest(theirs)
if scenario == "lying_manifest":
    manifest = bytes(16)
out.write(b.FRAME_END + b._END.pack(count, changed, transferred, manifest))
if scenario == "trailing":
    out.write(b"!")
out.flush()
'''


@pytest.mark.parametrize(
    "scenario, message, untouched",
    [
        ("bad_magic", "bad protocol magic", True),
        ("bad_version", "unsupported protocol version 3", True),
        ("misaligned", "not chunk aligned", True),
        ("beyond", "beyond the source size", True),
        ("wrong_length", "has length", True),
        ("unknown_type", "unknown frame type", True),
        ("out_of_order", "out of order", False),
        ("end_mismatch", "end frame mismatch", True),
        ("lying_manifest", "manifest digest mismatch", True),
        ("trailing", "unexpected data after the end frame", True),
    ],
)
def test_receiver_rejects_protocol_violations(tmp_path, scenario, message, untouched):
    module_dir = os.path.dirname(blocksync.__file__)
    fake = tmp_path / "fake_sender.py"
    fake.write_text(FAKE_SENDER)
    original = rand_bytes(4 * CHUNK, 77)
    dst = write(tmp_path / "dst", original)
    sender = [PYTHON, str(fake), scenario, module_dir, str(CHUNK), str(4 * CHUNK)]

    with pytest.raises(blocksync.ProtocolError, match=message) as caught:
        blocksync.run_receiver(str(dst), CHUNK, 1, sender)

    assert caught.value.exit_code == blocksync.EXIT_PROTOCOL
    if untouched:
        assert read(dst) == original, "a rejected frame must not reach the destination"


# --- The device's identity (SDD 6.2: the RV12-03 hazard on the warm path) --------------------------


def test_a_sender_never_reads_a_device_another_volume_holds(tmp_path, monkeypatch):
    """SDD 6.2: the kernel gives a freed /dev/vdX path to the next disk attached. With
    --expect-serial the sender checks the virtio serial of the device it opened before it reads a
    byte: one another volume holds exits 3, and nothing is sent."""
    src = write(tmp_path / "src", rand_bytes(2 * CHUNK, 31))
    monkeypatch.setattr(blocksync, "device_serial", lambda fd: "vol-other-0123456789")
    out = io.BytesIO()

    with pytest.raises(blocksync.BlocksyncError) as excinfo:
        blocksync.run_sender(
            src, CHUNK, 1, io.BytesIO(), out, expect_serial="vol-mine-0123456789-tail"
        )

    assert excinfo.value.exit_code == 3 and "vol-other" in str(excinfo.value)
    assert out.getvalue() == b""


def test_a_receiver_never_writes_a_device_another_volume_holds(tmp_path, monkeypatch):
    """SDD 6.2: the receiver writes its device - one another volume took the path of would be
    written into. It checks the serial of the device it opened first, and exits 3."""
    src = write(tmp_path / "src", rand_bytes(2 * CHUNK, 32))
    dst = zero_file(tmp_path / "dst", 2 * CHUNK)
    monkeypatch.setattr(blocksync, "device_serial", lambda fd: "vol-other-0123456789")

    with pytest.raises(blocksync.BlocksyncError) as excinfo:
        blocksync.run_receiver(
            dst, CHUNK, 1, sender_cmd(src), expect_serial="vol-mine-0123456789-tail"
        )

    assert excinfo.value.exit_code == 3
    assert read(dst) == bytes(2 * CHUNK)  # not a byte written


def test_a_device_whose_serial_matches_is_synced(tmp_path, monkeypatch):
    data = rand_bytes(3 * CHUNK, 33)
    src = write(tmp_path / "src", data)
    dst = zero_file(tmp_path / "dst", len(data))
    monkeypatch.setattr(blocksync, "device_serial", lambda fd: "vol-mine-0123456789-")

    summary = blocksync.run_receiver(
        dst, CHUNK, 1, sender_cmd(src), expect_serial="vol-mine-0123456789-tail"
    )

    assert summary.ok and read(dst) == data


def test_a_device_without_a_serial_is_used_as_given(tmp_path):
    """SDD 6.2: a regular file, or a disk that is not virtio-blk, shows no serial: used as given -
    by the CLI, as the warm sync runs it."""
    data = rand_bytes(3 * CHUNK, 34)
    src = write(tmp_path / "src", data)
    dst = zero_file(tmp_path / "dst", len(data))
    cmd = [
        PYTHON, BLOCKSYNC, "receive", "--device", dst, "--chunk-size", str(CHUNK),
        "--progress-interval", "0", "--expect-serial", "dst-volume-id", "--",
        PYTHON, BLOCKSYNC, "send", "--device", src, "--chunk-size", str(CHUNK),
        "--expect-serial", "src-volume-id",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
                          check=False)

    assert proc.returncode == 0, proc.stderr
    assert read(dst) == data


@pytest.mark.parametrize(
    "content, serial",
    [(b"vol-mine-0123456789-", "vol-mine-0123456789-"), (b"", None), (None, None)],
    ids=["its serial", "an empty serial", "no serial file"],
)
def test_the_serial_read_is_the_opened_devices_own(tmp_path, monkeypatch, content, serial):
    """SDD 6.2: the serial comes from /sys/dev/block/<major>:<minor>/serial of the device the
    descriptor is open on - no path that another disk could take in between; a regular file has
    none."""
    import stat as stat_module
    import types

    sysfs = tmp_path / "sys-dev-block"
    (sysfs / "252:16").mkdir(parents=True)
    if content is not None:
        (sysfs / "252:16" / "serial").write_bytes(content)
    monkeypatch.setattr(blocksync, "SYS_DEV_BLOCK", str(sysfs))
    device = types.SimpleNamespace(st_mode=stat_module.S_IFBLK | 0o660, st_rdev=os.makedev(252, 16))
    regular = os.stat(write(tmp_path / "disk", b"x"))

    assert blocksync._serial_of(device) == serial
    assert blocksync._serial_of(regular) is None


# --- protocol v2 and the extent check (SDD 6.2, 28.5; plan task CE2) ------------------------------


def extents_file(tmp_path, extents, name="extents.json"):
    """The extents a pass names, as the warm sync writes them: JSON [offset, length] pairs."""
    path = tmp_path / name
    path.write_text(extents if isinstance(extents, str) else json.dumps(extents))
    return str(path)


def extent_sender(src, extents, mode, chunk_size=CHUNK, workers=2, prefix=()):
    return list(prefix) + sender_cmd(src, chunk_size, workers) + [
        "--extents", extents, "--extents-mode", mode]


def receive_from(dst, sender, chunk_size=CHUNK, workers=2, assume_zero=False):
    cmd = [PYTHON, BLOCKSYNC, "receive", "--device", dst, "--workers", str(workers),
           "--progress-interval", "0", "--chunk-size", str(chunk_size)]
    if assume_zero:
        cmd.append("--assume-zero")
    proc = subprocess.run(cmd + ["--"] + sender, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=120, check=False)
    lines = proc.stdout.decode("utf-8").strip().splitlines()
    return proc.returncode, (json.loads(lines[-1]) if lines else None), proc.stderr.decode(
        "utf-8", "replace")


def chunks_of(data, chunk=CHUNK):
    return [data[i:i + chunk] for i in range(0, len(data), chunk)]


def changed_pair(tmp_path, changed, count=8, tail=0, seed=11):
    """A source and a destination equal but for the chunks ``changed`` (indexes) of the source."""
    base = rand_bytes(count * CHUNK + tail, seed)
    source = bytearray(base)
    for index in changed:
        start = index * CHUNK
        end = min(start + CHUNK, len(source))
        source[start:end] = rand_bytes(end - start, 1000 + index)
    return write(tmp_path / "src", bytes(source)), write(tmp_path / "dst", base), bytes(source)


def test_extents_map_to_every_chunk_they_touch():
    """SDD 6.2, 28.5: the extents are sorted and merged, and each chunk an extent touches is
    listed once, in order - a byte at a chunk's last offset, an extent across a boundary, the last
    short chunk."""
    size = 4 * CHUNK + 100
    mapped = blocksync.extent_chunks
    assert mapped([], size, CHUNK) == []
    assert mapped([[0, 1]], size, CHUNK) == [0]
    assert mapped([[CHUNK - 1, 2]], size, CHUNK) == [0, 1]
    assert mapped([[CHUNK, CHUNK]], size, CHUNK) == [1]
    assert mapped([[100, 2 * CHUNK]], size, CHUNK) == [0, 1, 2]
    assert mapped([[3 * CHUNK, 10], [0, 5], [2, 4]], size, CHUNK) == [0, 3]
    assert mapped([[0, CHUNK], [CHUNK, 1]], size, CHUNK) == [0, 1]  # touching: merged
    assert mapped([[4 * CHUNK + 99, 1]], size, CHUNK) == [4]  # the short last chunk
    assert mapped([[0, size]], size, CHUNK) == [0, 1, 2, 3, 4]


@pytest.mark.parametrize(
    "extents, reason",
    [
        ([[-1, 10]], "negative"),
        ([[0, -5]], "negative"),
        ([[0, 0]], "empty"),
        ([[0]], "an [offset, length] pair"),
        ([[0, None]], "an [offset, length] pair"),
        ([[True, 4]], "an [offset, length] pair"),
        ([[0, 1.5]], "an [offset, length] pair"),
        ([[0, 4 * CHUNK + 1]], "beyond the source"),
        ([[2 ** 64, 1]], "beyond the source"),
        ({"offset": 0}, "a list"),
        ("not json", "not JSON"),
    ],
    ids=["negative offset", "negative length", "empty", "no length", "null length", "a bool",
         "a float", "beyond the source", "unbounded", "an object", "not json"],
)
def test_an_extent_that_is_negative_empty_or_unbounded_is_refused(tmp_path, extents, reason):
    """SDD 6.2, 28.5: an extent that is negative, empty, without a length or reaching beyond the
    source - and a file that is no list of pairs - is refused before the sender writes its
    HELLO: exit 2, nothing on stdout."""
    src = write(tmp_path / "src", rand_bytes(4 * CHUNK, 3))
    for mode in ("read", "check"):
        proc = subprocess.run(
            extent_sender(src, extents_file(tmp_path, extents), mode),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60, check=False)
        assert proc.returncode == 2, (mode, proc.stderr)
        assert proc.stdout == b""
        assert reason in proc.stderr.decode(), proc.stderr
    missing = subprocess.run(sender_cmd(src) + ["--extents-mode", "read"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=60, check=False)
    assert missing.returncode == 2 and missing.stdout == b""


def test_a_read_pass_sends_only_the_listed_chunks(tmp_path):
    """SDD 6.2, 28.5: a read pass names the listed chunks in its version 2 HELLO; the receiver
    sends their digests alone and the sender frames only those that differ - a change outside
    the extents is not looked at."""
    src, dst, source = changed_pair(tmp_path, changed=[2, 5, 7])
    before = read(dst)
    extents = extents_file(tmp_path, [[5 * CHUNK + 7, 3], [2 * CHUNK, CHUNK + 10]])  # 2, 3, 5

    rc, summary, err = receive_from(dst, extent_sender(src, extents, "read"))

    assert rc == 0, err
    after = chunks_of(read(dst))
    for index in range(8):
        expected = chunks_of(source)[index] if index in (2, 5) else chunks_of(before)[index]
        assert after[index] == expected, index
    assert summary["chunks"] == 3 and summary["chunks_changed"] == 2
    assert summary["bytes_transferred"] == 2 * CHUNK and summary["bytes_changed"] == 2 * CHUNK


PREAD_SPY = r"""
import os, runpy, sys
script, log = sys.argv[1], sys.argv[2]
real = os.pread
def pread(fd, length, offset):
    with open(log, "a") as f:
        f.write("%d %d\n" % (offset, length))
    return real(fd, length, offset)
os.pread = pread
sys.argv = [script] + sys.argv[3:]
runpy.run_path(script, run_name="__main__")
"""


def test_a_read_pass_reads_no_unlisted_chunk_on_either_side(tmp_path, monkeypatch):
    """SDD 6.2, 28.5: a read pass reads the listed chunks alone - the sender on the source, the
    receiver on the destination -, which is what removes the scan from the final pass."""
    src, dst, _source = changed_pair(tmp_path, changed=[1, 6])
    listed = [1, 4, 6]
    extents = extents_file(tmp_path, [[i * CHUNK, CHUNK] for i in listed])
    spy = tmp_path / "spy.py"
    spy.write_text(PREAD_SPY)
    sender_log = tmp_path / "sender.log"
    sender = [PYTHON, str(spy), BLOCKSYNC, str(sender_log)] + sender_cmd(src)[2:] + [
        "--extents", extents, "--extents-mode", "read"]
    receiver_reads = []
    real = blocksync.os.pread

    def pread(fd, length, offset):
        receiver_reads.append((offset, length))
        return real(fd, length, offset)

    monkeypatch.setattr(blocksync.os, "pread", pread)
    summary = blocksync.run_receiver(dst, CHUNK, 2, sender)
    monkeypatch.setattr(blocksync.os, "pread", real)

    assert summary.chunks == 3
    sender_reads = [tuple(int(v) for v in line.split()) for line in read(sender_log).decode().split("\n") if line]
    for reads in (sender_reads, receiver_reads):
        assert reads, "nothing was read"
        assert {offset // CHUNK for offset, _length in reads} == set(listed)


FAKE_V2_SENDER = r"""
import struct, sys
sys.path.insert(0, sys.argv[2])
import blocksync as b
scenario, chunk, size = sys.argv[1], int(sys.argv[3]), int(sys.argv[4])
listed = [int(i) for i in sys.argv[5].split(",")] if sys.argv[5] else []
out, inp = sys.stdout.buffer, sys.stdin.buffer
out.write(b._HELLO.pack(b.PROTOCOL_MAGIC, 2, chunk, size))
out.write(struct.pack(">Q", len(listed)) + b"".join(struct.pack(">Q", i) for i in listed))
out.flush()
theirs = [b._read_exact(inp, b.DIGEST_SIZE) for _ in listed]
payload = b"\x5a" * chunk
transferred = changed = 0
if scenario == "unlisted_frame":
    out.write(b.FRAME_DATA + b._FRAME_HEADER.pack(2 * chunk, chunk) + payload)
    changed, transferred = 1, chunk
manifest = b.extent_manifest(zip(listed, theirs))
if scenario == "lying_manifest":
    manifest = b.manifest_digest(theirs)  # v1's manifest: no indexes
out.write(b.FRAME_END + b._END.pack(len(listed), changed, transferred, manifest))
out.flush()
"""


def run_fake_v2(tmp_path, scenario, listed, size=4 * CHUNK, assume_zero=False):
    fake = tmp_path / "fake_v2_sender.py"
    fake.write_text(FAKE_V2_SENDER)
    original = rand_bytes(4 * CHUNK, 77)
    dst = write(tmp_path / "dst", original)
    sender = [PYTHON, str(fake), scenario, os.path.dirname(blocksync.__file__), str(CHUNK),
              str(size), ",".join(str(i) for i in listed)]
    return dst, original, sender


def test_a_read_pass_verifies_a_manifest_over_the_listed_chunks(tmp_path):
    """SDD 6.2, 28.5: a version 2 end frame carries a manifest over each listed chunk's index and
    digest, in order; the receiver recomputes it from its own digests and the frames it applied
    and refuses one that differs (exit 3) - v1's manifest, which names no index, too."""
    dst, original, sender = run_fake_v2(tmp_path, "honest", [0, 3])
    summary = blocksync.run_receiver(dst, CHUNK, 1, sender)
    assert (summary.chunks, summary.chunks_changed) == (2, 0)
    dst, original, sender = run_fake_v2(tmp_path, "lying_manifest", [0, 3])
    with pytest.raises(blocksync.ProtocolError, match="manifest digest mismatch") as caught:
        blocksync.run_receiver(dst, CHUNK, 1, sender)
    assert caught.value.exit_code == 3 and read(dst) == original
    digests = [blocksync.chunk_digest(b"a"), blocksync.chunk_digest(b"b")]
    expected = blocksync.new_hasher(b"\0" * 7 + b"\x01" + digests[0] + b"\0" * 7 + b"\x05"
                                    + digests[1]).digest()
    assert blocksync.extent_manifest(zip([1, 5], digests)) == expected


def test_a_read_pass_counts_only_the_listed_chunks_as_scanned(tmp_path):
    """SDD 28.5: bytes_scanned counts the listed chunks - the short last one with its own
    length -, not the source's size."""
    src, dst, _source = changed_pair(tmp_path, changed=[0], count=4, tail=100)
    extents = extents_file(tmp_path, [[0, 10], [4 * CHUNK + 50, 10]])  # chunks 0 and 4 (100 B)

    rc, summary, err = receive_from(dst, extent_sender(src, extents, "read"))

    assert rc == 0, err
    assert summary["bytes_scanned"] == CHUNK + 100
    assert summary["chunks"] == 2 and summary["chunks_changed"] == 1
    events = progress_events(err)
    assert events[-1]["bytes_total"] == CHUNK + 100 and events[-1]["pct"] == 100.0


def test_a_frame_for_an_unlisted_chunk_exits_3(tmp_path):
    """SDD 6.2: in a version 2 pass every frame is for a listed chunk, after its digest; one for
    another chunk is refused before it is written."""
    dst, original, sender = run_fake_v2(tmp_path, "unlisted_frame", [1, 3])
    with pytest.raises(blocksync.ProtocolError, match="chunk 2 is not listed") as caught:
        blocksync.run_receiver(dst, CHUNK, 1, sender)
    assert caught.value.exit_code == 3 and read(dst) == original


@pytest.mark.parametrize(
    "listed, message",
    [([3, 1], "strictly increasing"), ([1, 1], "strictly increasing"),
     ([0, 4], "beyond the source"), ([2 ** 63], "beyond the source")],
    ids=["out of order", "twice", "beyond", "far beyond"],
)
def test_listed_chunks_out_of_order_or_beyond_the_source_exit_3(tmp_path, listed, message):
    """SDD 6.2: the HELLO's chunk indexes are strictly increasing and within the source, or the
    receiver refuses them (exit 3) before it reads or writes a chunk."""
    dst, original, sender = run_fake_v2(tmp_path, "honest", listed)
    with pytest.raises(blocksync.ProtocolError, match=message) as caught:
        blocksync.run_receiver(dst, CHUNK, 1, sender)
    assert caught.value.exit_code == 3 and read(dst) == original


def test_the_receiver_accepts_version_1_and_version_2_hellos(tmp_path):
    """SDD 6.2: one receiver takes a version 1 pass - a full scan, as before - and a version 2
    read pass alike."""
    src, dst, source = changed_pair(tmp_path, changed=[1, 2])
    rc, summary, err = receive_from(
        dst, extent_sender(src, extents_file(tmp_path, [[CHUNK, CHUNK]]), "read"))
    assert rc == 0 and summary["chunks"] == 1, err
    rc, summary, err = receive_from(dst, sender_cmd(src))
    assert rc == 0 and summary["chunks"] == 8 and summary["chunks_changed"] == 1, err
    assert read(dst) == source


def test_assume_zero_is_refused_with_extents(tmp_path):
    """SDD 6.2, 28.5: --assume-zero, which skips the destination's read, is refused with a
    version 2 pass: exit 2, the destination untouched."""
    src, dst, _source = changed_pair(tmp_path, changed=[1])
    before = read(dst)
    rc, summary, err = receive_from(
        dst, extent_sender(src, extents_file(tmp_path, [[CHUNK, 1]]), "read"), assume_zero=True)
    assert rc == 2 and summary["ok"] is False
    assert "--assume-zero" in summary["error"]
    assert read(dst) == before


def test_a_check_pass_copies_what_a_full_scan_copies(tmp_path):
    """SDD 6.2, 28.5: a check pass is protocol v1 on the wire and a full scan: it leaves the
    destination equal to the source and reports what a plain pass reports, plus the extents'
    counts."""
    src, dst, source = changed_pair(tmp_path, changed=[0, 3, 6])
    plain_dst = write(tmp_path / "plain", read(dst))
    extents = extents_file(tmp_path, [[0, 10], [3 * CHUNK, 10], [6 * CHUNK, 10]])

    rc, checked, err = receive_from(dst, extent_sender(src, extents, "check"))
    assert rc == 0, err
    rc, plain, err = receive_from(plain_dst, sender_cmd(src))
    assert rc == 0, err

    assert read(dst) == read(plain_dst) == source
    for key in ("chunks", "chunks_changed", "bytes_scanned", "bytes_changed", "bytes_transferred"):
        assert checked[key] == plain[key], key
    assert (checked["extent_chunks"], checked["missed_chunks"]) == (3, 0)
    assert "extent_chunks" not in plain and "missed_chunks" not in plain


def test_a_check_pass_counts_changes_outside_the_extents(tmp_path):
    """SDD 28.5: the sender of a check pass counts the chunks whose digests differed that the
    extents do not list - a miss, which turns tracking off for the disk."""
    src, dst, source = changed_pair(tmp_path, changed=[1, 4, 6])
    extents = extents_file(tmp_path, [[CHUNK, 2 * CHUNK], [4 * CHUNK, 1]])  # 1, 2, 4

    rc, summary, err = receive_from(dst, extent_sender(src, extents, "check"))

    assert rc == 0, err
    assert summary["chunks_changed"] == 3
    assert (summary["extent_chunks"], summary["missed_chunks"]) == (3, 1)
    assert read(dst) == source


def test_extents_that_miss_a_write_leave_it_to_a_read_and_a_check_finds_it(tmp_path):
    """SDD 28.5: why a read pass needs a clean check first - extents that miss a write leave that
    chunk as it was, and the manifest, over the listed chunks alone, cannot see it; a check pass
    of the same extents finds the miss and copies it."""
    src, dst, source = changed_pair(tmp_path, changed=[2, 5])
    extents = extents_file(tmp_path, [[2 * CHUNK, CHUNK]])  # 5 is missed

    rc, summary, err = receive_from(dst, extent_sender(src, extents, "read"))
    assert rc == 0 and summary["ok"] is True, err
    assert chunks_of(read(dst))[5] != chunks_of(source)[5]

    rc, summary, err = receive_from(dst, extent_sender(src, extents, "check"))
    assert rc == 0, err
    assert summary["missed_chunks"] == 1
    assert read(dst) == source
