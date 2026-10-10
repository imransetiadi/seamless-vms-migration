"""Application probes: SDD §9.6 suggestions and the probes a VM's verification runs (§7.5)."""

import pytest

from seamless_migrate.domain.models import AppProbe, VerificationConfig
from seamless_migrate.planning.probes import describe_probe, is_database, probes_for, suggest_probes
from tests.factories import make_plan, make_vm


def tcp(port):
    return AppProbe(kind="tcp", port=port)


def http(port, path="/"):
    return AppProbe(kind="http", port=port, path=path)


@pytest.mark.parametrize(
    ("name", "tags", "os_type", "expected"),
    [
        # the demo's finance databases name their engine in a role tag
        ("ledger-db-01", {"role": "postgresql"}, "rhel9", [tcp(5432)]),
        ("pg-orders", {}, "rhel9", [tcp(5432)]),
        ("payroll-db-01", {"role": "mysql"}, "rhel9", [tcp(3306)]),
        ("galera-02", {}, "rhel9", [tcp(3306)]),
        ("billing-db-01", {"role": "oracle"}, "rhel9", [tcp(1521)]),
        ("erp-sqlserver", {}, "windows2019srvNext_64Guest", [tcp(1433)]),
        ("events-mongo-1", {}, "rhel9", [tcp(27017)]),
        ("metrics-cassandra", {}, "rhel9", [tcp(9042)]),
        ("ledger-db2", {}, "rhel9", [tcp(50000)]),
        ("search-elastic-01", {}, "rhel9", [http(9200)]),
        ("docs-couchdb", {}, "rhel9", [http(5984)]),
        ("orders-rabbit-01", {}, "rhel9", [tcp(5672)]),
        ("stream-kafka-1", {}, "rhel9", [tcp(9092)]),
        ("sessions-redis", {}, "rhel9", [tcp(6379)]),
        ("shop-tomcat-01", {}, "rhel9", [http(8080)]),
        ("hr-weblogic", {}, "rhel9", [http(7001)]),
        ("idm-ldap-01", {}, "rhel9", [tcp(389)]),
        ("ns1", {}, "rhel9", [tcp(53)]),
        ("ledger-web-01", {"role": "frontend"}, "rhel9", [http(80)]),
        # a database that names no engine, an old OS, an unknown role: nothing to suggest
        ("dc2-db-01", {}, "rhel8_64Guest", []),
        # the service must be of the VM's tier: the database behind a directory is no LDAP server
        ("ldap-db-01", {}, "rhel9", []),
        ("legacy-rhel6-app", {}, "rhel6", []),
        ("report-01", {}, "rhel9", []),
    ],
)
def test_suggestions_follow_the_tier_and_the_service_a_vm_names(name, tags, os_type, expected):
    assert suggest_probes(make_vm(name=name, tags=tags, os_type=os_type)) == expected


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
