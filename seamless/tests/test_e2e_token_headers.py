"""The e2e scripts keep their bearer tokens off command lines (CLAUDE.md §4, audit A-15, A-16).

``ps`` shows every process's arguments to every user of the host, so a token reaches curl from a
header file (``curl -H @FILE``) that a shell builtin wrote into a private temporary directory,
removed when the script exits - never as an argument. What a script writes for itself goes in that
directory too, never under a fixed name in the shared temporary directory.
"""

import re
import secrets
import subprocess
import sys
from pathlib import Path

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()
E2E = ROOT / "tests" / "e2e"
HELPER = "token-headers.sh"


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """(line number, line) for each line of a shell script that is not a comment."""
    return [
        (n, line)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]


def test_no_e2e_script_puts_a_bearer_token_on_a_command_line():
    """Each token goes into its header file through printf, a shell builtin: no process gets it
    as an argument. A header built in a variable or on curl's command line puts it in ps."""
    offenders = []
    for path in sorted(E2E.glob("*.sh")):
        for n, line in _code_lines(path):
            if "Bearer" not in line:
                continue
            written = (
                path.name == HELPER
                and re.search(r"\bprintf 'Authorization: Bearer %s\\n' ", line)
                and ">" in line
            )
            if not written:
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == []


def test_no_e2e_script_writes_a_fixed_name_in_the_shared_temporary_directory():
    """A fixed name under ``$TMPDIR`` clobbers another run's file, follows a symlink another user
    planted there, and is left behind (audit A-16): the shared temporary directory is only ever
    the base of a ``mktemp`` template."""
    offenders = []
    for path in sorted(E2E.glob("*.sh")):
        lines = _code_lines(path)
        bases = {
            m.group(1)
            for _, line in lines
            for m in [re.match(r"\s*(\w+)=\$\{TMPDIR:-/tmp\}\s*$", line)]
            if m
        }
        for n, line in lines:
            uses_tmpdir = "TMPDIR" in line and not re.match(r"\s*\w+=\$\{TMPDIR:-/tmp\}\s*$", line)
            uses_base = any(re.search(rf"\$\{{?{base}\b", line) for base in bases)
            if (uses_tmpdir or uses_base) and "mktemp" not in line:
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == []


FAKE_CURL = """#!{python}
import os, stat, sys
args = sys.argv[1:]
with open(os.environ["CALLS_LOG"], "a", encoding="utf-8") as log:
    log.write("ARGV " + " ".join(args) + "\\n")
    for flag, value in zip(args, args[1:]):
        if flag == "-H" and value.startswith("@"):
            path = value[1:]
            modes = [stat.S_IMODE(os.stat(p).st_mode) for p in (path, os.path.dirname(path))]
            with open(path, encoding="utf-8") as header:
                log.write("HEADER %o %o %s" % (modes[0], modes[1], header.read()))
print("200", end="")
"""


def test_the_rbac_matrix_hands_curl_each_token_from_a_private_header_file(tmp_path):
    """rbac-live.sh against a curl that records its arguments and the header files it is given:
    no token is ever an argument, each request with a token reads it from a file in a directory
    only its owner can open, and that directory is gone once the script exits."""
    roles = ("VIEWER", "OPERATOR", "APPROVER", "ADMIN")
    tokens = {role: "smg_" + secrets.token_urlsafe(24) for role in roles}
    bin_dir, tmp = tmp_path / "bin", tmp_path / "tmp"
    bin_dir.mkdir()
    tmp.mkdir()
    (bin_dir / "curl").write_text(FAKE_CURL.format(python=sys.executable), encoding="utf-8")
    (bin_dir / "curl").chmod(0o755)
    log = tmp_path / "calls.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CALLS_LOG": str(log),
        "TMPDIR": str(tmp),
        "BASE": "http://127.0.0.1:9/api/v1",
        **tokens,
    }

    run = subprocess.run(
        ["bash", str(E2E / "rbac-live.sh")], env=env, capture_output=True, text=True, timeout=120
    )

    calls = log.read_text(encoding="utf-8").splitlines()
    argv = [line for line in calls if line.startswith("ARGV ")]
    headers = [line for line in calls if line.startswith("HEADER ")]
    assert run.returncode in (0, 1), run.stdout + run.stderr
    leaked = [
        line for line in argv + run.stdout.splitlines() if any(t in line for t in tokens.values())
    ]
    assert leaked == [], "a token was on curl's command line or in the output"
    # each check sends one request without a token, then one per role; the policy probes add some
    plain = [line for line in argv if " -H @" not in line]
    assert len(plain) >= 20, run.stdout + run.stderr
    for role, token in tokens.items():
        sent = [line for line in headers if line == f"HEADER 600 700 Authorization: Bearer {token}"]
        assert len(sent) >= len(plain), (role, len(sent), len(plain))
    assert len(headers) == len(argv) - len(plain)
    assert list(tmp.iterdir()) == [], "the private header directory outlived the script"
