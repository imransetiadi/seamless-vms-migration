"""The DNS swap (SDD §8.4, §9.3, §13.3, §23.5, plan task N7)."""

import logging

import dns.rcode
import pytest

from seamless_migrate.config import Settings
from seamless_migrate.dns_swap import (
    DesignateBackend,
    DnsSwapError,
    Rfc2136Backend,
    record_name,
    swap_addresses,
)
from seamless_migrate.domain.enums import Phase as P
from seamless_migrate.domain.enums import Severity, Strategy
from seamless_migrate.domain.models import DnsSwap, DnsTarget, Nic, VerificationConfig
from seamless_migrate.executors.base import StepName
from tests.dns_support import KEY_BYTES, KEY_NAME, KEY_SECRET, FakeDnsServer
from tests.factories import make_migration, make_plan, make_vm
from tests.orch_support import StaticDestination, build_harness
from tests.test_orchestrator import _RecordingKnowledge, plan_for, run_plan, vm
from tests.test_preflight import check

NAME = "web-01.example.com."
DESIGNATE = DnsTarget(kind="designate", zone="example.com.", record="web-01")


def rfc2136(server):
    return DnsTarget(
        kind="rfc2136",
        zone="example.com.",
        record="web-01",
        server=server.address,
        tsig_secret="swap-tsig",
    )


@pytest.fixture
def settings(tmp_path):
    """Settings whose secret store holds the synthetic TSIG key as SDD §13.3 names its keys."""
    secret = tmp_path / "secrets" / "swap-tsig"
    secret.mkdir(parents=True)
    (secret / "name").write_text(KEY_NAME)
    (secret / "secret").write_text(KEY_SECRET)
    return Settings(data_dir=tmp_path / "data", secrets_dir=tmp_path / "secrets")


class Designate:
    """Designate's recordsets as a destination provider reads and updates them (SDD §10): an
    update turns the recordset PENDING, and ``pending`` reads later ACTIVE - or ``ending``."""

    def __init__(self, records=("192.0.2.10",), ttl=300, pending=2, ending="ACTIVE"):
        self.recordset = {
            "zone_id": "zone-1",
            "id": "rs-1",
            "records": list(records),
            "ttl": ttl,
            "status": "ACTIVE",
        }
        self.pending, self.ending = pending, ending
        self.reads = 0
        self.updates: list[list[str]] = []

    async def dns_recordset(self, zone, name, rdtype):
        assert (zone, name, rdtype) == ("example.com.", NAME, "A")
        self.reads += 1
        if self.recordset["status"] == "PENDING":
            if self.pending:
                self.pending -= 1
            else:
                self.recordset["status"] = self.ending
        return dict(self.recordset, records=list(self.recordset["records"]))

    async def update_dns_recordset(self, zone_id, recordset_id, records):
        assert (zone_id, recordset_id) == ("zone-1", "rs-1")
        self.updates.append(list(records))
        self.recordset.update(records=list(records), status="PENDING")


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


async def test_a_designate_swap_waits_for_the_recordset_to_be_active():
    """SDD §8.4: a recordset holding exactly the old address gets the new one, and the swap reads
    it every 2 s until Designate made it ACTIVE; ERROR, or no ACTIVE within 60 s, fails it."""
    designate, clock = Designate(), Clock()
    backend = DesignateBackend(DESIGNATE, designate, sleep=clock.sleep, clock=clock)
    assert await backend.replace(NAME, "A", "192.0.2.10", "198.51.100.20") == ("swapped", [])
    assert designate.updates == [["198.51.100.20"]]
    assert designate.recordset["status"] == "ACTIVE"
    assert clock.sleeps == [2.0, 2.0]
    record = await backend.read(NAME, "A")
    assert (record.values, record.ttl) == (["198.51.100.20"], 300)

    broken = Designate(ending="ERROR")
    backend = DesignateBackend(DESIGNATE, broken, sleep=Clock().sleep, clock=Clock())
    with pytest.raises(DnsSwapError, match="ERROR"):
        await backend.replace(NAME, "A", "192.0.2.10", "198.51.100.20")
    stuck, clock = Designate(pending=1000), Clock()
    backend = DesignateBackend(DESIGNATE, stuck, sleep=clock.sleep, clock=clock)
    with pytest.raises(DnsSwapError, match="not ACTIVE after 60 s"):
        await backend.replace(NAME, "A", "192.0.2.10", "198.51.100.20")
    assert clock.now == pytest.approx(60.0, abs=2.0)


async def test_an_rfc2136_swap_is_a_compare_and_swap(settings):
    """SDD §8.4: one TSIG-signed UPDATE whose prerequisite is the RRset holding exactly the old
    address deletes it and adds the new one with the TTL a signed query read just before; the
    same update again finds the prerequisite false and changes nothing more."""
    async with FakeDnsServer(rrsets={(NAME, "A"): (300, {"192.0.2.10"})}) as server:
        backend = Rfc2136Backend(rfc2136(server), settings)
        assert await backend.replace(NAME, "A", "192.0.2.10", "198.51.100.20") == ("swapped", [])
        assert server.rrsets[(NAME, "A")] == (300, {"198.51.100.20"})
        [update] = server.updates
        [prerequisite] = update.prerequisite
        assert (prerequisite.name.to_text(), {r.to_text() for r in prerequisite}) == (
            NAME,
            {"192.0.2.10"},
        )
        assert update.keyname.to_text() == KEY_NAME  # signed
        # a swap a restart interrupted after its write: the record holds the new address already
        assert await backend.replace(NAME, "A", "192.0.2.10", "198.51.100.20") == ("swapped", [])
        assert server.rrsets[(NAME, "A")] == (300, {"198.51.100.20"})


async def test_a_swap_that_finds_another_address_changes_nothing(settings):
    """SDD §8.4: a record holding anything but the old address - another one, more, none - is a
    conflict: nothing is written, and the outcome names what the record holds."""
    designate = Designate(records=("203.0.113.99",))
    backend = DesignateBackend(DESIGNATE, designate, sleep=Clock().sleep, clock=Clock())
    assert await backend.replace(NAME, "A", "192.0.2.10", "198.51.100.20") == (
        "conflict",
        ["203.0.113.99"],
    )
    assert designate.updates == []
    rrsets = {(NAME, "A"): (300, {"192.0.2.10", "192.0.2.11"})}
    async with FakeDnsServer(rrsets=rrsets) as server:
        backend = Rfc2136Backend(rfc2136(server), settings)
        assert await backend.replace(NAME, "A", "192.0.2.10", "198.51.100.20") == (
            "conflict",
            ["192.0.2.10", "192.0.2.11"],
        )
        assert server.rrsets[(NAME, "A")] == (300, {"192.0.2.10", "192.0.2.11"})
    async with FakeDnsServer() as server:
        backend = Rfc2136Backend(rfc2136(server), settings)
        assert await backend.replace(NAME, "A", "192.0.2.10", "198.51.100.20") == (
            "conflict",
            [],
        )


class DnsDestination(StaticDestination, Designate):
    """A destination whose Designate holds the VM's record (SDD §8.4); its new server's port holds
    10.0.0.20 and ``floating``."""

    def __init__(self, floating=(), **kw):
        StaticDestination.__init__(self)
        Designate.__init__(self, **kw)
        self.floating = list(floating)

    async def get_server(self, server_id):
        server = await StaticDestination.get_server(self, server_id)
        port = {"status": "ACTIVE", "fixed_ips": ["10.0.0.20"], "floating_ips": self.floating}
        server["ports"] = [port]
        return server


MAC = "fa:16:3e:00:00:01"


def moved_vm(**nic):
    """web-01 at 10.0.0.10, which its NIC override moves to 10.0.0.20 (SDD §8.4, §9.2)."""
    return vm(1, nics=[Nic(network="app-net", mac=MAC, mtu=1500, fixed_ips=["10.0.0.10"], **nic)])


def dns_plan(vms, auto_rollback=True, **kw):
    """A cold plan whose web-01 has its record in Designate and its NIC's address moved."""
    data = {
        "default_strategy": Strategy.cold,
        "verification": VerificationConfig(timeout_s=2, auto_rollback=auto_rollback),
        "nic_overrides": {"vm-1": {MAC: {"fixed_ips": ["10.0.0.20"]}}},
        "network_cutover": {"dns_targets": {"vm-1": DESIGNATE.model_dump()}},
    }
    data.update(kw)
    return plan_for(vms, **data)


def dns_actions(h, mid):
    return [
        e.data
        for e in h.store.events(since_seq=0, migration_id=mid, limit=1000)
        if e.kind == "migration.action" and e.data.get("action") == "dns"
    ]


async def test_a_rollback_swaps_the_record_back(tmp_path, store):
    """SDD §8.4: in verifying the record moves from the VM's address at the source to the one
    taking its place - its NIC override's here -, journaled; a rollback swaps it back before the
    source starts."""
    destination = DnsDestination(records=("10.0.0.10",), pending=0)
    nic = Nic(network="app-net", mac="fa:16:3e:00:00:01", mtu=1500, fixed_ips=["10.0.0.10"])
    vms = [vm(1, nics=[nic])]
    h = build_harness(tmp_path, store, vms, destination=destination)
    plan = plan_for(
        vms,
        default_strategy=Strategy.cold,
        verification=VerificationConfig(timeout_s=2),
        nic_overrides={"vm-1": {"fa:16:3e:00:00:01": {"fixed_ips": ["10.0.0.20"]}}},
        network_cutover={"dns_targets": {"vm-1": DESIGNATE.model_dump()}},
    )
    store.put("plan", plan)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    done = await h.wait_phase(mid, P.completed)
    swap = done.dns_swap
    assert (swap.name, swap.rdtype, swap.old, swap.new, swap.state) == (
        NAME,
        "A",
        "10.0.0.10",
        "10.0.0.20",
        "swapped",
    )
    assert destination.recordset["records"] == ["10.0.0.20"]
    steps = []

    async def rollback(ctx):
        steps.append(list(destination.recordset["records"]))
        return await h.executor.sim.run(StepName.ROLLBACK, ctx)

    h.executor.hooks[StepName.ROLLBACK] = rollback
    await h.orch.rollback(mid, "alice", "the application team asked for it")
    back = await h.wait_phase(mid, P.rolled_back)
    await h.orch.stop()
    assert steps == [["10.0.0.10"]]  # swapped back before the step started the source
    assert back.dns_swap.state == "reverted"
    actions = [
        (e.data["step"], e.data["outcome"])
        for e in h.store.events(since_seq=0, migration_id=mid, limit=1000)
        if e.kind == "migration.action" and e.data.get("action") == "dns"
    ]
    assert actions == [("swap", "succeeded"), ("revert", "succeeded")]


async def test_a_designate_swap_of_a_plan_with_projects_uses_its_destination_credential(
    tmp_path, store
):
    """SDD §8.4, §27.5 (plan task TC5): Designate's zones are a project's, so for a plan with a
    destination project validation's read of the record's TTL, the swap and the revert all go
    with the plan's destination credential - never the destination provider's own."""
    from tests.credential_support import of_project, own, with_projects
    from tests.orch_support import DST_PROVIDER

    destination = DnsDestination(records=("10.0.0.10",), pending=0)
    vms = [moved_vm()]
    h = build_harness(tmp_path, store, vms, destination=destination)
    plan = dns_plan(vms)
    store.put("plan", plan)
    registry, t = with_projects(h, plan)
    await run_plan(h, t.plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    assert (await h.wait_phase(mid, P.completed)).dns_swap.state == "swapped"
    await h.orch.rollback(mid, "alice", "the application team asked for it")
    assert (await h.wait_phase(mid, P.rolled_back)).dns_swap.state == "reverted"
    await h.orch.stop()
    assert destination.updates == [["10.0.0.20"], ["10.0.0.10"]]
    dst = of_project(DST_PROVIDER, t.destination)
    assert registry.used("dns_recordset") and set(registry.used("dns_recordset")) == {dst}
    assert registry.used("update_dns_recordset") == [dst, dst]
    assert not {"dns_recordset", "update_dns_recordset"} & registry.methods(own(DST_PROVIDER))


async def test_a_failed_swap_fails_the_migration_as_a_post_create_hook_does(tmp_path, store):
    """SDD §8.4: a swap that fails - Designate's ERROR here - fails the migration in verifying,
    before the verification, as a post_create hook's failure does: auto_rollback decides (held
    here). Its error names the step dns, and agentmemory is not asked about it (§13.4)."""
    knowledge = _RecordingKnowledge()
    destination = DnsDestination(records=("10.0.0.10",), pending=0, ending="ERROR")
    vms = [moved_vm()]
    h = build_harness(tmp_path, store, vms, destination=destination, knowledge=knowledge)
    plan = dns_plan(vms, auto_rollback=False)
    store.put("plan", plan)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    m = await h.wait_phase(mid, P.failed)
    await h.wait_kind("migration.error")  # emitted once the failure is saved
    await h.orch.stop()
    assert m.error == f"dns: Designate set the recordset {NAME} to ERROR"
    assert m.verification is None and m.downtime_ended_at is None
    assert m.dns_swap.state == "swapping"  # a rollback reverts it, a retry resumes it
    [action] = dns_actions(h, mid)
    assert (action["name"], action["step"], action["outcome"]) == (NAME, "swap", "failed")
    errors = [
        e for e in h.store.events(since_seq=0, migration_id=mid) if e.kind == "migration.error"
    ]
    assert [e.data["step"] for e in errors] == ["dns"]
    assert knowledge.failures == []


async def test_a_conflict_lets_the_migration_go_on(tmp_path, store):
    """SDD §8.4: a record that holds another address is a conflict - nothing is written, the
    action names what it holds, the migration completes -, and a rollback reverts nothing."""
    destination = DnsDestination(records=("10.0.0.99",))
    vms = [moved_vm()]
    h = build_harness(tmp_path, store, vms, destination=destination)
    plan = dns_plan(vms)
    store.put("plan", plan)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    done = await h.wait_phase(mid, P.completed)
    assert done.dns_swap.state == "conflict"
    await h.orch.rollback(mid, "alice", "the application team asked for it")
    await h.wait_phase(mid, P.rolled_back)
    await h.orch.stop()
    assert destination.updates == []
    [action] = dns_actions(h, mid)
    assert (action["step"], action["outcome"], action["detail"]) == (
        "swap",
        "conflict",
        "it holds 10.0.0.99, not 10.0.0.10: left as it is",
    )


@pytest.mark.parametrize(
    ("floating", "outcome"), [("198.51.100.77", "swapped"), ("203.0.113.10", "unchanged")]
)
async def test_a_floating_ip_swaps_to_the_new_servers_and_a_kept_one_to_nothing(
    tmp_path, store, floating, outcome
):
    """SDD §8.4: the record of a VM with a floating IP moves to the new server's floating IP on
    the port holding the NIC's address; the same address there - a kept one - is unchanged and
    touches no record."""
    destination = DnsDestination(records=("203.0.113.10",), pending=0, floating=[floating])
    vms = [moved_vm(floating_ips=["203.0.113.10"])]
    h = build_harness(tmp_path, store, vms, destination=destination)
    plan = dns_plan(vms)
    store.put("plan", plan)
    await run_plan(h, plan)
    mid = (await h.by_vm(plan.id, "vm-1")).id
    done = await h.wait_phase(mid, P.completed)
    await h.orch.stop()
    swap = done.dns_swap
    assert (swap.old, swap.new, swap.state) == ("203.0.113.10", floating, outcome)
    assert destination.recordset["records"] == [floating]
    assert len(destination.updates) == (outcome == "swapped")
    [action] = dns_actions(h, mid)
    assert action["outcome"] == ("succeeded" if outcome == "swapped" else "unchanged")


async def test_a_restart_resumes_the_swap_it_saved(tmp_path, store):
    """SDD §8.4: a swap saved as swapping is resumed with its saved addresses - a record that
    holds the new one already is not written again -, one swapped, unchanged or in conflict is
    not made again; and a revert whose plan dropped the VM's target is recorded, not run."""
    destination = DnsDestination(records=("10.0.0.20",))
    vms = [moved_vm()]
    h = build_harness(tmp_path, store, vms, destination=destination)
    plan = dns_plan(vms)
    store.put("plan", plan)
    swap = DnsSwap(name=NAME, rdtype="A", old="10.0.0.10", new="10.0.0.20", state="swapping")
    m = make_migration(
        plan_id=plan.id,
        vm=vms[0],
        phase=P.verifying,
        destination_server_id="srv-1",
        dns_swap=swap,
    )
    store.put("migration", m)
    assert await h.orch._swap_dns(m) is None
    assert destination.updates == []
    assert (await h.migration(m.id)).dns_swap.state == "swapped"
    for state in ("swapped", "unchanged", "conflict"):
        store.put(
            "migration", m.model_copy(update={"dns_swap": swap.model_copy(update={"state": state})})
        )
        assert await h.orch._swap_dns(m) is None
    assert destination.reads == 1  # the resumed swap's one read, nothing after
    store.put(
        "migration", m.model_copy(update={"dns_swap": swap.model_copy(update={"state": "swapped"})})
    )
    store.put(
        "plan",
        plan.model_copy(
            update={"network_cutover": plan.network_cutover.model_copy(update={"dns_targets": {}})}
        ),
    )
    assert await h.orch._revert_dns(m) is None
    assert destination.recordset["records"] == ["10.0.0.20"]
    [*_, revert] = dns_actions(h, m.id)
    assert (revert["step"], revert["outcome"]) == ("revert", "failed")
    assert revert["detail"] == (
        "the plan names no DNS target for the VM any more: the record may hold 10.0.0.20"
    )


async def test_a_swaps_failure_never_carries_a_librarys_text(tmp_path, store):
    """SDD §8.4, §13.4: a failure other than the swap's own or a provider's is named by its kind
    alone - in the action and in what fails the migration -, never by the text a library put in
    it."""

    class Broken(DnsDestination):
        async def dns_recordset(self, zone, name, rdtype):
            raise RuntimeError("a library's text, which may quote anything")

    vms = [moved_vm()]
    h = build_harness(tmp_path, store, vms, destination=Broken(records=("10.0.0.10",)))
    plan = dns_plan(vms)
    store.put("plan", plan)
    m = make_migration(plan_id=plan.id, vm=vms[0], phase=P.verifying, destination_server_id="s")
    store.put("migration", m)
    failure = await h.orch._swap_dns(m)
    assert str(failure) == "RuntimeError"
    [action] = dns_actions(h, m.id)
    assert (action["outcome"], action["detail"]) == ("failed", "RuntimeError")


async def test_a_validation_reads_the_ttl_of_each_targets_record(tmp_path, store):
    """SDD §8.4, §9.3: a validation reads the record of each VM the plan names a DNS target for
    and warns when its TTL is longer than the downtime SLO; a VM without a target is not read."""
    destination = DnsDestination(records=("10.0.0.10",), ttl=3600)
    vms = [moved_vm(), vm(2)]
    h = build_harness(tmp_path, store, vms, destination=destination)
    plan = dns_plan(vms, downtime_slo_s=600)
    store.put("plan", plan)
    report = await h.orch.validate_plan(plan.id, "alice")
    await h.orch.stop()
    codes = {item.vm_name: [f.code for f in item.findings] for item in report.migrations}
    assert "NET_DNS_TTL_LONG" in codes["web-01"]
    assert "NET_DNS_TTL_LONG" not in codes["web-02"]
    assert destination.reads == 1


def test_a_ttl_longer_than_the_slo_warns():
    """SDD §9.3: a DNS target whose record validation read holds a TTL longer than the plan's
    downtime SLO warns; a shorter one, a record that could not be read or a VM without a target
    do not."""
    targets = {"dns_targets": {"vm-1": DESIGNATE.model_dump()}}
    plan = make_plan(downtime_slo_s=600, network_cutover=targets)
    [f] = [x for x in check(plan=plan, dns_ttl=3600) if x.code == "NET_DNS_TTL_LONG"]
    assert (f.severity, f.strategies) == (Severity.warning, [])
    assert f.message == (
        "The DNS record web-01.example.com. holds a TTL of 3600 s, longer than the plan's "
        "downtime SLO of 600 s: after the swap, clients may reach the old address for up to "
        "3600 s."
    )
    for ttl in (600, None):
        assert "NET_DNS_TTL_LONG" not in [x.code for x in check(plan=plan, dns_ttl=ttl)]
    assert "NET_DNS_TTL_LONG" not in [x.code for x in check(dns_ttl=3600)]


async def test_no_tsig_key_reaches_a_log_line(settings, caplog):
    """SDD §8.4, §13.4: neither the TSIG secret nor its key bytes reach a log line or an error -
    a refused update, a server signing with another key, an unreachable server."""
    caplog.set_level(logging.DEBUG)
    rrsets = {(NAME, "A"): (300, {"192.0.2.10"})}
    errors = []
    async with FakeDnsServer(rrsets=rrsets, refuse=dns.rcode.REFUSED) as server:
        with pytest.raises(DnsSwapError) as refused:
            await Rfc2136Backend(rfc2136(server), settings).replace(
                NAME, "A", "192.0.2.10", "198.51.100.20"
            )
        errors.append(str(refused.value))
        assert "REFUSED" in str(refused.value)
    async with FakeDnsServer(rrsets=rrsets) as server:
        server.keyring = {KEY_NAME: b"another-key-entirely-another-one"}
        with pytest.raises(DnsSwapError) as unsigned:
            await Rfc2136Backend(rfc2136(server), settings).replace(
                NAME, "A", "192.0.2.10", "198.51.100.20"
            )
        errors.append(str(unsigned.value))
    gone = DnsTarget(
        kind="rfc2136",
        zone="example.com.",
        record="web-01",
        server="127.0.0.1:9",
        tsig_secret="swap-tsig",
    )
    with pytest.raises(DnsSwapError) as unreachable:
        await Rfc2136Backend(gone, settings, timeout_s=2).replace(
            NAME, "A", "192.0.2.10", "198.51.100.20"
        )
    errors.append(str(unreachable.value))
    seen = "\n".join([caplog.text, *errors])
    for secret in (KEY_SECRET, KEY_BYTES.decode(), repr(KEY_BYTES)):
        assert secret not in seen


def test_the_swap_takes_the_vms_address_and_the_one_replacing_it():
    """SDD §8.4: the old address is the VM's first floating IP, else its first fixed IP; the new
    one the destination server's floating IP on that NIC's port, else the NIC's first address at
    the destination - its override's, else its own; equal addresses swap nothing."""
    fixed = Nic(network="app-net", mac="fa:16:3e:00:00:01", fixed_ips=["10.0.0.5"])
    floating = fixed.model_copy(update={"floating_ips": ["203.0.113.10"]})
    plan = make_plan()
    m = make_migration(vm=make_vm(nics=[fixed]))
    assert swap_addresses(m, plan, None) == ("10.0.0.5", "10.0.0.5")
    moved = make_plan(nic_overrides={"vm-1": {"fa:16:3e:00:00:01": {"fixed_ips": ["10.9.0.5"]}}})
    assert swap_addresses(m, moved, None) == ("10.0.0.5", "10.9.0.5")
    # the old address's family: an A record never gets an IPv6 address
    dual = {"fixed_ips": ["2001:db8::5", "10.9.0.6"]}
    dual_stack = make_plan(nic_overrides={"vm-1": {"fa:16:3e:00:00:01": dual}})
    assert swap_addresses(m, dual_stack, None) == ("10.0.0.5", "10.9.0.6")
    v6 = make_plan(nic_overrides={"vm-1": {"fa:16:3e:00:00:01": {"fixed_ips": ["2001:db8::5"]}}})
    with pytest.raises(DnsSwapError, match="has no IPv4 address in place of 10.0.0.5"):
        swap_addresses(m, v6, None)
    m = make_migration(vm=make_vm(nics=[floating]))
    server = {"ports": [{"fixed_ips": ["10.0.0.5"], "floating_ips": ["198.51.100.77"]}]}
    assert swap_addresses(m, plan, server) == ("203.0.113.10", "198.51.100.77")
    assert record_name(DESIGNATE) == NAME
    assert record_name(DESIGNATE.model_copy(update={"record": "web-01.example.com."})) == NAME
    assert record_name(DESIGNATE.model_copy(update={"zone": "example.com"})) == NAME
