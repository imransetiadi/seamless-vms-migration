# Copyright: Seamless Migrate contributors
# Apache License 2.0 (see LICENSE)
"""Block-level delta synchronisation for warm migrations (protocols v1 and v2).

This file is both an os-migrate ``module_utils`` module and a standalone
script that runs on the conversion hosts, so it depends on the Python 3.6+
standard library only::

    python3 blocksync.py send    --device PATH [--chunk-size N] [--workers W]
                                 [--expect-serial ID]
                                 [--extents FILE --extents-mode read|check]
    python3 blocksync.py receive --device PATH [--chunk-size N] [--workers W]
                                 [--assume-zero] [--progress-interval S]
                                 [--expect-serial ID] -- SENDER_COMMAND [ARGS...]
    python3 blocksync.py hash    --device PATH [--chunk-size N]
    python3 blocksync.py gate    --python PYTHON [--volume ID ...]

``--expect-serial ID`` checks, before a byte is read or written, that the
device opened is the disk of volume ``ID``: its virtio serial must be a prefix
of ``ID`` (exit 3 otherwise); a device that shows no serial is used as given
(Seamless Migrate SDD 6.2).

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

``gate`` is the forced command of a run's link key on the source conversion
host (Seamless Migrate SDD 6.2, 26.6): it reads ``SSH_ORIGINAL_COMMAND`` and
answers ``probe`` with the client address of ``SSH_CONNECTION``, or execs,
without a shell, exactly ``sudo -n PYTHON <this script> send --device PATH
--chunk-size N --workers W --expect-serial ID`` for an ID given as ``--volume``
on a disk that shows its serial. It refuses everything else with one line on
stderr and exit 2.

With ``--extents FILE`` (JSON ``[offset, length]`` pairs, sorted and merged
here; each chunk an extent touches is listed) the sender runs one of two
modes (Seamless Migrate SDD 6.2, 28.5):

* ``read`` - protocol v2: ``HELLO`` with ``version=2``, then ``u64 n`` and the
  ``n`` listed chunk indexes (``u64``, strictly increasing, within the
  source). The receiver sends the digests of those chunks alone, reading no
  other; frames are v1's, each for a listed chunk; the end frame carries ``n``
  and a manifest over each listed chunk's index (``u64``) and digest, in
  order. ``bytes_scanned`` counts the listed chunks; ``--assume-zero`` is
  refused (exit 2).
* ``check`` - protocol v1 on the wire and a full scan; the sender also counts
  the chunks whose digests differed that the extents do not list, and
  reports both counts on stderr for the receiver's summary
  (``extent_chunks``, ``missed_chunks``).

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
import re
import shlex
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

PROTOCOL_MAGIC = b"SMBS"
PROTOCOL_VERSION = 1
#: a read pass's protocol: the HELLO lists the chunks the extents touch (SDD 6.2, 28.5)
PROTOCOL_VERSION_EXTENTS = 2
EXTENTS_MODES = ("read", "check")
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
_U64 = struct.Struct(">Q")
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
    "duration_s manifest extent_chunks missed_chunks",
)
# a pass without extents has no counts of them (Python 3.6: no namedtuple defaults)
SenderStats.__new__.__defaults__ = (None, None)


class ReceiverSummary(
    collections.namedtuple(
        "ReceiverSummary",
        "ok chunks chunks_changed bytes_scanned bytes_changed bytes_transferred "
        "duration_s extent_chunks missed_chunks",
    )
):
    """Result of a receive; ``to_dict()`` is the CLI's JSON summary line. A check pass's
    sender reports ``extent_chunks`` and ``missed_chunks``; any other pass has none."""

    __slots__ = ()

    def to_dict(self):
        summary = collections.OrderedDict(
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
        if self.extent_chunks is not None:
            summary["extent_chunks"] = self.extent_chunks
            summary["missed_chunks"] = self.missed_chunks
        return summary


ReceiverSummary.__new__.__defaults__ = (None, None)


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


def extent_manifest(listed):
    """A version 2 pass's manifest: BLAKE2b-128 over each listed chunk's index (u64,
    big-endian) and digest, in order - ``listed`` yields (index, digest) pairs."""
    manifest = new_hasher()
    for index, digest in listed:
        manifest.update(_U64.pack(index))
        manifest.update(digest)
    return manifest.digest()


def _extent_pairs(extents):
    """``extents`` as (offset, length) pairs of non-negative integers, each length positive;
    BlocksyncError (exit 2) for anything else."""
    if not isinstance(extents, list):
        raise BlocksyncError("the extents are not a list of [offset, length] pairs")
    pairs = []
    for extent in extents:
        if (
            not isinstance(extent, (list, tuple))
            or len(extent) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) for v in extent)
        ):
            raise BlocksyncError(
                "an extent is not an [offset, length] pair of integers: %r" % (extent,)
            )
        offset, length = extent
        if offset < 0 or length < 0:
            raise BlocksyncError("a negative extent: [%d, %d]" % (offset, length))
        if length == 0:
            raise BlocksyncError("an empty extent: [%d, 0]" % offset)
        pairs.append((offset, length))
    return pairs


def extent_chunks(extents, size, chunk_size):
    """The indexes of the chunks ``extents`` touch - the pairs sorted and merged -, sorted, each
    once (SDD 6.2, 28.5). An extent that is no pair of integers, negative, empty or reaching
    beyond the source's ``size`` bytes raises BlocksyncError (exit 2)."""
    indexes = []
    for offset, length in sorted(_extent_pairs(extents)):
        end = offset + length
        if end > size:
            raise BlocksyncError(
                "an extent beyond the source (%d bytes): [%d, %d]" % (size, offset, length)
            )
        first = offset // chunk_size
        if indexes and first <= indexes[-1]:  # overlapping or in the same chunk: merged
            first = indexes[-1] + 1
        indexes.extend(range(first, (end - 1) // chunk_size + 1))
    return indexes


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
    # All-zero chunks (thin volumes, never-written regions) take the cached digest instead of
    # being hashed: counting zero bytes runs at memory speed, about twice BLAKE2b. The first
    # 4 KiB are checked alone first so chunks with data pay almost nothing for the test.
    head = min(length, 4096)
    if data.count(0, 0, head) == head and data.count(0) == length:
        return _Chunk(index, offset, length, zero_digest(length), data if keep_data else None)
    return _Chunk(index, offset, length, chunk_digest(data), data if keep_data else None)


def _scan_chunks(fd, size, chunk_size, workers, keep_data=False, stop=None, indexes=None):
    """Yield every chunk of the device in order - or, given ``indexes`` (increasing), those
    chunks alone: no other is read.

    With ``workers > 1`` chunks are read and hashed ahead by a thread pool
    (``os.pread`` and hashlib release the GIL); at most ``2 * workers``
    chunks are in flight, which bounds memory to ``2 * workers * chunk_size``.
    """
    if indexes is None:
        indexes = range(chunk_count(size, chunk_size))
    if workers <= 1:
        for index in indexes:
            if stop is not None and stop.is_set():
                return
            yield _hash_chunk(fd, index, chunk_size, size, keep_data)
        return

    pool = ThreadPoolExecutor(max_workers=workers)
    pending = collections.deque()
    upcoming = iter(indexes)
    try:
        for index in upcoming:
            pending.append(pool.submit(_hash_chunk, fd, index, chunk_size, size, keep_data))
            if len(pending) >= 2 * workers:
                break
        while pending:
            if stop is not None and stop.is_set():
                return
            chunk = pending.popleft().result()
            for index in upcoming:
                pending.append(pool.submit(_hash_chunk, fd, index, chunk_size, size, keep_data))
                break
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


#: where the kernel lists block devices by major:minor; a virtio-blk disk shows its serial there
SYS_DEV_BLOCK = "/sys/dev/block"


def _serial_of(st):
    """The virtio-blk serial of the block device ``st`` (an ``os.stat`` result) describes - the
    volume id Nova gave the disk, cut to 20 characters - or None: a regular file, a device that
    shows no serial (not virtio-blk)."""
    if not stat.S_ISBLK(st.st_mode):
        return None
    path = os.path.join(
        SYS_DEV_BLOCK, "%d:%d" % (os.major(st.st_rdev), os.minor(st.st_rdev)), "serial"
    )
    try:
        with open(path, "rb") as f:
            serial = f.read().decode("utf-8", "replace").strip("\x00").strip()
    except (IOError, OSError):
        return None
    return serial or None


def device_serial(fd):
    """The virtio-blk serial of the device open on ``fd``: read through the descriptor's own
    device numbers, so no disk can take the path between the check and the use."""
    return _serial_of(os.fstat(fd))


def _check_identity(fd, device, expected):
    """With ``expected`` (a volume id): fail before any I/O unless the device open on ``fd`` is
    that volume's disk. The kernel gives a freed /dev/vdX path to the next disk attached, so a path
    another volume took would be read, or written into (Seamless Migrate SDD 6.2). A device that
    shows no serial is used as given."""
    if not expected:
        return
    serial = device_serial(fd)
    if serial is None:
        _log("warning: %s shows no virtio serial; used as given" % device)
        return
    if not expected.startswith(serial):
        raise ProtocolError(
            "%s is the disk of volume %s, not of volume %s" % (device, serial, expected)
        )


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


def _extent_check_line(extent_chunks, missed_chunks):
    """A check pass's counts, which the sender reports on stderr for the receiver's summary."""
    return _json_line(
        collections.OrderedDict(
            [
                ("event", "extent_check"),
                ("extent_chunks", extent_chunks),
                ("missed_chunks", missed_chunks),
            ]
        )
    )


def _parse_extent_check(line):
    """(extent_chunks, missed_chunks) of a sender's extent_check line, else None."""
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if isinstance(event, dict) and event.get("event") == "extent_check":
        try:
            return int(event["extent_chunks"]), int(event["missed_chunks"])
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
    expect_serial=None,
    extents=None,
    extents_mode=None,
):
    """Sender side of protocols v1 and v2.

    Reads the receiver's digests from ``inp`` and writes frames to ``out``
    (binary streams). Returns ``SenderStats``. With ``extents`` (``[offset,
    length]`` pairs) and ``extents_mode``: ``read`` runs protocol v2 over the
    chunks they touch alone; ``check`` runs v1 in full and counts the changed
    chunks they do not list (Seamless Migrate SDD 6.2, 28.5).
    """
    started = time.monotonic()
    _validate_chunk_size(chunk_size)
    workers = _validate_workers(workers)
    if (extents is None) != (extents_mode is None):
        raise BlocksyncError("--extents and --extents-mode go together")
    if extents_mode is not None and extents_mode not in EXTENTS_MODES:
        raise BlocksyncError("the extents mode is read or check, not %r" % (extents_mode,))
    fd = os.open(device, os.O_RDONLY)
    try:
        _check_identity(fd, device, expect_serial)
        size = _fd_size(fd)
        listed = None if extents is None else extent_chunks(extents, size, chunk_size)
        if extents_mode == "read":
            indexes = listed
            total = sum(_chunk_length(index, size, chunk_size) for index in indexes)
            out.write(
                _HELLO.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION_EXTENTS, chunk_size, size)
                + _U64.pack(len(indexes))
                + b"".join(_U64.pack(index) for index in indexes)
            )
        else:
            indexes = None
            total = size
            out.write(_HELLO.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION, chunk_size, size))
        out.flush()
        count = len(indexes) if indexes is not None else chunk_count(size, chunk_size)
        outside = set(listed) if extents_mode == "check" else None
        reporter = _ProgressReporter(total, progress_cb, progress_interval)
        manifest = new_hasher()
        changed = bytes_changed = transferred = missed = done = 0
        for position, chunk in enumerate(
            _scan_chunks(fd, size, chunk_size, workers, keep_data=True, indexes=indexes)
        ):
            theirs = _read_exact(inp, DIGEST_SIZE)
            if len(theirs) != DIGEST_SIZE:
                raise ProtocolError(
                    "the receiver's digest stream ended after %d of %d chunks"
                    % (position, count)
                )
            if indexes is not None:
                manifest.update(_U64.pack(chunk.index))
            manifest.update(chunk.digest)
            if theirs != chunk.digest:
                changed += 1
                bytes_changed += chunk.length
                if outside is not None and chunk.index not in outside:
                    missed += 1
                if chunk.digest == zero_digest(chunk.length) and chunk.data == _zeros(
                    chunk.length
                ):
                    out.write(FRAME_ZERO + _FRAME_HEADER.pack(chunk.offset, chunk.length))
                else:
                    out.write(FRAME_DATA + _FRAME_HEADER.pack(chunk.offset, chunk.length))
                    out.write(chunk.data)
                    transferred += chunk.length
            done += chunk.length
            reporter.update(done)
        digest = manifest.digest()
        if outside is not None:
            _emit_stderr(_extent_check_line(len(listed), missed))
        out.write(FRAME_END + _END.pack(count, changed, transferred, digest))
        out.flush()
        reporter.update(total, force=True)
    finally:
        os.close(fd)
    return SenderStats(
        count,
        changed,
        total,
        bytes_changed,
        transferred,
        round(time.monotonic() - started, 3),
        digest,
        len(listed) if outside is not None else None,
        missed if outside is not None else None,
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
        # the chunks this pass syncs - every chunk (v1), or the HELLO's listed ones (v2) -,
        # their digests in that order, and for v2 each listed chunk's position and the bytes
        # done once it is (SDD 6.2, 28.5)
        self.indexes = None
        self.positions = None
        self.done_at = None
        self.extent_check = None
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
            # a script run on the conversion host, not an Ansible module: no run_command there
            self.proc = subprocess.Popen(  # pylint: disable=ansible-bad-function
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
            source_size, listed = self._read_hello(dest_size)
            count = chunk_count(source_size, self.chunk_size)
            if listed is None:
                self.indexes = range(count)
                scanned = source_size
            else:
                self.indexes = listed
                self.positions = dict((index, position) for position, index in enumerate(listed))
                self.done_at = []
                scanned = 0
                for index in listed:
                    scanned += _chunk_length(index, source_size, self.chunk_size)
                    self.done_at.append(scanned)
            self.digests = [None] * len(self.indexes)
            self.reporter = _ProgressReporter(scanned, self.progress_cb, self.interval)
            self.reporter.update(0)
            self.digest_thread = self._start(self._send_digests, source_size, count)
            changed, bytes_changed, transferred = self._apply_frames(source_size, count)
            os.fsync(self.fd)
            self._finish_sender()
            self._raise_callback_error()
            self.reporter.update(scanned, force=True)
        except BaseException:
            self._abort()
            raise
        extent_chunks, missed_chunks = self.extent_check or (None, None)
        return ReceiverSummary(
            True,
            len(self.indexes),
            changed,
            scanned,
            bytes_changed,
            transferred,
            round(time.monotonic() - started, 3),
            extent_chunks,
            missed_chunks,
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
        if version not in (PROTOCOL_VERSION, PROTOCOL_VERSION_EXTENTS):
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
        if version == PROTOCOL_VERSION:
            return source_size, None
        if self.assume_zero:
            # a read pass digests the destination's listed chunks: assuming them zero would
            # skip the read the pass relies on (SDD 28.5)
            raise BlocksyncError(
                "--assume-zero is refused with a version 2 pass, which reads the listed "
                "chunks of the destination"
            )
        return source_size, self._read_listed(chunk_count(source_size, chunk_size))

    def _read_listed(self, count):
        """A version 2 HELLO's chunk indexes: at most the source's chunks, strictly increasing,
        each within the source - else ProtocolError (exit 3), before a chunk is read."""
        raw = _read_exact(self.proc.stdout, _U64.size)
        if len(raw) != _U64.size:
            self._raise_sender_failure("the sender closed the stream inside its HELLO")
        (number,) = _U64.unpack(raw)
        if number > count:
            raise ProtocolError(
                "the HELLO lists %d chunks, beyond the source's %d" % (number, count)
            )
        raw = _read_exact(self.proc.stdout, number * _U64.size)
        if len(raw) != number * _U64.size:
            self._raise_sender_failure("the sender closed the stream inside its HELLO")
        listed = []
        for position in range(number):
            (index,) = _U64.unpack_from(raw, position * _U64.size)
            if index >= count:
                raise ProtocolError(
                    "listed chunk %d is beyond the source (%d chunks)" % (index, count)
                )
            if listed and index <= listed[-1]:
                raise ProtocolError("the listed chunk indexes are not strictly increasing")
            listed.append(index)
        return listed

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
                for position, chunk in enumerate(
                    _scan_chunks(
                        self.fd,
                        source_size,
                        self.chunk_size,
                        self.workers,
                        stop=self.stop,
                        indexes=self.indexes,
                    )
                ):
                    self.digests[position] = chunk.digest
                    # Counted before it is written: the sender may answer
                    # before this thread runs again.
                    self.sent = position + 1
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
                counts = None if done is not None else _parse_extent_check(line)
                if counts is not None:
                    # a check pass's counts, for the summary (SDD 28.5)
                    self.extent_check = counts
                elif done is None:
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
            index, position = self._check_frame(offset, length, last_index, count, source_size)
            last_index = index
            if kind == FRAME_DATA:
                payload = _read_exact(stream, length)
                if len(payload) != length:
                    self._raise_sender_failure("truncated data frame")
                _pwrite_full(self.fd, payload, offset)
                self.digests[position] = chunk_digest(payload)
                transferred += length
            else:
                _pwrite_full(self.fd, _zeros(length), offset)
                self.digests[position] = zero_digest(length)
            changed += 1
            bytes_changed += length
            self.reporter.update(
                offset + length if self.done_at is None else self.done_at[position]
            )
            self._raise_callback_error()

        end = _read_exact(stream, _END.size)
        if len(end) != _END.size:
            self._raise_sender_failure("truncated end frame")
        chunks, chunks_changed, bytes_transferred, their_manifest = _END.unpack(end)
        synced = len(self.indexes)
        if (chunks, chunks_changed, bytes_transferred) != (synced, changed, transferred):
            raise ProtocolError(
                "end frame mismatch: sender reports %d chunks/%d changed/%d bytes, "
                "receiver saw %d/%d/%d"
                % (chunks, chunks_changed, bytes_transferred, synced, changed, transferred)
            )
        self._check_digest_thread()
        if self.sent != synced or any(digest is None for digest in self.digests):
            raise ProtocolError("the receiver did not send every digest")
        if self.positions is None:
            ours = manifest_digest(self.digests)
        else:
            ours = extent_manifest(zip(self.indexes, self.digests))
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
        """The frame's chunk index and its position among the synced chunks."""
        if offset % self.chunk_size:
            raise ProtocolError("frame offset %d is not chunk aligned" % offset)
        index = offset // self.chunk_size
        if index >= count:
            raise ProtocolError("frame offset %d is beyond the source size" % offset)
        if index <= last_index:
            raise ProtocolError("frame for chunk %d is out of order" % index)
        if self.positions is None:
            position = index
        else:
            position = self.positions.get(index)
            if position is None:
                raise ProtocolError(
                    "frame for chunk %d: chunk %d is not listed in the HELLO" % (index, index)
                )
        if position >= self.sent:
            raise ProtocolError("frame for chunk %d arrived before its digest" % index)
        expected = _chunk_length(index, source_size, self.chunk_size)
        if length != expected:
            raise ProtocolError(
                "frame for chunk %d has length %d, expected %d" % (index, length, expected)
            )
        return index, position

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
                # pylint: disable-next=invalid-unary-operand-type
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
    expect_serial=None,
):
    """Receiver side of protocols v1 and v2.

    Spawns ``sender_cmd`` (an argv list), synchronises ``device`` with the
    sender's device - every chunk (v1), or the chunks a version 2 HELLO
    lists, reading no other - and returns a ``ReceiverSummary``. Raises
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
        _check_identity(fd, device, expect_serial)
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


#: the line a gate's refusal starts with, which the warm sync reads on the receiver's stderr as a
#: refused link (Seamless Migrate SDD 6.2, 6.6, 26.6)
GATE_REFUSED = "seamless-link gate refused: "
#: the options of the one send the gate runs, in the order the warm sync writes them (SDD 6.4)
_GATE_SEND_OPTIONS = ["--device", "--chunk-size", "--workers", "--expect-serial"]
#: the same send over a disk's extents (SDD 28.5): the file beside this script, named for it
_GATE_EXTENTS_OPTIONS = _GATE_SEND_OPTIONS + ["--extents", "--extents-mode"]
_GATE_EXTENTS_FILE = re.compile(r"^extents-[A-Za-z0-9_.-]{1,80}\.json$")


class _GateRefusal(Exception):
    """What the gate refuses, as its stderr line names it."""


def _disk_serial(path):
    """The virtio-blk serial of the disk at ``path``, or None: a path that names no block device,
    or a disk that shows no serial."""
    try:
        return _serial_of(os.stat(path))
    except (IOError, OSError):
        return None


def _gate_word(word):
    """A refused command's first word as the refusal names it: short, one line."""
    return word.replace("\n", " ")[:64]


def _gate_send(words, python, volumes):
    """The argv of the send ``words`` ask for, rebuilt from the values the gate checked; raises
    _GateRefusal for any other command (SDD 6.2, 26.6)."""
    head = ["sudo", "-n", python, os.path.abspath(__file__), "send"]
    if words[: len(head)] != head:
        raise _GateRefusal("a command it does not run: " + _gate_word(words[0]))
    options = words[len(head):]
    names = options[0::2]
    if "--expect-serial" not in names:
        raise _GateRefusal("send without --expect-serial")
    if len(options) == 2 * len(_GATE_SEND_OPTIONS) and names == _GATE_SEND_OPTIONS:
        extents = None
    elif len(options) == 2 * len(_GATE_EXTENTS_OPTIONS) and names == _GATE_EXTENTS_OPTIONS:
        extents = options[9], options[11]
    else:
        raise _GateRefusal("send with other options")
    device, chunk_size, workers, serial = options[1:8:2]
    if serial not in volumes:
        raise _GateRefusal("volume %s, which is not the run's" % _gate_word(serial))
    for name, value, check in (
        ("--chunk-size", chunk_size, _arg_chunk_size),
        ("--workers", workers, _arg_workers),
    ):
        try:
            check(value)
        except argparse.ArgumentTypeError as err:
            raise _GateRefusal("send with %s %s: %s" % (name, _gate_word(value), err))
    if not os.path.isabs(device):
        raise _GateRefusal("%s, which is no absolute path" % _gate_word(device))
    shown = _disk_serial(device)
    if shown is None:
        raise _GateRefusal("%s, which shows no virtio serial" % _gate_word(device))
    if not serial.startswith(shown):
        raise _GateRefusal(
            "%s is the disk of volume %s, not of volume %s" % (_gate_word(device), shown, serial)
        )
    argv = head + [
        "--device", device,
        "--chunk-size", str(_arg_chunk_size(chunk_size)),
        "--workers", str(_arg_workers(workers)),
        "--expect-serial", serial,
    ]
    if extents is not None:
        path, mode = extents
        if mode not in EXTENTS_MODES:
            raise _GateRefusal("send with --extents-mode %s" % _gate_word(mode))
        if (
            not os.path.isabs(path)
            or os.path.dirname(path) != os.path.dirname(os.path.abspath(__file__))
            or not _GATE_EXTENTS_FILE.match(os.path.basename(path))
        ):
            raise _GateRefusal("%s, which is no extents file beside the script" % _gate_word(path))
        argv += ["--extents", path, "--extents-mode", mode]
    return argv


def _cmd_gate(args):
    """The forced command of a run's link key (SDD 6.2, 26.6): ``probe``, the run's own send, or
    a refusal - never a shell."""
    original = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    try:
        if not original.strip():
            raise _GateRefusal("a shell")
        try:
            words = shlex.split(original)
        except ValueError:
            raise _GateRefusal("a command it cannot read")
        if words == ["probe"]:
            client = os.environ.get("SSH_CONNECTION", "").split()
            if not client:
                raise _GateRefusal("a probe without SSH_CONNECTION")
            sys.stdout.write(client[0] + "\n")
            sys.stdout.flush()
            return EXIT_OK
        argv = _gate_send(words, args.python, args.volume)
    except _GateRefusal as refusal:
        _emit_stderr(GATE_REFUSED + str(refusal))
        return EXIT_USAGE
    os.execvp("sudo", argv)
    return EXIT_USAGE  # never reached: execvp returns only by raising


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="blocksync.py",
        description="Block-level delta sync for warm migrations (protocols v1 and v2).",
    )
    commands = parser.add_subparsers(dest="command", metavar="{send,receive,hash,gate}")
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

    def identity_option(sub):
        sub.add_argument(
            "--expect-serial",
            metavar="ID",
            default=None,
            help="volume id the device's virtio serial must be a prefix of (checked on the opened "
            "device before any I/O; a device without a serial is used as given)",
        )

    send = commands.add_parser(
        "send",
        help="stream changed chunks (stdin: digests, stdout: frames)",
    )
    device_options(send)
    identity_option(send)
    send.add_argument(
        "--extents",
        metavar="FILE",
        default=None,
        help="JSON [offset, length] pairs of the source that changed since the base pass",
    )
    send.add_argument(
        "--extents-mode",
        choices=EXTENTS_MODES,
        default=None,
        help="read: protocol v2 over the chunks the extents touch alone; check: a full v1 "
        "pass that counts the changed chunks the extents miss",
    )

    receive = commands.add_parser(
        "receive",
        help="spawn the sender given after '--' and apply its frames",
        usage="%(prog)s --device PATH [--chunk-size N] [--workers W] "
        "[--assume-zero] [--progress-interval S] [--expect-serial ID] "
        "-- SENDER_COMMAND [ARGS...]",
    )
    device_options(receive)
    identity_option(receive)
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

    gate = commands.add_parser(
        "gate",
        help="the forced command of a run's link key: probe, or the run's own send",
    )
    gate.add_argument("--python", required=True, help="the interpreter the send must name")
    gate.add_argument(
        "--volume",
        metavar="ID",
        action="append",
        default=[],
        help="a volume of the run, whose disk a send may read (repeatable)",
    )
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


def _load_extents(path):
    """The extents file's JSON; BlocksyncError (exit 2) when it is not JSON."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except ValueError as err:
        raise BlocksyncError("the extents file is not JSON: %s" % err)


def _cmd_send(args):
    try:
        extents = None if args.extents is None else _load_extents(args.extents)
        run_sender(
            args.device,
            args.chunk_size,
            args.workers,
            sys.stdin.buffer,
            sys.stdout.buffer,
            progress_cb=_print_send_progress,
            expect_serial=args.expect_serial,
            extents=extents,
            extents_mode=args.extents_mode,
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
            expect_serial=args.expect_serial,
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
    if args.command == "gate":
        return _cmd_gate(args)
    return _cmd_hash(args)


if __name__ == "__main__":
    # the conversion hosts run it as a script: its exit status is the protocol's (no module)
    sys.exit(main())  # pylint: disable=ansible-bad-function
