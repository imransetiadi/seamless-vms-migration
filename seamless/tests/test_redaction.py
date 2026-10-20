"""The one redaction layer (SDD §13.4, ADV-06): no secret leaves for Jev or agentmemory."""

import json
import random
import re
import shlex
import subprocess
from pathlib import Path

import httpx
import pytest

from seamless_migrate.ai import redaction
from seamless_migrate.ai.jev import JevClient
from seamless_migrate.ai.memory import MemoryClient
from seamless_migrate.ai.redaction import (
    CUT_MARGIN,
    REDACTED,
    redact,
    redact_cut,
    redact_payload,
    redact_tail,
)
from seamless_migrate.config import Settings
from tests.jev_fakes import session_factory

#: Synthetic secrets of every kind SDD §13.4 lists (none of them is real).
FIXTURE = (
    "login failed: password=FakePass-Alpha1 for admin\n"
    '{"user": "svc", "password": "FakePass-Bravo2", "port": 5432}\n'
    "export OS_PASSWORD=FakePass-Charlie3\n"
    "openstack --os-password FakePass-Delta4 server list\n"
    "Authorization: Bearer FAKE.bearer.token-echo5\n"
    'curl -H "X-Auth-Token: gAAAAABfakeKeystoneFernetTokenFoxtrot6xx" '  # gitleaks:allow
    "https://keystone.example:5000/v3\n"
    "X-Subject-Token: 7f3c0a1b2d4e5f60718293a4b5c6d7e8\n"  # gitleaks:allow
    "api_key=fake-api-key-golf7 TYPESAFE_API_KEY=fake-typesafe-hotel8\n"  # gitleaks:allow
    "AKIAFAKEINDIA9ABCDEF ghp_FakeJulietFakeJulietFakeJulietFake10 "  # gitleaks:allow
    "sk-proj-FakeKiloFakeKiloFake11 xoxb-FakeLima-12345678\n"  # gitleaks:allow
    "-----BEGIN RSA PRIVATE KEY-----\n"  # gitleaks:allow (fake, no key material)
    "MIIEFAKEmikeKEYMATERIAL13\n"
    "-----END RSA PRIVATE KEY-----\n"
    "db postgresql+psycopg://seamless:Fake@Pass-November14"  # gitleaks:allow
    "@db.example:5432/seamless\n"
    "Server=sql.example;User Id=sa;Password=Fake#Oscar15;Encrypt=true\n"  # gitleaks:allow
    "clouds:\n"
    "  dc1:\n"
    "    auth:\n"
    "      auth_url: https://keystone.dc1.example:5000/v3\n"
    "      username: admin\n"
    "      password: FakeCloud-Papa16\n"  # gitleaks:allow
    "      project_name: admin\n"
    "      user_domain_name: Default\n"
    "    region_name: RegionOne\n"
    "    interface: public\n"
    '{"clouds": {"dc2": {"auth": {"auth_url": "https://keystone.dc2.example:5000/v3", '
    '"username": "admin", "password": "FakeCloud-Quebec17"}, '  # gitleaks:allow
    '"region_name": "RegionTwo"}}}\n'
    "seamless token smg_FakeRomeoFakeRomeoFake18 and jwt "  # gitleaks:allow
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.ZmFrZVNpZ25hdHVyZQ\n"  # gitleaks:allow
    # a quote inside a quoted value - JSON's \", YAML's '' - and JSON inside a JSON string, as
    # Ansible's output carries it (audit AD-08)
    '{"user": "svc", "password": "Fake\\"Pass Sierra-19"}\n'  # gitleaks:allow
    "password: 'Fake''Pass Tango-20'\n"  # gitleaks:allow
    '{\\"password\\": \\"Fake\\nPass Uniform-21\\"}\n'  # gitleaks:allow
    '{\\"token\\": \\"Fake\\\\\\"Pass Victor-22, x\\", \\"port\\": 5432}\n'  # gitleaks:allow
)
#: The reviewed snapshot of ``redact(FIXTURE)``.
SNAPSHOT = (
    "login failed: password=[REDACTED] for admin\n"
    '{"user": "svc", "password": [REDACTED], "port": 5432}\n'
    "export OS_PASSWORD=[REDACTED]\n"
    "openstack --os-password [REDACTED] server list\n"
    "Authorization: [REDACTED]\n"
    'curl -H "X-Auth-Token: [REDACTED] https://keystone.example:5000/v3\n'
    "X-Subject-Token: [REDACTED]\n"
    "api_key=[REDACTED] TYPESAFE_API_KEY=[REDACTED]\n"
    "[REDACTED] [REDACTED] [REDACTED] [REDACTED]\n"
    "[REDACTED PEM]\n"
    "db postgresql+psycopg://[REDACTED]@db.example:5432/seamless\n"
    "Server=sql.example;User Id=sa;Password=[REDACTED];Encrypt=true\n"
    "clouds:\n"
    "  dc1:\n"
    "    auth: [REDACTED]\n"
    "    region_name: RegionOne\n"
    "    interface: public\n"
    '{"clouds": {"dc2": {"auth": [REDACTED], "region_name": "RegionTwo"}}}\n'
    "seamless token [REDACTED] and jwt [REDACTED]\n"
    '{"user": "svc", "password": [REDACTED]}\n'
    "password: [REDACTED]\n"
    '{\\"password\\": [REDACTED]}\n'
    '{\\"token\\": [REDACTED], \\"port\\": 5432}\n'
)
#: Every fake secret of the fixture: none may survive anywhere it is sent.
SECRETS = re.compile(
    r"Fake(?:Pass|Cloud|Romeo|Juliet|Kilo|Lima|Oscar|@Pass)|fake-(?:api|typesafe)|FAKE\.bearer|"
    r"fakeKeystone|7f3c0a1b|AKIAFAKE|MIIEFAKE|eyJhbGci|Pass-November|admin\n      password|"
    r"Sierra-19|Tango-20|Uniform-21|Victor-22"
)


def leaks(text: str) -> list[str]:
    return SECRETS.findall(text)


def test_a_fixture_of_fake_secrets_redacts_to_the_reviewed_snapshot():
    """ADV-06: password, bearer token, PEM key and clouds.yaml credentials leave no trace."""
    out = redact(FIXTURE)
    assert leaks(FIXTURE)  # the canary list does see the fixture's secrets
    assert out == SNAPSHOT
    assert leaks(out) == []


def test_redaction_is_idempotent():
    assert redact(SNAPSHOT) == SNAPSHOT


def test_a_second_pass_over_a_json_escaped_text_changes_nothing():
    """SDD §13.4 (review RV51-21): a [REDACTED] the first pass wrote stays as it is. A step error is
    redacted by the executor, the failure handler, the knowledge service and the memory client: a
    bare [REDACTED] glued to an escaped line break took the next line's key at each pass."""
    once = redact(json.dumps({"stdout": "password: 'FakeMike-6'\nport: 5432"}))
    assert once == '{"stdout": "password: [REDACTED]\\nport: 5432"}'
    assert redact(once) == once
    # one glued to more text is no placeholder: the value is redacted whole
    assert "FakeEnd" not in redact("password: [REDACTED]FakeEnd")
    for line in FIXTURE.splitlines():
        first = redact(json.dumps({"stdout": line}))
        assert redact(first) == first, line


PEM_KEY = "-----BEGIN RSA PRIVATE KEY-----" + "K" * 900 + "-----END RSA PRIVATE KEY-----"
URL_WITH_PASSWORD = "postgresql://svc:Sup3rS3cretPw@db.example:5432/seamless"


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("x" * 400 + " " + PEM_KEY + " tail", "KKKK"),  # the cut falls inside the key
        ("x" * 470 + " " + URL_WITH_PASSWORD + " tail", "Sup3r"),  # ... inside the password
    ],
    # ids of their own: derived from the texts, they would carry the secrets into the cache and logs
    ids=["the cut splits a PEM key", "the cut splits a URL password"],
)
def test_redact_cut_finds_a_secret_the_cut_splits(text, leak):
    """SDD §13.4: cutting first leaves a PEM block without its END line and a URL without the @
    of its credentials, which redact no longer recognizes; redact_cut redacts the cut plus
    CUT_MARGIN characters and cuts after."""
    assert leak in redact(text[:500])
    cut = redact_cut(text, 500)
    assert len(cut) <= 500 and leak not in cut and "[REDACTED" in cut  # [REDACTED PEM] too


def test_redact_cut_redacts_no_more_than_the_cut_and_its_margin(monkeypatch):
    """SDD §13.4: the work is bounded whatever the text's length."""
    assert CUT_MARGIN == 16384
    seen: list[int] = []
    real = redaction.redact
    monkeypatch.setattr(
        redaction, "redact", lambda text, *a: seen.append(len(text)) or real(text, *a)
    )
    assert redaction.redact_cut("y" * 10**6, 500) == "y" * 500
    assert seen == [500 + CUT_MARGIN]


def test_redact_tail_finds_a_secret_whose_start_the_cut_drops(monkeypatch):
    """SDD §13.4: keeping a text's last characters drops the BEGIN line of a PEM block that starts
    before them; redact_tail redacts the kept part and CUT_MARGIN characters before it, then keeps
    the last limit characters — at a bounded cost whatever the text's length."""
    text = "head " + PEM_KEY + " the playbook's last words"
    assert "KKKK" in redact(text[-500:])
    tail = redact_tail(text, 500)
    assert tail == "head [REDACTED PEM] the playbook's last words"
    assert redact_tail("x" * 900 + text, 30) == "PEM] the playbook's last words"
    seen: list[int] = []
    real = redaction.redact
    monkeypatch.setattr(
        redaction, "redact", lambda text, *a: seen.append(len(text)) or real(text, *a)
    )
    assert redaction.redact_tail("y" * 10**6, 500) == "y" * 500
    assert seen == [500 + CUT_MARGIN]


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        # no credentials: the URL, its port and path stay
        (
            "https://keystone.example:5000/v3/auth/tokens",
            "https://keystone.example:5000/v3/auth/tokens",
        ),
        # words that merely contain a keyword are not keys
        (
            "passes: 3 tokens_used: 12 disk-0123456789abcdefghijkl",
            "passes: 3 tokens_used: 12 disk-0123456789abcdefghijkl",
        ),
        # an auth_type is not the auth section
        (
            "auth_type: password\nregion_name: RegionOne",
            "auth_type: password\nregion_name: RegionOne",
        ),
    ],
)
def test_redaction_keeps_what_is_not_a_secret(text, kept):
    assert redact(text) == kept


def test_redact_payload_redacts_every_string_and_whole_credential_values():
    payload = {
        "decision": "VM web-01: password=FakePass-Alpha1",
        "candidates": [{"id": "warm", "description": "Bearer FAKE.bearer.token-echo5"}],
        "auth": {"username": "admin", "password": "FakeCloud-Papa16"},
        "OS_PASSWORD": 123456,
        "passes": 3,
        "escalate": True,
        "note": None,
        "nested": (
            "api_key=fake-api-key-golf7",  # gitleaks:allow (synthetic)
            ["X-Subject-Token: 7f3c0a1b2d4e5f60718293a4b5c6d7e8"],  # gitleaks:allow (synthetic)
        ),
    }
    out = redact_payload(payload, ["web-01"])
    assert out == {
        "decision": "VM [vm]: password=[REDACTED]",
        "candidates": [{"id": "warm", "description": "Bearer [REDACTED]"}],
        "auth": REDACTED,
        "OS_PASSWORD": REDACTED,
        "passes": 3,
        "escalate": True,
        "note": None,
        "nested": ["api_key=[REDACTED]", ["X-Subject-Token: [REDACTED]"]],
    }
    assert leaks(json.dumps(out)) == []


async def test_every_jev_call_leaves_redacted():
    """No Jev call can leave unredacted, whoever builds the arguments (SDD §13.4, §14.1)."""
    factory, session = session_factory(
        lambda name, arguments: {"recommendation": {"action": "pass"}}
    )
    client = JevClient(Settings(jev_mode="stdio"), session_factory=factory)
    await client.screen(text=FIXTURE, purpose="console screening")
    await client.verify(
        claims=["password=FakePass-Alpha1"], evidence=[{"id": "checks", "text": FIXTURE}]
    )
    await client.decide(FIXTURE, FIXTURE, "p", [{"id": "warm", "description": FIXTURE}], [FIXTURE])
    await client.classify(
        [{"id": "vm-1", "text": FIXTURE}], [{"id": "web", "description": "x"}], FIXTURE
    )
    assert [name for name, _, _ in session.calls] == [
        "jev_screen",
        "jev_verify",
        "jev_decide",
        "jev_classify",
    ]
    for _, arguments, _ in session.calls:
        assert leaks(json.dumps(arguments)) == []
    assert session.calls[0][1]["text"] == SNAPSHOT


async def test_every_agentmemory_request_leaves_redacted():
    """Lessons, their concepts and files, and searches pass the layer (SDD §13.4, §14.3)."""
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content.decode())
        return httpx.Response(200, json={"success": True, "results": []})

    client = MemoryClient(
        "http://memory:3111", None, "seamless-migrate", transport=httpx.MockTransport(handler)
    )
    assert await client.remember(
        FIXTURE,
        "bug",
        ["seamless", "token=FakeRomeoFakeRomeoFake18"],
        ["/var/log/x?password=FakePass-Alpha1"],
    )
    await client.search(FIXTURE)
    assert len(bodies) == 2
    for body in bodies:
        assert leaks(body) == []
    assert json.loads(bodies[0])["content"] == SNAPSHOT


PACKAGE = Path(__file__).resolve().parents[1] / "src" / "seamless_migrate"


def _modules_matching(pattern: str) -> set[str]:
    regex = re.compile(pattern)
    return {
        str(path.relative_to(PACKAGE))
        for path in PACKAGE.rglob("*.py")
        if regex.search(path.read_text(encoding="utf-8"))
    }


def test_only_the_two_clients_talk_to_the_ai_services():
    """ADV-06: one client calls Jev, one writes to agentmemory, and both apply the layer."""
    assert _modules_matching(r"\.call_tool\(") == {"ai/jev.py"}
    assert _modules_matching(r"/agentmemory/") == {"ai/memory.py"}
    assert _modules_matching(r"\bJevClient\(") == {"api/app.py"}
    assert _modules_matching(r"\bMemoryClient\((?!\))|MemoryClient\.from_settings") == {
        "api/app.py"
    }
    # the patterns live in one module
    assert _modules_matching(r"re\.compile\(r[\"'][^\n]*BEGIN") == {"ai/redaction.py"}


def test_redact_writes_nul_and_lone_surrogates_as_the_replacement_character():
    """SDD §11, §13.4 (reviews ST-03, RV48-07): outside text - a step's or a provider's error, a
    migration.log line, an agentmemory hit - may carry a NUL or a lone surrogate, which the store
    refuses; redact writes each as U+FFFD, the keys of a payload's maps too, so such text is kept
    wherever it goes. A character outside the BMP is a character like any other."""
    assert redact("step\x00failed: \ud800 ok \U0001f600") == "step�failed: � ok \U0001f600"
    assert redact_cut("a\x00" * 10, 4) == "a�a�"
    assert redact_tail("x\ud800y", 2) == "�y"
    assert redact_payload({"evidence": ["da\x00ta"], "o\x00k": "v\ud800"}) == {
        "evidence": ["da�ta"],
        "o�k": "v�",
    }


#: a synthetic secret with a space and both quote kinds (review RV50-10)
QUOTED_SECRET = "Fake Pass'Word\"-9x"  # gitleaks:allow


def _nested(payload: dict, levels: int) -> str:
    """``payload`` as JSON, then put into a JSON string ``levels - 1`` more times - a command's
    stdout holding JSON, inside a task result, inside Ansible's callback."""
    text = json.dumps(payload)
    for _ in range(levels - 1):
        text = json.dumps({"stdout": text})
    return text


@pytest.mark.parametrize(
    "text",
    [
        *(_nested({"password": QUOTED_SECRET}, levels) for levels in (1, 2, 3, 4)),
        repr({"password": QUOTED_SECRET}),
        f"password={QUOTED_SECRET!r}",
        json.dumps({"msg": repr({"password": QUOTED_SECRET})}),
        "password: 'Fake Pass''Word\"\n  -9x'\n",
    ],
    ids=[
        "json",
        "json-in-json",
        "json-in-json-in-json",
        "four-levels",
        "repr-dict",
        "repr-value",
        "repr-in-json",
        "yaml-over-lines",
    ],
)
def test_a_quoted_secret_is_removed_whole_at_any_escape_level(text):
    """SDD §13.4 (review RV50-10): a quoted value runs to its own closing quote, read as its
    quoting reads it - at the escape level of its key, however many JSON strings hold it, and in a
    Python repr, whose single-quoted strings escape a quote with a backslash, and over lines in
    YAML. Three levels kept everything after the secret's first space, and a repr's \' ended the
    value early."""
    out = redact(text)
    assert "[REDACTED]" in out and "Pass" not in out and "-9x" not in out, out


@pytest.mark.parametrize(
    "text",
    [
        "{password: 'FakeAlpha\\', token: 'FakeWhiskey-23'}",
        "export OS_PASSWORD='FakeAlpha\\'; export OS_TOKEN='FakeWhiskey-23'",
        "password: 'FakeAlpha\\'\ntoken: 'FakeWhiskey-23'\n",
        "{'password': 'FakeAlpha\\\\', 'token': 'FakeWhiskey-23'}",
        # YAML writes a quote inside a single-quoted string as '' - after a backslash too (review
        # RV51-03): PyYAML's own dumps, and a flow map's next credential, its key quoted or not
        "password: 'FakeAlpha\\'': Pass Whiskey'\nport: 5432\n",
        "password: '!FakeAlpha\\'' Pass Whiskey'\nport: 5432\n",
        "{password: 'C:\\', token: 'FakeAlpha'' Pass Whiskey', port: 5432}",
        "{'password': 'C:\\', 'token': 'FakeAlpha Pass Whiskey', 'port': 5432}",
        # and a quoted credential key glued to a closed value is never taken into it
        "{'password': 'FakeAlpha'x'token': 'FakeWhiskey-23'}",
    ],
    ids=[
        "yaml-flow",
        "shell",
        "yaml-lines",
        "repr-backslash",
        "pyyaml-colon",
        "pyyaml-tag-char",
        "glued-close-then-doubled-quote",
        "glued-close-then-quoted-key",
        "glued-word-then-quoted-key",
    ],
)
def test_a_backslash_ending_a_single_quoted_value_hides_no_later_secret(text):
    """SDD §13.4 (review RV50-10): YAML and the shell keep a backslash before a single quote, a
    Python repr reads it as an escape - read either way, neither the value nor a credential after
    it on its line or the next survives."""
    out = redact(text)
    assert "FakeAlpha" not in out and "Whiskey" not in out, out


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        # a secret ending in a backslash: 2E + 2 backslashes for it, then the E of the close
        *(
            (
                _nested({"password": "FakeEnd\\", "port": 5432}, levels),
                "[REDACTED], " + "\\" * (2 ** (levels - 1) - 1) + '"port',
            )
            for levels in (2, 3, 4)
        ),
        # a bare quote ends a string inside JSON strings: it is no longer inside one
        ('password=\\"FakeEnd raw" port 5432', '[REDACTED]" port 5432'),
        # one that never closes - a cut, a broken quote - is removed to its line's end
        ('{\\"password\\": \\"FakeEnd raw\nport 5432', "[REDACTED]\nport 5432"),
        ("{'password': 'FakeEnd', 'port': 5432}", "[REDACTED], 'port': 5432"),
        ("password: 'FakeEnd'. port 5432", "[REDACTED]. port 5432"),
        # a close a > or a ! follows: the error text after it stays (review RV51-20)
        (
            "<Connection user='admin' password='FakeEnd-5'> failed for host 'db01'",
            "password=[REDACTED]> failed for host 'db01'",
        ),
        (
            "OS_PASSWORD='FakeEnd-5'! then: 'host db01 unreachable'",
            " then: 'host db01 unreachable'",
        ),
        (
            "<Connection password='Fake\\'End\"-5'> failed for host 'db01'",
            "> failed for host 'db01'",
        ),
        # a value read past a backslash before its quote stops before a quoted credential key,
        # which is redacted on its own
        (
            "{'password': 'C:\\\\', 'token': 'FakeEnd raw', 'port': 5432}",
            "'token': [REDACTED], 'port': 5432}",
        ),
    ],
    ids=[
        "json-in-json",
        "json-in-json-in-json",
        "four-levels",
        "bare-quote",
        "unclosed",
        "repr",
        "prose",
        "angle-bracket",
        "exclamation",
        "angle-bracket-after-an-escape",
        "quoted-key-after-a-backslash",
    ],
)
def test_what_follows_a_quoted_secret_stays(text, kept):
    """SDD §13.4 (review RV50-10): a quoted value ends at its own close - the text after it is
    kept, the next field of the same JSON object whole - and a string inside JSON strings that
    never closes is removed to the end of its line."""
    out = redact(text)
    assert "FakeEnd" not in out and "raw" not in out and kept in out, out


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"stdout": "Connecting\npassword: FakeHotel-1\ndone"}),
        json.dumps({"stdout": "env\ntoken=FakeHotel-1\nexit"}),
        json.dumps({"stdout": "conf\n\tsecret: FakeHotel-1\n"}),
        json.dumps({"stdout": "keys\napi_key: FakeHotel-1"}),
        'fatal: [localhost]: FAILED! => {"changed": true, "rc": 1, "stdout": "Reading clouds.yaml'
        '\\npassword: FakeHotel-1\\nexit"}',
    ],
    ids=["stdout-password", "env-token", "tab-secret", "api-key", "ansible-fatal-line"],
)
def test_a_credential_key_after_an_escaped_line_break_is_found(text):
    """SDD §13.4 (review RV51-02): JSON writes a line break inside a string as backslash-n, so a key
    starting a line of a command's output follows the letter n - and the key's start refused it:
    ansible-playbook's failed task, one JSON line, kept the password whole."""
    out = redact(text)
    assert "FakeHotel" not in out and "[REDACTED]" in out, out


@pytest.mark.parametrize(
    "secret",
    ["Fake' Pass\"Word", "Fake',Pass\"Word", "Fake'.Pass\"Word", "Fake' Pass' \"Word"],
    ids=["quote-space", "quote-comma", "quote-dot", "two-quotes"],
)
def test_a_repr_inside_json_keeps_no_part_of_a_secret(secret):
    """SDD §13.4 (review RV51-04): a Python repr escapes the quote of a secret holding both kinds
    (\\'), which inside JSON is \\\\' - read as an escaped backslash and a close, the rest of the
    secret after a space, comma or dot stayed."""
    for text in (
        json.dumps({"msg": "failed: " + repr({"password": secret, "port": 5432})}),
        _nested({"msg": "failed: " + repr({"password": secret, "port": 5432})}, 2),
    ):
        out = redact(text)
        assert "Pass" not in out and "Word" not in out and "5432" in out, out


@pytest.mark.parametrize(
    "text",
    [
        "export OS_PASSWORD=" + shlex.quote("Fake' Pass Word") + " port=5432",
        "+ OS_PASSWORD='Fake'\\'' Pass Word' port=5432",
        json.dumps(
            {"cmd": "mysql --password=" + shlex.quote("Fake' Pass Word") + " -e 'select 1'"}
        ),
        json.dumps({"stdout": "+ OS_PASSWORD='Fake'\\'' Pass Word' port=5432"}),
        "export OS_PASSWORD='Fake'Pass-Word port=5432",
        # a secret holding a backslash before a quote: the value is read past it, over the
        # shell's quotes after (each quote of the secret written '"'"' or '\\'')
        "export OS_PASSWORD=" + shlex.quote("Fake\\' Pass Word") + " port=5432",
        "+ OS_PASSWORD='" + "Fake\\' Pass'Word".replace("'", "'\\''") + "' port=5432",
    ],
    ids=[
        "shlex-quote",
        "bash-xtrace",
        "ansible-cmd-json",
        "xtrace-in-json",
        "glued-word",
        "shlex-backslash-quote",
        "xtrace-backslash-quote",
    ],
)
def test_a_shell_quoted_secret_with_a_quote_is_removed_whole(text):
    """SDD §13.4 (review RV51-05): the shell has no escape inside single quotes - a quote is
    written '\\'' (bash's xtrace) or '"'"' (shlex.quote, Ansible's quote filter), and the value
    ended at its first close."""
    out = redact(text)
    assert "Pass" not in out and "Word" not in out and "[REDACTED]" in out, out
    assert "port=5432" in out or "-e 'select 1'" in out, out


_ARGV = ["openstack", "--os-password", "Fake Pass-Word", "server", "list"]


@pytest.mark.parametrize(
    "text",
    [
        "fatal: [localhost]: FAILED! => " + json.dumps({"cmd": _ARGV, "rc": 1}),
        str(subprocess.CalledProcessError(1, _ARGV)),
        json.dumps({"msg": json.dumps({"cmd": _ARGV})}),
        repr(["openstack", "--os-password", "Fake\\'Pass-Word", "server", "list"]),
        "+ openstack --os-password 'Fake Pass-Word' server list",
        'openstack --os-password "Fake \\"Pass\\" Word" server list',
    ],
    ids=["ansible-json", "called-process-error", "json-in-json", "repr-quote", "xtrace", "dq"],
)
def test_a_cli_flag_in_an_argv_list_or_quoted_with_a_space_is_redacted_whole(text):
    """SDD §13.4 (review RV52-X3): a CLI flag's value was the word after a space, so an argv list
    (Ansible's cmd, a CalledProcessError) kept its secret, and a quoted value kept all but its
    first word. The value is the list's next item, or the quoted string whole."""
    out = redact(text)
    assert "Pass" not in out and "Word" not in out and REDACTED in out, out
    assert "server" in out and "list" in out, out
    assert redact(out) == out, out


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"Authorization": "Basic RmFrZVBhc3M6V29yZA=="}),
        repr({"Authorization": "Digest username=FakePass, response=Word"}),
        json.dumps({"msg": json.dumps({"headers": {"authorization": "Negotiate FakePassWord"}})}),
        "Authorization: Basic RmFrZVBhc3M6V29yZA==",
    ],
    ids=["json", "repr", "json-in-json", "header"],
)
def test_an_authorization_header_of_any_scheme_is_redacted_in_json_a_repr_and_a_payload(text):
    """SDD §13.4 (review RV52-X5): an Authorization header was redacted only as a header line or a
    bearer token, so a Basic, Digest or Negotiate one in JSON or a repr - a request an HTTP
    client logged - kept its credentials, and redact_payload kept an Authorization key's value."""
    out = redact(text)
    assert "RmFrZ" not in out and "Fake" not in out and "Word" not in out, out
    assert redact(out) == out, out
    payload = redact_payload({"headers": {"Authorization": "Basic RmFrZVBhc3M6V29yZA=="}})
    assert payload == {"headers": {"Authorization": REDACTED}}


_CLOUDS = (
    "clouds:\n  dc1:\n    auth:\n      username: FakeUser\n      password: Fake horse Word\n"
    "    region_name: RegionOne\n"
)


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"stdout": "Reading clouds.yaml\n" + _CLOUDS, "rc": 0}),
        _CLOUDS.replace("\n", "\r\n"),
        _CLOUDS.replace("    auth:\n", "    auth:  # the admin credential\n"),
        json.dumps({"stdout": _CLOUDS.replace("\n", "\r\n")}),
    ],
    ids=["inside-json", "crlf", "comment", "crlf-inside-json"],
)
def test_a_clouds_yaml_auth_section_inside_json_or_with_crlf_or_a_comment_is_removed_whole(text):
    """SDD §13.4 (review RV52-X4): the auth section was found only with LF line breaks and nothing
    after "auth:", so a clouds.yaml a step printed (its output JSON-escaped), one with CRLF line
    breaks or a comment kept its user name, and its multi-word password all but the first word."""
    out = redact(text)
    assert "Fake" not in out and "horse" not in out and "Word" not in out, out
    assert "auth: [REDACTED]" in out and "region_name: RegionOne" in out, out
    assert redact(out) == out, out


def test_a_multi_word_yaml_password_is_redacted_whole():
    """SDD §13.4 (review RV52-X4): a YAML plain value runs to its line's end or a comment - a
    password of several words kept all but its first."""
    out = redact("vcenter_password: Fake horse Word  # rotated\nport: 443")
    assert out == "vcenter_password: [REDACTED]  # rotated\nport: 443", out
    assert redact("password=Fake horse") == "password=[REDACTED] horse"


@pytest.mark.parametrize(
    "text",
    [
        'Authorization: "token":\n!;',
        "Authorization: 'token':Bearer '\\''.",
        "--os-password ':,>\\'\"'\"'\"token\":-auth: {a: b} next",
        "--os-password 'Fake'Pass-Word server list",
        json.dumps({"cmd": "--os-password 'Fake'Pass-Word server list"}),
    ],
    ids=["header-dq", "header-sq", "flag-glued-quotes", "flag-glued-word", "flag-in-json"],
)
def test_a_second_pass_changes_nothing_after_a_cli_flag_or_an_authorization_header(text):
    """SDD §13.4 (reviews RV52-X3, RV52-X5): a CLI flag's quoted value ended at its close and left
    what the shell glues to the word, and the Authorization header pattern read a [REDACTED] an
    earlier pass wrote as a value, so a second pass changed the text. A flag's value is a shell
    word whole, and the header pattern leaves a [REDACTED] as it is."""
    once = redact(text)
    assert redact(once) == once, once
    assert "Pass" not in once and "Word" not in once, once


@pytest.mark.parametrize(
    "text",
    [
        'export OS_PASSWORD="Fake"Pass-Word port=5432',
        'export OS_PASSWORD="Fake""Pass-Word" port=5432',
        "export OS_PASSWORD=\"Fake\"'Pass Word' port=5432",
        'export OS_PASSWORD="Fake"Pass"Word" port=5432',
        json.dumps({"cmd": 'export OS_PASSWORD="Fake"Pass-Word port=5432'}),
    ],
    ids=["glued-word", "glued-dq", "glued-sq", "glued-chain", "inside-json"],
)
def test_a_double_quoted_shell_value_keeps_no_glued_part_and_a_second_pass_changes_nothing(text):
    """SDD §13.4 (review RV52-X6): the shell glues what follows a closed double-quoted word to
    it ("Fake"Pass-Word is one word), as it does after a single-quoted one; the value ended at its
    close, so the glued part stayed - and a second pass, reading it as a plain value, changed the
    output."""
    out = redact(text)
    assert "Pass" not in out and "Word" not in out and REDACTED in out, out
    assert "port=5432" in out, out
    assert redact(out) == out, out


@pytest.mark.parametrize(
    "text",
    [
        '{"password": "x"}, "y"',
        '{"password":"x","user":"admin"}',
        "{'password': \"x\"}.",
        'password="x"; echo done',
    ],
    ids=["json-then-item", "json-compact", "repr-then-period", "shell-then-command"],
)
def test_what_follows_a_closed_double_quoted_secret_stays(text):
    """SDD §13.4 (review RV52-X6): only what the shell glues to the word goes with it - a JSON or
    repr delimiter, a separator or a sentence's end stays."""
    out = redact(text)
    assert out.replace(REDACTED, '"x"') == text, out  # the quoted value alone, its quotes too


def test_a_seeded_fuzz_of_cli_flags_and_authorization_headers_redacts_once_for_good():
    """SDD §13.4 (reviews RV52-X3, RV52-X5): a CLI flag's quoted value ended at its close and
    left what the shell glues to the word, and the Authorization header pattern read a
    [REDACTED] an earlier pass wrote as a value: a second pass changed 9 % of these texts. A
    second pass changes nothing, in a text or inside JSON."""
    rng = random.Random(52)
    atoms = [
        "--os-password ",
        "--token ",
        "Authorization: ",
        "Basic ",
        "Bearer ",
        "'token':",
        '"token":',
        "'",
        '"',
        "\\'",
        '\\"',
        "x",
        "a b",
        " ",
        ":",
        "=",
        ",",
        ">",
        "\n",
        "\\n",
        "[REDACTED]",
        "'\"'\"'",
        "'\\''",
    ]
    changed = []
    for _ in range(3000):
        text = "".join(rng.choice(atoms) for _ in range(rng.randint(2, 10)))
        for held in (text, json.dumps({"stdout": text})):
            once = redact(held)
            if redact(once) != once:
                changed.append(held)
    assert changed == [], changed[:5]


@pytest.mark.parametrize(
    "text",
    ["--os-password 'Fake Pass Word", '--os-password "Fake Pass Word', '--os-password "Fake\\'],
    ids=["single", "double", "backslash-at-end"],
)
def test_a_cli_flags_quote_that_never_closes_runs_to_its_lines_end(text):
    """SDD §13.4 (review RV52-X3): a flag's quote that never closes - a log line cut short - ended
    at the next space, so the secret's later words stayed. It runs to its line's end, the longest
    reading."""
    out = redact(text + "\nnext line")
    assert out == "--os-password [REDACTED]\nnext line", out
    assert redact(out) == out


def _quotings(pairs):
    """``pairs`` (key, value) as each quoting a step's output carries them prints them."""
    mapping = dict(pairs)
    yield json.dumps(mapping)
    yield repr(mapping)
    if all("\n" not in v for _, v in pairs):
        yield "\n".join(f"{k}: '" + v.replace("'", "''") + "'" for k, v in pairs)
        yield "{" + ", ".join(f"{k}: '" + v.replace("'", "''") + "'" for k, v in pairs) + "}"
        yield "{" + ", ".join(f"'{k}': '" + v.replace("'", "''") + "'" for k, v in pairs) + "}"
        yield "+ " + " ".join(f"{k.upper()}='" + v.replace("'", "'\\''") + "'" for k, v in pairs)
        if all(shlex.quote(v).startswith("'") for _, v in pairs):
            yield "export " + " ".join(f"{k.upper()}={shlex.quote(v)}" for k, v in pairs)
        yield "<Conn " + " ".join(f"{k}={v!r}" for k, v in pairs) + "> failed for host 'db01'"


def test_a_seeded_fuzz_of_quotings_keeps_no_part_of_a_secret_and_every_other_value():
    """SDD §13.4 (reviews RV51-03, RV51-04, RV51-05): mappings of credential and plain keys, each
    credential a canary over quotes, backslashes and the shell's and JSON's syntax, in JSON, Python
    reprs, YAML block and flow, the shell's quotings and an exception repr - alone and inside one
    and two JSON strings. No canary survives in part, and every plain value stays."""
    rng = random.Random(51)
    alphabet = "ab \"'\\,}:;.)]{&=\t/#>!<|$`*?n"
    for _ in range(300):
        pairs, canaries, plain = [], [], []
        for i in range(rng.randint(2, 5)):
            if rng.random() < 0.5:
                body = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 9)))
                pairs.append(
                    (f"k{i}_" + rng.choice(["password", "token", "api_key"]), f"Q{i}x{body}Z{i}k")
                )
                canaries += [f"Q{i}x", f"Z{i}k"]
            else:
                pairs.append((f"k{i}_" + rng.choice(["port", "user", "host"]), f"P{i}p"))
                plain.append(f"P{i}p")
        for text in _quotings(pairs):
            for levels in (0, 1, 2):
                held = _nested({"stdout": text}, levels) if levels else text
                out = redact(held)
                assert not [c for c in canaries if c in out], (held, out)
                assert all(p in out for p in plain), (held, out)
