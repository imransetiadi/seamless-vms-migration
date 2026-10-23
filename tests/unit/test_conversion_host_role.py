from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import os
import shlex
import subprocess
import sys

import pytest
import yaml

from ansible_collections.os_migrate.os_migrate.tests.unit import local_sshd

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


# --- the inventory's pinned conversion hosts (Seamless Migrate SDD 6.5, 26.4; plan task ST3) ------

ANSIBLE_PLAYBOOK = os.path.join(os.path.dirname(sys.executable), "ansible-playbook")
needs_ansible_playbook = pytest.mark.skipif(
    not os.path.exists(ANSIBLE_PLAYBOOK), reason="needs ansible-playbook next to python"
)
needs_sshd = pytest.mark.skipif(
    local_sshd.SSHD is None, reason="needs an sshd binary (openssh-server): SDD 26.9's real ssh"
)

#: the conversion host lookup, stubbed: every name is a server at 127.0.0.1
SERVER_INFO = """#!/usr/bin/python
# WANT_JSON
import json
import sys

with open(sys.argv[1]) as f:
    name = json.load(f)["server"].rstrip("*")
print(json.dumps({"changed": False, "servers": [
    {"id": "server-" + name, "name": name, "status": "ACTIVE", "access_ipv4": "127.0.0.1"}]}))
"""

#: conv_host_inventory.yml adds each side's host; then each host reports what the inventory gave
#: it and, when asked, what ssh did with a command over Ansible's ssh connection
INVENTORY_PLAYBOOK = """---
- name: Add the conversion hosts
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Add each side's conversion host
      ansible.builtin.include_tasks: "{{ tasks_file }}"
      vars:
        inventory_name: "{{ item.alias }}"
        inventory_group: "{{ item.group }}"
        os_migrate_conversion_host_name: "{{ item.name }}"
        os_migrate_conversion_cloud: "{{ item.cloud }}"
      loop:
        - {alias: os_migrate_conv_src, group: conversion_hosts_src, name: conv-src, cloud: src}
        - {alias: os_migrate_conv_dst, group: conversion_hosts_dst, name: conv-dst, cloud: dst}

- name: Reach each conversion host
  hosts: conversion_hosts
  gather_facts: false
  ignore_unreachable: true
  tasks:
    - name: Run a command over ssh
      ansible.builtin.raw: echo connected
      register: reached
      when: reach | bool
    - name: Report the host's ssh arguments and what ssh did
      ansible.builtin.copy:
        dest: "{{ report_dir }}/{{ inventory_hostname }}.json"
        mode: "0600"
        content: "{{ {'common_args': ansible_ssh_common_args | default(None),
                      'extra_args': ansible_ssh_extra_args | default(None),
                      'host_key_checking': ansible_ssh_host_key_checking | default(None),
                      'reached': reached | default(None)} | to_json }}"
      delegate_to: localhost
"""


def run_inventory(root, name, reach=False, **variables):
    """Run INVENTORY_PLAYBOOK under ``root/name`` with the role's conv_host_inventory.yml and the
    stub lookup; returns (exit status, output, {host: report}). Ansible's own host key checking
    is off, as Seamless's runs have it while SEAMLESS_ANSIBLE_HOST_KEY_CHECKING is false: only
    the inventory's own settings can check a key."""
    collections = os.path.join(root, "collections")
    modules = os.path.join(collections, "ansible_collections", "os_migrate", "os_migrate", "plugins", "modules")
    if not os.path.isdir(modules):
        os.makedirs(modules)
        with open(os.path.join(modules, "server_info.py"), "w") as f:
            f.write(SERVER_INFO)
    run_dir = os.path.join(root, name)
    reports = os.path.join(run_dir, "reports")
    os.makedirs(reports)
    with open(os.path.join(run_dir, "inventory"), "w") as f:
        f.write("localhost ansible_connection=local ansible_python_interpreter=%s\n" % sys.executable)
    with open(os.path.join(run_dir, "play.yml"), "w") as f:
        f.write(INVENTORY_PLAYBOOK)
    extra = dict(
        tasks_file=os.path.join(ROLE, "tasks", "conv_host_inventory.yml"),
        report_dir=reports,
        reach=reach,
        **variables
    )
    with open(os.path.join(run_dir, "vars.json"), "w") as f:
        json.dump(extra, f)
    home = os.path.join(run_dir, "home")
    env = dict(
        os.environ,
        HOME=home,
        ANSIBLE_HOME=os.path.join(home, ".ansible"),
        ANSIBLE_LOCAL_TEMP=os.path.join(home, "tmp"),
        ANSIBLE_COLLECTIONS_PATH=collections,
        ANSIBLE_COLLECTIONS_SCAN_SYS_PATH="false",
        ANSIBLE_HOST_KEY_CHECKING="False",
        ANSIBLE_SSH_ARGS="-o ControlMaster=no",  # no master connection outlives the run
        ANSIBLE_NOCOLOR="1",
        ANSIBLE_RETRY_FILES_ENABLED="false",
    )
    run = subprocess.run(
        [ANSIBLE_PLAYBOOK, "-i", os.path.join(run_dir, "inventory"), os.path.join(run_dir, "play.yml"),
         "-e", "@" + os.path.join(run_dir, "vars.json")],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, env=env, timeout=300, check=False,
    )
    found = {}
    for host in sorted(os.listdir(reports)):
        with open(os.path.join(reports, host)) as f:
            found[host[: -len(".json")]] = json.load(f)
    return run.returncode, run.stdout, found


def options_of(common_args):
    """The -o options of an ansible_ssh_common_args string, split as Ansible splits it."""
    assert isinstance(common_args, str), "no common arguments: %r" % (common_args,)
    words = shlex.split(common_args)
    assert words[::2] == ["-o"] * (len(words) // 2) and len(words) % 2 == 0, words
    return dict(word.split("=", 1) for word in words[1::2])


@needs_ansible_playbook
@needs_sshd
def test_the_inventory_pins_the_host_through_its_common_args(tmp_path):
    """Seamless Migrate SDD 6.5, 26.4 (plan task ST3): with os_migrate_conversion_host_known_hosts,
    conv_host_inventory.yml gives each conversion host the strict host key options under its
    inventory name, in ansible_ssh_common_args (which Ansible gives ssh, scp and sftp), the
    algorithms of the keys the file pins under that name, and its own host key checking, and
    drops the extra arguments' StrictHostKeyChecking=no. Over Ansible's ssh connection, with
    Ansible's host key checking off, a real sshd is reached under the alias that pins its key and
    refused under the one that pins another; a file ssh-keygen cannot read fails the inventory."""
    server = local_sshd.LocalSshd()
    try:
        rsa = local_sshd.keygen(str(tmp_path / "rsa"), "rsa")
        rogue = local_sshd.keygen(str(tmp_path / "rogue"))
        known_hosts = local_sshd.write_known_hosts(
            str(tmp_path / "known_hosts"),
            [
                ("os_migrate_conv_src", server.host_keys["ed25519"]),
                ("os_migrate_conv_dst", rogue),
                ("os_migrate_conv_src", rsa),
            ],
        )
        rc, out, reports = run_inventory(
            str(tmp_path), "pinned", reach=True,
            os_migrate_conversion_host_known_hosts=known_hosts,
            os_migrate_conversion_host_ssh_user=server.user,
            os_migrate_conversion_keypair_private_path=server.client_key,
            ansible_port=server.port,
        )
    finally:
        server.stop()

    assert sorted(reports) == ["os_migrate_conv_dst", "os_migrate_conv_src"], out
    for alias, algorithms in (
        ("os_migrate_conv_src", "ssh-ed25519,rsa-sha2-512,rsa-sha2-256"),
        ("os_migrate_conv_dst", "ssh-ed25519"),
    ):
        report = reports[alias]
        assert options_of(report["common_args"]) == {
            "StrictHostKeyChecking": "yes",
            "UserKnownHostsFile": known_hosts,
            "GlobalKnownHostsFile": "/dev/null",
            "HostKeyAlias": alias,
            "CheckHostIP": "no",
            "UpdateHostKeys": "no",
            "VerifyHostKeyDNS": "no",
            "HashKnownHosts": "yes",
            "HostKeyAlgorithms": algorithms,
            "IdentitiesOnly": "yes",
        }, report
        assert report["extra_args"] is None, report
        assert report["host_key_checking"] in (True, "True", "true"), report

    reached = reports["os_migrate_conv_src"]["reached"]
    assert not reached.get("unreachable") and "connected" in reached["stdout"], (reached, out)
    refused = reports["os_migrate_conv_dst"]["reached"]
    assert refused.get("unreachable") and "Host key verification failed." in refused["msg"], (refused, out)

    rc, out, reports = run_inventory(
        str(tmp_path), "missing",
        os_migrate_conversion_host_known_hosts=str(tmp_path / "no-such-file"),
        os_migrate_conversion_host_ssh_user="cloud-user",
        os_migrate_conversion_keypair_private_path=str(tmp_path / "key"),
    )
    assert rc != 0 and reports == {}, out
    assert "Cannot stat" in out and "no-such-file" in out, out


@needs_ansible_playbook
def test_the_inventory_without_a_known_hosts_file_keeps_upstreams_extra_args(tmp_path):
    """Seamless Migrate SDD 6.5, 26.4 (plan task ST3): without os_migrate_conversion_host_known_hosts
    - unset, or empty - conv_host_inventory.yml adds each conversion host as upstream does: the
    extra arguments' StrictHostKeyChecking=no, no common arguments and no host key checking of
    its own."""
    for name, variables in (("unset", {}), ("empty", {"os_migrate_conversion_host_known_hosts": ""})):
        rc, out, reports = run_inventory(
            str(tmp_path), name,
            os_migrate_conversion_host_ssh_user="cloud-user",
            os_migrate_conversion_keypair_private_path=str(tmp_path / "key"),
            **variables
        )
        assert rc == 0, out
        assert sorted(reports) == ["os_migrate_conv_dst", "os_migrate_conv_src"], out
        for report in reports.values():
            assert (report["common_args"], report["extra_args"], report["host_key_checking"]) == (
                None, "-o StrictHostKeyChecking=no", None
            ), (name, report)
