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
    r"password|passwd|passphrase|pwd|pass|secret|client[_-]?secret|token|auth[_-]?token|"
    r"x-auth-token|api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|credentials?|"
    # an Authorization header of any scheme, quoted in JSON or a repr (review RV52-X5)
    r"authorization"
)
#: The words a key may end in glued to another word in any case (``adminpassword``,
#: ``ROOTPASSWD``) - never ``pass`` or ``pwd``, which end other words (``bypass``, ``oldpwd``).
_LONG_KEYWORDS = r"(?=[pst])(?:pass(?:word|wd|phrase)|secret|token)"
#: Where a key's word starts inside a word, read case-sensitively: an upper-case letter after a
#: lower-case one or a digit (``adminPass``, Nova's server password; ``accessToken``), or the last
#: capital of an acronym (``DBPassword``) - so ``bypass`` and ``isPassive`` stay (review RV52-X7).
_CAMEL = r"(?-i:(?=[A-Z])(?:(?<=[a-z0-9])|(?<=[A-Z])(?=.[a-z])))"
#: The digits a key's word may end in: ``password2``, ``token_3`` (review RV52-X7).
_KEY_DIGITS = r"(?:[_-]?+[0-9]++)?+"
#: A credential key may carry a prefix joined by ``_``/``-``/``.`` (``OS_PASSWORD``,
#: ``vcenter_password``, ``ansible_become_pass``, ``AWS_SECRET_ACCESS_KEY``, ``auth.password``),
#: or joined to its word in camelCase or, for a long word, in one case (SDD §13.4, RV52-X7).
_KEY = (
    r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_.-])*(?:(?:[A-Za-z0-9]*?"
    + _CAMEL
    + r")?(?:"
    + _KEYWORDS
    + r")|[A-Za-z0-9]+?(?:"
    + _LONG_KEYWORDS
    + r"))"
    + _KEY_DIGITS
    + r"(?![A-Za-z0-9])"
)
#: The same key as a text is searched for it: each start looks at a prefix of at most 16 parts -
#: a longer one is found from a later part, the value redacted all the same - so a run of "a."
#: costs time linear in its length, not quadratic (SDD §13.4, review RV27-06). It starts after a
#: character but a letter or a digit, or after JSON's escape of a line break or a tab - a key that
#: starts a line of a command's output inside JSON (\npassword: x, review RV51-02) -, and a word
#: glued to another starts where camelCase starts it, a long word anywhere (review RV52-X7).
_SCANNED_KEY = (
    r"(?:(?:(?<![A-Za-z0-9])|(?<=\\[ntr]))(?:[A-Za-z0-9]+[_.-]){0,16}(?:"
    + _KEYWORDS
    + r")|(?<=[A-Za-z0-9])(?-i:(?=[A-Zpst]))(?:"
    + _CAMEL
    + r"(?:"
    + _KEYWORDS
    + r")|"
    + _LONG_KEYWORDS
    + r"))"
    + _KEY_DIGITS
    + r"(?![A-Za-z0-9])"
)
#: A mapping key whose whole value is a credential: a credential key, or a clouds.yaml ``auth``.
_CREDENTIAL_KEY = re.compile(r"(?i)" + _KEY + r"|auth")
#: A terminal's escape character - raw, as JSON's \u001b at any depth or as a repr's \x1b - with
#: the colour or erase-line sequence it starts (ESC [ ... m, ESC [ ... K): removed before the
#: patterns run, so a key or a value a terminal coloured is found. Never right after a part of
#: such an escape (\, \u0, ..., \x1), which the removal would complete: no escape is left, and a
#: second pass changes nothing (SDD §13.4, review RV52-X7).
_ESCAPE = re.compile(
    r"(?<!\\)(?<!\\u)(?<!\\u0)(?<!\\u00)(?<!\\u001)(?<!\\x)(?<!\\x1)"
    r"(?:\x1b|\\++(?:u001[bB]|x1[bB]))(?:\[[0-?]*+[mK])?+"
)
#: A PEM block's first and last lines; the block is redacted from one to the first other after it.
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]+-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]+-----")


#: What may follow a single-quoted value: a delimiter, a closing bracket or ">", a double quote or
#: a backslash - the string holding it ends, a JSON escape follows -, or nothing.
_VALUE_END = r"(?:[\s,;&})\].:\"\\>]|\Z)"
#: A quoted credential key and its separator ('token': ...): a value read past a close it doubts
#: stops before one, which is then redacted on its own (SDD §13.4, review RV51-03).
_QUOTED_KEY_AHEAD = (
    r"'(?:[A-Za-z0-9]+[_.-]){0,16}(?:(?:[A-Za-z0-9]*?"
    + _CAMEL
    + r")?(?:"
    + _KEYWORDS
    + r")|[A-Za-z0-9]+?(?:"
    + _LONG_KEYWORDS
    + r"))"
    + _KEY_DIGITS
    + r"'\s*[:=]"
)
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
#: What is glued to a closed double-quoted value, at the top or inside JSON strings: characters of
#: no shell or JSON syntax ("a"b is one word). Never a quoted part: a value that ends a JSON or a
#: repr list's item would take that item's closing quote and the next one's opening quote for one,
#: and the next key and its secret with them (SDD §13.4, review RV52-X6, PR #58 review).
_GLUED = r"[^\s'\"\\,;&|<>(){}\[\]:.]*+"


def _word(stops: str) -> str:
    r"""One character of a word that ends at white space or a character of ``stops`` and runs no
    further than a JSON-escaped line break (\n, \r in a JSON string): the next line's key, flag
    or quoted key is then redacted on its own. A word that runs on in letters and digits to its
    end or to a backslash before one (C:\new\root) keeps it; that look reads up to the next
    backslash, so a text costs time linear in its length (SDD §13.4, PR #58 review)."""
    return (
        rf"(?:[^\s\\{stops}]|\\(?:(?![nr])"
        rf"|(?=[nr][A-Za-z0-9]*+(?:[\s{stops}]|\Z|\\[A-Za-z0-9]))))"
    )


#: Where a string inside JSON strings closes: the backslashes that opened it before a quote, after
#: any number of the 2E + 2 each of its own backslashes became (E the opening's backslashes); a
#: quote of its own has 2E + 1 before it.
_ESCAPED_CLOSE = r"(?:(?P=escape){2}\\\\)*+(?P=escape)\""


def _auth_block(match: re.Match[str]) -> str:
    """The auth section's lines as one, ending in the line break its last one ended in: LF or
    CRLF (review RV52-X4)."""
    block = match.group(0)
    end = next((e for e in ("\r\n", "\n") if block.endswith(e)), "")
    return f"{match.group(1)}auth: {REDACTED}{end}"


#: One parameter of an Authorization header's scheme: key=value, the value a quoted string - at the
#: top, or inside a JSON string at its escape - or a word with no comma, quote or white space.
_HEADER_PARAM = r"[\w-]+=(?:\"(?:[^\"\\\n]|\\.)*+\"|\\\"(?:[^\"\\\n]|\\[^\"])*+\\\"|[^\s,\"'\\]+)"
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


#: The auth section of a clouds.yaml in JSON or YAML flow style ("auth": {...}): a quoted string
#: in it is read whole, so a brace in a password neither ends the section nor keeps it open (PR #58
#: review). Looked for again after the other patterns: a section a value's redaction closed - its
#: inner key's value ran to the section's brace - or one that held another goes whole in the same
#: pass, so a second pass changes nothing (SDD §13.4, review RV52-X7).
_AUTH_FLOW = re.compile(
    r"(?i)(?<![\w.-])([\"']?auth[\"']?\s*:\s*)"
    r"\{(?:[^{}\"'\n]|\"(?:[^\"\\\n]|\\.)*+\"|'(?:[^'\n]|'')*+')*+\}"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    # the auth section of a clouds.yaml: block style (every line indented under "auth:", a
    # comment after it, LF or CRLF line breaks) ...
    (
        re.compile(r"(?m)^([ \t]*)auth:[ \t]*(?:#[^\n]*)?\r?\n(?:\1[ \t]+\S[^\n]*(?:\n|$))+"),
        _auth_block,
    ),
    # ... and JSON or YAML flow style ("auth": {...})
    (_AUTH_FLOW, r"\1" + REDACTED),
    # credentials in URLs and connection strings: scheme://user:pass@host (a pass with "@" too)
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,63}://)[^\s/@:]+:[^\s/]*@"), r"\1[REDACTED]@"),
    # Authorization headers (any scheme); a [REDACTED] an earlier pass wrote is left as it is, so
    # a second pass changes nothing (review RV52-X5)
    (
        re.compile(
            r"(?i)\b(authorization)\s*[:=]\s*(?:[\w-]+\s+)?+(?!\[REDACTED\](?![A-Za-z0-9]))"
            # a scheme's parameters - Digest's response, AWS4's Signature -, key=value pairs apart
            # by commas, a value quoted (in a JSON string at its escape) or a word (PR #58 review)
            r"(?:" + _HEADER_PARAM + r"(?:\s*,\s*" + _HEADER_PARAM + r")*+"
            r"|" + _word(",;'\"") + "+)"
        ),
        r"\1: " + REDACTED,
    ),
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
            # a JSON or a shell string, and what the shell glues to it
            r"(\"(?:[^\"\\]|\\.)*+\""
            + _GLUED
            +
            # a single-quoted string on one line whose close every quoting agrees on, and what the
            # shell glues to it
            r"|"
            + _SQ_CLEAN
            + _SQ_WORD
            +
            # one with a backslash before a quote - an escaped quote in a repr (\', inside JSON
            # \\'), a close in YAML or the shell ('C:\', YAML's \''): the value runs on, the longer
            # reading, over the shell's quotes to a close a value end follows, or to its line's end,
            # stopping before a quoted credential key
            r"|'(?:[^'\\\n]|\\++(?!')|'')*+\\++''?+"
            r"(?:\\*+\"'\\*+\"'?+|[^'\\\n]|\\++(?:''?+)?+|''|'(?=\\++')"
            r"|(?!" + _QUOTED_KEY_AHEAD + r")'(?!" + _VALUE_END + r"))*+"
            r"(?:(?=" + _QUOTED_KEY_AHEAD + r")|'|(?=\n)|\Z)"
            # a YAML single-quoted string over lines, and what the shell glues to it: its [REDACTED]
            # never stands glued to the word after it (review RV52-X6)
            r"|'(?:[^']|'')*+'"
            + _SQ_WORD
            +
            # a string inside JSON strings, to its close, a bare quote or its line's end
            r"|(?P<escape>\\++)\"(?:[^\"\\\n]|(?!"
            + _ESCAPED_CLOSE
            + r")\\++\"?)*+"
            r"(?:" + _ESCAPED_CLOSE + _GLUED + r"|(?=[\n\"])|\Z)"
            # a [REDACTED] an earlier pass wrote is left as it is: a second pass changes nothing
            r"|\[REDACTED\](?![A-Za-z0-9])"
            # a YAML plain value of several words runs to its line's end, a comment or its
            # string's: every word of a password (review RV52-X4); a shell word (key=value) and
            # a header's value ("X-Auth-Token: x" url) end at a space. No word of it holds "=" or
            # a backslash, starts with "-" or ends in ": ": a later key=, --flag or key: ends the
            # reading, and that key is redacted on its own; each value is read once
            r"|(?<=:[ \t])[^\s,;&}\"'\\=]++"
            r"(?:[ \t]++(?![#-])(?:[^\s,;&}\\\"'=:]|:(?![\s\\]|\Z))++)++"
            r"(?=[ \t]*+(?:#|\r?\n|\\[nr]|[\"']|\Z))"
            # one word, no further than a JSON-escaped line break (PR #58 review)
            r"|" + _word(",;&}") + r"+)"
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
            r"(?:(?<=password)|(?<=passwd)|(?<=passphrase)|(?<=secret)|(?<=token)|(?<=key))"
            r"(?:\s+|(?P<argesc>\\{0,7}+)[\"']\s*,\s*(?P=argesc)(?P<argq>[\"'])))"
            r"((?(argq)(?:(?!(?P=argesc)(?P=argq))(?:\\.|[^\n]))*+"
            # a value that opens a quote: the quoted string and the rest of its word, one that
            # never closes to its line's end; else one word. A quote further in is read as part
            # of the word: as a quoted part, a list item's closing quote and the next one's
            # opening quote would join two items (review RV52-X3, PR #58 review)
            r"|(?:(?:\"(?:[^\"\\\n]|\\.?)*+(?:\"|(?=\n)|\Z)|'[^'\n]*+(?:'|(?=\n)|\Z))"
            + _word("")
            + r"*+|"
            + _word("")
            + r"+)))"
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
    # Slack incoming webhook URLs, a Slack channel's credential (SDD §13.4, §24.3, §24.8, plan task
    # NT7): the path after /services/, to white space, a quote, a bracket, a backslash (a JSON
    # string's escape) or a delimiter; a [REDACTED] an earlier pass wrote is left as it is
    (
        re.compile(
            r"(?i)\b((?:https?://)?hooks\.slack\.com/services/)(?!\[REDACTED\])"
            r"[^\s\"'<>\\\[\](){},;]+"
        ),
        r"\1" + REDACTED,
    ),
    # Ceph keys (SDD §13.4, §28.8): a keyring's `key = <base64>` line, whole after its `=`, and a
    # bare CephX key as `ceph auth get-key` prints it - AQ, 36 base64 characters and == (28 bytes)
    (re.compile(r"(?im)^([ \t]*key[ \t]*=[ \t]*)\S+"), r"\1" + REDACTED),
    (re.compile(r"\bAQ[A-Za-z0-9+/]{36}==(?![A-Za-z0-9+/=])"), REDACTED),
)


def _redact_pem(text: str) -> str:
    """Each PEM block (a key, a certificate) - its BEGIN line to the first END line after it -
    replaced, and an END line no BEGIN line comes before - a block a cut fell inside, a console's
    last lines (SDD §7.5) - with the whole text before it. No END line after a BEGIN line means
    none after a later one either, so each END line is looked for once: time linear in the text
    (SDD §13.4, reviews RV27-06, RV52-X1, PR #58 review)."""
    out: list[str] = []
    pos = 0
    first_end = _PEM_END.search(text)
    if first_end is not None and _PEM_BEGIN.search(text, 0, first_end.start()) is None:
        out.append("[REDACTED PEM]")
        pos = first_end.end()
    while (begin := _PEM_BEGIN.search(text, pos)) is not None:
        end = _PEM_END.search(text, begin.end())
        if end is None:
            break
        out += [text[pos : begin.start()], "[REDACTED PEM]"]
        pos = end.end()
    return "".join(out) + text[pos:]


def redact(text: str, redact_names: Sequence[str] | None = None) -> str:
    """Remove secrets (and optionally VM names) before text leaves the control plane; a NUL or a
    lone surrogate is written U+FFFD, and a terminal's escape character removed (SDD §13.4)."""
    out = replace_unstorable(str(text))
    if "\x1b" in out or "\\u001" in out or "\\x1" in out:
        out = _ESCAPE.sub("", out)
    out = _redact_pem(out)
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    out = _AUTH_FLOW.sub(r"\1" + REDACTED, out)
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
