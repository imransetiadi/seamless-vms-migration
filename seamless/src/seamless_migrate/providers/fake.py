"""Deterministic demo providers (SDD §10): 24 OpenStack VMs, 12 VMware VMs and a RHOSO cloud.

The traits (names, special cases) are fixed; sizes, usage and change rates vary with the seed.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Any

from ..domain.enums import ProviderKind
from ..domain.models import Disk, Nic, VMRef
from ..planning.preflight import DestinationInventory, SourceInventory
from .base import ProviderError

MIB = 2**20


@dataclass(frozen=True)
class _Spec:
    name: str
    flavor: str
    disks_gb: tuple[int, ...]
    os_type: str
    network: str = "finance-app"
    rate_mib: float = 1.0
    tags: tuple[tuple[str, str], ...] = ()
    power_state: str = "running"
    multiattach: bool = False
    image_root: bool = False
    encrypted: bool = False
    extra_specs: tuple[tuple[str, str], ...] = ()


_FLAVORS = {
    "m1.small": (2, 4096, 20),
    "m1.medium": (4, 8192, 40),
    "m1.large": (8, 16384, 80),
    "m1.xlarge": (16, 32768, 160),
    "g1.vgpu": (8, 32768, 80),
}

# 24 OpenStack VMs of the "Finance apps" estate (RHOSP 17.1).
_OPENSTACK: tuple[_Spec, ...] = (
    _Spec("web-01", "m1.small", (20,), "rhel9", rate_mib=0.5, tags=(("app", "portal"),)),
    _Spec("web-02", "m1.small", (20,), "rhel9", rate_mib=0.5, tags=(("app", "portal"),)),
    _Spec("web-03", "m1.small", (40,), "rhel9", rate_mib=0.8, image_root=True),
    _Spec("api-gw-01", "m1.medium", (30,), "rhel9", rate_mib=1.0),
    _Spec("portal-01", "m1.small", (25,), "rhel8", rate_mib=0.6),
    _Spec("app-01", "m1.large", (60,), "rhel8", rate_mib=2.0, tags=(("role", "tomcat"),)),
    _Spec("app-02", "m1.large", (60,), "rhel8", rate_mib=2.0, tags=(("role", "tomcat"),)),
    _Spec("mq-01", "m1.medium", (50,), "rhel9", rate_mib=4.0, tags=(("role", "rabbitmq"),)),
    _Spec("mq-02", "m1.medium", (80,), "rhel9", rate_mib=6.0, tags=(("role", "kafka"),)),
    _Spec("cache-01", "m1.medium", (30,), "rhel9", rate_mib=3.0, tags=(("role", "redis"),)),
    _Spec("dns-01", "m1.small", (20,), "rhel9", rate_mib=0.2),
    _Spec(
        "win-ad-01",
        "m1.large",
        (60,),
        "windows2019",
        rate_mib=1.0,
        tags=(("role", "domain-controller"),),
    ),
    _Spec("monitor-01", "m1.large", (100,), "rhel9", rate_mib=3.0, tags=(("role", "prometheus"),)),
    _Spec("bastion-01", "m1.small", (20,), "rhel9", rate_mib=0.1),
    _Spec(
        "ledger-db-01",
        "m1.xlarge",
        (100, 600),
        "rhel8",
        network="finance-db",
        rate_mib=8.0,
        tags=(("app", "ledger"), ("role", "postgresql")),
    ),
    _Spec(
        "payroll-db-01",
        "m1.xlarge",
        (100, 750),
        "rhel8",
        network="finance-db",
        rate_mib=6.0,
        tags=(("app", "payroll"), ("role", "mysql")),
        encrypted=True,
    ),
    _Spec(
        "billing-db-01",
        "m1.xlarge",
        (100, 1200),
        "rhel8",
        network="finance-db",
        rate_mib=12.0,
        tags=(("app", "billing"), ("role", "oracle")),
    ),
    _Spec(
        "billing-legacy-01",
        "m1.medium",
        (40,),
        "rhel6",
        rate_mib=1.0,
        tags=(("app", "billing-legacy"),),
    ),
    _Spec(
        "shared-fs-01",
        "m1.large",
        (50, 200),
        "rhel9",
        rate_mib=5.0,
        multiattach=True,
        tags=(("role", "clustered-filesystem"),),
    ),
    _Spec(
        "gpu-ml-01",
        "g1.vgpu",
        (100,),
        "rhel9",
        rate_mib=2.0,
        extra_specs=(("resources:VGPU", "1"),),
    ),
    _Spec("report-01", "m1.medium", (80,), "rhel9", rate_mib=1.5),
    _Spec("batch-01", "m1.medium", (60,), "rhel9", rate_mib=0.5, power_state="stopped"),
    _Spec("etl-01", "m1.large", (150,), "rhel9", rate_mib=4.0),
    _Spec(
        "ledger-web-01",
        "m1.small",
        (20,),
        "rhel9",
        rate_mib=0.5,
        tags=(("app", "ledger"), ("role", "frontend")),
    ),
)


@dataclass(frozen=True)
class _VSpec:
    name: str
    cpus: int
    ram_mb: int
    disks_gb: tuple[int, ...]
    guest_id: str
    network: str = "DC2-Prod"
    cbt: bool = True
    snapshots: int = 0
    independent: bool = False
    tools_ok: bool = True
    rate_mib: float = 1.0


# 12 VMware VMs of the "DC2" datacenter with mixed CBT/snapshot/independent-disk states.
_VMWARE: tuple[_VSpec, ...] = (
    _VSpec("dc2-web-01", 2, 4096, (40,), "rhel9_64Guest", network="DC2-DMZ", rate_mib=0.5),
    _VSpec("dc2-web-02", 2, 4096, (40,), "rhel9_64Guest", network="DC2-DMZ", cbt=False),
    _VSpec("dc2-proxy-01", 2, 4096, (30,), "rhel8_64Guest", network="DC2-DMZ"),
    _VSpec("dc2-app-01", 4, 8192, (80,), "rhel8_64Guest", rate_mib=2.0),
    _VSpec("dc2-mq-01", 4, 8192, (60,), "rhel8_64Guest", snapshots=2, rate_mib=3.0),
    _VSpec("dc2-dc-01", 4, 8192, (80,), "windows2019srv_64Guest"),
    _VSpec("dc2-mon-01", 4, 16384, (200,), "rhel9_64Guest", rate_mib=2.5),
    _VSpec("dc2-db-01", 8, 32768, (100, 400), "rhel8_64Guest", rate_mib=6.0),
    _VSpec("dc2-db-02", 8, 32768, (100, 600), "rhel8_64Guest", cbt=False, rate_mib=5.0),
    _VSpec("dc2-file-01", 4, 8192, (60, 500), "windows2019srv_64Guest", independent=True),
    _VSpec("dc2-legacy-01", 2, 4096, (40,), "windows2008_64Guest", tools_ok=False, snapshots=1),
    _VSpec("dc2-ci-01", 4, 8192, (120,), "rhel9_64Guest", cbt=False, rate_mib=4.0),
)


def _stable_id(prefix: str, name: str) -> str:
    return f"{prefix}-{hashlib.sha1(name.encode()).hexdigest()[:12]}"


def _mac(rng: random.Random) -> str:
    return "fa:16:3e:" + ":".join(f"{rng.randrange(256):02x}" for _ in range(3))


def _openstack_vm(spec: _Spec, rng: random.Random, index: int) -> VMRef:
    vcpus, ram, flavor_disk = _FLAVORS[spec.flavor]
    disks: list[Disk] = []
    for i, size in enumerate(spec.disks_gb):
        root = i == 0
        used = round(size * rng.uniform(0.25, 0.85), 1) if index % 5 != 4 else None
        kind = "image_root" if root and spec.image_root else "volume"
        disks.append(
            Disk(
                id=_stable_id("vol", f"{spec.name}-{i}"),
                name=f"{spec.name}-{'root' if root else f'data{i}'}",
                size_gb=size,
                used_gb=used,
                bootable=root,
                volume_type=None if kind == "image_root" else ("ceph-ssd" if root else "ceph-hdd"),
                device=f"/dev/vd{chr(ord('a') + i)}",
                kind=kind,
                multiattach=spec.multiattach and not root,
                encrypted=spec.encrypted and not root,
            )
        )
    net_mtu = 9000 if spec.network == "finance-db" else 1500
    return VMRef(
        source_id=_stable_id("srv", spec.name),
        name=spec.name,
        project="finance",
        flavor=spec.flavor,
        vcpus=vcpus,
        ram_mb=ram,
        disks=disks,
        nics=[
            Nic(
                network=spec.network,
                mac=_mac(rng),
                fixed_ips=[f"10.20.{1 if spec.network == 'finance-app' else 2}.{10 + index}"],
                mtu=net_mtu,
            )
        ],
        power_state=spec.power_state,  # type: ignore[arg-type]
        os_type=spec.os_type,
        host=f"compute-{index % 4}",
        tags=dict(spec.tags),
        flavor_extra_specs=dict(spec.extra_specs),
        change_rate_bps=round(spec.rate_mib * rng.uniform(0.8, 1.2) * MIB, 1),
    )


def _vmware_vm(spec: _VSpec, rng: random.Random, index: int) -> VMRef:
    disks = [
        Disk(
            id=_stable_id("vmdk", f"{spec.name}-{i}"),
            name=f"Hard disk {i + 1}",
            size_gb=size,
            used_gb=round(size * rng.uniform(0.3, 0.8), 1),
            bootable=i == 0,
            device=f"scsi0:{i}",
            kind="vmdk",
            independent=spec.independent and i > 0,
        )
        for i, size in enumerate(spec.disks_gb)
    ]
    return VMRef(
        source_id=_stable_id("vmw", spec.name),
        name=spec.name,
        project=None,
        flavor=None,
        vcpus=spec.cpus,
        ram_mb=spec.ram_mb,
        disks=disks,
        nics=[
            Nic(
                network=spec.network,
                mac="00:50:56:" + ":".join(f"{rng.randrange(256):02x}" for _ in range(3)),
                fixed_ips=[f"10.2.{0 if spec.network == 'DC2-Prod' else 9}.{20 + index}"],
            )
        ],
        power_state="running",
        os_type=spec.guest_id,
        host=f"esx-{index % 3 + 1:02d}.dc2",
        cbt_enabled=spec.cbt,
        snapshot_count=spec.snapshots,
        tools_ok=spec.tools_ok,
        change_rate_bps=round(spec.rate_mib * rng.uniform(0.8, 1.2) * MIB, 1),
    )


class FakeSourceProvider:
    def __init__(self, kind: ProviderKind | str, seed: int = 42) -> None:
        self.kind = ProviderKind(kind)
        self.seed = seed
        rng = random.Random(f"{self.kind}:{seed}")
        if self.kind == ProviderKind.vmware:
            self._vms = [_vmware_vm(s, rng, i) for i, s in enumerate(_VMWARE)]
        else:
            self._vms = [_openstack_vm(s, rng, i) for i, s in enumerate(_OPENSTACK)]
        self._by_id = {vm.source_id: vm for vm in self._vms}

    async def check(self) -> dict[str, Any]:
        if self.kind == ProviderKind.vmware:
            return {
                "admin": True,
                "api_version": "8.0.2.0",
                "cbt_enabled_vms": sum(1 for v in self._vms if v.cbt_enabled),
                "vms_with_snapshots": sum(1 for v in self._vms if v.snapshot_count),
                "vms_with_independent_disks": sum(
                    1 for v in self._vms if any(d.independent for d in v.disks)
                ),
                "vms_without_tools": sum(1 for v in self._vms if v.tools_ok is False),
            }
        return {
            "admin": True,
            "compute_microversion": "2.88",
            "ovn": True,
            "volume_backends": ["overcloud@tripleo_ceph#ceph-ssd", "overcloud@tripleo_ceph#hdd"],
        }

    async def list_vms(self) -> list[VMRef]:
        return [vm.model_copy(deep=True) for vm in self._vms]

    async def get_vm(self, source_id: str) -> VMRef:
        try:
            return self._by_id[source_id].model_copy(deep=True)
        except KeyError:
            raise ProviderError(f"VM {source_id!r} not found") from None

    async def power_on(self, source_id: str) -> None:
        await self.get_vm(source_id)

    async def delete_server(self, source_id: str) -> None:
        await self.get_vm(source_id)

    async def inventory(self) -> SourceInventory:
        if self.kind == ProviderKind.vmware:
            return SourceInventory(networks={"DC2-Prod": None, "DC2-DMZ": None}, projects=["DC2"])
        return SourceInventory(
            networks={"finance-app": 1500, "finance-db": 9000}, projects=["finance"]
        )


class FakeDestinationProvider:
    def __init__(self, seed: int = 42) -> None:
        self.seed = seed
        self.deleted: set[str] = set()

    async def check(self) -> dict[str, Any]:
        return {
            "admin": True,
            "compute_microversion": "2.95",
            "ovn": True,
            "volume_backends": ["hostgroup@ceph-ssd#ssd", "hostgroup@ceph-hdd#hdd"],
        }

    async def inventory(self) -> DestinationInventory:
        flavors = [
            {"name": name, "vcpus": v, "ram_mb": r, "disk_gb": d, "extra_specs": {}}
            for name, (v, r, d) in _FLAVORS.items()
            if name != "g1.vgpu"
        ]
        flavors.append(
            {"name": "m1.2xlarge", "vcpus": 32, "ram_mb": 65536, "disk_gb": 160, "extra_specs": {}}
        )
        free = {
            "cores": 2000,
            "ram_mb": 4_194_304,
            "instances": 500,
            "volumes": 1000,
            "gigabytes": 200_000,
        }
        return DestinationInventory(
            networks={
                "finance-app": 1500,
                "finance-db": 8942,
                "dc2-prod": 1500,
                "dc2-dmz": 1442,
            },
            flavors=flavors,
            volume_types=["ceph-ssd", "ceph-hdd", "__DEFAULT__"],
            quotas={"finance": dict(free), "dc2": dict(free)},
            projects=["finance", "dc2"],
        )

    async def get_server(self, server_id: str) -> dict[str, Any]:
        if server_id in self.deleted:
            raise ProviderError(f"server {server_id!r} not found")
        octet = int(hashlib.sha1(server_id.encode()).hexdigest()[:2], 16) % 200 + 20
        return {
            "id": server_id,
            "status": "ACTIVE",
            "ports": [{"status": "ACTIVE", "fixed_ips": [f"10.30.0.{octet}"], "floating_ips": []}],
        }

    async def console_log(self, server_id: str, lines: int = 200) -> str | None:
        return (
            "[    1.902] EXT4-fs (vda1): mounted filesystem with ordered data mode.\n"
            "[  OK  ] Reached target Multi-User System.\n"
            f"Red Hat Enterprise Linux 9.4 (Plow)\n{server_id} login: "
        )

    async def delete_server(self, server_id: str) -> None:
        self.deleted.add(server_id)

    async def find_server(self, name: str) -> str | None:
        return _stable_id("dst", name)
