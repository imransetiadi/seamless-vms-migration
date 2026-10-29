"""The Octavia member helper (SDD §8.5, §9.3, §10, §23.7, plan task N9)."""

import logging
import urllib.parse

import pytest

from seamless_migrate.domain.enums import Phase as P
from seamless_migrate.domain.enums import Severity, Strategy
from seamless_migrate.domain.models import LbMove, Nic, VerificationConfig
from seamless_migrate.executors.base import StepName
from seamless_migrate.lb_helper import LbHelperError, Pace
from seamless_migrate.planning.preflight import MemberRead, PoolMember, PoolPair, SourceInventory
from seamless_migrate.providers.base import ProviderConflict, ProviderError
from tests.factories import make_migration, make_plan, make_vm
from tests.orch_support import StaticDestination, StaticSource, build_harness
from tests.test_orchestrator import _RecordingKnowledge, plan_for, run_plan, vm
from tests.test_preflight import check, only, src_inv

MAC = "fa:16:3e:00:00:01"
#: the source's web pool - an HTTP listener, ROUND_ROBIN, an HTTP monitor - and its other member
WEB_POOL = {
    "name": "web-pool",
    "protocol": "HTTP",
    "lb_algorithm": "ROUND_ROBIN",
    "persistence": None,
    "monitor": "HTTP",
    "listeners": ["HTTP"],
    "provider": "amphora",
}
L4_POOL = {
    "name": "web-l4",
    "protocol": "TCP",
    "lb_algorithm": "SOURCE_IP_PORT",
    "persistence": None,
    "monitor": "TCP",
    "listeners": ["TCP"],
    "provider": "ovn",
}


def member(address, port=80, up=True, status="ACTIVE"):
    return {
        "address": address,
        "protocol_port": port,
        "admin_state_up": up,
        "provisioning_status": status,
    }


class Octavia:
    """One cloud's Octavia pools as the helper reads and changes them (SDD §10): each pool's facts
    and members by id; every change in ``log``, which both clouds and the steps share."""

    def __init__(self, name, pools, log, fail=()):
        self.name = name
        self.pools = pools
        self.log = log
        self.fail = set(fail)
        self.created = 0

    def _call(self, what):
        self.log.append(f"{self.name}:{what}")
        if what.split(" ")[0] in self.fail:
            raise ProviderError(f"{self.name}: Octavia refused {what.split(' ')[0]}: HTTP 403")

    async def lb_pool(self, pool_id):
        pool = self.pools.get(pool_id)
        return None if pool is None else {**pool["facts"], "id": pool_id}

    async def pool_members(self, pool_id):
        pool = self.pools.get(pool_id)
        return [] if pool is None else [{**m, "id": i} for i, m in pool["members"].items()]

    async def set_pool_member(self, pool_id, member_id, admin_state_up):
        self._call(f"set {pool_id} {member_id} {'up' if admin_state_up else 'down'}")
        self.pools[pool_id]["members"][member_id]["admin_state_up"] = admin_state_up

    async def add_pool_member(self, pool_id, address, protocol_port):
        self._call(f"add {pool_id} {address}:{protocol_port}")
        self.created += 1
        member_id = f"dst-member-{self.created}"
        self.pools[pool_id]["members"][member_id] = member(address, protocol_port)
        return member_id

    async def delete_pool_member(self, pool_id, member_id):
        self._call(f"delete {pool_id} {member_id}")
        self.pools[pool_id]["members"].pop(member_id, None)


class LbSource(StaticSource, Octavia):
    """The source: web-pool holds web-01 (10.0.0.10) and another VM's member (10.0.0.99)."""

    def __init__(self, vms, log, facts=None, **kw):
        StaticSource.__init__(self, vms)
        pools = {
            "src-pool": {
                "facts": facts or WEB_POOL,
                "members": {"m-1": member("10.0.0.10"), "m-2": member("10.0.0.99")},
            }
        }
        Octavia.__init__(self, "source", pools, log, **kw)

    async def inventory(self):
        members = [
            PoolMember(
                load_balancer="web-lb",
                pool="web-pool",
                pool_id="src-pool",
                address="10.0.0.10",
                port=80,
                network="app-net",
            )
        ]
        return SourceInventory(
            networks={"app-net": 1500},
            projects=["finance"],
            lb_members=MemberRead(members=members),
        )


class LbDestination(StaticDestination, Octavia):
    """The destination: dst-pool, empty, on a load balancer of ``provider``."""

    def __init__(self, log, provider="amphora", **kw):
        StaticDestination.__init__(self)
        facts = {**WEB_POOL, "name": "web-pool-dst", "provider": provider}
        Octavia.__init__(
            self, "destination", {"dst-pool": {"facts": facts, "members": {}}}, log, **kw
        )

    async def get_server(self, server_id):
        self.log.append("destination:verify")
        return await StaticDestination.get_server(self, server_id)


def web_vm():
    """web-01 at 10.0.0.10."""
    return vm(1, nics=[Nic(network="app-net", mac=MAC, mtu=1500, fixed_ips=["10.0.0.10"])])


async def lb_setup(tmp_path, store, *, plan_kw=None, source_kw=None, destination_kw=None, **kw):
    """A cold plan of web-01 whose network cut-over maps src-pool to dst-pool; the clouds' calls
    and the steps in one log."""
    log: list[str] = []
    vms = [web_vm()]
    destination = LbDestination(log, **(destination_kw or {}))
    h = build_harness(tmp_path, store, vms, destination=destination, **kw)
    source = LbSource(vms, log, **(source_kw or {}))
    h.orch.providers.source = h.source = source
    data = {
        "default_strategy": Strategy.cold,
        "network_cutover": {"lb_pools": {"src-pool": "dst-pool"}},
        "verification": VerificationConfig(timeout_s=2),
        **(plan_kw or {}),
    }
    plan = plan_for(vms, **data)
    store.put("plan", plan)

    def step(name):
        async def run(ctx):
            log.append(f"step:{name}")
            return await h.executor.sim.run(name, ctx)

        return run

    for name in (StepName.CUTOVER, StepName.ROLLBACK):
        h.executor.hooks[name] = step(name)
    return h, plan, source, destination, log


def lb_actions(h, mid):
    return [
        (e.data["step"], e.data["outcome"])
        for e in h.store.events(since_seq=0, migration_id=mid, limit=1000)
        if e.kind == "migration.action" and e.data.get("action") == "lb_member"
    ]


async def test_the_source_member_is_drained_before_the_stop(tmp_path, store):
    """SDD §8.5: in cutover at pre_stop, while the source runs, the VM's member of each mapped
    source pool - its address one of the VM's fixed IPs - is journaled, then drained
    (admin_state_up false) before the cutover step stops the source; another VM's member of the
    pool is left alone."""
    h, plan, source, _, log = await lb_setup(tmp_path, store)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    m = await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    assert log.index("source:set src-pool m-1 down") < log.index("step:cutover")
    members = source.pools["src-pool"]["members"]
    assert members["m-1"]["admin_state_up"] is False
    assert members["m-2"]["admin_state_up"] is True
    [move] = m.lb_moves
    assert (move.source_pool, move.source_member, move.address, move.port, move.pool) == (
        "src-pool",
        "m-1",
        "10.0.0.10",
        80,
        "dst-pool",
    )
    assert lb_actions(h, mid)[0] == ("drain", "succeeded")


async def test_a_drain_that_fails_keeps_the_source_running(tmp_path, store):
    """SDD §8.5: a drain's failure fails the attempt at pre_stop, as a pre_stop hook's does - the
    cutover step never runs and no clock opens -; its error names the step lb_member, and
    agentmemory is not asked about it (§13.4)."""
    knowledge = _RecordingKnowledge()
    h, plan, _, _, log = await lb_setup(
        tmp_path, store, source_kw={"fail": {"set"}}, knowledge=knowledge
    )
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    m = await h.wait_phase(mid, P.failed)
    await h.wait_kind("migration.error")
    await h.orch.stop()
    assert "step:cutover" not in log and m.downtime_started_at is None
    assert m.error == "lb_member: source: Octavia refused set: HTTP 403"
    assert [move.state for move in m.lb_moves] == ["draining"]
    assert lb_actions(h, mid) == [("drain", "failed")]
    assert knowledge.failures == []


async def test_the_vm_joins_the_destination_pool_after_the_verification(tmp_path, store):
    """SDD §8.5: in verifying at post_verify, once the verification passed, the address that takes
    the member's place - its NIC override's here - joins the destination pool on the same port;
    a verification that fails joins nothing."""
    h, plan, _, destination, log = await lb_setup(
        tmp_path,
        store,
        plan_kw={"nic_overrides": {"vm-1": {MAC: {"fixed_ips": ["10.0.0.20"]}}}},
    )
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    m = await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    added = log.index("destination:add dst-pool 10.0.0.20:80")
    assert added > max(i for i, line in enumerate(log) if line == "destination:verify")
    assert destination.pools["dst-pool"]["members"] == {"dst-member-1": member("10.0.0.20")}
    [move] = m.lb_moves
    assert (move.member, move.state) == ("dst-member-1", "joined")
    assert lb_actions(h, mid) == [("drain", "succeeded"), ("join", "succeeded")]


async def test_a_failed_verification_joins_nothing(tmp_path, store):
    """SDD §8.5: the join waits for a passed verification: a failed one rolls the VM back, and the
    destination pool never gets the member."""
    h, plan, source, destination, log = await lb_setup(tmp_path, store)
    h.destination.status = "ERROR"
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    m = await h.wait_phase(mid, P.rolled_back)
    await h.orch.stop()
    assert not [line for line in log if line.startswith("destination:add")]
    assert destination.pools["dst-pool"]["members"] == {}
    assert source.pools["src-pool"]["members"]["m-1"]["admin_state_up"] is True
    assert [move.state for move in m.lb_moves] == ["restored"]


async def test_a_rollback_removes_the_member_and_re_enables_the_source(tmp_path, store):
    """SDD §8.5: a rollback - in rolling_back, before its step starts the source - deletes the
    destination member and re-enables the source member; the entry is restored."""
    h, plan, source, destination, log = await lb_setup(tmp_path, store)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await h.wait_phase(mid, P.completed)
    await h.orch.rollback(mid, "alice", "the application team asked for it")
    m = await h.wait_phase(mid, P.rolled_back)
    await h.orch.stop()
    step = log.index("step:rollback")
    assert log.index("destination:delete dst-pool dst-member-1") < step
    assert log.index("source:set src-pool m-1 up") < step
    assert destination.pools["dst-pool"]["members"] == {}
    assert source.pools["src-pool"]["members"]["m-1"]["admin_state_up"] is True
    assert [move.state for move in m.lb_moves] == ["restored"]
    assert lb_actions(h, mid)[-1] == ("restore", "succeeded")


#: the member helper's calls of a cloud (SDD §8.5, §10)
LB_CALLS = {"lb_pool", "pool_members", "set_pool_member", "add_pool_member", "delete_pool_member"}


async def test_the_member_helper_acts_with_each_sides_project_credential(tmp_path, store):
    """SDD §8.5, §27.5 (plan task TC5): a plan with projects reads, drains and re-enables its
    source pool's member with the plan's source credential, and reads its destination pool, joins
    it and removes its member with the plan's destination credential - validation's reads of the
    pair too -; no call of the helper is made with a provider's own credential."""
    from tests.credential_support import of_project, own, with_projects
    from tests.orch_support import DST_PROVIDER, SRC_PROVIDER

    h, plan, source, destination, log = await lb_setup(tmp_path, store)
    # a plan that maps Octavia pools needs load-balancer_member in each side's project credential
    # (SDD §9.3 PROJECT_CREDENTIAL_ROLE_MISSING, §27.5, plan task TC6)
    registry, t = with_projects(h, plan, roles=("load-balancer_member", "member", "reader"))
    await run_plan(h, t.plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await h.wait_phase(mid, P.completed)
    await h.orch.rollback(mid, "alice", "the application team asked for it")
    m = await h.wait_phase(mid, P.rolled_back)
    await h.orch.stop()
    assert [move.state for move in m.lb_moves] == ["restored"]
    src = of_project(SRC_PROVIDER, t.source)
    dst = of_project(DST_PROVIDER, t.destination)
    assert {"lb_pool", "pool_members", "set_pool_member"} <= registry.methods(src)
    assert {"lb_pool", "add_pool_member", "delete_pool_member"} <= registry.methods(dst)
    assert set(registry.used("set_pool_member")) == {src}
    assert not LB_CALLS & (
        registry.methods(own(SRC_PROVIDER)) | registry.methods(own(DST_PROVIDER))
    )


async def test_a_restore_that_fails_is_recorded_and_the_rollback_goes_on(tmp_path, store):
    """SDD §8.5: the source must run again: a restore's failure is recorded, the entry keeps its
    state for the operator, and the rollback's step runs."""
    h, plan, _, _, log = await lb_setup(tmp_path, store, destination_kw={"fail": {"delete"}})
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    await h.wait_phase(mid, P.completed)
    await h.orch.rollback(mid, "alice", "the application team asked for it")
    m = await h.wait_phase(mid, P.rolled_back)
    await h.orch.stop()
    assert "step:rollback" in log
    assert [move.state for move in m.lb_moves] == ["joined"]
    assert lb_actions(h, mid)[-1] == ("restore", "failed")


async def test_a_restart_resumes_the_helper_from_its_journal(tmp_path, store):
    """SDD §8.5: a step resumes from the journal - a drained member is not drained again, and a
    join a restart interrupted after Octavia created the member takes that member."""
    h, plan, source, destination, log = await lb_setup(tmp_path, store)
    source.pools["src-pool"]["members"]["m-1"]["admin_state_up"] = False
    destination.pools["dst-pool"]["members"]["dst-member-7"] = member("10.0.0.10")
    moves = [
        LbMove(
            source_pool="src-pool",
            source_member="m-1",
            address="10.0.0.10",
            port=80,
            pool="dst-pool",
            member=None,
            state="joining",
        )
    ]
    m = make_migration(
        plan_id=plan.id,
        vm=web_vm(),
        phase=P.verifying,
        destination_server_id="srv-1",
        lb_moves=moves,
    )
    store.put("migration", m)
    assert await h.orch._drain_lb_members(m) is None
    assert await h.orch._join_lb_members(m) is None
    assert log == []  # nothing drained again, nothing created twice
    [move] = (await h.migration(m.id)).lb_moves
    assert (move.member, move.state) == ("dst-member-7", "joined")


def pair(source=None, destination=None):
    return PoolPair(
        source={**WEB_POOL, "id": "src-pool", **(source or {})},
        destination={**L4_POOL, "id": "dst-pool", **(destination or {})},
    )


def lb_vm():
    return make_vm(nics=[Nic(network="app-net", mac=MAC, fixed_ips=["10.0.0.10"])])


def lb_src():
    members = [
        PoolMember(
            load_balancer="web-lb",
            pool="web-pool",
            pool_id="src-pool",
            address="10.0.0.10",
            port=80,
            network="app-net",
        )
    ]
    return src_inv(lb_members=MemberRead(members=members))


def test_an_ovn_pool_refuses_a_listener_it_cannot_serve():
    """SDD §9.3, §23.7: the OVN provider is L4 only - TCP, UDP and SCTP with SOURCE_IP_PORT and
    TCP, UDP-CONNECT or SCTP monitors, no session persistence -, so a member of a source pool the
    plan maps to an OVN pool is warned of each thing the source pool uses that OVN lacks; an L4
    pool, another provider, or a pair a read could not see is not judged."""
    plan = make_plan(network_cutover={"lb_pools": {"src-pool": "dst-pool"}})
    tls = {"protocol": "HTTP", "listeners": ["TERMINATED_HTTPS", "HTTP"]}
    sticky = {**tls, "persistence": "HTTP_COOKIE"}
    findings = check(lb_vm(), plan=plan, src=lb_src(), lb_pools={"src-pool": pair(sticky)})
    f = only(findings, "MAP_LB_POOL_INCOMPATIBLE")
    assert (f.severity, f.strategies) == (Severity.warning, [])
    assert f.message == (
        "web-01 is a member of source pool 'web-pool', which the plan maps to pool 'web-l4' of "
        "provider ovn: it serves TCP, UDP and SCTP alone, so the pool loses the HTTP protocol, "
        "the ROUND_ROBIN algorithm, HTTP_COOKIE session persistence, an HTTP health monitor, a "
        "TERMINATED_HTTPS listener (TLS terminated at the load balancer) and an HTTP listener."
    )
    # the helper moves the membership: SRC_VM_LB_MEMBER leaves it out
    assert "SRC_VM_LB_MEMBER" not in [x.code for x in findings]
    l4 = pair({k: v for k, v in L4_POOL.items() if k != "provider"})
    for pairs in (
        {"src-pool": l4},
        {"src-pool": pair(destination={"provider": "amphora"})},
        {"src-pool": PoolPair(source=None, destination=pair().destination)},
        {"src-pool": PoolPair(source=pair().source, destination=None)},
        None,
    ):
        codes = [x.code for x in check(lb_vm(), plan=plan, src=lb_src(), lb_pools=pairs)]
        assert "MAP_LB_POOL_INCOMPATIBLE" not in codes
    # a pool the plan does not map: the membership is not moved, and warned of
    unmapped = check(lb_vm(), src=lb_src(), lb_pools={"src-pool": pair(sticky)})
    assert "SRC_VM_LB_MEMBER" in [x.code for x in unmapped]
    assert "MAP_LB_POOL_INCOMPATIBLE" not in [x.code for x in unmapped]


async def test_a_validation_reads_each_mapped_pair_once(tmp_path, store):
    """SDD §8.5: a validation reads each mapped pair - the source pool and the destination pool -
    once, for MAP_LB_POOL_INCOMPATIBLE."""
    h, plan, source, destination, _ = await lb_setup(
        tmp_path, store, destination_kw={"provider": "ovn"}
    )
    reads: list[str] = []
    for cloud in (source, destination):
        read = cloud.lb_pool

        async def counted(pool_id, read=read, name=cloud.name):
            reads.append(f"{name}:{pool_id}")
            return await read(pool_id)

        cloud.lb_pool = counted
    report = await h.orch.validate_plan(plan.id, "alice")
    await h.orch.stop()
    [item] = report.migrations
    assert "MAP_LB_POOL_INCOMPATIBLE" in [f.code for f in item.findings]
    assert sorted(reads) == ["destination:dst-pool", "source:src-pool"]


async def test_octavias_pace_sends_a_change_again_and_waits_for_active():
    """SDD §8.5: a change Octavia refuses while the load balancer is PENDING_* (409) is sent again
    every 2 s, a member read every 2 s until ACTIVE, at most 60 s each; ERROR or the bound fails."""
    clock = {"t": 0.0}
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    pace = Pace(sleep=sleep, clock=lambda: clock["t"])
    calls = {"n": 0}

    async def busy_twice():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderConflict("dst: Octavia refused the change: HTTP 409")
        return "dst-member-1"

    assert await pace.change(busy_twice) == "dst-member-1"
    assert (calls["n"], slept) == (3, [2.0, 2.0])
    states = iter(["PENDING_CREATE", "PENDING_CREATE", "ACTIVE"])

    async def read():
        return {"provisioning_status": next(states)}

    await pace.active(read, "member 10.0.0.10:80")
    slept.clear()
    with pytest.raises(LbHelperError, match="^Octavia set member 10.0.0.10:80 to ERROR$"):

        async def broken():
            return {"provisioning_status": "ERROR"}

        await pace.active(broken, "member 10.0.0.10:80")
    assert slept == []  # at once: ERROR is no state Octavia leaves by itself
    clock["t"] = 0.0

    async def always_busy():
        raise ProviderConflict("dst: Octavia refused the change: HTTP 409")

    with pytest.raises(ProviderConflict):
        await pace.change(always_busy)
    assert clock["t"] == pytest.approx(60.0, abs=2.0)
    # any other refusal is not sent again
    slept.clear()
    with pytest.raises(ProviderError):

        async def forbidden():
            raise ProviderError("dst: Octavia refused the change: HTTP 403")

        await pace.change(forbidden)
    assert slept == []


# -- the OpenStack provider's calls over the in-process cloud (SDD §10) ------------------------

POOL = {
    "id": "pool-1",
    "name": "web-pool",
    "protocol": "HTTP",
    "lb_algorithm": "ROUND_ROBIN",
    "session_persistence": {"type": "HTTP_COOKIE", "cookie_name": None},
    "healthmonitor_id": "hm-1",
    "listeners": [{"id": "ls-1"}],
    "loadbalancers": [{"id": "lb-1"}],
}
KEY_MANAGER = "https://key-manager.example.org"
CONTAINER = f"{KEY_MANAGER}/v1/containers/c0ffee00-0000-4000-8000-000000000001"


def octavia_answers(**extra):
    return {
        "/v2.0/lbaas/pools/pool-1": (200, {"pool": POOL}),
        "/v2.0/lbaas/healthmonitors/hm-1": (200, {"healthmonitor": {"type": "HTTP"}}),
        "/v2.0/lbaas/listeners/ls-1": (
            200,
            {
                "listener": {
                    "protocol": "TERMINATED_HTTPS",
                    "default_tls_container_ref": CONTAINER,
                    "sni_container_refs": [CONTAINER],
                }
            },
        ),
        "/v2.0/lbaas/loadbalancers/lb-1": (200, {"loadbalancer": {"provider": "amphora"}}),
        "/v2.0/lbaas/pools/pool-1/members": (
            200,
            {
                "members": [
                    {
                        "id": "m-1",
                        "address": "2001:0db8::0010",
                        "protocol_port": 443,
                        "admin_state_up": True,
                        "provisioning_status": "ACTIVE",
                        "operating_status": "ONLINE",
                        "weight": 1,
                    }
                ]
            },
        ),
        **extra,
    }


def octavia_cloud(tmp_path, octavia):
    """A real OpenStackProvider over the in-process cloud, its catalog listing Octavia and a key
    manager; its subnet sub-app is 192.0.2.0/24."""
    from tests.test_providers import destination_cloud

    return destination_cloud(tmp_path, octavia=octavia, catalog={"key-manager": KEY_MANAGER})


async def test_tls_containers_are_mapped_never_read(tmp_path, caplog):
    """SDD §8.5, §23.7: a pool's read keeps its listeners' protocols - a TERMINATED_HTTPS one
    terminates TLS at the load balancer - and never follows nor keeps a TLS container reference:
    no request reaches the key manager, and no reference is in the read, a log line or the
    finding."""
    caplog.set_level(logging.DEBUG)
    cloud, provider = octavia_cloud(tmp_path, octavia_answers())
    pool = await provider.lb_pool("pool-1")
    assert pool == {
        "id": "pool-1",
        "name": "web-pool",
        "protocol": "HTTP",
        "lb_algorithm": "ROUND_ROBIN",
        "persistence": "HTTP_COOKIE",
        "monitor": "HTTP",
        "listeners": ["TERMINATED_HTTPS"],
        "provider": "amphora",
    }
    hosts = {urllib.parse.urlsplit(url).netloc for _, url in cloud.seen}
    assert "key-manager.example.org" not in hosts
    plan = make_plan(network_cutover={"lb_pools": {"src-pool": "dst-pool"}})
    findings = check(
        lb_vm(),
        plan=plan,
        src=lb_src(),
        lb_pools={"src-pool": PoolPair(source=pool, destination={**L4_POOL, "id": "dst-pool"})},
    )
    seen = "\n".join([repr(pool), caplog.text, *(f.message for f in findings)])
    # the catalog the token lists names the key manager; no reference to a container is kept
    assert "c0ffee00" not in seen and "/v1/containers" not in seen
    assert "TERMINATED_HTTPS listener" in only(findings, "MAP_LB_POOL_INCOMPATIBLE").message


async def test_an_openstack_provider_drains_joins_and_removes_a_member_unlogged(tmp_path, caplog):
    """SDD §8.5, §10: the members of a pool - [] for a pool the credential does not see -, a
    member's admin state changed, a member created on the credential's subnet that holds its
    address, one deleted - one gone is no error -; no request logged, a refusal named by its HTTP
    status alone, a 409 as Octavia's conflict."""
    caplog.set_level(logging.DEBUG)
    answers = octavia_answers(
        **{
            "PUT /v2.0/lbaas/pools/pool-1/members/m-1": (202, {"member": {"id": "m-1"}}),
            "POST /v2.0/lbaas/pools/pool-1/members": (201, {"member": {"id": "m-9"}}),
            "DELETE /v2.0/lbaas/pools/pool-1/members/m-9": (204, None),
            "DELETE /v2.0/lbaas/pools/pool-1/members/m-gone": (404, {"faultstring": "gone"}),
            "PUT /v2.0/lbaas/pools/pool-1/members/m-busy": (409, {"faultstring": "PENDING"}),
            "PUT /v2.0/lbaas/pools/pool-1/members/m-no": (403, {"faultstring": "web-pool no"}),
        }
    )
    cloud, provider = octavia_cloud(tmp_path, answers)
    assert await provider.pool_members("pool-1") == [
        {
            "id": "m-1",
            "address": "2001:db8::10",
            "protocol_port": 443,
            "admin_state_up": True,
            "provisioning_status": "ACTIVE",
        }
    ]
    assert await provider.pool_members("pool-gone") == []
    await provider.set_pool_member("pool-1", "m-1", False)
    member_id = await provider.add_pool_member("pool-1", "192.0.2.20", 80)
    # an address no subnet of the credential's holds: Octavia reaches it from the VIP subnet
    await provider.add_pool_member("pool-1", "198.51.100.5", 80)
    await provider.delete_pool_member("pool-1", member_id)
    await provider.delete_pool_member("pool-1", "m-gone")
    assert member_id == "m-9"
    assert cloud.written == [
        ("PUT", "/v2.0/lbaas/pools/pool-1/members/m-1", {"member": {"admin_state_up": False}}),
        (
            "POST",
            "/v2.0/lbaas/pools/pool-1/members",
            {"member": {"address": "192.0.2.20", "protocol_port": 80, "subnet_id": "sub-app"}},
        ),
        (
            "POST",
            "/v2.0/lbaas/pools/pool-1/members",
            {"member": {"address": "198.51.100.5", "protocol_port": 80}},
        ),
        ("DELETE", "/v2.0/lbaas/pools/pool-1/members/m-9", None),
        ("DELETE", "/v2.0/lbaas/pools/pool-1/members/m-gone", None),
    ]
    with pytest.raises(ProviderConflict) as busy:
        await provider.set_pool_member("pool-1", "m-busy", True)
    with pytest.raises(ProviderError) as refused:
        await provider.set_pool_member("pool-1", "m-no", True)
    assert str(busy.value) == "dst: Octavia refused the change: HTTP 409"
    assert str(refused.value) == "dst: Octavia refused the change: HTTP 403"
    assert not isinstance(refused.value, ProviderConflict)
    for text in ("web-pool", "192.0.2.20", "2001:db8::10"):
        assert text not in caplog.text
