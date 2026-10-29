"""Deterministic demo providers (SDD §10): 24 OpenStack VMs, 16 VMware VMs and two destinations,
RHOSO 18.0 and a community cloud, with GPU flavors and the Placement pools of their vGPUs (§22.10).

The traits (names, special cases) are fixed; sizes, usage and change rates vary with the seed.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..domain.enums import ProviderKind
from ..domain.models import Disk, GpuDevice, Nic, Provider, VMRef, utcnow
from ..planning.gpu import gpu_pools, gpu_requests, pool_name
from ..planning.platforms import destination_release
from ..planning.preflight import (
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
from .base import ProviderError, VMNotFound

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
    netapp: bool = False  # volumes on the ONTAP NFS backend instead of Ceph
    # what its port carries that the migration does not (SDD §4.2, §9.3, plan task N2b): its
    # floating IPs, its allowed address pairs, and its DNS name under the domain DEMO_DNS_DOMAIN
    floating_ips: tuple[str, ...] = ()
    address_pairs: tuple[str, ...] = ()
    dns_name: str | None = None


# Cinder pools of the demo clouds: Ceph RBD and NetApp ONTAP over NFS (SDD §7.3.1, §10).
_CEPH = {"vendor": "Open Source", "protocol": "ceph", "family": "rbd"}
_ONTAP_NFS = {"vendor": "NetApp", "protocol": "nfs", "family": "netapp_nfs"}
#: the demo clusters' fsids (SDD §10, §28.4, plan task CE11): an rbd pool reports its cluster's
#: and its Ceph pool, as the real check reads them from location_info
DEMO_CEPH_FSID = "6f8c2d4e-1b3a-4c5d-9e7f-0a1b2c3d4e5f"
DEMO_DST_CEPH_FSID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"


def _rbd(fsid: str, pool: str) -> dict[str, Any]:
    return {**_CEPH, "ceph_fsid": fsid, "ceph_pool": pool}


_SOURCE_POOLS = {
    "ceph-ssd": ("overcloud@tripleo_ceph#ceph-ssd", _rbd(DEMO_CEPH_FSID, "volumes-ssd")),
    "ceph-hdd": ("overcloud@tripleo_ceph#hdd", _rbd(DEMO_CEPH_FSID, "volumes")),
    "netapp-nfs": ("overcloud@tripleo_netapp#192.0.2.50:/cinder_finance", _ONTAP_NFS),
}
_DESTINATION_POOLS = (
    ("hostgroup@ceph-ssd#ssd", _rbd(DEMO_DST_CEPH_FSID, "ssd")),
    ("hostgroup@ceph-hdd#hdd", _rbd(DEMO_DST_CEPH_FSID, "hdd")),
    # same SVM export, mounted through the RHOSO storage network's LIF
    ("hostgroup@ontap-nfs#198.51.100.50:/cinder_finance", _ONTAP_NFS),
)


def _storage_caps(pools: Any) -> dict[str, Any]:
    backends = sorted(({"pool": name, **caps} for name, caps in pools), key=lambda b: b["pool"])
    return {"volume_backends": [b["pool"] for b in backends], "storage_backends": backends}


_FLAVORS = {
    "m1.small": (2, 4096, 20),
    "m1.medium": (4, 8192, 40),
    "m1.large": (8, 16384, 80),
    "m1.xlarge": (16, 32768, 160),
    "g1.vgpu": (8, 32768, 80),
}

#: the domain the demo source publishes its ports' DNS names in, which its destination has no
#: Designate zone of (SDD §9.3, plan task N2b)
DEMO_DNS_DOMAIN = "example.com."
#: the Designate zones the demo destination holds
DEMO_DNS_ZONES = ("internal.example.org.",)
#: the projects the demo clouds' provider credentials are scoped to, as their checks name them
#: (SDD §10 "The check of project credentials", §27.7): the source's is its VMs' project, the
#: destination's owns its own networks (network_details)
DEMO_SOURCE_PROJECT = {"id": "project-finance", "name": "finance", "domain": "Default"}
DEMO_DESTINATION_PROJECT = {"id": "project-demo", "name": "demo", "domain": "Default"}

# the demo's network cut-over (SDD §10, §23.9, plan task N11)
#: the source's Heat stack: web-01 a server resource, the Tomcat pair its autoscaling group's
DEMO_STACK = "example-portal"
DEMO_STACK_SERVERS = {
    "web-01": ("portal_web", None),
    "app-01": ("app_group", "app_group"),
    "app-02": ("app_group", "app_group"),
}
#: the source's Octavia pools, by id: their load balancer, members (VM names) and protocol port
DEMO_PORTAL_POOL = "example-portal-pool"
DEMO_SOURCE_POOLS = {
    DEMO_PORTAL_POOL: ("example-portal-lb", ("web-01", "web-02", "web-03"), 80),
    "example-api-pool": ("example-api-lb", ("api-gw-01",), 8443),
}
#: the pool the finance plan maps the portal pool to, on the destination's amphora load balancer
DEMO_DESTINATION_POOL = "example-portal-pool-rhoso"
#: the VM whose record the demo destination's Designate holds, and the address its NIC override
#: gives it there (SDD §10)
DEMO_DNS_VM = "ledger-web-01"
DEMO_DNS_NEW_ADDRESS = "10.20.1.133"

# 24 OpenStack VMs of the "Finance apps" estate (RHOSP 17.1).
_OPENSTACK: tuple[_Spec, ...] = (
    _Spec(
        "web-01",
        "m1.small",
        (20,),
        "rhel9",
        rate_mib=0.5,
        tags=(("app", "portal"),),
        # a public address the destination does not hold, and a name in DNS (plan task N2b)
        floating_ips=("203.0.113.10",),
        dns_name="web-01",
    ),
    _Spec("web-02", "m1.small", (20,), "ubuntu 24.04", rate_mib=0.5, tags=(("app", "portal"),)),
    _Spec("web-03", "m1.small", (40,), "rhel9", rate_mib=0.8, image_root=True),
    _Spec("api-gw-01", "m1.medium", (30,), "rocky 9.4", rate_mib=1.0),
    _Spec("portal-01", "m1.small", (25,), "debian 12", rate_mib=0.6, netapp=True),
    _Spec(
        "app-01", "m1.large", (60,), "rhel8", rate_mib=2.0, tags=(("role", "tomcat"),), netapp=True
    ),
    _Spec(
        "app-02",
        "m1.large",
        (60,),
        "almalinux 8",
        rate_mib=2.0,
        tags=(("role", "tomcat"),),
        netapp=True,
    ),
    _Spec("mq-01", "m1.medium", (50,), "ubuntu 22.04", rate_mib=4.0, tags=(("role", "rabbitmq"),)),
    _Spec("mq-02", "m1.medium", (80,), "debian 11", rate_mib=6.0, tags=(("role", "kafka"),)),
    _Spec("cache-01", "m1.medium", (30,), "rhel9", rate_mib=3.0, tags=(("role", "redis"),)),
    _Spec("dns-01", "m1.small", (20,), "ubuntu 20.04", rate_mib=0.2),
    _Spec(
        "win-ad-01",
        "m1.large",
        (60,),
        "windows2019",
        rate_mib=1.0,
        tags=(("role", "domain-controller"),),
    ),
    _Spec("monitor-01", "m1.large", (100,), "rhel9", rate_mib=3.0, tags=(("role", "prometheus"),)),
    _Spec("bastion-01", "m1.small", (20,), "centos 7", rate_mib=0.1),
    _Spec(
        "ledger-db-01",
        "m1.xlarge",
        (100, 600),
        "rhel8",
        network="finance-db",
        rate_mib=8.0,
        tags=(("app", "ledger"), ("role", "postgresql")),
        # its keepalived address, which a standby takes over (plan task N2b)
        address_pairs=("10.20.2.200",),
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
        # the demo's application failure (ADV-18): its Oracle listener never starts on RHOSO
        tags=(("app", "billing"), ("role", "oracle"), ("demo-fault", "app-down")),
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
        # one vGPU of the A100-4C type, which its trait names; the finance plan maps the flavor
        # to the destination's g1.a100-4c (SDD §22.10)
        extra_specs=(("resources:VGPU", "1"), ("trait:CUSTOM_NVIDIA_A100_4C", "required")),
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
    tags: tuple[tuple[str, str], ...] = ()
    power_state: str = "running"
    gpus: tuple[GpuDevice, ...] = ()


# 16 VMware VMs of the "DC2" datacenter with mixed CBT/snapshot/independent-disk states.
_VMWARE: tuple[_VSpec, ...] = (
    _VSpec("dc2-web-01", 2, 4096, (40,), "rhel9_64Guest", network="DC2-DMZ", rate_mib=0.5),
    _VSpec("dc2-web-02", 2, 4096, (40,), "rhel9_64Guest", network="DC2-DMZ", cbt=False),
    _VSpec("dc2-proxy-01", 2, 4096, (30,), "Ubuntu 22.04.4 LTS", network="DC2-DMZ"),
    _VSpec("dc2-app-01", 4, 8192, (80,), "rhel8_64Guest", rate_mib=2.0),
    _VSpec("dc2-mq-01", 4, 8192, (60,), "rhel8_64Guest", snapshots=2, rate_mib=3.0),
    _VSpec("dc2-dc-01", 4, 8192, (80,), "windows2019srvNext_64Guest"),
    _VSpec("dc2-mon-01", 4, 16384, (200,), "debian12_64Guest", rate_mib=2.5),
    _VSpec("dc2-db-01", 8, 32768, (100, 400), "rhel8_64Guest", rate_mib=6.0),
    _VSpec("dc2-db-02", 8, 32768, (100, 600), "rhel8_64Guest", cbt=False, rate_mib=5.0),
    _VSpec("dc2-file-01", 4, 8192, (60, 500), "windows8Server64Guest", independent=True),
    _VSpec("dc2-legacy-01", 2, 4096, (40,), "windows2008_64Guest", tools_ok=False, snapshots=1),
    _VSpec("dc2-ci-01", 4, 8192, (120,), "rockylinux_64Guest", cbt=False, rate_mib=4.0),
    # no seeded plan holds it: planned warm, its first passes meet what the cutover will refuse
    # (SDD §7.4)
    _VSpec(
        "dc2-portal-02",
        2,
        4096,
        (40,),
        "rhel9_64Guest",
        tags=(("app", "portal"), ("demo-fault", "cutover-refusal")),
    ),
    # the GPU VMs (SDD §22.10), last so that the VMs above keep their addresses and sizes: the
    # VMware plan maps dc2-ml-01's vGPU; no seeded plan holds the MIG-backed vGPU of dc2-ml-02 or
    # the DirectPath I/O GPU of dc2-cuda-01, powered off, which only vmware_cold takes (§22.6)
    _VSpec(
        "dc2-ml-01",
        8,
        32768,
        (120,),
        "rhel9_64Guest",
        rate_mib=2.0,
        tags=(("app", "ml-training"),),
        gpus=(GpuDevice(kind="vgpu", profile="grid_a100-4c"),),
    ),
    _VSpec(
        "dc2-ml-02",
        16,
        65536,
        (200,),
        "rhel9_64Guest",
        rate_mib=3.0,
        tags=(("app", "ml-inference"),),
        gpus=(GpuDevice(kind="vgpu", profile="grid_a100-3-20c"),),
    ),
    _VSpec(
        "dc2-cuda-01",
        16,
        131072,
        (250,),
        "rhel9_64Guest",
        tags=(("app", "cuda-batch"),),
        power_state="stopped",
        gpus=(GpuDevice(kind="passthrough", vendor_id="10de", device_id="20b5"),),
    ),
)


def _stable_id(prefix: str, name: str) -> str:
    return f"{prefix}-{hashlib.sha1(name.encode()).hexdigest()[:12]}"


def _mac(rng: random.Random) -> str:
    return "fa:16:3e:" + ":".join(f"{rng.randrange(256):02x}" for _ in range(3))


def _openstack_address(name: str) -> str:
    """A demo OpenStack VM's fixed IP, as _openstack_vm gives it (SDD §10)."""
    index, spec = next((i, s) for i, s in enumerate(_OPENSTACK) if s.name == name)
    return f"10.20.{1 if spec.network == 'finance-app' else 2}.{10 + index}"


def _pool_facts(name: str, balancer: str) -> dict[str, Any]:
    """A demo Octavia pool as lb_pool reads it (SDD §8.5, §10): HTTP behind an amphora."""
    return {
        "name": name,
        "protocol": "HTTP",
        "lb_algorithm": "ROUND_ROBIN",
        "persistence": None,
        "monitor": "HTTP",
        "listeners": ["HTTP"],
        "provider": "amphora",
        "load_balancer": balancer,
    }


class _DemoOctavia:
    """A demo cloud's Octavia pools for the member helper (SDD §8.5, §10): each pool's facts and
    members by id; a change is ACTIVE at once."""

    _pools: dict[str, dict[str, Any]]

    async def lb_pool(self, pool_id: str) -> dict[str, Any] | None:
        pool = self._pools.get(pool_id)
        if pool is None:
            return None
        facts = {k: v for k, v in pool["facts"].items() if k != "load_balancer"}
        return {**facts, "id": pool_id}

    async def pool_members(self, pool_id: str) -> list[dict[str, Any]]:
        pool = self._pools.get(pool_id)
        return [] if pool is None else [{**m, "id": i} for i, m in pool["members"].items()]

    async def set_pool_member(self, pool_id: str, member_id: str, admin_state_up: bool) -> None:
        member = self._pools.get(pool_id, {}).get("members", {}).get(member_id)
        if member is None:
            raise ProviderError(f"Octavia holds no member {member_id} in pool {pool_id}")
        member["admin_state_up"] = admin_state_up

    async def add_pool_member(self, pool_id: str, address: str, protocol_port: int) -> str:
        pool = self._pools.get(pool_id)
        if pool is None:
            raise ProviderError(f"Octavia holds no pool {pool_id}")
        member_id = _stable_id("member", f"{pool_id}-{address}-{protocol_port}")
        pool["members"][member_id] = _member(address, protocol_port)
        return member_id

    async def delete_pool_member(self, pool_id: str, member_id: str) -> None:
        self._pools.get(pool_id, {}).get("members", {}).pop(member_id, None)


def _member(address: str, port: int) -> dict[str, Any]:
    return {
        "address": address,
        "protocol_port": port,
        "admin_state_up": True,
        "provisioning_status": "ACTIVE",
    }


def _openstack_vm(spec: _Spec, rng: random.Random, index: int) -> VMRef:
    vcpus, ram, flavor_disk = _FLAVORS[spec.flavor]
    disks: list[Disk] = []
    for i, size in enumerate(spec.disks_gb):
        root = i == 0
        used = round(size * rng.uniform(0.25, 0.85), 1) if index % 5 != 4 else None
        kind = "image_root" if root and spec.image_root else "volume"
        vtype = None
        if kind == "volume":
            vtype = "netapp-nfs" if spec.netapp else ("ceph-ssd" if root else "ceph-hdd")
        disks.append(
            Disk(
                id=_stable_id("vol", f"{spec.name}-{i}"),
                name=f"{spec.name}-{'root' if root else f'data{i}'}",
                size_gb=size,
                used_gb=used,
                bootable=root,
                volume_type=vtype,
                pool=_SOURCE_POOLS[vtype][0] if vtype else None,
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
                allowed_address_pairs=list(spec.address_pairs),
                floating_ips=list(spec.floating_ips),
                dns_name=spec.dns_name,
                dns_domain=DEMO_DNS_DOMAIN if spec.dns_name else None,
            )
        ],
        power_state=spec.power_state,  # type: ignore[arg-type]
        os_type=spec.os_type,
        host=f"compute-{index % 4}",
        tags=dict(spec.tags),
        flavor_extra_specs=dict(spec.extra_specs),
        gpus=gpu_requests(dict(spec.extra_specs)),  # what its flavor asks for (SDD §22.2)
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
                # a standard port group's NIC: the kit maps it by its summary, the name (SDD §7.2)
                network_key=spec.network,
            )
        ],
        power_state=spec.power_state,  # type: ignore[arg-type]
        os_type=spec.guest_id,
        host=f"esx-{index % 3 + 1:02d}.dc2",
        cbt_enabled=spec.cbt,
        snapshot_count=spec.snapshots,
        tools_ok=spec.tools_ok,
        change_rate_bps=round(spec.rate_mib * rng.uniform(0.8, 1.2) * MIB, 1),
        tags=dict(spec.tags),
        # its VirtualPCIPassthrough devices, as vCenter reports them (SDD §22.2)
        gpus=[gpu.model_copy() for gpu in spec.gpus],
    )


class _FloatingIPs:
    """A fake's project's floating IPs as the keep-address move moves them (SDD §8.3, §10)."""

    _floating: dict[str, str | None]

    async def floating_ip_port(self, address: str) -> str | None:
        return self._floating.get(address)

    async def disassociate_floating_ip(self, address: str) -> str | None:
        port = self._floating.get(address)
        if address in self._floating:
            self._floating[address] = None
        return port

    async def associate_floating_ip(self, address: str, port_id: str) -> None:
        if address not in self._floating:
            raise ProviderError(f"the project holds no floating IP {address}")
        self._floating[address] = port_id

    async def release_floating_ip(self, address: str) -> None:
        self._floating.pop(address, None)


class _NoKeypairs:
    """A fake's credential's keypairs (SDD §10): the demo's hold none - its simulated executor
    deploys no conversion host (§7.4), so no conversion key is ever resolved (§7.2)."""

    async def keypair_public_key(self, name: str) -> str | None:
        return None


class FakeSourceProvider(_FloatingIPs, _DemoOctavia, _NoKeypairs):
    def __init__(self, kind: ProviderKind | str, seed: int = 42) -> None:
        self.kind = ProviderKind(kind)
        self.seed = seed
        rng = random.Random(f"{self.kind}:{seed}")
        if self.kind == ProviderKind.vmware:
            self._vms = [_vmware_vm(s, rng, i) for i, s in enumerate(_VMWARE)]
        else:
            self._vms = [_openstack_vm(s, rng, i) for i, s in enumerate(_OPENSTACK)]
        self._by_id = {vm.source_id: vm for vm in self._vms}
        #: power states the demo changed (SDD §8.1): a simulated cutover stops the source, a
        #: rollback starts it, the demo power control either; the registry keeps one fake each
        self._power: dict[str, str] = {}
        #: the floating IPs of its VMs' ports (SDD §8.3)
        self._floating = {
            address: f"port-{vm.source_id}-{i}"
            for vm in self._vms
            for i, nic in enumerate(vm.nics)
            for address in nic.floating_ips
        }
        #: its Octavia pools and their members, the OpenStack source's alone (SDD §10, N11)
        self._pools: dict[str, dict[str, Any]] = (
            {
                pool: {
                    "facts": _pool_facts(pool, balancer),
                    "members": {
                        _stable_id("member", f"{pool}-{name}"): _member(
                            _openstack_address(name), port
                        )
                        for name in names
                    },
                }
                for pool, (balancer, names, port) in DEMO_SOURCE_POOLS.items()
            }
            if self.kind != ProviderKind.vmware
            else {}
        )

    def adopt(self, old: FakeSourceProvider) -> None:
        """Rebuilt for a changed provider document (SDD §10): its VMs keep the power states the
        replaced fake gave them - the same states, which a call on either changes -, their
        floating IPs where the replaced fake left them, and its pools' members as they are."""
        if old.kind == self.kind:
            self._power = old._power
            self._floating = old._floating
            self._pools = old._pools

    def _as_now(self, vm: VMRef) -> VMRef:
        copy = vm.model_copy(deep=True)
        if vm.source_id in self._power:
            copy.power_state = self._power[vm.source_id]  # type: ignore[assignment]
        return copy

    async def check(self) -> dict[str, Any]:
        if self.kind == ProviderKind.vmware:
            # the real vCenter check's keys (VMwareProvider.check, SDD §10, review PV-06)
            return {
                "api_version": "8.0.2.0",
                "version": "8.0.2",
                "vm_count": len(self._vms),
                "cbt_enabled_vms": sum(1 for v in self._vms if v.cbt_enabled),
                "vms_with_snapshots": sum(1 for v in self._vms if v.snapshot_count),
                "vms_with_independent_disks": sum(
                    1 for v in self._vms if any(d.independent for d in v.disks)
                ),
                "vms_without_tools": sum(1 for v in self._vms if v.tools_ok is False),
            }
        return {
            "admin": True,
            "roles": ["admin", "member", "reader"],
            "project": dict(DEMO_SOURCE_PROJECT),
            "compute_microversion": "2.88",
            "block_storage_microversion": "3.64",  # Wallaby (RHOSP 17.1)
            "ovn": True,
            **_storage_caps(_SOURCE_POOLS.values()),
            "tls": True,
            "plain_http": [],
        }

    async def list_vms(self) -> list[VMRef]:
        return [self._as_now(vm) for vm in self._vms]

    async def get_vm(self, source_id: str) -> VMRef:
        try:
            return self._as_now(self._by_id[source_id])
        except KeyError:
            raise VMNotFound(f"VM {source_id!r} not found") from None

    async def power_on(self, source_id: str) -> None:
        await self.get_vm(source_id)
        self._power[source_id] = "running"

    async def powered_on_since(self, source_id: str, since: Any) -> bool:
        # the demo runs no VMware kit, whose cutover retry asks it (SDD §7.2)
        await self.get_vm(source_id)
        return False

    async def disk_keys(self, source_id: str) -> list[int]:
        # vCenter's keys of the VM's disks, in its device order: 2000 on (SDD §7.2, §10)
        vm = await self.get_vm(source_id)
        return [2000 + i for i in range(len(vm.disks))]

    async def power_off(self, source_id: str) -> None:
        await self.get_vm(source_id)
        self._power[source_id] = "stopped"

    async def delete_server(self, source_id: str) -> None:
        await self.get_vm(source_id)

    async def inventory(self) -> SourceInventory:
        if self.kind == ProviderKind.vmware:
            return SourceInventory(networks={"DC2-Prod": None, "DC2-DMZ": None}, projects=["DC2"])
        # its Heat stack and Octavia pools, as an OpenStack source reads them (SDD §10, N11)
        ids = {vm.name: vm.source_id for vm in self._vms}
        servers = {
            ids[name]: StackPlace(
                stack=DEMO_STACK,
                resource=resource,
                group=group,
                group_type="OS::Heat::AutoScalingGroup" if group else None,
            )
            for name, (resource, group) in DEMO_STACK_SERVERS.items()
        }
        members = [
            PoolMember(
                load_balancer=balancer,
                pool=pool,
                pool_id=pool,
                address=_openstack_address(name),
                port=port,
                network="finance-app",
            )
            for pool, (balancer, names, port) in DEMO_SOURCE_POOLS.items()
            for name in names
        ]
        return SourceInventory(
            networks={"finance-app": 1500, "finance-db": 9000},
            projects=["finance"],
            stacks=StackRead(servers=servers),
            lb_members=MemberRead(members=members),
        )


#: the demo destinations' maxima by kind (SDD §10): RHOSO 18.0's, and 2026.2 Hibiscus's for a
#: community cloud - its block storage maximum a plausible value, §21.3 has not confirmed it
_DESTINATION_MAXIMA = {
    ProviderKind.rhoso: ("2.95", "3.70"),
    ProviderKind.openstack: ("2.104", "3.71"),
}

#: the demo destinations' GPU flavors, with the sizes of the dashboard's mock (SDD §10, §22.10):
#: two vGPU types by their traits, and an A100 passed through by PCI alias
_GPU_FLAVORS: dict[str, tuple[int, int, int, dict[str, str]]] = {
    "g1.a100-4c": (
        8,
        32768,
        200,
        {"resources:VGPU": "1", "trait:CUSTOM_NVIDIA_A100_4C": "required"},
    ),
    "g1.a100-3-20c": (
        16,
        65536,
        300,
        {"resources:VGPU": "1", "trait:CUSTOM_NVIDIA_A100_3_20C": "required"},
    ),
    "p1.a100": (16, 131072, 400, {"pci_passthrough:alias": "a100:1"}),
}
#: the GPUs the demo destinations' Placement holds (SDD §10, §22.10), each one resource provider
#: on a compute node whose service is enabled: its class, its trait and its units
_GPUS: tuple[tuple[str, str, int], ...] = (
    ("VGPU", "CUSTOM_NVIDIA_A100_4C", 4),
    ("VGPU", "CUSTOM_NVIDIA_A100_3_20C", 2),
)
#: the GPU §22.10's other tenant holds on the cutover gate's first reads that ask for its trait,
#: and how many of those reads find it held
_TENANT_GPU = 1
_TENANT_READS = 2


def _counts(
    gpu: tuple[str, str, int], resource_class: str, traits: Iterable[str], forbidden: Iterable[str]
) -> bool:
    """Whether a GPU counts for a request or a pool (SDD §22.3): it holds the class and the required
    traits, and none of the forbidden ones."""
    cls, trait, _ = gpu
    return cls == resource_class and set(traits) <= {trait} and trait not in set(forbidden)


def _vgpus(flavor: str | None) -> list[GpuDevice]:
    """The vGPU requests of a demo destination flavor (SDD §22.2); none for any other."""
    specs = _GPU_FLAVORS[flavor][3] if flavor in _GPU_FLAVORS else {}
    return [d for d in gpu_requests(specs) if d.kind == "vgpu" and d.resource_class]


@dataclass
class _DestinationCloud:
    """What a fake destination's simulated cloud holds (SDD §10): a fake rebuilt for a changed
    provider document shares it with the one it replaces, as a real cloud keeps its servers."""

    #: the servers the rollbacks deleted
    deleted: set[str] = field(default_factory=set)
    #: the servers the simulated cutovers created, by id: the flavor each was created with
    #: (SDD §7.4, §10)
    servers: dict[str, str | None] = field(default_factory=dict)
    #: the GPU units each of them holds, by GPU: its Placement allocations (§22.3)
    held: dict[str, dict[int, int]] = field(default_factory=dict)
    #: the cutover gate's reads that found the other tenant's GPU held (SDD §22.10)
    tenant_reads: int = 0
    #: its project's floating IPs by address: the port each is on, None detached (SDD §8.3)
    floating: dict[str, str | None] = field(default_factory=dict)
    #: its Octavia pools: the one the finance plan maps the portal pool to, empty (SDD §10, N11)
    pools: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            DEMO_DESTINATION_POOL: {
                "facts": _pool_facts(DEMO_DESTINATION_POOL, "example-portal-lb-rhoso"),
                "members": {},
            }
        }
    )
    #: its Designate recordsets by (zone, name, type): ledger-web-01's record (SDD §10, N11)
    recordsets: dict[tuple[str, str, str], dict[str, Any]] = field(
        default_factory=lambda: {
            (
                DEMO_DNS_ZONES[0],
                f"{DEMO_DNS_VM}.{DEMO_DNS_ZONES[0]}",
                "A",
            ): {
                "zone_id": _stable_id("zone", DEMO_DNS_ZONES[0]),
                "id": _stable_id("recordset", DEMO_DNS_VM),
                "records": [_openstack_address(DEMO_DNS_VM)],
                "ttl": 300,
                "status": "ACTIVE",
            }
        }
    )


class FakeDestinationProvider(_FloatingIPs, _DemoOctavia, _NoKeypairs):
    def __init__(self, seed: int = 42, provider: Provider | None = None) -> None:
        self.seed = seed
        #: the demo provider it stands for: its kind, declared series and profile (SDD §10)
        self.provider = provider
        self._cloud = _DestinationCloud()

    def adopt(self, old: FakeDestinationProvider) -> None:
        """Rebuilt for a changed provider document - a rename, its declared profiles, a written
        credential (SDD §10): it takes over the replaced fake's cloud, its servers, their GPU
        units and the gate's reads, which a call on either fake changes from then on."""
        self._cloud = old._cloud

    @property
    def _pools(self) -> dict[str, dict[str, Any]]:  # type: ignore[override]
        return self._cloud.pools

    @property
    def deleted(self) -> set[str]:
        """The servers its rollbacks deleted (SDD §10)."""
        return self._cloud.deleted

    def _gpu_flavor_list(self) -> list[dict[str, Any]]:
        """The GPU flavors with the profiles its provider declares (SDD §22.3, §22.10)."""
        profiles = self.provider.gpu_profiles if self.provider is not None else {}
        return [
            {
                "name": name,
                "vcpus": vcpus,
                "ram_mb": ram_mb,
                "disk_gb": disk_gb,
                "gpus": gpu_requests(specs, profiles.get(name)),
            }
            for name, (vcpus, ram_mb, disk_gb, specs) in _GPU_FLAVORS.items()
        ]

    def _free(self, gpu: int) -> int:
        """The units of a GPU its servers leave free."""
        return _GPUS[gpu][2] - sum(held.get(gpu, 0) for held in self._cloud.held.values())

    def _capacity(self, requests: Iterable[GpuDevice], tenant: bool = False) -> GpuCapacity:
        """The pools of the vGPU requests, read as §22.3 counts them on the fake's GPUs less the
        units its servers hold - with ``tenant``, the other tenant's GPU full (§22.10)."""
        pools = []
        for pool in gpu_pools(requests):
            free = [
                0 if tenant and i == _TENANT_GPU else self._free(i)
                for i, gpu in enumerate(_GPUS)
                if _counts(gpu, pool.resource_class, pool.traits, pool.forbidden_traits)
            ]
            pools.append(
                GpuPool(
                    resource_class=pool.resource_class,
                    traits=list(pool.traits),
                    forbidden_traits=list(pool.forbidden_traits),
                    same_provider=pool.same_provider,
                    free=sum(free),
                    providers=sum(1 for units in free if units > 0),
                    largest_free=max(free, default=0),
                )
            )
        return GpuCapacity(status="ok", read_at=utcnow(), pools=pools)

    def _flavor_capacity(self) -> GpuCapacity:
        """The pools of every GPU flavor's requests: the inventory's and the check's (§22.3)."""
        return self._capacity(g for flavor in self._gpu_flavor_list() for g in flavor["gpus"])

    async def check(self) -> dict[str, Any]:
        kind = self.provider.kind if self.provider is not None else ProviderKind.rhoso
        compute, block_storage = _DESTINATION_MAXIMA.get(
            kind, _DESTINATION_MAXIMA[ProviderKind.rhoso]
        )
        release = self.provider.release if self.provider is not None else None
        profile = self.provider.policy_profile if self.provider is not None else None
        # an admin token (SDD §10): the demo's shared provider network takes its ports (§6.6)
        return {
            "admin": True,
            "roles": ["admin", "member", "reader"],
            "project": dict(DEMO_DESTINATION_PROJECT),
            "compute_microversion": compute,
            "block_storage_microversion": block_storage,
            "ovn": True,
            **_storage_caps(_DESTINATION_POOLS),
            "tls": True,
            "plain_http": [],
            # RHOSO 18.0 validated by its kind and legacy until D14; a community cloud by its
            # series (SDD §10.1)
            **destination_release(kind, release, profile, compute, block_storage),
            # its GPU flavors' pools, which the provider card shows (SDD §22.3, §22.11)
            "gpu_capacity": self._flavor_capacity().to_dict(),
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
        flavors += [
            {"name": name, "vcpus": v, "ram_mb": r, "disk_gb": d, "extra_specs": dict(specs)}
            for name, (v, r, d, specs) in _GPU_FLAVORS.items()
        ]
        free = {
            "cores": 2000,
            "ram_mb": 4_194_304,
            "instances": 500,
            "volumes": 1000,
            "gigabytes": 200_000,
        }
        # the credential's own networks, and a provider network another project shares (SDD §10)
        networks = [
            ("finance-app", 1500, True, "10.20.1.0/24"),
            ("finance-db", 8942, True, "10.20.2.0/24"),
            ("dc2-prod", 1500, True, "10.2.0.0/24"),
            ("dc2-dmz", 1442, True, "10.2.9.0/24"),
            ("provider-vlan100", 1500, False, "172.16.100.0/24"),
        ]
        return DestinationInventory(
            networks={name: mtu for name, mtu, _, _ in networks},
            flavors=flavors,
            volume_types=["ceph-ssd", "ceph-hdd", "netapp-nfs", "__DEFAULT__"],
            quotas={"finance": dict(free), "dc2": dict(free)},
            projects=["finance", "dc2"],
            # the demo's admin token, as its check reports it (SDD §10, RV19-01)
            roles=["admin", "member", "reader"],
            network_details=[
                {
                    "id": f"net-{name}",
                    "name": name,
                    "mtu": mtu,
                    "project_id": DEMO_DESTINATION_PROJECT["id"] if own else "project-admin",
                    "own": own,
                    "shared": not own,
                    "external": False,
                }
                for name, mtu, own, _ in networks
            ],
            # the demo VMs' addresses lie in these (SDD §10): NIC addresses validate against them
            subnets=[
                {
                    "id": f"sub-{name}-v4",
                    "name": f"{name}-v4",
                    "network": name,
                    "network_id": f"net-{name}",
                    "cidr": cidr,
                    "ipv6_address_mode": None,
                    "ipv6_ra_mode": None,
                }
                for name, _, _, cidr in networks
            ],
            # the GPU flavors with their declared profiles, and their pools (SDD §22.3, §22.10)
            gpu_flavors=self._gpu_flavor_list(),
            gpu_capacity=self._flavor_capacity(),
            # no floating IP held detached, and Neutron's DNS integration without the demo
            # source's zone (SDD §10, plan task N2b)
            floating_ips=[],
            dns=DnsRead(extension=True, zones=list(DEMO_DNS_ZONES)),
        )

    async def gpu_capacity(self, requests: list[GpuDevice]) -> GpuCapacity:
        """The pools of the requests (SDD §22.3): the cutover gate's read (§22.7), whose first two
        reads that ask for the other tenant's GPU's trait find it full, as if that tenant then
        released its vGPUs (§22.10) - the inventory, the validation's read, never."""
        trait = _GPUS[_TENANT_GPU][1]
        tenant = self._cloud.tenant_reads < _TENANT_READS and any(
            device.kind == "vgpu" and trait in device.traits for device in requests
        )
        if tenant:
            self._cloud.tenant_reads += 1
        return self._capacity(requests, tenant)

    async def gpu_flavors(self) -> list[dict[str, Any]]:
        """The GPU flavors as the inventory lists them, the declared profiles applied (SDD §10,
        §22.3): the cutover gate's read of a GPU flavor's requests (§22.7)."""
        return self._gpu_flavor_list()

    async def create_server(self, server_id: str, flavor: str | None) -> None:
        """The server a simulated cutover creates with ``flavor`` (SDD §7.4, §10): each vGPU
        request of the flavor takes its units from the first GPU of its pool that has them free.
        When one finds none, nothing is recorded and the create fails as Nova finds no valid host,
        naming the flavor and the pool, never a GPU (§22.12). Created again, it holds anew."""
        self._cloud.held.pop(server_id, None)
        held: dict[int, int] = {}
        for device in _vgpus(flavor):
            gpu = next(
                (
                    i
                    for i, candidate in enumerate(_GPUS)
                    if _counts(
                        candidate,
                        device.resource_class or "",
                        device.traits,
                        device.forbidden_traits,
                    )
                    and self._free(i) - held.get(i, 0) >= device.count
                ),
                None,
            )
            if gpu is None:
                self._cloud.servers.pop(server_id, None)
                pool = pool_name(
                    device.resource_class or "", device.traits, device.forbidden_traits
                )
                units = "1 unit" if device.count == 1 else f"{device.count} units"
                raise ProviderError(
                    f"no valid host was found for flavor {flavor!r}: no GPU of {pool} has "
                    f"{units} free"
                )
            held[gpu] = held.get(gpu, 0) + device.count
        self._cloud.deleted.discard(server_id)
        self._cloud.servers[server_id] = flavor
        self._cloud.held[server_id] = held

    async def server_allocations(self, server_id: str) -> dict[str, int] | None:
        """The GPU units the server holds, per resource class (SDD §10, §22.3): the fake's
        Placement holds GPUs alone, so None for a server that holds none."""
        units: dict[str, int] = {}
        for gpu, count in self._cloud.held.get(server_id, {}).items():
            units[_GPUS[gpu][0]] = units.get(_GPUS[gpu][0], 0) + count
        return units or None

    async def get_server(self, server_id: str) -> dict[str, Any]:
        if server_id in self._cloud.deleted:
            raise ProviderError(f"server {server_id!r} not found")
        octet = int(hashlib.sha1(server_id.encode()).hexdigest()[:2], 16) % 200 + 20
        # the flavor it was created with, as compute 2.47 embeds it (SDD §10, §22.3)
        flavor = self._cloud.servers.get(server_id)
        specs = _GPU_FLAVORS[flavor][3] if flavor in _GPU_FLAVORS else {}
        return {
            "id": server_id,
            "status": "ACTIVE",
            "ports": [{"status": "ACTIVE", "fixed_ips": [f"10.30.0.{octet}"], "floating_ips": []}],
            "flavor": {"name": flavor, "extra_specs": dict(specs)},
        }

    async def console_log(self, server_id: str, lines: int | None = 200) -> str | None:
        # no keys block: the demo pins no conversion host (SDD §10 "Host key pins")
        return (
            "[    1.902] EXT4-fs (vda1): mounted filesystem with ordered data mode.\n"
            "[  OK  ] Reached target Multi-User System.\n"
            f"Red Hat Enterprise Linux 9.4 (Plow)\n{server_id} login: "
        )

    async def probe_service(self, server_id: str, vm: VMRef, probe: Any, address: str) -> Any:
        """The simulated guest's answer to an application probe (SDD §7.4, ADV-18): it runs its
        suggested services (§9.6) and SSH, or RDP on Windows, and answers HTTP 200; a VM tagged
        ``demo-fault: app-down`` runs none of its application services."""
        # imported here: the verifier and the probe rules import the providers' module
        from ..planning.probes import suggest_probes
        from ..verification import ProbeAnswer

        listening = {3389 if vm.guest_os.family == "windows" else 22}
        if vm.tags.get("demo-fault") != "app-down":
            listening |= {p.port for p in suggest_probes(vm)}
        where = f"{address}:{probe.port}"
        if probe.port not in listening:
            return ProbeAnswer("failed", f"{where} refused the connection", False)
        if probe.kind == "tcp":
            return ProbeAnswer("passed", f"connected to {where}", True)
        if probe.expect_status not in (None, 200):
            return ProbeAnswer("failed", f"HTTP 200, expected {probe.expect_status}", True)
        return ProbeAnswer("passed", "HTTP 200", True)

    async def delete_server(self, server_id: str) -> None:
        self._cloud.deleted.add(server_id)
        # its units are free again (SDD §7.4, §10)
        self._cloud.servers.pop(server_id, None)
        self._cloud.held.pop(server_id, None)

    async def find_server(self, name: str) -> str | None:
        return _stable_id("dst", name)

    async def ports(
        self,
        *,
        network_id: str | None = None,
        mac_address: str | None = None,
        ip_address: str | None = None,
    ) -> list[dict[str, Any]]:
        # the demo runs the simulated executor: no port of another migration holds anything
        return []

    async def delete_port(self, port_id: str) -> None:
        return None

    async def volumes(self) -> list[dict[str, Any]]:
        # nor a kit volume a VMware rollback would delete (SDD §7.2)
        return []

    async def delete_volume(self, volume_id: str) -> None:
        return None

    async def detach_volume(self, server_id: str, volume_id: str) -> None:
        return None

    async def server_of_address(self, address: str) -> str | None:
        return None

    async def server_volumes(self, server_id: str) -> list[str]:
        return []

    async def set_volume_metadata(self, volume_id: str, metadata: dict[str, str]) -> None:
        return None

    @property
    def _floating(self) -> dict[str, str | None]:  # type: ignore[override]
        return self._cloud.floating

    async def reserve_floating_ip(self, address: str) -> None:
        """SDD §8.3: the demo's external network takes any address; one a port holds is refused."""
        if self._cloud.floating.get(address) is not None:
            raise ProviderError(f"floating IP {address} is held by a port of the project")
        self._cloud.floating[address] = None

    async def server_port(self, server_id: str, address: str) -> str | None:
        return f"port-{server_id}" if server_id in self._cloud.servers else None

    async def dns_recordset(self, zone: str, name: str, rdtype: str) -> dict[str, Any] | None:
        """SDD §8.4, §10: the demo's Designate holds ledger-web-01's record in internal.example.org.
        (plan task N11); another name has none - a swap finds nothing to move, a conflict."""
        found = self._cloud.recordsets.get((zone, name, rdtype))
        return None if found is None else {**found, "records": list(found["records"])}

    async def update_dns_recordset(
        self, zone_id: str, recordset_id: str, records: list[str]
    ) -> None:
        """The recordset's records replaced, ACTIVE at once (SDD §8.4, §10)."""
        for found in self._cloud.recordsets.values():
            if (found["zone_id"], found["id"]) == (zone_id, recordset_id):
                found["records"] = list(records)
                return
        raise ProviderError(f"Designate holds no recordset {recordset_id} in zone {zone_id}")
