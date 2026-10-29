"""Test doubles and a harness for orchestrator tests (fast and deterministic)."""

from __future__ import annotations

import asyncio
import time
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
from seamless_migrate.providers.base import ProviderError, VMNotFound
from seamless_migrate.store import Store


class _Keypairs:
    """The public keys of the credential's user's keypairs, by name (SDD §10): none unless a test
    gives some - the os_migrate_conv one a pre-staging resolves the conversion key with (§7.2)."""

    keypairs: dict[str, str]

    async def keypair_public_key(self, name: str) -> str | None:
        # a subclass whose __init__ sets none holds no keypair
        return (getattr(self, "keypairs", None) or {}).get(name)


class StaticSource(_Keypairs):
    def __init__(self, vms: list[VMRef]) -> None:
        self.vms = {vm.source_id: vm for vm in vms}
        self.keypairs = {}

    async def check(self) -> dict[str, Any]:
        return {"admin": True, "compute_microversion": "2.88", "ovn": True}

    async def list_vms(self) -> list[VMRef]:
        return list(self.vms.values())

    async def get_vm(self, source_id: str) -> VMRef:
        if source_id not in self.vms:
            raise VMNotFound(f"VM {source_id} not found")  # SDD §10
        return self.vms[source_id].model_copy(deep=True)

    async def inventory(self) -> SourceInventory:
        return SourceInventory(networks={"app-net": 1500}, projects=["finance"])


class StaticDestination(_Keypairs):
    def __init__(self) -> None:
        self.keypairs = {}
        self.status = "ACTIVE"
        self.console: str | None = "[  OK  ] Reached target Multi-User System.\nweb-01 login: "
        self.deleted: list[str] = []
        #: the simulated guest's answer per probed port (SDD §7.5): passed, failed or unreachable
        self.services: dict[int, str] = {}

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
            # the test VMs' addresses (10.0.0.<i>): NIC overrides validate against it (SDD §11)
            subnets=[{"name": "app-v4", "network": "app-net", "cidr": "10.0.0.0/24"}],
        )

    async def get_server(self, server_id: str) -> dict[str, Any]:
        return {
            "status": self.status,
            "ports": [{"status": "ACTIVE", "fixed_ips": ["10.0.0.5"], "floating_ips": []}],
        }

    async def console_log(self, server_id: str, lines: int = 200) -> str | None:
        return self.console

    async def probe_service(self, server_id: str, vm: Any, probe: Any, address: str) -> Any:
        from seamless_migrate.verification import ProbeAnswer

        result = self.services.get(probe.port, "failed")
        detail = {
            "passed": f"connected to {address}:{probe.port}",
            "failed": f"{address}:{probe.port} refused the connection",
            "unreachable": f"{address}:{probe.port} unreachable (TimeoutError)",
        }[result]
        return ProbeAnswer(result, detail, result == "passed")

    async def delete_server(self, server_id: str) -> None:
        self.deleted.append(server_id)

    async def find_server(self, name: str) -> str | None:
        return f"dst-{name}"

    async def gpu_flavors(self) -> list[dict[str, Any]]:
        """The GPU flavors its inventory lists (SDD §10, §22.3): none, unless a test's double lists
        some - the cutover gate reads them for a GPU VM (§22.7)."""
        return list((await self.inventory()).gpu_flavors)

    async def gpu_capacity(self, requests: list[Any]) -> Any:
        """Its inventory's GPU capacity (SDD §22.3): ok with no pool, unless a test's double has
        some."""
        return (await self.inventory()).gpu_capacity


class StaticRegistry:
    def __init__(self, source: StaticSource, destination: StaticDestination) -> None:
        self.source = source
        self.destination = destination

    def get(self, provider: Provider, project_id: str | None = None) -> Any:
        """The side's double, whatever the credential (SDD §10): a project credential the provider
        holds reaches it too - tests/credential_support.py's registry tells them apart -, one it
        does not hold is a provider error, never its own credential (§27.5)."""
        if project_id is not None and project_id not in provider.project_credentials:
            raise ProviderError(f"{provider.id}: it holds no credential for project {project_id}")
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

    async def wait_kind(self, kind: str, count: int = 1, timeout: float = 10.0) -> None:
        """Until the trail holds ``count`` events of ``kind``. A state is written before its event
        (the plan's completion: its status, then ``plan.completed``, SDD §8), so a test that counts
        events waits for them - stopping on the state alone cancels the event still to come."""
        deadline = asyncio.get_running_loop().time() + timeout
        while (seen := self.kinds().count(kind)) < count:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"{seen} {kind} events, wanted {count}")
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


#: two replicas of the control plane (SDD §25): their host names and addresses (RFC 5737)
REPLICAS = {
    "seamless-a": "192.0.2.10:8080",
    "seamless-b": "192.0.2.11:8080",
    "seamless-c": "192.0.2.12:8080",
    "seamless-d": "192.0.2.13:8080",
}
#: a lease of two seconds: its renew deadline 1.33 s, the last third its margin (SDD §8, §25.3)
SHORT_LEASE_S = 2.0
#: a try or a renewal every tenth of a second
SHORT_RETRY_S = 0.1


class Monotonic:
    """A monotonic clock a test moves forward: what the orchestrator's tick health reads
    (SDD §12)."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


def build_replica(
    tmp_path,
    url: str,
    vms: list[VMRef],
    *,
    executor: Any = None,
    settings: Settings | None = None,
    source: StaticSource | None = None,
    destination: StaticDestination | None = None,
    clock: Clock | None = None,
    hook_runner: Any = None,
    monotonic: Callable[[], float] | None = None,
) -> Harness:
    """A replica of the control plane on the database at ``url`` (SDD §25): a fenced store, a bus
    and an orchestrator of its own, over cloud doubles that two replicas may share, as they share
    one cloud. It writes nothing until its leadership takes the lease (SDD §11)."""
    settings = settings or make_settings(tmp_path)
    store = Store(url, fenced=True)
    bus = EventBus(store)
    source = source or StaticSource(vms)
    destination = destination or StaticDestination()
    executor = executor or ScriptedExecutor(settings)
    clock = clock or Clock()
    orch = Orchestrator(
        store,
        bus,
        StaticRegistry(source, destination),
        SingleRouter(executor),
        None,
        None,
        settings,
        sleep=fast_sleep,
        clock=clock,
        verify_poll_s=0.01,
        hook_runner=hook_runner,
        **({} if monotonic is None else {"monotonic": monotonic}),
    )
    return Harness(store, bus, orch, executor, source, destination, settings, clock)


def lead(h: Harness, holder: str, **kw: Any) -> Any:
    """``h``'s leadership as the replica ``holder`` (SDD §8): a lease of two seconds, a try every
    tenth of a second, unless ``kw`` says otherwise."""
    from seamless_migrate.leader import Leadership

    kw.setdefault("lease_s", SHORT_LEASE_S)
    kw.setdefault("retry_s", SHORT_RETRY_S)
    return Leadership(h.store, h.orch, holder=holder, address=REPLICAS[holder], **kw)


async def until(predicate: Callable[[], object], timeout: float = 10.0, what: str = "it") -> None:
    """Until ``predicate()`` holds, looked at every 10 ms; ``what`` names it when it never does."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"{what} did not come within {timeout:g} s")
        await asyncio.sleep(0.01)
