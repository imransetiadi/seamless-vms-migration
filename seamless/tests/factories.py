"""Small builders shared by the test-suite (plain functions, no fixtures)."""

from __future__ import annotations

from typing import Any

from seamless_migrate.domain.enums import Phase, ProviderKind, ProviderRole, Strategy
from seamless_migrate.domain.models import (
    ConversionHostConfig,
    Disk,
    Migration,
    Nic,
    Plan,
    Provider,
    VMRef,
)

GIB = 2**30


def make_disk(**kw: Any) -> Disk:
    data: dict[str, Any] = {
        "id": "vol-1",
        "size_gb": 20,
        "used_gb": 10.0,
        "bootable": True,
        "volume_type": "ceph-ssd",
        "device": "/dev/vda",
    }
    data.update(kw)
    return Disk(**data)


def make_vm(**kw: Any) -> VMRef:
    data: dict[str, Any] = {
        "source_id": "vm-1",
        "name": "web-01",
        "project": "finance",
        "flavor": "m1.small",
        "vcpus": 2,
        "ram_mb": 4096,
        "disks": [make_disk()],
        "nics": [Nic(network="app-net", mac="fa:16:3e:00:00:01", fixed_ips=["10.0.0.5"], mtu=1500)],
        "power_state": "running",
        "os_type": "rhel9",
    }
    data.update(kw)
    return VMRef(**data)


def make_provider(**kw: Any) -> Provider:
    data: dict[str, Any] = {
        "id": "src-osp",
        "name": "RHOSP 17.1",
        "kind": ProviderKind.openstack,
        "role": ProviderRole.source,
        "endpoint": "https://keystone.src.example:13000/v3",
        "cloud": "src",
        "conversion_host": ConversionHostConfig(name="conv-src", flavor="m1.large"),
    }
    data.update(kw)
    return Provider(**data)


def make_plan(**kw: Any) -> Plan:
    data: dict[str, Any] = {
        "name": "Finance",
        "source_provider_id": "src-osp",
        "destination_provider_id": "dst-rhoso",
        "vm_ids": ["vm-1"],
    }
    data.update(kw)
    return Plan(**data)


def make_migration(**kw: Any) -> Migration:
    data: dict[str, Any] = {
        "plan_id": "plan-0000abcd",
        "vm": make_vm(),
        "strategy": Strategy.warm,
        "phase": Phase.pending,
    }
    data.update(kw)
    return Migration(**data)
