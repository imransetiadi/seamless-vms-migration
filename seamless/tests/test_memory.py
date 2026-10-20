import json

import httpx
import pytest

from seamless_migrate.ai.memory import MemoryClient, MemoryHit, parse_hits
from seamless_migrate.ai.redaction import redact, replace_names
from seamless_migrate.config import Settings


def test_redact_removes_secrets():
    text = (
        "login failed password=hunter2 for admin; token: abc123def; "
        'json {"password": "s3cr3t", "api_key": "AKIA-xyz"} '
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl "
        "curl -H 'X-Auth-Token: gAAAAABlongfernettokenvalue1234567890' "  # gitleaks:allow
        "db postgresql+psycopg://seamless:pgpass@db.internal:5432/seamless "
        "key -----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAK\n-----END RSA PRIVATE KEY----- end "
        "api token smg_abcdefghijklmnopqrstuvwxyz012345"
    )
    out = redact(text)
    for secret in (
        "hunter2",
        "abc123def",
        "s3cr3t",
        "AKIA-xyz",
        "eyJhbGciOiJIUzI1NiJ9",
        "gAAAAABlongfernettokenvalue",
        "pgpass",
        "MIIEow",
        "smg_abcdefghijklmnopqrstuvwxyz012345",
    ):
        assert secret not in out, secret
    assert "db.internal:5432/seamless" in out  # the non-secret part of the URL survives
    assert "login failed" in out and "end" in out
    assert redact("Bearer abcdef0123456789") == "Bearer [REDACTED]"


def test_redact_prefixed_credential_keys_and_cli_flags():
    """The shapes OpenStack, Ansible and VMware errors actually use (Security.md R-08 review)."""
    cases = {
        "OS_PASSWORD=hunter2 export failed": "hunter2",
        "vcenter_password=hunter2": "hunter2",
        "ansible_become_pass=hunter2": "hunter2",
        "ssh_password: hunter2": "hunter2",
        "AWS_SECRET_ACCESS_KEY=AKIAxxxx": "AKIAxxxx",
        "OS_AUTH_TOKEN=gAAAAABshorttoken": "gAAAAABshorttoken",
        '"auth.password": "s3cr3t"': "s3cr3t",
        "--os-password hunter2 --os-username admin": "hunter2",
        "openstack --os-token=abc123 server list": "abc123",
    }
    for text, secret in cases.items():
        out = redact(text)
        assert secret not in out, (text, out)
    assert "export failed" in redact("OS_PASSWORD=hunter2 export failed")
    assert redact("--os-password hunter2 --os-username admin").endswith("--os-username admin")
    # ordinary words that merely contain a keyword are untouched
    assert redact("passthrough=1 tokenizer: fast") == "passthrough=1 tokenizer: fast"


def test_redact_names_optional():
    text = "web-01 failed; web-010 is fine; db-02 too"
    assert redact(text) == text
    out = redact(text, ["web-01", "db-02"])
    assert "web-01 " not in out and "db-02" not in out and "web-010" in out


def test_redact_names_covers_names_built_from_the_vm_name():
    """SDD §13.4 (advisor Phase 1 audit, A10): disks and ports named after the VM do not carry its
    name out of the installation; a name inside another word stays - another VM's."""
    text = (
        "disk web-01-data, boot web-01_root, nic data-web-01, host web-01.lab; "
        "web-010 and xweb-01 are other VMs"
    )
    assert redact(text, ["web-01"]) == (
        "disk [vm]-data, boot [vm]_root, nic data-[vm], host [vm].lab; "
        "web-010 and xweb-01 are other VMs"
    )


def test_names_are_replaced_in_one_pass_whose_replacements_are_never_searched_again():
    """SDD §13.4 (review RV22-04): one pass over every name, the longest first where two start at
    the same place; what a replacement wrote is never searched again - a VM named like a word of
    the descriptor the provider's name became leaves the descriptor whole, and "[vm]" is no VM
    named "vm"."""
    names = {"RHOSO prod (Jakarta)": "RHOSO 18.0", "RHOSO": "[vm]"}
    assert replace_names("required on RHOSO prod (Jakarta); VM RHOSO", names) == (
        "required on RHOSO 18.0; VM [vm]"
    )
    assert redact("web-01 and vm", ["web-01", "vm"]) == "[vm] and [vm]"


def captured(handler_result, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = handler_result(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


async def test_memory_remember_payload_and_auth_header():
    seen: list[httpx.Request] = []
    transport = captured(lambda r: (201, {"success": True}), seen)
    client = MemoryClient("http://memory:3111", "mem-secret", "seamless-test", transport=transport)
    ok = await client.remember(
        "cutover of web-01 failed: password=hunter2",
        "bug",
        ["seamless", "warm", "cutover"],
        files=["seamless/src/x.py"],
    )
    assert ok is True
    [req] = seen
    assert req.method == "POST" and str(req.url) == "http://memory:3111/agentmemory/remember"
    assert req.headers["authorization"] == "Bearer mem-secret"
    body = json.loads(req.content)
    assert body["type"] == "bug" and body["project"] == "seamless-test"
    assert body["concepts"] == ["seamless", "warm", "cutover"]
    assert body["files"] == ["seamless/src/x.py"]
    assert "hunter2" not in body["content"] and "web-01" in body["content"]

    seen.clear()
    no_secret = MemoryClient(
        "http://memory:3111", None, "p", transport=transport, redact_names=True
    )
    await no_secret.remember("web-01 broke", "fact", [], names=["web-01"])
    assert "authorization" not in seen[0].headers
    assert "web-01" not in json.loads(seen[0].content)["content"]


async def test_memory_search_tolerant_parsing():
    seen: list[httpx.Request] = []
    payload = {
        "mode": "compact",
        "results": [
            {"obsId": "o1", "title": "Warm cutover timed out on conv host", "score": 0.82},
            {
                "content": "Raise os_migrate_timeout for >500G volumes",
                "title": "timeouts",
                "score": "0.5",
            },
        ],
    }
    client = MemoryClient(
        "http://memory:3111/", None, "proj", transport=captured(lambda r: (200, payload), seen)
    )
    hits = await client.search("warm cutover Timeout", limit=5)
    assert hits == [
        MemoryHit(
            title="Warm cutover timed out on conv host",
            content="Warm cutover timed out on conv host",
            score=0.82,
        ),
        MemoryHit(
            title="timeouts", content="Raise os_migrate_timeout for >500G volumes", score=0.5
        ),
    ]
    body = json.loads(seen[0].content)
    assert body == {"query": "warm cutover Timeout", "limit": 5, "project": "proj"}
    assert str(seen[0].url) == "http://memory:3111/agentmemory/smart-search"

    assert parse_hits({"memories": [{"narrative": "n1"}]}, 5) == [
        MemoryHit(title="n1", content="n1", score=None)
    ]
    assert parse_hits({"items": ["plain text"]}, 5)[0].content == "plain text"
    assert parse_hits({"results": [{"observation": {"title": "t", "narrative": "deep"}}]}, 5) == [
        MemoryHit(title="t", content="deep", score=None)
    ]
    assert parse_hits([{"content": "a"}, {"content": "b"}], 1) == [
        MemoryHit(title="a", content="a", score=None)
    ]
    assert parse_hits({"unexpected": True}, 5) == []
    assert parse_hits("garbage", 5) == []


def test_memory_hits_are_redacted_where_they_enter():
    """SDD §13.4 (audit AP-05): agentmemory is an outside service, so its hits are redacted as they
    enter - a title, a content, a nested observation, a plain-text item - before anything cuts or
    publishes them: a plain item's title is cut from its redacted text, so credentials in a URL
    whose "@" lies past the cut never survive in it."""
    secret = "Hunter2-Prod-Secret"  # gitleaks:allow
    hits = parse_hits(
        {
            "results": [
                {
                    "title": f"db-01 OS_PASSWORD={secret}",
                    "content": f"stop failed; OS_PASSWORD={secret}",
                },
                {
                    "observation": {
                        "title": "t",
                        "narrative": f"ran --os-password {secret} server stop",
                    }
                },
                f"{'x' * 50} https://admin:{secret}@keystone.dc1.example:5000/v3 refused",
            ]
        },
        5,
    )
    assert len(hits) == 3
    assert all(secret[:8] not in h.title + h.content for h in hits), hits


@pytest.mark.parametrize(
    "text",
    [
        "a." * 16384,  # a credential key's prefix, tried again from every start
        "ab." * 10923,
        "-" * 32768,  # a CLI flag's name: a line of dashes, as logs print them
        "a" * 32768,  # a URL scheme
        "-----BEGIN A-----" * 4000,  # PEM blocks that never end
        # quoted values that never close: each key's value runs only to its own quote (AD-08)
        'password: \\"x ' * 2520,
        "password: '" + "''" * 16384,
        'password: "' + '\\"' * 16384,
        # a run of backslashes: a key's quote may follow one, read once from its start (RV50-01)
        "\\" * 32768,
        "\\" * 32767 + '"',
        # single-quoted values whose close is glued to text, or comes lines later, and strings
        # inside JSON strings each one level deeper: every value is read once (RV50-10)
        "password: 'a'x " * 2340,
        "password: 'a\n" * 2730,
        "".join("password: " + "\\" * depth + '"a ' for depth in range(1, 220)),
        # YAML plain values of several words that never reach their line's end, an argv list's
        # flags whose value never closes, a JSON-escaped auth section's lines (RV52-X3, RV52-X4)
        "token: a " * 3640 + ",",
        "--os-password', '" * 1820,
        '"auth:\\n' + "   p: q\\n" * 3270,
        # what the shell glues to a double-quoted value: parts that never close, and a chain of
        # them, at the top and inside JSON strings (RV52-X6)
        'password="a""' + "x" * 32768,
        'password="a"" ' * 2340,
        'password="a"' + '"b"' * 8190,
        '{"m": "password=\\"a\\"' + '\\"b\\"' * 5460 + '"}',
        # a CLI flag's shell word: an unclosed quote before a run of escaped ones, and quoted
        # parts (RV52-X3)
        '--os-password "' + '\\"' * 16384,
        "--os-password " + "'a'\"b\"" * 5460,
    ],
    ids=[
        "key prefix",
        "dotted words",
        "dash line",
        "scheme letters",
        "pem begins",
        "escaped quotes",
        "doubled quotes",
        "quotes escaped in a string",
        "backslash run",
        "backslash run then a quote",
        "glued single-quoted closes",
        "single-quoted values over lines",
        "deeper and deeper escapes",
        "plain values to no line end",
        "argv flags never closed",
        "json auth section lines",
        "glued quote never closed",
        "glued quotes never closed",
        "glued quoted parts",
        "glued escaped parts",
        "flag quote never closed",
        "flag quoted parts",
    ],
)
def test_redact_costs_time_linear_in_the_text(text):
    """SDD §13.4 (review RV27-06): redact runs in the event loop - over agentmemory's hits and
    every migration.log line - for a time linear in the text's length: 32 KiB of the texts that
    made a pattern quadratic cost well under a second, never seconds."""
    import time

    start = time.perf_counter()
    redact(text)
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize(
    "unit",
    [
        "eyJ-",
        "eyJaaaaaaaa.",
        "eyJaaaaaaaa.aaaaaaa.",
        "-eyJaaaaaaaa",
        "--x ",
        "a--",
        "a.",
        "-",
        "ab.",
        "\\",
    ],
    ids=[
        "jwt starts",
        "jwt headers",
        "jwt halves",
        "jwts after dashes",
        "flag starts",
        "flags after words",
        "key prefix",
        "dash line",
        "dotted words",
        "backslashes",
    ],
)
def test_four_times_the_text_costs_redact_about_four_times_the_time(unit):
    """SDD §13.4 (reviews RV27-06, RV31-01): every pattern of redact costs time linear in the
    text's length. 64 KiB of a text built to start a pattern every few characters costs at most
    eight times 16 KiB of it - a quadratic pattern costs sixteen - each the best of three runs, so
    another load on the machine slows both alike."""
    import time

    def cost(text):
        best = float("inf")
        for _ in range(3):
            start = time.perf_counter()
            redact(text)
            best = min(best, time.perf_counter() - start)
        return best

    small = cost(unit * (16384 // len(unit)))
    large = cost(unit * (65536 // len(unit)))
    assert large < 8 * small + 0.02, (small, large)


def test_redact_finds_a_credential_word_glued_into_a_cli_flag():
    """SDD §13.4 (review RV31-02): a CLI flag whose name, at most 64 characters, ends in a
    credential word - glued to the word before it or not, in any case - has its value redacted;
    another flag keeps its value, and the flag after it is still looked at."""
    secret = "Hunter2-Prod-Secret"  # gitleaks:allow
    for flag in (
        "--apikey",
        "--authToken",
        "--clientSecret",
        "--adminpassword",
        "--os-password",
        "--sshkey",
        "--os-application-credential-secret",
    ):
        assert secret not in redact(f"tool {flag} {secret} --next"), flag
    text = f"tool --name web-01 --password {secret}"
    assert redact(text) == "tool --name web-01 --password [REDACTED]"


def test_redact_finds_a_cli_flag_after_an_escaped_line_break():
    """SDD §13.4 (review RV32B-01): Ansible prints a failed task's result as one JSON line, where a
    line break of a command or its output is a backslash and an "n" - a flag that starts a line
    follows a letter. A flag is read after any character but a dash, so its value is redacted -
    also glued after a word -, and a flag of another name keeps its value."""
    import json

    secret = "Hunter2-Prod-Secret"  # gitleaks:allow
    result = {"cmd": "openstack --os-cloud src server list \\\n--os-password " + secret}
    line = "fatal: [localhost]: FAILED! => " + json.dumps(result)
    assert "\\n--os-password" in line  # as ansible-playbook prints it
    assert secret not in redact(line)
    for glued in ("x--password", "1--password", "_--password", "a-b--password", "x---password"):
        assert secret not in redact(f"{glued} {secret}"), glued
    assert redact(f"x--name {secret}") == f"x--name {secret}"


def test_redact_finds_a_jwt_glued_after_a_dash():
    """SDD §13.4 (review RV32B-06): a JWT is redacted where it starts a base64url run or follows a
    dash inside one - an OAuth state, a session id, a token file's name - as the pattern before the
    linear rewrite found it; one glued after a letter or "_" is no JWT start, as before."""
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWFtbGVzcyJ9.Zq7PlaceholderSignature0"  # gitleaks:allow
    payload = "eyJzdWIiOiJzZWFtbGVzcyJ9"
    for text in (
        f"GET /sso/return/state-{jwt} 302",
        f"session-{jwt}",
        f"a.b-{jwt}.tail",
        jwt,
        f"Bearer {jwt}",
    ):
        assert payload not in redact(text), text
    assert redact(f"state-{jwt}.tail") == "state-[REDACTED].tail"
    assert payload in redact(f"x_{jwt}")  # no start after a word character, as ever
    # a header or a part shorter than 8 characters is no JWT
    for text in (
        "eyJabc.defghijkl.mnopqrstu",
        "eyJhbGciOiJIUzI1NiJ9.short.mnopqrstu",
        "eyJhbGciOiJIUzI1NiJ9.defghijkl.short",
    ):
        assert redact(text) == text, text


def test_redact_still_finds_what_its_linear_patterns_could_hide():
    """SDD §13.4 (review RV27-06): the linear patterns find what the unbounded ones found - a key
    with more than 16 prefix parts, a PEM block whose BEGIN line has no END line of its own, a long
    URL scheme, a long CLI flag."""
    secret = "Hunter2-Prod-Secret"  # gitleaks:allow
    deep = ".".join("abcdefghijklmnopqrstu") + f".password={secret}"
    assert secret not in redact(deep)
    pem = f"x -----BEGIN A----- {secret} -----BEGIN B----- {secret} -----END B----- tail"
    assert redact(pem) == "x [REDACTED PEM] tail"
    assert redact(f"-----BEGIN A----- {secret}") == f"-----BEGIN A----- {secret}"  # no END: none
    url = "postgresql+psycopg2+extra://svc:" + secret + "@db.example:5432/x"
    assert secret not in redact(url)
    assert secret not in redact("--os-application-credential-secret " + secret)


def test_a_memory_hit_is_redacted_and_cut_as_it_arrives():
    """SDD §13.4 (review RV27-06): a hit's content is redacted and cut at 4000 characters as
    redact_cut cuts it - a secret the cut splits is still found - and its title at 200."""
    secret = "Hunter2-Prod-Secret"  # gitleaks:allow
    content = "x" * 3970 + f" https://svc:{secret}@db.example:5432 " + "y" * 20000
    [hit] = parse_hits({"results": [{"title": "t" * 500, "content": content}]}, 5)
    assert len(hit.content) == 4000 and secret[:6] not in hit.content
    assert hit.title == "t" * 200


def test_only_the_first_limit_hits_of_an_answer_are_read(monkeypatch):
    """SDD §13.4 (review RV27-06): parse_hits reads hits until it has `limit` of them."""
    from seamless_migrate.ai import memory as memory_module

    seen = []
    one = memory_module._hit
    monkeypatch.setattr(memory_module, "_hit", lambda item: seen.append(item) or one(item))
    hits = parse_hits({"results": [{}, *({"content": f"c{i}"} for i in range(10))]}, 3)
    assert [h.content for h in hits] == ["c0", "c1", "c2"]
    assert len(seen) == 4, "an item without content counts no hit; the reading stops at the third"


async def test_one_agentmemory_hit_cannot_hold_the_event_loop():
    """SDD §13.4 (review RV27-06): a search's hits are redacted in the event loop; one hit of the
    text that made a redaction pattern quadratic leaves every other coroutine running."""
    import asyncio
    import time

    def memory(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"title": "notes", "content": "a." * 8192}]})

    client = MemoryClient("http://memory:3111", None, "p", transport=httpx.MockTransport(memory))
    ticks: list[float] = []

    async def ticker():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    tick = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    try:
        assert await client.search("cutover failed", limit=3)
        await asyncio.sleep(0.05)
    finally:
        tick.cancel()
    gap = max(later - earlier for earlier, later in zip(ticks, ticks[1:], strict=False))
    assert gap < 1.0, f"no other coroutine ran for {gap:.1f} s while one hit was redacted"


@pytest.mark.parametrize("mode", ["http_error", "connect_error", "bad_json"])
async def test_memory_failures_swallowed(mode):
    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "connect_error":
            raise httpx.ConnectError("refused", request=request)
        if mode == "bad_json":
            return httpx.Response(200, content=b"<html>")
        return httpx.Response(500, json={"error": "boom"})

    client = MemoryClient("http://memory:3111", None, "p", transport=httpx.MockTransport(handler))
    assert await client.remember("x", "fact", []) is False
    assert await client.search("x") == []
    assert await client.health() is False
    assert client.last_error


async def test_a_memory_failure_says_what_happened():
    """The Advisor page shows memory.last_error: a timeout reads like Jev's ("search timed out
    after 5 s"), and an error without a message names its kind without an empty tail."""

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    client = MemoryClient("http://memory:3111", None, "p", transport=httpx.MockTransport(timeout))
    assert await client.search("x") == []
    assert (client.available, client.last_error) == (False, "search timed out after 5 s")
    assert await client.remember("x", "fact", []) is False
    assert client.last_error == "remember timed out after 5 s"
    quick = MemoryClient(
        "http://memory:3111", None, "p", transport=httpx.MockTransport(timeout), timeout_s=2.5
    )
    await quick.search("x")
    assert quick.last_error == "search timed out after 2.5 s"

    def silent(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("", request=request)

    client = MemoryClient("http://memory:3111", None, "p", transport=httpx.MockTransport(silent))
    assert await client.health() is False
    assert client.last_error == "health: ConnectError"


@pytest.mark.parametrize("call", ["health", "remember"])
async def test_an_unexpected_memory_answer_is_redacted_before_it_is_cut(call):
    """SDD §13.4 (review RV27-07): health() and remember() name agentmemory's unexpected answer
    in last_error, which the Advisor page shows every viewer - redacted before it is cut to 120
    characters, so credentials in a URL whose "@" lies past the cut are still found."""
    secret = "Hunter2-Prod-Secret"  # gitleaks:allow

    def answer(pad):
        text = "x" * pad + f" https://svc:{secret}@db.example:5432/agentmemory unreachable"
        if call == "health":
            return {"status": "degraded", "detail": text}
        return {"success": False, "error": text}

    # the secret starts 10 characters before the cut, so its "@" lies past it
    pad = next(n for n in range(300) if str(answer(n)).find(secret) == 110)

    def memory(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answer(pad))

    client = MemoryClient("http://memory:3111", None, "p", transport=httpx.MockTransport(memory))
    if call == "health":
        assert await client.health() is False
    else:
        assert await client.remember("lesson", "bug", ["seamless"]) is False
    assert "unexpected answer" in client.last_error
    assert secret[:6] not in client.last_error, client.last_error


async def test_memory_health_and_from_settings():
    client = MemoryClient(
        "http://memory:3111",
        None,
        "p",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok"})),
    )
    assert await client.health() is True and client.available is True
    assert MemoryClient.from_settings(Settings()) is None
    built = MemoryClient.from_settings(
        Settings(memory_url="http://localhost:3111", memory_project="x", memory_redact_names=True)
    )
    assert built.project == "x" and built.redact_names is True


@pytest.mark.parametrize(
    ("score", "kept"),
    [
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        ("nan", None),
        ("-inf", None),
        (True, None),
        (10**400, None),
        ("0.82", 0.82),
        (0.5, 0.5),
        (1, 1.0),
    ],
    ids=[
        "nan",
        "inf",
        "-inf",
        "nan-text",
        "-inf-text",
        "true",
        "400-digits",
        "text",
        "float",
        "int",
    ],
)
def test_a_hit_score_is_a_finite_number_or_none(score, kept):
    """SDD §11, §14.3 (review RV50-04): a hit's score is a finite number or a number's text;
    NaN, an infinity - as the answer's token, a number past a float's range or text -, a JSON
    true and an integer no float holds are no score: the store keeps no such number."""
    [hit] = parse_hits({"results": [{"title": "t", "content": "c", "score": score}]}, 5)
    assert hit.score == kept
