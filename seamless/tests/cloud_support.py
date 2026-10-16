"""An in-process OpenStack cloud for tests: Seamless's own providers.openstack.connect() reads a
synthetic clouds.yaml and builds openstacksdk's Connection over a keystoneauth session, and every
request goes to a transport mounted on that session that answers in process - no socket is opened.

What a stub cannot show, this does: keystoneauth's own parsing of the version documents, so a
service's maximum microversion arrives as keystoneauth hands it (a tuple, ``(2, 96)``), not as
the text a test double would hand (review RV18-01, RV18-02). Every credential is a placeholder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from seamless_migrate.config import Settings

AUTH_URL = "https://identity.example.org/v3"
CATALOG = {
    "identity": "https://identity.example.org/v3",
    "compute": "https://compute.example.org/v2.1",
    "block-storage": "https://block-storage.example.org/v3/p1",
    "network": "https://network.example.org",
    "image": "https://image.example.org",
}


@dataclass
class InProcessCloud:
    """A cloud whose compute API's maximum is ``compute`` and block storage's ``block_storage``;
    ``roles`` are its token's, ``catalog`` replaces endpoints, ``auth_url`` is where the clouds.yaml
    entry signs in. ``connect`` is what OpenStackProvider takes as ``connect_fn``."""

    root: Path
    compute: str = "2.96"
    block_storage: str | None = "3.71"
    roles: tuple[str, ...] = ("member",)
    auth_url: str = AUTH_URL
    catalog: dict[str, str] = field(default_factory=dict)
    seen: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        pytest.importorskip("requests")
        pytest.importorskip("openstack")
        self.root.mkdir(parents=True, exist_ok=True)
        clouds = self.root / "clouds.yaml"
        clouds.write_text(
            "clouds:\n  dst:\n    auth:\n"
            f"      auth_url: {self.auth_url}\n"
            "      username: svc\n      password: synthetic-placeholder\n"
            "      project_name: finance\n      user_domain_name: Default\n"
            "      project_domain_name: Default\n"
            "    region_name: RegionOne\n",
            encoding="utf-8",
        )
        self.settings = Settings(clouds_yaml=clouds)

    def _routes(self) -> dict[tuple[str, str], tuple[int, Any, dict[str, str]]]:
        urls = {**CATALOG, **self.catalog}
        token = {
            "token": {
                "methods": ["password"],
                "expires_at": "2099-01-01T00:00:00.000000Z",
                "user": {"id": "u1", "name": "svc", "domain": {"id": "default", "name": "D"}},
                "project": {
                    "id": "p1",
                    "name": "finance",
                    "domain": {"id": "default", "name": "D"},
                },
                "roles": [{"id": f"r{i}", "name": name} for i, name in enumerate(self.roles)],
                "catalog": [
                    {
                        "type": kind,
                        "id": kind,
                        "name": kind,
                        "endpoints": [
                            {
                                "id": kind,
                                "interface": "public",
                                "region": "RegionOne",
                                "region_id": "RegionOne",
                                "url": url,
                            }
                        ],
                    }
                    for kind, url in urls.items()
                ],
            }
        }
        identity = {
            "id": "v3.14",
            "status": "stable",
            "links": [{"rel": "self", "href": self.auth_url}],
        }
        compute = {
            "id": "v2.1",
            "status": "CURRENT",
            "version": self.compute,
            "min_version": "2.1",
            "links": [{"rel": "self", "href": urls["compute"].rstrip("/") + "/"}],
        }
        compute_url = urls["compute"].rstrip("/")
        image = urls["image"].rstrip("/")
        auth = self.auth_url.rstrip("/")
        routes: dict[tuple[str, str], tuple[int, Any, dict[str, str]]] = {
            ("GET", auth): (200, {"version": identity}, {}),
            ("POST", auth + "/auth/tokens"): (201, token, {"X-Subject-Token": "t"}),
            ("GET", compute_url): (200, {"version": compute}, {}),
            ("GET", compute_url.rsplit("/", 1)[0]): (300, {"versions": [compute]}, {}),
            ("GET", image): (
                300,
                {
                    "versions": [
                        {
                            "id": "v2.16",
                            "status": "CURRENT",
                            "links": [{"rel": "self", "href": image + "/v2/"}],
                        }
                    ]
                },
                {},
            ),
        }
        if self.block_storage is not None:
            block = urls["block-storage"].rstrip("/")
            document = {
                "versions": [
                    {
                        "id": "v3.0",
                        "status": "CURRENT",
                        "version": self.block_storage,
                        "min_version": "3.0",
                        "links": [{"rel": "self", "href": block.split("/v3")[0] + "/v3/"}],
                    }
                ]
            }
            for url in (block.split("/v3")[0], block.split("/v3")[0] + "/v3", block):
                routes[("GET", url)] = (200, document, {})
        return routes

    def connect(self, provider: Any, _settings: Any) -> Any:
        """providers.openstack.connect over this cloud: the same Connection production builds."""
        import requests

        from seamless_migrate.providers.openstack import connect

        routes, seen = self._routes(), self.seen

        class Transport(requests.adapters.BaseAdapter):
            def send(self, request, **kwargs):
                seen.append((request.method, request.url))
                status, body, headers = routes.get(
                    (request.method, request.url.rstrip("/")), (404, {"error": "none"}, {})
                )
                response = requests.models.Response()
                response.request, response.url, response.status_code = (
                    request,
                    request.url,
                    status,
                )
                response._content = json.dumps(body).encode()
                response.headers.update({"Content-Type": "application/json", **headers})
                response.encoding = "utf-8"
                return response

            def close(self):
                pass

        conn = connect(provider, self.settings)
        for scheme in ("http://", "https://"):
            conn.session.session.mount(scheme, Transport())
        return conn
