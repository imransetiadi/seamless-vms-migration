"""Test doubles and a harness for orchestrator tests (fast and deterministic)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import Phase, ProviderKind, ProviderRole
from seamless_migrate.domain.models import (
    ConversionHostConfig,
    Migration,
    Plan,
    Provider,
    VMRef,
    utcnow,
)
from seamless_migrate.events import EventBus
from seamless_migrate.executors.base import StepName, StepResult
from seamless_migrate.executors.simulated import SimulatedExecutor
from seamless_migrate.orchestrator import Orchestrator
from seamless_migrate.planning.preflight import DestinationInventory, SourceInventory
from seamless_migrate.providers.base import ProviderError
from seamless_migrate.store import Store


class StaticSource:
    def __init__(self, vms: list[VMRef]) -> None:
        self.vms = {vm.source_id: vm for vm in vms}

    async def check(self) -> dict[str, Any]:
        return {"admin": True, "compute_microversion": "2.88", "ovn": True}

    async def list_vms(self) -> list[VMRef]:
        return list(self.vms.values())

    async def get_vm(self, source_id: str) -> VMRef:
        if source_id not in self.vms:
            raise ProviderError(f"VM {source_id} not found")
        return self.vms[source_id].model_copy(deep=True)

    async def inventory(self) -> SourceInventory:
        return SourceInventory(networks={"app-net": 1500}, projects=["finance"])


class StaticDestination:
    def __init__(self) -> None:
        self.status = "ACTIVE"
        self.console: str | None = "[  OK  ] Reached target Multi-User System.\nweb-01 login: "
        self.deleted: list[str] = []

    async def check(self) -> dict[str, Any]:
        return {"admin": True, "compute_microversion": "2.95", "ovn": True}

    async def inventory(self) -> DestinationInventory:
        return DestinationInventory(
            networks={"app-net": 1500},
            flavors=[
                {
                    "name": "m1.small",
                    "vcpus": 64,
                    "ram_mb": 262144,
                    "disk_gb": 500,
                    "extra_specs": {},
                }
            ],
            volume_types=["ceph-ssd", "ceph-hdd"],
            quotas={},
            projects=["finance"],
        )

    async def get_server(self, server_id: str) -> dict[str, Any]:
        return {
            "status": self.status,
            "ports": [{"status": "ACTIVE", "fixed_ips": ["10.0.0.5"], "floating_ips": []}],
        }

    async def console_log(self, server_id: str, lines: int = 200) -> str | None:
        return self.console

    async def delete_server(self, server_id: str) -> None:
        self.deleted.append(server_id)

    async def find_server(self, name: str) -> str | None:
        return f"dst-{name}"


class StaticRegistry:
    def __init__(self, source: StaticSource, destination: StaticDestination) -> None:
        self.source = source
        self.destination = destination

    def get(self, provider: Provider) -> Any:
        return self.destination if provider.role == ProviderRole.destination else self.source


class SingleRouter:
    def __init__(self, executor: Any) -> None:
        self.executor = executor

    def for_strategy(self, strategy: Any) -> Any:
        return self.executor

    def prestage_executor(self, source_kind: Any) -> Any:
        return self.executor


@dataclass
class ScriptedExecutor:
    """Delegates to the simulated executor unless a hook overrides a step."""

    settings: Settings
    hooks: dict[StepName, Callable[..., Any]] = field(default_factory=dict)
    calls: list[tuple[str, StepName]] = field(default_factory=list)
    prestaged: list[str] = field(default_factory=list)
    name: str = "scripted"

    def __post_init__(self) -> None:
        self.sim = SimulatedExecutor(self.settings)

    def supports(self, strategy: Any) -> bool:
        return True

    async def prestage(self, plan: Plan, source: Provider, destination: Provider) -> None:
        self.prestaged.append(plan.id)

    async def run(self, step: StepName, ctx: Any) -> StepResult:
        self.calls.append((ctx.migration.id, StepName(step)))
        hook = self.hooks.get(StepName(step))
        if hook is not None:
            return await hook(ctx)
        return await self.sim.run(step, ctx)


class Clock:
    """Controllable wall clock (UTC) for gates and keep-warm decisions."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def __call__(self):
        return utcnow() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


async def fast_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


SRC_PROVIDER = Provider(
    id="src-osp",
    name="RHOSP 17.1",
    kind=ProviderKind.openstack,
    role=ProviderRole.source,
    endpoint="https://keystone.src.example:13000/v3",
    cloud="src",
    conversion_host=ConversionHostConfig(name="conv-src"),
)
DST_PROVIDER = Provider(
    id="dst-rhoso",
    name="RHOSO 18.0",
    kind=ProviderKind.rhoso,
    role=ProviderRole.destination,
    endpoint="https://keystone.dst.example:5000/v3",
    cloud="dst",
    conversion_host=ConversionHostConfig(name="conv-dst"),
)


def make_settings(tmp_path, **kw: Any) -> Settings:
    data: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "tick_s": 0.01,
        "demo_speed": 1e7,
        "demo_failure_rate": 0.0,
        "max_step_retries": 2,
    }
    data.update(kw)
    return Settings(**data)


@dataclass
class Harness:
    store: Store
    bus: EventBus
    orch: Orchestrator
    executor: Any
    source: StaticSource
    destination: StaticDestination
    settings: Settings
    clock: Clock

    async def migration(self, mid: str) -> Migration:
        return await asyncio.to_thread(self.store.get, "migration", mid, Migration)

    async def migrations(self, plan_id: str) -> list[Migration]:
        return await asyncio.to_thread(self.store.list, "migration", Migration, plan_id=plan_id)

    async def by_vm(self, plan_id: str, vm_id: str) -> Migration:
        for m in await self.migrations(plan_id):
            if m.vm.source_id == vm_id:
                return m
        raise KeyError(vm_id)

    async def plan(self, plan_id: str) -> Plan:
        return await asyncio.to_thread(self.store.get, "plan", plan_id, Plan)

    async def wait_phase(self, mid: str, *phases: Phase, timeout: float = 10.0) -> Migration:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            m = await self.migration(mid)
            if m.phase in phases:
                return m
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"{mid} stuck in {m.phase}, wanted {phases}: "
                    f"{[(c.from_phase, c.to_phase) for c in m.phase_history]}"
                )
            await asyncio.sleep(0.01)

    async def wait_plan(self, plan_id: str, status, timeout: float = 10.0) -> Plan:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            plan = await self.plan(plan_id)
            if plan.status == status:
                return plan
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"plan {plan_id} is {plan.status}, wanted {status}")
            await asyncio.sleep(0.01)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.store.events(since_seq=0, limit=100000)]

    def history(self, m: Migration) -> list[Phase]:
        return [c.to_phase for c in m.phase_history]


def build_harness(
    tmp_path,
    store: Store,
    vms: list[VMRef],
    *,
    executor: Any = None,
    advisor: Any = None,
    knowledge: Any = None,
    settings: Settings | None = None,
    destination: StaticDestination | None = None,
    clock: Clock | None = None,
) -> Harness:
    settings = settings or make_settings(tmp_path)
    bus = EventBus(store)
    source = StaticSource(vms)
    destination = destination or StaticDestination()
    executor = executor or ScriptedExecutor(settings)
    clock = clock or Clock()
    orch = Orchestrator(
        store,
        bus,
        StaticRegistry(source, destination),
        SingleRouter(executor),
        advisor,
        knowledge,
        settings,
        sleep=fast_sleep,
        clock=clock,
        verify_poll_s=0.01,
    )
    store.put("provider", SRC_PROVIDER)
    store.put("provider", DST_PROVIDER)
    return Harness(store, bus, orch, executor, source, destination, settings, clock)
