"""A run's own link between the conversion hosts (Seamless Migrate SDD 6.4, 26.6; plan task ST6).

Two conversion hosts on this machine: each a home and a temporary directory of its own, where the
link's commands really run - sh, mktemp, ssh-keygen, sha256sum, the source host's entry change
and the gate of the run's own blocksync script. Only the hop between them is simulated (the
destination host's ssh to the source host's sshd: its key, from=, the forced command); the last
test runs every hop through a real sshd on 127.0.0.1 (local_sshd.py)."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

import pytest

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
    conversion_link,
    volume_common,
)
from ansible_collections.os_migrate.os_migrate.tests.unit import local_sshd

#: what the module sees as each host's temporary directory: every host maps it to its own
REMOTE_TMP = "/seamless-test-remote-tmp"
SRC = {"id": "src-conv", "address": "192.0.2.10"}
DST = {"id": "dst-conv", "address": "192.0.2.20"}
#: the address the source host sees for the destination host (a NAT's, say)
SEEN = "198.51.100.23"
WORKLOAD = "3f1c2b7a-0d4e-4a51-9e8f-6b2c1d0e9a87"
VOLUMES = ["0b5e7f1c-9a2d-4e6b-8c1f-3d2a1b0c9e8f", "7d3c2b1a-0f9e-4d8c-b7a6-5e4d3c2b1a0f"]
ENTRY = re.compile(r"^(?P<options>.*) (?P<key>ssh-ed25519 \S+) (?P<comment>seamless-link .*)$")


def keygen(path):
    """A key pair ssh-keygen makes at ``path``; returns its public line."""
    return local_sshd.keygen(str(path))


def options_of(line):
    """An authorized_keys line's options, split at the commas outside quotes."""
    options, word, quoted = [], "", False
    for char in ENTRY.match(line).group("options"):
        if char == '"':
            quoted = not quoted
        if char == "," and not quoted:
            options.append(word)
            word = ""
        else:
            word += char
    return options + [word]


class Host:
    """A conversion host of the tests: its home - every command's HOME, so ~/.ssh is its own -,
    its temporary directory (REMOTE_TMP), and every command it ran."""

    def __init__(self, root, name, address):
        self.name = name
        self.address = address
        self.home = os.path.join(str(root), name, "home")
        self.tmp = os.path.join(str(root), name, "tmp")
        os.makedirs(os.path.join(self.home, ".ssh"), 0o700)
        os.makedirs(self.tmp)
        self.commands = []
        self.inputs = []

    def local(self, text):
        return text.replace(REMOTE_TMP, self.tmp)

    def remote(self, text):
        return text.replace(self.tmp, REMOTE_TMP)

    @property
    def authorized_keys(self):
        return os.path.join(self.home, ".ssh", "authorized_keys")

    def entries(self):
        if not os.path.exists(self.authorized_keys):
            return []
        with open(self.authorized_keys, encoding="ascii") as f:
            return f.read().splitlines()

    def link_entries(self):
        return [line for line in self.entries() if " seamless-link " in line]

    def links(self):
        """The link directories in this host's temporary directory."""
        return sorted(name for name in os.listdir(self.tmp) if name.startswith("seamless-link-"))

    def environment(self, **extra):
        env = dict(
            PATH=os.pathsep.join([os.path.dirname(sys.executable), "/usr/bin", "/bin",
                                  "/usr/sbin", "/sbin"]),
            HOME=self.home,
            LC_ALL="C",
        )
        env.update(extra)
        return env

    def sh(self, text, stdin=None, **extra):
        return subprocess.run(
            ["/bin/sh", "-c", self.local(text)],
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment(**extra),
            timeout=60,
            check=False,
        )


class World:
    """The two hosts, and what the hop between them does: the source host's sshd lets the
    destination host's link key in by its entry - its from=, its expiry - and runs its forced
    command, or refuses as ``refuse`` says ("key": the source host's key is not the pinned one;
    "denied": a refused login)."""

    def __init__(self, root):
        self.src = Host(root, "src", SRC["address"])
        self.dst = Host(root, "dst", DST["address"])
        self.refuse = None
        self.seen = SEEN
        self.probes = []
        self.shells = []

    def host(self, address):
        return self.src if address == self.src.address else self.dst

    def factory(self, address, ssh_user, key_path=None, **pin):
        shell = LocalShell(self, self.host(address), ssh_user, key_path, pin)
        self.shells.append(shell)
        return shell

    def hop(self, words):
        """The destination host's ssh to the source host: what it prints, its exit status."""
        user_host = next(w for w in words if "@" in w)
        command = words[words.index(user_host) + 1:]
        key = words[words.index("-i") + 1]
        self.probes.append({"words": words, "command": command})
        if self.refuse == "key":
            return "Host key verification failed.\n", 255
        if self.refuse == "gate":  # the forced command's check: the copy is not the run's script
            return "seamless-link gate refused: /tmp/x/blocksync.py does not match its SHA-256\n", 2
        with open(self.dst.local(key) + ".pub", encoding="ascii") as f:
            public = " ".join(f.read().split()[:2])
        for line in self.src.link_entries():
            entry = ENTRY.match(line)
            if entry.group("key") != public or self.refuse == "denied":
                continue
            options = options_of(line)
            if 'from="%s"' % self.seen not in options and any(o.startswith("from=") for o in options):
                continue
            expiry = [o for o in options if o.startswith("expiry-time=")]
            if expiry and expiry[0].split('"')[1] <= time.strftime("%Y%m%d%H%M"):
                continue
            forced = [o for o in options if o.startswith("command=")][0][len('command="'):-1]
            run = self.src.sh(
                forced,
                SSH_ORIGINAL_COMMAND=" ".join(command),
                SSH_CONNECTION="%s 50122 %s 22" % (self.seen, self.src.address),
            )
            return run.stdout.decode() + run.stderr.decode(), run.returncode
        return "%s: Permission denied (publickey).\n" % user_host, 255


class LocalShell:
    """RemoteShell's stand-in on a Host: commands run in sh with the host's HOME and temporary
    directory; sudo and the hop to the other host are the World's."""

    def __init__(self, world, host, ssh_user, key_path, pin):
        self.world = world
        self.host = host
        self.address = host.address
        self.ssh_user = ssh_user
        self.key_path = key_path
        self.pin = pin

    def ssh_preamble(self):
        """The link's preamble, as link_shell's RemoteShell writes it: its key, its pin."""
        words = ["ssh", "-o", "BatchMode=yes", "-i", self.key_path]
        if self.pin.get("known_hosts"):
            words += ["-o", "UserKnownHostsFile=" + self.pin["known_hosts_at"],
                      "-o", "HostKeyAlias=" + volume_common.HOST_KEY_ALIASES[self.pin["side"]]]
        return words + ["%s@%s" % (self.ssh_user, self.address)]

    def _run(self, command, stdin=None):
        self.host.commands.append(list(command))
        if stdin is not None:
            self.host.inputs.append(stdin)
        words = shlex.split(" ".join(command).split(" 2>&1;", maxsplit=1)[0]) if command else []
        if words[:1] == ["sudo"]:
            return "", "", 1  # pkill finding nothing
        if words[:1] == ["ssh"] and "probe" in command:
            said, status = self.world.hop(words)
            return "%s%s%d\n" % (said, conversion_link.PROBE_EXIT, status), "", 0
        run = self.host.sh(" ".join(command), stdin=stdin)
        return self.host.remote(run.stdout.decode()), run.stderr.decode(), run.returncode

    def cmd_out(self, command, input=None, **kwargs):
        out, err, status = self._run(command, input)
        if status:
            raise volume_common.RemoteCommandFailed(status, list(command), out, err)
        return out.strip()

    def cmd_val(self, command, **kwargs):
        return self._run(command)[2]

    def scp_to(self, source, destination):
        self.host.commands.append(["scp", source, destination])
        shutil.copyfile(source, self.host.local(destination))
        return 0

    def test_ssh_connection(self):
        self.cmd_out(["echo connected"])


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(conversion_link, "REMOTE_TMP", REMOTE_TMP)
    return World(tmp_path)


def make_link(world, tmp_path, known_hosts=True, **options):
    run_file = None
    if known_hosts:
        run_file = str(tmp_path / "known_hosts")
        if not os.path.exists(run_file):
            src_key = keygen(tmp_path / "src_host_key")
            dst_key = keygen(tmp_path / "dst_host_key")
            local_sshd.write_known_hosts(
                run_file, [("os_migrate_conv_src", src_key), ("os_migrate_conv_dst", dst_key)]
            )
    given = dict(
        ssh_key_path="/keys/conversion",
        src_ssh_user="cloud-user",
        dst_ssh_user="ubuntu",
        workload=WORKLOAD,
        ssh_known_hosts=run_file,
        python_interpreter=sys.executable,
        shell_factory=world.factory,
    )
    given.update(options)
    return conversion_link.ConversionLink(SRC, DST, **given)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_the_link_key_is_made_on_the_destination_host_and_never_copied(world, tmp_path):
    """SDD 26.6 (owner decision 5): each run makes its own ed25519 key on the destination host,
    in a fresh 0700 directory there; its private half never leaves that host - no command reads
    it, nothing copies it, the result names its path and its public half alone - and the source
    host holds only the public half, in its user's entry."""
    link = make_link(world, tmp_path).present()

    [directory] = world.dst.links()
    assert directory.startswith("seamless-link-%s." % link["id"])
    local_dir = os.path.join(world.dst.tmp, directory)
    assert mode(local_dir) == 0o700
    assert link["dst_dir"] == REMOTE_TMP + "/" + directory
    assert link["key"] == link["dst_dir"] + "/key"
    private = os.path.join(local_dir, "key")
    with open(private, encoding="ascii") as f:
        private_text = f.read()
    assert "PRIVATE KEY" in private_text and mode(private) == 0o600
    with open(private + ".pub", encoding="ascii") as f:
        public = " ".join(f.read().split()[:2])
    assert link["public_key"] == public and public.startswith("ssh-ed25519 ")

    body = private_text.splitlines()[1]
    seen = json.dumps(link) + json.dumps(world.src.commands + world.dst.commands)
    seen += "".join(i.decode() for i in world.src.inputs + world.dst.inputs)
    assert body not in seen
    assert not [c for c in world.dst.commands if c[:1] == ["cat"] and not c[1].endswith(".pub")]
    assert not [c for c in world.src.commands + world.dst.commands if c[:1] == ["scp"]
                and "/key" in c[2]]
    [entry] = world.src.link_entries()
    assert public in entry
    assert "key" not in os.listdir(os.path.join(world.src.tmp, world.src.links()[0]))


def test_the_source_entry_holds_restrict_the_gate_and_an_expiry(world, tmp_path):
    """SDD 26.6: the source user's entry holds restrict, the forced command - the run's script
    checked against its SHA-256 (R-02), then its gate with the interpreter and, once the snapshot
    made them, the run's temporary volumes - and an expiry-time the source host's clock wrote,
    the run's start plus the step timeout, in its own time zone without the Z OpenSSH 8.0 does not
    read; its comment names the workload and the run. The file stays 0600."""
    started = time.time()
    link = make_link(world, tmp_path, expiry_s=3600).present()
    link = make_link(world, tmp_path, expiry_s=3600).present(link, volume_ids=VOLUMES)

    [line] = world.src.link_entries()
    entry = ENTRY.match(line)
    assert entry.group("comment") == "seamless-link workload=%s run=%s" % (WORKLOAD, link["id"])
    options = options_of(line)
    assert options[0] == "restrict"
    assert not [o for o in options if o.startswith(("port-forwarding", "permitopen"))]
    [command] = [o for o in options if o.startswith("command=")]
    script = link["script"]
    assert script == link["src_dir"] + "/blocksync.py"
    with open(world.src.local(script), "rb") as f:
        import hashlib

        assert hashlib.sha256(f.read()).hexdigest() == link["sha256"]
    assert command == (
        'command="echo \'%s  %s\' | sha256sum -c - >/dev/null 2>&1 || { echo \'seamless-link '
        "gate refused: %s does not match its SHA-256' >&2; exit 2; }; exec %s %s gate --python %s"
        ' --volume %s --volume %s"'
        % (link["sha256"], script, script, sys.executable, script, sys.executable, *VOLUMES)
    )
    [expiry] = [o for o in options if o.startswith("expiry-time=")]
    assert re.match(r'^expiry-time="\d{12}"$', expiry), expiry
    expires = time.mktime(time.strptime(expiry.split('"')[1], "%Y%m%d%H%M"))
    assert started + 3600 <= expires + 60 and expires <= time.time() + 3600 + 120
    assert link["expires"] == expiry.split('"')[1]
    assert mode(world.src.authorized_keys) == 0o600
    assert mode(world.src.local(link["src_dir"])) == 0o700

    # a script changed after the copy is never run: the forced command refuses it
    with open(world.src.local(script), "a", encoding="utf-8") as f:
        f.write("\n# changed\n")
    said, status = world.hop(["ssh", "-i", link["key"], "cloud-user@" + SRC["address"], "probe"])
    assert status == 2 and "seamless-link gate refused: %s does not match its SHA-256" % (
        world.src.local(script)) in said


def test_the_probe_adds_the_address_the_source_host_saw_as_from(world, tmp_path):
    """SDD 26.6: the destination host probes the link - its own key, the source host's pin in
    the link's known_hosts beside the key - and the gate answers the address the source host saw
    in SSH_CONNECTION; the entry then names it in from=. The entry the probe met had no from= yet:
    no topology is guessed. A gate that answers no address fails the link."""
    link = make_link(world, tmp_path).present()

    assert link["from"] == SEEN
    [line] = world.src.link_entries()
    assert 'from="%s"' % SEEN in options_of(line)
    [probe] = world.probes
    assert probe["command"] == ["probe"]
    assert probe["words"][probe["words"].index("-i") + 1] == link["key"]
    assert "UserKnownHostsFile=" + link["known_hosts"] in probe["words"]
    assert "HostKeyAlias=os_migrate_conv_src" in probe["words"]
    with open(world.dst.local(link["known_hosts"]), encoding="ascii") as f:
        pins = f.read().splitlines()
    with open(str(tmp_path / "known_hosts"), encoding="ascii") as f:
        assert pins == [f.read().splitlines()[0]]  # the source host's pin alone
    assert mode(world.dst.local(link["known_hosts"])) == 0o600

    world.seen = "not-an-address"
    with pytest.raises(conversion_link.LinkProbeFailed, match="answered the probe with no address"):
        make_link(world, tmp_path).present()


def test_a_cold_entry_permits_only_the_runs_export_ports(world, tmp_path):
    """SDD 26.6: once the export claimed its ports, a cold run's entry allows port forwarding to
    127.0.0.1 on those ports alone - one permitopen each, no wildcard, no other host name - and
    before that, none; a warm entry never forwards."""
    first = make_link(world, tmp_path).present()
    [line] = world.src.link_entries()
    assert not [o for o in options_of(line) if "port" in o.split("=")[0]]

    make_link(world, tmp_path).present(first, ports=[10810, 10809, 10809])

    [line] = world.src.link_entries()
    options = options_of(line)
    assert options[:4] == ["restrict", "port-forwarding", 'permitopen="127.0.0.1:10809"',
                           'permitopen="127.0.0.1:10810"']
    assert [o for o in options if o.startswith("permitopen")] == options[2:4]
    assert "*" not in line and "localhost" not in line
    with pytest.raises(ValueError):
        make_link(world, tmp_path).present(first, ports=[70000])


def test_the_link_is_removed_when_the_run_fails(world, tmp_path):
    """SDD 26.6: a link the source host refuses - a probe denied, a source host whose key is not
    the pinned one, the gate's refusal (also without known_hosts) - fails at once with its
    refusal and leaves nothing behind: no entry, no directory on either host. A link that came up is removed when its run ends: its senders
    stopped, its entry and both directories gone."""
    world.refuse = "denied"
    with pytest.raises(volume_common.LinkRefused, match=r"^link refused by the source conversion "
                       r"host \(os_migrate_conv_src at 192\.0\.2\.10\): Permission denied"):
        make_link(world, tmp_path).present()
    assert world.src.link_entries() == [] and world.src.links() == [] and world.dst.links() == []

    world.refuse = "key"
    with pytest.raises(volume_common.HostKeyRefused, match=r"^host key refused: the source "
                       r"conversion host \(os_migrate_conv_src, server src-conv at 192\.0\.2\.10\)"):
        make_link(world, tmp_path).present()
    assert world.src.link_entries() == [] and world.src.links() == [] and world.dst.links() == []

    world.refuse = "gate"
    with pytest.raises(volume_common.LinkRefused, match=r"\): its gate refused /tmp/x/blocksync\.py "
                       r"does not match its SHA-256$"):
        make_link(world, tmp_path, known_hosts=False).present()
    assert world.src.link_entries() == [] and world.src.links() == [] and world.dst.links() == []

    world.refuse = None
    link = make_link(world, tmp_path).present(volume_ids=VOLUMES[:1])
    assert world.src.link_entries() and world.src.links() and world.dst.links()
    del world.src.commands[:]
    make_link(world, tmp_path).absent(link)
    assert world.src.link_entries() == [] and world.src.links() == [] and world.dst.links() == []
    [pkill] = [c for c in world.src.commands if c[:2] == ["sudo", "pkill"]]
    assert shlex.split(pkill[3]) == [conversion_link.sender_pattern(link["script"])]
    assert re.search(shlex.split(pkill[3])[0], link["script"] + " send --device /dev/vdb")
    assert not re.search(shlex.split(pkill[3])[0], "sudo pkill -f " + pkill[3])


def test_earlier_and_expired_entries_are_removed_first(world, tmp_path):
    """SDD 26.6 "Leftovers": a link first removes its workload's earlier entries - a run a crash
    left - and every expired one, with their runs' directories on both hosts; an operator's key
    and another workload's live entry stay. The file is replaced whole, 0600."""
    operator = keygen(tmp_path / "operator")
    other_key = keygen(tmp_path / "other").split()[:2]
    future = time.strftime("%Y%m%d%H%M", time.localtime(time.time() + 86400))
    past = time.strftime("%Y%m%d%H%M", time.localtime(time.time() - 120))

    def entry(workload, run, expiry):
        return 'restrict,command="true",expiry-time="%s" %s %s seamless-link workload=%s run=%s' % (
            expiry, other_key[0], other_key[1], workload, run)

    runs = {"earlier": "a" * 32, "other": "b" * 32, "expired": "c" * 32}
    other_workload = "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b"
    lines = [operator, entry(WORKLOAD, runs["earlier"], future),
             entry(other_workload, runs["other"], future),
             entry(other_workload, runs["expired"], past)]
    with open(world.src.authorized_keys, "w", encoding="ascii") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(world.src.authorized_keys, 0o644)
    for host in (world.src, world.dst):
        for run in runs.values():
            os.mkdir(os.path.join(host.tmp, "seamless-link-%s.0123456789" % run))
    before = os.stat(world.src.authorized_keys).st_ino

    link = make_link(world, tmp_path).present()

    after = world.src.entries()
    assert after[:2] == [operator, entry(other_workload, runs["other"], future)]
    assert len(after) == 3 and "run=%s" % link["id"] in after[2]
    for host in (world.src, world.dst):
        assert [name.split(".")[0] for name in host.links()] == sorted(
            ["seamless-link-" + runs["other"], "seamless-link-" + link["id"]])
    assert os.stat(world.src.authorized_keys).st_ino != before
    assert mode(world.src.authorized_keys) == 0o600

    make_link(world, tmp_path).absent()  # no link: every entry of the workload
    assert world.src.entries() == [operator, entry(other_workload, runs["other"], future)]


def test_the_old_static_link_is_removed_but_an_operators_id_rsa_is_kept(world, tmp_path):
    """SDD 26.6, 26.8: the first link on a host pair removes 0.4.0's link - the CONVERSION LINK
    KEY block on the source host, and the destination's ~/.ssh/id_rsa when it holds the plan's old
    link key -; an operator's own id_rsa stays, and so does every line outside the block."""
    old_key = tmp_path / "plan" / "link-ssh.key"
    old_key.parent.mkdir()
    old_public = keygen(old_key)
    operator = keygen(tmp_path / "operator")
    with open(world.src.authorized_keys, "w", encoding="ascii") as f:
        f.write("%s\n%s\n%s\n%s\n" % (operator, conversion_link.OLD_LINK_BEGIN, old_public,
                                      conversion_link.OLD_LINK_END))
    dst_rsa = os.path.join(world.dst.home, ".ssh", "id_rsa")
    shutil.copyfile(str(old_key), dst_rsa)
    os.chmod(dst_rsa, 0o600)

    link = make_link(world, tmp_path, old_link_key_path=str(old_key)).present()

    assert world.src.entries()[0] == operator
    assert old_public not in world.src.entries()
    assert len(world.src.entries()) == 2
    assert not os.path.exists(dst_rsa)

    make_link(world, tmp_path).absent(link)
    keygen(tmp_path / "operators_rsa")
    shutil.copyfile(str(tmp_path / "operators_rsa"), dst_rsa)
    os.chmod(dst_rsa, 0o600)
    make_link(world, tmp_path, old_link_key_path=str(old_key)).present()
    assert os.path.exists(dst_rsa)  # the operator's own key
    shutil.copyfile(str(old_key), dst_rsa)
    make_link(world, tmp_path).present()
    assert os.path.exists(dst_rsa)  # no plan key to compare with: kept


class LinkSshd(local_sshd.LocalSshd):
    """The test sshd as both conversion hosts: sessions find a fake sudo first on their PATH,
    which records what the gate would run as root."""

    def __init__(self, bin_dir):
        self.bin_dir = bin_dir
        super(LinkSshd, self).__init__()

    def _config(self, port, penalties):
        config = super(LinkSshd, self)._config(port, penalties)
        with open(config, "a", encoding="ascii") as f:
            f.write("SetEnv PATH=%s:/usr/bin:/bin:/usr/sbin:/sbin\n" % self.bin_dir)
        return config


@pytest.mark.skipif(not local_sshd.SSHD, reason="needs sshd (openssh-server)")
def test_a_link_to_a_real_sshd_runs_only_the_gate(tmp_path, monkeypatch):
    """SDD 26.6, 26.9 (S-11): through a real sshd, the run's link key - made on the destination
    host, its entry written by the module - runs the gate and nothing else: the probe answers;
    a command, a shell, a copy, a forward and a send of another volume are refused; once the run
    removed the link, and once its entry expired, the key no longer logs in."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sudo").write_text("#!/bin/sh\necho \"sudo $*\" >> %s\n" % (tmp_path / "sudo.log"))
    (bin_dir / "sudo").chmod(0o755)
    remote_tmp = tmp_path / "remote-tmp"
    remote_tmp.mkdir()
    monkeypatch.setattr(conversion_link, "REMOTE_TMP", str(remote_tmp))
    sshd = LinkSshd(str(bin_dir))
    try:
        port = sshd.port

        class Shell(volume_common.RemoteShell):
            def _default_options(self):
                return super(Shell, self)._default_options() + ["-o", "Port=%d" % port]

        run_file = local_sshd.write_known_hosts(
            str(tmp_path / "known_hosts"),
            [(alias, sshd.host_keys["ed25519"]) for alias in volume_common.HOST_KEY_ALIASES.values()],
        )
        host = {"id": "conv", "address": "127.0.0.1"}
        link = conversion_link.ConversionLink(
            host, host, sshd.client_key, sshd.user, sshd.user, WORKLOAD,
            ssh_known_hosts=run_file, python_interpreter=sys.executable,
            authorized_keys=sshd.path("authorized_keys"), shell_factory=Shell,
        )
        made = link.present()
        assert made["from"] == "127.0.0.1"
        made = link.present(made, volume_ids=VOLUMES[:1])

        def through(*words, **kwargs):
            shell = volume_common.link_shell(
                "127.0.0.1", sshd.user, made, known_hosts=run_file, shell_factory=Shell)
            preamble = shell.ssh_preamble()
            return subprocess.run(
                preamble[:1] + ["-T"] + preamble[1:] + list(words),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=60, check=False, **kwargs)

        probe = through("probe")
        assert (probe.returncode, probe.stdout) == (0, "127.0.0.1\n"), probe.stderr
        for words, refused in (
            (["id"], "a command it does not run: id"),
            ([], "a shell"),
            (["scp", "-t", "/tmp"], "a command it does not run: scp"),
            (["sudo", "-n", sys.executable, made["script"], "send", "--device", "/dev/null",
              "--chunk-size", "4194304", "--workers", "4", "--expect-serial", VOLUMES[1]],
             "volume %s, which is not the run's" % VOLUMES[1]),
        ):
            run = through(*words)
            assert run.returncode == 2, (words, run.stdout, run.stderr)
            assert "seamless-link gate refused: " + refused in run.stderr
        forward = through("-W", "127.0.0.1:%d" % port)
        assert forward.returncode == 255 and "administratively prohibited" in forward.stderr
        assert not (tmp_path / "sudo.log").exists()

        link._edit(made["id"], entry=link._entry(made), expiry="202001010000")  # expired
        assert through("probe").returncode == 255
        link._edit(made["id"], entry=link._entry(made), expiry=made["expires"])
        assert through("probe").returncode == 0
        kept = tmp_path / "kept"  # the key and its pin, kept to try them once the link is gone
        kept.mkdir(mode=0o700)
        for name in ("key", "known_hosts"):
            shutil.copy(os.path.join(made["dst_dir"], name), str(kept / name))
        link.absent(made)
        made = dict(made, key=str(kept / "key"), known_hosts=str(kept / "known_hosts"))
        gone = through("probe")
        assert gone.returncode == 255 and "Permission denied" in gone.stderr
        assert [n for n in os.listdir(str(remote_tmp)) if n.startswith("seamless-link-")] == []
    finally:
        sshd.stop()
