"""Builders for API tests: settings with role tokens, an app and a TestClient."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import Role
from seamless_migrate.security.auth import generate_token, hash_token
from seamless_migrate.store import Store

ROLES = [Role.viewer, Role.operator, Role.approver, Role.admin]


@dataclass
class Api:
    client: TestClient
    store: Store
    settings: Settings
    tokens: dict[Role, str]

    def h(self, role: Role | str | None) -> dict[str, str]:
        if role is None:
            return {}
        return {"Authorization": f"Bearer {self.tokens[Role(role)]}"}

    def get(self, path: str, role: Role | str | None = Role.viewer, **kw: Any):
        return self.client.get(path, headers=self.h(role), **kw)

    def post(self, path: str, role: Role | str | None = Role.admin, json: Any = None, **kw: Any):
        return self.client.post(path, headers=self.h(role), json=json, **kw)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.store.events(since_seq=0, limit=100000)]


def write_tokens(path: Path) -> dict[Role, str]:
    tokens = {role: generate_token() for role in ROLES}
    lines = ["tokens:"]
    for role, token in tokens.items():
        lines.append(
            f"  - {{name: {role.value}-user, role: {role.value}, sha256: {hash_token(token)}}}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return tokens


def api_settings(tmp_path: Path, **kw: Any) -> tuple[Settings, dict[Role, str]]:
    tokens = write_tokens(tmp_path / "tokens.yaml")
    data: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "tokens_file": tmp_path / "tokens.yaml",
        "demo": True,
        "demo_speed": 1e7,
        "demo_failure_rate": 0.0,
        "tick_s": 0.02,
        "dashboard_dir": None,
    }
    data.update(kw)
    return Settings(**data), tokens


SOURCE = {
    "id": "src-osp",
    "name": "RHOSP 17.1 (finance)",
    "kind": "openstack",
    "role": "source",
    "endpoint": "https://keystone.src.example:13000/v3",
    "cloud": "src",
    "conversion_host": {"name": "conv-src", "flavor": "m1.large"},
}
DESTINATION = {
    "id": "dst-rhoso",
    "name": "RHOSO 18.0",
    "kind": "rhoso",
    "role": "destination",
    "endpoint": "https://keystone.dst.example:5000/v3",
    "cloud": "dst",
    "conversion_host": {"name": "conv-dst", "flavor": "m1.large"},
}
