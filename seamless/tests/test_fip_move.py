"""The floating IP keep-address move (SDD §8.3, §9.3, §10, §23.4, plan task N6a)."""

import asyncio

from seamless_migrate.domain.enums import Phase as P
from seamless_migrate.domain.enums import PlanStatus, Severity, Strategy
from seamless_migrate.domain.models import Nic, VerificationConfig
from seamless_migrate.executors.base import StepName
from seamless_migrate.providers.base import ProviderError
from seamless_migrate.store import Store
from tests.factories import make_plan, make_vm
from tests.orch_support import StaticDestination, StaticSource, build_harness
from tests.test_orchestrator import plan_for, run_plan, vm
from tests.test_preflight import check, dst_inv

KEEP = {"floating_ips": "keep_address"}
#: the destination's external network and its subnet (SDD §9.3)
PUBLIC = {
    "id": "net-public",
    "name": "public",
    "mtu": 1500,
    "project_id": "admin-project",
    "own": False,
    "shared": False,
    "external": True,
}
PUBLIC_SUBNET = {
    "id": "sub-public",
    "name": "public-v4",
    "network": "public",
    "network_id": "net-public",
    "cidr": "203.0.113.0/24",
}
ADMIN = {"roles": ["admin", "member"], "policy_profile": "legacy"}


class FipCloud:
    """Neutron's floating IPs of one project, by address - the port each is on, None detached -,
    and each call in ``log``, which the clouds and the steps share."""

    def __init__(self, name, held=None, log=None, refuse=()):
        self.name = name
        self.fips: dict[str, str | None] = dict(held or {})
        self.log = log if log is not None else []
        self.refuse = set(refuse)

    def _call(self, what):
        self.log.append(f"{self.name}:{what}")
        if what.split(" ")[0] in self.refuse:
            raise ProviderError(f"{self.name}: Neutron refused {what}")

    async def reserve_floating_ip(self, address):
        self._call(f"reserve {address}")
        if self.fips.get(address) is not None:
            raise ProviderError(f"{address} is held by port {self.fips[address]}")
        self.fips[address] = None

    async def server_port(self, server_id, address):
        return f"port-{server_id}-{address}"

    async def floating_ip_port(self, address):
        return self.fips.get(address)

    async def disassociate_floating_ip(self, address):
        self._call(f"disassociate {address}")
        port = self.fips.get(address)
        if address in self.fips:
            self.fips[address] = None
        return port

    async def associate_floating_ip(self, address, port_id):
        self._call(f"associate {address} {port_id}")
        self.fips[address] = port_id

    async def release_floating_ip(self, address):
        self._call(f"release {address}")
        self.fips.pop(address, None)


class FipSource(StaticSource, FipCloud):
    def __init__(self, vms, log, **kw):
        StaticSource.__init__(self, vms)
        FipCloud.__init__(self, "source", held={"203.0.113.10": "src-port-1"}, log=log, **kw)


class FipDestination(StaticDestination, FipCloud):
    def __init__(self, log, **kw):
        StaticDestination.__init__(self)
        FipCloud.__init__(self, "destination", log=log, **kw)


def floating_vm(i=1):
    nic = Nic(
        network="app-net",
        mac=f"fa:16:3e:00:00:0{i}",
        mtu=1500,
        fixed_ips=[f"10.0.0.{i}"],
        floating_ips=["203.0.113.10"],
    )
    return vm(i, nics=[nic])


async def fip_setup(
    tmp_path, store, plan_kw=None, source_kw=None, destination_kw=None, destination_cls=None
):
    """A plan of one OpenStack VM with a floating IP whose network cut-over keeps the address; the
    clouds' calls and the steps in one log. ``destination_cls``: a FipDestination of the test's."""
    log: list[str] = []
    vms = [floating_vm()]
    destination = (destination_cls or FipDestination)(log, **(destination_kw or {}))
    h = build_harness(tmp_path, store, vms, destination=destination)
    source = FipSource(vms, log, **(source_kw or {}))
    h.orch.providers.source = h.source = source
    kw = {
        "default_strategy": Strategy.cold,
        "network_cutover": KEEP,
        "verification": VerificationConfig(timeout_s=2),
        **(plan_kw or {}),
    }
    plan = plan_for(vms, **kw)
    store.put("plan", plan)

    async def step(name):
        async def run(ctx):
            log.append(f"step:{name}")
            return await h.executor.sim.run(name, ctx)

        return run

    for name in (StepName.CUTOVER, StepName.ROLLBACK, StepName.FINALIZE):
        h.executor.hooks[name] = await step(name)
    return h, plan, source, destination, log


def _fip_actions(h, mid):
    return [
        (e.data["step"], e.data["outcome"])
        for e in h.store.events(since_seq=0, migration_id=mid, limit=1000)
        if e.kind == "migration.action" and e.data.get("action") == "floating_ip"
    ]


def test_the_preflight_blocks_an_address_outside_the_destinations_external_subnets():
    """SDD §9.3: under keep_address, a floating IP the destination cannot keep is a blocker naming
    each address and why - outside every subnet of its external networks, held by a port of the
    project, not choosable by the token's roles, or beyond the quota -; under auto, the move's
    blocker is not judged and MAP_FIP_ADDRESS_CHANGE warns instead. A destination whose external
    subnets the credential cannot see is not judged on them."""
    nic = Nic(
        network="app-net",
        mac="fa:16:3e:00:00:01",
        fixed_ips=["10.0.0.5"],
        floating_ips=["198.51.100.7", "203.0.113.10"],
    )
    one = make_vm(nics=[nic])
    keep = make_plan(network_cutover=KEEP)
    seen = dst_inv(network_details=[PUBLIC], subnets=[PUBLIC_SUBNET])
    found = check(one, plan=keep, dst=seen, dst_caps=ADMIN)
    [f] = [x for x in found if x.code == "DST_FIP_ADDRESS_UNAVAILABLE"]
    assert (f.severity, f.strategies) == (Severity.blocker, [])
    assert f.message == (
        "Floating IPs of web-01 that RHOSO 18.0 cannot keep: 198.51.100.7 lies in no subnet of "
        "an external network of RHOSO 18.0."
    )
    assert "MAP_FIP_ADDRESS_CHANGE" not in [x.code for x in found]
    # under auto nothing is moved: the warning of a new address instead
    auto = check(one, dst=seen, dst_caps=ADMIN)
    assert "DST_FIP_ADDRESS_UNAVAILABLE" not in [x.code for x in auto]
    assert "MAP_FIP_ADDRESS_CHANGE" in [x.code for x in auto]
    # every reason, each address once, in the NIC's order
    member = {"roles": ["member"], "policy_profile": "legacy"}
    crowded = dst_inv(
        network_details=[PUBLIC],
        subnets=[PUBLIC_SUBNET],
        attached_floating_ips=["203.0.113.10"],
        floating_ip_quota=0,
    )
    [f] = [
        x
        for x in check(one, plan=keep, dst=crowded, dst_caps=member)
        if x.code == "DST_FIP_ADDRESS_UNAVAILABLE"
    ]
    assert f.message == (
        "Floating IPs of web-01 that RHOSO 18.0 cannot keep: 198.51.100.7 lies in no subnet of "
        "an external network of RHOSO 18.0; 203.0.113.10 is held by a port of its project. The "
        "credential's token has no role that may choose a floating IP's address (admin), and the "
        "project's floating IP quota leaves 0 for 2 addresses."
    )
    # a manager may choose one under secure RBAC; an address held detached needs no choice
    manager = {"roles": ["manager", "member"], "policy_profile": "secure_rbac"}
    held = dst_inv(network_details=[PUBLIC], subnets=[PUBLIC_SUBNET], floating_ips=["203.0.113.10"])
    only_inside = make_vm(nics=[nic.model_copy(update={"floating_ips": ["203.0.113.10"]})])
    assert "DST_FIP_ADDRESS_UNAVAILABLE" not in [
        x.code for x in check(only_inside, plan=keep, dst=seen, dst_caps=manager)
    ]
    assert "DST_FIP_ADDRESS_UNAVAILABLE" not in [
        x.code for x in check(only_inside, plan=keep, dst=held, dst_caps=member)
    ]
    # no external subnet seen, no roles reported, no quota read: nothing judged
    assert "DST_FIP_ADDRESS_UNAVAILABLE" not in [
        x.code for x in check(one, plan=keep, dst=dst_inv(), dst_caps=None)
    ]


async def _until(condition, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


async def test_prestaging_reserves_the_address_detached(tmp_path, store):
    """SDD §8.3: the plan's pre-staging reserves each floating IP's address at the destination,
    detached, before any source stops - the source's stays where it is; a reservation Neutron
    refuses fails the pre-staging, and no source is stopped."""
    h, plan, source, destination, log = await fip_setup(
        tmp_path, store, plan_kw={"auto_cutover": False}
    )
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await _until(lambda: ("reserve", "succeeded") in _fip_actions(h, mid))
    await h.orch.stop()
    assert destination.fips == {"203.0.113.10": None}
    assert source.fips == {"203.0.113.10": "src-port-1"}
    assert log == ["destination:reserve 203.0.113.10"]

    refused_store = Store(f"sqlite:///{tmp_path / 'refused.db'}")
    refused_store.create_schema()
    try:
        h, plan, source, destination, log = await fip_setup(
            tmp_path / "refused", refused_store, destination_kw={"refuse": {"reserve"}}
        )
        await run_plan(h, plan)
        await h.wait_plan(plan.id, PlanStatus.failed)
        await h.orch.stop()
        mid = (await h.by_vm(plan.id, "vm-1")).id
        assert log == ["destination:reserve 203.0.113.10"]
        assert source.fips == {"203.0.113.10": "src-port-1"}
        assert (await h.migration(mid)).downtime_started_at is None
        assert _fip_actions(h, mid) == [("reserve", "failed")]
    finally:
        refused_store.dispose()


async def test_the_cutover_moves_the_address_after_the_stop(tmp_path, store):
    """SDD §8.3: in verifying, after the cutover step and before the post_create hooks, the
    source's floating IP is disassociated - its port saved in the journal - and the destination's
    associated with the new server's port holding the NIC's address; each step is recorded."""
    h, plan, source, destination, log = await fip_setup(tmp_path, store)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    m = await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    server = m.destination_server_id
    assert log == [
        "destination:reserve 203.0.113.10",
        "step:cutover",
        "source:disassociate 203.0.113.10",
        "destination:reserve 203.0.113.10",
        f"destination:associate 203.0.113.10 port-{server}-10.0.0.1",
    ]
    assert source.fips == {"203.0.113.10": None}
    assert destination.fips == {"203.0.113.10": f"port-{server}-10.0.0.1"}
    [move] = m.fip_moves
    assert (move.address, move.fixed_ip, move.source_port, move.state) == (
        "203.0.113.10",
        "10.0.0.1",
        "src-port-1",
        "moved",
    )
    assert _fip_actions(h, mid) == [("reserve", "succeeded"), ("move", "succeeded")]


async def test_a_rollback_gives_the_address_back_to_the_source_before_it_starts(tmp_path, store):
    """SDD §8.3: a rollback, before its step starts the source, releases the destination's floating
    IP and associates the source's with the port it was on again; a failure there fails the
    rollback - the source is not started without its address - and a rollback asked again resumes
    from the journal."""
    h, plan, source, destination, log = await fip_setup(tmp_path, store)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await h.wait_phase(mid, P.completed)
    log.clear()
    destination.refuse = {"release"}
    await h.orch.rollback(mid, "alice", "the application team asked for it")
    failed = await h.wait_phase(mid, P.failed)
    assert "step:rollback" not in log
    assert source.fips == {"203.0.113.10": None}
    assert failed.fip_moves[0].state == "moved"
    destination.refuse = set()
    log.clear()
    await h.orch.rollback(mid, "alice", "once more")
    back = await h.wait_phase(mid, P.rolled_back)
    await h.orch.stop()
    assert log == [
        "destination:disassociate 203.0.113.10",
        "destination:release 203.0.113.10",
        "source:associate 203.0.113.10 src-port-1",
        "step:rollback",
    ]
    assert source.fips == {"203.0.113.10": "src-port-1"} and destination.fips == {}
    assert back.fip_moves[0].state == "returned"


async def test_finalize_releases_the_sources_allocation(tmp_path, store):
    """SDD §8.3: a finalize, after its step, deletes the source project's floating IP of each
    moved address - the address lives at the destination now."""
    h, plan, source, destination, log = await fip_setup(tmp_path, store)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    m = await h.wait_phase(mid, P.completed)
    log.clear()
    done = await h.orch.finalize(mid, "sari", confirm=m.vm.name)
    await h.orch.stop()
    assert done.phase == P.finalized
    assert log == ["step:finalize", "source:release 203.0.113.10"]
    assert source.fips == {} and destination.fips != {}
    assert done.fip_moves[0].state == "finalized"
    assert ("release", "succeeded") in _fip_actions(h, mid)


async def test_a_handover_moves_the_address_too(tmp_path, store):
    """SDD §8.3: the move is the control plane's, so a storage handover keeps the address as a cold
    or warm migration does."""
    h, plan, source, destination, log = await fip_setup(tmp_path, store)
    await h.orch.validate_plan(plan.id, "alice")
    m = await h.by_vm(plan.id, "vm-1")
    store.put("migration", m.model_copy(update={"strategy": Strategy.storage_handover}))
    await h.orch.start_plan(plan.id, "alice")
    await h.orch.start()
    done = await h.wait_phase(m.id, P.completed)
    await h.orch.stop()
    assert done.strategy == Strategy.storage_handover
    assert done.fip_moves[0].state == "moved"
    assert destination.fips == {"203.0.113.10": f"port-{done.destination_server_id}-10.0.0.1"}
