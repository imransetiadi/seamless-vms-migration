import json
import secrets
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from seamless_migrate.domain.enums import (
    Phase,
    PlanStatus,
    ProviderKind,
    ProviderRole,
    Role,
    Strategy,
    SyncPassKind,
)
from seamless_migrate.domain.models import (
    BLANK_RANGES,
    AdvisorNote,
    CutoverWindow,
    Estimate,
    Event,
    Finding,
    GpuDevice,
    Mappings,
    Migration,
    Plan,
    Provider,
    SyncPass,
    VMRef,
    is_blank,
    new_migration_id,
    new_plan_id,
)
from tests.factories import GIB, make_disk, make_migration, make_plan, make_provider, make_vm


def test_vmref_used_bytes_fallback_is_60_percent():
    vm = make_vm(
        disks=[
            make_disk(size_gb=100, used_gb=None),
            make_disk(id="vol-2", size_gb=50, used_gb=40.0),
        ]
    )
    assert vm.disk_bytes == 150 * GIB
    # one disk lacks used_gb -> 60 % of the provisioned size (floored)
    assert vm.used_bytes == int(150 * GIB * 0.6)

    exact = make_vm(disks=[make_disk(size_gb=100, used_gb=12.5), make_disk(id="v2", used_gb=0.5)])
    assert exact.used_bytes == int(13 * GIB)


def test_vmref_derived_sizes_are_serialized():
    vm = make_vm(disks=[make_disk(size_gb=10, used_gb=None)])
    dumped = json.loads(vm.model_dump_json())
    assert dumped["disk_bytes"] == 10 * GIB
    assert dumped["used_bytes"] == int(10 * GIB * 0.6)


def test_models_roundtrip_json():
    plan = make_plan(
        strategy_overrides={"vm-1": Strategy.cold},
        cutover_window=CutoverWindow(
            start=datetime(2026, 10, 10, 22, tzinfo=UTC), end=datetime(2026, 10, 11, 4, tzinfo=UTC)
        ),
    )
    assert Plan.model_validate_json(plan.model_dump_json()) == plan

    mig = make_migration(
        phase=Phase.awaiting_cutover,
        sync_passes=[
            SyncPass(
                number=1,
                kind=SyncPassKind.full,
                started_at=datetime(2026, 10, 8, 1, tzinfo=UTC),
                ended_at=datetime(2026, 10, 8, 2, tzinfo=UTC),
                bytes_scanned=10,
                bytes_changed=5,
                bytes_transferred=5,
                duration_s=3600.0,
            )
        ],
        estimate=Estimate(
            strategy=Strategy.warm,
            eligible=True,
            precopy_s=10.0,
            passes=2,
            downtime_s=300.0,
            total_s=310.0,
            final_delta_bytes=1024,
            meets_slo=True,
        ),
        findings=[
            Finding(code="NET_MTU_SHRINK", severity="warning", message="mtu", remediation=None)
        ],
        advisor_notes=[
            AdvisorNote(kind="strategy", source="rules", summary="kept", confidence=None)
        ],
    )
    assert Migration.model_validate_json(mig.model_dump_json()) == mig


def test_timestamps_serialize_as_utc_z():
    plan = make_plan(created_at=datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC))
    data = json.loads(plan.model_dump_json())
    assert data["created_at"] == "2026-10-08T12:00:00Z"
    # naive datetimes are interpreted as UTC
    ev = Event(
        seq=1, ts=datetime(2026, 10, 8, 12, 0, 0), kind="plan.created", actor="a", message="m"
    )
    assert json.loads(ev.model_dump_json())["ts"] == "2026-10-08T12:00:00Z"


def test_ids_have_documented_shapes():
    assert new_plan_id().startswith("plan-") and len(new_plan_id()) == len("plan-") + 8
    assert new_migration_id().startswith("mig-") and len(new_migration_id()) == len("mig-") + 10
    assert make_plan().id.startswith("plan-")
    assert make_migration().id.startswith("mig-")


def test_provider_id_regex_enforced():
    make_provider(id="rhoso-18")
    for bad in ("A", "-abc", "x", "has_underscore", "a" * 64):
        with pytest.raises(ValidationError):
            make_provider(id=bad)


def test_provider_release_is_optional_and_validated_as_a_series():
    """SDD §4.2: a provider declares no series unless its operator does, and a declared one reads
    as releases.openstack.org names them - a year, a dot, 1 or 2 (§10.1, §21.3)."""
    assert make_provider().release is None
    for series in ("2023.1", "2024.2", "2026.2", "2027.1"):
        assert make_provider(release=series).release == series
    for bad in (
        "2026.3",
        "26.2",
        "2026.2.1",
        "2026",
        "Hibiscus",
        " 2026.2",
        "2026.2 ",
        "1999.1",
        "",
    ):
        with pytest.raises(ValidationError, match="release"):
            make_provider(release=bad)


def test_a_release_on_a_vmware_provider_is_refused():
    """SDD §4.2: an OpenStack series means nothing for vCenter - refused, never stored."""
    vmware = {"id": "vc-01", "kind": ProviderKind.vmware, "endpoint": "https://vcenter.example"}
    assert make_provider(**vmware).release is None
    with pytest.raises(ValidationError, match="release"):
        make_provider(**vmware, release="2026.2")
    destination = {"id": "dst-hibiscus", "role": ProviderRole.destination, "release": "2026.2"}
    assert make_provider(**destination).release == "2026.2"


def test_provider_policy_profile_is_declared_or_null_and_refused_on_vmware():
    """SDD §4.2, §21.5: a provider declares secure_rbac or legacy, or nothing (by kind and series,
    §10.1); a vmware provider declares none."""
    assert make_provider().policy_profile is None
    for profile in ("secure_rbac", "legacy"):
        assert make_provider(policy_profile=profile).policy_profile == profile
    for bad in ("unknown", "strict", "SECURE_RBAC", ""):
        with pytest.raises(ValidationError, match="policy_profile"):
            make_provider(policy_profile=bad)
    vmware = {"id": "vc-01", "kind": ProviderKind.vmware, "endpoint": "https://vcenter.example"}
    with pytest.raises(ValidationError, match="policy_profile"):
        make_provider(**vmware, policy_profile="legacy")


def test_plan_port_creation_mode_is_nova_unless_neutron_is_chosen():
    """SDD §4.2: "nova" keeps today's ports; "neutron" recreates each with its MAC, every fixed
    IP and its security groups; nothing else is a mode."""
    assert make_plan().port_creation_mode == "nova"
    assert make_plan(port_creation_mode="neutron").port_creation_mode == "neutron"
    with pytest.raises(ValidationError, match="port_creation_mode"):
        make_plan(port_creation_mode="ovs")


def test_plan_nic_overrides_keep_lower_case_macs_and_canonical_addresses():
    """SDD §4.2: per VM and NIC MAC (stored lower case) a RHOSO network and addresses; null keeps
    the mapping or the NIC's own; the addresses in their canonical form."""
    from seamless_migrate.domain.models import invalid_plan_settings

    plan = make_plan(
        nic_overrides={
            "vm-1": {
                "FA:16:3E:00:00:01": {"network": "rhoso-dmz", "fixed_ips": ["fd00:0::0005"]},
                "fa:16:3e:00:00:02": {"network": "rhoso-dmz"},
            }
        }
    )
    entries = plan.nic_overrides["vm-1"]
    assert sorted(entries) == ["fa:16:3e:00:00:01", "fa:16:3e:00:00:02"]
    assert entries["fa:16:3e:00:00:01"].fixed_ips == ["fd00::5"]
    assert entries["fa:16:3e:00:00:02"].fixed_ips is None
    assert invalid_plan_settings(plan) == []
    assert make_plan().nic_overrides == {}


def test_plan_nic_overrides_refuse_what_cannot_be_applied():
    """SDD §12: a key that is no MAC, an entry with neither field, an empty address list, a value
    that is no IP address and the same address twice are refused (422)."""
    from seamless_migrate.domain.models import invalid_plan_settings

    plan = make_plan(
        nic_overrides={
            "vm-1": {
                "not-a-mac": {"network": "rhoso-dmz"},
                "fa:16:3e:00:00:01": {},
                "fa:16:3e:00:00:02": {"fixed_ips": []},
                "fa:16:3e:00:00:03": {"fixed_ips": ["10.0.0.300", "10.0.0.5", "10.0.0.5"]},
                "fa:16:3e:00:00:04": {"network": ""},
            }
        }
    )
    problems = invalid_plan_settings(plan)
    assert problems == [
        "nic_overrides[vm-1][fa:16:3e:00:00:01]: give a network, addresses or both",
        "nic_overrides[vm-1][fa:16:3e:00:00:02]: fixed_ips must list at least one address "
        "(leave it out to keep the NIC's own)",
        "nic_overrides[vm-1][fa:16:3e:00:00:03]: not an IP address: 10.0.0.300",
        "nic_overrides[vm-1][fa:16:3e:00:00:03]: 10.0.0.5 given twice",
        "nic_overrides[vm-1][fa:16:3e:00:00:04]: network must not be empty",
        "nic_overrides[vm-1][not-a-mac]: not a MAC address",
    ]


def test_plan_defaults_match_sdd():
    plan = make_plan()
    assert plan.default_strategy == "auto"
    assert plan.selection_policy == "min_downtime"
    assert plan.downtime_slo_s == 600
    assert plan.require_approval is True and plan.auto_cutover is False
    assert plan.keep_warm_interval_s == 900
    assert plan.convergence_threshold_bytes == 1073741824
    assert plan.max_sync_passes == 5
    assert plan.link_bps == 131072000.0
    assert plan.estimator_overrides == {}
    assert plan.prestage_resources == [
        "networks",
        "subnets",
        "routers",
        "router_interfaces",
        "security_groups",
        "security_group_rules",
    ]
    assert plan.status == PlanStatus.draft
    assert plan.verification.console_success_patterns == [
        "login:",
        "Cloud-init v\\. .* finished",
        "Reached target .*Multi-User",
    ]
    assert plan.verification.timeout_s == 600
    assert plan.handover.enabled is False


def test_role_ordering():
    assert Role.viewer.rank < Role.operator.rank < Role.approver.rank < Role.admin.rank
    assert Role.approver.at_least(Role.operator)
    assert not Role.viewer.at_least(Role.operator)


def test_plan_policy_bounds():
    import pytest
    from pydantic import ValidationError

    assert make_plan(keep_warm_interval_s=60).keep_warm_interval_s == 60
    with pytest.raises(ValidationError):
        make_plan(keep_warm_interval_s=10)  # back-to-back delta passes
    with pytest.raises(ValidationError):
        make_plan(downtime_slo_s=0)


def test_vmref_keeps_the_names_a_cutover_reads_and_replaces_in_its_free_text():
    """SDD §4.2, §11 (reviews RV48-05, RV48-08, RV49-02): a VM's strings were stripped of NUL as
    the model was built - a name the source does not have, which os-migrate's export filter would
    match on another VM, and two tags merged into one. The names a cutover reads stay as the source
    gives them (the store refuses one holding a NUL); the free text no filter or mapping reads - a
    tenant's metadata, which Nova lets hold any string, an image's os_type, the specs, the host -
    is written U+FFFD, so one tenant's value never fails a plan's validation, and two tags stay
    two."""
    vm = make_vm(
        name="web\x00-01",
        tags={"ow\x00ner": "fin\x00ance", "owner": "ops", "note": "\ud83d"},
        flavor_extra_specs={"hw:cpu\ud800": "a\x00"},
        os_type="lin\x00ux",
        host="cmp\ud800-1",
    )
    assert vm.name == "web\x00-01"
    assert (vm.tags, vm.flavor_extra_specs, vm.os_type, vm.host) == (
        {"ow\ufffdner": "fin\ufffdance", "owner": "ops", "note": "\ufffd"},
        {"hw:cpu\ufffd": "a\ufffd"},
        "lin\ufffdux",
        "cmp\ufffd-1",
    )


def test_keep_sync_history_drops_the_oldest_passes_between_the_first_and_the_latest():
    """SDD §5.4: the first max_sync_passes passes and the latest 20 are kept; the bytes of a
    dropped pass move to sync_bytes_dropped and pass numbers keep counting."""
    from seamless_migrate.domain.models import SyncPass, keep_sync_history, next_pass_number, utcnow

    m = make_migration()
    assert next_pass_number(m) == 1
    for n in range(1, 31):
        kind = "full" if n == 1 else "delta"
        m.sync_passes.append(
            SyncPass(number=n, kind=kind, started_at=utcnow(), bytes_transferred=n)
        )
    keep_sync_history(m, keep_first=5)
    assert [p.number for p in m.sync_passes] == [1, 2, 3, 4, 5, *range(11, 31)]
    assert m.sync_bytes_dropped == sum(range(6, 11))
    assert next_pass_number(m) == 31
    keep_sync_history(m, keep_first=5)  # at the bound nothing more is dropped
    assert len(m.sync_passes) == 25 and m.sync_bytes_dropped == sum(range(6, 11))


def test_prestage_resources_name_resources_os_migrate_can_pre_stage():
    """SDD §4.2: an entry without an export/import playbook pair (a typo) or naming the
    workloads themselves is refused; the defaults and "flavors" pass."""
    from seamless_migrate.domain.models import DEFAULT_PRESTAGE, invalid_plan_settings

    assert invalid_plan_settings(make_plan(prestage_resources=[*DEFAULT_PRESTAGE, "flavors"])) == []
    assert invalid_plan_settings(make_plan(prestage_resources=[])) == []
    assert invalid_plan_settings(
        make_plan(prestage_resources=["networks", "flavours", "workloads"])
    ) == ["prestage_resources: cannot pre-stage 'flavours', 'workloads'"]


def test_prestage_resources_are_the_playbook_pairs_of_the_collection():
    from pathlib import Path

    from seamless_migrate.domain.models import PRESTAGE_RESOURCES

    playbooks = Path(__file__).resolve().parents[2] / "playbooks"
    pairs = {
        p.stem.removeprefix("export_")
        for p in playbooks.glob("export_*.yml")
        if (playbooks / ("import_" + p.stem.removeprefix("export_") + ".yml")).exists()
    }
    assert set(PRESTAGE_RESOURCES) == pairs - {"workloads"}


LABEL_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "provider_name_cases.json").read_text(encoding="utf-8")
)["label_cases"]


@pytest.mark.parametrize("case", LABEL_CASES, ids=[c["note"] for c in LABEL_CASES])
def test_a_provider_stored_with_a_blank_name_loads_and_is_named_by_its_id(case):
    """SDD §1 naming, §4.2 (reviews RV20-11, RV21-08): the API refuses a blank name at the request
    boundary, so a provider stored blank before keeps loading; a text names it by its id - blank
    by the rule the dashboard shares (provider_name_cases.json): no character outside Unicode's
    separators, controls and format characters."""
    stored = make_provider(id="dst-case", name="RHOSO").model_dump(mode="json")
    loaded = Provider.model_validate({**stored, "name": case["name"]})
    assert loaded.name == case["name"] and loaded.label == case["label"]


BLANK = json.loads(
    (Path(__file__).parent / "fixtures" / "blank_ranges.json").read_text(encoding="utf-8")
)


def test_the_blank_rule_is_one_pinned_list_never_the_runtimes_database(monkeypatch):
    """SDD §4.2 (review RV22-06): blank is Unicode 15.1's separators, controls and format
    characters, one pinned list the dashboard reads too - never the runtime's own database, which
    differs between Python 3.11 (the image) and newer: U+13439 is blank on every runtime."""
    assert [list(r) for r in BLANK_RANGES] == BLANK["ranges"]
    pinned = {cp for first, last in BLANK["ranges"] for cp in range(first, last + 1)}

    def read(ch):
        raise AssertionError(f"the runtime's database was read for {ch!r}")

    monkeypatch.setattr(unicodedata, "category", read)
    assert [cp for cp in range(0x110000) if is_blank(chr(cp)) != (cp in pinned)] == []
    assert is_blank("\U00013439 \u200b") and not is_blank("\U00013439x")


@pytest.mark.skipif(
    unicodedata.unidata_version != BLANK["unicode"], reason="another Unicode version"
)
def test_the_pinned_blank_list_is_what_its_unicode_version_says():
    """The fixture is Unicode 15.1's Z, Cc and Cf: regenerate it on a runtime of that version."""
    categories = {"Zs", "Zl", "Zp", "Cc", "Cf"}
    pinned = {cp for first, last in BLANK["ranges"] for cp in range(first, last + 1)}
    assert pinned == {cp for cp in range(0x110000) if unicodedata.category(chr(cp)) in categories}


def test_vmref_gpus_default_to_an_empty_list_and_round_trip():
    """SDD §4.2, §22.2 (plan task G1): a VM has no GPU unless its source reports one; its GPUs
    survive the store's JSON, and a VM recorded before 0.3.0 loads with none."""
    assert make_vm().gpus == []
    vm = make_vm(
        gpus=[
            GpuDevice(kind="vgpu", profile="grid_a100-4c", count=2),
            GpuDevice(kind="passthrough", vendor_id="10de", device_id="20b5"),
        ]
    )
    assert VMRef.model_validate(vm.model_dump(mode="json")) == vm
    recorded = make_vm().model_dump(mode="json")
    recorded.pop("gpus")
    assert VMRef.model_validate(recorded).gpus == []


def test_a_vmref_keeps_its_projects_id():
    """SDD §4.2, §27.4 (plan task TC4): an OpenStack VM carries its project's Keystone id beside
    its project's name; the id survives the store's JSON as the source gave it - a rule compares
    it, so it is never written U+FFFD as free text is -, and a VMware VM, or one recorded before,
    carries none."""
    tenant_a = secrets.token_hex(16)  # a Keystone id, made up for the test
    assert make_vm().project_id is None
    vm = make_vm(project="tenant-a", project_id=tenant_a)
    assert (vm.project, vm.project_id) == ("tenant-a", tenant_a)
    stored = vm.model_dump(mode="json")
    assert stored["project_id"] == tenant_a
    assert VMRef.model_validate(stored) == vm
    stored.pop("project_id")
    assert VMRef.model_validate(stored).project_id is None
    assert make_vm(project_id="ten\x00ant").project_id == "ten\x00ant"


def test_mappings_gpus_is_a_map_of_strings():
    """SDD §4.2, §22.4: a VMware GPU key to a destination GPU flavor, as the other mappings map a
    name to a name; a plan stored before 0.3.0 loads with none."""
    assert Mappings().gpus == {}
    mappings = Mappings(gpus={"grid_a100-4c": "g1.a100-4c", "10de:20b5": "p1.a100"})
    assert make_plan(mappings=mappings).mappings.gpus["10de:20b5"] == "p1.a100"
    with pytest.raises(ValidationError, match="gpus"):
        Mappings(gpus={"grid_a100-4c": ["g1.a100-4c"]})
    stored = make_plan().model_dump(mode="json")
    stored["mappings"] = {"networks": {"app-net": "prod"}}
    assert Plan.model_validate(stored).mappings.gpus == {}


def test_provider_gpu_profiles_is_refused_on_a_vmware_provider():
    """SDD §4.2, §22.3: vCenter reports a VM's own vGPU profile, so a vmware provider declares
    none; an OpenStack or RHOSO provider, source or destination, may."""
    vmware = {"id": "vc-01", "kind": ProviderKind.vmware, "endpoint": "https://vcenter.example"}
    assert make_provider(**vmware).gpu_profiles == {}
    with pytest.raises(ValidationError, match="gpu_profiles"):
        make_provider(**vmware, gpu_profiles={"g1.a100-4c": "A100-4C"})
    profiles = {"g1.a100-4c": "A100-4C"}
    assert make_provider(gpu_profiles=profiles).gpu_profiles == profiles
    destination = make_provider(
        id="dst-rhoso",
        kind=ProviderKind.rhoso,
        role=ProviderRole.destination,
        gpu_profiles=profiles,
    )
    assert destination.gpu_profiles == profiles


def test_provider_gpu_profiles_bounds_its_profile_names():
    """SDD §4.2: a flavor name of 1-255 characters to a profile name of 1-64."""
    longest = {"f" * 255: "p" * 64, "g1.a100-4c": "A100-4C"}
    assert make_provider(gpu_profiles=longest).gpu_profiles == longest
    for bad in ({"": "A100-4C"}, {"f" * 256: "A100-4C"}, {"g1": ""}, {"g1": "p" * 65}):
        with pytest.raises(ValidationError, match="gpu_profiles"):
            make_provider(gpu_profiles=bad)


def test_a_gpu_devices_ids_and_count_are_validated():
    """SDD §4.2: a kind of the two, PCI ids of four lower-case hex digits, at least one device."""
    device = GpuDevice(kind="passthrough", vendor_id="10de", device_id="20b5")
    assert (device.count, device.profile, device.traits, device.same_provider) == (
        1,
        None,
        [],
        False,
    )
    for bad in (
        {"kind": "mig"},
        {"vendor_id": "10DE"},
        {"vendor_id": "10d"},
        {"device_id": "20b5x"},
        {"count": 0},
    ):
        with pytest.raises(ValidationError):
            GpuDevice(**{"kind": "passthrough", **bad})


def test_a_gpu_devices_free_text_is_written_u_fffd():
    """SDD §4.2 (§11, review RV49-02): a GPU's profile, alias and traits are free text, written
    U+FFFD where they hold a NUL or a lone surrogate, as the model is built."""
    device = GpuDevice(
        kind="vgpu",
        profile="grid\x00a100-4c",
        traits=["CUSTOM_\ud800X"],
        forbidden_traits=["CUSTOM_\x00"],
        alias="a\x00",
    )
    assert device.profile == "grid\ufffda100-4c"
    assert device.traits == ["CUSTOM_\ufffdX"]
    assert device.forbidden_traits == ["CUSTOM_\ufffd"]
    assert device.alias == "a\ufffd"
    assert make_vm(gpus=[device]).gpus[0].profile == "grid\ufffda100-4c"


def test_the_migration_summary_carries_the_gpu_wait():
    """SDD §4.2 (§5.4 rule 5, §22.7): a migration whose cutover waits for GPU capacity holds its
    GpuWait - through the store's JSON, its free text as the store keeps it - and its summary
    carries it as the migration has it; one stored before 0.3.0 loads waiting for nothing."""
    from seamless_migrate.domain.models import GpuWait, MigrationSummary

    at = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    wait = GpuWait(
        since=at,
        checked_at=at,
        pool="VGPU CUSTOM_NVIDIA_A100_4C",
        needed=2,
        free=1,
        reason="VGPU CUSTOM_NVIDIA_A100_4C of RHOSO 18.0 has 1 unit free; it needs 2",
    )
    m = make_migration(phase=Phase.ready, gpu_wait=wait)
    assert m.gpu_wait == wait
    assert Migration.model_validate(m.model_dump(mode="json")).gpu_wait == wait
    assert MigrationSummary.of(m).gpu_wait == wait
    assert MigrationSummary.of(m).model_dump(mode="json")["gpu_wait"] == wait.model_dump(
        mode="json"
    )
    stored = make_migration().model_dump(mode="json")
    stored.pop("gpu_wait")
    assert Migration.model_validate(stored).gpu_wait is None
    assert MigrationSummary.of(Migration.model_validate(stored)).gpu_wait is None
    failed = GpuWait(
        since=at, checked_at=at, pool="\x00", needed=0, free=None, reason="read\ud800 failed"
    )
    assert (failed.pool, failed.free, failed.reason) == ("�", None, "read� failed")
    with pytest.raises(ValidationError):
        GpuWait(since=at, checked_at=at, pool="VGPU", needed=1, free=0)  # a wait says why


def test_a_plans_network_cutover_settings_round_trip():
    """SDD §4.2 (plan task N3): a plan's network cut-over settings - the hooks it runs per point,
    the floating IP move, a DNS target per VM and the Octavia pool mapping - are stored and read
    back as given; a plan without them, or stored before they existed, has the defaults; a DNS
    target that lacks what its kind needs, a point named twice or an unknown point is refused."""
    from seamless_migrate.domain.models import DnsTarget, NetworkCutover, Plan
    from tests.factories import make_plan

    settings = NetworkCutover(
        hooks={"pre_stop": ["drain-lb"], "post_create": ["dns-swap", "drain-lb"]},
        floating_ips="keep_address",
        dns_targets={
            "vm-1": DnsTarget(kind="designate", zone="example.com.", record="web-01"),
            "vm-2": DnsTarget(
                kind="rfc2136",
                zone="example.org.",
                record="db",
                server="ns1.example.org:53",
                tsig_secret="tsig-example",
            ),
        },
        lb_pools={"pool-src-1": "pool-dst-9"},
    )
    plan = make_plan(network_cutover=settings)
    again = Plan.model_validate_json(plan.model_dump_json())
    assert again.network_cutover == settings
    assert json.loads(plan.model_dump_json())["network_cutover"] == {
        "hooks": {"pre_stop": ["drain-lb"], "post_create": ["dns-swap", "drain-lb"]},
        "floating_ips": "keep_address",
        "dns_targets": {
            "vm-1": {
                "kind": "designate",
                "zone": "example.com.",
                "record": "web-01",
                "server": None,
                "tsig_secret": None,
            },
            "vm-2": {
                "kind": "rfc2136",
                "zone": "example.org.",
                "record": "db",
                "server": "ns1.example.org:53",
                "tsig_secret": "tsig-example",
            },
        },
        "lb_pools": {"pool-src-1": "pool-dst-9"},
    }
    # the defaults: os-migrate's floating IPs, no hook, no DNS target, no pool mapping
    assert make_plan().network_cutover == NetworkCutover()
    assert NetworkCutover().model_dump(mode="json") == {
        "hooks": {},
        "floating_ips": "auto",
        "dns_targets": {},
        "lb_pools": {},
    }
    stored = json.loads(make_plan().model_dump_json())
    del stored["network_cutover"]
    assert Plan.model_validate(stored).network_cutover == NetworkCutover()

    refused = [
        {"hooks": {"pre_start": ["drain-lb"]}},
        {"hooks": {"pre_stop": ["drain-lb", "drain-lb"]}},
        {"hooks": {"pre_stop": [""]}},
        {"floating_ips": "move"},
        {
            "dns_targets": {
                "vm-1": {
                    "kind": "designate",
                    "zone": "example.com.",
                    "record": "web",
                    "server": "ns1",
                }
            }
        },
        {
            "dns_targets": {
                "vm-1": {
                    "kind": "designate",
                    "zone": "example.com.",
                    "record": "web",
                    "tsig_secret": "t",
                }
            }
        },
        {
            "dns_targets": {
                "vm-1": {
                    "kind": "rfc2136",
                    "zone": "example.com.",
                    "record": "web",
                    "tsig_secret": "t",
                }
            }
        },
        {
            "dns_targets": {
                "vm-1": {
                    "kind": "rfc2136",
                    "zone": "example.com.",
                    "record": "web",
                    "server": "ns1",
                }
            }
        },
        {
            "dns_targets": {
                "vm-1": {
                    "kind": "rfc2136",
                    "zone": "example.com.",
                    "record": "web",
                    "server": "ns1",
                    "tsig_secret": "../key",
                }
            }
        },
        {"dns_targets": {"vm-1": {"kind": "designate", "zone": "", "record": "web"}}},
        {"lb_pools": {"pool-src-1": ""}},
    ]
    for body in refused:
        with pytest.raises(ValidationError):
            NetworkCutover.model_validate(body)


def test_a_sync_pass_keeps_its_extent_use_per_device():
    """SDD §4.2, §28.5 (plan task CE7): a pass records how it used each disk's extents - the
    mode, the reason, the query's time, the misses and whether it assumed zeros -, {} for a plan
    that does not track extents, and round-trips through JSON."""
    from seamless_migrate.domain.models import ExtentUse, SyncPass, utcnow

    use = {
        "/dev/vda": ExtentUse(mode="checked", query_s=1.5, missed=2),
        "/dev/vdb": ExtentUse(mode="scanned", reason="first_pass", assumed_zero=True),
    }
    sync_pass = SyncPass(number=1, kind=SyncPassKind.delta, started_at=utcnow(), extents=use)
    assert SyncPass.model_validate_json(sync_pass.model_dump_json()) == sync_pass
    assert sync_pass.extents["/dev/vda"].reason is None
    assert sync_pass.extents["/dev/vdb"].model_dump() == {
        "mode": "scanned",
        "reason": "first_pass",
        "query_s": None,
        "missed": 0,
        "assumed_zero": True,
    }
    assert SyncPass(number=2, kind=SyncPassKind.final, started_at=utcnow()).extents == {}
    with pytest.raises(ValueError):
        ExtentUse(mode="guessed")
    with pytest.raises(ValueError):
        ExtentUse(mode="checked", missed=-1)


def test_an_email_channel_needs_recipients_and_takes_no_url():
    """SDD §4.2, §24.3 (plan task NT2): an email channel is one audience - 1-50 recipients, each an
    RFC 5322 addr-spec in its dot-atom form of at most 254 characters - and names no URL; its
    secret is notify-{name}, which the admin never chooses."""
    from seamless_migrate.domain.models import NotificationChannel

    channel = NotificationChannel(
        name="app-owners", kind="email", recipients=["owners@example.com", "ops.team@example.org"]
    )
    assert channel.id == "app-owners"
    assert channel.secret_name == "notify-app-owners"
    assert channel.url is None and channel.description is None
    assert channel.redact_names is False and channel.enabled is True
    assert channel.created_at.tzinfo is not None and channel.updated_at >= channel.created_at
    fifty = [f"owner-{i}@example.com" for i in range(50)]
    assert NotificationChannel(name="many", kind="email", recipients=fifty).recipients == fifty
    longest = "a" * (254 - len("@example.com")) + "@example.com"
    assert NotificationChannel(name="long", kind="email", recipients=[longest]).recipients == [
        longest
    ]

    refused = {
        "no recipient": {"recipients": []},
        "missing": {},
        "51 recipients": {"recipients": fifty + ["one-more@example.com"]},
        "a URL": {"recipients": ["owners@example.com"], "url": "https://mail.example.com/"},
        "no at sign": {"recipients": ["owners.example.com"]},
        "no local part": {"recipients": ["@example.com"]},
        "no domain": {"recipients": ["owners@"]},
        "a space": {"recipients": ["app owners@example.com"]},
        "a name-addr": {"recipients": ["Owners <owners@example.com>"]},
        "two at signs": {"recipients": ["owners@example.com@example.org"]},
        "a dot twice": {"recipients": ["app..owners@example.com"]},
        "a leading dot": {"recipients": [".owners@example.com"]},
        "255 characters": {"recipients": ["a" + longest]},
        "a control character": {"recipients": ["owners@example.com\n"]},
    }
    for why, body in refused.items():
        with pytest.raises(ValidationError, match="recipients|url"):
            NotificationChannel(name="app-owners", kind="email", **body)
        print("refused:", why)
    # the message names the field, never the address count alone
    with pytest.raises(ValidationError, match="recipients: an email channel needs 1-50"):
        NotificationChannel(name="app-owners", kind="email", recipients=[])


def test_a_webhook_channels_url_is_https_without_user_information():
    """SDD §4.2, §24.3 (plan task NT2): a webhook channel POSTs to its URL, which follows a webhook
    hook's rule - https://, a host, no user information - and takes no recipients; the signing key
    lives in its secret alone, so the document has no field for it."""
    from seamless_migrate.domain.models import NotificationChannel

    channel = NotificationChannel(name="cmdb", kind="webhook", url="https://cmdb.example.com/hook")
    assert channel.url == "https://cmdb.example.com/hook" and channel.recipients == []
    assert channel.secret_name == "notify-cmdb"
    assert "signing_key" not in NotificationChannel.model_fields
    assert "secret" not in NotificationChannel.model_fields

    refused = {
        "missing": {},
        "http": {"url": "http://cmdb.example.com/hook"},
        "user information": {"url": "https://token@cmdb.example.com/hook"},
        "a password": {"url": "https://svc:s3cret@cmdb.example.com/hook"},
        "no host": {"url": "https:///hook"},
        "not a URL": {"url": "cmdb.example.com/hook"},
        "recipients": {"url": "https://cmdb.example.com/hook", "recipients": ["a@example.com"]},
    }
    for why, body in refused.items():
        with pytest.raises(ValidationError, match="url|recipients"):
            NotificationChannel(name="cmdb", kind="webhook", **body)
        print("refused:", why)


def test_a_slack_channel_holds_no_url_in_its_document():
    """SDD §4.2, §24.3 (plan task NT2): a Slack webhook's URL is the credential, so a Slack
    channel's document carries neither a URL nor recipients - the URL lives in the secret
    notify-{name} as its key `url` (plan task NT3 writes it)."""
    from seamless_migrate.domain.models import NotificationChannel

    channel = NotificationChannel(name="ops-slack", kind="slack", description="#ops")
    assert channel.url is None and channel.recipients == []
    assert channel.secret_name == "notify-ops-slack"
    document = json.loads(channel.model_dump_json())
    assert document["url"] is None and document["recipients"] == []
    assert "hooks.slack.com" not in channel.model_dump_json()
    for body in (
        {"url": "https://hooks.slack.com/services/T000/B000/XXXX"},
        {"url": "https://example.com/"},
        {"recipients": ["ops@example.com"]},
    ):
        with pytest.raises(ValidationError, match="url|recipients"):
            NotificationChannel(name="ops-slack", kind="slack", **body)
    # the disabled and the redacting channel are documents like any other
    quiet = NotificationChannel(name="quiet", kind="slack", enabled=False, redact_names=True)
    assert quiet.enabled is False and quiet.redact_names is True
    assert NotificationChannel.model_validate(json.loads(quiet.model_dump_json())) == quiet


def test_a_channel_name_follows_the_hook_name_rule():
    """SDD §4.2, §24.3 (plan task NT2): an admin registers channels by name under the rule of a
    hook's name (^[a-z0-9][a-z0-9-]{1,62}$), so a secret named after one is a secret name and
    the kind stays what it was registered as."""
    from seamless_migrate.domain.models import HOOK_NAME, Hook, NotificationChannel

    assert NotificationChannel.model_fields["name"].metadata[0].pattern == HOOK_NAME
    for name in ("ops", "a1", "app-owners-2", "x" * 63, "0-9"):
        assert NotificationChannel(name=name, kind="slack").name == name
        assert Hook(name=name, kind="playbook", playbook="/h.yml").name == name
    for name in ("", "a", "Ops", "-ops", "ops_x", "ops.x", "ops x", "x" * 64, "ops/x", "ops\n"):
        with pytest.raises(ValidationError, match="name"):
            NotificationChannel(name=name, kind="slack")
        with pytest.raises(ValidationError, match="name"):
            Hook(name=name, kind="playbook", playbook="/h.yml")
    with pytest.raises(ValidationError, match="kind"):
        NotificationChannel(name="ops", kind="teams")


def test_a_subscription_defaults_to_approval_downtime_and_problem():
    """SDD §4.1, §4.2, §24.4, §24.5 (plan task NT2): a subscription names a channel, a plan or every
    plan (null) and its topics - approval, downtime and problem unless it says otherwise, at least
    one and each once; a cursor starts at 0 with no outcome; each trigger's topic is the part of
    its name before the dot."""
    from seamless_migrate.domain.enums import NotifyTopic, NotifyTrigger
    from seamless_migrate.domain.models import NotificationCursor, NotificationSubscription

    sub = NotificationSubscription(channel="app-owners", plan_id=None, created_by="admin")
    assert sub.topics == [NotifyTopic.approval, NotifyTopic.downtime, NotifyTopic.problem]
    assert sub.plan_id is None and sub.created_by == "admin"
    assert unicodedata.category(sub.id[0]) == "Ll" and len(sub.id) == len("sub-") + 8
    assert sub.id.startswith("sub-") and int(sub.id[4:], 16) >= 0
    assert json.loads(sub.model_dump_json())["topics"] == ["approval", "downtime", "problem"]
    chosen = NotificationSubscription(
        channel="ops-slack",
        plan_id="plan-0000abcd",
        topics=["progress", "problem"],
        created_by="op",
    )
    assert chosen.topics == [NotifyTopic.progress, NotifyTopic.problem]  # in the order given
    for body in (
        {"topics": []},
        {"topics": ["approval", "approval"]},
        {"topics": ["digest"]},
        {"channel": "Ops"},
    ):
        with pytest.raises(ValidationError, match="topics|channel"):
            NotificationSubscription(
                **{"channel": "app-owners", "plan_id": None, "created_by": "admin", **body}
            )

    cursor = NotificationCursor(channel="app-owners", seq=0)
    assert cursor.id == "app-owners"
    assert cursor.last_outcome is None and cursor.last_outcome_at is None
    assert cursor.last_reason is None and cursor.failures_in_a_row == 0
    with pytest.raises(ValidationError, match="seq"):
        NotificationCursor(channel="app-owners", seq=-1)
    with pytest.raises(ValidationError, match="last_outcome"):
        NotificationCursor(channel="app-owners", seq=1, last_outcome="dropped")

    assert [t.value for t in NotifyTopic] == [
        "approval",
        "downtime",
        "problem",
        "outcome",
        "progress",
    ]
    assert len(NotifyTrigger) == 18
    assert NotifyTrigger.approval_cutover.value == "approval.cutover"
    assert NotifyTrigger("progress.wave_completed").topic == NotifyTopic.progress
    assert {trigger.topic for trigger in NotifyTrigger} == set(NotifyTopic)
    assert NotifyTrigger("downtime.ended").topic == NotifyTopic.downtime
