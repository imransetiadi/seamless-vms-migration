# Copyright: Seamless Migrate contributors
# Apache License 2.0 (see LICENSE)
"""The extent query of changed-extent tracking (Seamless Migrate SDD 6.4; plan task CE3).

``query_extents`` lists the extents of a Cinder RBD volume that changed between two of Cinder's
snapshots of it - the base, the last completed pass's, and the current pass's - with ``rbd diff``
on the source conversion host, through the module's RemoteShell::

    rbd --id <client> --conf <dir>/ceph.conf --keyring <dir>/keyring diff
        --from-snap snapshot-<base> --whole-object --format json
        <pool>/volume-<name_id>@snapshot-<current>

What plan task CE4's warm modules read, per disk - ``ExtentUse``'s reason and ``query_s``
(SDD 28.5)::

    {"extents": [[offset, length], ...], "reason": None, "query_s": 1.204}    # it succeeded
    {"extents": None, "reason": "query_failed", "query_s": 300.517}           # it failed

The extents are byte ranges within the volume, sorted, those that overlap or touch merged: what
blocksync's ``--extents`` takes (SDD 6.2, plan task CE2). An empty list is a query that found no
change, so a caller reads ``reason``, never the list's truth. A failure is a reason, never an
exception: tracking never fails a pass (SDD 28.5, owner decision 10). A refused host key is no
failure of tracking: HostKeyRefused propagates, as from every other SSH use of a module (SDD 6.6,
26.5).

The run's ceph.conf and keyring are copied for each query into a fresh 0700 directory on the host,
removed after it whatever its outcome; the key stays in those files - no log line, result,
exception or command line holds it, and this module never reads the keyring (SDD 28.8).

Bound by SDD 6.4 (the extent query: its command, the names, the chunk mapping, the timeout, the
files on the conversion host, the result), 28.4 (which disks, the names, the base, the query, the
credentials), 28.5 (``ExtentUse``: the reason query_failed, ``query_s``), 28.8 (the Ceph key), 6.6
(RemoteShell, pinned SSH and its refusals) and 6.2 (blocksync's chunk, which the extents name).
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import logging
import math
import re
import shlex
import subprocess
import time
import uuid

from ansible_collections.os_migrate.os_migrate.plugins.module_utils.volume_common import (
    RemoteCommandFailed,
    SshRefused,
    ssh_failed,
)

#: the reason of a query that failed - a non-zero exit, output that is not rbd diff's JSON list,
#: an extent beyond the volume, the timeout, ssh, a name rbd could misread (SDD 28.5)
QUERY_FAILED = "query_failed"
#: how long rbd may run on the conversion host, by default (SDD 28.5: 300 s)
QUERY_TIMEOUT_S = 300
#: how much longer the module waits for the query's answer: ssh's connection, timeout's kill
QUERY_GRACE_S = 30
#: timeout's -k: how long after its SIGTERM a query still running is killed
KILL_AFTER_S = 10
#: how long the module waits for its directory's creation, and for its removal
HOUSEKEEPING_S = 60
#: where the query makes its directory on the conversion host, as blocksync's script goes there
REMOTE_TMP = "/tmp"
#: the name of the query's directory before its random part
DIRECTORY_PREFIX = "seamless-extents-"
#: the names of the copies of the run's Ceph files in that directory
CONF_NAME = "ceph.conf"
KEYRING_NAME = "keyring"
#: Cinder's names of a volume's RBD image and of a snapshot of it: volume_name_template,
#: snapshot_name_template (SDD 28.4)
IMAGE_NAME = "volume-%s"
SNAPSHOT_NAME = "snapshot-%s"
GIB = 1 << 30
#: the collection modules' logger, which writes a module's log_file (volume_common)
LOGGER = "osp-osp"

#: the script the query runs in on the host, every value a positional parameter: the directory,
#: then timeout's words and rbd's. It discards rbd's standard error - Ceph prints there a key it
#: cannot parse ("error setting modifier ... val=...") - and removes the directory as rbd ends,
#: whatever became of the module meanwhile (SDD 6.4, 28.8).
HOST_SCRIPT = 'd=$1; shift; timeout "$@" 2>/dev/null; s=$?; rm -rf -- "$d"; exit "$s"'

#: timeout's exit status when it ended rbd: 124 after its SIGTERM, 137 after -k's SIGKILL
_TIMED_OUT = (124, 137)
#: the shell's exit status for a program it could not run (126) or find (127)
_NOT_RUN = (126, 127)
#: a pool, a name_id or a snapshot id rbd cannot misread: no "/" or "@", which separate the
#: parts of its image spec, no leading "-", an option to it
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: the Ceph user without "client.": 1-64 letters, digits, ".", "-" or "_" (SDD 28.4)
_CLIENT = re.compile(r"[A-Za-z0-9._-]{1,64}")


class ExtentQueryError(ValueError):
    """Why a query failed (query_failed, SDD 28.5): a message that holds no key and quotes none of
    rbd's output."""


def _whole(value):
    """A whole number of at least 0, never a bool."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _seconds(value):
    """``value`` seconds as timeout(1) takes them and a message says them."""
    return str(value) if isinstance(value, int) else repr(float(value))


def _named(what, value, pattern=_NAME):
    """``value``, a word rbd reads as the name it is; else ExtentQueryError."""
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ExtentQueryError("the %s %r is not a name rbd can be given" % (what, value))
    return value


def merged(extents):
    """``(offset, length)`` pairs as ``[offset, length]`` pairs sorted by offset, those that overlap
    or touch merged into one: what blocksync's ``--extents`` takes (SDD 6.2, 28.5)."""
    result = []
    for offset, length in sorted(extents):
        if result and offset <= result[-1][0] + result[-1][1]:
            result[-1][1] = max(result[-1][1], offset + length - result[-1][0])
        else:
            result.append([offset, length])
    return result


def parse_rbd_diff(output, size):
    """``rbd diff --format json``'s ``output`` - a list of ``{offset, length, exists}`` objects,
    offsets and lengths in bytes, ``exists`` the string "true" or "false" as rbd prints it (a JSON
    boolean reads alike) - as sorted, merged ``[offset, length]`` pairs (``merged``).

    An extent rbd lists with ``exists`` false - zeroed, discarded or trimmed since the base - is kept
    like a data extent: it is read like the others, and blocksync sends its zero frame. Output that
    is not such a list, and an extent that ends beyond ``size`` bytes - none of this volume's -,
    raise ExtentQueryError, whose message quotes none of the output (SDD 6.4, 28.4)."""
    try:
        listed = json.loads(output)
    except (ValueError, RecursionError) as err:  # json's messages name a position, never the text
        raise ExtentQueryError("rbd's output is not JSON (%s)" % err) from None
    if not isinstance(listed, list):
        raise ExtentQueryError("rbd's output is not a list of extents")
    found = []
    for number, extent in enumerate(listed):
        fields = extent if isinstance(extent, dict) else {}
        offset, length, exists = (fields.get(key) for key in ("offset", "length", "exists"))
        if not (
            _whole(offset)
            and _whole(length)
            and length > 0
            and (exists is True or exists is False or exists in ("true", "false"))
        ):
            raise ExtentQueryError("extent %d is not {offset, length, exists} as rbd lists one" % number)
        if offset + length > size:
            raise ExtentQueryError(
                "extent %d ends at byte %d, beyond the volume's %d bytes"
                % (number, offset + length, size)
            )
        found.append((offset, length))
    return merged(found)


def diff_command(directory, *, ceph_client, pool, name_id, base_snapshot_id, snapshot_id):
    """rbd's words for the diff of the image ``volume-<name_id>`` of ``pool`` from the snapshot
    ``snapshot-<base_snapshot_id>`` to ``snapshot-<snapshot_id>``, whole objects, as JSON, as the
    Ceph user ``ceph_client`` with ``directory``'s ceph.conf and keyring alone - never /etc/ceph
    (SDD 6.4, 28.4). A word rbd could misread, or a base that is the current snapshot - whose diff
    would list nothing -, raises ExtentQueryError."""
    base = _named("base snapshot id", base_snapshot_id)
    current = _named("snapshot id", snapshot_id)
    if base == current:
        raise ExtentQueryError(
            "the base snapshot %s is the current snapshot: its diff would list nothing" % base
        )
    image = "%s/%s@%s" % (
        _named("pool", pool),
        IMAGE_NAME % _named("name_id", name_id),
        SNAPSHOT_NAME % current,
    )
    return [
        "rbd",
        "--id", _named("Ceph client", ceph_client, _CLIENT),
        "--conf", "%s/%s" % (directory, CONF_NAME),
        "--keyring", "%s/%s" % (directory, KEYRING_NAME),
        "diff",
        "--from-snap", SNAPSHOT_NAME % base,
        "--whole-object",
        "--format", "json",
        image,
    ]


def host_command(directory, timeout, command):
    """The words the query's ssh runs on the host, each quoted for its shell: ``command``, rbd's
    words, under ``timeout -k KILL_AFTER_S <timeout>`` in HOST_SCRIPT, which discards rbd's
    standard error and removes ``directory`` as rbd ends (SDD 6.4, 28.8)."""
    words = ["sh", "-c", HOST_SCRIPT, "sh", directory, "-k", _seconds(KILL_AFTER_S)]
    return [shlex.quote(word) for word in words + [_seconds(timeout)] + list(command)]


def _failure(err):
    """What failed, in words that hold no command line (SDD 6.6, review RV42B-02)."""
    if isinstance(err, subprocess.TimeoutExpired):
        return "no answer from the conversion host within %s s" % _seconds(err.timeout)
    if isinstance(err, subprocess.CalledProcessError) and ssh_failed(err.returncode):
        return "ssh to the conversion host failed (status %d)" % err.returncode
    return str(err)


def _make_directory(shell, directory):
    """``directory`` on the host, fresh - mkdir refuses one that exists - and 0700."""
    try:
        shell.cmd_out(["mkdir", "-m", "0700", "--", shlex.quote(directory)], timeout=HOUSEKEEPING_S)
    except (OSError, subprocess.SubprocessError) as err:
        raise ExtentQueryError(
            "could not make the query's directory on the conversion host: %s" % _failure(err)
        ) from None


def _copy(shell, local, directory, name):
    """The run's file ``local`` into ``directory`` on the host, as ``name``."""
    try:
        status = shell.scp_to(local, "%s/%s" % (directory, name))
    except (OSError, subprocess.SubprocessError) as err:
        raise ExtentQueryError(
            "could not copy the %s to the conversion host: %s" % (name, _failure(err))
        ) from None
    if status != 0:
        raise ExtentQueryError(
            "could not copy the %s to the conversion host (scp exit status %d)" % (name, status)
        )


def _run(shell, command, timeout):
    """The query's output: rbd's JSON. The host ends rbd after ``timeout`` seconds; the module
    waits QUERY_GRACE_S longer for the answer."""
    try:
        return shell.cmd_out(command, timeout=timeout + QUERY_GRACE_S)
    except RemoteCommandFailed as err:
        if err.returncode in _TIMED_OUT:
            cause = "rbd did not answer within %s s" % _seconds(timeout)
        elif err.returncode in _NOT_RUN:
            cause = "rbd could not run on the conversion host (status %d)" % err.returncode
        else:
            cause = "rbd exited with status %d" % err.returncode
    except UnicodeDecodeError:
        cause = "rbd's output is not text"
    except (OSError, subprocess.SubprocessError) as err:
        cause = _failure(err)
    raise ExtentQueryError(cause)


def _remove(shell, directory):
    """Remove ``directory`` from the host, where the query's shell removed it already unless rbd
    never ran or its answer never came. A removal that fails is logged, naming the directory, and
    fails nothing; a refused host key propagates (SDD 6.6)."""
    try:
        status = shell.cmd_val(["rm", "-rf", "--", shlex.quote(directory)], timeout=HOUSEKEEPING_S)
    except (OSError, subprocess.SubprocessError) as err:
        cause = _failure(err)
    else:
        if status == 0:
            return
        if ssh_failed(status):
            cause = "ssh to the conversion host failed (status %d)" % status
        else:
            cause = "rm exited with status %d" % status
    logging.getLogger(LOGGER).warning(
        "Could not remove %s from the conversion host %s: %s", directory, shell.address, cause
    )


def _query(shell, ceph_conf, ceph_keyring, size_gib, timeout, rbd):
    """The extents rbd lists, ``rbd`` holding diff_command's keywords: the directory made, the
    Ceph files copied, rbd run and its output parsed, the directory removed. Every failure raises
    ExtentQueryError, before anything ran when an argument is wrong; SshRefused propagates."""
    for name, path in ((CONF_NAME, ceph_conf), (KEYRING_NAME, ceph_keyring)):
        if not isinstance(path, str) or not path:
            raise ExtentQueryError("no %s file is named for the query" % name)
    if not (
        isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and math.isfinite(timeout)
        and timeout > 0
    ):
        raise ExtentQueryError("the timeout %r is not a positive number of seconds" % (timeout,))
    if not (_whole(size_gib) and size_gib > 0):
        raise ExtentQueryError("the volume's size %r is not a positive number of GiB" % (size_gib,))
    # a random name: no other query's, and none another user of the host can foresee
    directory = "%s/%s%s" % (REMOTE_TMP, DIRECTORY_PREFIX, uuid.uuid4().hex)
    command = host_command(directory, timeout, diff_command(directory, **rbd))
    refused = False
    try:
        _make_directory(shell, directory)
        _copy(shell, ceph_conf, directory, CONF_NAME)
        _copy(shell, ceph_keyring, directory, KEYRING_NAME)
        return parse_rbd_diff(_run(shell, command, timeout), size_gib * GIB)
    except SshRefused:
        refused = True  # a host whose key is refused gets no further command
        raise
    finally:
        if not refused:
            _remove(shell, directory)


def query_extents(
    shell,
    *,
    ceph_conf,
    ceph_keyring,
    ceph_client,
    pool,
    volume_id,
    name_id,
    size_gib,
    base_snapshot_id,
    snapshot_id,
    timeout=QUERY_TIMEOUT_S,
):
    """The extents of the Cinder volume ``volume_id`` that changed from its Cinder snapshot
    ``base_snapshot_id`` to ``snapshot_id``, as rbd lists them on the source conversion host that
    ``shell``, the module's RemoteShell, reaches (SDD 6.4, 28.4).

    ``ceph_conf`` and ``ceph_keyring`` are the run's local 0600 files (plan task CE7), copied into
    the query's directory on the host and read there alone; ``ceph_client`` is the Ceph user
    without "client."; ``pool`` the volume's Ceph pool; ``name_id`` the volume's name_id - its id
    when Cinder shows none -, which names its RBD image, while ``volume_id`` names the volume in the
    log; ``size_gib`` its size, Cinder's; ``timeout`` the seconds rbd may run on the host.

    Returns ``{"extents": [[offset, length], ...], "reason": None, "query_s": s}`` - sorted,
    merged, within the volume, an empty list when nothing changed - or, when anything failed,
    ``{"extents": None, "reason": "query_failed", "query_s": s}``, the cause logged as a warning.
    ``s`` is the query's seconds, its copies and removal included (``ExtentUse``, SDD 28.5). It
    raises nothing but SshRefused: the host's key was refused, no failure of tracking (SDD 6.6)."""
    log = logging.getLogger(LOGGER)
    started = time.monotonic()
    rbd = dict(
        ceph_client=ceph_client,
        pool=pool,
        name_id=name_id,
        base_snapshot_id=base_snapshot_id,
        snapshot_id=snapshot_id,
    )
    try:
        found = _query(shell, ceph_conf, ceph_keyring, size_gib, timeout, rbd)
    except ExtentQueryError as err:
        query_s = round(time.monotonic() - started, 3)
        log.warning(
            "The extent query of volume %s failed after %.3f s (%s): %s",
            volume_id,
            query_s,
            QUERY_FAILED,
            err,
        )
        return {"extents": None, "reason": QUERY_FAILED, "query_s": query_s}
    query_s = round(time.monotonic() - started, 3)
    log.info(
        "The extent query of volume %s found %d extents, %d bytes, in %.3f s",
        volume_id,
        len(found),
        sum(length for _offset, length in found),
        query_s,
    )
    return {"extents": found, "reason": None, "query_s": query_s}
