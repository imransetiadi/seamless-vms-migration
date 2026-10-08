from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os

import yaml

ROLE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "roles", "conversion_host")
)


def load(*parts):
    with open(os.path.join(ROLE, *parts), encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_security_group_rules_use_the_configurable_source_prefix():
    rules = [
        task["os_migrate.os_migrate.security_group_rule"]
        for task in load("tasks", "main.yml")
        if "os_migrate.os_migrate.security_group_rule" in task
    ]

    assert {rule["protocol"] for rule in rules} == {"tcp", "icmp"}
    for rule in rules:
        assert rule["remote_ip_prefix"] == "{{ os_migrate_conversion_secgroup_remote_ip_prefix }}"
    # Backward compatible default.
    assert load("defaults", "main.yml")["os_migrate_conversion_secgroup_remote_ip_prefix"] == "0.0.0.0/0"
