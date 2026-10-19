"""The one redaction layer (SDD §13.4, ADV-06): no secret leaves for Jev or agentmemory."""

import json
import re
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
