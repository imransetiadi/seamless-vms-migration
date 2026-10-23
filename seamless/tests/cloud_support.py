"""An in-process OpenStack cloud for tests: Seamless's own providers.openstack.connect() reads a
synthetic clouds.yaml and builds openstacksdk's Connection over a keystoneauth session, and every
request goes to a transport mounted on that session that answers in process - no socket is opened.

What a stub cannot show, this does: keystoneauth's own parsing of the version documents, so a
service's maximum microversion arrives as keystoneauth hands it (a tuple, ``(2, 96)``), not as
the text a test double would hand (review RV18-01, RV18-02) - and openstacksdk's own Placement
proxy, its version discovery and keystoneauth's request log (PR #62 review). Every credential is a
placeholder.
"""

from __future__ import annotations

import json
import re
import urllib.parse
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
#: where the catalog lists Placement, when a cloud has one
PLACEMENT_URL = "https://placement.example.org"
#: where the catalog lists Heat and Octavia, when a cloud has them (SDD §10, plan task N2)
HEAT_URL = "https://orchestration.example.org/v1/p1"
OCTAVIA_URL = "https://load-balancer.example.org"
#: where the catalog lists Designate, when a cloud has it (SDD §10, plan task N2b)
DESIGNATE_URL = "https://dns.example.org"


@dataclass
class InProcessCloud:
    """A cloud whose compute API's maximum is ``compute`` and block storage's ``block_storage``;
    ``roles`` are its token's, ``catalog`` replaces endpoints, ``auth_url`` is where the clouds.yaml
    entry signs in. ``servers`` are Nova's servers of the credential's project (``{id, name}``),
    which ``GET /servers`` and ``GET /servers/detail`` list with a ``name`` filter read as Nova
    reads it: a regular expression searched anywhere in the name - after Nova's escape of ``|``
    when ``nova_db`` is ``mysql`` (MySQL/MariaDB REGEXP), as sent when ``postgresql`` (``~``) - and
    a 400 for one that is no regular expression (the API schema's ``regex`` format). ``flavors``
    are what ``GET /flavors/detail`` lists (``{name, vcpus, ram, disk, extra_specs}``).
    ``placement`` gives Placement's answers by path (``/allocation_candidates``: ``(status, body)``)
    and puts Placement in the catalog at ``placement_url``; ``placement_seen`` records each request
    it answers with its headers. ``heat`` and ``octavia`` give Heat's and Octavia's answers by path
    below their endpoints (``/stacks``, ``/v2.0/lbaas/pools``: ``(status, body)``, or an exception
    the transport raises) and put them in the catalog, as ``designate`` gives Designate's
    (``/v2/zones``, plan task N2b) - a request other than a GET answered by its method and path
    (``PUT /v2/zones/z-1/recordsets/rs-1``, plan task N7); ``heat_seen``, ``octavia_seen`` and
    ``designate_seen`` record each GET's path and query, ``written`` each other request's method,
    path and JSON body. ``networks``, ``subnets`` and ``keypairs`` are what Neutron's and
    Nova's lists answer, as a source's inventory reads them - and a keypair's GET by its name
    answers its entry, a 404 for a name none has (``os-keypairs/{name}``, issue #112);
    ``floatingips`` (filtered by the
    query's ``project_id`` and ``port_id``, as Neutron filters them) and ``extensions`` what
    Neutron's answer, and Cinder's volume types none, as a destination's reads them (plan task
    N2b). ``connect`` is what OpenStackProvider
    takes as ``connect_fn``."""

    root: Path
    compute: str = "2.96"
    block_storage: str | None = "3.71"
    roles: tuple[str, ...] = ("member",)
    auth_url: str = AUTH_URL
    catalog: dict[str, str] = field(default_factory=dict)
    seen: list[tuple[str, str]] = field(default_factory=list)
    servers: list[dict[str, Any]] = field(default_factory=list)
    nova_db: str = "postgresql"
    flavors: list[dict[str, Any]] = field(default_factory=list)
    placement: dict[str, tuple[int, Any]] | None = None
    placement_url: str = PLACEMENT_URL
    placement_seen: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    heat: dict[str, Any] | None = None
    heat_seen: list[tuple[str, dict[str, list[str]]]] = field(default_factory=list)
    octavia: dict[str, Any] | None = None
    octavia_seen: list[tuple[str, dict[str, list[str]]]] = field(default_factory=list)
    designate: dict[str, Any] | None = None
    designate_seen: list[tuple[str, dict[str, list[str]]]] = field(default_factory=list)
    written: list[tuple[str, str, Any]] = field(default_factory=list)
    networks: list[dict[str, Any]] = field(default_factory=list)
    subnets: list[dict[str, Any]] = field(default_factory=list)
    keypairs: list[dict[str, Any]] = field(default_factory=list)
    floatingips: list[dict[str, Any]] = field(default_factory=list)
    extensions: list[dict[str, Any]] = field(default_factory=list)

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
        if self.placement is not None:
            urls["placement"] = self.placement_url
        if self.heat is not None:
            urls["orchestration"] = HEAT_URL
        if self.octavia is not None:
            urls["load-balancer"] = OCTAVIA_URL
        if self.designate is not None:
            urls["dns"] = DESIGNATE_URL
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
        if self.placement is not None:
            # Placement's root document: its one version and microversion range (handlers/root.py)
            placement = {
                "id": "v1.0",
                "status": "CURRENT",
                "min_version": "1.0",
                "max_version": "1.39",
                "links": [{"rel": "self", "href": ""}],
            }
            routes[("GET", self.placement_url.rstrip("/"))] = (200, {"versions": [placement]}, {})
        if self.designate is not None:
            # Designate's root document: its one version (designate/api/versions.py)
            designate = {
                "id": "v2.0",
                "status": "CURRENT",
                "links": [{"rel": "self", "href": DESIGNATE_URL + "/v2"}],
            }
            routes[("GET", DESIGNATE_URL)] = (200, {"versions": [designate]}, {})
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
        answers = (
            self._servers_answer,
            self._flavors_answer,
            self._placement_answer,
            self._service_answer,
            self._lists_answer,
            self._keypair_answer,
        )

        class Transport(requests.adapters.BaseAdapter):
            def send(self, request, **kwargs):
                seen.append((request.method, request.url))
                answer = next((a for a in (f(request) for f in answers) if a), None)
                if isinstance(answer, BaseException):
                    raise answer
                status, body, headers = answer or routes.get(
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

    def _flavors_answer(self, request: Any) -> tuple[int, Any, dict[str, str]] | None:
        """Nova's answer to a list of the flavors with their details, None for any other request."""
        compute = {**CATALOG, **self.catalog}["compute"].rstrip("/")
        parts = urllib.parse.urlsplit(request.url)
        if request.method != "GET" or f"{parts.scheme}://{parts.netloc}{parts.path}" != (
            compute + "/flavors/detail"
        ):
            return None
        flavors = [
            {"id": f"flavor-{i}", "links": [], **flavor} for i, flavor in enumerate(self.flavors)
        ]
        return 200, {"flavors": flavors}, {}

    def _placement_answer(self, request: Any) -> tuple[int, Any, dict[str, str]] | None:
        """Placement's answer to a request below its root, by path; None for any other request."""
        root = self.placement_url.rstrip("/")
        parts = urllib.parse.urlsplit(request.url)
        url = f"{parts.scheme}://{parts.netloc}{parts.path}"
        path = url[len(root) :] if url.startswith(root) else ""
        if self.placement is None or not path.strip("/"):
            return None  # not Placement's, or its root document: a route answers it
        self.placement_seen.append((request.method, request.url, dict(request.headers)))
        status, body = self.placement.get(path, (404, {"errors": []}))
        return status, body, {}

    def _service_answer(self, request: Any) -> Any:
        """Heat's, Octavia's or Designate's answer to a request below its endpoint, by path - an
        exception for the transport to raise; None for any other request (SDD §10, plan tasks N2,
        N2b)."""
        parts = urllib.parse.urlsplit(request.url)
        url = f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
        for root, answers, seen in (
            (HEAT_URL, self.heat, self.heat_seen),
            (OCTAVIA_URL, self.octavia, self.octavia_seen),
            (DESIGNATE_URL, self.designate, self.designate_seen),
        ):
            if answers is None or not url.startswith(root + "/"):
                continue
            path = url[len(root) :]
            if request.method == "GET":
                seen.append((path, urllib.parse.parse_qs(parts.query)))
            else:
                self.written.append((request.method, path, json.loads(request.body or "null")))
                path = f"{request.method} {path}"
            answer = answers.get(path, (404, {"error": "none"}))
            if isinstance(answer, BaseException):
                return answer
            status, body = answer
            return status, body, {}
        return None

    def _lists_answer(self, request: Any) -> tuple[int, Any, dict[str, str]] | None:
        """Neutron's network, subnet, floating IP and extension lists, Nova's keypairs and Cinder's
        volume types, as an inventory reads them; None for any other request."""
        urls = {**CATALOG, **self.catalog}
        parts = urllib.parse.urlsplit(request.url)
        url = f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
        network, compute = urls["network"].rstrip("/"), urls["compute"].rstrip("/")
        query = urllib.parse.parse_qs(parts.query)
        floatingips = [
            f
            for f in self.floatingips
            if all(
                str(f.get(key)) in query[key] for key in ("project_id", "port_id") if key in query
            )
        ]
        lists = {
            network + "/v2.0/networks": {"networks": self.networks},
            network + "/v2.0/subnets": {"subnets": self.subnets},
            network + "/v2.0/floatingips": {"floatingips": floatingips},
            network + "/v2.0/extensions": {"extensions": self.extensions},
            compute + "/os-keypairs": {"keypairs": [{"keypair": k} for k in self.keypairs]},
            urls["block-storage"].rstrip("/") + "/types": {"volume_types": []},
        }
        if request.method != "GET" or url not in lists:
            return None
        return 200, lists[url], {}

    def _keypair_answer(self, request: Any) -> tuple[int, Any, dict[str, str]] | None:
        """Nova's answer to a keypair's GET by its name (``os-keypairs/{name}``): the credential's
        user's keypair of that name, 404 when the user holds none; None for any other request."""
        compute = {**CATALOG, **self.catalog}["compute"].rstrip("/") + "/os-keypairs/"
        parts = urllib.parse.urlsplit(request.url)
        url = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if request.method != "GET" or not url.startswith(compute):
            return None
        name = urllib.parse.unquote(url[len(compute) :])
        found = [k for k in self.keypairs if k.get("name") == name]
        if not found:
            return 404, {"itemNotFound": {"code": 404, "message": "Keypair not found"}}, {}
        return 200, {"keypair": found[0]}, {}

    def _servers_answer(self, request: Any) -> tuple[int, Any, dict[str, str]] | None:
        """Nova's answer to a list of the project's servers, None for any other request."""
        method, url = request.method, request.url
        compute = {**CATALOG, **self.catalog}["compute"].rstrip("/")
        parts = urllib.parse.urlsplit(url)
        path = f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
        if method != "GET" or path not in (compute + "/servers", compute + "/servers/detail"):
            return None
        found = list(self.servers)
        names = urllib.parse.parse_qs(parts.query).get("name")
        if names:
            pattern = names[0]
            try:
                re.compile(pattern)
            except re.error:
                return 400, {"badRequest": {"code": 400, "message": "Invalid input"}}, {}
            if self.nova_db == "mysql":
                pattern = pattern.replace("|", "\\|")  # nova/db/utils.py _safe_regex_mysql
            found = [s for s in found if re.search(pattern, s["name"])]
        body = {"servers": [{"id": s["id"], "name": s["name"], "links": []} for s in found]}
        return 200, body, {}
