"""A source conversion host that becomes unreachable mid-export (SDD 6.6, QASuite R-07, review
RV8-01, RV9-02, RV9-04): a lock wait, the source cleanup's SSH check and the waits for the NBD
exports ride out an outage of up to 15 minutes, a reboot or a blip, and the step completes, or its
rescue gives every disk back and the retry completes; an outage longer than that fails the export,
which first attaches the data volumes it detached back to the source VM through the API alone -
the source cleanup cannot run while the host is down, and the rollback that follows starts the
source VM."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import ast
import shutil
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import volume_common
from ansible_collections.os_migrate.os_migrate.plugins.modules import (
    import_workload_export_volumes as export_module,
)
from ansible_collections.os_migrate.os_migrate.plugins.modules import (
    import_workload_src_cleanup as cleanup_module,
)
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake
from ansible_collections.os_migrate.os_migrate.tests.unit.test_cold_export_failure import (
    COMMON,
    DISKS,
    SourceHost,
    _boot_from_volume_vm,
    _holders,
    _image_booted_vm,
)

DATA_VOLUMES = ("vol-data1", "vol-data2")


class Clock:
    """volume_common's time module: sleep moves the clock on at once."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


class RebootingHost(SourceHost):
    """The source conversion host goes down once ``armed``: at the ``at``-th command ``starts``
    matches - by default a take of ``lock`` - or at once on ``go_down()``, for ``outage`` seconds
    of ``clock``. While it is down every ssh to it fails as ssh does - exit status 255 - and each
    try costs the 10 s connect timeout (RemoteShell's ConnectTimeout=10). Back up, its /var/lock
    (/run/lock, a tmpfs) is empty: a lock a module held when the host went down is gone."""

    def __init__(self, cloud, clock, lock, outage, starts=None, at=1):
        super().__init__(cloud)
        self.cloud, self.addresses, self.rebooted = cloud, set(), False
        self.clock, self.lock, self.outage = clock, lock, outage
        self.starts = starts or (
            lambda command: command[:2] == ["sudo", "flock"] and command[6] == lock
        )
        self.at, self.seen = at, 0
        self.armed = False
        self.down_since = None

    def go_down(self):
        self.down_since = self.clock.now

    def down(self):
        if self.down_since is None:
            return False
        if self.clock.now < self.down_since + self.outage:
            return True
        if not self.rebooted:
            self.rebooted = True
            for address in self.addresses:
                self.cloud.held_locks.pop(address, None)
        return False

    def _watch(self, command):
        if self.armed and self.down_since is None and self.starts(command):
            self.seen += 1
            if self.seen == self.at:
                self.go_down()

    def __call__(self, address, ssh_user, key_path=None):
        shell = super().__call__(address, ssh_user, key_path)
        host = self
        host.addresses.add(address)
        val, out, ping = shell.cmd_val, shell.cmd_out, shell.test_ssh_connection

        def unreachable(command):
            host.clock.now += 10
            return subprocess.CalledProcessError(
                255, ["ssh", "%s@%s" % (ssh_user, address)] + list(command)
            )

        def cmd_val(command, **kwargs):
            host._watch(command)
            if host.down():
                host.clock.now += 10
                return 255
            return val(command, **kwargs)

        def cmd_out(command, **kwargs):
            host._watch(command)
            if host.down():
                raise unreachable(command)
            return out(command, **kwargs)

        def test_ssh_connection():
            if host.down():
                raise unreachable(["echo connected"])
            return ping()

        shell.cmd_val, shell.cmd_out, shell.test_ssh_connection = cmd_val, cmd_out, test_ssh_connection
        return shell


def _run(module, args, src, host):
    """One module run as Ansible reports it: its result, or MODULE FAILURE for an exception."""
    try:
        return fake.run_module(module, dict(args), src, host)
    except Exception as err:  # pylint: disable=broad-except
        return {"failed": True, "msg": "MODULE FAILURE: %s" % err}


def _cleanup_args(data, exported):
    return dict(COMMON, data=data, transfer_uuid=exported["transfer_uuid"],
                volume_map=exported["volume_map"])


def _outage(phase, outage, vm="volume-booted"):
    """The export, then - as the role runs them - the block's final cleanup, or the rescue's."""
    src, data = _image_booted_vm() if vm == "image-booted" else _boot_from_volume_vm()
    clock = Clock()
    lock = (volume_common.ATTACH_LOCK_FILE_SOURCE if phase == "export attach lock"
            else volume_common.PORT_LOCK_FILE)
    host = RebootingHost(src, clock, lock, outage)
    common = dict(COMMON, data=data)
    cleaned = rescued = None
    with mock.patch.object(volume_common, "time", clock):
        host.armed = phase == "export attach lock"
        exported = _run(export_module, common, src, host)
        if not exported.get("failed"):
            # the transfer and the destination server are made meanwhile; the block's last task
            host.armed = True
            cleaned = _run(cleanup_module, _cleanup_args(data, exported), src, host)
        if (exported.get("failed") or cleaned.get("failed")) and "volume_map" in exported:
            rescued = _run(cleanup_module, _cleanup_args(data, exported), src, host)
    return src, host, exported, cleaned, rescued


@pytest.mark.parametrize("phase", ["export attach lock", "final cleanup port lock"])
def test_a_two_minute_conversion_host_reboot_is_ridden_out(phase):
    """Review RV8-01: a first window of one minute gave up inside the reboot, and the rollback
    started the source VM without its data volumes. The step now completes, and the final
    cleanup puts the data volumes back on the source VM."""
    src, host, exported, cleaned, _rescued = _outage(phase, outage=120)

    assert not exported.get("failed"), exported
    assert not cleaned.get("failed"), cleaned
    assert host.down_since is not None, "the outage never started"
    for volume_id in DATA_VOLUMES:
        assert _holders(src, volume_id) == ["srv-1"], (volume_id, src.volumes[volume_id].attachments)


def test_an_outage_past_the_window_leaves_the_data_volumes_on_the_source_vm():
    """An outage of 40 minutes that begins while the export waits for the attach lock, the data
    volumes detached: the wait gives up after 15 minutes, and the export attaches them back to the
    source VM through the API before it fails; the rescue's cleanup rides out 15 minutes more,
    then gives up too, and the rollback that follows starts the source VM, which must have every
    data volume."""
    src, _host, exported, _cleaned, rescued = _outage("export attach lock", outage=2400)

    assert exported.get("failed") and "Lock timeout" in exported["msg"], exported
    assert rescued is not None and rescued.get("failed"), rescued  # the host is still down
    assert "SSH timeout: conversion host 10.0.0.10 unreachable over SSH" in rescued["msg"], rescued
    for volume_id in DATA_VOLUMES:
        assert _holders(src, volume_id) == ["srv-1"], (volume_id, src.volumes[volume_id].attachments)
    # the source VM holds exactly its own disks: the root copy the export made is not one of them
    held = sorted(v.id for v in src.volumes.values() if "srv-1" in _holders(src, v.id))
    assert held == sorted(DISKS), held


@pytest.mark.parametrize("phase", ["export, between two attaches", "transfer"])
def test_a_reboot_outside_a_lock_wait_is_ridden_out_by_the_rescue(phase):
    """Review RV9-02: a two-minute reboot that begins outside a lock wait, after the export
    attached volumes to the conversion host - its lsblk between two attaches, or the transfer,
    whose SSH forward dies with the host - fails the step at once. The rescue's cleanup failed at
    once too, on its SSH check, as did the retries 2 s and 4 s later, and the rollback started the
    source VM without the volumes the host held. The cleanup's check now rides out the outage, as
    a lock wait does: the rescue gives every disk back, and the retry completes."""
    src, data = _boot_from_volume_vm()
    clock = Clock()
    # lsblk 1-2 attach the root copy, 3-4 vol-data1: the 5th, before vol-data2, meets the reboot
    host = RebootingHost(src, clock, None, 120, starts=lambda command: command[:1] == ["lsblk"], at=5)
    host.armed = phase.startswith("export")
    common = dict(COMMON, data=data)
    with mock.patch.object(volume_common, "time", clock):
        exported = _run(export_module, common, src, host)
        if phase == "transfer":
            assert not exported.get("failed"), exported
            host.go_down()  # the transfer's SSH forward dies with the host, so the transfer fails
        rescued = _run(cleanup_module, _cleanup_args(data, exported), src, host)
        given_back = {volume_id: _holders(src, volume_id) for volume_id in DISKS}
        left = sorted(set(src.volumes) - set(DISKS)) + sorted(src.snapshots)
        clock.now += 2  # the step's first retry
        retried = _run(export_module, common, src, host)

    assert host.down_since is not None, "the reboot never started"
    assert bool(exported.get("failed")) == (phase != "transfer"), exported
    assert not rescued.get("failed"), rescued
    assert given_back == {volume_id: ["srv-1"] for volume_id in DISKS}, given_back
    assert left == [], left
    assert not retried.get("failed"), retried


def test_the_exports_wait_for_its_nbd_exports_gives_up_after_the_ssh_window():
    """Review RV9-04: the export's wait for its NBD exports to answer retried every failed
    `qemu-img info`, ssh's 255 too, `timeout` times, each try up to the connect timeout: a
    conversion host that went down right after nbdkit started held the stopped source VM for
    about 5.5 h. The wait now gives up once ssh has failed for the window, as a lock wait does."""
    src, data = _boot_from_volume_vm()
    clock = Clock()
    host = RebootingHost(src, clock, None, 10 ** 9,
                         starts=lambda command: command[:2] == ["qemu-img", "info"])
    host.armed = True
    with mock.patch.object(volume_common, "time", clock):
        exported = _run(export_module, dict(COMMON, data=data, timeout=1800), src, host)

    assert exported.get("failed"), exported
    assert "SSH timeout: conversion host 10.0.0.10 unreachable over SSH" in exported["msg"], exported
    assert clock.now - host.down_since <= volume_common.SSH_UNREACHABLE_S + 60


@pytest.mark.parametrize("vm", ["volume-booted", "image-booted"])
def test_an_outage_past_both_windows_leaves_no_root_copy_behind(vm):
    """Review RV9-03: after an outage the cleanup cannot outlast, the export's root copy and its
    snapshot or image - which no server holds, and which the API alone can delete - stayed in the
    source cloud, recorded nowhere (with Ceph a snapshot keeps the boot volume from being
    deleted). The export now deletes them before it fails, as it attaches the data volumes back,
    and never the source VM's own disks."""
    own = set(DISKS) if vm == "volume-booted" else {"vol-data1"}

    src, _host, exported, _cleaned, rescued = _outage("export attach lock", outage=2400, vm=vm)

    assert exported.get("failed") and "Lock timeout" in exported["msg"], exported
    assert rescued is not None and rescued.get("failed"), rescued  # the host is still down
    assert (set(src.volumes), src.snapshots, src.images) == (own, {}, {})
    for volume_id in own:
        assert _holders(src, volume_id) == ["srv-1"], volume_id


def test_a_failed_root_copy_leaves_its_snapshot_to_no_one_even_when_the_cleanup_cannot_run():
    """Review RV9-03: the export failed making the root copy - the root entry still names the
    source VM's own boot volume - and the conversion host is then gone for good, so the rescue
    cannot run. The export deletes the snapshot it made, through the API, and keeps the boot
    volume the source VM holds."""
    src, data = _boot_from_volume_vm()
    src.fail_on["create_volume"] = lambda call: call.get("snapshot_id") is not None
    clock = Clock()
    host = RebootingHost(src, clock, None, 10 ** 9)
    with mock.patch.object(volume_common, "time", clock):
        exported = _run(export_module, dict(COMMON, data=data), src, host)
        host.go_down()
        rescued = _run(cleanup_module, _cleanup_args(data, exported), src, host)

    assert exported.get("failed") and rescued.get("failed"), (exported, rescued)
    assert (set(src.volumes), src.snapshots) == (set(DISKS), {})
    for volume_id in DISKS:
        assert _holders(src, volume_id) == ["srv-1"], volume_id


def test_the_exports_port_probe_gives_up_after_the_ssh_window():
    """Review RV10-03: the probe for a free port, `timeout 1 nc -l PORT` over ssh under the port
    lock, read ssh's exit status 255 as a port in use: a conversion host that went down during it
    made the probe walk all 16383 ephemeral ports, each try up to the 10 s connect timeout - about
    45 h with the source VM stopped - before "No free ports on conversion host!". It now gives up
    after the window, as the other waits do (Seamless Migrate SDD 6.6)."""
    src, data = _boot_from_volume_vm()
    clock = Clock()
    host = RebootingHost(src, clock, None, 10 ** 9,
                         starts=lambda command: command[:3] == ["timeout", "1", "nc"])
    host.armed = True
    with mock.patch.object(volume_common, "time", clock):
        exported = _run(export_module, dict(COMMON, data=data, timeout=1800), src, host)

    assert exported.get("failed"), exported
    assert "SSH timeout: conversion host 10.0.0.10 unreachable over SSH" in exported["msg"], exported
    assert clock.now - host.down_since <= volume_common.SSH_UNREACHABLE_S + 60


class PortOutage(volume_common.SshOutage):
    """An SshOutage subclass with a default prefix of its own."""

    def __init__(self, address, waiting_for, prefix="Port probe gave up"):
        super().__init__(address, waiting_for, prefix)


class LateWordedOutage(volume_common.SshOutage):
    """An SshOutage subclass that words its give-up after SshOutage.__init__ checked the default."""

    def __init__(self, address, waiting_for):
        super().__init__(address, waiting_for)
        self.prefix = "Port probe gave up"


def _reworded_later():
    outage = volume_common.SshOutage("10.0.0.10", "a free port")
    outage.prefix = "Port probe gave up"
    return outage


@pytest.mark.parametrize(
    "build",
    [
        lambda: volume_common.SshOutage("10.0.0.10", "a free port", "Port probe gave up"),
        lambda: volume_common.SshOutage("10.0.0.10", "a free port", prefix="Port probe gave up"),
        lambda: PortOutage("10.0.0.10", "a free port"),
        lambda: LateWordedOutage("10.0.0.10", "a free port"),
        _reworded_later,
    ],
    ids=[
        "passed by position", "passed by keyword", "a subclass's default",
        "set by a subclass after it was built", "assigned later",
    ],
)
def test_an_ssh_outage_takes_only_a_give_up_prefix_the_control_plane_retries(build):
    """Review RV11-05: a give-up reads "{prefix}: conversion host ... unreachable over SSH ...",
    and the control plane retries it by its "timeout" alone (Seamless Migrate SDD 6.6). Its guard
    reads GIVE_UP_PREFIXES, and found a prefix only as the class default or a `prefix=` keyword of
    a call spelled `SshOutage(...)`, so "Port probe gave up: ..." - permanent - slipped past it
    passed by position, through the module or as a subclass's default. An SshOutage now takes no
    prefix but those, whatever form passes it."""
    with pytest.raises(ValueError, match="Port probe gave up"):
        build()
    for prefix in volume_common.GIVE_UP_PREFIXES:
        assert volume_common.SshOutage("10.0.0.10", "a free port", prefix).prefix == prefix


def _message_texts(tree, exempt=frozenset()):
    """Every string a module can build as a message: each literal, an f-string with its fields
    as {}, a `+` chain of strings folded into one, a %-formatted part as its template - so a
    message split across a concatenation is read whole (review RV13-06). Docstrings, and the
    nodes whose ids are in ``exempt``, are left out."""
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }

    def fold(node):
        """(text, ids of the nodes it took) or None when ``node`` builds no string."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, {id(node)}
        if isinstance(node, ast.JoinedStr):
            text, taken = "", {id(node)}
            for value in node.values:
                if isinstance(value, ast.Constant):
                    text += value.value
                    taken.add(id(value))
                else:
                    text += "{}"
            return text, taken
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            left = fold(node.left)
            return (left[0], left[1] | {id(node)}) if left else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = fold(node.left), fold(node.right)
            if left and right:
                return left[0] + right[0], left[1] | right[1] | {id(node)}
        return None

    texts, taken = [], set()
    for node in ast.walk(tree):  # outermost first: a chain is read before its parts
        if id(node) in taken or id(node) in docstrings or id(node) in exempt:
            continue
        folded = fold(node)
        if folded:
            texts.append(folded[0])
            taken |= folded[1]
    return texts


#: where SshOutage, its give-up and the setter that checks the give-up's prefix live
GIVE_UP_HOME = Path("module_utils") / "volume_common.py"
#: the prefixes the control plane retries, and the check every give-up's prefix passes
PREFIX_CHECKS = frozenset({"GIVE_UP_PREFIXES", "_checked_give_up_prefix"})


def _joined(node):
    """The string that ``node`` builds from literals alone - a literal, a `+` chain of them, an
    f-string whose fields are literals - or None."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _joined(node.left), _joined(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts = [
            _joined(value.value if isinstance(value, ast.FormattedValue) else value)
            for value in node.values
        ]
        return "".join(parts) if None not in parts else None
    return None


def _give_up_uses(tree, home):
    """(definition, strays) of SshOutage.GIVE_UP in ``tree``: the string node that defines it in
    class SshOutage, when ``tree`` is its home module, and every other name or attribute
    GIVE_UP. Only SshOutage.seen fills the template, with the prefix its setter checked; read
    anywhere else it takes any prefix, and assigned anywhere else it is another give-up's text
    (review RV14-03). Also strays: the name as a string, and a prefix written past the setter
    (review RV15-03); the name built from literals, and a write to GIVE_UP_PREFIXES or
    _checked_give_up_prefix but the prefixes' own definition, or either name as a string, which
    widens what the setter and seen() take (review RV16-07)."""
    definition, allowed = None, set()
    for cls in ast.walk(tree) if home else ():
        if not (isinstance(cls, ast.ClassDef) and cls.name == "SshOutage"):
            continue
        for stmt in cls.body:
            if (
                isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "GIVE_UP"
                and isinstance(stmt.value, ast.Constant)
            ):
                definition = stmt.value
                allowed.add(id(stmt.targets[0]))
            elif isinstance(stmt, ast.FunctionDef) and stmt.name == "seen":
                allowed |= {id(node) for node in ast.walk(stmt)}
    strays = [
        node for node in ast.walk(tree)
        if id(node) not in allowed
        and (
            (isinstance(node, ast.Name) and node.id == "GIVE_UP")
            or (isinstance(node, ast.Attribute) and node.attr == "GIVE_UP")
            # the template read by its name as a string: getattr, vars(), __dict__ (RV15-03),
            # the name built from literals too (RV16-07)
            or (isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr)) and _joined(node) == "GIVE_UP")
        )
    ]
    # the prefixes widened or their check replaced: any write to either name but the prefixes'
    # definition in the home module, or the name as a string - setattr, vars(), mock (RV16-07)
    definition_of_prefixes = next(
        (
            target for stmt in (tree.body if home else ()) if isinstance(stmt, ast.Assign)
            for target in stmt.targets
            if isinstance(target, ast.Name) and target.id == "GIVE_UP_PREFIXES"
        ),
        None,
    )
    strays += [
        node for node in ast.walk(tree)
        if node is not definition_of_prefixes
        and (
            (
                isinstance(node, (ast.Name, ast.Attribute))
                and (node.id if isinstance(node, ast.Name) else node.attr) in PREFIX_CHECKS
                and isinstance(node.ctx, (ast.Store, ast.Del))
            )
            or (isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr)) and _joined(node) in PREFIX_CHECKS)
        )
    ]
    # a prefix written past the setter: _prefix outside SshOutage, or a subclass's own prefix -
    # a property or a class attribute - which seen() would fill the template with (review RV15-03)
    inside = {
        id(node)
        for cls in (ast.walk(tree) if home else ())
        if isinstance(cls, ast.ClassDef) and cls.name == "SshOutage"
        for node in ast.walk(cls)
    }
    strays += [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "_prefix"
        and isinstance(node.ctx, ast.Store) and id(node) not in inside
    ]
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef) or cls.name == "SshOutage" or not any(
            (isinstance(base, ast.Name) and base.id == "SshOutage")
            or (isinstance(base, ast.Attribute) and base.attr == "SshOutage")
            for base in cls.bases
        ):
            continue
        strays += [
            stmt for stmt in cls.body
            if (isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "prefix")
            or (isinstance(stmt, (ast.Assign, ast.AnnAssign)) and any(
                isinstance(target, ast.Name) and target.id == "prefix"
                for target in (stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target])
            ))
        ]
    return definition, strays


def _unretried_give_ups(plugins):
    """(file, message) for each message under ``plugins`` that says a conversion host is
    "unreachable over" its transport - SSH in the text or passed to it, as by a wording helper's
    text (review RV16-07) - but starts with no GIVE_UP_PREFIXES member, the prefixes the control
    plane retries, and (file, "GIVE_UP at line N") for each stray use of SshOutage's give-up.
    That give-up is exempt only where it is defined: its template, filled by SshOutage.seen,
    takes the prefix the setter checked - a copy of its text, or the template filled elsewhere,
    takes any (review RV14-03)."""
    found = []
    for path in sorted(plugins.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        definition, strays = _give_up_uses(tree, path.relative_to(plugins) == GIVE_UP_HOME)
        found += [(path.name, "a give-up built past its checks, line %d" % node.lineno) for node in strays]
        exempt = {id(definition)} if definition is not None else set()
        for text in _message_texts(tree, exempt):
            if "unreachable over" not in text:
                continue
            if text.split(":", 1)[0] not in volume_common.GIVE_UP_PREFIXES:
                found.append((path.name, text))
    return found


def test_every_hand_built_give_up_carries_a_prefix_the_control_plane_retries():
    """Review RV12-07, RV13-06: a give-up built by hand outside SshOutage - the port map's read
    gives up so - escapes both SshOutage's prefix check and the control plane's guard, which reads
    only GIVE_UP_PREFIXES. Every such message starts with a GIVE_UP_PREFIXES member, or is
    SshOutage's own give-up (Seamless Migrate SDD 6.6)."""
    home = Path(volume_common.__file__).resolve()
    definition = _give_up_uses(ast.parse(home.read_text(encoding="utf-8")), home=True)[0]
    assert definition is not None and definition.value == volume_common.SshOutage.GIVE_UP
    assert home.relative_to(home.parents[1]) == GIVE_UP_HOME
    assert _unretried_give_ups(home.parents[1]) == []


#: the import a module of the collection would write to use SshOutage
IMPORT_SSH_OUTAGE = (
    "from ansible_collections.os_migrate.os_migrate.plugins.module_utils.volume_common import (\n"
    "    SshOutage,\n"
    ")\n"
)
#: and the one it would write to reach the module's names
IMPORT_VOLUME_COMMON = (
    "from ansible_collections.os_migrate.os_migrate.plugins.module_utils import volume_common\n"
)


@pytest.mark.parametrize(
    "source, refused",
    [
        ('M = "%s: conversion host %s unreachable over SSH, probing" % ("Port probe gave up", "h")\n',
         True),
        ('M = "Port probe gave up: conversion host %s unreachable " % "h" + "over SSH"\n', True),
        ('M = f"Port probe gave up: conversion host {ADDRESS} unreachable over SSH"\n', True),
        ('M = f"SSH timeout: conversion host {ADDRESS} unreachable over SSH, reading the map"\n',
         False),
        ('M = "SSH timeout: conversion host %s unreachable " % "h" + "over SSH"\n', False),
        ("from ansible_collections.os_migrate.os_migrate.plugins.module_utils.volume_common import (\n"
         "    SshOutage,\n"
         ")\n"
         'M = SshOutage.GIVE_UP % ("Port probe gave up", "10.0.0.10", 900, "a free port")\n', True),
        ('M = "%s: conversion host %s unreachable over SSH for %d s, waiting for %s" % (\n'
         '    "Port probe gave up", "10.0.0.10", 900, "a free port")\n', True),
        ("class SshOutage:\n"
         '    GIVE_UP = "%s: conversion host %s unreachable over SSH for %d s, waiting for %s"\n'
         "\n"
         "    def seen(self):\n"
         '        return self.GIVE_UP % ("Port probe gave up", "10.0.0.10", 900, "a free port")\n',
         True),
        (IMPORT_SSH_OUTAGE
         + 'M = getattr(SshOutage, "GIVE_UP") % ("Port probe gave up", "10.0.0.10", 900, "a port")\n',
         True),
        (IMPORT_SSH_OUTAGE
         + 'M = vars(SshOutage)["GIVE_UP"] % ("Port probe gave up", "10.0.0.10", 900, "a port")\n',
         True),
        (IMPORT_SSH_OUTAGE
         + 'OUTAGE = SshOutage("10.0.0.10", "a free port")\nOUTAGE._prefix = "Port probe gave up"\n',
         True),
        (IMPORT_SSH_OUTAGE
         + "class PortOutage(SshOutage):\n"
         "    @property\n"
         "    def prefix(self):\n"
         '        return "Port probe gave up"\n',
         True),
        # review RV16-07: the transport as a %-argument, a wording helper, the template's name
        # built from parts, the prefixes widened or their check replaced
        ('M = "Port probe gave up: conversion host %s unreachable over %s, probing" % ("h", "SSH")\n',
         True),
        ('M = "SSH timeout: conversion host %s unreachable over %s, probing" % ("h", "SSH")\n',
         False),
        ("def _unreachable(host, transport):\n"
         '    return "conversion host %s unreachable over %s, probing" % (host, transport)\n'
         "\n"
         "\n"
         'M = "Port probe gave up: " + _unreachable("10.0.0.10", "SSH")\n',
         True),
        (IMPORT_SSH_OUTAGE
         + 'M = vars(SshOutage)["GIVE" + "_UP"] % ("Port probe gave up", "10.0.0.10", 900, "a port")\n',
         True),
        (IMPORT_VOLUME_COMMON + 'volume_common.GIVE_UP_PREFIXES += ("Port probe gave up",)\n', True),
        (IMPORT_VOLUME_COMMON
         + 'setattr(volume_common, "GIVE_UP_PREFIXES", ("Lock timeout", "SSH timeout", "Port"))\n',
         True),
        (IMPORT_VOLUME_COMMON + "volume_common._checked_give_up_prefix = lambda prefix: prefix\n",
         True),
    ],
    ids=[
        "a %s template with another prefix", "the phrase split by +", "an f-string, another prefix",
        "an f-string, a retried prefix", "split by +, a retried prefix",
        "SshOutage.GIVE_UP filled outside SshOutage", "its text copied, another prefix",
        "an SshOutage of its own, outside volume_common",
        "getattr with the name as a string", "vars()", "_prefix written past the setter",
        "a subclass's own prefix property",
        "the transport as a %-argument", "the transport as a %-argument, a retried prefix",
        "a wording helper", "the template's name built from parts", "the prefixes widened",
        "the prefixes replaced by setattr", "the prefix check replaced",
    ],
)
def test_the_give_up_guard_reads_each_message_whole(tmp_path, source, refused):
    """Review RV13-06: the guard took any literal starting with "%s: " for SshOutage's give-up,
    and saw only a literal that held the whole phrase - a give-up split by `+`, or a template
    filled with another prefix, read "Port probe gave up: ...", which the control plane fails as
    permanent, and the guard passed."""
    plugins = tmp_path / "plugins"
    shutil.copytree(Path(volume_common.__file__).resolve().parents[1], plugins)
    (plugins / "modules" / "port_probe.py").write_text(source, encoding="utf-8")

    assert bool(_unretried_give_ups(plugins)) is refused


class PropertyPortOutage(volume_common.SshOutage):
    """A subclass whose prefix property gives what the setter never saw (review RV15-03)."""

    @property
    def prefix(self):
        return "Port probe gave up"

    @prefix.setter
    def prefix(self, value):
        pass


def _written_past_the_setter():
    outage = volume_common.SshOutage("10.0.0.10", "a free port")
    outage._prefix = "Port probe gave up"
    return outage


@pytest.mark.parametrize(
    "build",
    [_written_past_the_setter, lambda: PropertyPortOutage("10.0.0.10", "a free port")],
    ids=["_prefix written past the setter", "a subclass's own prefix property"],
)
def test_seen_refuses_a_give_up_prefix_that_came_past_the_setter(build):
    """Review RV15-03: the setter checks a prefix as it is set, but a prefix written past it -
    _prefix itself, or a subclass's own property - reached the give-up: "Port probe gave up: ...",
    which the control plane fails as permanent. seen() checks the prefix again as it fills the
    template, so the outage tests, which run every give-up, catch such code (Seamless Migrate SDD
    6.6)."""
    outage, clock = build(), Clock()
    with mock.patch.object(volume_common, "time", clock):
        outage.seen(True)
        clock.now += volume_common.SSH_UNREACHABLE_S
        with pytest.raises(ValueError, match="Port probe gave up"):
            outage.seen(True)


def test_only_ssh_outage_seen_fills_its_give_up():
    """Review RV14-03: in its home module too, the template is filled only by SshOutage.seen,
    with the prefix the setter checked; another method that fills it, or a class that assigns a
    GIVE_UP of its own, is a stray."""
    tree = ast.parse(
        "class SshOutage:\n"
        '    GIVE_UP = "%s: conversion host %s unreachable over SSH for %d s, waiting for %s"\n'
        "\n"
        "    def seen(self):\n"
        '        return self.GIVE_UP % (self.prefix, "10.0.0.10", 900, "a free port")\n'
        "\n"
        "    def given_up(self):\n"
        '        return self.GIVE_UP % ("Port probe gave up", "10.0.0.10", 900, "a free port")\n'
        "\n"
        "\n"
        "class PortOutage(SshOutage):\n"
        '    GIVE_UP = "Port probe gave up for %s"\n'
    )

    definition, strays = _give_up_uses(tree, home=True)

    assert definition.value.startswith("%s: conversion host %s unreachable over SSH")
    assert sorted(node.lineno for node in strays) == [8, 12]


class StalePortLock(SourceHost):
    """The source conversion host, reachable, whose port lock file names a module of another
    migrator - one killed in a container since recreated: check_process keeps another host's
    owner, so the lock is never removed (Seamless Migrate SDD 6.6). After ``free`` takes of it,
    every try for the port lock answers 17 ("Another conversion is holding the lock") and costs
    ``rtt`` seconds of ``clock``; the attach lock works."""

    def __init__(self, cloud, clock, free=0, rtt=0.2):
        super().__init__(cloud)
        self.clock, self.free, self.rtt = clock, free, rtt
        self.port_lock_tries = 0
        self.port_lock_tried_at = []  # the clock at each refused try

    def __call__(self, address, ssh_user, key_path=None):
        shell = super().__call__(address, ssh_user, key_path)
        host, out, val = self, shell.cmd_out, shell.cmd_val

        def cmd_out(command, **kwargs):
            if command == ["sudo", "cat", volume_common.PORT_LOCK_FILE + ".lock"]:
                shell.commands.append(list(command))
                return "migrator-2:4242"
            return out(command, **kwargs)

        def cmd_val(command, **kwargs):
            if command[:2] == ["sudo", "flock"] and command[6] == volume_common.PORT_LOCK_FILE:
                host.port_lock_tries += 1
                if host.free > 0:
                    host.free -= 1
                    return val(command, **kwargs)
                shell.commands.append(list(command))
                host.port_lock_tried_at.append(host.clock.now)
                host.clock.now += host.rtt
                return 17
            return val(command, **kwargs)

        shell.cmd_out, shell.cmd_val = cmd_out, cmd_val
        return shell


@pytest.mark.parametrize("free", [0, 1], ids=["no port claimed", "a port claimed first"])
def test_the_rescue_of_an_export_whose_port_lock_wait_gave_up_puts_the_data_volumes_back(free):
    """Review RV13-04: the source cleanup freed the map's ports first, under the port lock - even
    for a map that records none. When another migrator holds that lock, the export's own wait gave
    up after 1800 tries, and the rescue's gave up 1800 tries later and raised before it put
    anything back: the rollback started the source VM without its data volumes. The cleanup now
    puts the volumes back first, logs a release that fails, and takes no port lock for a map with
    no port (Seamless Migrate SDD 6.6)."""
    src, data = _boot_from_volume_vm()
    clock = Clock()
    host = StalePortLock(src, clock, free=free)
    attach, put_back_at = src.attach_volume, []

    def attach_volume(server, volume, device=None, wait=True, timeout=None):
        if server.id == "srv-1":
            put_back_at.append(clock.now)
        return attach(server, volume, device=device, wait=wait, timeout=timeout)

    with mock.patch.object(volume_common, "time", clock):
        exported = _run(export_module, dict(COMMON, data=data, timeout=1800), src, host)
        tries, put_back_at[:] = host.port_lock_tries, []
        src.attach_volume = attach_volume
        rescued = _run(cleanup_module, dict(_cleanup_args(data, exported), timeout=1800), src, host)

    assert exported.get("failed"), exported
    assert "Unable to acquire lock " + volume_common.PORT_LOCK_FILE in exported["msg"], exported
    claimed = [m["port"] for m in exported["volume_map"].values() if m.get("port")]
    assert len(claimed) == free, exported["volume_map"]
    for volume_id in DATA_VOLUMES:
        assert _holders(src, volume_id) == ["srv-1"], (volume_id, rescued.get("msg"))
    assert not rescued.get("failed"), rescued  # a release that fails is logged
    rescue_tries = host.port_lock_tried_at[tries - free:]
    if not claimed:
        assert not rescue_tries, "a map with no port takes no port lock"
    else:  # the volumes went back before the port lock was waited for
        assert put_back_at and max(put_back_at) <= min(rescue_tries), (put_back_at, rescue_tries[:1])


def test_a_rescue_whose_handback_met_an_outage_waits_no_second_window_for_the_port_lock():
    """Review RV14-01: RV13-04 moved the rescue's port release after the handback, with no ssh
    check first. A conversion host that goes down for good once the cleanup's SSH check passed
    makes the handback's attach-lock wait give up after the window, and the release then waited a
    second whole window for the port lock - a host it cannot reach cannot update its port map - the
    source VM stopped meanwhile. One ssh check now decides the release, as a failed transfer's does
    (Seamless Migrate SDD 6.6)."""
    src, data = _boot_from_volume_vm()
    clock = Clock()
    host = RebootingHost(src, clock, None, 10 ** 9, starts=lambda command: command[:2] == ["pgrep", "-f"])
    with mock.patch.object(volume_common, "time", clock):
        exported = _run(export_module, dict(COMMON, data=data, timeout=1800), src, host)
        assert not exported.get("failed"), exported
        host.armed = True
        rescued = _run(cleanup_module, dict(_cleanup_args(data, exported), timeout=1800), src, host)

    assert host.down_since is not None, "the outage never started"
    assert rescued.get("failed") and "Lock timeout" in rescued["msg"], rescued
    for volume_id in DATA_VOLUMES:  # the host keeps them, as before: only the time differs
        assert _holders(src, volume_id) == ["src-conv"], (volume_id, _holders(src, volume_id))
    took = clock.now - host.down_since
    assert took <= volume_common.SSH_UNREACHABLE_S + 120, (took, rescued["msg"])
