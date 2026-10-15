"""The e2e scripts keep their bearer tokens off command lines (CLAUDE.md §4, audit A-15, A-16).

``ps`` shows every process's arguments to every user of the host, so a token reaches curl from a
header file (``curl -H @FILE``) that a shell builtin wrote into a private temporary directory,
removed when the script exits - never as an argument. What a script writes for itself goes in that
directory too, never under a fixed name in the shared temporary directory, and a script that sources
the helper sets no EXIT trap of its own: one would replace the helper's, and the header files, each
holding its token, would outlive the script.
"""

import re
import secrets
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from seamless_migrate.config import find_repo_root

ROOT = find_repo_root()
E2E = ROOT / "tests" / "e2e"
HELPER = "token-headers.sh"
#: the helper's write of each token: printf, the shell builtin - no path, no env - into a file
HELPER_WRITE = re.compile(r"""(?:^|[;&|(])\s*printf 'Authorization: Bearer %s\\n' "\$2" ?>""")
#: the variables that hold a token, handed to token_header by the scripts of this tree
KNOWN_TOKENS = {"TOKEN", "APPROVER_TOKEN", "VIEWER", "OPERATOR", "APPROVER", "ADMIN"}


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """(line number, line) for each line of a shell script that is not a comment."""
    return [
        (n, line)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]


def _token_variables(e2e: Path) -> set[str]:
    """The variables the scripts in ``e2e`` hand to token_header: each holds a bearer token."""
    return {
        m.group(1)
        for path in e2e.glob("*.sh")
        for _, line in _code_lines(path)
        for m in re.finditer(r'\btoken_header \w+ "\$\{?(\w+)\}?"', line)
    }


def _tokens_on_command_lines(e2e: Path) -> list[str]:
    """Lines of the scripts in ``e2e`` that may put a bearer token among a process's arguments:
    "bearer" in any case (curl's --oauth2-bearer too) outside the helper's write, and a token
    variable expanded anywhere but in a token_header call or the ``:`` builtin's check that it is
    set."""
    tokens = _token_variables(e2e)
    names = "|".join(sorted(tokens))
    expanded = re.compile(rf"\$\{{?(?:{names})\b") if tokens else None
    offenders = []
    for path in sorted(e2e.glob("*.sh")):
        for n, line in _code_lines(path):
            written = path.name == HELPER and HELPER_WRITE.search(line)
            bearer = re.search("bearer", line, re.IGNORECASE) and not written
            checked = re.match(r"\s*:\s", line) and "$(" not in line and "`" not in line
            rest = re.sub(r'\btoken_header \w+ "\$\{?\w+\}?"', "", line)
            if bearer or (expanded and expanded.search(rest) and not checked):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    return offenders


def _fixed_names_in_shared_tmp(e2e: Path) -> list[str]:
    """Lines of the scripts in ``e2e`` that name a path in the shared temporary directory."""
    offenders = []
    for path in sorted(e2e.glob("*.sh")):
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
            # /tmp, /var/tmp, /private/tmp or /dev/shm by name; ${TMPDIR:-/tmp} is read above
            literal = re.search(r"(?<!:-)/tmp\b|/dev/shm\b", line)
            if (uses_tmpdir or uses_base or literal) and "mktemp" not in line:
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    return offenders


def _traps_exit(line: str) -> bool:
    """Whether the ``trap`` on ``line`` sets, clears or ignores EXIT; True if it cannot be read."""
    try:
        words = shlex.split(line[re.search(r"\btrap\b", line).start() :], comments=True)
    except ValueError:
        return True
    args = [word.rstrip(";&|)") for word in words[1:]]
    if args[:1] == ["--"]:
        args = args[1:]
    if not args or args[0] in ("-p", "-l"):
        return False
    signals = args if len(args) == 1 else args[1:]  # `trap EXIT` alone resets it
    return any(signal.upper() in ("EXIT", "SIGEXIT", "0") for signal in signals)


def _exit_traps(e2e: Path) -> list[str]:
    """Lines that set, clear or ignore the EXIT trap in a script of ``e2e`` that sources the helper:
    the helper's EXIT trap removes the header files, and a script's own replaces it."""
    offenders = []
    for path in sorted(e2e.glob("*.sh")):
        lines = _code_lines(path)
        sourced = any(
            re.match(rf"\s*(?:\.|source)\s.*{re.escape(HELPER)}", line) for _, line in lines
        )
        if path.name == HELPER or not sourced:
            continue
        for n, line in lines:
            if re.search(r"(?:^|[\s;&|({])trap\s", line) and _traps_exit(line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    return offenders


def _s30_offenders(e2e: Path) -> list[str]:
    """Everything the static S-30 guards flag in the scripts in ``e2e``."""
    return _tokens_on_command_lines(e2e) + _fixed_names_in_shared_tmp(e2e) + _exit_traps(e2e)


def test_no_e2e_script_puts_a_bearer_token_on_a_command_line():
    """Each token goes into its header file through printf, a shell builtin: no process gets it
    as an argument. A header built in a variable or on curl's command line puts it in ps."""
    assert KNOWN_TOKENS <= _token_variables(E2E)
    assert _tokens_on_command_lines(E2E) == []


def test_no_e2e_script_writes_a_fixed_name_in_the_shared_temporary_directory():
    """A fixed name under ``$TMPDIR`` clobbers another run's file, follows a symlink another user
    planted there, and is left behind (audit A-16): the shared temporary directory is only ever
    the base of a ``mktemp`` template."""
    assert _fixed_names_in_shared_tmp(E2E) == []


def test_no_script_that_sources_the_helper_sets_or_clears_an_exit_trap():
    """token-headers.sh's EXIT trap removes the private directory and the header files in it; an
    EXIT trap a script sets after sourcing it replaces that one, and the files, each holding its
    token, outlive the script (review RV16-05). What a script must clean up goes in that
    directory."""
    assert _exit_traps(E2E) == []


SMOKE_ME = """me=$(curl -fsS -m 10 -H "$H" "$BASE/api/v1/me" || echo '{}')"""
SMOKE_H = 'H=$(token_header admin "$TOKEN") || exit 2\n'
JOURNEY_HA = 'HA=$(token_header approver "$APPROVER_TOKEN")\n'


@pytest.mark.parametrize(
    "script, old, new",
    [
        ("smoke-demo.sh", SMOKE_ME, SMOKE_ME.replace('-H "$H"', '--oauth2-bearer "$TOKEN"')),
        (
            "smoke-demo.sh",
            SMOKE_ME,
            SMOKE_ME.replace('-H "$H"', '-H "authorization: bearer $TOKEN"'),
        ),
        (
            "smoke-demo.sh",
            SMOKE_ME,
            SMOKE_ME.replace(
                '-H "$H"', '-H "authorization: bearer $(cat "$HOME/.seamless-token")"'
            ),
        ),
        ("smoke-demo.sh", SMOKE_ME, SMOKE_ME.replace('-H "$H"', '-H "X-Api-Key: ${TOKEN}"')),
        ("demo-restart.sh", '> "$S/seamless-events.json"', "> /tmp/seamless-events.json"),
        ("demo-journey.sh", JOURNEY_HA, JOURNEY_HA + "trap 'echo \"journey done\"' EXIT\n"),
        ("smoke-demo.sh", SMOKE_H, SMOKE_H + "trap - EXIT\n"),
        (HELPER, "(umask 077 && printf ", "(umask 077 && /usr/bin/printf "),
        (HELPER, "(umask 077 && printf ", "(umask 077 && env printf "),
    ],
    ids=[
        "curl's --oauth2-bearer",
        "the scheme in lower case",
        "the scheme in lower case, the token read from a file",
        "a token in a header of another name",
        "a fixed name in /tmp",
        "an EXIT trap of the script's own",
        "the helper's EXIT trap cleared",
        "the helper's printf an external one",
        "the helper's printf run through env",
    ],
)
def test_the_s30_guards_catch_a_token_back_on_a_command_line_or_a_header_file_left_behind(
    tmp_path, script, old, new
):
    """Review RV16-05: each of these puts a token among a process's arguments, writes under a fixed
    name in the shared temporary directory, or keeps the helper's EXIT trap from removing the header
    files, each holding its token - and the S-30 guards passed every one. They run here over a copy
    of tests/e2e with one line changed."""
    copy = tmp_path / "e2e"
    shutil.copytree(E2E, copy)
    path = copy / script
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new), encoding="utf-8")

    assert _s30_offenders(E2E) == []
    assert _s30_offenders(copy) != [], f"the S-30 guards passed: {new.strip()}"


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
