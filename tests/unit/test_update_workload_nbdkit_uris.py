"""update_workload_nbdkit_uris records the disks import_from_hypervisor exported
(SEC-01) in workloads.yml. The module types them: before ansible-core 2.19,
"{{ port | int }}" reaches a module as the string "10809"."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import yaml

from ansible_collections.os_migrate.os_migrate.plugins.modules import (
    update_workload_nbdkit_uris as nbdkit_uris_module,
)
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake


def _workloads(tmp_path):
    path = tmp_path / "workloads.yml"
    resource = {
        "type": "openstack.compute.Server",
        "params": {"name": "vm1"},
        "_info": {"id": "srv-1"},
    }
    path.write_text(yaml.safe_dump({"resources": [resource]}))
    return str(path)


def _disk(**overrides):
    # as ansible-core 2.18 renders the role's disk record: port and size are strings
    disk = {
        "device": "/dev/vda",
        "uri": "nbd://hv-test:10809",
        "port": "10809",
        "size": "10",
        "bootable": True,
        "disk_path": "/var/lib/nova/instances/srv-1/disk",
        "disk_name": "disk",
    }
    disk.update(overrides)
    return disk


def _run(path, disks):
    args = {"path": path, "instance_id": "srv-1", "nbdkit_disks": disks}
    return fake.run_module(nbdkit_uris_module, args, None)


def test_disk_port_and_size_are_stored_as_integers(tmp_path):
    path = _workloads(tmp_path)

    second = _disk(device="/dev/vdb", port="10810", size="20", bootable=False)
    result = _run(path, [_disk(), second])

    assert result["_exit_code"] in (None, 0), result
    with open(path, encoding="utf-8") as f:
        disks = yaml.safe_load(f)["resources"][0]["_migration_params"]["nbdkit_disks"]
    typed = [(d["port"], d["size"], d["bootable"]) for d in disks]
    assert typed == [(10809, 10, True), (10810, 20, False)]


def test_an_undeclared_disk_key_is_refused_and_nothing_is_written(tmp_path):
    path = _workloads(tmp_path)
    with open(path, encoding="utf-8") as f:
        before = f.read()

    result = _run(path, [_disk(format="qcow2")])

    assert result["_exit_code"] == 1 and "format" in result["msg"], result
    with open(path, encoding="utf-8") as f:
        assert f.read() == before
