"""Control-plane settings, read from ``SEAMLESS_*`` environment variables (SDD §15.1, §18)."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

JevMode = Literal["off", "stdio", "http"]
#: where the Jev model runs (SDD §13.4, ADV-06)
JevDeployment = Literal["on_prem", "remote"]

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def find_repo_root(start: Path | None = None) -> Path:
    """The collection/repository root: first ancestor holding ``galaxy.yml`` (else the cwd)."""
    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "galaxy.yml").is_file():
            return candidate
    return Path.cwd()


#: characters Python's IDNA 2003 maps where browsers (UTS #46, nontransitional) keep them: their
#: hosts' ASCII forms differ - Python writes strasse for the browser's xn--strae-oqa (RV31-03)
_IDNA_DEVIATIONS = frozenset("\u00df\u03c2\u200c\u200d")
_IPV4_DIGITS = {10: "0123456789", 16: "0123456789abcdefABCDEF", 8: "01234567"}
#: what the URL Standard forbids in a domain - a browser sends no host that holds one
_FORBIDDEN_IN_HOST = frozenset(" #%/:<>?@[\\]^|\x7f") | {chr(c) for c in range(0x20)}


class _IdnaDeviation(ValueError):
    """A host whose ASCII form Python's IDNA 2003 writes otherwise than browsers."""


def _ipv4_number(part: str) -> int | None:
    """A part of an IPv4 address as the URL Standard reads it: decimal, 0x hexadecimal or 0 octal;
    None when it is no such number."""
    base = 10
    if part[:2] in ("0x", "0X"):
        part, base = part[2:], 16
    elif len(part) > 1 and part[0] == "0":
        part, base = part[1:], 8
    if not part:
        return 0 if base == 16 else None
    if any(c not in _IPV4_DIGITS[base] for c in part):
        return None
    return int(part, base)


def _ipv4_host(host: str) -> int | None:
    """The URL Standard's IPv4 parser over a host that ends in a number: up to four parts, each a
    number, the last filling the bytes left; None for a host it rejects."""
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    if len(parts) > 4:
        return None
    numbers: list[int] = []
    for part in parts:
        number = _ipv4_number(part)
        if number is None:
            return None
        numbers.append(number)
    *head, last = numbers
    if any(n > 255 for n in head) or last >= 256 ** (5 - len(numbers)):
        return None
    return last + sum(n * 256 ** (3 - i) for i, n in enumerate(head))


def _ends_in_a_number(host: str) -> bool:
    """The URL Standard's check: the last label - a trailing dot aside - is all digits or 0x hex."""
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    last = parts[-1]
    return (bool(last) and all(c in "0123456789" for c in last)) or (
        last[:2] in ("0x", "0X") and _ipv4_number(last) is not None
    )


def _ipv6_text(address: ipaddress.IPv6Address) -> str:
    """An IPv6 address as the URL Standard writes it: eight hex pieces, the first longest run of
    two or more zero pieces compressed - never the dotted tail Python 3.13 writes for ::ffff:."""
    pieces = [int.from_bytes(address.packed[i : i + 2], "big") for i in range(0, 16, 2)]
    start, length, i = -1, 1, 0
    while i < 8:
        if pieces[i]:
            i += 1
            continue
        j = i
        while j < 8 and not pieces[j]:
            j += 1
        if j - i > length:
            start, length = i, j - i
        i = j
    words = [f"{piece:x}" for piece in pieces]
    if start < 0:
        return ":".join(words)
    return ":".join(words[:start]) + "::" + ":".join(words[start + length :])


def _browser_origin(scheme: str, host: str, port: int | None) -> str | None:
    """An origin as a browser serializes it, by the URL Standard's host parser (SDD §15.1, reviews
    RV27-08, RV31-03): an IPv6 address in hex pieces, a percent-encoded host decoded, a host ending
    in a number an IPv4 address in any of its forms written dotted, any other host lower-cased and
    in punycode; no default port. None for a host it rejects; _IdnaDeviation for one whose ASCII
    form Python writes otherwise than browsers."""
    if ":" in host:
        try:
            address = ipaddress.IPv6Address(host)
        except ValueError:
            return None
        if address.scope_id:
            return None  # a zone: no browser sends one
        host = f"[{_ipv6_text(address)}]"
    else:
        host = unquote(host)
        if _FORBIDDEN_IN_HOST & set(host):
            return None
        if _ends_in_a_number(host):
            number = _ipv4_host(host)
            if number is None:
                return None
            host = str(ipaddress.IPv4Address(number))
        elif _IDNA_DEVIATIONS & set(host):
            raise _IdnaDeviation(host)
        else:
            try:
                host = host.encode("idna").decode("ascii").lower()
            except UnicodeError:
                return None
    default = {"http": 80, "https": 443}.get(scheme)
    return f"{scheme}://{host}" if port in (None, default) else f"{scheme}://{host}:{port}"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_dir: Path = Path("./data")
    db_url: str = ""
    host: str = "127.0.0.1"
    #: SDD §15.1 states the bounds; a value outside them refuses to start the control plane
    port: int = Field(default=8080, ge=1, le=65535)
    demo: bool = False
    demo_speed: float = Field(default=60.0, gt=0)
    demo_seed: int = 42
    demo_failure_rate: float = Field(default=0.1, ge=0, le=1)
    auth_disabled: bool = False
    tokens_file: Path | None = None
    cors_origins: list[str] = Field(default_factory=list)
    clouds_yaml: Path | None = None
    secrets_dir: Path = Path("/var/run/secrets/seamless")
    #: where dashboard-entered credentials are written (SDD §13.3)
    secret_store: Literal["files", "kubernetes"] = "files"
    k8s_namespace: str | None = None
    ansible_playbook: str = "ansible-playbook"
    collection_root: Path = Field(default_factory=find_repo_root)
    #: ANSIBLE_HOST_KEY_CHECKING of every playbook run (SDD §15.1): off while SEC-02 is open
    ansible_host_key_checking: bool = False
    max_concurrent_migrations: int = Field(default=10, ge=1)
    max_concurrent_cutovers: int = Field(default=3, ge=1)
    tick_s: float = Field(default=1.0, gt=0)
    #: how often a completed migration's source VM is checked for running again (SDD §8.1)
    source_watch_s: float = Field(default=300.0, ge=0)
    max_step_retries: int = Field(default=2, ge=0)
    #: wall-clock ceiling of one step attempt in seconds; 0 = unbounded (SDD §15.1)
    step_timeout_s: float = Field(default=0.0, ge=0)
    #: failed bearer authentications per client address per minute before 429 (0 = off)
    auth_lockout_per_minute: int = Field(default=60, ge=0)
    jev_mode: JevMode = "off"
    jev_command: str = "npx -y @jkudish/jev-mcp@0.14.1"
    jev_url: str | None = None
    jev_token: str | None = Field(default=None, repr=False)
    jev_timeout_s: float = Field(default=20.0, gt=0)
    jev_min_confidence: float = Field(default=0.6, ge=0, le=1)
    #: where the model runs and the host shown on its badge; with a remote model the console
    #: excerpt stays local unless console_screening_send_to_remote (SDD §13.4, ADV-06)
    jev_deployment: JevDeployment = "remote"
    jev_endpoint_host: str | None = None
    console_screening_send_to_remote: bool = False
    memory_url: str | None = None
    memory_secret: str | None = Field(default=None, repr=False)
    memory_project: str = "seamless-migrate"
    memory_redact_names: bool = False
    dashboard_dir: Path | None = None
    metrics_public: bool = False
    log_level: str = "INFO"
    log_json: bool = False

    def model_post_init(self, __context: object) -> None:
        if not self.db_url:
            object.__setattr__(self, "db_url", f"sqlite:///{self.data_dir / 'seamless.db'}")

    @field_validator("cors_origins")
    @classmethod
    def _one_origin_each(cls, origins: list[str]) -> list[str]:
        """Each entry is one origin as a browser sends it - never "*", which would let every page
        call the API (SDD §15.1, Security.md C1-12, audit AP-10)."""
        for origin in origins:
            refused = f"{origin!r} is not an origin (scheme://host[:port], no path; never '*')"
            try:
                parts = urlsplit(origin)
            except ValueError:  # a bracketed host Python refuses, an unclosed bracket (RV32B-08)
                raise ValueError(refused) from None
            try:
                port_ok = parts.port is None or 1 <= parts.port <= 65535
            except ValueError:  # a port that is no number or out of range
                port_ok = False
            if not (
                parts.scheme in ("http", "https")
                and parts.hostname
                and port_ok
                and not (parts.username or parts.password)
                # nothing after the authority - no path, not even "/", no query or fragment - and
                # the scheme as browsers send it
                and origin == f"{parts.scheme}://{parts.netloc}"
            ):
                raise ValueError(refused)
            # the comparison is exact: an origin written otherwise than the browser sends it would
            # start the server and allow nothing (SDD §15.1, review RV27-08)
            try:
                sent = _browser_origin(parts.scheme, parts.hostname, parts.port)
            except _IdnaDeviation:
                raise ValueError(
                    f"{origin!r}: its host holds \u00df, \u03c2 or a zero-width joiner, which "
                    "Python writes otherwise than a browser - write the origin's xn-- form as the "
                    "browser sends it (its Origin header)"
                ) from None
            if origin != sent:
                raise ValueError(
                    f"{origin!r} is not an origin as a browser sends it: write {sent!r}"
                    if sent
                    else f"{origin!r} is not an origin a browser sends"
                )
        return origins

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = source.get(f"SEAMLESS_{name}")
            return value if value is not None and value.strip() != "" else None

        values: dict[str, object] = {}
        text_fields = {
            "DATA_DIR": "data_dir",
            "DB_URL": "db_url",
            "HOST": "host",
            "TOKENS_FILE": "tokens_file",
            "CLOUDS_YAML": "clouds_yaml",
            "SECRETS_DIR": "secrets_dir",
            "SECRET_STORE": "secret_store",
            "K8S_NAMESPACE": "k8s_namespace",
            "ANSIBLE_PLAYBOOK": "ansible_playbook",
            "COLLECTION_ROOT": "collection_root",
            "JEV_MODE": "jev_mode",
            "JEV_COMMAND": "jev_command",
            "JEV_URL": "jev_url",
            "JEV_TOKEN": "jev_token",
            "JEV_DEPLOYMENT": "jev_deployment",
            "JEV_ENDPOINT_HOST": "jev_endpoint_host",
            "MEMORY_URL": "memory_url",
            "MEMORY_SECRET": "memory_secret",
            "MEMORY_PROJECT": "memory_project",
            "LOG_LEVEL": "log_level",
        }
        for env_name, field in text_fields.items():
            value = get(env_name)
            if value is not None:
                values[field] = value.strip()
        numeric_fields = {
            "PORT": ("port", int),
            "DEMO_SPEED": ("demo_speed", float),
            "DEMO_SEED": ("demo_seed", int),
            "DEMO_FAILURE_RATE": ("demo_failure_rate", float),
            "MAX_CONCURRENT_MIGRATIONS": ("max_concurrent_migrations", int),
            "MAX_CONCURRENT_CUTOVERS": ("max_concurrent_cutovers", int),
            "TICK_S": ("tick_s", float),
            "SOURCE_WATCH_S": ("source_watch_s", float),
            "MAX_STEP_RETRIES": ("max_step_retries", int),
            "STEP_TIMEOUT_S": ("step_timeout_s", float),
            "AUTH_LOCKOUT_PER_MINUTE": ("auth_lockout_per_minute", int),
            "JEV_TIMEOUT_S": ("jev_timeout_s", float),
            "JEV_MIN_CONFIDENCE": ("jev_min_confidence", float),
        }
        for env_name, (field, cast) in numeric_fields.items():
            value = get(env_name)
            if value is not None:
                values[field] = cast(value)
        bool_fields = {
            "DEMO": "demo",
            "AUTH_DISABLED": "auth_disabled",
            "MEMORY_REDACT_NAMES": "memory_redact_names",
            "METRICS_PUBLIC": "metrics_public",
            "LOG_JSON": "log_json",
            "CONSOLE_SCREENING_SEND_TO_REMOTE": "console_screening_send_to_remote",
            "ANSIBLE_HOST_KEY_CHECKING": "ansible_host_key_checking",
        }
        for env_name, field in bool_fields.items():
            value = get(env_name)
            if value is not None:
                values[field] = _bool(value)
        cors = get("CORS_ORIGINS")
        if cors is not None:
            values["cors_origins"] = _csv(cors)
        if "jev_mode" in values:
            values["jev_mode"] = str(values["jev_mode"]).lower()
        if "jev_deployment" in values:
            values["jev_deployment"] = str(values["jev_deployment"]).lower()

        dashboard = get("DASHBOARD_DIR")
        if dashboard is not None and dashboard.lower() != "auto":
            values["dashboard_dir"] = Path(dashboard)
        else:
            root = Path(str(values.get("collection_root") or find_repo_root()))
            candidate = root / "dashboard" / "dist"
            values["dashboard_dir"] = candidate if candidate.is_dir() else None
        return cls(**values)  # type: ignore[arg-type]

    @property
    def is_postgres(self) -> bool:
        return self.db_url.startswith("postgresql")
