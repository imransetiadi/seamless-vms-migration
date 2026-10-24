# Copyright: Seamless Migrate contributors
# Apache License 2.0 (see LICENSE)
"""A run's own link from the destination conversion host to the source conversion host (Seamless
Migrate SDD 6.4, 26.6; plan task ST6).

Each pre-copy, sync or cutover run of a workload makes its link and removes it when it ends:

* the key, made on the destination host with ``ssh-keygen -t ed25519`` in a fresh 0700 directory
  ``/tmp/seamless-link-<run>.XXXXXXXXXX``: its private half never leaves that host; beside it, the
  source host's pin (``known_hosts``) when the run pins its hosts;
* the run's blocksync script on the source host, in a fresh 0700 directory of the same name: the
  gate (blocksync.py's ``gate``, SDD 6.2) that the key's forced command runs, checked against its
  SHA-256 before anything runs it (R-02);
* the source host's entry, one line in its user's ``~/.ssh/authorized_keys``, changed under a lock
  and replaced whole (a temporary file renamed)::

      restrict[,port-forwarding,permitopen="127.0.0.1:<port>"...],command="<check>; exec <python>
      <script> gate --python <python> [--volume <id>...]",from="<address>",expiry-time="<time>"
      ssh-ed25519 <key> seamless-link workload=<server id> run=<run>

  ``from=`` is the address the source host saw for the destination host's probe; ``expiry-time``
  the source host's own clock and time zone, without the ``Z`` that OpenSSH reads only since 9.1;
  ``permitopen`` one per export port of a cold run, ``--volume`` one per temporary volume of a
  warm run, written once the export or the snapshot made them.

``present`` makes a link (and first removes the workload's earlier entries, every expired one and
0.4.0's static link) or, given one, writes its ports or volumes; ``absent`` removes it. A link
that fails to come up is removed before the error is raised. No private key reaches this module,
a log line or a result: the result holds paths and the public key alone.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import hashlib
import ipaddress
import json
import logging
import os
import re
import shlex
import tempfile
import uuid

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
    blocksync,
    volume_common,
)
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.volume_common import (
    HOST_KEY_ALIASES,
    HostKeyRefused,
    RemoteCommandFailed,
    RemoteShell,
    link_refused,
    link_shell,
    names_alias,
    refused_host_key,
    refused_link,
)
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.warm_migration import (
    blocksync_source,
    sender_pattern,
)

#: where each host's link directory is made (SDD 26.6)
REMOTE_TMP = "/tmp"
#: a link directory's name before its run id: /tmp/seamless-link-<run>.XXXXXXXXXX
LINK_PREFIX = "seamless-link-"
#: the source user's file sshd reads keys from, unless the operator's sshd reads another
AUTHORIZED_KEYS = "~/.ssh/authorized_keys"
#: an entry's bound when the run has no step timeout (SDD 26.6: 7 days)
DEFAULT_EXPIRY_S = 7 * 24 * 3600
#: 0.4.0's static link on the source host: upstream's blockinfile markers (SDD 26.6)
OLD_LINK_BEGIN = "# BEGIN CONVERSION LINK KEY"
OLD_LINK_END = "# END CONVERSION LINK KEY"
#: 0.4.0's link key on the destination host
OLD_LINK_PRIVATE_KEY = "~/.ssh/id_rsa"
#: the comment that names an entry's workload and run
ENTRY_COMMENT = "seamless-link workload=%s run=%s"
#: the line the probe's exit status follows
PROBE_EXIT = "seamless-link-probe-exit="

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_PYTHON = re.compile(r"^[A-Za-z0-9_./+-]+$")
_RUN = re.compile(r"^[0-9a-f]{32}$")
_PUBLIC_KEY = re.compile(r"^(ssh-ed25519) ([A-Za-z0-9+/]+={0,2})(?: .*)?$")

#: the source host's side of an entry change, run as its user by its python (3.6 or later): the
#: request on stdin, the answer on stdout. Under an exclusive lock on <file>.seamless-lock it drops
#: the run's entry, the workload's earlier ones, every expired one and 0.4.0's block, adds the
#: given entry with its expiry - written in the system time zone, which sshd reads it in - and
#: replaces the file with a 0600 temporary file renamed over it.
EDIT_PROGRAM = r'''
import fcntl, json, os, re, sys, tempfile, time
req = json.load(sys.stdin)
os.environ.pop("TZ", None)
time.tzset()
path = os.path.expanduser(req["path"])
directory = os.path.dirname(path) or "."
if not os.path.isdir(directory):
    os.makedirs(directory, 0o700)
now = time.strftime("%Y%m%d%H%M", time.localtime())
expiry = req.get("expiry")
entry = req.get("entry")
if entry and not expiry:
    expiry = time.strftime("%Y%m%d%H%M", time.localtime(time.time() + int(req["expiry_s"]) + 59))
link = re.compile(r"\sseamless-link workload=(\S+) run=(\S+)\s*$")
expires = re.compile(r'(?:^|,)expiry-time="(\d{12})"')
with open(path + ".seamless-lock", "a") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except (IOError, OSError):
        if os.path.exists(path):
            raise
        lines = []
    old = False
    begin = [i for i, line in enumerate(lines) if line.strip() == req["old_begin"]]
    if begin:
        end = [i for i, line in enumerate(lines) if i > begin[0] and line.strip() == req["old_end"]]
        if end:
            lines = lines[:begin[0]] + lines[end[0] + 1:]
            old = True
    kept, removed = [], []
    for line in lines:
        named = link.search(line)
        if named:
            workload, run = named.groups()
            expiry_of = expires.search(line)
            if run == req["run"] or workload == req["workload"] or (
                expiry_of and expiry_of.group(1) <= now
            ):
                if run != req["run"]:
                    removed.append(run)
                continue
        kept.append(line)
    if entry:
        kept.append(
            ",".join(entry["options"] + ['expiry-time="%s"' % expiry])
            + " " + entry["key"] + " " + entry["comment"]
        )
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".authorized_keys.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("".join(line + "\n" for line in kept))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.rename(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
json.dump({"expiry": expiry, "removed": removed, "old_link": old}, sys.stdout)
'''


class LinkProbeFailed(RuntimeError):
    """The destination host could not reach the source host's gate for a reason that is no
    refusal - the network, the source host's sshd: transient, as an outage is."""


def _checked(pattern, value, what):
    value = "" if value is None else str(value)
    if not pattern.match(value):
        raise ValueError("%s %r is not one the link can name" % (what, value))
    return value


def _sorted_ports(ports):
    checked = []
    for port in ports or ():
        number = int(port)
        if not 0 < number < 65536:
            raise ValueError("port %r is not a TCP port" % (port,))
        if number not in checked:
            checked.append(number)
    return sorted(checked)


def forced_command(link):
    """The command the link key's entry forces: the run's script checked against its SHA-256
    before anything runs it (R-02), then its gate, which admits the probe and the run's own
    sends of the volumes it names (SDD 6.2, 26.6)."""
    script = link["script"]
    python = link["python"]
    gate = ["exec", python, script, "gate", "--python", python]
    for volume in link.get("volume_ids") or ():
        gate += ["--volume", volume]
    return (
        "echo '%s  %s' | sha256sum -c - >/dev/null 2>&1 || { echo '%s%s does not match its"
        " SHA-256' >&2; exit 2; }; %s"
        % (link["sha256"], script, blocksync.GATE_REFUSED, script, " ".join(gate))
    )


def entry_options(link):
    """The entry's options but its expiry, which the source host's clock writes (SDD 26.6)."""
    options = ["restrict"]
    ports = link.get("ports") or ()
    if ports:
        options.append("port-forwarding")
        options += ['permitopen="127.0.0.1:%d"' % port for port in ports]
    options.append('command="%s"' % forced_command(link))
    if link.get("from"):
        options.append('from="%s"' % link["from"])
    return options


class ConversionLink:
    """A workload's link between ``src_host`` and ``dst_host`` - each {id, address}, as
    os_conversion_host_info reports a conversion host - reached from the control plane with the
    conversion key, each host pinned under its side's alias given ``ssh_known_hosts``."""

    def __init__(
        self,
        src_host,
        dst_host,
        ssh_key_path,
        src_ssh_user,
        dst_ssh_user,
        workload,
        ssh_known_hosts=None,
        python_interpreter="python3",
        expiry_s=DEFAULT_EXPIRY_S,
        authorized_keys=AUTHORIZED_KEYS,
        old_link_key_path=None,
        shell_factory=None,
    ):
        self.workload = _checked(_NAME, workload, "the workload")
        self.python = _checked(_PYTHON, python_interpreter, "the python interpreter")
        self.expiry_s = int(expiry_s or DEFAULT_EXPIRY_S)
        if self.expiry_s <= 0:
            raise ValueError("expiry_s must be positive")
        self.src_host = dict(src_host)
        self.dst_host = dict(dst_host)
        self.ssh_key_path = ssh_key_path
        self.src_ssh_user = src_ssh_user
        self.ssh_known_hosts = ssh_known_hosts or None
        self.authorized_keys = authorized_keys or AUTHORIZED_KEYS
        self.old_link_key_path = old_link_key_path or None
        # looked up when the link is made, so that a test's RemoteShell stands in for both hops
        self.shell_factory = shell_factory or volume_common.RemoteShell
        self.log = logging.getLogger("osp-osp")
        self.src = self._shell(self.src_host, src_ssh_user, "source")
        self.dst = self._shell(self.dst_host, dst_ssh_user, "destination")

    def _shell(self, host, ssh_user, side):
        if not self.ssh_known_hosts:
            return self.shell_factory(host["address"], ssh_user, self.ssh_key_path)
        return self.shell_factory(
            host["address"],
            ssh_user,
            self.ssh_key_path,
            known_hosts=self.ssh_known_hosts,
            side=side,
            server=host.get("id"),
        )

    # --- public ------------------------------------------------------------------------------

    def present(self, link=None, volume_ids=None, ports=None):
        """A new link - leftovers removed, the key, the script, the entry, the probe, ``from=`` -,
        or ``link`` with its entry rewritten for ``volume_ids`` or ``ports``. Returns the link."""
        volume_ids = [_checked(_NAME, v, "the volume") for v in volume_ids or ()]
        ports = _sorted_ports(ports)
        if link:
            link = dict(link)
            _checked(_RUN, link.get("id"), "the run")
            link["volume_ids"], link["ports"] = volume_ids, ports
            self._edit(link["id"], entry=self._entry(link), expiry=link["expires"])
            return link
        run = uuid.uuid4().hex
        link = {"id": run, "workload": self.workload, "python": self.python, "volume_ids": [],
                "ports": [], "known_hosts": None}
        try:
            self._remove_leftovers(run)
            self._make_key(link)
            self._install_gate(link)
            link["expires"] = self._edit(run, entry=self._entry(link))["expiry"]
            link["from"] = self._probe(link)
            link["volume_ids"], link["ports"] = volume_ids, ports
            self._edit(run, entry=self._entry(link), expiry=link["expires"])
        except BaseException:
            self._remove_quietly(link)
            raise
        return link

    def absent(self, link=None):
        """Remove ``link`` - its senders, its entry, both directories -, or with none every
        entry of the workload and the directories of their runs. The first failure is raised once
        every step was tried."""
        steps = []
        if link:
            run = _checked(_RUN, link.get("id"), "the run")
            if link.get("script"):
                steps.append(lambda: self._kill_senders(link["script"]))
            steps.append(lambda: self._edit(run))
            for shell, key in ((self.src, "src_dir"), (self.dst, "dst_dir")):
                if link.get(key):
                    steps.append(lambda shell=shell, path=link[key]: self._remove_dir(shell, path))
        else:
            steps.append(lambda: self._remove_runs(self._edit(None)["removed"]))
        first = None
        for step in steps:
            try:
                step()
            except Exception as err:  # pylint: disable=broad-except
                self.log.error("Removing the conversion host link failed: %s", err)
                if first is None:
                    first = err
        if first is not None:
            raise first

    # --- the steps -----------------------------------------------------------------------------

    def _remove_leftovers(self, run):
        """The workload's earlier entries, every expired one and 0.4.0's link, with the
        directories of the runs they name (SDD 26.6 "Leftovers")."""
        done = self._edit(run)
        self._remove_runs(done["removed"])
        if done["old_link"]:
            self.log.info("Removed 0.4.0's conversion link key from the source host")
        self._remove_old_private_key()

    def _remove_runs(self, runs):
        for run in runs:
            if not _RUN.match(run):
                continue  # an entry another tool wrote under our comment: its files are not ours
            for shell in (self.src, self.dst):
                shell.cmd_out(["rm", "-rf", "--", self._run_glob(run)])

    def _remove_old_private_key(self):
        """0.4.0's link key on the destination host, ~/.ssh/id_rsa, goes when it holds the plan's
        old link key - an operator's own key stays (SDD 26.6)."""
        if not self.old_link_key_path:
            return
        try:
            with open(self.old_link_key_path + ".pub", encoding="ascii") as f:
                old = f.read().split()[:2]
        except (IOError, OSError, UnicodeDecodeError):
            return
        try:
            held = self.dst.cmd_out(
                ["ssh-keygen", "-y", "-P", "''", "-f", OLD_LINK_PRIVATE_KEY, "2>/dev/null"]
            ).split()[:2]
        except RemoteCommandFailed:
            return  # none, or a key ssh-keygen cannot read without a passphrase: not ours
        if len(old) == 2 and held == old:
            self.dst.cmd_out(["rm", "-f", OLD_LINK_PRIVATE_KEY])
            self.log.info("Removed 0.4.0's conversion link key from the destination host")

    def _mktemp(self, shell, run):
        template = "%s/%s%s.XXXXXXXXXX" % (REMOTE_TMP, LINK_PREFIX, run)
        path = shell.cmd_out(["mktemp", "-d", shlex.quote(template)]).strip()
        if not path.startswith(template[: -len("XXXXXXXXXX")]) or "\n" in path:
            raise RuntimeError("mktemp on %s made no link directory: %r" % (shell.address, path))
        return path

    def _make_key(self, link):
        link["dst_dir"] = directory = self._mktemp(self.dst, link["id"])
        link["key"] = directory + "/key"
        self.dst.cmd_out(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "''", "-C", "seamless-link", "-f",
             shlex.quote(link["key"])]
        )
        public = self.dst.cmd_out(["cat", shlex.quote(link["key"] + ".pub")]).strip()
        matched = _PUBLIC_KEY.match(public)
        if not matched:
            raise RuntimeError("the link key made on %s is no ed25519 key" % self.dst.address)
        link["public_key"] = "%s %s" % matched.groups()
        if self.ssh_known_hosts:
            link["known_hosts"] = directory + "/known_hosts"
            self.dst.cmd_out(
                ["umask", "077", "&&", "cat", ">", shlex.quote(link["known_hosts"])],
                input=self._source_pins().encode("ascii"),
            )

    def _source_pins(self):
        """The lines of the run's known_hosts that pin the source host, as they are (hashed
        under its alias): the link's own file (SDD 26.4, 26.6)."""
        alias = HOST_KEY_ALIASES["source"]
        with open(self.ssh_known_hosts, encoding="ascii", errors="replace") as f:
            lines = f.read().splitlines()
        pins = []
        for line in lines:
            fields = line.split()
            if len(fields) >= 3 and not fields[0].startswith(("#", "@")):
                if names_alias(fields[0], alias):
                    pins.append(line)
        return "".join(line + "\n" for line in pins)

    def _install_gate(self, link):
        link["src_dir"] = directory = self._mktemp(self.src, link["id"])
        link["script"] = directory + "/blocksync.py"
        source = blocksync_source()
        link["sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
        fd, local = tempfile.mkstemp(prefix="seamless-link-", suffix=".py")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(source)
            status = self.src.scp_to(local, link["script"])
        finally:
            os.unlink(local)
        if status != 0:
            raise RuntimeError(
                "Could not copy the link's gate to %s (scp exit status %s)"
                % (self.src.address, status)
            )

    def _entry(self, link):
        return {
            "options": entry_options(link),
            "key": link["public_key"],
            "comment": ENTRY_COMMENT % (self.workload, link["id"]),
        }

    def _edit(self, run, entry=None, expiry=None):
        """Change the source host's entries under its lock (EDIT_PROGRAM); its answer."""
        request = {
            "path": self.authorized_keys,
            "workload": self.workload,
            "run": run,
            "entry": entry,
            "expiry": expiry,
            "expiry_s": self.expiry_s,
            "old_begin": OLD_LINK_BEGIN,
            "old_end": OLD_LINK_END,
        }
        out = self.src.cmd_out(
            [self.python, "-c", shlex.quote(EDIT_PROGRAM)],
            input=json.dumps(request).encode("utf-8"),
        )
        try:
            answer = json.loads(out)
        except ValueError:
            raise RuntimeError(
                "the source conversion host %s answered no entry change" % self.src.address
            )
        return answer

    def _probe(self, link):
        """From the destination host, through the link - its key, the source host's pin -, the
        gate's answer: the address the source host saw (SDD 26.6). A refused key or link raises
        its refusal; any other failure LinkProbeFailed."""
        source = link_shell(
            self.src_host["address"],
            self.src_ssh_user,
            link,
            known_hosts=self.ssh_known_hosts,
            shell_factory=self.shell_factory,
        )
        command = [shlex.quote(word) for word in source.ssh_preamble()]
        command += ["probe", "2>&1;", "echo", PROBE_EXIT + "$?"]
        lines = self.dst.cmd_out(command).splitlines()
        if not lines or not lines[-1].startswith(PROBE_EXIT):
            raise LinkProbeFailed("the link's probe on %s printed no status" % self.dst.address)
        status = lines[-1][len(PROBE_EXIT):]
        said = "\n".join(lines[:-1])
        address = self.src_host["address"]
        if status == "0":
            answer = said.strip().splitlines()[-1].strip() if said.strip() else ""
            try:
                return str(ipaddress.ip_address(answer))
            except ValueError:
                raise LinkProbeFailed(
                    "the gate on %s answered the probe with no address" % address
                )
        self.log.debug("The link's probe from %s said: %s", self.dst.address, said)
        offered = refused_host_key(said)
        if offered is not None and self.ssh_known_hosts:
            pinned = RemoteShell(
                address,
                self.src_ssh_user,
                known_hosts=self.ssh_known_hosts,
                side="source",
                server=self.src_host.get("id"),
            )
            raise HostKeyRefused(pinned._refusal(offered))  # pylint: disable=protected-access
        reason = refused_link(said, address)
        if reason is not None:
            raise link_refused(address, reason)
        last = said.strip().splitlines()[-1] if said.strip() else "no output"
        raise LinkProbeFailed(
            "the link from the destination conversion host to the source conversion host at %s "
            "failed its probe (ssh exit status %s): %s" % (address, status, last)
        )

    def _kill_senders(self, script):
        """The senders the gate started from the run's script, before their directory goes;
        pkill's 1 (none left) is no failure."""
        self.src.cmd_val(["sudo", "pkill", "-f", shlex.quote(sender_pattern(script))])

    def _remove_dir(self, shell, path):
        if not path.startswith("%s/%s" % (REMOTE_TMP, LINK_PREFIX)):
            raise ValueError("%r is no link directory" % (path,))
        shell.cmd_out(["rm", "-rf", "--", shlex.quote(path)])

    def _run_glob(self, run):
        return shlex.quote("%s/%s%s" % (REMOTE_TMP, LINK_PREFIX, run)) + ".*"

    def _remove_quietly(self, link):
        try:
            self.absent(link)
        except Exception as err:  # pylint: disable=broad-except
            self.log.error("The link that failed to come up was not removed in full: %s", err)
