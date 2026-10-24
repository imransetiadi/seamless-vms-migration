#!/usr/bin/python
# Copyright: Seamless Migrate contributors
# Apache License 2.0 (see LICENSE)

from __future__ import absolute_import, division, print_function

__metaclass__ = type

ANSIBLE_METADATA = {
    "metadata_version": "1.1",
    "status": ["preview"],
    "supported_by": "community",
}

DOCUMENTATION = r"""
---
module: conversion_host_link

short_description: Set up or remove a run's own link between the conversion hosts

version_added: "1.1.0"

author: "Seamless Migrate contributors (!UNKNOWN)"

description:
  - "The link a cold transfer forwards its exports over, and a warm pass runs its senders
    through, from the destination conversion host to the source conversion host, made for one run
    of one workload and removed when the run ends (Seamless Migrate SDD 6.4, 26.6)."
  - "C(state=present) without I(link) removes the workload's earlier entries, every expired one
    and 0.4.0's static link, then makes an ed25519 key on the destination conversion host - its
    private half never leaves that host -, writes the source host's pin beside it, copies the
    run's blocksync script to a private directory of the source host and writes the source user's
    entry for the key: C(restrict), a forced command that checks the script's SHA-256 and runs its
    C(gate), and an C(expiry-time). It then probes the link from the destination host and adds the
    address the source host saw as C(from=). A link that fails to come up is removed before the
    module fails."
  - "C(state=present) with I(link) writes the run's I(ports) (a cold run's exports:
    C(port-forwarding) and one C(permitopen) each) or I(volume_ids) (a warm run's temporary
    volumes, which the gate lets the run's senders read) into its entry."
  - "C(state=absent) removes I(link) - its senders, its entry and both directories - or, without
    one, every entry of the workload."

options:
  state:
    description:
      - C(present) makes a link, or writes I(ports) or I(volume_ids) into I(link); C(absent)
        removes it.
    required: false
    default: present
    choices: [present, absent]
    type: str
  src_conversion_host:
    description:
      - The source conversion host as M(os_migrate.os_migrate.os_conversion_host_info) reports
        it; its C(address) and C(id) are read.
    required: true
    type: dict
  dst_conversion_host:
    description:
      - The destination conversion host, as I(src_conversion_host).
    required: true
    type: dict
  ssh_key_path:
    description:
      - Path to the SSH private key authorized on both conversion hosts (the conversion key).
    required: true
    type: str
  src_ssh_user:
    description:
      - The SSH user of the source conversion host, whose C(authorized_keys) holds the entry.
    required: true
    type: str
  dst_ssh_user:
    description:
      - The SSH user of the destination conversion host, who owns the link's key.
    required: true
    type: str
  ssh_known_hosts:
    description:
      - A known_hosts file that pins the conversion hosts' keys under their aliases (Seamless
        Migrate SDD 6.6). With it, both hosts are checked strictly, and the link checks the
        source host's key against the source pin written beside its key on the destination host.
      - Unset or empty, no host key is checked.
    required: false
    type: path
  workload:
    description:
      - The id of the workload's source server, which the entry's comment names.
    required: true
    type: str
  link:
    description:
      - The link an earlier C(state=present) returned.
    required: false
    type: dict
  volume_ids:
    description:
      - A warm run's temporary volumes, whose disks the gate lets the run's senders read.
    required: false
    type: list
    elements: str
  ports:
    description:
      - A cold run's export ports on the source conversion host, which the link may forward to
        on C(127.0.0.1).
    required: false
    type: list
    elements: int
  expiry_s:
    description:
      - Seconds after the link is made when the source host's sshd stops accepting its key.
    required: false
    default: 604800
    type: int
  python_interpreter:
    description:
      - The Python interpreter of the source conversion host, which runs the gate and the senders.
    required: false
    default: python3
    type: str
  old_link_key_path:
    description:
      - The plan's 0.4.0 link key on this host; the destination host's C(~/.ssh/id_rsa) is removed
        only when it holds that key (its C(.pub) beside it is read).
    required: false
    type: path
  src_authorized_keys:
    description:
      - The source user's file the source host's sshd reads keys from.
    required: false
    default: ~/.ssh/authorized_keys
    type: str
"""

EXAMPLES = r"""
- name: Set up the run's link before the source stops
  os_migrate.os_migrate.conversion_host_link:
    src_conversion_host: "{{ os_src_conversion_host_info.openstack_conversion_host }}"
    dst_conversion_host: "{{ os_dst_conversion_host_info.openstack_conversion_host }}"
    ssh_key_path: "{{ os_migrate_conversion_keypair_private_path }}"
    src_ssh_user: cloud-user
    dst_ssh_user: cloud-user
    workload: "{{ item['_info']['id'] }}"
  register: link

- name: Remove it
  os_migrate.os_migrate.conversion_host_link:
    src_conversion_host: "{{ os_src_conversion_host_info.openstack_conversion_host }}"
    dst_conversion_host: "{{ os_dst_conversion_host_info.openstack_conversion_host }}"
    ssh_key_path: "{{ os_migrate_conversion_keypair_private_path }}"
    src_ssh_user: cloud-user
    dst_ssh_user: cloud-user
    workload: "{{ item['_info']['id'] }}"
    link: "{{ link.link }}"
    state: absent
"""

RETURN = r"""
link:
  description:
    - The link - its run id, both directories, the key's path on the destination host and its
      public half, the script and its SHA-256, the expiry, the address C(from=) names, its
      ports and volumes. Never a private key. Empty after C(state=absent).
  returned: always
  type: dict
"""

from ansible.module_utils.basic import AnsibleModule  # noqa: E402

from ansible_collections.os_migrate.os_migrate.plugins.module_utils.conversion_link import (  # noqa: E402
    AUTHORIZED_KEYS,
    DEFAULT_EXPIRY_S,
    ConversionLink,
)


def run_module():
    argument_spec = dict(
        state=dict(type="str", default="present", choices=["present", "absent"]),
        src_conversion_host=dict(type="dict", required=True),
        dst_conversion_host=dict(type="dict", required=True),
        ssh_key_path=dict(type="str", required=True, no_log=True),
        src_ssh_user=dict(type="str", required=True),
        dst_ssh_user=dict(type="str", required=True),
        ssh_known_hosts=dict(type="path", required=False, default=None),
        workload=dict(type="str", required=True),
        link=dict(type="dict", required=False, default=None),
        volume_ids=dict(type="list", elements="str", required=False, default=None),
        ports=dict(type="list", elements="int", required=False, default=None),
        expiry_s=dict(type="int", default=DEFAULT_EXPIRY_S),
        python_interpreter=dict(type="str", default="python3"),
        old_link_key_path=dict(type="path", required=False, default=None),
        src_authorized_keys=dict(type="str", default=AUTHORIZED_KEYS, no_log=False),
    )
    module = AnsibleModule(argument_spec=argument_spec)
    params = module.params
    try:
        link = ConversionLink(
            params["src_conversion_host"],
            params["dst_conversion_host"],
            params["ssh_key_path"],
            params["src_ssh_user"],
            params["dst_ssh_user"],
            params["workload"],
            ssh_known_hosts=params["ssh_known_hosts"],
            python_interpreter=params["python_interpreter"],
            expiry_s=params["expiry_s"],
            authorized_keys=params["src_authorized_keys"],
            old_link_key_path=params["old_link_key_path"],
        )
        if params["state"] == "absent":
            link.absent(params["link"])
            module.exit_json(changed=True, link={})
        made = link.present(
            params["link"], volume_ids=params["volume_ids"], ports=params["ports"]
        )
    except Exception as err:  # pylint: disable=broad-except
        # the refusal, or the error, as its message: a traceback shows a call's arguments
        # (Seamless Migrate SDD 6.6)
        module.fail_json(msg=str(err))
    module.exit_json(changed=True, link=made)


def main():
    run_module()


if __name__ == "__main__":
    main()
