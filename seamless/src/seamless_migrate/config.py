"""Control-plane settings, read from ``SEAMLESS_*`` environment variables (SDD §15.1, §18)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

JevMode = Literal["off", "stdio", "http"]

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


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_dir: Path = Path("./data")
    db_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8080
    demo: bool = False
    demo_speed: float = 60.0
    demo_seed: int = 42
    demo_failure_rate: float = 0.1
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
    max_concurrent_migrations: int = 10
    max_concurrent_cutovers: int = 3
    tick_s: float = 1.0
    max_step_retries: int = 2
    #: wall-clock ceiling of one step attempt in seconds; 0 = unbounded (SDD §15.1)
    step_timeout_s: float = 0.0
    #: failed bearer authentications per client address per minute before 429 (0 = off)
    auth_lockout_per_minute: int = 60
    jev_mode: JevMode = "off"
    jev_command: str = "npx -y @jkudish/jev-mcp@0.14.1"
    jev_url: str | None = None
    jev_token: str | None = Field(default=None, repr=False)
    jev_timeout_s: float = 20.0
    jev_min_confidence: float = 0.6
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
