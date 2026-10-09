"""Rehearsal of the lab module in CI: the three lab tests run end to end through the real
provider and executor code (clouds.yaml parsing, openstack.connect, OpenStackProvider, the
readiness report, cutover and rollback), with the strict Cinder/Nova doubles of
test_executor_handover behind openstack.connect. It proves the lab module's wiring; the lab
itself proves the clouds (QASuite LAB-H07…H10)."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from tests.lab import test_lab_storage_handover as lab_module
from tests.test_executor_handover import (
    NFS_DST,
    NFS_SRC,
    ONTAP_NFS,
    FakeCloud,
    source_cloud,
)


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    openstack = pytest.importorskip("openstack")
    clouds_yaml = tmp_path / "clouds.yaml"
    clouds_yaml.write_text(
        "clouds:\n"
        "  rhosp17:\n    auth:\n      auth_url: https://keystone.rhosp17.example/v3\n"
        "      username: admin\n      password: pw\n      project_name: admin\n"
        "  rhoso18:\n    auth:\n      auth_url: https://keystone.rhoso18.example/v3\n"
        "      username: admin\n      password: pw\n      project_name: admin\n"
    )
    calls: list = []
    src = source_cloud(calls)
    for volume in src.volumes.values():
        volume.host = NFS_SRC
    src.pools = [(NFS_SRC, ONTAP_NFS)]
    dst = FakeCloud("dst", calls, pools=[(NFS_DST, ONTAP_NFS)])
    for cloud in (src, dst):
        cloud.compute.hypervisors = lambda: [NS(name="compute-0")]
        cloud.network.agents = lambda **_: [NS(agent_type="OVN Controller agent", binary="ovn")]
        cloud.compute.find_server = lambda name, ignore_missing=True: None

    def connect(**kwargs):
        return src if "rhosp17" in kwargs["auth"]["auth_url"] else dst

    monkeypatch.setattr(openstack, "connect", connect)
    env = {
        "SEAMLESS_LAB_CLOUDS_YAML": str(clouds_yaml),
        "SEAMLESS_LAB_SOURCE_CLOUD": "rhosp17",
        "SEAMLESS_LAB_DEST_CLOUD": "rhoso18",
        "SEAMLESS_LAB_HANDOVER_SERVER": "srv-1",
        "SEAMLESS_LAB_BACKEND_MAP": json.dumps(
            {"ceph-ssd": "hostgroup@ontap_nfs", "ceph-hdd": "hostgroup@ontap_nfs"}
        ),
        "SEAMLESS_LAB_DESTRUCTIVE": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return src, dst, calls


async def test_lab_module_runs_end_to_end_against_the_doubles(rehearsal, tmp_path):
    src, dst, calls = rehearsal
    lab = lab_module.lab.__wrapped__(tmp_path)  # the fixture body, with the rehearsal environment
    await lab_module.test_lab_cinder_pools_are_classified_by_driver_family(lab)
    await lab_module.test_lab_handover_readiness_of_the_test_server(lab)
    assert calls == [], "pool listing and readiness change nothing"
    await lab_module.test_lab_handover_cutover_then_rollback(lab)
    ops = [(cloud, op) for cloud, op, _ in calls]
    assert ("dst", "manage") in ops and ("dst", "create_server") in ops
    assert ops[-1] == ("src", "create_server"), "rolled back: the source server was recreated"
    assert src.deleted_by_nova == []
