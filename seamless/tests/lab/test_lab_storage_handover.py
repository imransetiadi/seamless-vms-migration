"""Lab verification of storage handover against a real source cloud and RHOSO 18.0
(QASuite LAB-H01…H10, SDD §7.3, §7.3.1). Skipped unless the lab is configured; never runs in CI.

Read-only by default — it lists the Cinder pools by driver family and prints the readiness report
of one server (per volume: family, source pool, RHOSO pool and the exact manage reference):

    SEAMLESS_LAB_CLOUDS_YAML=~/lab/clouds.yaml \\
    SEAMLESS_LAB_SOURCE_CLOUD=rhosp17 SEAMLESS_LAB_DEST_CLOUD=rhoso18 \\
    SEAMLESS_LAB_HANDOVER_SERVER=<id of a disposable test server> \\
    SEAMLESS_LAB_BACKEND_MAP='{"netapp-nfs": "hostgroup@ontap-nfs"}' \\
    .venv/bin/pytest -m lab tests/lab -v -s          # or: make lab-handover

SEAMLESS_LAB_DESTRUCTIVE=1 also hands the server over to RHOSO and rolls it back (LAB-H07…H09):
the server is stopped, deleted and recreated, and its volumes move to RHOSO and back. Use a
disposable test server only.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from seamless_migrate.config import Settings
from seamless_migrate.domain.enums import ProviderKind, ProviderRole, Strategy
from seamless_migrate.domain.models import HandoverConfig, Provider
from seamless_migrate.executors.base import StepName
from seamless_migrate.executors.handover import HandoverExecutor
from tests.executor_support import make_ctx
from tests.factories import make_migration, make_plan

pytestmark = pytest.mark.lab

ENV = (
    "SEAMLESS_LAB_CLOUDS_YAML",
    "SEAMLESS_LAB_SOURCE_CLOUD",
    "SEAMLESS_LAB_DEST_CLOUD",
    "SEAMLESS_LAB_HANDOVER_SERVER",
    "SEAMLESS_LAB_BACKEND_MAP",
)


@pytest.fixture
def lab(tmp_path):
    missing = [name for name in ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(f"lab not configured: {', '.join(missing)}")
    pytest.importorskip("openstack")
    settings = Settings(
        clouds_yaml=Path(os.environ["SEAMLESS_LAB_CLOUDS_YAML"]).expanduser(),
        data_dir=tmp_path / "data",
    )
    source = Provider(
        id="lab-source",
        name="lab source",
        kind=ProviderKind.openstack,
        role=ProviderRole.source,
        # informational: the connection comes from the clouds.yaml entry
        endpoint=f"clouds.yaml:{os.environ['SEAMLESS_LAB_SOURCE_CLOUD']}",
        cloud=os.environ["SEAMLESS_LAB_SOURCE_CLOUD"],
    )
    destination = Provider(
        id="lab-rhoso",
        name="lab RHOSO",
        kind=ProviderKind.rhoso,
        role=ProviderRole.destination,
        endpoint=f"clouds.yaml:{os.environ['SEAMLESS_LAB_DEST_CLOUD']}",
        cloud=os.environ["SEAMLESS_LAB_DEST_CLOUD"],
    )
    backend_map = json.loads(os.environ["SEAMLESS_LAB_BACKEND_MAP"])
    return settings, source, destination, os.environ["SEAMLESS_LAB_HANDOVER_SERVER"], backend_map


async def test_lab_cinder_pools_are_classified_by_driver_family(lab):
    """LAB-P01 for storage: both clouds list their pools with vendor, protocol and family."""
    from seamless_migrate.providers.openstack import OpenStackProvider

    settings, source, destination, _server, _map = lab
    for provider in (source, destination):
        caps = await OpenStackProvider(provider, settings).check()
        print(f"\n{provider.cloud}: " + json.dumps(caps["storage_backends"], indent=2))
        assert caps["admin"], f"{provider.cloud}: handover needs admin credentials"
        assert caps["storage_backends"], f"{provider.cloud}: no Cinder pools listed"


async def test_lab_handover_readiness_of_the_test_server(lab):
    """LAB-H07/H08/H10: the step-0 report of the real server — no change to either cloud."""
    settings, source, destination, server_id, backend_map = lab
    report = await HandoverExecutor(settings).readiness(source, destination, server_id, backend_map)
    print("\n" + json.dumps(report, indent=2))
    assert report["problems"] == [], report["problems"]
    assert all(v["reference"] for v in report["volumes"])


async def test_lab_handover_cutover_then_rollback(lab):
    """LAB-H07…H09 (destructive, opt-in): hand the test server over to RHOSO, check it boots,
    then roll it back and check the source boots again on its volumes."""
    if os.environ.get("SEAMLESS_LAB_DESTRUCTIVE") != "1":
        pytest.skip("set SEAMLESS_LAB_DESTRUCTIVE=1 to move the disposable test server and back")
    from seamless_migrate.providers.openstack import OpenStackProvider

    settings, source, destination, server_id, backend_map = lab
    src = OpenStackProvider(source, settings)
    dst = OpenStackProvider(destination, settings)
    vm = await src.get_vm(server_id)
    plan = make_plan(
        source_provider_id=source.id,
        destination_provider_id=destination.id,
        handover=HandoverConfig(enabled=True, backend_map=backend_map),
    )
    migration = make_migration(plan_id=plan.id, vm=vm, strategy=Strategy.storage_handover)
    executor = HandoverExecutor(settings)

    started = time.monotonic()
    ctx, rec = make_ctx(plan, migration, source, destination, settings)
    result = await executor.run(StepName.CUTOVER, ctx)
    handed_over = time.monotonic() - started
    server = await dst.get_server(result.destination_server_id)
    print(f"\ncutover: {handed_over:.0f} s, destination {result.destination_server_id} {server}")
    assert server["status"] == "ACTIVE" and rec.downtime_marks == 1

    migration.destination_server_id = result.destination_server_id
    back = await executor.run(
        StepName.ROLLBACK, make_ctx(plan, migration, source, destination, settings)[0]
    )
    recreated = (back.details.get("vm") or {}).get("source_id", server_id)
    source_server = await src.get_server(recreated)
    print(f"rollback: source {recreated} {source_server}")
    assert back.details["source_running"] is True and source_server["status"] == "ACTIVE"
