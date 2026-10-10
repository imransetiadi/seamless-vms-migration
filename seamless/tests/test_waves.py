from seamless_migrate.planning.waves import TIERS, heuristic_tier, plan_waves
from tests.factories import make_disk, make_vm


def vm(source_id: str, name: str, size_gb: int = 20, os_type: str = "rhel9", **tags: str):
    return make_vm(
        source_id=source_id,
        name=name,
        os_type=os_type,
        tags=dict(tags),
        disks=[make_disk(id=f"{source_id}-d", size_gb=size_gb)],
    )


def test_tier_order_matches_sdd():
    assert TIERS == (
        "stateless_web",
        "middleware_queue",
        "infrastructure_service",
        "stateful_database",
        "legacy_os",
        "manual_review",
    )


def test_heuristic_tier():
    assert heuristic_tier(vm("1", "web-01")) == "stateless_web"
    assert heuristic_tier(vm("2", "shop-frontend")) == "stateless_web"
    assert heuristic_tier(vm("3", "db-02")) == "stateful_database"
    assert heuristic_tier(vm("4", "app-7", role="postgresql")) == "stateful_database"
    assert heuristic_tier(vm("5", "mq-01")) == "middleware_queue"
    assert heuristic_tier(vm("6", "kafka-broker-2")) == "middleware_queue"
    assert heuristic_tier(vm("7", "win-ad-01", os_type="windows2019")) == "infrastructure_service"
    assert heuristic_tier(vm("8", "ns1-dns")) == "infrastructure_service"
    # legacy OS takes precedence over the role
    assert heuristic_tier(vm("9", "web-legacy", os_type="rhel-6.10")) == "legacy_os"
    assert heuristic_tier(vm("10", "xyz-42")) == "manual_review"
    # an explicit tier tag wins
    assert heuristic_tier(vm("11", "xyz-43", tier="stateful_database")) == "stateful_database"
    # tag keys are not evidence (only values are): "app" must not mean middleware
    assert heuristic_tier(vm("12", "shop-web", app="shop")) == "stateless_web"


def test_pilot_wave_first():
    vms = [
        vm("db", "db-01", 500),
        vm("web-big", "web-03", 200),
        vm("mq", "mq-01", 50),
        vm("web-a", "web-01", 10),
        vm("web-b", "web-02", 30),
        vm("infra", "dns-01", 15),
    ]
    tiers = {v.source_id: heuristic_tier(v) for v in vms}
    waves = plan_waves(vms, tiers, max_wave_size=10)
    pilot = waves[0]
    assert (pilot.id, pilot.name, pilot.order, pilot.depends_on) == ("wave-1", "Pilot", 1, [])
    # stateless_web first, smallest disks first, at most 3 VMs
    assert pilot.vm_ids == ["web-a", "web-b", "web-big"]
    rest = waves[1]
    assert rest.id == "wave-2" and rest.depends_on == ["wave-1"]
    assert rest.vm_ids == ["mq", "infra", "db"]
    assert all(w.max_parallel == 5 for w in waves)


def test_pilot_never_takes_a_riskier_vm_than_one_it_left_out():
    """SDD §9.4: the pilot holds the lowest-risk VMs. App groups are taken whole in risk order; when
    one does not fit, a smaller but riskier one never takes its place (the pilot then holds fewer
    than 3 VMs), while one of the same risk still may."""
    blog = [
        vm("blog-1", "blog-web-01", 10, app="blog"),
        vm("blog-2", "blog-web-02", 10, app="blog"),
    ]
    shop = [
        vm("shop-1", "shop-web-01", 20, app="shop"),
        vm("shop-2", "shop-web-02", 20, app="shop"),
    ]
    dbs = [vm("db", "orders-postgres-01", 30), vm("db2", "billing-mysql-01", 40)]
    tiers = {v.source_id: heuristic_tier(v) for v in [*blog, *shop, *dbs]}
    assert {tiers[v.source_id] for v in dbs} == {"stateful_database"}
    assert {tiers[v.source_id] for v in [*blog, *shop]} == {"stateless_web"}

    waves = plan_waves([*blog, *shop, *dbs], tiers, max_wave_size=10)
    assert waves[0].name == "Pilot" and waves[0].vm_ids == ["blog-1", "blog-2"]
    assert waves[1].vm_ids == ["shop-1", "shop-2", "db", "db2"]

    # a web VM as low-risk as the app group left out may still fill the room
    cms = vm("cms", "cms-web-01", 50)
    waves = plan_waves([*blog, *shop, *dbs, cms], {**tiers, "cms": heuristic_tier(cms)})
    assert waves[0].vm_ids == ["blog-1", "blog-2", "cms"]
    assert waves[1].vm_ids == ["shop-1", "shop-2", "db", "db2"]


def test_waves_chunked_by_size_and_chained():
    vms = [vm(f"w{i}", f"web-{i:02d}", 10 + i) for i in range(10)]
    tiers = {v.source_id: "stateless_web" for v in vms}
    waves = plan_waves(vms, tiers, max_wave_size=3)
    assert [len(w.vm_ids) for w in waves] == [3, 3, 3, 1]
    for prev, nxt in zip(waves, waves[1:], strict=False):
        assert nxt.depends_on == [prev.id] and nxt.order == prev.order + 1
    assert sorted(v for w in waves for v in w.vm_ids) == sorted(v.source_id for v in vms)


def test_app_tag_kept_together():
    vms = [vm(f"w{i}", f"web-{i:02d}", 10 + i) for i in range(6)]
    shop = [
        vm("shop-web", "shop-web", 5, app="shop"),
        vm("shop-mq", "shop-mq", 8, app="shop"),
        vm("shop-db", "shop-db", 300, app="shop"),
        vm("shop-db2", "shop-db2", 300, app="shop"),
    ]
    allvms = vms + shop
    tiers = {v.source_id: heuristic_tier(v) for v in allvms}
    waves = plan_waves(allvms, tiers, max_wave_size=3)
    holders = [w for w in waves if set(w.vm_ids) & {s.source_id for s in shop}]
    assert len(holders) == 1, "an app must not be split across waves"
    assert {s.source_id for s in shop} <= set(holders[0].vm_ids)
    assert len(holders[0].vm_ids) >= 4  # may exceed max_wave_size to keep the app together
    assert waves[0].name == "Pilot" and not set(waves[0].vm_ids) & {s.source_id for s in shop}


def test_manual_review_last_wave():
    vms = [
        vm("odd", "xyz-01"),
        vm("web", "web-01"),
        vm("db", "db-01"),
        vm("odd2", "qq-77", 5),
    ]
    tiers = {v.source_id: heuristic_tier(v) for v in vms}
    waves = plan_waves(vms, tiers)
    last = waves[-1]
    assert last.name == "Manual review" and set(last.vm_ids) == {"odd", "odd2"}
    assert last.depends_on == [waves[-2].id]
    assert all("odd" not in w.vm_ids for w in waves[:-1])


def test_empty_plan_has_no_waves():
    assert plan_waves([], {}) == []
