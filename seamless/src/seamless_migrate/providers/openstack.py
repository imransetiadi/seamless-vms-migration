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
from typing import Any, TypeVar

from ..ai.redaction import redact_cut
from ..config import Settings
from ..domain.enums import ProviderRole
from ..domain.models import Disk, GpuDevice, Nic, Provider, VMRef, utcnow
from ..guest_os import identify
from ..planning.gpu import PoolRequest, gpu_pools, gpu_requests
from ..planning.platforms import destination_release
from ..planning.preflight import DestinationInventory, GpuCapacity, GpuPool, SourceInventory
from ..security.secrets import SecretNotFound, openstack_cloud_entry
from ..storage import storage_family
from .base import ProviderError, VMNotFound, microversion_text, missing_dependency

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


def connect(provider: Provider, settings: Settings) -> Any:
    """Open an openstacksdk connection for ``provider`` (credentials from ``clouds.yaml``)."""
    openstack = _import_openstack()
    kwargs: dict[str, Any] = {"app_name": "seamless-migrate"}
    if provider.credentials_secret or (settings.clouds_yaml is not None and provider.cloud):
        try:
            entry = openstack_cloud_entry(provider, settings)
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
    found = []
    for name, read in urls:
        url = _try(read, None)
        if isinstance(url, str) and url.strip().lower().startswith("http://"):
            found.append(name)
    return found


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
    status and the error code (SDD §22.3, §22.12)."""
    response = conn.placement.get(
        url, params=params, microversion=PLACEMENT_MICROVERSION, raise_exc=False
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


def _no_placement(exc: Exception) -> bool:
    """Whether ``exc`` says the catalog has no Placement: keystoneauth finds no endpoint, or
    openstacksdk's proxy of a service the catalog lacks refuses every call (SDD §22.3)."""
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
    if _no_placement(exc):
        return "no_placement", None
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return "error", f"HTTP {status}"
    return "error", type(exc).__name__


def _gpu_capacity(conn: Any, requests: Iterable[GpuDevice]) -> GpuCapacity:
    """The Placement pools of the vGPU requests (SDD §22.3), counts only: no provider's name or
    uuid leaves the read (§22.12). Without a vGPU request it sends Placement nothing. A refused
    credential, a catalog without Placement and any other failure read unknown - never a guess -
    and fail neither the inventory nor the check."""
    pools = gpu_pools(requests)
    read_at = utcnow()
    if not pools:
        return GpuCapacity(status="ok", read_at=read_at)
    try:
        counted = [_read_pool(conn, pool) for pool in pools]
    except Exception as exc:
        reason, detail = _gpu_read_failure(exc)
        log.info("GPU capacity not read: %s %s", reason, detail or "")
        return GpuCapacity(status="unknown", reason=reason, detail=detail, read_at=read_at)
    return GpuCapacity(status="ok", read_at=read_at, pools=counted)


def _candidates(conn: Any, pool: PoolRequest, units: int) -> Any:
    """GET /allocation_candidates for ``units`` of the pool's class with its traits, a forbidden
    one as !<trait> (SDD §22.3)."""
    params = {"resources": f"{pool.resource_class}:{units}"}
    required = [*pool.traits, *(f"!{trait}" for trait in pool.forbidden_traits)]
    if required:
        params["required"] = ",".join(required)
    return _placement_get(conn, "/allocation_candidates", params)


def _free_units(answer: Any, pool: PoolRequest) -> dict[str, int]:
    """The units free on each provider of an allocation-candidates answer that counts for the pool
    (SDD §22.3): it holds the class and the pool's traits, lacks the forbidden ones, and its root
    provider lacks COMPUTE_STATUS_DISABLED - a disabled compute service, which Nova's own requests
    forbid. Its free is Placement's capacity less its used. Keyed by uuid, kept by nothing."""
    summaries = answer.get("provider_summaries") or {}
    free: dict[str, int] = {}
    for uuid, summary in summaries.items():
        held = (summary.get("resources") or {}).get(pool.resource_class)
        traits = set(summary.get("traits") or [])
        root = summaries.get(summary.get("root_provider_uuid") or uuid) or summary
        if (
            held is None
            or not traits.issuperset(pool.traits)
            or traits.intersection(pool.forbidden_traits)
            or _COMPUTE_DISABLED in (root.get("traits") or [])
        ):
            continue
        free[uuid] = max(0, int(held.get("capacity", 0)) - int(held.get("used", 0)))
    return free


def _read_pool(conn: Any, pool: PoolRequest) -> GpuPool:
    """One pool as Placement shows it (SDD §22.3): the units free on the providers that count, how
    many have one free, and the most on one. Nova fills a request group from one provider, whose
    inventory's max_unit may cap it below its free: the pool is read again as <class>:<n> for each
    group of n > 1 units, and where that read finds no provider its largest free stays below n."""
    free = _free_units(_candidates(conn, pool, 1), pool)
    largest = max(free.values(), default=0)
    for units in pool.groups:
        answer = _candidates(conn, pool, units)
        named = {
            uuid
            for request in answer.get("allocation_requests") or []
            for uuid, allocation in (request.get("allocations") or {}).items()
            if pool.resource_class in (allocation.get("resources") or {})
        }
        if not named.intersection(_free_units(answer, pool)):
            largest = min(largest, units - 1)
    return GpuPool(
        resource_class=pool.resource_class,
        traits=list(pool.traits),
        forbidden_traits=list(pool.forbidden_traits),
        free=sum(free.values()),
        providers=sum(1 for left in free.values() if left > 0),
        largest_free=largest,
    )


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

    async def server_allocations(self, server_id: str) -> dict[str, int] | None:
        return await self._run(self._server_allocations, server_id)

    async def console_log(self, server_id: str, lines: int = 200) -> str | None:
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
                storage.append(
                    {
                        "pool": str(_attr(pool, "name")),
                        "vendor": caps.get("vendor_name"),
                        "protocol": caps.get("storage_protocol"),
                        "family": storage_family(caps),
                    }
                )
            storage.sort(key=lambda b: b["pool"])
        # the role names of its token: what Neutron's port policy names (SDD §10, §6.6); left out
        # when the token cannot tell, so that half of NET_PORT_NOT_PERMITTED is not judged
        roles = _try(
            lambda: sorted(set(conn.session.auth.get_access(conn.session).role_names or [])), None
        )
        plain_http = _plain_http(self.provider.endpoint, conn)
        report = {
            "admin": admin,
            **({"roles": roles} if roles is not None else {}),
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
            # the Placement pools of its GPU flavors' vGPU requests, read now (SDD §22.3)
            report["gpu_capacity"] = self._check_gpu_capacity(conn).to_dict()
        return report

    def _check_gpu_capacity(self, conn: Any) -> GpuCapacity:
        try:
            flavors = _flavors(conn)
        except Exception as exc:  # a probe, as the check's others: it fails nothing (SDD §22.3)
            reason, detail = _gpu_read_failure(exc)
            return GpuCapacity(status="unknown", reason=reason, detail=detail, read_at=utcnow())
        gpu_flavors = _gpu_flavors(flavors, self.provider.gpu_profiles)
        return _gpu_capacity(conn, (gpu for flavor in gpu_flavors for gpu in flavor["gpus"]))

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
            return SourceInventory(
                networks=networks, projects=[project] if project else [], keypairs=keypairs
            )
        flavors = _flavors(conn)
        # the flavors that ask for a GPU, with their declared profiles, and the Placement pools of
        # their vGPU requests (SDD §22.3)
        gpu_flavors = _gpu_flavors(flavors, self.provider.gpu_profiles)
        gpu_capacity = _gpu_capacity(conn, (gpu for f in gpu_flavors for gpu in f["gpus"]))
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
        )

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
        # compute 2.47's embedded flavor: its extra specs stay empty where Nova's policy hides them
        # (SDD §10, §22.3)
        flavor = _attr(server, "flavor", default={}) or {}
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
        return _gpu_capacity(self._connection(), requests)

    def _server_allocations(self, server_id: str) -> dict[str, int] | None:
        conn = self._connection()
        try:
            answer = _placement_get(conn, f"/allocations/{urllib.parse.quote(server_id, safe='')}")
            units: dict[str, int] = {}
            for held in (answer.get("allocations") or {}).values():
                for resource_class, amount in (held.get("resources") or {}).items():
                    units[str(resource_class)] = units.get(str(resource_class), 0) + int(amount)
        except Exception as exc:  # unread, never a guess: the verification skips its check (§22.8)
            reason, detail = _gpu_read_failure(exc)
            log.info("allocations of server %s not read: %s %s", server_id, reason, detail or "")
            return None
        return units or None

    def _console_log(self, server_id: str, lines: int) -> str | None:
        conn = self._connection()
        try:
            result = conn.compute.get_server_console_output(server_id, length=lines)
        except Exception:
            log.info("console log unavailable for %s", server_id, exc_info=True)
            return None
        output = _attr(result, "output") if result is not None else None
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

    def volume(self, volume_id: str) -> Any:
        if self._volumes is not None and volume_id in self._volumes:
            return self._volumes[volume_id]
        # not in the listing (e.g. a volume of another project attached by an admin)
        return self.conn.block_storage.get_volume(volume_id)

    def ports(self, server_id: str) -> list[Any]:
        if self._ports is not None:
            return self._ports.get(server_id, [])
        return list(self.conn.network.ports(device_id=server_id))

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
        flavor = _attr(server, "flavor", default={}) or {}
        flavor_id = _attr(flavor, "id")
        if not _attr(flavor, "original_name") and flavor_id:
            # Nova embeds the flavor from compute 2.47 on; below it the server holds only the
            # flavor's id, which openstacksdk's Flavor reads as 0 everywhere (review RV28-08) -
            # and its extra specs only when asked, below 2.61 (review RV31-04): read apart, so a
            # policy that forbids them keeps the flavor without them, as Nova 2.47+ does (RV32B-03)
            looked = _try(lambda: conn.compute.get_flavor(flavor_id), None)
            if looked is not None:
                flavor = _try(lambda: looked.fetch_extra_specs(conn.compute), None) or looked
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
