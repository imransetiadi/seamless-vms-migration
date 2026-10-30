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
    entry signs in. ``servers`` are Nova's servers (``{id, name}``, with ``project_id`` when not of
    ``project``), which ``GET /servers`` and ``GET /servers/detail`` list - those of the project the
    request's token is scoped to alone, as Nova lists a project-scoped token's (``tokens``), the
    detail with the server's other keys and its project as ``tenant_id`` - with a ``name`` filter
    read as Nova reads it: a regular expression searched anywhere in the name - after Nova's escape
    of ``|`` when ``nova_db`` is ``mysql`` (MySQL/MariaDB REGEXP), as sent when ``postgresql``
    (``~``) - and a 400 for one that is no regular expression (the API schema's ``regex`` format);
    a server's GET by id answers it in that detail when it is of the token's project or the token
    holds ``admin`` (Nova's default policy), 404 otherwise; its volume attachments are none and
    Neutron's port list is empty, so a source's ``list_vms`` and ``get_vm`` read them, and a
    project's GET by id answers ``project``'s or an application credential's project, 404
    another's (``projects/{id}``, plan task TC4). ``flavors``
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
    N2b). ``project`` is the project the clouds.yaml credential's token is scoped to (``{id, name,
    domain: {id, name}}``). ``app_credentials`` are Keystone's application credentials by id
    (``{secret, user_id, project, roles, unrestricted, expires_at}``, plan task TC3): each signs in
    to its project as its user - a wrong secret or an unknown id answers 401 -, its token naming it
    restricted unless ``unrestricted``, and its record answers its user's GET of it
    (``users/{user_id}/application_credentials/{id}``), or ``record`` (a status, or an exception the
    transport raises) instead; ``hold``
    (a ``threading.Event``) keeps its sign-in unanswered until it is set, and ``sign_in`` answers
    it instead - a status, or an exception the transport raises (requests' connection refused, TLS
    handshake or timeout, which keystoneauth turns into its own). ``sign_ins`` records who
    each sign-in came with - the clouds.yaml user's name or an application credential's id -,
    ``tokens`` the project and the roles of each token it answered (``{project, roles}``), and
    ``records_read`` each record read. ``connect`` is what OpenStackProvider takes as
    ``connect_fn``, a project's connection too (``project_id``)."""

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
    project: dict[str, Any] = field(
        default_factory=lambda: {
            "id": "p1",
            "name": "finance",
            "domain": {"id": "default", "name": "D"},
        }
    )
    app_credentials: dict[str, dict[str, Any]] = field(default_factory=dict)
    sign_ins: list[str] = field(default_factory=list)
    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    records_read: list[str] = field(default_factory=list)

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
        # a project credential's secret is read from the secrets directory (SDD §13.3, §27.4)
        self.settings = Settings(clouds_yaml=clouds, secrets_dir=self.root / "secrets")

    def _urls(self) -> dict[str, str]:
        """The catalog's endpoints by service type."""
        urls = {**CATALOG, **self.catalog}
        if self.placement is not None:
            urls["placement"] = self.placement_url
        if self.heat is not None:
            urls["orchestration"] = HEAT_URL
        if self.octavia is not None:
            urls["load-balancer"] = OCTAVIA_URL
        if self.designate is not None:
            urls["dns"] = DESIGNATE_URL
        return urls

    def _token(
        self,
        project: dict[str, Any],
        user_id: str,
        roles: Any,
        application_credential: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A Keystone token scoped to ``project`` for ``user_id`` with ``roles``, with the
        catalog; one an application credential signed in with names it (``id``, ``name``,
        ``restricted``), as Keystone's token does."""
        token: dict[str, Any] = {
            "methods": ["application_credential" if application_credential else "password"],
            "expires_at": "2099-01-01T00:00:00.000000Z",
            "user": {"id": user_id, "name": user_id, "domain": {"id": "default", "name": "D"}},
            "project": project,
            "roles": [{"id": f"r{i}", "name": name} for i, name in enumerate(roles)],
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
                for kind, url in self._urls().items()
            ],
        }
        if application_credential is not None:
            token["application_credential"] = application_credential
        return {"token": token}

    def _routes(self) -> dict[tuple[str, str], tuple[int, Any, dict[str, str]]]:
        urls = self._urls()
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

    def connect(self, provider: Any, _settings: Any, project_id: str | None = None) -> Any:
        """providers.openstack.connect over this cloud: the same Connection production builds - with
        the provider's credential for the project ``project_id`` when one is given (SDD §27.4)."""
        import requests

        from seamless_migrate.providers.openstack import connect

        routes, seen = self._routes(), self.seen
        answers = (
            self._sign_in_answer,
            self._record_answer,
            self._project_answer,
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

        conn = (
            connect(provider, self.settings)
            if project_id is None
            else connect(provider, self.settings, project_id=project_id)
        )
        for scheme in ("http://", "https://"):
            conn.session.session.mount(scheme, Transport())
        return conn

    def _sign_in_answer(self, request: Any) -> tuple[int, Any, dict[str, str]] | None:
        """Keystone's answer to a sign-in at the clouds.yaml entry's auth URL, recorded in
        ``sign_ins`` by who it came with: the password's user is scoped to ``project``; an
        application credential signs in to its own project as its user, 401 for a wrong secret or
        an unknown id (SDD §27.7, plan task TC3). None for any other request."""
        if request.method != "POST" or request.url.rstrip("/") != (
            self.auth_url.rstrip("/") + "/auth/tokens"
        ):
            return None
        identity = json.loads(request.body)["auth"]["identity"]
        if "application_credential" not in identity["methods"]:
            self.sign_ins.append(identity["password"]["user"]["name"])
            self.tokens["t"] = {"project": self.project["id"], "roles": list(self.roles)}
            return 201, self._token(self.project, "u1", self.roles), {"X-Subject-Token": "t"}
        given = identity["application_credential"]
        self.sign_ins.append(given["id"])
        known = self.app_credentials.get(given["id"])
        if known is not None and known.get("hold") is not None:
            known["hold"].wait(30)
        answer = known.get("sign_in") if known is not None else None
        if isinstance(answer, BaseException):
            return answer  # the transport raises it: nothing answered
        if answer is not None:
            return answer, {"error": {"code": answer, "title": "Keystone's answer"}}, {}
        if known is None or known["secret"] != given["secret"]:
            refusal = {"code": 401, "title": "Unauthorized", "message": "authentication required"}
            return 401, {"error": refusal}, {}
        application_credential = {
            "id": given["id"],
            "name": f"seamless-{known['project']['name']}",
            "restricted": not known.get("unrestricted", False),
        }
        token = self._token(
            known["project"], known["user_id"], known["roles"], application_credential
        )
        subject = f"t-{len(self.sign_ins)}"
        self.tokens[subject] = {"project": known["project"]["id"], "roles": list(known["roles"])}
        return 201, token, {"X-Subject-Token": subject}

    def _record_answer(self, request: Any) -> tuple[int, Any, dict[str, str]] | None:
        """Keystone's answer to a user's GET of an application credential's record (``users/
        {user_id}/application_credentials/{id}``): its record without its secret, 404 for another
        user's or an unknown one, ``record``'s status instead when it has one; None for any other
        request (plan task TC3)."""
        identity = self._urls()["identity"].rstrip("/") + "/users/"
        parts = urllib.parse.urlsplit(request.url)
        url = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if request.method != "GET" or not url.startswith(identity):
            return None
        found = re.fullmatch(r"([^/]+)/application_credentials/([^/]+)", url[len(identity) :])
        if found is None:
            return None
        user_id, credential_id = (urllib.parse.unquote(part) for part in found.groups())
        self.records_read.append(credential_id)
        known = self.app_credentials.get(credential_id)
        if known is None or known["user_id"] != user_id:
            return 404, {"error": {"code": 404, "title": "Not Found"}}, {}
        if isinstance(known.get("record"), BaseException):
            return known["record"]  # the transport raises it: nothing answered
        if known.get("record") is not None:
            return known["record"], {"error": {"code": known["record"]}}, {}
        record = {
            "id": credential_id,
            "name": f"seamless-{known['project']['name']}",
            "description": None,
            "project_id": known["project"]["id"],
            "roles": [{"id": f"r{i}", "name": name} for i, name in enumerate(known["roles"])],
            "unrestricted": known.get("unrestricted", False),
            "expires_at": known.get("expires_at"),
            "access_rules": [],
            "links": {"self": url},
        }
        return 200, {"application_credential": record}, {}

    def _project_answer(self, request: Any) -> tuple[int, Any, dict[str, str]] | None:
        """Keystone's answer to a project's GET by id (``projects/{id}``), as a token scoped to it
        may read it: ``project``, or an application credential's project, 404 another; None for any
        other request (plan task TC4)."""
        identity = self._urls()["identity"].rstrip("/") + "/projects/"
        parts = urllib.parse.urlsplit(request.url)
        url = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if request.method != "GET" or not url.startswith(identity):
            return None
        wanted = urllib.parse.unquote(url[len(identity) :])
        known = [self.project, *(c["project"] for c in self.app_credentials.values())]
        found = next((p for p in known if p["id"] == wanted), None)
        if found is None:
            return 404, {"error": {"code": 404, "title": "Not Found"}}, {}
        project = {"id": found["id"], "name": found["name"], "domain_id": found["domain"]["id"]}
        return 200, {"project": project}, {}

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
        """Neutron's network, subnet, port (none), floating IP and extension lists, Nova's keypairs
        and Cinder's volume types - below the catalog's project, or below none, as keystoneauth asks
        with a token of another project than the catalog's (Cinder takes either from 3.67, plan
        task TC4) -, as an inventory reads them; None for any other request."""
        urls = {**CATALOG, **self.catalog}
        parts = urllib.parse.urlsplit(request.url)
        url = f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
        network, compute = urls["network"].rstrip("/"), urls["compute"].rstrip("/")
        block = urls["block-storage"].rstrip("/").split("/v3")[0]
        if request.method == "GET" and url == block + "/v3/types":
            return 200, {"volume_types": []}, {}
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
            network + "/v2.0/ports": {"ports": []},
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

    def _server_detail(self, server: dict[str, Any]) -> dict[str, Any]:
        """A server as Nova's detail writes it: its keys, its project as ``tenant_id``."""
        detail = {"links": [], **{k: v for k, v in server.items() if k != "project_id"}}
        return {**detail, "tenant_id": server.get("project_id", self.project["id"])}

    def _servers_answer(self, request: Any) -> tuple[int, Any, dict[str, str]] | None:
        """Nova's answer to a list of the servers of the project the request's token is scoped to,
        to a server's GET by id - of that project, or any for a token holding ``admin`` (Nova's
        default policy) - and to a server's volume attachments (none); None for any other
        request."""
        method, url = request.method, request.url
        compute = {**CATALOG, **self.catalog}["compute"].rstrip("/")
        parts = urllib.parse.urlsplit(url)
        path = f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
        if method != "GET" or not path.startswith(compute + "/servers"):
            return None
        own = self.project["id"]
        token = self.tokens.get(request.headers.get("X-Auth-Token", ""))
        scope = token or {"project": own, "roles": list(self.roles)}
        rest = path[len(compute + "/servers") :]
        if re.fullmatch(r"/[^/]+/os-volume_attachments", rest):
            return 200, {"volumeAttachments": []}, {}
        if re.fullmatch(r"/[^/]+", rest) and rest != "/detail":
            wanted = urllib.parse.unquote(rest[1:])
            server = next((s for s in self.servers if s["id"] == wanted), None)
            if server is None or (
                server.get("project_id", own) != scope["project"] and "admin" not in scope["roles"]
            ):
                return 404, {"itemNotFound": {"code": 404, "message": "Instance not found"}}, {}
            return 200, {"server": self._server_detail(server)}, {}
        if rest not in ("", "/detail"):
            return None
        found = [s for s in self.servers if s.get("project_id", own) == scope["project"]]
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
        if rest != "/detail":
            body = {"servers": [{"id": s["id"], "name": s["name"], "links": []} for s in found]}
            return 200, body, {}
        return 200, {"servers": [self._server_detail(s) for s in found]}, {}
