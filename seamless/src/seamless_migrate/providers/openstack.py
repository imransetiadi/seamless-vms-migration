"""OpenStack / RHOSO provider on openstacksdk (lazily imported; blocking calls in threads)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import math
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any, Literal, TypeVar

from ..ai.redaction import redact_cut
from ..config import Settings
from ..domain.enums import ProviderRole
from ..domain.models import Disk, GpuDevice, Nic, Provider, VMRef, utc_text, utcnow
from ..guest_os import identify
from ..planning.gpu import PoolRequest, gpu_pools, gpu_requests
from ..planning.platforms import destination_release
from ..planning.preflight import (
    AUTOSCALING_GROUP_TYPES,
    DestinationInventory,
    DnsRead,
    GpuCapacity,
    GpuPool,
    MemberRead,
    PoolMember,
    SourceInventory,
    StackPlace,
    StackRead,
)
from ..security.secrets import (
    SecretNotFound,
    openstack_cloud_entry,
    openstack_project_cloud_entry,
)
from ..storage import ceph_location, storage_family
from .base import (
    ProviderConflict,
    ProviderError,
    VMNotFound,
    microversion_text,
    missing_dependency,
)

log = logging.getLogger(__name__)

#: openstacksdk per-request timeout and the ceiling of one provider call from the control plane
PROVIDER_API_TIMEOUT_S = 60
PROVIDER_CALL_TIMEOUT_S = 300.0
#: Cinder statuses of a volume still attached to, or detaching from, a server: no delete yet
_VOLUME_SETTLING = frozenset({"in-use", "attaching", "detaching", "reserved"})
#: Placement's microversion of the GPU reads: allocation candidates with each provider's traits and
#: root, which every series of the window serves (SDD §21.1, §22.3)
PLACEMENT_MICROVERSION = "1.29"
#: the trait Nova puts on the root provider of a disabled compute service (SDD §22.3)
_COMPUTE_DISABLED = "COMPUTE_STATUS_DISABLED"
#: how long a provider does not ask again a Placement that gave a read no answer: an endpoint that
#: accepts connections and never answers costs one wait, not one per read (SDD §22.3)
PLACEMENT_SILENCE_S = 300.0
#: the clock of that silence, apart from time's so that a test moves it alone
_monotonic = time.monotonic
#: Placement's error codes (placement/errors.py, read 2026-10-20): what a failure's detail keeps of
#: its answer, one of these or none - never the message text, which names providers after their
#: hosts (SDD §22.12)
_PLACEMENT_CODES = frozenset(
    {
        "placement.undefined_code",
        "placement.inventory.inuse",
        "placement.concurrent_update",
        "placement.duplicate_name",
        "placement.resource_provider.inuse",
        "placement.resource_provider.cannot_delete_parent",
        "placement.resource_provider.not_found",
        "placement.query.duplicate_key",
        "placement.query.bad_value",
        "placement.query.missing_value",
    }
)
T = TypeVar("T")

_POWER = {
    "ACTIVE": "running",
    "SHUTOFF": "stopped",
    "PAUSED": "paused",
    "SUSPENDED": "paused",
    "SHELVED": "stopped",
    "SHELVED_OFFLOADED": "stopped",
    "ERROR": "error",
    # a Nova task in flight: a stop or snapshot would fail (SDD §4.2, §9.3)
    "BUILD": "transitioning",
    "REBUILD": "transitioning",
    "REBOOT": "transitioning",
    "HARD_REBOOT": "transitioning",
    "RESIZE": "transitioning",
    "VERIFY_RESIZE": "transitioning",
    "REVERT_RESIZE": "transitioning",
    "MIGRATING": "transitioning",
    "RESCUE": "transitioning",
    "PASSWORD": "transitioning",
}


def _import_openstack() -> Any:
    try:
        import openstack
    except ImportError:
        raise missing_dependency("openstack", "openstacksdk") from None
    return openstack


def connect(provider: Provider, settings: Settings, project_id: str | None = None) -> Any:
    """Open an openstacksdk connection for ``provider`` (credentials from ``clouds.yaml`` or its
    secret) - with its credential for the Keystone project ``project_id`` when one is given, the
    provider's reach kept (SDD §13.3, §27.4)."""
    openstack = _import_openstack()
    # a token's body names the application credential it signed in with (SDD §10, §13.4)
    logging.getLogger(_TOKEN_LOGGER).addFilter(_NO_APPLICATION_CREDENTIAL_TOKEN)  # kept once
    kwargs: dict[str, Any] = {"app_name": "seamless-migrate"}
    if (
        project_id is not None
        or provider.credentials_secret
        or (settings.clouds_yaml is not None and provider.cloud)
    ):
        try:
            entry = (
                openstack_cloud_entry(provider, settings)
                if project_id is None
                else openstack_project_cloud_entry(provider, project_id, settings)
            )
        except SecretNotFound as exc:
            raise ProviderError(f"{provider.id}: {exc}") from None
        kwargs.update(entry)
        kwargs["load_yaml_config"] = False
        kwargs["load_envvars"] = False
    elif provider.cloud:
        kwargs["cloud"] = provider.cloud
    else:
        raise ProviderError(f"{provider.id}: no 'cloud' configured for this provider")
    if provider.region:
        kwargs["region_name"] = provider.region
    kwargs["verify"] = provider.verify_tls
    if not provider.verify_tls:
        log.warning("%s: TLS certificate verification is disabled (verify_tls=false)", provider.id)
    if provider.ca_cert_path:
        kwargs["cacert"] = provider.ca_cert_path
    # a black-holed endpoint must not pin a worker thread forever (SDD §7)
    kwargs["api_timeout"] = PROVIDER_API_TIMEOUT_S
    return openstack.connect(**kwargs)


def _name_pattern(name: str) -> str:
    """Nova's ``name`` filter for the servers named ``name``: Nova reads it as a regular expression
    searched anywhere in the name (MySQL/MariaDB after escaping ``|`` itself, PostgreSQL as sent),
    so every ASCII character but a letter, a digit, a space, ``_`` and ``-`` stands as ``.`` - no
    alternation, group, class or escape on any backend - and the caller keeps the exact name
    (SDD §10, review PV-01)."""
    kept = (c if not c.isascii() or c.isalnum() or c in " _-" else "." for c in name)
    return "^" + "".join(kept) + "$"


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _token_value(access: Any, *names: str) -> Any:
    """The first of a token's properties that is set (SDD §10): keystoneauth's AccessInfo raises
    KeyError for a part its token lacks - a project's domain without its name -, read as unset."""
    for name in names:
        try:
            value = getattr(access, name, None)
        except KeyError:
            value = None
        if value is not None:
            return value
    return None


def _token_project(access: Any) -> dict[str, Any] | None:
    """The project a token is scoped to - its id, its name and its domain's name, else that
    domain's id (SDD §10, §27.7) -; None when the token names none."""
    project_id = _token_value(access, "project_id")
    if not project_id:
        return None
    return {
        "id": str(project_id),
        "name": _token_value(access, "project_name"),
        "domain": _token_value(access, "project_domain_name", "project_domain_id"),
    }


#: keystoneauth's logger of a sign-in's answer, whose whole token body it writes at DEBUG
_TOKEN_LOGGER = "keystoneauth.identity.v3.base"


class _NoApplicationCredentialToken(logging.Filter):
    """keystoneauth logs each sign-in's token body at DEBUG: a token an application credential
    signed in with names that credential's id, so the record is dropped (SDD §10 "The check of
    project credentials", §13.4). This keeps the line out of any handler, a test's capture
    included."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return '"application_credential"' not in record.getMessage()
        except Exception:  # a record whose args do not format: nothing to read, let it be
            return True


_NO_APPLICATION_CREDENTIAL_TOKEN = _NoApplicationCredentialToken()


def _close_quietly(conn: Any) -> None:
    """Close a connection, best effort."""
    if callable(getattr(conn, "close", None)):
        with contextlib.suppress(Exception):
            conn.close()


def _holders(volume: Any) -> list[str]:
    """The servers Cinder lists among the volume's attachments (SDD §10)."""
    attachments = _attr(volume, "attachments", default=None) or []
    return [str(a.get("server_id")) for a in attachments if a.get("server_id")]


def _canonical_ip(address: str) -> str:
    """An address as Neutron writes it; a string that is no address stays as it is."""
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        return address


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _try(fn: Callable[[], T], default: T) -> T:
    try:
        return fn()
    except Exception:  # capability probing: absence of permission is an answer
        log.debug("capability probe failed", exc_info=True)
        return default


#: the services Seamless calls, with the connection's proxy for each (SDD §10)
_SERVICES = (
    ("identity", "identity"),
    ("compute", "compute"),
    ("block-storage", "block_storage"),
    ("network", "network"),
    ("image", "image"),
)


def _catalog_url(adapter: Any) -> Any:
    """A service's endpoint as keystoneauth reads it from the catalog. openstacksdk's identity
    proxy overrides get_endpoint() - it fetches a Keystone endpoint record -, so keystoneauth's
    own method is asked of any of its adapters (SDD §10, review RV18-03)."""
    try:
        from keystoneauth1.adapter import Adapter
    except ImportError:  # a test double, without openstacksdk installed
        return adapter.get_endpoint()
    if isinstance(adapter, Adapter):
        return Adapter.get_endpoint(adapter)
    return adapter.get_endpoint()


def _plain_http(endpoint: str, conn: Any) -> list[str]:
    """The provider's own endpoint (``endpoint``), the auth URL its credential is posted to
    (``auth_url``) and the services whose endpoint uses plain HTTP (SDD §10, Security.md); an
    endpoint that cannot be read counts neither way."""
    urls: list[tuple[str, Callable[[], Any]]] = [
        ("endpoint", lambda: endpoint),
        ("auth_url", lambda: conn.session.auth.auth_url),
    ]
    urls += [(name, lambda p=proxy: _catalog_url(getattr(conn, p))) for name, proxy in _SERVICES]
    return [name for name, read in urls if _is_plain_http(_try(read, None))]


def _is_plain_http(url: Any) -> bool:
    return isinstance(url, str) and url.strip().lower().startswith("http://")


def _server_flavor(conn: Any, server: Any) -> Any:
    """A server's flavor (SDD §10, §22.3): Nova embeds it from compute 2.47 on; below it - or with a
    cloud entry that pins an older compute microversion - the server holds only the flavor's id,
    which openstacksdk's Flavor reads as 0 everywhere (review RV28-08), so it is looked up by that
    id - its extra specs only when asked, below 2.61 (review RV31-04): read apart, so a policy that
    forbids them keeps the flavor without them, as Nova 2.47+ does (RV32B-03). A flavor deleted
    since stays the bare reference, which names none."""
    flavor = _attr(server, "flavor", default={}) or {}
    flavor_id = _attr(flavor, "id")
    if not _attr(flavor, "original_name") and flavor_id:
        looked = _try(lambda: conn.compute.get_flavor(flavor_id), None)
        if looked is not None:
            flavor = _try(lambda: looked.fetch_extra_specs(conn.compute), None) or looked
    return flavor


def _flavors(conn: Any) -> list[dict[str, Any]]:
    """Every flavor the credential sees: [{name, vcpus, ram_mb, disk_gb, extra_specs}] (SDD §10)."""
    return [
        {
            "name": str(_attr(f, "name")),
            "vcpus": int(_attr(f, "vcpus", default=0)),
            "ram_mb": int(_attr(f, "ram", default=0)),
            "disk_gb": int(_attr(f, "disk", default=0)),
            "extra_specs": dict(_attr(f, "extra_specs", default={}) or {}),
        }
        for f in conn.compute.flavors(details=True)
    ]


def _gpu_flavors(
    flavors: Iterable[Mapping[str, Any]], profiles: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The flavors whose extra specs ask for a GPU by §22.2's reading, each with its GPU requests
    and the profile its provider declares for it: [{name, vcpus, ram_mb, disk_gb, gpus}]
    (SDD §22.3)."""
    found = []
    for flavor in flavors:
        specs = {str(k): str(v) for k, v in flavor["extra_specs"].items()}
        gpus = gpu_requests(specs, profiles.get(flavor["name"]))
        if gpus:
            size = {key: flavor[key] for key in ("name", "vcpus", "ram_mb", "disk_gb")}
            found.append({**size, "gpus": gpus})
    return found


class _PlacementAnswer(Exception):
    """Placement answered a read with an error: `detail` names its HTTP status and error code."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _placement_get(conn: Any, url: str, params: dict[str, str] | None = None) -> Any:
    """The body of Placement's 200 to a GET at microversion 1.29, else _PlacementAnswer with the
    status and the error code (SDD §22.3, §22.12). keystoneauth logs nothing of it: at a DEBUG level
    it writes each answer's body, provider uuids and error texts included (PR #62 review)."""
    response = conn.placement.get(
        url, params=params, microversion=PLACEMENT_MICROVERSION, raise_exc=False, log=False
    )
    status = int(response.status_code)
    if status == 200:
        return response.json()
    code = None
    with contextlib.suppress(Exception):  # an answer that is no Placement error has no code
        code = response.json()["errors"][0]["code"]
    if isinstance(code, str) and code in _PLACEMENT_CODES:
        raise _PlacementAnswer(status, f"HTTP {status} {code}")
    raise _PlacementAnswer(status, f"HTTP {status}")


def _not_in_catalog(exc: Exception) -> bool:
    """Whether ``exc`` says the catalog lacks the service - Placement, Heat, Octavia: keystoneauth
    finds no endpoint, or openstacksdk's proxy of a service the catalog lacks refuses every call
    (SDD §10, §22.3)."""
    try:
        from keystoneauth1.exceptions import EndpointNotFound
        from openstack.exceptions import ServiceDisabledException
    except ImportError:  # a test double, without openstacksdk installed
        return False
    return isinstance(exc, (EndpointNotFound, ServiceDisabledException))


def _gpu_read_failure(exc: Exception) -> tuple[str, str | None]:
    """Why a GPU read failed (SDD §22.3): `forbidden` when Placement refused the credential (403),
    `no_placement` when the catalog has none, else `error` - its detail the HTTP status and the
    Placement error code, or the failure's kind when nothing answered; never a message's text,
    which may name a provider (§22.12)."""
    if isinstance(exc, _PlacementAnswer):
        return ("forbidden" if exc.status == 403 else "error"), exc.detail
    if _not_in_catalog(exc):
        return "no_placement", None
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return "error", f"HTTP {status}"
    return "error", type(exc).__name__


def _answered(exc: Exception) -> bool:
    """Whether Placement, or the SDK reading it, had an HTTP answer before ``exc``."""
    return isinstance(exc, _PlacementAnswer) or isinstance(getattr(exc, "status_code", None), int)


class _Candidates:
    """The allocation-candidates answers of one capacity read (SDD §22.3), each request sent once:
    a numbered group's pool asks with its traits, and a class alone and its unnumbered groups'
    pools share one answer - Placement would check sent traits on the GPU's provider alone, where
    Nova's unnumbered group has them checked across it and its compute node (PR #62 review)."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.answers: dict[tuple[tuple[str, str], ...], Any] = {}
        #: Placement answered one of them: its endpoint has the token (SDD §10 tls)
        self.answered = False

    def __call__(self, pool: PoolRequest, units: int) -> Any:
        params = {"resources": f"{pool.resource_class}:{units}"}
        required = [*pool.traits, *(f"!{trait}" for trait in pool.forbidden_traits)]
        if pool.same_provider and required:
            params["required"] = ",".join(required)
        key = tuple(sorted(params.items()))
        if key not in self.answers:
            try:
                self.answers[key] = _placement_get(self.conn, "/allocation_candidates", params)
            except _PlacementAnswer as refused:
                self.answers[key] = refused
            self.answered = True
        answer = self.answers[key]
        if isinstance(answer, _PlacementAnswer):
            raise answer
        return answer


def _free_units(answer: Any, pool: PoolRequest) -> dict[str, int]:
    """The units free on each provider of an allocation-candidates answer that counts for the pool
    (SDD §22.3): it holds the class, its root provider lacks COMPUTE_STATUS_DISABLED - a disabled
    compute service, which Nova's own requests forbid -, and the pool's traits hold as Placement
    checks them for Nova's request: a numbered group's on the provider itself, the unnumbered
    group's across it and its root, the compute node Nova's unnumbered group takes VCPU and
    MEMORY_MB from (_check_traits_for_alloc_request: the required ones on either, no forbidden one
    on either). Its free is Placement's capacity less its used, never below 0. Keyed by uuid, kept
    by nothing."""
    summaries = answer.get("provider_summaries") or {}
    free: dict[str, int] = {}
    for uuid, summary in summaries.items():
        held = (summary.get("resources") or {}).get(pool.resource_class)
        if held is None:
            continue
        root = summaries.get(summary.get("root_provider_uuid") or uuid) or summary
        root_traits = set(root.get("traits") or [])
        traits = set(summary.get("traits") or [])
        if not pool.same_provider:
            traits |= root_traits
        if (
            _COMPUTE_DISABLED in root_traits
            or not traits.issuperset(pool.traits)
            or traits.intersection(pool.forbidden_traits)
        ):
            continue
        free[uuid] = max(0, int(held.get("capacity", 0)) - int(held.get("used", 0)))
    return free


def _read_pool(read: _Candidates, pool: PoolRequest) -> GpuPool:
    """One pool as Placement shows it (SDD §22.3): the units free on the providers that count, how
    many have one free, and the most on one. Nova fills a request group from one provider, whose
    inventory's max_unit may cap it below its free: the pool is read again as <class>:<n> for each
    group of n > 1 units, and where that read finds no provider that counts its largest free stays
    below n. A request Placement refuses as a bad one (400: a trait or a class it does not know)
    counts nothing, its detail naming the refusal - Nova's is refused alike -; any other failure is
    the whole read's."""
    shape = {
        "resource_class": pool.resource_class,
        "traits": list(pool.traits),
        "forbidden_traits": list(pool.forbidden_traits),
        "same_provider": pool.same_provider,
    }
    try:
        free = _free_units(read(pool, 1), pool)
        largest = max(free.values(), default=0)
        for units in pool.groups:
            answer = read(pool, units)
            named = {
                uuid
                for request in answer.get("allocation_requests") or []
                for uuid, allocation in (request.get("allocations") or {}).items()
                if pool.resource_class in (allocation.get("resources") or {})
            }
            if not named.intersection(_free_units(answer, pool)):
                largest = min(largest, units - 1)
    except _PlacementAnswer as refused:
        if refused.status != 400:
            raise
        return GpuPool(**shape, detail=refused.detail)
    return GpuPool(
        **shape,
        free=sum(free.values()),
        providers=sum(1 for left in free.values() if left > 0),
        largest_free=largest,
    )


#: the Heat resource types of a server (SDD §10)
_HEAT_SERVER_TYPES = frozenset({"OS::Nova::Server", "AWS::EC2::Instance"})
#: how deep Heat lists a stack's nested stacks' resources: its default max_nested_stack_depth, its
#: ceiling (SDD §10)
_HEAT_NESTED_DEPTH = 5


class _ServiceAnswer(Exception):
    """Heat or Octavia answered a read with an error: its HTTP status alone (SDD §10)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class _BudgetedGet:
    """The GETs of one optional read of a source's Heat or Octavia, or a destination's Designate
    (SDD §10, plan tasks N2, N2b): each is sent while the read has run for less than
    PROVIDER_API_TIMEOUT_S - past it, TimeoutError -, so a slow service costs about one request's
    timeout; and none is logged: keystoneauth would write each URL and body, which name the
    customer's stacks, load balancers and DNS zones (§13.4)."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.deadline = _monotonic() + PROVIDER_API_TIMEOUT_S

    def get(self, path: str, params: Mapping[str, str], *, may_be_gone: bool = False) -> Any:
        """The body of the service's 200; None for a 404 of what ``may_be_gone``; else
        _ServiceAnswer."""
        if _monotonic() >= self.deadline:
            raise TimeoutError
        response = self.adapter.get(path, params=dict(params), raise_exc=False, log=False)
        status = int(response.status_code)
        if status == 404 and may_be_gone:
            return None
        if status != 200:
            raise _ServiceAnswer(status)
        return response.json()

    def pages(self, path: str, key: str, params: Mapping[str, str] | None = None) -> list[Any]:
        """Every item of a list the service pages - by its ``<key>_links`` next link, Octavia's as
        openstacksdk follows it, or ``links``, Heat's, or Designate's mapping of ``links`` -, the
        next page asked at the same path with that link's query: the token never follows a link
        to another host."""
        base = dict(params or {})
        query, items = base, []
        while True:
            body = self.get(path, query)
            items += body.get(key) or []
            links = body.get(f"{key}_links") or body.get("links") or []
            if isinstance(links, Mapping):  # Designate's: {"self": ..., "next": ...}
                href = links.get("next")
            else:
                href = next((link.get("href") for link in links if link.get("rel") == "next"), None)
            if not href:
                return items
            asked = urllib.parse.parse_qs(urllib.parse.urlsplit(str(href)).query)
            following = {**base, **{name: values[-1] for name, values in asked.items()}}
            if following == query:  # the page just read: there is no other
                return items
            query = following


def _read_failure(exc: Exception) -> tuple[Literal["forbidden", "error"], str]:
    """Why a read of a source's Heat or Octavia failed, or a sign-in to Keystone (SDD §10, §27.7):
    `forbidden` when the service refused the credential (403), else `error` - its detail the HTTP
    status (openstacksdk's status_code, keystoneauth's http_status), or the failure's kind when
    nothing answered; never a message's text, which may name a stack, a load balancer or a
    credential."""
    status = (
        exc.status
        if isinstance(exc, _ServiceAnswer)
        else getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    )
    if isinstance(status, int) and not isinstance(status, bool):
        return ("forbidden" if status == 403 else "error"), f"HTTP {status}"
    return "error", type(exc).__name__


def _link_id(resource: Mapping[str, Any], rel: str) -> str | None:
    """The last path segment of a Heat resource's link ``rel``: the id of the stack it names."""
    for link in resource.get("links") or []:
        if isinstance(link, Mapping) and link.get("rel") == rel and link.get("href"):
            return urllib.parse.urlsplit(str(link["href"])).path.rstrip("/").rsplit("/", 1)[-1]
    return None


def _servers_of_stack(
    stack: str, stack_id: str, resources: list[Mapping[str, Any]]
) -> dict[str, StackPlace]:
    """The servers of a top-level stack's resource list, those of its nested stacks included
    (SDD §10): a nested stack's id is the physical_resource_id of the resource that holds it, so a
    server's chain of stack links runs up to the top-level stack - its resource there is the server
    or the one it is nested under, and the nearest autoscaling group on the way its group."""
    holders = {
        str(r["physical_resource_id"]): r for r in resources if r.get("physical_resource_id")
    }
    places: dict[str, StackPlace] = {}
    for resource in resources:
        server_id = resource.get("physical_resource_id")
        if resource.get("resource_type") not in _HEAT_SERVER_TYPES or not server_id:
            continue
        at, group = resource, None
        for _ in range(_HEAT_NESTED_DEPTH + 1):
            parent = _link_id(at, "stack")
            holder = holders.get(parent) if parent is not None and parent != stack_id else None
            if holder is None:
                break
            at = holder
            if group is None and at.get("resource_type") in AUTOSCALING_GROUP_TYPES:
                group = at
        places[str(server_id)] = StackPlace(
            stack=stack,
            resource=str(at.get("resource_name")),
            group=str(group.get("resource_name")) if group is not None else None,
            group_type=str(group.get("resource_type")) if group is not None else None,
        )
    return places


def _stack_places(get: _BudgetedGet) -> dict[str, StackPlace]:
    """Where each server of the credential's project's Heat stacks is (SDD §10): the stack list,
    then each top-level stack's resources with its nested stacks' (nested_depth) - one call per
    stack, whatever the plan's size. A stack gone between the two calls (404) holds nothing."""
    places: dict[str, StackPlace] = {}
    for stack in get.pages("/stacks", "stacks"):
        name, stack_id = str(stack.get("stack_name")), str(stack.get("id"))
        path = "/stacks/{}/{}/resources".format(
            urllib.parse.quote(name, safe=""), urllib.parse.quote(stack_id, safe="")
        )
        body = get.get(path, {"nested_depth": str(_HEAT_NESTED_DEPTH)}, may_be_gone=True)
        if body is None:
            continue
        resources = [r for r in body.get("resources") or [] if isinstance(r, Mapping)]
        for server_id, place in _servers_of_stack(name, stack_id, resources).items():
            places.setdefault(server_id, place)
    return places


def _pool_members(
    get: _BudgetedGet, project: str | None, subnets: Callable[[], dict[str, str | None] | None]
) -> list[PoolMember]:
    """The members of the credential's project's Octavia pools (SDD §10): its load balancers and
    pools, the members of each pool that lists some, and - once one does - the subnets once, to
    name each member's network: its subnet's, else its load balancer's VIP subnet's."""
    scope = {"project_id": project} if project else {}
    balancers = {
        str(lb.get("id")): lb for lb in get.pages("/lbaas/loadbalancers", "loadbalancers", scope)
    }
    pools = [pool for pool in get.pages("/lbaas/pools", "pools", scope) if pool.get("members")]
    if not pools:
        return []
    networks = subnets()
    members: list[PoolMember] = []
    for pool in pools:
        refs = [str(ref.get("id")) for ref in pool.get("loadbalancers") or [] if ref.get("id")]
        lb_id = refs[0] if refs else ""
        lb = balancers.get(lb_id, {})
        path = "/lbaas/pools/{}/members".format(urllib.parse.quote(str(pool.get("id")), safe=""))
        for member in get.pages(path, "members"):
            subnet = member.get("subnet_id") or lb.get("vip_subnet_id")
            members.append(
                PoolMember(
                    load_balancer=str(lb.get("name") or lb_id),
                    pool=str(pool.get("name") or pool.get("id")),
                    pool_id=str(pool.get("id")),
                    address=_canonical_ip(str(member.get("address"))),
                    port=int(member.get("protocol_port")),
                    network=networks.get(str(subnet)) if networks is not None else None,
                )
            )
    return members


class OpenStackProvider:
    """Source and destination provider for kinds ``openstack`` and ``rhoso``."""

    def __init__(
        self,
        provider: Provider,
        settings: Settings,
        *,
        connect_fn: Callable[[Provider, Settings], Any] | None = None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self._connect_fn = connect_fn or connect
        self._conn: Any = None
        self._lock = threading.Lock()
        #: (until, failure): a Placement read got no answer, so none is sent before then (§22.3)
        self._placement_silent: tuple[float, str | None] | None = None

    # -- plumbing ----------------------------------------------------------------------------
    def _connection(self) -> Any:
        with self._lock:
            if self._conn is None:
                self._conn = self._connect_fn(self.provider, self.settings)
            return self._conn

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), PROVIDER_CALL_TIMEOUT_S)
        except ProviderError:
            raise
        except TimeoutError:
            raise ProviderError(
                f"{self.provider.id}: call timed out after {PROVIDER_CALL_TIMEOUT_S:g} s"
            ) from None
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            raise ProviderError(f"{self.provider.id}: {redact_cut(message, 500)}") from exc

    def close(self) -> None:
        """Close the cached connection (a replaced provider must not keep its session)."""
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None and callable(getattr(conn, "close", None)):
            with contextlib.suppress(Exception):
                conn.close()

    # -- protocol ----------------------------------------------------------------------------
    async def check(self) -> dict[str, Any]:
        return await self._run(self._check)

    # the provider check's sign-in with a project credential (SDD §10, §27.7, plan task TC3)
    async def check_project(self, project_id: str) -> dict[str, Any]:
        return await self._run(self._check_project, project_id)

    async def list_vms(self) -> list[VMRef]:
        return await self._run(self._list_vms)

    async def get_vm(self, source_id: str) -> VMRef:
        return await self._run(self._get_vm, source_id)

    async def inventory(self) -> SourceInventory | DestinationInventory:
        return await self._run(self._inventory)

    async def get_server(self, server_id: str) -> dict[str, Any]:
        return await self._run(self._get_server, server_id)

    async def gpu_capacity(self, requests: list[GpuDevice]) -> GpuCapacity:
        return await self._run(self._gpu_capacity, requests)

    async def gpu_flavors(self) -> list[dict[str, Any]]:
        return await self._run(self._list_gpu_flavors)

    async def server_allocations(self, server_id: str) -> dict[str, int] | None:
        return await self._run(self._server_allocations, server_id)

    async def console_log(self, server_id: str, lines: int | None = 200) -> str | None:
        return await self._run(self._console_log, server_id, lines)

    async def delete_server(self, server_id: str) -> None:
        await self._run(self._delete_server, server_id)

    async def find_server(self, name: str) -> str | None:
        return await self._run(self._find_server, name)

    async def ports(
        self,
        *,
        network_id: str | None = None,
        mac_address: str | None = None,
        ip_address: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._run(self._ports, network_id, mac_address, ip_address)

    async def delete_port(self, port_id: str) -> None:
        await self._run(self._delete_port, port_id)

    async def volumes(self) -> list[dict[str, Any]]:
        return await self._run(self._volumes)

    async def delete_volume(self, volume_id: str) -> None:
        await self._run(self._delete_volume, volume_id)

    async def detach_volume(self, server_id: str, volume_id: str) -> None:
        await self._run(self._detach_volume, server_id, volume_id)

    async def server_of_address(self, address: str) -> str | None:
        return await self._run(self._server_of_address, address)

    async def server_volumes(self, server_id: str) -> list[str]:
        return await self._run(self._server_volumes, server_id)

    async def set_volume_metadata(self, volume_id: str, metadata: dict[str, str]) -> None:
        await self._run(self._set_volume_metadata, volume_id, metadata)

    # the keep-address move of floating IPs (SDD §8.3, §10, plan task N6a)
    async def reserve_floating_ip(self, address: str) -> None:
        await self._run(self._reserve_floating_ip, address)

    async def server_port(self, server_id: str, address: str) -> str | None:
        return await self._run(self._server_port, server_id, address)

    async def floating_ip_port(self, address: str) -> str | None:
        return await self._run(self._floating_ip_port, address)

    async def disassociate_floating_ip(self, address: str) -> str | None:
        return await self._run(self._disassociate_floating_ip, address)

    async def associate_floating_ip(self, address: str, port_id: str) -> None:
        await self._run(self._associate_floating_ip, address, port_id)

    async def release_floating_ip(self, address: str) -> None:
        await self._run(self._release_floating_ip, address)

    # the conversion keypair a pre-staging resolves its key with (SDD §7.2, §10, issue #112)
    async def keypair_public_key(self, name: str) -> str | None:
        return await self._run(self._keypair_public_key, name)

    # the Octavia member helper's calls (SDD §8.5, §10, plan task N9)
    async def lb_pool(self, pool_id: str) -> dict[str, Any] | None:
        return await self._run(self._lb_pool, pool_id)

    async def pool_members(self, pool_id: str) -> list[dict[str, Any]]:
        return await self._run(self._list_pool_members, pool_id)

    async def set_pool_member(self, pool_id: str, member_id: str, admin_state_up: bool) -> None:
        await self._run(self._set_pool_member, pool_id, member_id, admin_state_up)

    async def add_pool_member(self, pool_id: str, address: str, protocol_port: int) -> str:
        return await self._run(self._add_pool_member, pool_id, address, protocol_port)

    async def delete_pool_member(self, pool_id: str, member_id: str) -> None:
        await self._run(self._delete_pool_member, pool_id, member_id)

    # the DNS swap's Designate calls (SDD §8.4, §10, plan task N7)
    async def dns_recordset(self, zone: str, name: str, rdtype: str) -> dict[str, Any] | None:
        return await self._run(self._dns_recordset, zone, name, rdtype)

    async def update_dns_recordset(
        self, zone_id: str, recordset_id: str, records: list[str]
    ) -> None:
        await self._run(self._update_dns_recordset, zone_id, recordset_id, records)

    # -- implementation (runs in worker threads) ----------------------------------------------
    def _check(self) -> dict[str, Any]:
        conn = self._connection()
        admin = _try(lambda: list(conn.compute.hypervisors()), None) is not None
        # keystoneauth hands a parsed tuple: reported as "2.96" (review RV18-01)
        microversion = _try(
            lambda: microversion_text(conn.compute.get_endpoint_data().max_microversion), ""
        )
        block_storage_microversion = _try(
            lambda: microversion_text(conn.block_storage.get_endpoint_data().max_microversion), ""
        )
        agents = _try(lambda: list(conn.network.agents()), [])
        ovn = any(
            "ovn" in str(_attr(a, "agent_type", default="")).lower()
            or "ovn" in str(_attr(a, "binary", default="")).lower()
            for a in agents
        )
        storage: list[dict[str, Any]] = []
        if admin:
            for pool in _try(lambda: list(conn.block_storage.backend_pools()), []):
                caps = _attr(pool, "capabilities", default={}) or {}
                entry: dict[str, Any] = {
                    "pool": str(_attr(pool, "name")),
                    "vendor": caps.get("vendor_name"),
                    "protocol": caps.get("storage_protocol"),
                    "family": storage_family(caps),
                }
                if entry["family"] == "rbd":
                    # the cluster and the pool the extents are read from (SDD §10, §28.4)
                    entry["ceph_fsid"], entry["ceph_pool"] = ceph_location(
                        caps.get("location_info")
                    )
                storage.append(entry)
            storage.sort(key=lambda b: b["pool"])
        # the role names of its token: what Neutron's port policy names (SDD §10, §6.6); left out
        # when the token cannot tell, so that half of NET_PORT_NOT_PERMITTED is not judged
        access = _try(lambda: conn.session.auth.get_access(conn.session), None)
        roles = (
            _try(lambda: sorted(set(access.role_names or [])), None) if access is not None else None
        )
        # the project its token is scoped to, left out when the token names none (SDD §10, §27.7)
        project = _token_project(access)
        plain_http = _plain_http(self.provider.endpoint, conn)
        report = {
            "admin": admin,
            **({"roles": roles} if roles is not None else {}),
            **({"project": project} if project is not None else {}),
            "compute_microversion": microversion,
            "block_storage_microversion": block_storage_microversion,
            "ovn": ovn,
            "volume_backends": [b["pool"] for b in storage],
            "storage_backends": storage,
            "tls": not plain_http,
            "plain_http": plain_http,
        }
        if self.provider.role == ProviderRole.destination:
            # the series it runs, its support tier and its policy profile (SDD §10.1, §21)
            report.update(
                destination_release(
                    self.provider.kind,
                    self.provider.release,
                    self.provider.policy_profile,
                    microversion,
                    block_storage_microversion,
                )
            )
            # the Placement pools of its GPU flavors' vGPU requests, read now (SDD §22.3); the
            # token goes to Placement too once it answered the read (§10 tls)
            capacity, answered = self._check_gpu_capacity(conn)
            report["gpu_capacity"] = capacity.to_dict()
            if answered and _is_plain_http(_try(lambda: _catalog_url(conn.placement), None)):
                report["plain_http"] = [*plain_http, "placement"]
                report["tls"] = False
        return report

    def _check_gpu_capacity(self, conn: Any) -> tuple[GpuCapacity, bool]:
        try:
            flavors = _flavors(conn)
        except Exception as exc:  # a probe, as the check's others: it fails nothing (SDD §22.3)
            reason, detail = _gpu_read_failure(exc)
            failed = GpuCapacity(status="unknown", reason=reason, detail=detail, read_at=utcnow())
            return failed, False
        gpu_flavors = _gpu_flavors(flavors, self.provider.gpu_profiles)
        return self._read_gpu_capacity(conn, (gpu for f in gpu_flavors for gpu in f["gpus"]))

    def _read_gpu_capacity(
        self, conn: Any, requests: Iterable[GpuDevice]
    ) -> tuple[GpuCapacity, bool]:
        """The Placement pools of the vGPU requests (SDD §22.3), counts only: no provider's name
        or uuid leaves the read (§22.12); and whether Placement answered it. Without a vGPU request
        it sends Placement nothing. A refused credential, a catalog without Placement and any other
        failure read unknown - never a guess - and fail neither the inventory nor the check; while
        a read that got no answer is remembered, none is sent."""
        pools = gpu_pools(requests)
        read_at = utcnow()
        if not pools:
            return GpuCapacity(status="ok", read_at=read_at), False
        silent = self._placement_silence()
        if silent is not None:
            return (
                GpuCapacity(status="unknown", reason="error", detail=silent[1], read_at=read_at),
                False,
            )
        read = _Candidates(conn)
        try:
            counted = [_read_pool(read, pool) for pool in pools]
        except Exception as exc:
            reason, detail = self._placement_failure(exc)
            log.info("GPU capacity not read: %s %s", reason, detail or "")
            failed = GpuCapacity(status="unknown", reason=reason, detail=detail, read_at=read_at)
            return failed, read.answered
        return GpuCapacity(status="ok", read_at=read_at, pools=counted), True

    def _placement_silence(self) -> tuple[float, str | None] | None:
        """The read that got no answer from Placement, while the provider does not ask again."""
        silent = self._placement_silent
        return silent if silent is not None and _monotonic() < silent[0] else None

    def _placement_failure(self, exc: Exception) -> tuple[str, str | None]:
        """Why a Placement read failed (``_gpu_read_failure``), remembered when nothing answered
        it - a connection or a version discovery that failed or timed out - for
        PLACEMENT_SILENCE_S (SDD §22.3, PR #62 review)."""
        reason, detail = _gpu_read_failure(exc)
        if reason == "error" and not _answered(exc):
            self._placement_silent = (_monotonic() + PLACEMENT_SILENCE_S, detail)
        return reason, detail

    def _check_project(self, project_id: str) -> dict[str, Any]:
        """One sign-in with the provider's credential for the Keystone project ``project_id``, on
        a connection of its own that is closed after it (SDD §10 "The check of project
        credentials", §27.7): what its token says - its project, that project's domain, its user
        and its role names -, and, only when the token's project is ``project_id``, the
        application credential's restricted flag and expiry from its record. A failure names an
        HTTP status or its kind, never a message Keystone wrote."""
        conn = self._connect_fn(self.provider, self.settings, project_id=project_id)
        try:
            try:
                access = conn.session.auth.get_access(conn.session)
            except Exception as exc:
                raise ProviderError(
                    f"{self.provider.id}: the sign-in failed: {_read_failure(exc)[1]}"
                ) from None
            project = _token_project(access) or {}
            read: dict[str, Any] = {
                "project_id": project.get("id"),
                "project_name": project.get("name"),
                "domain": project.get("domain"),
                "user_id": _token_value(access, "user_id"),
                "roles": sorted(set(_token_value(access, "role_names") or [])),
            }
            if read["project_id"] != project_id:
                return read  # another project's credential: never used further (SDD §27.7)
            read.update(self._credential_record(conn, access))
            return read
        finally:
            _close_quietly(conn)

    def _credential_record(self, conn: Any, access: Any) -> dict[str, Any]:
        """The application credential's restricted flag and expiry, read from its record by its
        own user: a raw GET, not logged - keystoneauth would write its URL, which names the
        credential (SDD §10, §27.7). A record that cannot be read, or whose expiry does not read as
        a time, is a provider error: an expiry not read must never stand for none."""
        user_id = _token_value(access, "user_id")
        credential_id = _token_value(access, "application_credential_id")
        if not user_id or not credential_id:
            raise ProviderError(f"{self.provider.id}: its token names no application credential")
        record_of = f"{self.provider.id}: the application credential's record"
        path = "/users/{}/application_credentials/{}".format(
            urllib.parse.quote(str(user_id), safe=""),
            urllib.parse.quote(str(credential_id), safe=""),
        )
        try:
            response = conn.identity.get(path, raise_exc=False, log=False)
            status = int(response.status_code)
            record = response.json().get("application_credential") if status == 200 else None
        except Exception as exc:
            raise ProviderError(f"{record_of} was not read: {_read_failure(exc)[1]}") from None
        if status != 200:
            raise ProviderError(f"{record_of} was not read: HTTP {status}")
        if not isinstance(record, Mapping):
            raise ProviderError(f"{record_of} was not read: Keystone answered none")
        expires_at = record.get("expires_at")
        try:
            expiry = (
                None
                if expires_at is None
                else utc_text(datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")))
            )
        except ValueError:
            raise ProviderError(f"{record_of} holds an expiry that is no time") from None
        return {"restricted": not _truthy(record.get("unrestricted")), "expires_at": expiry}

    def _list_vms(self) -> list[VMRef]:
        conn = self._connection()
        ctx = _MapContext(conn, self.provider.gpu_profiles)
        servers = list(conn.compute.servers(details=True))
        if len(servers) > 1:
            ctx.prefetch()
        return [ctx.server_to_vmref(s) for s in servers]

    def _get_vm(self, source_id: str) -> VMRef:
        conn = self._connection()
        import openstack.exceptions

        try:
            server = conn.compute.get_server(source_id)
        except openstack.exceptions.NotFoundException:
            # Nova answered 404: it has no such server (SDD §10), not an unreachable cloud
            raise VMNotFound(f"{self.provider.id}: VM {source_id!r} not found") from None
        return _MapContext(conn, self.provider.gpu_profiles).server_to_vmref(server)

    def _project_name(self, conn: Any) -> str:
        pid = _attr(conn, "current_project_id", default="")
        return _MapContext(conn).project_name(pid) or str(pid)

    def _inventory(self) -> SourceInventory | DestinationInventory:
        conn = self._connection()
        visible = list(conn.network.networks())
        networks = {str(_attr(n, "name")): _attr(n, "mtu") for n in visible}
        project = self._project_name(conn)
        # the credential's own keypairs: what it can export, or owns as the creator (SDD §10)
        keypairs = sorted(str(_attr(k, "name")) for k in conn.compute.keypairs())
        if self.provider.role == ProviderRole.source:
            names = {_attr(n, "id"): str(_attr(n, "name")) for n in visible}
            return SourceInventory(
                networks=networks,
                projects=[project] if project else [],
                keypairs=keypairs,
                # where its servers are in the project's Heat stacks and Octavia pools: optional
                # reads, which fail nothing (SDD §9.3, §10)
                stacks=self._read_stacks(conn),
                lb_members=self._read_members(conn, names),
            )
        flavors = _flavors(conn)
        # the flavors that ask for a GPU, with their declared profiles, and the Placement pools of
        # their vGPU requests (SDD §22.3)
        gpu_flavors = _gpu_flavors(flavors, self.provider.gpu_profiles)
        gpu_capacity, _ = self._read_gpu_capacity(conn, (g for f in gpu_flavors for g in f["gpus"]))
        volume_types = [str(_attr(t, "name")) for t in conn.block_storage.types()]
        quotas = _try(lambda: {project: self._free_quota(conn)}, {})
        # every network it sees with its owner: what a NIC's network name resolves to; shared and
        # external: whether the credential may put a port on another project's (SDD §10, §6.6)
        own_project = _attr(conn, "current_project_id", default=None)
        network_details = [
            {
                "id": str(_attr(n, "id")),
                "name": str(_attr(n, "name")),
                "mtu": _attr(n, "mtu"),
                "project_id": _attr(n, "project_id", default=None),
                "own": _attr(n, "project_id", default=None) == own_project,
                "shared": bool(_attr(n, "is_shared", default=False)),
                "external": bool(_attr(n, "is_router_external", default=False)),
            }
            for n in visible
        ]
        # where a NIC's addresses must lie, by network name and id (SDD §10, §9.3)
        names = {_attr(n, "id"): str(_attr(n, "name")) for n in visible}
        subnets = [
            {
                "id": str(_attr(s, "id")),
                "name": str(_attr(s, "name")),
                "network": names[_attr(s, "network_id")],
                "network_id": str(_attr(s, "network_id")),
                "cidr": str(_attr(s, "cidr")),
                "ipv6_address_mode": _attr(s, "ipv6_address_mode", default=None),
                "ipv6_ra_mode": _attr(s, "ipv6_ra_mode", default=None),
            }
            for s in conn.network.subnets()
            if _attr(s, "network_id") in names
        ]
        # the token's role names, read now: a check report new credentials reset names none
        # (SDD §10, §7.2, review RV19-01)
        roles = _try(
            lambda: sorted(set(conn.session.auth.get_access(conn.session).role_names or [])), None
        )
        # what os-migrate's `auto` attaches: the project's floating IPs no port holds (SDD §10,
        # §9.3); [] when they cannot be read
        # what os-migrate's `auto` attaches - the project's floating IPs no port holds -, and those
        # a port holds, which the keep-address move cannot take (SDD §10, §9.3); [] when unread
        listed = _try(lambda: list(conn.network.ips(project_id=own_project)), [])
        floating_ips = [
            _canonical_ip(str(_attr(ip, "floating_ip_address")))
            for ip in listed
            if not _attr(ip, "port_id")
        ]
        attached_floating_ips = [
            _canonical_ip(str(_attr(ip, "floating_ip_address")))
            for ip in listed
            if _attr(ip, "port_id")
        ]
        return DestinationInventory(
            networks=networks,
            flavors=flavors,
            volume_types=volume_types,
            quotas=quotas,
            projects=[project] if project else [],
            keypairs=keypairs,
            subnets=subnets,
            network_details=network_details,
            roles=roles,
            gpu_flavors=gpu_flavors,
            gpu_capacity=gpu_capacity,
            floating_ips=floating_ips,
            dns=self._read_dns(conn),
            attached_floating_ips=attached_floating_ips,
            floating_ip_quota=_try(lambda: _free_floating_ips(conn, own_project), None),
        )

    def _read_dns(self, conn: Any) -> DnsRead:
        """Neutron's DNS integration and Designate's zones (SDD §10, plan task N2b), each read once:
        whether Neutron lists the dns-integration extension - None when its extensions cannot be
        read -, and the names of the zones the credential sees: [] for a catalog without Designate;
        a Designate that refuses or fails is not judged (None), its kind logged, never a name."""
        extension = _try(
            lambda: any(_attr(e, "alias") == "dns-integration" for e in conn.network.extensions()),
            None,
        )
        zones: list[str] | None
        try:
            listed = _BudgetedGet(conn.dns).pages("/zones", "zones")
            zones = sorted(
                {str(z["name"]) for z in listed if isinstance(z, Mapping) and z.get("name")}
            )
        except Exception as exc:
            if _not_in_catalog(exc):
                zones = []
            else:
                reason, detail = _read_failure(exc)
                log.info("%s: Designate zones not read: %s %s", self.provider.id, reason, detail)
                zones = None
        return DnsRead(extension=extension, zones=zones)

    def _read_stacks(self, conn: Any) -> StackRead:
        """The servers of the credential's project's Heat stacks (SDD §10, plan task N2): `ok` with
        nothing when the catalog has no Heat; refused (403) or failed, `unknown` with nothing of it
        kept - a partial read would guess -, its kind logged, never a name."""
        try:
            return StackRead(servers=_stack_places(_BudgetedGet(conn.orchestration)))
        except Exception as exc:
            if _not_in_catalog(exc):
                return StackRead()
            reason, detail = _read_failure(exc)
            log.info("%s: Heat stacks not read: %s %s", self.provider.id, reason, detail)
            return StackRead(status="unknown", reason=reason, detail=detail)

    def _read_members(self, conn: Any, networks: Mapping[Any, str]) -> MemberRead:
        """The members of the credential's project's Octavia pools (SDD §10, plan task N2), their
        networks by ``networks`` (network id -> name) - none when the subnets cannot be read;
        absent, refused or failed as ``_read_stacks``."""

        def subnets() -> dict[str, str | None] | None:
            return _try(
                lambda: {
                    str(_attr(s, "id")): networks.get(_attr(s, "network_id"))
                    for s in conn.network.subnets()
                },
                None,
            )

        project = _attr(conn, "current_project_id", default=None)
        try:
            get = _BudgetedGet(conn.load_balancer)
            return MemberRead(members=_pool_members(get, project, subnets))
        except Exception as exc:
            if _not_in_catalog(exc):
                return MemberRead()
            reason, detail = _read_failure(exc)
            log.info("%s: Octavia pools not read: %s %s", self.provider.id, reason, detail)
            return MemberRead(status="unknown", reason=reason, detail=detail)

    @staticmethod
    def _free_quota(conn: Any) -> dict[str, int | None]:
        pid = conn.current_project_id
        compute = conn.compute.get_quota_set(pid, usage=True)
        volume = conn.block_storage.get_quota_set(pid, usage=True)

        def free(qs: Any, key: str, usage_key: str | None = None) -> int | None:
            limit = _attr(qs, key)
            if limit is None:
                return None
            limit = int(limit)
            if limit < 0:
                return -1
            usage = _attr(qs, "usage", default={}) or {}
            used = int(usage.get(usage_key or key, 0) or 0)
            return max(0, limit - used)

        return {
            "cores": free(compute, "cores"),
            "ram_mb": free(compute, "ram"),
            "instances": free(compute, "instances"),
            "volumes": free(volume, "volumes"),
            "gigabytes": free(volume, "gigabytes"),
        }

    def _get_server(self, server_id: str) -> dict[str, Any]:
        conn = self._connection()
        server = conn.compute.get_server(server_id)
        ports = []
        for port in conn.network.ports(device_id=server_id):
            port_id = _attr(port, "id")
            floating = [
                str(_attr(ip, "floating_ip_address"))
                for ip in _try(lambda pid=port_id: list(conn.network.ips(port_id=pid)), [])
            ]
            ports.append(
                {
                    "status": _attr(port, "status"),
                    "fixed_ips": [
                        ip.get("ip_address") for ip in _attr(port, "fixed_ips", default=[])
                    ],
                    "floating_ips": floating,
                }
            )
        # compute 2.47's embedded flavor, else the one looked up by its id: its extra specs stay
        # empty where Nova's policy hides them (SDD §10, §22.3, G8 review)
        flavor = _server_flavor(conn, server)
        extra_specs = _attr(flavor, "extra_specs", default={}) or {}
        return {
            "id": _attr(server, "id"),
            "name": _attr(server, "name"),
            "status": _attr(server, "status"),
            "ports": ports,
            "flavor": {
                "name": _attr(flavor, "original_name", "name"),
                "extra_specs": {str(k): str(v) for k, v in extra_specs.items()},
            },
        }

    def _gpu_capacity(self, requests: list[GpuDevice]) -> GpuCapacity:
        return self._read_gpu_capacity(self._connection(), requests)[0]

    def _list_gpu_flavors(self) -> list[dict[str, Any]]:
        """The GPU flavors with their declared profiles, read with one flavor list, as the
        inventory reads them (SDD §10, §22.3): the cutover gate's read of a GPU flavor's requests
        (§22.7)."""
        return _gpu_flavors(_flavors(self._connection()), self.provider.gpu_profiles)

    def _server_allocations(self, server_id: str) -> dict[str, int] | None:
        conn = self._connection()
        silent = self._placement_silence()
        if silent is not None:
            log.info("allocations of server %s not read: error %s", server_id, silent[1] or "")
            return None
        try:
            answer = _placement_get(conn, f"/allocations/{urllib.parse.quote(server_id, safe='')}")
            units: dict[str, int] = {}
            for held in (answer.get("allocations") or {}).values():
                for resource_class, amount in (held.get("resources") or {}).items():
                    units[str(resource_class)] = units.get(str(resource_class), 0) + int(amount)
        except Exception as exc:  # unread, never a guess: the verification skips its check (§22.8)
            reason, detail = self._placement_failure(exc)
            log.info("allocations of server %s not read: %s %s", server_id, reason, detail or "")
            return None
        return units or None

    def _console_log(self, server_id: str, lines: int | None) -> str | None:
        conn = self._connection()
        if lines is None:
            return self._whole_console(conn, server_id)
        try:
            result = conn.compute.get_server_console_output(server_id, length=lines)
        except Exception:
            log.info("console log unavailable for %s", server_id, exc_info=True)
            return None
        output = _attr(result, "output") if result is not None else None
        return str(output) if output is not None else None

    @staticmethod
    def _whole_console(conn: Any, server_id: str) -> str | None:
        """The server's whole console, which a host key pin reads (SDD §10, §26.3): a raw
        os-getConsoleOutput without ``length``, sent unlogged - keystoneauth writes an answer's body
        at DEBUG, and the console is the guest's text (§13.4); None when Nova refuses or fails to
        answer, its failure logged by its status or kind alone."""
        path = f"/servers/{urllib.parse.quote(server_id, safe='')}/action"
        try:
            response = conn.compute.post(
                path, json={"os-getConsoleOutput": {}}, raise_exc=False, log=False
            )
            status = int(response.status_code)
            output = response.json().get("output") if status == 200 else None
        except Exception as exc:
            log.info("console of server %s not read: %s", server_id, type(exc).__name__)
            return None
        if status != 200:
            log.info("console of server %s not read: HTTP %s", server_id, status)
        return str(output) if output is not None else None

    def _delete_server(self, server_id: str) -> None:
        # the wait ends within the call's bound: past it the call reports a timeout while the
        # deletion goes on (SDD §8.1)
        deadline = time.monotonic() + PROVIDER_CALL_TIMEOUT_S - 5
        conn = self._connection()
        server = conn.compute.find_server(server_id, ignore_missing=True)
        if server is None:
            return
        conn.compute.delete_server(server, ignore_missing=True)
        wait = getattr(conn.compute, "wait_for_delete", None)
        if wait is not None:
            wait(server, wait=max(1, int(deadline - time.monotonic())))

    def _find_server(self, name: str) -> str | None:
        conn = self._connection()
        found = [
            server
            for server in conn.compute.servers(details=False, name=_name_pattern(name))
            if _attr(server, "name") == name
        ]
        if len(found) > 1:
            raise ProviderError(f"{self.provider.id}: more than one server is named {name!r}")
        return str(_attr(found[0], "id")) if found else None

    def _ports(
        self, network_id: str | None, mac_address: str | None, ip_address: str | None
    ) -> list[dict[str, Any]]:
        """The ports matching every filter given (SDD §10); Neutron keeps MACs in lower case and
        addresses in their canonical form, and each filter is checked again on what it returns."""
        mac = mac_address.lower() if mac_address is not None else None
        address = _canonical_ip(ip_address) if ip_address is not None else None
        query: dict[str, Any] = {}
        if network_id is not None:
            query["network_id"] = network_id
        if mac is not None:
            query["mac_address"] = mac
        if address is not None:
            query["fixed_ips"] = [f"ip_address={address}"]
        out: list[dict[str, Any]] = []
        for port in self._connection().network.ports(**query):
            fixed = [
                str(ip.get("ip_address"))
                for ip in _attr(port, "fixed_ips", default=None) or []
                if ip.get("ip_address")
            ]
            port_mac = str(_attr(port, "mac_address", default="") or "").lower()
            port_network = str(_attr(port, "network_id", default="") or "")
            if network_id is not None and port_network != network_id:
                continue
            if mac is not None and port_mac != mac:
                continue
            if address is not None and address not in (_canonical_ip(a) for a in fixed):
                continue
            out.append(
                {
                    "id": str(_attr(port, "id")),
                    "name": str(_attr(port, "name", default="") or ""),
                    "network_id": port_network,
                    "device_id": str(_attr(port, "device_id", default="") or ""),
                    "mac_address": port_mac,
                    "fixed_ips": fixed,
                }
            )
        return out

    def _delete_port(self, port_id: str) -> None:
        self._connection().network.delete_port(port_id, ignore_missing=True)

    def _keypair_public_key(self, name: str) -> str | None:
        """The credential's user's keypair of that name, by Nova's GET of it, then its list
        (openstacksdk's find): its public key, None when the user holds none (SDD §10)."""
        conn = self._connection()
        import openstack.exceptions

        try:
            # ignore_missing=False: openstacksdk deprecates True for removal (6.0)
            keypair = conn.compute.find_keypair(name, ignore_missing=False)
        except openstack.exceptions.NotFoundException:
            return None  # Nova answered that the user holds none - never a failure taken for it
        # a keypair Nova holds is never taken for none, whatever its key reads
        return str(_attr(keypair, "public_key", default="") or "")

    def _volumes(self) -> list[dict[str, Any]]:
        """The project's volumes, with their metadata and the servers holding them (SDD §10)."""
        out: list[dict[str, Any]] = []
        for volume in self._connection().block_storage.volumes(details=True):
            metadata = _attr(volume, "metadata", default=None) or {}
            out.append(
                {
                    "id": str(_attr(volume, "id")),
                    "name": str(_attr(volume, "name", default="") or ""),
                    "status": str(_attr(volume, "status", default="") or ""),
                    "metadata": {str(k): str(v) for k, v in metadata.items()},
                    "attachments": _holders(volume),
                }
            )
        return out

    def _delete_volume(self, volume_id: str) -> None:
        # Cinder refuses to delete a volume still detaching from the server just deleted: wait for
        # it to settle, then for the deletion - within the call's bound (SDD §8.1, §10)
        deadline = time.monotonic() + PROVIDER_CALL_TIMEOUT_S - 5
        storage = self._connection().block_storage
        while True:
            volume = storage.find_volume(volume_id, ignore_missing=True)
            if volume is None:
                return
            status = str(_attr(volume, "status", default="") or "").lower()
            if status == "error_deleting":
                raise ProviderError(
                    f"{self.provider.id}: volume {volume_id} is error_deleting: Cinder failed to "
                    "delete it - reset its state, then try again"
                )
            if status == "deleting":
                break  # Cinder deletes it already: waited for, never deleted twice (RV29-04)
            if status not in _VOLUME_SETTLING:
                try:
                    storage.delete_volume(volume, ignore_missing=True)
                except Exception:
                    # Cinder refuses a volume it began deleting meanwhile: that deletion counts
                    again = storage.find_volume(volume_id, ignore_missing=True)
                    if again is not None and _attr(again, "status") != "deleting":
                        raise
                break
            if time.monotonic() > deadline:
                raise ProviderError(f"{self.provider.id}: volume {volume_id} is still {status}")
            time.sleep(2)
        storage.wait_for_delete(volume, wait=max(1, int(deadline - time.monotonic())))

    def _detach_volume(self, server_id: str, volume_id: str) -> None:
        # the wait ends within the call's bound (SDD §8.1, §10)
        deadline = time.monotonic() + PROVIDER_CALL_TIMEOUT_S - 5
        conn = self._connection()
        if server_id not in _holders(conn.block_storage.get_volume(volume_id)):
            return  # not attached to that server: no error, nothing to wait for (review PV-03)
        conn.compute.delete_volume_attachment(server_id, volume_id, ignore_missing=True)
        # until Cinder no longer lists the server: available, or in-use with another holder - a
        # multi-attach volume another server holds never reads available (SDD §10, review PV-03)
        while True:
            volume = conn.block_storage.get_volume(volume_id)
            if str(_attr(volume, "status", default="") or "").lower() == "error":
                raise ProviderError(
                    f"{self.provider.id}: volume {volume_id} is in error: Cinder failed to detach "
                    f"it from {server_id}"
                )
            if server_id not in _holders(volume):
                return
            if time.monotonic() > deadline:
                raise ProviderError(
                    f"{self.provider.id}: volume {volume_id} is still attached to {server_id}"
                )
            time.sleep(2)

    def _project_fip(self, conn: Any, address: str) -> Any:
        """The credential's project's floating IP of the address, None when it holds none."""
        project = _attr(conn, "current_project_id", default=None)
        canonical = _canonical_ip(address)
        for fip in conn.network.ips(floating_ip_address=canonical, project_id=project):
            if _canonical_ip(str(_attr(fip, "floating_ip_address"))) == canonical:
                return fip
        return None

    def _reserve_floating_ip(self, address: str) -> None:
        """SDD §8.3: keep the project's detached floating IP of the address, else create one on
        the external network one of whose subnets holds it."""
        conn = self._connection()
        held = self._project_fip(conn, address)
        if held is not None:
            if _attr(held, "port_id"):
                raise ProviderError(
                    f"{self.provider.id}: floating IP {address} is held by a port of the project"
                )
            return
        ip = ipaddress.ip_address(_canonical_ip(address))
        for network in conn.network.networks(is_router_external=True):
            for subnet in conn.network.subnets(network_id=_attr(network, "id")):
                with contextlib.suppress(ValueError):
                    if ip in ipaddress.ip_network(str(_attr(subnet, "cidr")), strict=False):
                        conn.network.create_ip(
                            floating_network_id=_attr(network, "id"),
                            floating_ip_address=str(ip),
                        )
                        return
        raise ProviderError(
            f"{self.provider.id}: {address} lies in no subnet of an external network"
        )

    def _server_port(self, server_id: str, address: str) -> str | None:
        canonical = _canonical_ip(address)
        for port in self._connection().network.ports(device_id=server_id):
            fixed = [
                _canonical_ip(str(ip.get("ip_address"))) for ip in _attr(port, "fixed_ips") or []
            ]
            if canonical in fixed:
                return str(_attr(port, "id"))
        return None

    def _floating_ip_port(self, address: str) -> str | None:
        fip = self._project_fip(self._connection(), address)
        return (_attr(fip, "port_id") or None) if fip is not None else None

    def _disassociate_floating_ip(self, address: str) -> str | None:
        conn = self._connection()
        fip = self._project_fip(conn, address)
        port = _attr(fip, "port_id") if fip is not None else None
        if not port:
            return None
        conn.network.update_ip(fip, port_id=None)
        return str(port)

    def _associate_floating_ip(self, address: str, port_id: str) -> None:
        conn = self._connection()
        fip = self._project_fip(conn, address)
        if fip is None:
            raise ProviderError(f"{self.provider.id}: the project holds no floating IP {address}")
        if _attr(fip, "port_id") != port_id:
            conn.network.update_ip(fip, port_id=port_id)

    def _release_floating_ip(self, address: str) -> None:
        conn = self._connection()
        fip = self._project_fip(conn, address)
        if fip is not None:
            conn.network.delete_ip(fip, ignore_missing=True)

    def _lb_pool(self, pool_id: str) -> dict[str, Any] | None:
        """The pool as MAP_LB_POOL_INCOMPATIBLE compares it (SDD §8.5, §10): its protocol,
        algorithm, session persistence's and health monitor's types, its listeners' protocols and
        its load balancer's provider - raw GETs under one read's bound, none logged. A listener's
        TLS container references are neither followed nor kept (§23.7)."""
        get = _BudgetedGet(self._connection().load_balancer)

        def one(kind: str, key: str, ref: Any) -> dict[str, Any]:
            path = f"/lbaas/{kind}/{urllib.parse.quote(str(ref), safe='')}"
            return dict((get.get(path, {}, may_be_gone=True) or {}).get(key) or {})

        try:
            pool = one("pools", "pool", pool_id)
            if not pool:
                return None
            monitor = pool.get("healthmonitor_id")
            listeners = [
                one("listeners", "listener", ref.get("id")).get("protocol")
                for ref in pool.get("listeners") or []
                if ref.get("id")
            ]
            balancers = [ref.get("id") for ref in pool.get("loadbalancers") or [] if ref.get("id")]
            provider = (
                one("loadbalancers", "loadbalancer", balancers[0]).get("provider")
                if balancers
                else None
            )
            monitor_type = (
                one("healthmonitors", "healthmonitor", monitor).get("type") if monitor else None
            )
        except Exception as exc:
            raise ProviderError(f"{self.provider.id}: Octavia: {_read_failure(exc)[1]}") from None
        return {
            "id": str(pool.get("id") or pool_id),
            "name": pool.get("name"),
            "protocol": pool.get("protocol"),
            "lb_algorithm": pool.get("lb_algorithm"),
            "persistence": (pool.get("session_persistence") or {}).get("type"),
            "monitor": monitor_type,
            "listeners": [str(p) for p in listeners if p],
            "provider": provider,
        }

    def _list_pool_members(self, pool_id: str) -> list[dict[str, Any]]:
        """The pool's members (SDD §8.5, §10); [] for a pool the credential does not see."""
        get = _BudgetedGet(self._connection().load_balancer)
        path = f"/lbaas/pools/{urllib.parse.quote(pool_id, safe='')}/members"
        try:
            members = get.pages(path, "members")
        except _ServiceAnswer as exc:
            if exc.status == 404:
                return []
            raise ProviderError(f"{self.provider.id}: Octavia: HTTP {exc.status}") from None
        except Exception as exc:
            raise ProviderError(f"{self.provider.id}: Octavia: {_read_failure(exc)[1]}") from None
        return [
            {
                "id": str(m.get("id")),
                "address": _canonical_ip(str(m.get("address"))),
                "protocol_port": int(m.get("protocol_port")),
                "admin_state_up": bool(m.get("admin_state_up", True)),
                "provisioning_status": m.get("provisioning_status"),
            }
            for m in members
        ]

    def _octavia_change(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, gone_ok: bool = False
    ) -> dict[str, Any]:
        """One change sent to Octavia (SDD §8.5, §10): raw, not logged; its answer's body. A 409 -
        the load balancer PENDING_* - is ProviderConflict, any other refusal a provider error by
        its HTTP status alone; a 404 is no error when ``gone_ok``."""
        adapter = self._connection().load_balancer
        try:
            response = adapter.request(path, method, json=body, raise_exc=False, log=False)
        except Exception as exc:
            raise ProviderError(f"{self.provider.id}: Octavia: {type(exc).__name__}") from None
        status = int(response.status_code)
        if status in (200, 201, 202):
            return dict(response.json() or {}) if response.content else {}
        if status == 204 or (status == 404 and gone_ok):
            return {}
        if status == 409:
            raise ProviderConflict(f"{self.provider.id}: Octavia refused the change: HTTP 409")
        raise ProviderError(f"{self.provider.id}: Octavia refused the change: HTTP {status}")

    @staticmethod
    def _member_path(pool_id: str, member_id: str | None = None) -> str:
        path = f"/lbaas/pools/{urllib.parse.quote(pool_id, safe='')}/members"
        return path if member_id is None else f"{path}/{urllib.parse.quote(member_id, safe='')}"

    def _set_pool_member(self, pool_id: str, member_id: str, admin_state_up: bool) -> None:
        body = {"member": {"admin_state_up": admin_state_up}}
        self._octavia_change("PUT", self._member_path(pool_id, member_id), body)

    def _add_pool_member(self, pool_id: str, address: str, protocol_port: int) -> str:
        """A member of the address, on the credential's subnet that holds it - none when no
        subnet does, Octavia then reaching it from the VIP subnet (SDD §8.5, §10)."""
        conn = self._connection()
        ip = ipaddress.ip_address(address)
        subnet = next(
            (
                str(_attr(sub, "id"))
                for sub in _try(lambda: list(conn.network.subnets()), [])
                if _attr(sub, "cidr")
                and ip in ipaddress.ip_network(str(_attr(sub, "cidr")), strict=False)
            ),
            None,
        )
        member: dict[str, Any] = {"address": address, "protocol_port": protocol_port}
        if subnet is not None:
            member["subnet_id"] = subnet
        answer = self._octavia_change("POST", self._member_path(pool_id), {"member": member})
        return str((answer.get("member") or {}).get("id"))

    def _delete_pool_member(self, pool_id: str, member_id: str) -> None:
        self._octavia_change("DELETE", self._member_path(pool_id, member_id), gone_ok=True)

    def _dns_recordset(self, zone: str, name: str, rdtype: str) -> dict[str, Any] | None:
        """Designate's recordset of the name and type in the zone the credential sees by that
        name (SDD §8.4, §10): raw GETs under one read's bound, none logged - they name the zone and
        the record (§13.4) -, a failure named by its HTTP status or its kind alone."""
        get = _BudgetedGet(self._connection().dns)
        try:
            zones = get.get("/zones", {"name": zone}).get("zones") or []
            if not zones:
                return None
            zone_id = str(zones[0]["id"])
            found = get.get(f"/zones/{zone_id}/recordsets", {"name": name, "type": rdtype})
        except Exception as exc:
            raise ProviderError(f"{self.provider.id}: Designate: {_read_failure(exc)[1]}") from None
        recordsets = found.get("recordsets") or []
        if not recordsets:
            return None
        recordset = recordsets[0]
        ttl = recordset.get("ttl")
        return {
            "zone_id": zone_id,
            "id": str(recordset["id"]),
            "records": [str(r) for r in recordset.get("records") or []],
            # a recordset without its own TTL has its zone's
            "ttl": ttl if ttl is not None else zones[0].get("ttl"),
            "status": recordset.get("status"),
        }

    def _update_dns_recordset(self, zone_id: str, recordset_id: str, records: list[str]) -> None:
        """The recordset's records replaced, its TTL kept (SDD §8.4, §10): a raw PUT, not logged;
        a refusal named by its HTTP status alone."""
        dns = self._connection().dns
        try:
            response = dns.put(
                f"/zones/{zone_id}/recordsets/{recordset_id}",
                json={"records": records},
                raise_exc=False,
                log=False,
            )
        except Exception as exc:
            raise ProviderError(f"{self.provider.id}: Designate: {type(exc).__name__}") from None
        status = int(response.status_code)
        if status not in (200, 202):
            raise ProviderError(f"{self.provider.id}: Designate refused the update: HTTP {status}")

    def _server_of_address(self, address: str) -> str | None:
        # a compute port's device only: a floating IP's own port names the floating IP, a
        # router's the router (SDD §7.2, §10, review RV30-06); of the credential's project only,
        # where the kit's conversion host and volumes live - an admin credential sees every
        # project's ports, and tenant networks overlap (review PV-04)
        conn = self._connection()
        network = conn.network
        canonical = _canonical_ip(address)
        project = _attr(conn, "current_project_id", default=None)

        def server(port: Any) -> str | None:
            owner = str(_attr(port, "device_owner", default="") or "")
            device = _attr(port, "device_id")
            ours = not project or _attr(port, "project_id") == project
            return str(device) if owner.startswith("compute:") and device and ours else None

        def one(holders: set[str]) -> str | None:
            if len(holders) > 1:  # two networks of one subnet: no answer to guess (PV-04)
                raise ProviderError(
                    f"{self.provider.id}: more than one server holds {address}: "
                    f"{', '.join(sorted(holders))}"
                )
            return next(iter(holders), None)

        scope = {"project_id": project} if project else {}
        holders: set[str] = set()
        for port in network.ports(fixed_ips=[f"ip_address={canonical}"], **scope):
            fixed = [ip.get("ip_address") for ip in _attr(port, "fixed_ips", default=None) or []]
            held = server(port)
            if held and canonical in (_canonical_ip(str(a)) for a in fixed if a):
                holders.add(held)
        if holders:
            return one(holders)
        import openstack.exceptions

        for floating in network.ips(floating_ip_address=canonical):
            port_id = _attr(floating, "port_id")
            if not port_id:
                continue
            try:
                port = network.get_port(port_id)
            except openstack.exceptions.NotFoundException:
                continue  # gone since the listing; a read that failed is no answer (RV32A-04)
            held = server(port)
            if held:
                holders.add(held)
        return one(holders)

    def _server_volumes(self, server_id: str) -> list[str]:
        # Nova's record of the server (os-extended-volumes): no Cinder call, so a VMware rollback
        # reads it before the source's power-on (SDD §7.2, §10)
        import openstack.exceptions

        try:
            server = self._connection().compute.get_server(server_id)
        except openstack.exceptions.NotFoundException:
            return []  # a server gone holds none
        attached = _attr(server, "attached_volumes", default=None) or []
        return [str(_attr(v, "id")) for v in attached if _attr(v, "id")]

    def _set_volume_metadata(self, volume_id: str, metadata: dict[str, str]) -> None:
        # Cinder's POST /volumes/{id}/metadata merges: the kit's own keys stay (SDD §7.2, §10)
        self._connection().block_storage.set_volume_metadata(volume_id, **metadata)


def _os_hint(properties: Mapping[str, Any]) -> str | None:
    """``os_distro`` + ``os_version`` of image properties, else their ``os_type``."""
    distro = str(properties.get("os_distro") or "").strip()
    version = str(properties.get("os_version") or "").strip()
    if distro:
        return f"{distro} {version}".strip()
    return str(properties.get("os_type") or "").strip() or None


def _best_os_type(*candidates: str | None) -> str | None:
    """The most specific candidate: one that names a distribution, else the first one given."""
    given = [c for c in candidates if c]
    for candidate in given:
        if identify(candidate).distro is not None:
            return candidate
    return given[0] if given else None


def _free_floating_ips(conn: Any, project: Any) -> int | None:
    """How many more floating IPs the project may allocate, by Neutron's quota details (SDD §10):
    None when unlimited."""
    details = _attr(conn.network.get_quota(project, details=True), "floating_ips") or {}
    limit = int(details.get("limit", -1))
    if limit < 0:
        return None
    return max(0, limit - int(details.get("used", 0)) - int(details.get("reserved", 0)))


class _MapContext:
    """Per-call caches used while mapping servers to :class:`VMRef`."""

    def __init__(self, conn: Any, gpu_profiles: Mapping[str, str] | None = None) -> None:
        self.conn = conn
        #: the source's declared vGPU profiles by flavor name, which its VMs' GPUs take (SDD §22.3)
        self.gpu_profiles = dict(gpu_profiles or {})
        self._projects: dict[str, str | None] = {}
        self._networks: dict[str, Any] = {}
        self._images: dict[str, dict[str, Any]] = {}
        # filled by prefetch(): one listing each instead of a call per volume and per server.
        # None means "not prefetched" and the per-VM calls are used.
        self._volumes: dict[str, Any] | None = None
        self._ports: dict[str, list[Any]] | None = None
        #: the addresses of the floating IPs by the port they are associated with (SDD §10)
        self._floating: dict[str, list[str]] | None = None

    def prefetch(self) -> None:
        """Lists the volumes and ports of the connection's scope once (inventory of many VMs).

        Each listing is optional: a policy that refuses it (HTTP 403) or an old API falls back
        to the per-VM calls, which is what a single-VM lookup always uses.
        """
        volumes = _try(lambda: list(self.conn.block_storage.volumes(details=True)), None)
        if volumes is not None:
            self._volumes = {str(_attr(v, "id")): v for v in volumes}
        ports = _try(lambda: list(self.conn.network.ports()), None)
        if ports is not None:
            grouped: dict[str, list[Any]] = {}
            for port in ports:
                grouped.setdefault(str(_attr(port, "device_id", default="")), []).append(port)
            self._ports = grouped
        floating = _try(lambda: list(self.conn.network.ips()), None)
        if floating is not None:
            held: dict[str, list[str]] = {}
            for ip in floating:
                if _attr(ip, "port_id"):
                    held.setdefault(str(_attr(ip, "port_id")), []).append(
                        str(_attr(ip, "floating_ip_address"))
                    )
            self._floating = held

    def volume(self, volume_id: str) -> Any:
        if self._volumes is not None and volume_id in self._volumes:
            return self._volumes[volume_id]
        # not in the listing (e.g. a volume of another project attached by an admin)
        return self.conn.block_storage.get_volume(volume_id)

    def ports(self, server_id: str) -> list[Any]:
        if self._ports is not None:
            return self._ports.get(server_id, [])
        return list(self.conn.network.ports(device_id=server_id))

    def floating_ips(self, port_id: str) -> list[str]:
        """The addresses of the floating IPs associated with a port (SDD §10, plan task N2b): from
        the one list prefetch() read, else the port's own; [] when they cannot be read - a missing
        finding, never a failed inventory."""
        if self._floating is not None:
            return self._floating.get(port_id, [])
        listed = _try(lambda: list(self.conn.network.ips(port_id=port_id)), [])
        return [str(_attr(ip, "floating_ip_address")) for ip in listed]

    def project_name(self, project_id: str | None) -> str | None:
        if not project_id:
            return None
        if project_id not in self._projects:
            project = _try(lambda: self.conn.identity.get_project(project_id), None)
            self._projects[project_id] = _attr(project, "name") if project is not None else None
        return self._projects[project_id]

    def image_properties(self, image_id: str | None) -> dict[str, Any]:
        """``os_distro``/``os_version``/``os_type`` of a Glance image (cached per call)."""
        if not image_id:
            return {}
        if image_id not in self._images:
            image = _try(lambda: self.conn.image.get_image(image_id), None)
            self._images[image_id] = {
                key: _attr(image, key)
                for key in ("os_distro", "os_version", "os_type")
                if image is not None and _attr(image, key)
            }
        return self._images[image_id]

    def network(self, network_id: str) -> Any:
        if network_id not in self._networks:
            self._networks[network_id] = _try(
                lambda: self.conn.network.get_network(network_id), None
            )
        return self._networks[network_id]

    def server_to_vmref(self, server: Any) -> VMRef:
        conn = self.conn
        flavor = _server_flavor(conn, server)
        vcpus, ram, disk_gb = (
            _attr(flavor, "vcpus"),
            _attr(flavor, "ram"),
            _attr(flavor, "disk"),
        )
        flavor_name = _attr(flavor, "original_name", "name")
        extra_specs = _attr(flavor, "extra_specs", default={}) or {}
        specs = {str(k): str(v) for k, v in extra_specs.items()}

        image = _attr(server, "image", default={}) or {}
        image_booted = bool(_attr(image, "id"))
        root_device = _attr(server, "root_device_name", default="/dev/vda")
        server_id = str(_attr(server, "id"))
        disks: list[Disk] = []
        if image_booted:
            disks.append(
                Disk(
                    id=f"{server_id}-root",
                    name="root",
                    size_gb=int(disk_gb or 0),
                    bootable=True,
                    device=root_device,
                    kind="image_root",
                )
            )
        # flavor ephemeral (GiB) and swap (MiB) disks live on the hypervisor: they are copied
        # like the root disk and count towards capacity and the estimate (SDD §4.2)
        ephemeral_gb = int(_attr(flavor, "ephemeral", default=0) or 0)
        swap_mb = int(_attr(flavor, "swap", default=0) or 0)
        if ephemeral_gb > 0:
            disks.append(
                Disk(
                    id=f"{server_id}-ephemeral",
                    name="ephemeral",
                    size_gb=ephemeral_gb,
                    kind="ephemeral",
                )
            )
        if swap_mb > 0:
            disks.append(
                Disk(
                    id=f"{server_id}-swap",
                    name="swap",
                    size_gb=math.ceil(swap_mb / 1024),
                    kind="ephemeral",
                )
            )
        volume_disks: list[tuple[Disk, bool]] = []
        boot_meta: dict[str, Any] = {}
        for attachment in conn.compute.volume_attachments(server):
            volume = self.volume(str(_attr(attachment, "volume_id")))
            device = _attr(attachment, "device")
            disk = Disk(
                id=str(_attr(volume, "id")),
                name=_attr(volume, "name"),
                size_gb=int(_attr(volume, "size", default=0)),
                bootable=not image_booted and device == root_device,
                volume_type=_attr(volume, "volume_type"),
                device=device,
                kind="volume",
                multiattach=_truthy(_attr(volume, "is_multiattach", "multiattach")),
                encrypted=_truthy(_attr(volume, "is_encrypted", "encrypted")),
                pool=_attr(volume, "host"),
            )
            volume_disks.append((disk, _truthy(_attr(volume, "is_bootable", "bootable"))))
            if disk.bootable or (
                not boot_meta and _truthy(_attr(volume, "is_bootable", "bootable"))
            ):
                boot_meta = dict(_attr(volume, "volume_image_metadata", default={}) or {})
        if not image_booted and volume_disks and not any(d.bootable for d, _ in volume_disks):
            # Nova reports no root_device_name (or a device the attachment does not carry, e.g.
            # virtio-scsi /dev/sda): fall back to Cinder's bootable flag, lowest device first
            flagged = sorted((d for d, b in volume_disks if b), key=lambda d: d.device or "")
            if flagged:
                boot = flagged[0]
                volume_disks = [
                    (d.model_copy(update={"bootable": True}) if d is boot else d, b)
                    for d, b in volume_disks
                ]
        disks.extend(d for d, _ in volume_disks)
        disks.sort(key=lambda d: (not d.bootable, d.device or ""))

        nics = []
        for port in self.ports(server_id):
            network = self.network(_attr(port, "network_id"))
            nics.append(
                Nic(
                    network=str(_attr(network, "name", default=_attr(port, "network_id"))),
                    mac=_attr(port, "mac_address"),
                    fixed_ips=[ip.get("ip_address") for ip in _attr(port, "fixed_ips", default=[])],
                    vnic_type=_attr(port, "binding_vnic_type", default="normal") or "normal",
                    mtu=_attr(network, "mtu") if network is not None else None,
                    # what the import does not carry or may change (SDD §4.2, §9.3, plan task N2b)
                    allowed_address_pairs=[
                        str(pair["ip_address"])
                        for pair in _attr(port, "allowed_address_pairs", default=[]) or []
                        if isinstance(pair, Mapping) and pair.get("ip_address")
                    ],
                    floating_ips=self.floating_ips(str(_attr(port, "id"))),
                    dns_name=_attr(port, "dns_name") or None,
                    dns_domain=_attr(port, "dns_domain")
                    or (_attr(network, "dns_domain") if network is not None else None)
                    or None,
                )
            )

        metadata = {
            str(k): str(v) for k, v in (_attr(server, "metadata", default={}) or {}).items()
        }
        image_props = self.image_properties(_attr(image, "id")) if image_booted else {}
        os_type = _best_os_type(
            metadata.get("os_type"),
            _os_hint(metadata),
            _os_hint(boot_meta),
            _os_hint(image_props),
        )
        project_id = _attr(server, "project_id")
        return VMRef(
            source_id=str(_attr(server, "id")),
            name=str(_attr(server, "name")),
            project=self.project_name(project_id) or project_id,
            flavor=flavor_name,
            vcpus=int(vcpus or 0),
            ram_mb=int(ram or 0),
            disks=disks,
            nics=nics,
            power_state=_POWER.get(str(_attr(server, "status", default="")).upper(), "unknown"),  # type: ignore[arg-type]
            # a task under a steady status - an image snapshot, a stop in flight - is no power
            # state: the source watch reads a running guest as running (SDD §4.2, review PV-05)
            task_state=str(_attr(server, "task_state", default="") or "") or None,
            os_type=os_type,
            host=_attr(server, "compute_host", "hypervisor_hostname", "host"),
            tags=metadata,
            flavor_extra_specs=specs,
            key_name=_attr(server, "key_name", default=None) or None,
            # the GPUs its flavor asks for, with the profile its source declares (SDD §22.2)
            gpus=gpu_requests(specs, self.gpu_profiles.get(flavor_name) if flavor_name else None),
        )
