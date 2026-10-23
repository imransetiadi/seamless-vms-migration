from __future__ import absolute_import, division, print_function

__metaclass__ = type

import base64
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


def playbook_env(run_dir, collections):
    """The environment of an ansible-playbook the tests run under ``run_dir``: a home of its own,
    the modules of ``collections`` alone, and Ansible's own host key checking off."""
    home = os.path.join(run_dir, "home")
    return dict(
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
    run = subprocess.run(
        [ANSIBLE_PLAYBOOK, "-i", os.path.join(run_dir, "inventory"), os.path.join(run_dir, "play.yml"),
         "-e", "@" + os.path.join(run_dir, "vars.json")],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, env=playbook_env(run_dir, collections), timeout=300, check=False,
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


# --- the injected host key and the report (Seamless Migrate SDD 6.5, 26.3; plan task ST4) ---------

DEPLOY = os.path.join(ROLE, os.pardir, os.pardir, "playbooks", "deploy_conversion_hosts.yml")

#: the server task's arguments without a host key - upstream's, once those it omits are gone
UPSTREAM_SERVER_ARGUMENTS = sorted([
    "name", "flavor", "image", "key_name", "boot_from_volume", "volume_size", "terminate_volume",
    "network", "security_groups", "auto_ip", "wait", "timeout", "cloud",
])

#: Nova, stubbed: its servers by name in nova.json, under the directory each stub names
STUB_HEAD = '''#!/usr/bin/python
# WANT_JSON
import base64
import json
import os
import sys
import uuid

NOVA = %r


def arguments():
    with open(sys.argv[1]) as f:
        return dict((k, v) for k, v in json.load(f).items() if not k.startswith("_ansible_"))


def servers():
    with open(os.path.join(NOVA, "nova.json")) as f:
        return json.load(f)


def answer(params, **result):
    # as AnsibleModule answers: the arguments it got come back as the result's invocation
    result["invocation"] = {"module_args": params}
    print(json.dumps(result))
'''

#: the security group, its rules and the keypair: nothing to do here
NOOP_STUB = '''
answer(arguments(), changed=False)
'''

#: the role's lookup: the servers of the name, with the user data Nova details to an admin
SERVER_INFO_STUB = '''
params = arguments()
answer(params, changed=False, servers=[s for s in servers().values() if s["name"] == params["server"]])
'''

#: the vendored server module: a server of the name is found as it is; another is created, and Nova
#: keeps its user data and shows it, base64-encoded, to an admin - in the server the module returns.
#: Each call's arguments are kept in calls.json
SERVER_STUB = '''
params = arguments()
calls_file = os.path.join(NOVA, "calls.json")
calls = []
if os.path.exists(calls_file):
    with open(calls_file) as f:
        calls = json.load(f)
with open(calls_file, "w") as f:
    json.dump(calls + [params], f)
nova = servers()
server = nova.get(params["name"])
if server is None:
    user_data = params.get("userdata")
    server = nova[params["name"]] = {
        "id": str(uuid.uuid4()), "name": params["name"], "status": "ACTIVE",
        "user_data": base64.b64encode(user_data.encode("utf-8")).decode("ascii") if user_data else None,
    }
    with open(os.path.join(NOVA, "nova.json"), "w") as f:
        json.dump(nova, f)
    answer(params, changed=True, server=server)
else:
    answer(params, changed=False, server=server)
'''


def deploy_play():
    """The deploy playbook's includes of the role, one per side with that side's variables, as
    includes of its main tasks (the role's dependencies, which need clouds, stay out), under the
    role's defaults."""
    with open(DEPLOY, encoding="utf-8") as f:
        plays = yaml.safe_load(f)
    tasks = []
    for play in plays:
        for task in play.get("tasks", []):
            include = task.get("ansible.builtin.include_role") or {}
            if include.get("name") == "os_migrate.os_migrate.conversion_host" and "tasks_from" not in include:
                tasks.append({
                    "name": task["name"],
                    "ansible.builtin.include_tasks": os.path.join(ROLE, "tasks", "main.yml"),
                    "vars": task["vars"],
                    "when": task["when"],
                })
    assert [task["vars"]["os_migrate_conversion_cloud"] for task in tasks] == ["src", "dst"], tasks
    return [{
        "name": "Deploy the conversion hosts",
        "hosts": "localhost",
        "gather_facts": False,
        "vars_files": [os.path.join(ROLE, "defaults", "main.yml")],
        "tasks": tasks,
    }]


def run_deploy(root, name, servers=None, **variables):
    """Run deploy_play() at -vvv under ``root/name`` against the stubbed Nova, which holds
    ``servers`` ({name: server}) at the start; returns (exit status, output, the server module's
    calls, the role's report - None without one -, Nova's servers at the end)."""
    run_dir = os.path.join(root, name)
    nova = os.path.join(run_dir, "nova")
    collections = os.path.join(run_dir, "collections")
    modules = os.path.join(collections, "ansible_collections", "os_migrate", "os_migrate", "plugins", "modules")
    os.makedirs(nova)
    os.makedirs(modules)
    with open(os.path.join(nova, "nova.json"), "w") as f:
        json.dump(servers or {}, f)
    for module, body in (
        ("security_group", NOOP_STUB),
        ("security_group_rule", NOOP_STUB),
        ("keypair", NOOP_STUB),
        ("server_info", SERVER_INFO_STUB),
        ("server", SERVER_STUB),
    ):
        with open(os.path.join(modules, module + ".py"), "w") as f:
            f.write(STUB_HEAD % nova + body)
    with open(os.path.join(run_dir, "inventory"), "w") as f:
        f.write("localhost ansible_connection=local ansible_python_interpreter=%s\n" % sys.executable)
    with open(os.path.join(run_dir, "play.yml"), "w") as f:
        yaml.safe_dump(deploy_play(), f, default_flow_style=False, sort_keys=False)
    login_key = os.path.join(run_dir, "ssh.key")
    local_sshd.keygen(login_key)
    data_dir = os.path.join(run_dir, "osm")
    extra = dict(
        os_migrate_data_dir=data_dir,
        os_migrate_conversion_keypair_generate=False,
        os_migrate_conversion_keypair_private_path=login_key,
        os_migrate_src_conversion_flavor_name="m1.large",
        os_migrate_dst_conversion_flavor_name="m1.large",
        os_migrate_src_conversion_manage_network=False,
        os_migrate_dst_conversion_manage_network=False,
        os_migrate_src_conversion_manage_fip=False,
        os_migrate_dst_conversion_manage_fip=False,
    )
    extra.update(variables)
    with open(os.path.join(run_dir, "vars.json"), "w") as f:
        json.dump(extra, f)
    run = subprocess.run(
        [ANSIBLE_PLAYBOOK, "-vvv", "-i", os.path.join(run_dir, "inventory"), os.path.join(run_dir, "play.yml"),
         "-e", "@" + os.path.join(run_dir, "vars.json")],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, env=playbook_env(run_dir, collections), timeout=300, check=False,
    )
    calls = []
    if os.path.exists(os.path.join(nova, "calls.json")):
        with open(os.path.join(nova, "calls.json")) as f:
            calls = json.load(f)
    report = None
    if os.path.exists(os.path.join(data_dir, "conversion", "hosts.json")):
        with open(os.path.join(data_dir, "conversion", "hosts.json")) as f:
            report = json.load(f)
    with open(os.path.join(nova, "nova.json")) as f:
        return run.returncode, run.stdout, calls, report, json.load(f)


def key_body(path):
    """The base64 lines of the private key at ``path``, between its BEGIN and END lines."""
    with open(path, encoding="ascii") as f:
        return [line for line in f.read().splitlines() if line and not line.startswith("-----")]


def secrets_of(*paths, **texts):
    """{name: text} of what no output may show: each base64 line of the private keys at ``paths``
    - but a line its public key (``path.pub``) holds as well: an RSA key's body repeats the public
    modulus, which may be printed -, and ``texts``."""
    found = dict(texts)
    for path in paths:
        with open(path + ".pub", encoding="ascii") as f:
            public = f.read()
        for number, line in enumerate(key_body(path), 1):
            if line not in public:
                found["%s line %d" % (os.path.basename(path), number)] = line
    return found


def leaked(output, secrets):
    """The names of the secrets the output shows."""
    return sorted(name for name, text in secrets.items() if text and text in output)


def shown(output, secrets):
    """The output for a failure message, each secret blanked: no key reaches a test's output."""
    for text in secrets.values():
        if text:
            output = output.replace(text, "<secret>")
    return output


def cloud_config_of(user_data):
    """The cloud-config a user data holds, parsed as cloud-init parses it. Only booleans and names
    are asserted on what holds a key, so that no failure message shows one."""
    is_cloud_config = isinstance(user_data, str) and user_data.startswith("#cloud-config\n")
    assert is_cloud_config, "the user data is no cloud-config"
    return yaml.safe_load(user_data)


def task_outputs(output, name):
    """What the output shows of each run of the task called ``name``: its banner to the next."""
    return [part for part in output.split("TASK [") if part.startswith(name + "]")]


def earlier_user_data(root):
    """The user data of a server created earlier with an injected host key of its own, as Nova
    details it to an admin (base64), and that key's path."""
    earlier = os.path.join(root, "earlier_host_key")
    public = local_sshd.keygen(earlier)
    with open(earlier, encoding="ascii") as f:
        cloud_config = "#cloud-config\n" + yaml.safe_dump(
            {"ssh_deletekeys": True, "ssh_keys": {"ed25519_private": f.read(), "ed25519_public": public}}
        )
    return base64.b64encode(cloud_config.encode("utf-8")).decode("ascii"), earlier


@needs_ansible_playbook
def test_a_created_conversion_host_gets_the_injected_host_key_as_user_data(tmp_path):
    """Seamless Migrate SDD 6.5, 26.3 (plan task ST4): given a host key - the private half of an
    ed25519 pair, as a file -, the role creates its side's conversion host with cloud-init's
    ssh_keys as the server's userdata: ed25519_private the file's text, ed25519_public its public
    half, and ssh_deletekeys true, so the host presents that key alone. The report, 0600 in a 0700
    folder beside the role's other outputs, names the server it created under the side's alias."""
    key = str(tmp_path / "host_key")
    public = local_sshd.keygen(key)
    secrets = secrets_of(key)
    rc, out, calls, report, nova = run_deploy(
        str(tmp_path), "created",
        os_migrate_src_conversion_host_key_private_path=key,
        os_migrate_deploy_dst_conversion_host=False,
    )
    assert rc == 0, shown(out, secrets)
    names = [call["name"] for call in calls]
    assert names == ["os_migrate_conv_src"], names
    cloud_config = cloud_config_of(calls[0].get("userdata"))
    keys = cloud_config.get("ssh_keys") or {}
    with open(key, encoding="ascii") as f:
        injected = keys.get("ed25519_private") == f.read()
    assert injected, "ed25519_private is not the host key's private half, byte for byte"
    assert sorted(keys) == ["ed25519_private", "ed25519_public"], sorted(keys)
    public_half = keys["ed25519_public"].split()[:2]
    assert public_half == public.split()[:2], public_half
    deletes = cloud_config.get("ssh_deletekeys") is True
    assert deletes, "ssh_deletekeys is not true"
    assert sorted(cloud_config) == ["ssh_deletekeys", "ssh_keys"], sorted(cloud_config)

    assert report == {
        "created": [{"alias": "os_migrate_conv_src", "id": nova["os_migrate_conv_src"]["id"]}],
        "found": [],
    }, report
    conversion = tmp_path / "created" / "osm" / "conversion"
    assert oct(os.stat(str(conversion)).st_mode & 0o777) == "0o700"
    assert oct(os.stat(str(conversion / "hosts.json")).st_mode & 0o777) == "0o600"


@needs_ansible_playbook
def test_the_server_task_never_logs_the_user_data(tmp_path):
    """Seamless Migrate SDD 6.5, 26.3 (plan task ST4): at -vvv, a run that creates one conversion
    host with an injected key and finds the other - created earlier with a key of its own - prints
    no line of either private key and neither user data as Nova returns it (base64): the lookup
    and the server task run with no_log, since Nova's server details carry the user data to an
    admin, and no other task prints the private half or holds it in a fact."""
    key = str(tmp_path / "host_key")
    local_sshd.keygen(key)
    earlier, earlier_key = earlier_user_data(str(tmp_path))
    found = {"id": "4b1f2a9e-0c7d-4f1e-9a55-2d3c4b5a6f70", "name": "os_migrate_conv_dst",
             "status": "ACTIVE", "user_data": earlier}
    rc, out, calls, report, nova = run_deploy(
        str(tmp_path), "logged",
        servers={"os_migrate_conv_dst": found},
        os_migrate_src_conversion_host_key_private_path=key,
    )
    secrets = secrets_of(key, earlier_key, earlier=earlier, injected=nova["os_migrate_conv_src"]["user_data"])
    assert rc == 0, shown(out, secrets)
    names = [call["name"] for call in calls]
    assert names == ["os_migrate_conv_src", "os_migrate_conv_dst"], names
    injected = bool(calls[0].get("userdata")) and bool(nova["os_migrate_conv_src"]["user_data"])
    assert injected, "no key was injected: nothing to hide"
    for task in ("Look up the os_migrate conversion host", "Create os_migrate conversion host"):
        runs = task_outputs(out, task)
        hidden = len(runs) == 2 and all("'no_log: true' was specified for this result" in run for run in runs)
        assert hidden, shown("TASK [".join([""] + runs), secrets)

    leaks = leaked(out, secrets)
    assert leaks == [], "the output shows %s" % leaks
    assert report == {
        "created": [{"alias": "os_migrate_conv_src", "id": nova["os_migrate_conv_src"]["id"]}],
        "found": [{"alias": "os_migrate_conv_dst", "id": found["id"]}],
    }, report


@needs_ansible_playbook
def test_a_found_conversion_host_is_reported_as_found_and_gets_no_user_data(tmp_path):
    """Seamless Migrate SDD 6.5, 26.3, 26.4 (plan task ST4): a conversion host the role finds keeps
    its own key - Nova keeps its earlier user data - and gets no user data, even when the run gave
    its side a host key; the other side's host, created, gets its own side's key. The report names
    the found one under found and the created one under created, each with its id and its side's
    alias: the run pins the injected key of a server the role created alone."""
    src_key = str(tmp_path / "src_host_key")
    dst_key = str(tmp_path / "dst_host_key")
    local_sshd.keygen(src_key)
    local_sshd.keygen(dst_key)
    earlier, earlier_key = earlier_user_data(str(tmp_path))
    found = {"id": "9d8c7b6a-5f4e-4d3c-8b2a-190817263544", "name": "os_migrate_conv_src",
             "status": "ACTIVE", "user_data": earlier}
    secrets = secrets_of(src_key, dst_key, earlier_key, earlier=earlier)
    rc, out, calls, report, nova = run_deploy(
        str(tmp_path), "found",
        servers={"os_migrate_conv_src": dict(found)},
        os_migrate_src_conversion_host_key_private_path=src_key,
        os_migrate_dst_conversion_host_key_private_path=dst_key,
    )
    assert rc == 0, shown(out, secrets)
    names = [call["name"] for call in calls]
    assert names == ["os_migrate_conv_src", "os_migrate_conv_dst"], names
    assert sorted(calls[0]) == UPSTREAM_SERVER_ARGUMENTS, sorted(calls[0])
    with open(dst_key, encoding="ascii") as f:
        injected = (cloud_config_of(calls[1].get("userdata")).get("ssh_keys") or {}).get(
            "ed25519_private") == f.read()
    assert injected, "the created host did not get its own side's key"
    unchanged = nova["os_migrate_conv_src"] == found
    assert unchanged, "the found server changed"

    assert report == {
        "created": [{"alias": "os_migrate_conv_dst", "id": nova["os_migrate_conv_dst"]["id"]}],
        "found": [{"alias": "os_migrate_conv_src", "id": found["id"]}],
    }, report


@needs_ansible_playbook
def test_without_a_host_key_the_server_task_passes_no_user_data(tmp_path):
    """Seamless Migrate SDD 6.5, 26.3 (plan task ST4): without a host key - unset, or empty - the
    server task passes no user data at all: exactly upstream's arguments. The role still reports
    the servers it created."""
    for name, variables in (
        ("unset", {}),
        ("empty", {"os_migrate_src_conversion_host_key_private_path": "",
                   "os_migrate_dst_conversion_host_key_private_path": ""}),
    ):
        rc, out, calls, report, nova = run_deploy(str(tmp_path), name, **variables)
        assert rc == 0, out
        names = [call["name"] for call in calls]
        assert names == ["os_migrate_conv_src", "os_migrate_conv_dst"], (name, names)
        for call in calls:
            assert sorted(call) == UPSTREAM_SERVER_ARGUMENTS, (name, sorted(call))
        assert [server["user_data"] for server in nova.values()] == [None, None], name
        assert report == {
            "created": [
                {"alias": "os_migrate_conv_src", "id": nova["os_migrate_conv_src"]["id"]},
                {"alias": "os_migrate_conv_dst", "id": nova["os_migrate_conv_dst"]["id"]},
            ],
            "found": [],
        }, (name, report)


@needs_ansible_playbook
def test_a_host_key_that_is_no_ed25519_key_fails_the_role_before_its_server_task(tmp_path):
    """Seamless Migrate SDD 6.5, 26.3 (plan task ST4): cloud-init writes the injected key as the
    host's ed25519 key, so a key of another type - RSA here - fails the role before its server
    task, and its private half is printed nowhere."""
    key = str(tmp_path / "rsa_host_key")
    local_sshd.keygen(key, "rsa")
    secrets = secrets_of(key)
    rc, out, calls, report, nova = run_deploy(
        str(tmp_path), "rsa",
        os_migrate_src_conversion_host_key_private_path=key,
        os_migrate_deploy_dst_conversion_host=False,
    )
    assert rc != 0, shown(out, secrets)
    untouched = calls == [] and nova == {} and report is None
    assert untouched, "the server task ran"
    runs = task_outputs(out, "Read the public half of the injected host key")
    refused = len(runs) == 1 and "FAILED!" in runs[0] and "ssh-rsa" in runs[0]
    assert refused, shown(out, secrets)
    leaks = leaked(out, secrets)
    assert leaks == [], "the output shows %s" % leaks
