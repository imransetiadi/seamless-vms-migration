#!/usr/bin/python

from __future__ import absolute_import, division, print_function

__metaclass__ = type

ANSIBLE_METADATA = {
    "metadata_version": "1.1",
    "status": ["preview"],
    "supported_by": "community",
}

DOCUMENTATION = r"""
---
module: import_workload_warm_snapshot

short_description: Snapshot a running workload for a warm migration pass

extends_documentation_fragment:
  - os_migrate.os_migrate.openstack

version_added: "1.1.0"

author: "Seamless Migrate contributors"

description:
  - "Source-cloud half of a warm migration pass. With C(state=present) every
    volume of the (running) source server is snapshotted with C(force), a
    temporary volume is created from each snapshot and attached to the source
    conversion host. Image-booted servers with C(boot_disk_copy) are
    snapshotted to a temporary image instead. The source server is never
    stopped or detached."
  - "With C(state=absent) the temporary volumes, snapshots and images recorded
    in the warm state file are detached and deleted."
  - "Idempotent per I(transfer_uuid): a complete snapshot of the same transfer
    is reused, an unfinished or older one is cleaned up first. Every
    resource is recorded in the warm state file
    C({state_dir}/{source_server_id}.json) before it is waited on."

options:
  data:
    description:
      - Data structure with server parameters as loaded from OS-Migrate workloads YAML file.
    required: true
    type: dict
  conversion_host:
    description:
      - Dictionary with information about the source conversion host (address, status, name, id).
    required: true
    type: dict
  ssh_key_path:
    description:
      - Path to an SSH private key authorized on the source conversion host.
    required: true
    type: str
  ssh_user:
    description:
      - The SSH user to connect to the conversion hosts.
    required: true
    type: str
  state_dir:
    description:
      - Directory of the warm state files, normally C({{ os_migrate_data_dir }}/workload_warm).
    required: true
    type: str
  state:
    description:
      - C(present) creates the pass snapshot, C(absent) removes it.
    required: false
    default: present
    choices: [present, absent]
    type: str
  transfer_uuid:
    description:
      - Identifier of this pass. Generated when omitted (C(state=present)) or
        taken from the warm state (C(state=absent)).
    required: false
    type: str
  src_conversion_host_address:
    description:
      - Optional IP address of the source conversion host. Without this, the
        plugin will use the 'access_ipv4' property of the conversion host instance.
    required: false
    type: str
  boot_volume_prefix:
    description:
      - Name prefix of the temporary snapshots, volumes and images.
    required: false
    type: str
  log_file:
    description:
      - Path to store a log file for this conversion process.
    required: false
    type: str
  state_file:
    description:
      - Path to store a transfer progress file for this conversion process.
    required: false
    type: str
  timeout:
    description:
      - Timeout for long running operations, in seconds.
    required: false
    default: 1800
    type: int
"""

EXAMPLES = r"""
- name: Snapshot the running workload into temporary volumes
  os_migrate.os_migrate.import_workload_warm_snapshot:
    cloud: src
    data: "{{ item }}"
    conversion_host: "{{ os_src_conversion_host_info.openstack_conversion_host }}"
    ssh_key_path: "{{ os_migrate_conversion_keypair_private_path }}"
    ssh_user: "{{ os_migrate_conversion_host_ssh_user }}"
    state_dir: "{{ os_migrate_data_dir }}/workload_warm"
    transfer_uuid: "{{ transfer_uuid }}"
    state: present
  register: snapshot

- name: Remove the temporary volumes and snapshots
  os_migrate.os_migrate.import_workload_warm_snapshot:
    cloud: src
    data: "{{ item }}"
    conversion_host: "{{ os_src_conversion_host_info.openstack_conversion_host }}"
    ssh_key_path: "{{ os_migrate_conversion_keypair_private_path }}"
    ssh_user: "{{ os_migrate_conversion_host_ssh_user }}"
    state_dir: "{{ os_migrate_data_dir }}/workload_warm"
    state: absent
"""

RETURN = r"""
transfer_uuid:
  description: Identifier of the pass the snapshot belongs to.
  returned: success
  type: str
  sample: 3e0a4a52-3c4e-4a55-9c55-1a3e1f6c4c0b
volume_map:
  description:
    - Source device path to the temporary copy attached to the source conversion host.
  returned: when state is present
  type: dict
  sample:
    "/dev/vda":
      source_id: 059635b7-451f-4a64-978a-7c2e9e4c15ff
      tmp_volume_id: 4f9e0a39-3c4e-4a55-9c55-1a3e1f6c4c0b
      snap_id: 564398da-3e39-462d-93aa-aa5b7ea8ea61
      image_id: null
      source_dev: /dev/vdc
      size: 20
      bootable: true
      name: migration-vm-boot
      volume_type: tripleo
"""

import uuid

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import os_auth
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import server
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.volume_common import (
    DEFAULT_TIMEOUT,
)
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.warm_migration import (
    OpenstackWarmSnapshot,
    WarmState,
)


def run_module():
    argument_spec = os_auth.openstack_full_argument_spec(
        data=dict(type="dict", required=True),
        conversion_host=dict(type="dict", required=True),
        ssh_key_path=dict(type="str", required=True, no_log=True),
        ssh_user=dict(type="str", required=True),
        state_dir=dict(type="str", required=True),
        state=dict(type="str", default="present", choices=["present", "absent"]),
        transfer_uuid=dict(type="str", default=None),
        src_conversion_host_address=dict(type="str", default=None),
        boot_volume_prefix=dict(type="str", default=None),
        log_file=dict(type="str", default=None),
        state_file=dict(type="str", default=None),
        timeout=dict(type="int", default=DEFAULT_TIMEOUT),
    )

    module = AnsibleModule(argument_spec=argument_spec)
    params = module.params

    ser_server = server.Server.from_data(params["data"])
    server_id = ser_server.info()["id"]
    try:
        state = WarmState.load(params["state_dir"], server_id)
    except ValueError as err:
        module.fail_json(msg=str(err))

    pending = state.pending_snapshot
    if params["state"] == "absent" and pending is None:
        module.exit_json(changed=False, transfer_uuid=params["transfer_uuid"])

    transfer_uuid = params["transfer_uuid"]
    if transfer_uuid is None:
        if params["state"] == "absent":
            transfer_uuid = pending["transfer_uuid"]
        else:
            transfer_uuid = str(uuid.uuid4())

    conn = os_auth.get_connection(module)
    try:
        snapshot = OpenstackWarmSnapshot(
            conn,
            params["conversion_host"]["id"],
            params["ssh_key_path"],
            params["ssh_user"],
            transfer_uuid,
            ser_server,
            state,
            conversion_host_address=params["src_conversion_host_address"],
            boot_volume_prefix=params["boot_volume_prefix"],
            state_file=params["state_file"],
            log_file=params["log_file"],
            timeout=params["timeout"],
        )
        if params["state"] == "present":
            volume_map = snapshot.create()
            module.exit_json(
                changed=snapshot.changed, transfer_uuid=transfer_uuid, volume_map=volume_map
            )
        snapshot.cleanup()
    except Exception as err:  # pylint: disable=broad-except
        module.fail_json(
            msg="Warm snapshot (%s) of server %s failed: %s"
            % (params["state"], server_id, err),
            transfer_uuid=transfer_uuid,
        )
    module.exit_json(changed=True, transfer_uuid=transfer_uuid)


def main():
    run_module()


if __name__ == "__main__":
    main()
