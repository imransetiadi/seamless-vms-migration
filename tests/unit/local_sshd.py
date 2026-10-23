"""A real sshd on 127.0.0.1 for the tests that claim a conversion host's key is accepted or refused
(Seamless Migrate SDD 6.6, 26.9). It runs unprivileged, as the test's own user, with host keys, a
client key and an authorized_keys that ssh-keygen made in a temporary directory of its own, and it
stops - the directory removed, keys and all - however the test ends. No network, no key from
outside the test."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import base64
import hashlib
import hmac
import os
import pwd
import shutil
import socket
import subprocess
import tempfile
import time

#: the sshd binary (openssh-server; the GitHub runner image installs it with its `ssh` package)
SSHD = next(
    (path for path in ("/usr/sbin/sshd", "/usr/local/sbin/sshd") if os.access(path, os.X_OK)),
    shutil.which("sshd"),
)

#: how long sshd may take to listen
START_S = 15


def user():
    """The test's own user: the only one an unprivileged sshd lets in."""
    return pwd.getpwuid(os.getuid()).pw_name


def keygen(path, key_type="ed25519"):
    """A key pair ssh-keygen makes at ``path`` (no passphrase); returns its public key's line."""
    size = ["-b", "2048"] if key_type == "rsa" else []
    subprocess.run(
        ["ssh-keygen", "-q", "-t", key_type] + size + ["-N", "", "-C", "seamless-test", "-f", path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
    )
    with open(path + ".pub", encoding="ascii") as f:
        return f.read().strip()


def fingerprint(public_key_path):
    """The SHA256 fingerprint of the public key in ``public_key_path``, as ssh-keygen -l prints it."""
    listed = subprocess.run(
        ["ssh-keygen", "-l", "-E", "sha256", "-f", public_key_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=True,
    )
    return listed.stdout.split()[1]


def hashed_entry(alias, public_key):
    """The known_hosts line that pins ``public_key`` under ``alias``, the name hashed as
    HashKnownHosts writes it: |1|salt|HMAC-SHA1(salt, name)."""
    salt = os.urandom(20)
    digest = hmac.new(salt, alias.encode("ascii"), hashlib.sha1).digest()
    key_type, blob = public_key.split()[:2]
    return "|1|%s|%s %s %s" % (
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
        key_type,
        blob,
    )


def write_known_hosts(path, entries):
    """A known_hosts file of hashed entries, one per (alias, public key line) pair; returns its
    path."""
    with open(path, "w", encoding="ascii") as f:
        for alias, public_key in entries:
            f.write(hashed_entry(alias, public_key) + "\n")
    return path


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LocalSshd:
    """``sshd -D -e`` on a free port of 127.0.0.1 with a configuration of its own: its host keys,
    the client key it lets in, no PAM, no strict modes, no pid file, and no per-source penalties
    where the sshd knows them (OpenSSH 9.8 and later drop 127.0.0.1 for a while after a dozen
    connections that never authenticate - every refused host key is one)."""

    def __init__(self, host_key_types=("ed25519",)):
        self.directory = tempfile.mkdtemp(prefix="seamless-sshd-")
        self.process = None
        self.port = None
        self.user = user()
        self.host_keys = {}
        try:
            for key_type in host_key_types:
                self.host_keys[key_type] = keygen(self.path("host_" + key_type), key_type)
            self.client_key = self.path("client")
            with open(self.path("authorized_keys"), "w", encoding="ascii") as f:
                f.write(keygen(self.client_key) + "\n")
            self._start()
        except BaseException:
            self.stop()
            raise

    def path(self, name):
        return os.path.join(self.directory, name)

    def host_key_path(self, key_type="ed25519"):
        """The public half of the host key of ``key_type`` this sshd presents."""
        return self.path("host_%s.pub" % key_type)

    def _config(self, port, penalties):
        lines = ["ListenAddress 127.0.0.1", "Port %d" % port]
        lines += ["HostKey " + self.path("host_" + key_type) for key_type in self.host_keys]
        lines += [
            "AuthorizedKeysFile " + self.path("authorized_keys"),
            "UsePAM no",
            "StrictModes no",
            "PidFile none",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "PrintMotd no",
            "Subsystem sftp internal-sftp",
        ]
        if penalties:
            lines.append("PerSourcePenalties no")
        path = self.path("sshd_config")
        with open(path, "w", encoding="ascii") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def _start(self):
        for _attempt in range(3):  # another process may take the free port first
            port = _free_port()
            config = self._config(port, penalties=True)
            checked = subprocess.run(
                [SSHD, "-t", "-f", config],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if checked.returncode != 0:  # an sshd before OpenSSH 9.8 knows no PerSourcePenalties
                config = self._config(port, penalties=False)
            with open(self.path("sshd.log"), "w", encoding="utf-8") as log:
                self.process = subprocess.Popen(  # pylint: disable=consider-using-with
                    [SSHD, "-D", "-e", "-f", config],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            if self._listening(port):
                self.port = port
                return
            self._end_process()
        raise RuntimeError("sshd did not start: " + self.log())

    def _listening(self, port):
        deadline = time.monotonic() + START_S
        while time.monotonic() < deadline:
            if "Server listening on 127.0.0.1 port %d" % port in self.log():
                return True
            if self.process.poll() is not None:
                return False
            time.sleep(0.05)
        return False

    def log(self):
        """What sshd logged so far."""
        try:
            with open(self.path("sshd.log"), encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def _end_process(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def stop(self):
        """Stop sshd and remove its directory, keys and all."""
        try:
            self._end_process()
        finally:
            shutil.rmtree(self.directory, ignore_errors=True)
