"""Rehearsal of the lab module in CI: the three lab tests run end to end through the real
provider and executor code (clouds.yaml parsing, openstack.connect, OpenStackProvider, the
readiness report, cutover and rollback), with the strict Cinder/Nova doubles of
test_executor_handover behind openstack.connect. It proves the lab module's wiring; the lab
itself proves the clouds (QASuite LAB-H07…H10)."""

from __future__ import annotations

import json
import os
from pathlib import Path
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
    # rolled back: the source server was recreated, its boot volume's delete_on_termination given
    # back once it ran (SDD §7.3, review RV35B-04)
    assert ops[-2:] == [("src", "create_server"), ("src", "dot")], ops
    assert src.deleted_by_nova == []


# ------------------------------------------------- changed-extent tracking (plan task CE13)


def _use(mode: str, reason: str | None = None, missed: int = 0) -> dict:
    query = None if mode == "scanned" else 9.6
    return {
        "/dev/vda": {
            "mode": mode,
            "reason": reason,
            "query_s": query,
            "missed": missed,
            "assumed_zero": False,
        }
    }


@pytest.fixture
def extents_rehearsal(tmp_path, monkeypatch):
    """The changed-extents lab module's environment against doubles: the fake ansible-playbook
    (its passes' extent use scripted per pass through a file the fallback commands rewrite), the
    executor tests' provider registry double, and the provider's check and VM read as the lab's
    source answers them (the handover rehearsal proves the provider code itself)."""
    from seamless_migrate.providers.openstack import OpenStackProvider
    from tests.factories import make_disk, make_vm
    from tests.fake_ansible import install
    from tests.test_executor_ansible import CEPH_FSID, CEPH_KEY

    pytest.importorskip("openstack")
    fake = install(tmp_path / "bin")
    monkeypatch.setenv("ANSIBLE_FAKE_LOG", str(tmp_path / "ansible.log"))
    monkeypatch.setenv("ANSIBLE_FAKE_CREATED", str(tmp_path / "created.txt"))
    monkeypatch.setenv("ANSIBLE_FAKE_SLEEP", "0.01")
    extents = tmp_path / "extents.json"
    monkeypatch.setenv("ANSIBLE_FAKE_EXTENTS", f"@{extents}")
    clouds_yaml = tmp_path / "clouds.yaml"
    clouds_yaml.write_text(
        "clouds:\n"
        "  rhosp17:\n    auth:\n      auth_url: https://keystone.rhosp17.example/v3\n"
        "      username: admin\n      password: pw\n      project_name: admin\n"
        "  rhoso18:\n    auth:\n      auth_url: https://keystone.rhoso18.example/v3\n"
        "      username: admin\n      password: pw\n      project_name: admin\n"
    )
    key_file = tmp_path / "seamless.key"
    key_file.write_text(CEPH_KEY + "\n")  # synthetic, no key material
    # the fallback commands of LAB-CE03 rewrite what the next passes use (the lab's: a deleted base,
    # blocked monitors); the final pass reads again
    scripts = {}
    for reason in ("base_gone", "query_failed"):
        script = tmp_path / f"{reason}.json"
        script.write_text(
            json.dumps({"by_pass": {"default": _use("scanned", reason), "final": _use("read")}})
        )
        scripts[reason] = f"cp {script} {extents}"
    env = {
        "SEAMLESS_LAB_CLOUDS_YAML": str(clouds_yaml),
        "SEAMLESS_LAB_SOURCE_CLOUD": "rhosp17",
        "SEAMLESS_LAB_DEST_CLOUD": "rhoso18",
        "SEAMLESS_LAB_EXTENTS_SERVER": "srv-1",
        "SEAMLESS_LAB_CEPH_FSID": CEPH_FSID,
        "SEAMLESS_LAB_CEPH_MONITORS": "mon1.ceph.example, mon2.ceph.example:6789",
        "SEAMLESS_LAB_CEPH_KEY_FILE": str(key_file),
        "SEAMLESS_LAB_ANSIBLE_PLAYBOOK": str(fake),
        "SEAMLESS_LAB_MAPPINGS": json.dumps({"networks": {"app-net": "rhoso-app"}}),
        "SEAMLESS_LAB_PRESTAGE": "1",
        "SEAMLESS_LAB_DESTRUCTIVE": "1",
        "SEAMLESS_LAB_CHECKSUM_CMD": "printf 'deadbeef\\ndeadbeef\\n'",
        "SEAMLESS_LAB_CE02_PASSES": "3",
        "SEAMLESS_LAB_FALLBACK_CMDS": json.dumps(scripts),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    capabilities = {
        "admin": True,
        "storage_backends": [
            {
                "pool": "hostgroup@ceph#ceph",
                "vendor": "Open Source",
                "protocol": "ceph",
                "family": "rbd",
                "ceph_fsid": CEPH_FSID,
                "ceph_pool": "volumes",
            },
        ],
    }

    async def check(self):
        return capabilities

    async def get_vm(self, source_id):
        disk = make_disk(id="vol-1", device="/dev/vda", pool="hostgroup@ceph#ceph")
        return make_vm(source_id=source_id, name="web-01", disks=[disk])

    monkeypatch.setattr(OpenStackProvider, "check", check)
    monkeypatch.setattr(OpenStackProvider, "get_vm", get_vm)
    return extents


async def test_the_changed_extents_lab_runs_through_the_real_executor_code(
    extents_rehearsal, tmp_path, monkeypatch
):
    """QASuite LAB-CE01…CE03 rehearsed (plan task CE13): each lab test drives the real
    AnsibleExecutor - the Ceph run files, the extent variables, the passes' extent use read from
    the warm state - against the fake ansible-playbook, and a check that missed a write fails the
    lab, which is what it is for."""
    from tests.fake_ansible import read_log
    from tests.lab import test_lab_changed_extents as module
    from tests.test_executor_ansible import FakeRegistry

    extents = extents_rehearsal

    def fresh(name: str) -> module.Lab:
        # the fixture body, with the rehearsal environment; each lab its own destination, so a
        # server one cutover created is not another's "already exists" (SDD §7.2)
        monkeypatch.setenv("ANSIBLE_FAKE_CREATED", str(tmp_path / name / "created.txt"))
        lab = module.lab.__wrapped__(tmp_path / name)
        lab.providers = FakeRegistry()
        return lab

    def scripted(by_pass: dict) -> None:
        extents.write_text(json.dumps({"by_pass": by_pass}))

    scripted(
        {"1": _use("scanned", "first_pass"), "default": _use("checked"), "final": _use("read")}
    )
    await module.test_lab_ce01_a_tracked_final_pass_leaves_equal_checksums(fresh("ce01"))
    scripted({"1": _use("scanned", "first_pass"), "default": _use("checked")})
    await module.test_lab_ce02_no_check_misses_a_write_under_io(fresh("ce02"))
    scripted(
        {"1": _use("scanned", "first_pass"), "default": _use("checked"), "final": _use("read")}
    )
    await module.test_lab_ce03_every_fallback_scans_in_full_and_completes(fresh("ce03"))
    # the real executor code ran each pass: the Ceph files beside secrets.yml, 0600, and the
    # modes per run
    runs = [
        r
        for r in read_log(Path(os.environ["ANSIBLE_FAKE_LOG"]))
        if "os_migrate_warm_extents_precopy" in r["vars"]
        or "os_migrate_warm_extents_final" in r["vars"]
    ]
    assert runs, "no warm run carried an extent variable"
    assert {r["vars"].get("os_migrate_warm_extents_precopy") for r in runs} == {"check", None}
    assert {r["vars"].get("os_migrate_warm_extents_final") for r in runs} == {"read", None}
    assert all(r["ceph_files"] == {"ceph.conf": "0o600", "ceph.keyring": "0o600"} for r in runs), (
        runs[0]["ceph_files"]
    )
    assert not any("key =" in json.dumps(r["vars"]) for r in runs)
    # a check that missed a write fails LAB-CE02: the lab asserts, never only prints
    scripted({"1": _use("scanned", "first_pass"), "default": _use("checked", None, 2)})
    with pytest.raises(AssertionError, match="missed"):
        await module.test_lab_ce02_no_check_misses_a_write_under_io(fresh("ce02-missed"))
