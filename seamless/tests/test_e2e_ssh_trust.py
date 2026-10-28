"""The SSH trust drill, ``tests/e2e/ssh-trust.sh``, against fakes (SDD §26.9, QASuite S-10, S-11,
plan task ST9): it runs the lab's half of S-10 (a rogue sshd at a conversion host's address, in a
cutover before the stop) and of S-11 (the link key's refusals from the destination host), records
the images' versions, and prints what it saw. The fakes stand in for curl, ssh and ssh-keyscan: a
drill that reported PASS whatever they answered would prove nothing.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()
DRILL = ROOT / "tests" / "e2e" / "ssh-trust.sh"

PINNED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPinnedPinnedPinnedPinnedPinnedPinnedPinnedPinn"
ROGUE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIRogueRogueRogueRogueRogueRogueRogueRogueRogu"
REFUSAL = (
    "host key refused: the source conversion host (os_migrate_conv_src, server srv-1 at "
    "203.0.113.5) presented SHA256:rogue, not its pinned key SHA256:pinned"
)

FAKE_CURL = r'''#!/usr/bin/env python3
"""curl, as the drill calls it: the API's answers come from the scenario's state directory."""
import json, os, sys
args = sys.argv[1:]
method = "GET"
url = None
i = 0
while i < len(args):
    a = args[i]
    if a == "-X":
        method = args[i + 1]; i += 2; continue
    if a in ("-H", "-m", "-d", "-o"):
        i += 2; continue
    if a.startswith("http"):
        url = a
    i += 1
state = os.environ["FAKE_STATE"]
path, _, query = url.partition("?")
route = path.split("/api/v1", 1)[1]
def load(name):
    with open(os.path.join(state, name)) as f:
        return json.load(f)
def save(name, value):
    with open(os.path.join(state, name), "w") as f:
        json.dump(value, f)
if route.startswith("/migrations/"):
    mid = route.split("/")[2]
    m = load("migration.json")
    if method == "POST" and route.endswith("/cutover"):
        # the scenario decides what the run did with the rogue key
        if os.environ.get("FAKE_CUTOVER") == "accepted":
            m.update(phase="cutover", downtime_started_at="2026-10-29T10:00:00Z")
        else:
            m.update(phase="failed", error=os.environ["FAKE_REFUSAL"])
            events = load("events.json")
            seq = max(e["seq"] for e in events) + 1
            data = {"side": "source", "pinned": ["SHA256:pinned"], "offered": "SHA256:rogue"}
            events.append({"seq": seq, "kind": "conversion_host.key_refused",
                           "migration_id": mid, "data": data})
            save("events.json", events)
        save("migration.json", m)
    print(json.dumps(m))
elif route.startswith("/plans/"):
    print(json.dumps({"id": "plan-1", "source_provider_id": "src-1"}))
elif route.startswith("/providers/"):
    pin = {"server_id": "srv-1", "keys": [os.environ["FAKE_PINNED"]], "source": "injected",
           "pinned_at": "2026-10-29T09:00:00Z"}
    print(json.dumps({"id": "src-1", "conversion_host_pin": pin}))
elif route == "/events":
    events = load("events.json")
    if "tail=true" in query:
        events = events[-1:]
    print(json.dumps(events))
else:
    sys.exit(22)
'''

FAKE_SSH = r'''#!/usr/bin/env python3
"""ssh, as the drill calls it: the last argument is the remote command; the scenario answers it."""
import os, sys
command = sys.argv[-1]
if "os-release" in command:
    host = next(a for a in sys.argv[1:] if "@" in a).split("@")[1]
    pairs = os.environ.get("FAKE_VERSIONS", "").split(";")
    versions = dict(pair.split("=", 1) for pair in pairs if pair)
    if host not in versions:
        print("ssh: connect to host %s port 22: No route to host" % host, file=sys.stderr)
        sys.exit(255)
    print(versions[host].replace("|", "\n"))
    sys.exit(0)
if command.rstrip().endswith(" id"):
    if os.environ.get("FAKE_LINK_ID"):
        print(os.environ["FAKE_LINK_ID"])
        sys.exit(0)
    print("Permission denied (publickey).", file=sys.stderr)
    sys.exit(255)
if "FORWARD_OPEN" in command:
    if os.environ.get("FAKE_LINK_FORWARD") == "open":
        print("FORWARD_OPEN")
    sys.exit(0)
if "scp " in command:
    sys.exit(0 if os.environ.get("FAKE_LINK_SCP") == "ok" else 1)
# a shell, and anything else the link key may not run
sys.exit(255)
'''

FAKE_KEYSCAN = r"""#!/usr/bin/env python3
import os, sys
host = sys.argv[-1]
for key in os.environ["FAKE_OFFERED"].split(";"):
    print(host + " " + key)
"""

VERSIONS = ";".join(
    [
        "203.0.113.10=PRETTY_NAME=Red Hat Enterprise Linux 9.4 (Plow)|OpenSSH_8.7p1, OpenSSL 3.2.2"
        "|/usr/bin/cloud-init 23.4",
        "203.0.113.11=PRETTY_NAME=Ubuntu 24.04.1 LTS|OpenSSH_9.6p1 Ubuntu-3ubuntu13.5, "
        "OpenSSL 3.0.13|/usr/bin/cloud-init 24.1.3",
    ]
)


class Lab:
    """A fake lab: the fakes first on PATH, the API's state, and the drill's environment."""

    def __init__(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, body in (("curl", FAKE_CURL), ("ssh", FAKE_SSH), ("ssh-keyscan", FAKE_KEYSCAN)):
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "FAKE_STATE": str(self.state),
            "FAKE_PINNED": PINNED,
            "FAKE_OFFERED": ROGUE,
            "FAKE_REFUSAL": REFUSAL,
            "SEAMLESS_URL": "http://127.0.0.1:8080",
            "TOKEN": "drill-token-not-a-secret",  # gitleaks:allow
            "MIGRATION": "mig-1",
            "SRC_HOST": "203.0.113.5",
            "DST_HOST": "203.0.113.6",
            "SSH_USER": "cloud-user",
            "SSH_IDENTITY": str(tmp_path / "id_ed25519"),
            "LINK_KEY": "/home/cloud-user/.ssh/seamless-link",
            "IMAGE_HOSTS": "",
            "BOUND": "5",
        }

    def run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        """The drill once, on a fresh migration awaiting its cutover."""
        migration = {
            "id": "mig-1",
            "plan_id": "plan-1",
            "phase": "awaiting_cutover",
            "error": None,
            "downtime_started_at": None,
        }
        (self.state / "migration.json").write_text(json.dumps(migration))
        phase = {"seq": 7, "kind": "migration.phase", "migration_id": "mig-1", "data": {}}
        (self.state / "events.json").write_text(json.dumps([phase]))
        return subprocess.run(
            ["bash", str(DRILL)],
            env={**self.env, **overrides},
            capture_output=True,
            text=True,
            timeout=120,
        )


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    return Lab(tmp_path)


def test_the_drill_fails_when_a_rogue_host_key_is_accepted(lab: Lab):
    """S-10's lab half (QASuite LAB-SSH01): the host offers another key than the pin, the cutover
    is requested, and the run must refuse it before the stop. A run that went on with the rogue
    key is the failure the drill exists to catch."""
    accepted = lab.run(FAKE_CUTOVER="accepted")
    assert accepted.returncode != 0, accepted.stdout + accepted.stderr
    assert "FAIL  S-10" in accepted.stdout and "accepted" in accepted.stdout, accepted.stdout
    # the refusal before the stop, named as the API records it, passes
    refused = lab.run(FAKE_CUTOVER="refused")
    out = refused.stdout + refused.stderr
    assert "PASS  S-10" in refused.stdout and "FAIL  S-10" not in refused.stdout, out
    assert "host key refused:" in refused.stdout
    # a host that still offers its pinned key is not a rogue: the drill says so instead of a verdict
    same = lab.run(FAKE_OFFERED=PINNED, FAKE_CUTOVER="refused")
    assert "NOT RUN  S-10" in same.stdout and "pinned key" in same.stdout, same.stdout
    assert "FAIL  S-10" not in same.stdout


def test_the_drill_fails_when_the_link_key_runs_a_command(lab: Lab):
    """S-11's lab half (QASuite LAB-SSH04): from the destination host, the run's link key may run
    neither `id`, a shell, scp nor a forward to another port on the source host."""
    ran = lab.run(FAKE_CUTOVER="refused", FAKE_LINK_ID="uid=1000(cloud-user) gid=1000(cloud-user)")
    assert ran.returncode != 0
    assert "FAIL  S-11" in ran.stdout and "id" in ran.stdout, ran.stdout + ran.stderr
    assert "uid=1000" not in ran.stdout.split("FAIL  S-11")[0]
    forwarded = lab.run(FAKE_CUTOVER="refused", FAKE_LINK_FORWARD="open")
    assert forwarded.returncode != 0 and "FAIL  S-11" in forwarded.stdout, forwarded.stdout
    assert "forward" in forwarded.stdout
    copied = lab.run(FAKE_CUTOVER="refused", FAKE_LINK_SCP="ok")
    assert copied.returncode != 0 and "FAIL  S-11" in copied.stdout, copied.stdout
    assert "scp" in copied.stdout
    # every refusal: the four checks pass, and the drill exits 0
    refused = lab.run(FAKE_CUTOVER="refused")
    assert refused.returncode == 0, refused.stdout + refused.stderr
    assert refused.stdout.count("PASS  S-11") == 4 and "FAIL" not in refused.stdout, refused.stdout


def test_the_drill_records_the_images_versions(lab: Lab):
    """The results go in QASuite §13 with the images and their OpenSSH and cloud-init versions
    (SDD §26.9): the drill reads each host's os-release, `ssh -V` and `cloud-init --version` and
    prints them on one line per image; a host it cannot reach is said so, never left out."""
    hosts = "rhel9=203.0.113.10 ubuntu2404=203.0.113.11 rocky9=203.0.113.12"
    ran = lab.run(FAKE_CUTOVER="refused", FAKE_VERSIONS=VERSIONS, IMAGE_HOSTS=hosts)
    rhel = "VERSIONS  rhel9: Red Hat Enterprise Linux 9.4 (Plow) | OpenSSH_8.7p1 | cloud-init 23.4"
    assert rhel in ran.stdout, ran.stdout
    ubuntu = "VERSIONS  ubuntu2404: Ubuntu 24.04.1 LTS | OpenSSH_9.6p1 | cloud-init 24.1.3"
    assert ubuntu in ran.stdout
    assert "VERSIONS  rocky9: not read" in ran.stdout
    # the versions are a record, not a check: the drill still exits 0
    assert ran.returncode == 0, ran.stdout + ran.stderr
