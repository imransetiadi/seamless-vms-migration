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


def run_prelim(tmp_path, name, existing=()):
    from ansible_collections.os_migrate.os_migrate.plugins.modules import (
        import_workload_prelim as prelim_module,
    )

    dst = fake.FakeCloud("dst")
    for index, server_name in enumerate(existing):
        dst.add_server("dst-%d" % index, server_name)
    args = {
        "cloud": "dst",
        "data": fake.workload_data(name=name),
        "src_conversion_host": {"id": "src-conv"},
        "log_dir": str(tmp_path / "workload_logs"),
    }
    return fake.run_module(prelim_module, args, dst)


@pytest.mark.parametrize(
    ("name", "existing"),
    [("web.01", ["web-01"]), ("db[1]", ["db1"]), ("app(", ["app(x"]), ("vm1", ["vm10", "a-vm1"])],
)
def test_prelim_does_not_skip_for_a_server_of_another_name(tmp_path, name, existing):
    """SDD §6.6: the name is a literal, not a pattern (audit COL-08)."""
    result = run_prelim(tmp_path, name, existing)
    assert result["_exit_code"] in (None, 0), result
    assert result["changed"] is True and "log_file" in result, result


@pytest.mark.parametrize("name", ["web.01", "db[1]", "app(", "vm1"])
def test_prelim_skips_for_a_server_of_exactly_its_name(tmp_path, name):
    result = run_prelim(tmp_path, name, [name])
    assert result["changed"] is False
    assert "already exists on destination, skipping" in result["msg"]


def test_prelim_does_not_skip_for_a_server_a_case_insensitive_nova_matches(tmp_path, monkeypatch):
    """Nova's name filter runs in the database: on MySQL with a case-insensitive
    collation ^web\\-01$ matches WEB-01 too; only a server of exactly the name counts."""
    import re

    def servers_ignoring_case(self, details=True, **query):
        pattern = query.get("name")
        return [s for s in self.cloud.servers.values() if re.search(pattern, s.name, re.IGNORECASE)]

    monkeypatch.setattr(fake._FakeCompute, "servers", servers_ignoring_case)
    result = run_prelim(tmp_path, "web-01", ["WEB-01"])
    assert result["changed"] is True and "log_file" in result, result
