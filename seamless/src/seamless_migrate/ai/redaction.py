"""The one redaction layer (SDD §13.4, ADV-06).

Everything that leaves the control plane for an AI service passes through it: ``JevClient.call``
redacts every tool argument and ``MemoryClient`` every request body, so no caller can forget it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..domain.text import replace_unstorable

REDACTED = "[REDACTED]"
#: how far past a cut ``redact_cut`` redacts, so it still sees the end of a secret the cut splits
CUT_MARGIN = 16384
_KEYWORDS = (
    r"password|passwd|pwd|pass|secret|client[_-]?secret|token|auth[_-]?token|x-auth-token|"
    r"api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|credentials?"
)
#: A credential key may carry a prefix joined by ``_``/``-``/``.`` (``OS_PASSWORD``,
#: ``vcenter_password``, ``ansible_become_pass``, ``AWS_SECRET_ACCESS_KEY``, ``auth.password``).
_KEY = r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_.-])*(?:" + _KEYWORDS + r")(?![A-Za-z0-9])"
#: The same key as a text is searched for it: each start looks at a prefix of at most 16 parts -
#: a longer one is found from a later part, the value redacted all the same - so a run of "a."
#: costs time linear in its length, not quadratic (SDD §13.4, review RV27-06). It starts after a
#: character but a letter or a digit, or after JSON's escape of a line break or a tab - a key that
#: starts a line of a command's output inside JSON (\npassword: x, review RV51-02).
_SCANNED_KEY = (
    r"(?:(?<![A-Za-z0-9])|(?<=\\[ntr]))(?:[A-Za-z0-9]+[_.-]){0,16}(?:"
    + _KEYWORDS
    + r")(?![A-Za-z0-9])"
)
#: A mapping key whose whole value is a credential: a credential key, or a clouds.yaml ``auth``.
_CREDENTIAL_KEY = re.compile(r"(?i)" + _KEY + r"|auth")
#: A PEM block's first and last lines; the block is redacted from one to the first other after it.
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]+-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]+-----")


#: What may follow a single-quoted value: a delimiter, a closing bracket or ">", a double quote or
#: a backslash - the string holding it ends, a JSON escape follows -, or nothing.
_VALUE_END = r"(?:[\s,;&})\].:\"\\>]|\Z)"
#: A quoted credential key and its separator ('token': ...): a value read past a close it doubts
#: stops before one, which is then redacted on its own (SDD §13.4, review RV51-03).
_QUOTED_KEY_AHEAD = r"'(?:[A-Za-z0-9]+[_.-]){0,16}(?:" + _KEYWORDS + r")'\s*[:=]"
#: A single-quoted string on one line with no backslash before a quote: every quoting agrees on
#: its close - a Python repr, YAML ('' a quote), the shell, any of them inside JSON strings.
_SQ_CLEAN = r"'(?:[^'\\\n]|\\++(?!')|'')*+'"
#: A quoted part the shell glues to its word, never a quoted credential key's name.
_SQ_PART = r"(?:(?!" + _QUOTED_KEY_AHEAD + r")'[^'\n]*+')?"
#: What the shell glues to a closed word: its escaped quote (\') or "'" - JSON's escapes of them
#: too - and the quoted part that opens after, or characters of no shell or JSON syntax ('a'b!),
#: each with a quoted part after; a space, a delimiter or ">" ends the word (SDD §13.4, reviews
#: RV51-05, RV51-20).
_SQ_WORD = (
    r"(?:(?:\\++'|\\*+\"'\\*+\")" + _SQ_PART + r"|[^\s'\"\\,;&|<>(){}\[\]:.]++" + _SQ_PART + r")*+"
)
#: Where a string inside JSON strings closes: the backslashes that opened it before a quote, after
#: any number of the 2E + 2 each of its own backslashes became (E the opening's backslashes); a
#: quote of its own has 2E + 1 before it.
_ESCAPED_CLOSE = r"(?:(?P=escape){2}\\\\)*+(?P=escape)\""


def _auth_block(match: re.Match[str]) -> str:
    end = "\n" if match.group(0).endswith("\n") else ""
    return f"{match.group(1)}auth: {REDACTED}{end}"


#: a dotted chain of base64url runs, each run read once (possessive): where JWTs are looked for
_BASE64URL_CHAIN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]++(?:\.[A-Za-z0-9_-]++)*+")


def _jwt_start(run: str) -> int:
    """Where a JWT's header starts in one run - at the run's start or after a "-" in it, "eyJ"
    and 8 characters more -; -1 when it holds none."""
    position = 0
    while (index := run.find("eyJ", position)) >= 0 and len(run) - index - 3 >= 8:
        if index == 0 or run[index - 1] == "-":
            return index
        position = index + 1
    return -1


def _redact_jwts(match: re.Match[str]) -> str:
    """Each JWT of a dotted chain - a header, then two parts of 8 characters at least - replaced.
    Every run is looked at once, so "eyJ-eyJ-..." costs time linear in its length while a JWT
    glued after a "-" - an OAuth state, a session id - is found (SDD §13.4, reviews RV31-01,
    RV32B-06)."""
    runs, out, i = match.group(0).split("."), [], 0
    while i < len(runs):
        if i + 2 < len(runs) and len(runs[i + 1]) >= 8 and len(runs[i + 2]) >= 8:
            start = _jwt_start(runs[i])
            if start >= 0:
                out.append(runs[i][:start] + REDACTED)
                i += 3
                continue
        out.append(runs[i])
        i += 1
    return ".".join(out)


_PATTERNS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    # the auth section of a clouds.yaml: block style (every line indented under "auth:") ...
    (re.compile(r"(?m)^([ \t]*)auth:[ \t]*\n(?:\1[ \t]+\S[^\n]*(?:\n|$))+"), _auth_block),
    # ... and JSON or YAML flow style ("auth": {...})
    (re.compile(r"(?i)(?<![\w.-])([\"']?auth[\"']?\s*:\s*)\{[^{}]*\}"), r"\1" + REDACTED),
    # credentials in URLs and connection strings: scheme://user:pass@host (a pass with "@" too)
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,63}://)[^\s/@:]+:[^\s/]*@"), r"\1[REDACTED]@"),
    # Authorization headers (any scheme)
    (re.compile(r"(?i)\b(authorization)\s*[:=]\s*(?:\w+\s+)?[^\s,;'\"]+"), r"\1: " + REDACTED),
    # bare bearer tokens
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + REDACTED),
    # key=value / key: value / "key": "value" (the key possibly prefixed, e.g. OS_PASSWORD), which
    # also covers X-Auth-Token / X-Subject-Token headers and Password=...; in connection strings.
    # A quoted value runs to its own closing quote, read in every quoting it may be in, each run
    # once: JSON's \" and YAML's '' stay inside it, a Python repr's \' too, the shell's '\'' and
    # '"'"', and a string inside JSON strings - Ansible's output carries JSON in JSON (\"key\":
    # \"value\") - closes at the escape that opened it, however deep; where the readings disagree
    # on its end, it runs to the longest one's. A key's quote is looked for where its backslashes
    # start, so a run of them costs time linear in its length (SDD §13.4, audit AD-08, reviews
    # RV50-01, RV50-10, RV51-02 to RV51-05, RV51-20, RV51-21)
    (
        re.compile(
            r"(?i)((?:(?<!\\)\\*+[\"'])?" + _SCANNED_KEY + r"(?:\\*+[\"'])?\s*[:=]\s*)"
            r"(\"(?:[^\"\\]|\\.)*+\""  # a JSON or a shell string
            # a single-quoted string on one line whose close every quoting agrees on, and what the
            # shell glues to it
            r"|" + _SQ_CLEAN + _SQ_WORD +
            # one with a backslash before a quote - an escaped quote in a repr (\', inside JSON
            # \\'), a close in YAML or the shell ('C:\', YAML's \''): the value runs on, the longer
            # reading, over the shell's quotes to a close a value end follows, or to its line's end,
            # stopping before a quoted credential key
            r"|'(?:[^'\\\n]|\\++(?!')|'')*+\\++''?+"
            r"(?:\\*+\"'\\*+\"'?+|[^'\\\n]|\\++(?:''?+)?+|''|'(?=\\++')"
            r"|(?!" + _QUOTED_KEY_AHEAD + r")'(?!" + _VALUE_END + r"))*+"
            r"(?:(?=" + _QUOTED_KEY_AHEAD + r")|'|(?=\n)|\Z)"
            r"|'(?:[^']|'')*+'"  # a YAML single-quoted string over lines
            # a string inside JSON strings, to its close, a bare quote or its line's end
            r"|(?P<escape>\\++)\"(?:[^\"\\\n]|(?!" + _ESCAPED_CLOSE + r")\\++\"?)*+"
            r"(?:" + _ESCAPED_CLOSE + r"|(?=[\n\"])|\Z)"
            # a [REDACTED] an earlier pass wrote is left as it is: a second pass changes nothing
            r"|\[REDACTED\](?![A-Za-z0-9])|[^\s,;&}]+)"
        ),
        r"\1" + REDACTED,
    ),
    # CLI form: --os-password hunter2, --apikey x, --authToken x - a flag name of at most 64
    # characters ending in a credential word, read once per run of dashes - after any character
    # but a dash, so also after the "n" of a line break Ansible's JSON prints as backslash-n, or
    # glued to a word -, a text costing time linear in its length (SDD §13.4, reviews RV27-06,
    # RV31-02, RV32B-01); --password=hunter2 is the key=value form above, and -p hunter2 too
    # ambiguous. The value is a quoted string whole, or an argv list's next item - JSON's, a
    # repr's (CalledProcessError), inside JSON strings - to the quote that opened it, at that
    # escape (review RV52-X3)
    (
        re.compile(
            r"(?i)(?<!-)(--[A-Za-z0-9-]{1,64}+"
            r"(?:(?<=password)|(?<=passwd)|(?<=secret)|(?<=token)|(?<=key))"
            r"(?:\s+|(?P<argesc>\\*+)[\"']\s*,\s*(?P=argesc)(?P<argq>[\"'])))"
            r"((?(argq)(?:(?!(?P=argesc)(?P=argq))(?:\\.|[^\n]))*+"
            r"|(?:\"(?:[^\"\\\n]|\\.)*+\"|'[^'\n]*+'|\S+)))"
        ),
        r"\1" + REDACTED,
    ),
    # well-known token shapes: seamless API tokens, Keystone fernet tokens, JWTs
    (re.compile(r"\bsmg_[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bgAAAAA[A-Za-z0-9_-]{20,}"), REDACTED),
    # JWTs: each dotted base64url run read once (SDD §13.4, reviews RV31-01, RV32B-06)
    (_BASE64URL_CHAIN, _redact_jwts),
    # API key shapes: AWS access key ids, GitHub tokens, sk- keys, Slack tokens
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}|\bgithub_pat_[A-Za-z0-9_]{22,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), REDACTED),
)


def _redact_pem(text: str) -> str:
    """Each PEM block (a key, a certificate) - its BEGIN line to the first END line after it -
    replaced. No END line after a BEGIN line means none after a later one either, so each END
    line is looked for once: time linear in the text (SDD §13.4, review RV27-06)."""
    out: list[str] = []
    pos = 0
    while (begin := _PEM_BEGIN.search(text, pos)) is not None:
        end = _PEM_END.search(text, begin.end())
        if end is None:
            break
        out += [text[pos : begin.start()], "[REDACTED PEM]"]
        pos = end.end()
    return "".join(out) + text[pos:]


def redact(text: str, redact_names: Sequence[str] | None = None) -> str:
    """Remove secrets (and optionally VM names) before text leaves the control plane; a NUL or a
    lone surrogate is written U+FFFD (SDD §13.4)."""
    out = _redact_pem(replace_unstorable(str(text)))
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return replace_names(out, {name: "[vm]" for name in redact_names or []})


def replace_names(text: str, replacements: dict[str, str]) -> str:
    r"""Replace each name by its replacement wherever it stands as a word (SDD §13.4): between
    characters that are not letters or digits ([^\W_]), so web-01-data, web-01_root and data-web-01
    lose web-01 while web-010 (another VM) keeps it. One pass over every name - the longest first
    where two start at the same place -, so what a replacement wrote is never searched again, and
    the text is the same in every process, whatever its string hash seed (review RV22-04)."""
    names = sorted({n for n in replacements if n}, key=lambda n: (-len(n), n))
    if not names:
        return str(text)
    words = re.compile(rf"(?<![^\W_])(?:{'|'.join(map(re.escape, names))})(?![^\W_])")
    return words.sub(lambda m: replacements[m.group(0)], str(text))


def redact_cut(text: str, limit: int, redact_names: Sequence[str] | None = None) -> str:
    """``redact`` the first ``limit`` + CUT_MARGIN characters, then cut to ``limit`` (SDD §13.4).

    Cutting first could leave a PEM block without its END line or a URL without the ``@`` of its
    credentials, which ``redact`` no longer recognizes; the margin bounds the work whatever the
    text's length."""
    return redact(str(text)[: limit + CUT_MARGIN], redact_names)[:limit]


def redact_tail(text: str, limit: int, redact_names: Sequence[str] | None = None) -> str:
    """``redact`` the last ``limit`` + CUT_MARGIN characters, then keep the last ``limit``
    (SDD §13.4): keeping the end first could drop the BEGIN line of a PEM block it ends in."""
    return redact(str(text)[-(limit + CUT_MARGIN) :], redact_names)[-limit:]


def redact_payload(value: Any, redact_names: Sequence[str] | None = None) -> Any:
    """``redact`` every string of a JSON-like value; a credential key's whole value is removed,
    and a key's NUL or lone surrogate is written U+FFFD (SDD §13.4)."""
    if isinstance(value, str):
        return redact(value, redact_names)
    if isinstance(value, Mapping):
        return {
            (replace_unstorable(key) if isinstance(key, str) else key): REDACTED
            if isinstance(key, str) and _CREDENTIAL_KEY.fullmatch(key)
            else redact_payload(item, redact_names)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_payload(item, redact_names) for item in value]
    return value
