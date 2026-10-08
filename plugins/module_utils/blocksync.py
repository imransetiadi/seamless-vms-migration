# Copyright: Seamless Migrate contributors
# Apache License 2.0 (see LICENSE)
"""Block-level delta synchronisation for warm migrations (protocol v1).

This file is both an os-migrate ``module_utils`` module and a standalone
script that runs on the conversion hosts, so it depends on the Python 3.6+
standard library only::

    python3 blocksync.py send    --device PATH [--chunk-size N] [--workers W]
    python3 blocksync.py receive --device PATH [--chunk-size N] [--workers W]
                                 [--assume-zero] [--progress-interval S]
                                 -- SENDER_COMMAND [ARGS...]
    python3 blocksync.py hash    --device PATH [--chunk-size N]

The receiver runs next to the destination device and spawns the sender
(normally ``ssh <source host> sudo python3 blocksync.py send ...``) with
stdin/stdout pipes. Wire protocol v1, all integers big-endian:

1. sender -> receiver ``HELLO``: ``b"SMBS"``, ``u8 version=1``,
   ``u32 chunk_size``, ``u64 source_size``;
2. receiver -> sender: one 16-byte BLAKE2b digest per destination chunk,
   in chunk order (``--assume-zero`` sends the digest of an all-zero chunk
   without reading the device);
3. sender -> receiver, for every chunk whose digest differs:
   ``b"D" + u64 offset + u32 length + data`` or, for an all-zero source
   chunk, ``b"Z" + u64 offset + u32 length``;
4. sender -> receiver ``b"E" + u64 chunks + u64 chunks_changed +
   u64 bytes_transferred + 16-byte manifest digest`` (BLAKE2b-128 over the
   concatenated source chunk digests).

The receiver sends digests and applies frames on separate threads (one
direction blocking can never deadlock the other) and verifies the manifest
digest of the result before it fsyncs the device and prints its summary.

Exit codes: 0 ok, 2 usage or I/O error, 3 protocol or verification error,
4 destination smaller than the source.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import argparse
import collections
import hashlib
import json
import math
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

PROTOCOL_MAGIC = b"SMBS"
PROTOCOL_VERSION = 1
DEFAULT_CHUNK_SIZE = 4194304
DEFAULT_WORKERS = 4
DEFAULT_PROGRESS_INTERVAL = 1.0
DIGEST_SIZE = 16
MIN_CHUNK_SIZE = 4096
MAX_CHUNK_SIZE = 1 << 30
MAX_WORKERS = 64

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PROTOCOL = 3
EXIT_DEST_TOO_SMALL = 4

FRAME_DATA = b"D"
FRAME_ZERO = b"Z"
FRAME_END = b"E"

# Exit statuses of a failed sender that point at the environment (missing
# device or interpreter, SSH failure) rather than at the protocol.
_SENDER_ENVIRONMENT_FAILURES = (EXIT_USAGE, 126, 127, 255)
# Seconds the sender gets to exit after its end frame.
_SENDER_EXIT_TIMEOUT = 30
# Seconds a helper thread gets to finish with its pipe before the sender's
# process group is signalled or the pipe is left to the thread.
_STREAM_GRACE = 5.0
# Seconds between SIGTERM and SIGKILL of the sender's process group.
_KILL_GRACE = 5.0

_HELLO = struct.Struct(">4sBIQ")
_FRAME_HEADER = struct.Struct(">QI")
_END = struct.Struct(">QQQ16s")

_STDERR_LOCK = threading.Lock()


class BlocksyncError(Exception):
    """A failed transfer; ``exit_code`` is the matching CLI exit status."""

    exit_code = EXIT_USAGE

    def __init__(self, message, exit_code=None):
        super(BlocksyncError, self).__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class ProtocolError(BlocksyncError):
    exit_code = EXIT_PROTOCOL


class DestinationTooSmall(BlocksyncError):
    exit_code = EXIT_DEST_TOO_SMALL


SenderStats = collections.namedtuple(
    "SenderStats",
    "chunks chunks_changed bytes_scanned bytes_changed bytes_transferred "
    "duration_s manifest",
)


class ReceiverSummary(
    collections.namedtuple(
        "ReceiverSummary",
        "ok chunks chunks_changed bytes_scanned bytes_changed bytes_transferred "
        "duration_s",
    )
):
    """Result of a receive; ``to_dict()`` is the CLI's JSON summary line."""

    __slots__ = ()

    def to_dict(self):
        return collections.OrderedDict(
            [
                ("ok", self.ok),
                ("chunks", self.chunks),
                ("chunks_changed", self.chunks_changed),
                ("bytes_scanned", self.bytes_scanned),
                ("bytes_changed", self.bytes_changed),
                ("bytes_transferred", self.bytes_transferred),
                ("duration_s", float(self.duration_s)),
            ]
        )


_Chunk = collections.namedtuple("_Chunk", "index offset length digest data")

_ZERO_BUFFERS = {}
_ZERO_DIGESTS = {}


# On FIPS-enabled systems BLAKE2 is only available for non-security use,
# requested with usedforsecurity=False (Python >= 3.9, RHEL's 3.6 and 3.8).
_BLAKE2B_KWARGS = None


def _blake2b_kwargs():
    global _BLAKE2B_KWARGS  # pylint: disable=global-statement
    if _BLAKE2B_KWARGS is None:
        try:
            hashlib.blake2b(digest_size=DIGEST_SIZE, usedforsecurity=False)  # novermin
            _BLAKE2B_KWARGS = {"digest_size": DIGEST_SIZE, "usedforsecurity": False}
        except TypeError:
            _BLAKE2B_KWARGS = {"digest_size": DIGEST_SIZE}
    return _BLAKE2B_KWARGS


def new_hasher(data=b""):
    """A BLAKE2b-128 hasher (an integrity checksum, not a security control)."""
    return hashlib.blake2b(data, **_blake2b_kwargs())


def chunk_digest(data):
    """BLAKE2b-128 digest of one chunk."""
    return new_hasher(data).digest()


def _zeros(length):
    buf = _ZERO_BUFFERS.get(length)
    if buf is None:
        buf = bytes(length)
        if len(_ZERO_BUFFERS) < 8:
            _ZERO_BUFFERS[length] = buf
    return buf


def zero_digest(length):
    """Digest of an all-zero chunk of ``length`` bytes (cached)."""
    digest = _ZERO_DIGESTS.get(length)
    if digest is None:
        digest = chunk_digest(_zeros(length))
        _ZERO_DIGESTS[length] = digest
    return digest


def manifest_digest(digests):
    """BLAKE2b-128 over the concatenation of chunk digests, in chunk order."""
    manifest = new_hasher()
    for digest in digests:
        manifest.update(digest)
    return manifest.digest()


def chunk_count(size, chunk_size):
    return (size + chunk_size - 1) // chunk_size


def _chunk_length(index, size, chunk_size):
    return min(chunk_size, size - index * chunk_size)


def _fd_size(fd):
    # Works for regular files and block devices alike.
    return os.lseek(fd, 0, os.SEEK_END)


def device_size(path):
    """Size in bytes of a block device or regular file."""
    fd = os.open(path, os.O_RDONLY)
    try:
        return _fd_size(fd)
    finally:
        os.close(fd)


def _validate_chunk_size(chunk_size):
    if not MIN_CHUNK_SIZE <= int(chunk_size) <= MAX_CHUNK_SIZE:
        raise BlocksyncError(
            "chunk size must be between %d and %d bytes, got %s"
            % (MIN_CHUNK_SIZE, MAX_CHUNK_SIZE, chunk_size)
        )


def _validate_workers(workers):
    workers = int(workers)
    if not 1 <= workers <= MAX_WORKERS:
        raise BlocksyncError(
            "workers must be between 1 and %d, got %s" % (MAX_WORKERS, workers)
        )
    return workers


def _read_exact(stream, size):
    """Read ``size`` bytes unless the stream ends first."""
    parts = []
    remaining = size
    while remaining > 0:
        data = stream.read(remaining)
        if not data:
            break
        parts.append(data)
        remaining -= len(data)
    return b"".join(parts)


def _pread_full(fd, length, offset):
    data = os.pread(fd, length, offset)
    if len(data) == length:
        return data
    parts = [data]
    done = len(data)
    while done < length:
        more = os.pread(fd, length - done, offset + done)
        if not more:
            raise BlocksyncError(
                "short read at offset %d: the device shrank during the transfer"
                % (offset + done)
            )
        parts.append(more)
        done += len(more)
    return b"".join(parts)


def _pwrite_full(fd, data, offset):
    view = memoryview(data)
    while len(view):
        written = os.pwrite(fd, view, offset)
        if written <= 0:
            raise BlocksyncError(
                "no progress writing at offset %d: the device is full or failing" % offset
            )
        view = view[written:]
        offset += written


def _hash_chunk(fd, index, chunk_size, size, keep_data):
    offset = index * chunk_size
    length = min(chunk_size, size - offset)
    data = _pread_full(fd, length, offset)
    return _Chunk(index, offset, length, chunk_digest(data), data if keep_data else None)


def _scan_chunks(fd, size, chunk_size, workers, keep_data=False, stop=None):
    """Yield every chunk of the device in order.

    With ``workers > 1`` chunks are read and hashed ahead by a thread pool
    (``os.pread`` and hashlib release the GIL); at most ``2 * workers``
    chunks are in flight, which bounds memory to ``2 * workers * chunk_size``.
    """
    count = chunk_count(size, chunk_size)
    if workers <= 1:
        for index in range(count):
            if stop is not None and stop.is_set():
                return
            yield _hash_chunk(fd, index, chunk_size, size, keep_data)
        return

    pool = ThreadPoolExecutor(max_workers=workers)
    pending = collections.deque()
    next_index = 0
    try:
        while next_index < count and len(pending) < 2 * workers:
            pending.append(
                pool.submit(_hash_chunk, fd, next_index, chunk_size, size, keep_data)
            )
            next_index += 1
        while pending:
            if stop is not None and stop.is_set():
                return
            chunk = pending.popleft().result()
            if next_index < count:
                pending.append(
                    pool.submit(_hash_chunk, fd, next_index, chunk_size, size, keep_data)
                )
                next_index += 1
            yield chunk
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True)


def _pct(done, total):
    if not total:
        return 100.0
    return math.floor(10000.0 * done / total) / 100.0


class _ProgressReporter:
    """Thread-safe, monotonic and rate-limited progress callback.

    Intermediate updates never reach ``total``: 100 % is reported only by the
    final (forced) update once the transfer has been verified.
    """

    def __init__(self, total, callback, interval):
        self.total = total
        self.callback = callback
        self.interval = interval
        self._lock = threading.Lock()
        self._done = 0
        self._last_emit = None

    def update(self, done, force=False):
        if self.callback is None:
            return
        with self._lock:
            if force:
                done = self.total
            else:
                done = min(max(done, self._done), max(self.total - 1, 0))
                if done == self._done and self._last_emit is not None:
                    return
            self._done = done
            now = time.monotonic()
            if (
                not force
                and self._last_emit is not None
                and now - self._last_emit < self.interval
            ):
                return
            self._last_emit = now
            self.callback(done, self.total)


def _emit_stderr(text):
    with _STDERR_LOCK:
        try:
            sys.stderr.write(text + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass


def _log(message):
    _emit_stderr("blocksync: " + message)


def _json_line(payload):
    return json.dumps(payload, separators=(",", ":"))


def _parse_send_progress(line):
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if isinstance(event, dict) and event.get("event") == "send_progress":
        try:
            return int(event["bytes_done"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def run_sender(
    device,
    chunk_size,
    workers,
    inp,
    out,
    progress_cb=None,
    progress_interval=DEFAULT_PROGRESS_INTERVAL,
):
    """Sender side of protocol v1.

    Reads the receiver's digests from ``inp`` and writes frames to ``out``
    (binary streams). Returns ``SenderStats``.
    """
    started = time.monotonic()
    _validate_chunk_size(chunk_size)
    workers = _validate_workers(workers)
    fd = os.open(device, os.O_RDONLY)
    try:
        size = _fd_size(fd)
        count = chunk_count(size, chunk_size)
        out.write(_HELLO.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION, chunk_size, size))
        out.flush()
        reporter = _ProgressReporter(size, progress_cb, progress_interval)
        manifest = new_hasher()
        changed = bytes_changed = transferred = 0
        for chunk in _scan_chunks(fd, size, chunk_size, workers, keep_data=True):
            theirs = _read_exact(inp, DIGEST_SIZE)
            if len(theirs) != DIGEST_SIZE:
                raise ProtocolError(
                    "the receiver's digest stream ended after %d of %d chunks"
                    % (chunk.index, count)
                )
            manifest.update(chunk.digest)
            if theirs != chunk.digest:
                changed += 1
                bytes_changed += chunk.length
                if chunk.digest == zero_digest(chunk.length) and chunk.data == _zeros(
                    chunk.length
                ):
                    out.write(FRAME_ZERO + _FRAME_HEADER.pack(chunk.offset, chunk.length))
                else:
                    out.write(FRAME_DATA + _FRAME_HEADER.pack(chunk.offset, chunk.length))
                    out.write(chunk.data)
                    transferred += chunk.length
            reporter.update(chunk.offset + chunk.length)
        digest = manifest.digest()
        out.write(FRAME_END + _END.pack(count, changed, transferred, digest))
        out.flush()
        reporter.update(size, force=True)
    finally:
        os.close(fd)
    return SenderStats(
        count,
        changed,
        size,
        bytes_changed,
        transferred,
        round(time.monotonic() - started, 3),
        digest,
    )


class _ReceiveSession:
    """One receive: owns the sender process and the helper threads.

    The sender runs in a new session, so its whole process tree can be
    signalled as a process group. Cleanup is bounded: a buffered pipe is
    never closed while a helper thread may be blocked on it (close() would
    wait for that thread), and a pipe still held open by a process outside
    the sender's group is left to its daemon thread.
    """

    def __init__(
        self, fd, chunk_size, workers, sender_cmd, assume_zero, progress_cb, interval
    ):
        self.fd = fd
        self.chunk_size = chunk_size
        self.workers = workers
        self.sender_cmd = list(sender_cmd)
        self.assume_zero = assume_zero
        self.progress_cb = progress_cb
        self.interval = interval
        self.proc = None
        self.reporter = None
        self.digests = []
        self.sent = 0
        self.digest_error = None
        self.callback_error = None
        self.trailing_data = False
        self.stop = threading.Event()
        self.digest_thread = None
        self.stderr_thread = None
        self.drain_thread = None

    def run(self):
        started = time.monotonic()
        dest_size = _fd_size(self.fd)
        try:
            self.proc = subprocess.Popen(
                self.sender_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as err:
            raise BlocksyncError(
                "cannot start the sender %r: %s" % (self.sender_cmd[0], err)
            )
        try:
            self.stderr_thread = self._start(self._pump_stderr)
            source_size = self._read_hello(dest_size)
            count = chunk_count(source_size, self.chunk_size)
            self.digests = [None] * count
            self.reporter = _ProgressReporter(source_size, self.progress_cb, self.interval)
            self.reporter.update(0)
            self.digest_thread = self._start(self._send_digests, source_size, count)
            changed, bytes_changed, transferred = self._apply_frames(source_size, count)
            os.fsync(self.fd)
            self._finish_sender()
            self._raise_callback_error()
            self.reporter.update(source_size, force=True)
        except BaseException:
            self._abort()
            raise
        return ReceiverSummary(
            True,
            count,
            changed,
            source_size,
            bytes_changed,
            transferred,
            round(time.monotonic() - started, 3),
        )

    @staticmethod
    def _start(target, *args):
        thread = threading.Thread(target=target, args=args)
        thread.daemon = True
        thread.start()
        return thread

    def _read_hello(self, dest_size):
        hello = _read_exact(self.proc.stdout, _HELLO.size)
        if len(hello) != _HELLO.size:
            self._raise_sender_failure("the sender closed the stream before HELLO")
        magic, version, chunk_size, source_size = _HELLO.unpack(hello)
        if magic != PROTOCOL_MAGIC:
            raise ProtocolError("bad protocol magic %r" % (magic,))
        if version != PROTOCOL_VERSION:
            raise ProtocolError("unsupported protocol version %d" % version)
        if chunk_size != self.chunk_size:
            raise ProtocolError(
                "chunk size mismatch: sender %d, receiver %d"
                % (chunk_size, self.chunk_size)
            )
        if dest_size < source_size:
            raise DestinationTooSmall(
                "destination is smaller than the source (%d < %d bytes)"
                % (dest_size, source_size)
            )
        return source_size

    def _send_digests(self, source_size, count):
        out = self.proc.stdin
        try:
            if self.assume_zero:
                batch = []
                for index in range(count):
                    if self.stop.is_set():
                        return
                    digest = zero_digest(_chunk_length(index, source_size, self.chunk_size))
                    self.digests[index] = digest
                    batch.append(digest)
                    if len(batch) == 4096 or index == count - 1:
                        self.sent = index + 1
                        out.write(b"".join(batch))
                        out.flush()
                        batch = []
            else:
                for chunk in _scan_chunks(
                    self.fd, source_size, self.chunk_size, self.workers, stop=self.stop
                ):
                    self.digests[chunk.index] = chunk.digest
                    # Counted before it is written: the sender may answer
                    # before this thread runs again.
                    self.sent = chunk.index + 1
                    out.write(chunk.digest)
                    out.flush()
        except BaseException as err:  # pylint: disable=broad-except
            if not self.stop.is_set():
                self.digest_error = err
        finally:
            _close_quietly(out)

    def _pump_stderr(self):
        """Forward sender logs; turn its send_progress lines into progress."""
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                done = _parse_send_progress(line)
                if done is None:
                    _emit_stderr(line)
                elif self.reporter is not None and self.callback_error is None:
                    try:
                        self.reporter.update(done)
                    except Exception as err:  # pylint: disable=broad-except
                        # The caller's progress callback failed: the main
                        # thread raises it, this one keeps draining stderr
                        # (a full pipe would block the sender).
                        self.callback_error = err
        except (OSError, ValueError):
            pass

    def _drain_stdout(self):
        """Wait for the end of the frame stream after the end frame."""
        try:
            if self.proc.stdout.read(1):
                self.trailing_data = True
        except (OSError, ValueError):
            pass

    def _raise_callback_error(self):
        if self.callback_error is not None:
            raise self.callback_error

    def _apply_frames(self, source_size, count):
        stream = self.proc.stdout
        changed = bytes_changed = transferred = 0
        last_index = -1
        while True:
            kind = stream.read(1)
            if not kind:
                self._raise_sender_failure("the sender stream ended without an end frame")
            if kind == FRAME_END:
                break
            if kind not in (FRAME_DATA, FRAME_ZERO):
                raise ProtocolError("unknown frame type %r" % (kind,))
            header = _read_exact(stream, _FRAME_HEADER.size)
            if len(header) != _FRAME_HEADER.size:
                self._raise_sender_failure("truncated frame header")
            offset, length = _FRAME_HEADER.unpack(header)
            index = self._check_frame(offset, length, last_index, count, source_size)
            last_index = index
            if kind == FRAME_DATA:
                payload = _read_exact(stream, length)
                if len(payload) != length:
                    self._raise_sender_failure("truncated data frame")
                _pwrite_full(self.fd, payload, offset)
                self.digests[index] = chunk_digest(payload)
                transferred += length
            else:
                _pwrite_full(self.fd, _zeros(length), offset)
                self.digests[index] = zero_digest(length)
            changed += 1
            bytes_changed += length
            self.reporter.update(offset + length)
            self._raise_callback_error()

        end = _read_exact(stream, _END.size)
        if len(end) != _END.size:
            self._raise_sender_failure("truncated end frame")
        chunks, chunks_changed, bytes_transferred, their_manifest = _END.unpack(end)
        if (chunks, chunks_changed, bytes_transferred) != (count, changed, transferred):
            raise ProtocolError(
                "end frame mismatch: sender reports %d chunks/%d changed/%d bytes, "
                "receiver saw %d/%d/%d"
                % (chunks, chunks_changed, bytes_transferred, count, changed, transferred)
            )
        self._check_digest_thread()
        if self.sent != count or any(digest is None for digest in self.digests):
            raise ProtocolError("the receiver did not send every digest")
        ours = manifest_digest(self.digests)
        if ours != their_manifest:
            raise ProtocolError(
                "manifest digest mismatch: source %s, destination %s"
                % (_hex(their_manifest), _hex(ours))
            )
        return changed, bytes_changed, transferred

    def _check_digest_thread(self):
        """After a valid end frame every digest has been read by the sender."""
        self.digest_thread.join(_STREAM_GRACE)
        error = self.digest_error
        if self.digest_thread.is_alive() or isinstance(error, BrokenPipeError):
            raise ProtocolError("the sender ended before reading every digest")
        if error is not None:
            raise error

    def _check_frame(self, offset, length, last_index, count, source_size):
        if offset % self.chunk_size:
            raise ProtocolError("frame offset %d is not chunk aligned" % offset)
        index = offset // self.chunk_size
        if index >= count:
            raise ProtocolError("frame offset %d is beyond the source size" % offset)
        if index <= last_index:
            raise ProtocolError("frame for chunk %d is out of order" % index)
        if index >= self.sent:
            raise ProtocolError("frame for chunk %d arrived before its digest" % index)
        expected = _chunk_length(index, source_size, self.chunk_size)
        if length != expected:
            raise ProtocolError(
                "frame for chunk %d has length %d, expected %d" % (index, length, expected)
            )
        return index

    def _raise_sender_failure(self, message):
        try:
            rc = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            rc = None
        error = self.digest_error
        if error is not None and not isinstance(error, BrokenPipeError):
            raise error
        if rc is not None and rc < 0:
            # Killed by a signal (e.g. the OOM killer): not a protocol issue.
            raise BlocksyncError(
                "%s (the sender was killed by signal %d)" % (message, -rc),
                exit_code=EXIT_USAGE,
            )
        if rc is not None and rc != 0:
            code = EXIT_USAGE if rc in _SENDER_ENVIRONMENT_FAILURES else EXIT_PROTOCOL
            raise BlocksyncError(
                "%s (the sender exited with status %d)" % (message, rc), exit_code=code
            )
        raise ProtocolError(message)

    def _kill_sender(self):
        """SIGTERM, then SIGKILL, the sender's whole process group."""
        _signal_group(self.proc, signal.SIGTERM)
        try:
            self.proc.wait(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass
        _signal_group(self.proc, signal.SIGKILL)
        try:
            self.proc.wait(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            _log("warning: the sender (pid %d) survived SIGKILL" % self.proc.pid)

    def _release(self, thread, stream, name):
        """Close ``stream`` once ``thread`` (its only user) is done with it."""
        if thread is not None:
            thread.join(_STREAM_GRACE)
            if thread.is_alive():
                _log(
                    "warning: the sender's %s is held open by a process outside "
                    "its process group; leaving it to a background thread" % name
                )
                return
        _close_quietly(stream)

    def _finish_sender(self):
        """Reap the sender after a verified transfer, in bounded time.

        The frame stream must end right after the end frame: a helper thread
        drains it so that descendants of the sender keeping stdout or stderr
        open cannot block the receiver; they are stopped with the sender's
        process group.
        """
        proc = self.proc
        try:
            rc = proc.wait(timeout=_SENDER_EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            _log("warning: the sender did not exit after a verified transfer; stopping it")
            self._kill_sender()
            rc = proc.poll()
        self.drain_thread = self._start(self._drain_stdout)
        for thread in (self.drain_thread, self.stderr_thread):
            thread.join(_STREAM_GRACE)
        if self.drain_thread.is_alive() or self.stderr_thread.is_alive():
            self._kill_sender()
        self._release(self.drain_thread, proc.stdout, "stdout")
        self._release(self.stderr_thread, proc.stderr, "stderr")
        _close_quietly(proc.stdin)
        if self.trailing_data:
            raise ProtocolError("unexpected data after the end frame")
        if rc != 0:
            _log("warning: the sender exited with status %s after a verified transfer" % rc)

    def _abort(self):
        """Error path: stop the sender's process tree and release the pipes."""
        self.stop.set()
        proc = self.proc
        if proc is None:
            return
        if self.drain_thread is None:
            # Only this thread reads stdout, so it can be closed right away:
            # a sender blocked on writing frames then fails with EPIPE.
            _close_quietly(proc.stdout)
        self._kill_sender()
        self._release(self.drain_thread, proc.stdout, "stdout")
        self._release(self.digest_thread, proc.stdin, "stdin")
        self._release(self.stderr_thread, proc.stderr, "stderr")


def _signal_group(proc, sig):
    """Signal the process group of ``proc`` (started in a new session)."""
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
    except OSError:
        # Not allowed to signal the whole group: at least the direct child.
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _close_quietly(stream):
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _hex(data):
    return bytes(data).hex()


def run_receiver(
    device,
    chunk_size,
    workers,
    sender_cmd,
    assume_zero=False,
    progress_cb=None,
    progress_interval=DEFAULT_PROGRESS_INTERVAL,
):
    """Receiver side of protocol v1.

    Spawns ``sender_cmd`` (an argv list), synchronises ``device`` with the
    sender's device and returns a ``ReceiverSummary``. Raises
    ``BlocksyncError`` (or ``OSError`` for local I/O failures).
    ``progress_cb(bytes_done, bytes_total)`` is called at most every
    ``progress_interval`` seconds plus once at 100 %.
    """
    _validate_chunk_size(chunk_size)
    workers = _validate_workers(workers)
    if not sender_cmd:
        raise BlocksyncError("a sender command is required")
    fd = os.open(device, os.O_RDWR)
    try:
        session = _ReceiveSession(
            fd,
            chunk_size,
            workers,
            sender_cmd,
            assume_zero,
            progress_cb,
            progress_interval,
        )
        return session.run()
    finally:
        os.close(fd)


def _arg_chunk_size(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("not an integer: %r" % value)
    if not MIN_CHUNK_SIZE <= number <= MAX_CHUNK_SIZE:
        raise argparse.ArgumentTypeError(
            "must be between %d and %d" % (MIN_CHUNK_SIZE, MAX_CHUNK_SIZE)
        )
    return number


def _arg_workers(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("not an integer: %r" % value)
    if not 1 <= number <= MAX_WORKERS:
        raise argparse.ArgumentTypeError("must be between 1 and %d" % MAX_WORKERS)
    return number


def _arg_interval(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("not a number: %r" % value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="blocksync.py",
        description="Block-level delta sync for warm migrations (protocol v1).",
    )
    commands = parser.add_subparsers(dest="command", metavar="{send,receive,hash}")
    commands.required = True

    def device_options(sub):
        sub.add_argument("--device", required=True, help="block device or file")
        sub.add_argument(
            "--chunk-size",
            type=_arg_chunk_size,
            default=DEFAULT_CHUNK_SIZE,
            help="chunk size in bytes (default %(default)s)",
        )
        sub.add_argument(
            "--workers",
            type=_arg_workers,
            default=DEFAULT_WORKERS,
            help="read-ahead hashing threads (default %(default)s)",
        )

    send = commands.add_parser(
        "send",
        help="stream changed chunks (stdin: digests, stdout: frames)",
    )
    device_options(send)

    receive = commands.add_parser(
        "receive",
        help="spawn the sender given after '--' and apply its frames",
        usage="%(prog)s --device PATH [--chunk-size N] [--workers W] "
        "[--assume-zero] [--progress-interval S] -- SENDER_COMMAND [ARGS...]",
    )
    device_options(receive)
    receive.add_argument(
        "--assume-zero",
        action="store_true",
        help="treat the destination as all zeros without reading it",
    )
    receive.add_argument(
        "--progress-interval",
        type=_arg_interval,
        default=DEFAULT_PROGRESS_INTERVAL,
        help="seconds between JSON progress lines on stderr (default %(default)s)",
    )

    hash_cmd = commands.add_parser(
        "hash", help="print the manifest digest (hex) and size of a device"
    )
    device_options(hash_cmd)
    return parser


def _silence_stdout():
    """Avoid a second BrokenPipeError when the interpreter flushes stdout."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError):
        pass


def _print_receive_progress(done, total):
    _emit_stderr(
        _json_line(
            collections.OrderedDict(
                [
                    ("event", "progress"),
                    ("bytes_done", done),
                    ("bytes_total", total),
                    ("pct", _pct(done, total)),
                ]
            )
        )
    )


def _print_send_progress(done, total):
    _emit_stderr(
        _json_line(
            collections.OrderedDict(
                [("event", "send_progress"), ("bytes_done", done), ("bytes_total", total)]
            )
        )
    )


def _cmd_send(args):
    try:
        run_sender(
            args.device,
            args.chunk_size,
            args.workers,
            sys.stdin.buffer,
            sys.stdout.buffer,
            progress_cb=_print_send_progress,
        )
    except BrokenPipeError:
        _silence_stdout()
        _log("error: the receiver closed the connection")
        return EXIT_PROTOCOL
    except BlocksyncError as err:
        _silence_stdout()
        _log("error: %s" % err)
        return err.exit_code
    except OSError as err:
        _silence_stdout()
        _log("error: %s" % err)
        return EXIT_USAGE
    return EXIT_OK


def _fail_receive(code, message):
    _log("error: " + message)
    sys.stdout.write(_json_line({"ok": False, "error": message}) + "\n")
    sys.stdout.flush()
    return code


def _cmd_receive(args, sender_cmd):
    if not sender_cmd:
        return _fail_receive(
            EXIT_USAGE, "receive needs a sender command after '--'"
        )
    try:
        summary = run_receiver(
            args.device,
            args.chunk_size,
            args.workers,
            sender_cmd,
            assume_zero=args.assume_zero,
            progress_cb=_print_receive_progress,
            progress_interval=args.progress_interval,
        )
    except BlocksyncError as err:
        return _fail_receive(err.exit_code, str(err))
    except OSError as err:
        return _fail_receive(EXIT_USAGE, "I/O error: %s" % err)
    sys.stdout.write(_json_line(summary.to_dict()) + "\n")
    sys.stdout.flush()
    return EXIT_OK


def _cmd_hash(args):
    try:
        fd = os.open(args.device, os.O_RDONLY)
        try:
            size = _fd_size(fd)
            digest = manifest_digest(
                chunk.digest
                for chunk in _scan_chunks(fd, size, args.chunk_size, args.workers)
            )
        finally:
            os.close(fd)
    except (OSError, BlocksyncError) as err:
        _log("error: %s" % err)
        return getattr(err, "exit_code", EXIT_USAGE)
    sys.stdout.write("%s %d\n" % (_hex(digest), size))
    sys.stdout.flush()
    return EXIT_OK


def main(argv=None):
    """CLI entry point; returns the exit status."""
    argv = list(sys.argv[1:] if argv is None else argv)
    sender_cmd = []
    if "--" in argv:
        split = argv.index("--")
        sender_cmd = argv[split + 1:]
        argv = argv[:split]
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if args.command == "receive":
        return _cmd_receive(args, sender_cmd)
    if sender_cmd:
        _log("error: only receive takes a command after '--'")
        return EXIT_USAGE
    if args.command == "send":
        return _cmd_send(args)
    return _cmd_hash(args)


if __name__ == "__main__":
    sys.exit(main())
