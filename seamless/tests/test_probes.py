"""Application probes: SDD §9.6 suggestions and the probes a VM's verification runs (§7.5)."""

import json
from pathlib import Path

import pytest

from seamless_migrate.domain.models import AppProbe, GpuDevice, VerificationConfig
from seamless_migrate.planning.probes import describe_probe, is_database, probes_for, suggest_probes
from tests.factories import make_plan, make_vm


def tcp(port):
    return AppProbe(kind="tcp", port=port)


def http(port, path="/"):
    return AppProbe(kind="http", port=port, path=path)


CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "probe_suggestion_cases.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_suggestions_follow_the_tier_and_the_service_a_vm_names(case):
    """One case table for the control plane and the dashboard's mock (SDD §9.6)."""
    gpus = [GpuDevice(**gpu) for gpu in case.get("gpus", [])]
    specs = case.get("flavor_extra_specs", {})
    vm = make_vm(
        name=case["name"],
        tags=case["tags"],
        os_type=case["os_type"],
        gpus=gpus,
        flavor_extra_specs=specs,
    )
    assert [p.model_dump() for p in suggest_probes(vm)] == case["probes"]


def test_a_suggestion_is_a_fresh_copy_each_time():
    first = suggest_probes(make_vm(name="ledger-db-01", tags={"role": "postgresql"}))
    first[0].port = 1
    assert suggest_probes(make_vm(name="ledger-db-01", tags={"role": "postgresql"})) == [tcp(5432)]


def test_a_vm_runs_its_override_or_else_the_plans_probes():
    vm = make_vm(source_id="vm-db", name="ledger-db-01")
    plan = make_plan(verification=VerificationConfig(app_probes=[http(80)]))
    assert probes_for(vm, plan) == [http(80)]
    with_override = plan.model_copy(update={"probe_overrides": {"vm-db": [tcp(5432)]}})
    assert probes_for(vm, with_override) == [tcp(5432)]
    # an empty override means no probe for this VM, whatever the plan's default
    assert probes_for(vm, plan.model_copy(update={"probe_overrides": {"vm-db": []}})) == []
    # another VM's override changes nothing here
    assert probes_for(vm, plan.model_copy(update={"probe_overrides": {"vm-x": [tcp(1)]}})) == [
        http(80)
    ]


def test_database_vms_are_those_of_the_stateful_database_tier():
    assert is_database(make_vm(name="dc2-db-01"))
    assert is_database(
        make_vm(name="billing-01", tags={"role": "oracle", "demo-fault": "app-down"})
    )
    assert not is_database(make_vm(name="ledger-web-01", tags={"role": "frontend"}))


def test_probes_are_described_the_way_people_say_them():
    assert describe_probe(tcp(5432)) == "TCP 5432"
    assert describe_probe(http(9200)) == "HTTP 9200 /"
    assert (
        describe_probe(AppProbe(kind="https", port=443, path="/health", expect_status=204))
        == "HTTPS 443 /health (204)"
    )


def test_a_gpu_vm_is_suggested_the_dcgm_exporter_port_besides_its_tiers_probe():
    """SDD §9.6, §22.8 (plan task G5): a GPU VM outside the database tier is also suggested tcp
    9400 - NVIDIA's DCGM exporter serves its metrics there -, after its tier's probe; a VM recorded
    before 0.3.0 is a GPU VM by its flavor's requests. A database VM keeps its engine's probe alone,
    or none: the exporter's port never stands in for the database's, nor rolls a database back
    whose service answers (PR #64 review)."""
    vgpu = [GpuDevice(kind="vgpu", profile="grid_a100-4c")]
    assert suggest_probes(make_vm(name="web-01", gpus=vgpu)) == [http(80), tcp(9400)]
    mq = make_vm(name="ml-queue-01", tags={"role": "rabbitmq"}, gpus=vgpu)
    assert suggest_probes(mq) == [tcp(5672), tcp(9400)]
    assert suggest_probes(make_vm(name="render-01", gpus=vgpu)) == [tcp(9400)]
    for specs in ({"resources:VGPU": "1"}, {"pci_passthrough:alias": "PCI device 0"}):
        assert suggest_probes(make_vm(name="render-02", flavor_extra_specs=specs)) == [tcp(9400)]
    assert (
        suggest_probes(make_vm(name="render-03", flavor_extra_specs={"resources:VGPU": "0"})) == []
    )
    assert suggest_probes(make_vm(name="render-04")) == []
    # a database VM: its engine's probe alone, or none - never the exporter's port
    db = make_vm(name="ledger-db-01", tags={"role": "postgresql"}, gpus=vgpu)
    assert suggest_probes(db) == [tcp(5432)]
    assert suggest_probes(make_vm(name="db-ml-01", gpus=vgpu)) == []
