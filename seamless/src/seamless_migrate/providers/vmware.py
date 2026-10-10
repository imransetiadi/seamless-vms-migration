"""VMware vSphere source provider on pyVmomi (lazily imported; blocking calls in threads)."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import ssl
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlparse

from ..config import Settings
from ..domain.models import Disk, Nic, Provider, VMRef
from ..planning.preflight import SourceInventory
from ..security.secrets import SecretNotFound, resolve
from .base import ProviderError, missing_dependency
from .openstack import PROVIDER_CALL_TIMEOUT_S

log = logging.getLogger(__name__)

GIB = 2**30
_POWER = {"poweredOn": "running", "poweredOff": "stopped", "suspended": "paused"}

Connector = Callable[..., Any]


_PRETTY_NAME = re.compile(r"prettyName='([^']+)'")


def _tools_os_name(guest: Any) -> str | None:
    """The exact release VMware Tools report (``guestDetailedData`` prettyName), if any."""
    detailed = str(getattr(guest, "guestDetailedData", "") or "")
    match = _PRETTY_NAME.search(detailed)
    return match.group(1).strip() or None if match else None


def _import_pyvmomi() -> tuple[Any, Any, Any]:
    try:
        from pyVim.connect import Disconnect, SmartConnect
        from pyVmomi import vim
    except ImportError:
        raise missing_dependency("vmware", "pyvmomi") from None
    return SmartConnect, Disconnect, vim


def default_connector(
    host: str, port: int, user: str, pwd: str, verify: bool, ca_cert: str | None
) -> Any:
    smart_connect, _, _ = _import_pyvmomi()
    if not verify:
        log.warning("%s: TLS certificate verification is disabled (verify_tls=false)", host)
    if verify:
        context = ssl.create_default_context(cafile=ca_cert) if ca_cert else None
        if context is not None:
            return smart_connect(host=host, port=port, user=user, pwd=pwd, sslContext=context)
        return smart_connect(host=host, port=port, user=user, pwd=pwd)
    return smart_connect(host=host, port=port, user=user, pwd=pwd, disableSslCertValidation=True)


def parse_endpoint(endpoint: str) -> tuple[str, int]:
    url = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    if not url.hostname:
        raise ProviderError(f"invalid vCenter endpoint {endpoint!r}")
    return url.hostname, url.port or 443


def _count_snapshots(nodes: Iterable[Any] | None) -> int:
    total = 0
    for node in nodes or []:
        total += 1 + _count_snapshots(getattr(node, "childSnapshotList", None))
    return total


def _disk_usage(vm: Any) -> dict[int, int]:
    """Bytes each virtual disk really occupies on its datastore (``vm.layoutEx``): the sum of
    the extent files behind the disk's chain, which is the used space of a thin disk. Without
    the layout the estimator falls back to its 60 % rule (SDD §4.2)."""
    layout = getattr(vm, "layoutEx", None)
    files = {
        int(getattr(f, "key", -1)): int(getattr(f, "size", 0) or 0)
        for f in (getattr(layout, "file", None) or [])
    }
    out: dict[int, int] = {}
    for disk in getattr(layout, "disk", None) or []:
        keys = [
            int(k)
            for chain in (getattr(disk, "chain", None) or [])
            for k in (getattr(chain, "fileKey", None) or [])
        ]
        if keys:
            out[int(getattr(disk, "key", -1))] = sum(files.get(k, 0) for k in keys)
    return out


def map_vm(
    vm: Any,
    vim: Any,
    portgroups: dict[str, str] | None = None,
    fields: dict[int, str] | None = None,
) -> VMRef:
    """Map a ``vim.VirtualMachine`` (or a duck-typed stub) to :class:`VMRef`."""
    portgroups = portgroups or {}
    fields = fields or {}
    config = vm.config
    source_id = str(getattr(config, "instanceUuid", None) or getattr(vm, "_moId", vm.name))
    devices = list(getattr(config.hardware, "device", []) or [])
    disk_cls = vim.vm.device.VirtualDisk
    nic_cls = vim.vm.device.VirtualEthernetCard

    used_by_key = _disk_usage(vm)
    disks: list[Disk] = []
    for dev in sorted((d for d in devices if isinstance(d, disk_cls)), key=lambda d: d.key):
        size_bytes = getattr(dev, "capacityInBytes", None) or int(dev.capacityInKB) * 1024
        backing = getattr(dev, "backing", None)
        mode = str(getattr(backing, "diskMode", "") or "")
        used = used_by_key.get(int(dev.key))
        disks.append(
            Disk(
                id=str(getattr(backing, "uuid", None) or f"{source_id}:{dev.key}"),
                name=getattr(getattr(dev, "deviceInfo", None), "label", None),
                size_gb=math.ceil(size_bytes / GIB),
                used_gb=None if used is None else round(min(used, size_bytes) / GIB, 3),
                bootable=not disks,
                device=getattr(getattr(dev, "deviceInfo", None), "label", None),
                kind="vmdk",
                independent="independent" in mode,
            )
        )

    guest = getattr(vm, "guest", None)
    guest_ips: dict[str, list[str]] = {}
    for entry in getattr(guest, "net", None) or []:
        mac = str(getattr(entry, "macAddress", "") or "").lower()
        guest_ips[mac] = [str(ip) for ip in (getattr(entry, "ipAddress", None) or [])]

    nics: list[Nic] = []
    for dev in (d for d in devices if isinstance(d, nic_cls)):
        backing = getattr(dev, "backing", None)
        network = getattr(backing, "deviceName", None)
        if not network:
            port = getattr(backing, "port", None)
            key = getattr(port, "portgroupKey", None)
            network = portgroups.get(key, key) if key else None
        if not network:
            network = getattr(backing, "opaqueNetworkId", None) or "unknown"
        mac = str(getattr(dev, "macAddress", "") or "")
        nics.append(
            Nic(network=str(network), mac=mac or None, fixed_ips=guest_ips.get(mac.lower(), []))
        )

    # PCI passthrough and vGPU (shared PCI) devices: expressed as the flavor extra specs the
    # pre-flight catalog keys off (SDD §9.3 VM_PCI_PASSTHROUGH / VM_VGPU)
    extra_specs: dict[str, str] = {}
    pci_cls = getattr(getattr(vim, "vm", None), "device", None)
    pci_cls = getattr(pci_cls, "VirtualPCIPassthrough", None)
    for dev in devices if pci_cls is not None else []:
        if not isinstance(dev, pci_cls):
            continue
        backing = getattr(dev, "backing", None)
        label = str(getattr(getattr(dev, "deviceInfo", None), "label", "") or "PCI device")
        if getattr(backing, "vgpu", None):
            extra_specs["resources:VGPU"] = "1"
        else:
            extra_specs["pci_passthrough:alias"] = label

    runtime = getattr(vm, "runtime", None)
    host = getattr(getattr(runtime, "host", None), "name", None)
    snapshot = getattr(vm, "snapshot", None)
    tags = {
        fields[cv.key]: str(cv.value)
        for cv in (getattr(vm, "customValue", None) or [])
        if getattr(cv, "key", None) in fields
    }
    return VMRef(
        source_id=source_id,
        name=str(vm.name),
        project=None,
        flavor=None,
        vcpus=int(getattr(config.hardware, "numCPU", 0) or 0),
        ram_mb=int(getattr(config.hardware, "memoryMB", 0) or 0),
        disks=disks,
        nics=nics,
        power_state=_POWER.get(str(getattr(runtime, "powerState", "")), "unknown"),  # type: ignore[arg-type]
        os_type=_tools_os_name(guest) or getattr(config, "guestId", None),
        host=str(host) if host else None,
        tags=tags,
        flavor_extra_specs=extra_specs,
        cbt_enabled=bool(getattr(config, "changeTrackingEnabled", False)),
        snapshot_count=_count_snapshots(getattr(snapshot, "rootSnapshotList", None)),
        tools_ok=str(getattr(guest, "toolsRunningStatus", "")) == "guestToolsRunning",
    )


class VMwareProvider:
    """Source provider for ``vmware`` (vCenter) providers."""

    def __init__(
        self,
        provider: Provider,
        settings: Settings,
        *,
        connector: Connector | None = None,
        vim: Any = None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self._connector = connector
        self._vim_module = vim

    # -- plumbing ----------------------------------------------------------------------------
    def _vim(self) -> Any:
        return self._vim_module if self._vim_module is not None else _import_pyvmomi()[2]

    def _connect(self) -> Any:
        self._vim()  # fail fast (ProviderError) when pyVmomi is missing
        if not self.provider.credentials_secret:
            raise ProviderError(f"{self.provider.id}: no credentials_secret configured")
        try:
            creds = resolve(self.provider.credentials_secret, self.settings)
        except SecretNotFound as exc:
            raise ProviderError(f"{self.provider.id}: {exc}") from None
        host, port = parse_endpoint(self.provider.endpoint)
        connector = self._connector or default_connector
        return connector(
            host=host,
            port=port,
            user=creds["username"],
            pwd=creds["password"],
            verify=self.provider.verify_tls,
            ca_cert=self.provider.ca_cert_path,
        )

    def _disconnect(self, si: Any) -> None:
        if self._connector is not None:
            return
        try:
            _import_pyvmomi()[1](si)
        except Exception:  # best effort logout
            pass

    async def _run(self, fn: Callable[[Any, Any], Any]) -> Any:
        def work() -> Any:
            si = self._connect()
            try:
                return fn(si.RetrieveContent(), self._vim())
            finally:
                self._disconnect(si)

        try:
            return await asyncio.wait_for(asyncio.to_thread(work), PROVIDER_CALL_TIMEOUT_S)
        except ProviderError:
            raise
        except TimeoutError:
            raise ProviderError(
                f"{self.provider.id}: call timed out after {PROVIDER_CALL_TIMEOUT_S:g} s"
            ) from None
        except Exception as exc:
            message = str(getattr(exc, "msg", "") or exc).strip() or type(exc).__name__
            raise ProviderError(f"{self.provider.id}: {message[:500]}") from exc

    @staticmethod
    def _find_by_uuid(content: Any, source_id: str) -> Any:
        """One SOAP call instead of a walk over every VM (``instanceUuid`` lookup)."""
        index = getattr(content, "searchIndex", None)
        find = getattr(index, "FindByUuid", None)
        if not callable(find):
            return None
        try:
            return find(None, source_id, True, True)
        except Exception:
            return None

    @staticmethod
    def _objects(content: Any, vim: Any, type_name: str) -> list[Any]:
        cls = getattr(vim, type_name, None)
        if cls is None:
            return []
        view = content.viewManager.CreateContainerView(content.rootFolder, [cls], True)
        try:
            return [obj for obj in view.view if isinstance(obj, cls)]
        finally:
            view.Destroy()

    def _context(self, content: Any, vim: Any) -> tuple[dict[str, str], dict[Any, str]]:
        """Custom-field names and DVS portgroup names used while mapping VMs."""
        fields = {
            f.key: f.name
            for f in (getattr(getattr(content, "customFieldsManager", None), "field", None) or [])
        }
        portgroups: dict[str, str] = {}
        dvs = getattr(vim, "dvs", None)
        if dvs is not None and hasattr(dvs, "DistributedVirtualPortgroup"):
            view = content.viewManager.CreateContainerView(
                content.rootFolder, [dvs.DistributedVirtualPortgroup], True
            )
            try:
                portgroups = {pg.key: pg.name for pg in view.view if hasattr(pg, "key")}
            finally:
                view.Destroy()
        return fields, portgroups

    def _vms(self, content: Any, vim: Any) -> list[VMRef]:
        fields, portgroups = self._context(content, vim)
        out = []
        for vm in self._objects(content, vim, "VirtualMachine"):
            config = getattr(vm, "config", None)
            if config is None or getattr(config, "template", False):
                continue
            out.append(map_vm(vm, vim, portgroups, fields))
        return out

    # -- protocol ----------------------------------------------------------------------------
    async def check(self) -> dict[str, Any]:
        def work(content: Any, vim: Any) -> dict[str, Any]:
            vms = self._vms(content, vim)
            about = content.about
            return {
                "api_version": str(getattr(about, "apiVersion", "")),
                "version": str(getattr(about, "version", "")),
                "vm_count": len(vms),
                "cbt_enabled_vms": sum(1 for v in vms if v.cbt_enabled),
                "vms_with_snapshots": sum(1 for v in vms if v.snapshot_count),
                "vms_with_independent_disks": sum(
                    1 for v in vms if any(d.independent for d in v.disks)
                ),
                "vms_without_tools": sum(1 for v in vms if v.tools_ok is False),
            }

        return await self._run(work)

    async def list_vms(self) -> list[VMRef]:
        return await self._run(self._vms)

    async def get_vm(self, source_id: str) -> VMRef:
        def work(content: Any, vim: Any) -> VMRef | None:
            vm = self._find_by_uuid(content, source_id)
            if vm is None or getattr(vm, "config", None) is None:
                return None
            fields, portgroups = self._context(content, vim)
            return map_vm(vm, vim, portgroups, fields)

        found = await self._run(work)
        if found is not None:
            return found
        for vm in await self.list_vms():  # moId-addressed or index-less setups
            if vm.source_id == source_id:
                return vm
        raise ProviderError(f"{self.provider.id}: VM {source_id!r} not found")

    async def power_on(self, source_id: str) -> None:
        """Power the source VM on again (rollback of VMware strategies)."""

        def work(content: Any, vim: Any) -> None:
            direct = self._find_by_uuid(content, source_id)
            candidates = (
                [direct] if direct is not None else self._objects(content, vim, "VirtualMachine")
            )
            for vm in candidates:
                config = getattr(vm, "config", None)
                uuid = getattr(config, "instanceUuid", None) or getattr(vm, "_moId", None)
                if uuid != source_id:
                    continue
                if str(getattr(vm.runtime, "powerState", "")) == "poweredOn":
                    return
                task = vm.PowerOnVM_Task()
                deadline = time.monotonic() + 600
                while str(getattr(task.info, "state", "")) not in ("success", "error"):
                    if time.monotonic() > deadline:
                        raise ProviderError(f"{self.provider.id}: power-on timed out")
                    time.sleep(2)
                if str(task.info.state) == "error":
                    raise ProviderError(f"{self.provider.id}: power-on failed: {task.info.error}")
                return
            raise ProviderError(f"{self.provider.id}: VM {source_id!r} not found")

        await self._run(work)

    async def inventory(self) -> SourceInventory:
        def work(content: Any, vim: Any) -> SourceInventory:
            networks = {str(n.name): None for n in self._objects(content, vim, "Network")}
            datacenters = [str(d.name) for d in self._objects(content, vim, "Datacenter")]
            return SourceInventory(networks=networks, projects=datacenters)

        return await self._run(work)
