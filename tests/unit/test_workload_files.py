from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os

import pytest

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import filesystem
from ansible_collections.os_migrate.os_migrate.tests.unit import fake_openstack as fake


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("web-01", "web-01"),
        ("web 01 (prod)", "web 01 (prod)"),
        ("../../victim/progress", "..%2F..%2Fvictim%2Fprogress"),
        ("/etc/shadow", "%2Fetc%2Fshadow"),
        ("50%/x", "50%25%2Fx"),
        ("a\0b", "a%00b"),
    ],
)
def test_workload_file_stem_keeps_ordinary_names_and_escapes_path_characters(name, stem):
    """SDD §7.2: % -> %25, / -> %2F, NUL -> %00; nothing else changes."""
    assert filesystem.workload_file_stem(name) == stem


def test_workload_file_stem_never_gives_two_names_one_file():
    names = ["a/b", "a%2Fb", "a%252Fb", "a_b"]
    assert len({filesystem.workload_file_stem(n) for n in names}) == len(names)


@pytest.mark.parametrize("name", ["../../victim/progress", "/abs/victim", "vm1"])
def test_prelim_keeps_the_log_and_state_files_in_the_log_dir(tmp_path, name):
    """A tenant's VM name never names a file outside workload_logs (audit COL-07)."""
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_prelim as prelim_module,
    )

    log_dir = str(tmp_path / "osm" / "workload_logs")
    args = {
        "cloud": "dst",
        "data": fake.workload_data(name=name),
        "src_conversion_host": {"id": "src-conv"},
        "log_dir": log_dir,
    }
    result = fake.run_module(prelim_module, args, fake.FakeCloud("dst"))

    assert result["_exit_code"] is None or result["_exit_code"] == 0, result
    for key in ("log_file", "state_file"):
        assert os.path.dirname(os.path.abspath(result[key])) == log_dir, result[key]
    stem = filesystem.workload_file_stem(name)
    assert os.path.basename(result["log_file"]) == stem + ".log"
    assert os.path.basename(result["state_file"]) == stem + ".state"
