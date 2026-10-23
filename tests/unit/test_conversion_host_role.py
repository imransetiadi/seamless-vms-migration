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


def test_the_keypair_task_takes_its_state_from_each_side():
    """Seamless Migrate SDD 7.2 (issue #112): the role's keypair task takes its state from
    os_migrate_conversion_keypair_state, present unless set - openstack.cloud's keypair then
    refuses a name that holds another key -, and deploy_conversion_hosts.yml passes each side's
    own os_migrate_{src,dst}_conversion_keypair_state, as it passes the keypair's name: a
    re-keyed side's keypair is replaced, the other side's left alone."""
    [keypair] = [
        task["os_migrate.os_migrate.keypair"]
        for task in load("tasks", "main.yml")
        if "os_migrate.os_migrate.keypair" in task
    ]
    assert keypair["state"] == "{{ os_migrate_conversion_keypair_state | default('present') }}"

    path = os.path.join(ROLE, os.pardir, os.pardir, "playbooks", "deploy_conversion_hosts.yml")
    with open(path, encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    deploys = {}
    for play in plays:
        for task in play.get("tasks", []):
            include = task.get("ansible.builtin.include_role") or {}
            if include.get("name") == "os_migrate.os_migrate.conversion_host" and "tasks_from" not in include:
                deploys[task["vars"]["os_migrate_conversion_cloud"]] = task["vars"]
    assert sorted(deploys) == ["dst", "src"]
    for side, variables in deploys.items():
        assert variables["os_migrate_conversion_keypair_state"] == (
            "{{ os_migrate_%s_conversion_keypair_state | default('present') }}" % side
        )
        assert variables["os_migrate_conversion_keypair_name"] == (
            "{{ os_migrate_%s_conversion_keypair_name | default('os_migrate_conv') }}" % side
        )
