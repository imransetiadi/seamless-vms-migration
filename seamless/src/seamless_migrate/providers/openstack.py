"""OpenStack / RHOSO provider on openstacksdk (lazily imported; blocking calls in threads)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import threading
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from ..config import Settings
from ..domain.enums import ProviderRole
from ..domain.models import Disk, Nic, Provider, VMRef
from ..planning.preflight import DestinationInventory, SourceInventory
from ..security.secrets import SecretNotFound, openstack_cloud_entry
from .base import ProviderError, missing_dependency

log = logging.getLogger(__name__)

#: openstacksdk per-request timeout and the ceiling of one provider call from the control plane
PROVIDER_API_TIMEOUT_S = 60
PROVIDER_CALL_TIMEOUT_S = 300.0
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


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)
        if value is not None:
            return value
    return default


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
            raise ProviderError(f"{self.provider.id}: {message[:500]}") from exc

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

    async def console_log(self, server_id: str, lines: int = 200) -> str | None:
        return await self._run(self._console_log, server_id, lines)

    async def delete_server(self, server_id: str) -> None:
        await self._run(self._delete_server, server_id)

    async def find_server(self, name: str) -> str | None:
        return await self._run(self._find_server, name)

    # -- implementation (runs in worker threads) ----------------------------------------------
    def _check(self) -> dict[str, Any]:
        conn = self._connection()
        admin = _try(lambda: list(conn.compute.hypervisors()), None) is not None
        microversion = _try(
            lambda: str(conn.compute.get_endpoint_data().max_microversion or ""), ""
        )
        agents = _try(lambda: list(conn.network.agents()), [])
        ovn = any(
            "ovn" in str(_attr(a, "agent_type", default="")).lower()
            or "ovn" in str(_attr(a, "binary", default="")).lower()
            for a in agents
        )
        backends: list[str] = []
        if admin:
            backends = sorted(
                str(_attr(p, "name"))
                for p in _try(lambda: list(conn.block_storage.backend_pools()), [])
            )
        return {
            "admin": admin,
            "compute_microversion": microversion,
            "ovn": ovn,
            "volume_backends": backends,
        }

    def _list_vms(self) -> list[VMRef]:
        conn = self._connection()
        ctx = _MapContext(conn)
        return [ctx.server_to_vmref(s) for s in conn.compute.servers(details=True)]

    def _get_vm(self, source_id: str) -> VMRef:
        conn = self._connection()
        return _MapContext(conn).server_to_vmref(conn.compute.get_server(source_id))

    def _project_name(self, conn: Any) -> str:
        pid = _attr(conn, "current_project_id", default="")
        return _MapContext(conn).project_name(pid) or str(pid)

    def _inventory(self) -> SourceInventory | DestinationInventory:
        conn = self._connection()
        networks = {str(_attr(n, "name")): _attr(n, "mtu") for n in conn.network.networks()}
        project = self._project_name(conn)
        if self.provider.role == ProviderRole.source:
            return SourceInventory(networks=networks, projects=[project] if project else [])
        flavors = [
            {
                "name": str(_attr(f, "name")),
                "vcpus": int(_attr(f, "vcpus", default=0)),
                "ram_mb": int(_attr(f, "ram", default=0)),
                "disk_gb": int(_attr(f, "disk", default=0)),
                "extra_specs": dict(_attr(f, "extra_specs", default={}) or {}),
            }
            for f in conn.compute.flavors(details=True)
        ]
        volume_types = [str(_attr(t, "name")) for t in conn.block_storage.types()]
        quotas = _try(lambda: {project: self._free_quota(conn)}, {})
        return DestinationInventory(
            networks=networks,
            flavors=flavors,
            volume_types=volume_types,
            quotas=quotas,
            projects=[project] if project else [],
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
        return {
            "id": _attr(server, "id"),
            "name": _attr(server, "name"),
            "status": _attr(server, "status"),
            "ports": ports,
        }

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
        conn = self._connection()
        server = conn.compute.find_server(server_id, ignore_missing=True)
        if server is None:
            return
        conn.compute.delete_server(server, ignore_missing=True)
        wait = getattr(conn.compute, "wait_for_delete", None)
        if wait is not None:
            wait(server, wait=600)

    def _find_server(self, name: str) -> str | None:
        conn = self._connection()
        server = conn.compute.find_server(name, ignore_missing=True)
        return None if server is None else str(_attr(server, "id"))


class _MapContext:
    """Per-call caches used while mapping servers to :class:`VMRef`."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self._projects: dict[str, str | None] = {}
        self._networks: dict[str, Any] = {}

    def project_name(self, project_id: str | None) -> str | None:
        if not project_id:
            return None
        if project_id not in self._projects:
            project = _try(lambda: self.conn.identity.get_project(project_id), None)
            self._projects[project_id] = _attr(project, "name") if project is not None else None
        return self._projects[project_id]

    def network(self, network_id: str) -> Any:
        if network_id not in self._networks:
            self._networks[network_id] = _try(
                lambda: self.conn.network.get_network(network_id), None
            )
        return self._networks[network_id]

    def server_to_vmref(self, server: Any) -> VMRef:
        conn = self.conn
        flavor = _attr(server, "flavor", default={}) or {}
        vcpus, ram, disk_gb = (
            _attr(flavor, "vcpus"),
            _attr(flavor, "ram"),
            _attr(flavor, "disk"),
        )
        flavor_name = _attr(flavor, "original_name", "name")
        extra_specs = _attr(flavor, "extra_specs", default={}) or {}
        if vcpus is None and _attr(flavor, "id"):
            full = conn.compute.get_flavor(_attr(flavor, "id"))
            vcpus, ram, disk_gb = _attr(full, "vcpus"), _attr(full, "ram"), _attr(full, "disk")
            flavor_name = _attr(full, "name", default=flavor_name)
            extra_specs = _attr(full, "extra_specs", default={}) or {}

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
        for attachment in conn.compute.volume_attachments(server):
            volume = conn.block_storage.get_volume(_attr(attachment, "volume_id"))
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
            )
            volume_disks.append((disk, _truthy(_attr(volume, "is_bootable", "bootable"))))
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
        for port in conn.network.ports(device_id=_attr(server, "id")):
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
            os_type=metadata.get("os_type") or metadata.get("os_distro"),
            host=_attr(server, "compute_host", "hypervisor_hostname", "host"),
            tags=metadata,
            flavor_extra_specs={str(k): str(v) for k, v in extra_specs.items()},
        )
