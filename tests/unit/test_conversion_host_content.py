"""conversion_host_content sets up a conversion host by its OS (Seamless Migrate SDD 6.5,
21.6; plan task T7, D15): the RHEL family, then Ubuntu 24.04 LTS; any other OS fails the role."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
import subprocess
import sys

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

try:
    from ansible.template import trust_as_template
except ImportError:  # ansible-core before 2.19 templates every string

    def trust_as_template(value):
        return value


ROLE = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), os.pardir, os.pardir, "roles", "conversion_host_content"
    )
)


def _load(*parts):
    with open(os.path.join(ROLE, *parts), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _tasks_for(distribution, os_family, version):
    """The task file the role's defaults name for a host with these facts, rendered as Ansible
    renders it."""
    loader = DataLoader()
    defaults = loader.load_from_file(os.path.join(ROLE, "defaults", "main.yml"))
    facts = {
        "ansible_distribution": distribution,
        "ansible_os_family": os_family,
        "ansible_distribution_major_version": version.split(".")[0],
        "ansible_distribution_version": version,
    }
    expression = defaults["os_migrate_conversion_host_content_tasks"]
    return Templar(loader=loader, variables=facts).template(trust_as_template(expression))


def _main_tasks():
    return _load("tasks", "main.yml")[0]["block"]


@pytest.mark.parametrize(
    "distribution, version, tasks",
    [
        ("RedHat", "9.4", "rhel.yml"),
        ("RedHat", "8.10", "rhel.yml"),
        ("CentOS", "9", "centos.yml"),
        ("CentOS", "10", "centos.yml"),
        ("Rocky", "9.4", "centos.yml"),
        ("AlmaLinux", "9.4", "centos.yml"),
        ("Rocky", "8.10", ""),
        ("CentOS", "8", ""),
        # the RedHat family, but no EL rebuild: EPEL's package is not epel-release there
        ("Fedora", "40", ""),
        ("OracleLinux", "9.4", ""),
    ],
)
def test_conversion_host_content_supports_the_rhel_family_by_os_family(
    distribution, version, tasks
):
    """RHEL keeps its subscription tasks; CentOS Stream 9 and 10, Rocky Linux 9 and AlmaLinux 9
    get EPEL and the copy tools - before, a Rocky or AlmaLinux host installed nothing."""
    assert _tasks_for(distribution, "RedHat", version) == tasks
    if tasks:
        assert os.path.isfile(os.path.join(ROLE, "tasks", tasks))


def test_conversion_host_content_installs_the_copy_tools_on_ubuntu_24_04():
    """Ubuntu 24.04 LTS or later: qemu-utils, nbdkit, libnbd-bin (nbdcopy) and libguestfs-tools,
    with apt and fresh package lists - a cloud image's lists are often stale or empty."""
    assert _tasks_for("Ubuntu", "Debian", "24.04") == "ubuntu.yml"
    assert _tasks_for("Ubuntu", "Debian", "26.04") == "ubuntu.yml"
    assert _tasks_for("Ubuntu", "Debian", "22.04") == ""
    installs = [t["ansible.builtin.apt"] for t in _load("tasks", "ubuntu.yml") if "ansible.builtin.apt" in t]
    assert len(installs) == 1, installs
    apt = installs[0]
    assert sorted(apt["name"]) == ["libguestfs-tools", "libnbd-bin", "nbdkit", "qemu-utils"]
    assert apt["state"] == "present" and apt["update_cache"] is True


@pytest.mark.parametrize(
    "distribution, os_family, version",
    [
        ("Debian", "Debian", "12"),
        ("SLES", "Suse", "15.6"),
        ("Ubuntu", "Debian", "22.04"),
        ("Rocky", "RedHat", "8.10"),
    ],
)
def test_conversion_host_content_fails_on_an_os_it_cannot_set_up(distribution, os_family, version):
    """Any other OS fails the role, naming it and the supported ones, before any task file runs:
    it used to install nothing, and the transfer then failed for want of nbdkit."""
    assert _tasks_for(distribution, os_family, version) == ""
    tasks = _main_tasks()
    fails = [i for i, t in enumerate(tasks) if "ansible.builtin.fail" in t]
    includes = [
        i
        for i, t in enumerate(tasks)
        if t.get("ansible.builtin.include_tasks") == "{{ os_migrate_conversion_host_content_tasks }}"
    ]
    assert len(fails) == 1 and len(includes) == 1 and fails[0] < includes[0], tasks
    fail = tasks[fails[0]]
    assert fail["when"] == "not os_migrate_conversion_host_content_tasks"
    for named in ("ansible_distribution", "Rocky Linux", "AlmaLinux", "Ubuntu 24.04"):
        assert named in fail["ansible.builtin.fail"]["msg"], named
    # no task file is chosen by a distribution's name any more
    assert not [t for t in tasks if "ansible_distribution in" in str(t.get("when", ""))]


PLAYBOOK = """
- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    ansible_distribution: Debian
    ansible_os_family: Debian
    ansible_distribution_major_version: "12"
    ansible_distribution_version: "12"
  tasks:
    - name: Set up the conversion host
      ansible.builtin.include_role:
        name: os_migrate.os_migrate.conversion_host_content
"""


@pytest.mark.skipif(
    not os.path.exists(os.path.join(os.path.dirname(sys.executable), "ansible-playbook")),
    reason="needs ansible-playbook next to python",
)
def test_the_role_fails_on_an_os_it_cannot_set_up_before_it_installs_anything(tmp_path):
    """The role itself, run by ansible-playbook on a Debian host's facts: the defaults' expression,
    the fail task's condition and the templated include behave as rendered above - the play fails
    with the message, and no task file runs."""
    playbook = tmp_path / "play.yml"
    playbook.write_text(PLAYBOOK, encoding="utf-8")
    home = tmp_path / "home"
    env = dict(
        os.environ,
        HOME=str(home),
        ANSIBLE_HOME=str(home / ".ansible"),
        ANSIBLE_LOCAL_TEMP=str(home / "tmp"),
        ANSIBLE_COLLECTIONS_SCAN_SYS_PATH="false",
        ANSIBLE_NOCOLOR="1",
        ANSIBLE_RETRY_FILES_ENABLED="false",
    )
    run = subprocess.run(
        [
            os.path.join(os.path.dirname(sys.executable), "ansible-playbook"),
            "-i",
            "localhost,",
            "-e",
            "ansible_python_interpreter=" + sys.executable,
            str(playbook),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        env=env,
        timeout=300,
        check=False,
    )

    assert run.returncode != 0, run.stdout
    assert "The conversion host runs Debian 12" in run.stdout, run.stdout
    assert "Ubuntu 24.04 or later" in run.stdout, run.stdout
    assert "Install content" not in run.stdout, run.stdout


def _rendered(task, host, os_family="RedHat"):
    loader = DataLoader()
    facts = {"inventory_hostname": host, "ansible_os_family": os_family}
    return Templar(loader=loader, variables=facts).template(trust_as_template(task))


def test_the_content_install_allows_local_forwarding_only_on_a_source_host(tmp_path):
    """SDD 26.6 (plan task ST6): with each run's own link, the content install sets the source
    host's sshd to forward local ports only - a key's port-forwarding would allow remote forwards
    too, which permitlisten cannot forbid -, the destination host's to forward none, and tunnels
    off on both (restrict covers tunnels only since OpenSSH 10.6). Its lines go first in
    sshd_config, before any Include, since sshd keeps an option's first value; sshd checks the
    file before it replaces it and reloads. Upstream's install, without the variable, leaves sshd
    as it is."""
    tasks = _main_tasks()
    [include] = [t for t in tasks if t.get("ansible.builtin.include_tasks") == "sshd_link.yml"]
    assert include["when"] == "os_migrate_conversion_link_per_run | default(false) | bool"
    position = tasks.index(include)
    assert position > [i for i, t in enumerate(tasks)
                       if t.get("ansible.builtin.include_tasks") == "{{ os_migrate_conversion_host_content_tasks }}"][0]
    edit, reload = _load("tasks", "sshd_link.yml")
    settings = edit["ansible.builtin.blockinfile"]
    assert settings["path"] == "/etc/ssh/sshd_config"
    assert settings["insertbefore"] == "BOF"
    assert settings["validate"] == "/usr/sbin/sshd -t -f %s"
    blocks = {host: _rendered(settings["block"], host)
              for host in ("os_migrate_conv_src", "os_migrate_conv_dst")}
    assert blocks["os_migrate_conv_src"].split("\n")[:2] == ["AllowTcpForwarding local", "PermitTunnel no"]
    assert blocks["os_migrate_conv_dst"].split("\n")[:2] == ["AllowTcpForwarding no", "PermitTunnel no"]
    service = reload["ansible.builtin.service"]
    assert service["state"] == "reloaded"
    assert _rendered(service["name"], "x", "Debian") == "ssh"
    assert _rendered(service["name"], "x", "RedHat") == "sshd"
    assert reload["when"] == "conversion_host_content_sshd is changed"

    # a real sshd reads the first value: the block first wins over an image's drop-in
    sshd = next((p for p in ("/usr/sbin/sshd", "/usr/local/sbin/sshd") if os.access(p, os.X_OK)), None)
    if sshd is None:
        return
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "host")],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=True)
    (tmp_path / "conf.d").mkdir()
    (tmp_path / "conf.d" / "50-image.conf").write_text("AllowTcpForwarding yes\nPermitTunnel yes\n")
    config = tmp_path / "sshd_config"
    config.write_text(blocks["os_migrate_conv_src"] + "\nInclude %s/*.conf\nHostKey %s\n"
                      "UsePAM no\n" % (tmp_path / "conf.d", tmp_path / "host"))
    effective = subprocess.run([sshd, "-T", "-f", str(config)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, check=False)
    if effective.returncode != 0 and "Include" in effective.stderr:
        pytest.skip("this sshd reads no Include: %s" % effective.stderr.strip())
    assert effective.returncode == 0, effective.stderr
    lines = effective.stdout.splitlines()
    assert "allowtcpforwarding local" in lines and "permittunnel no" in lines
