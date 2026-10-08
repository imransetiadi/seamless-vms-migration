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
module: import_workload_warm_sync

short_description: Run one blocksync pass of a warm workload migration

extends_documentation_fragment:
  - os_migrate.os_migrate.openstack

version_added: "1.1.0"

author: "Seamless Migrate contributors"

description:
  - "Destination-cloud half of a warm migration pass. Ensures the destination
    volumes exist (created on the first pass with the same name, size,
    bootable flag and parameters as the cold import), attaches them to the
    destination conversion host, copies blocksync.py to both conversion hosts
    as C(/tmp/seamless-blocksync-{transfer_uuid}.py) and runs
    C(blocksync receive) on the destination host, which pulls the changed
    chunks of the pending snapshot from the source conversion host over SSH."
  - "Progress is streamed into I(state_file) (C({device: percent})), the pass
    is appended to the warm state file and the destination volumes are
    detached again."
  - "Run M(os_migrate.os_migrate.import_workload_warm_snapshot) with
    C(state=present) first; the pending snapshot of the warm state is the
    source of the pass."

options:
  data:
    description:
      - Data structure with server parameters as loaded from OS-Migrate workloads YAML file.
    required: true
    type: dict
  conversion_host:
    description:
      - Dictionary with information about the destination conversion host (address, status, name, id).
    required: true
    type: dict
  ssh_key_path:
    description:
      - Path to an SSH private key authorized on both conversion hosts.
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
  src_conversion_host_address:
    description:
      - IP address of the source conversion host, as reached from the destination conversion host.
    required: true
    type: str
  dst_conversion_host_address:
    description:
      - Optional IP address of the destination conversion host. Without this, the
        plugin will use the 'access_ipv4' property of the conversion host instance.
    required: false
    type: str
  transfer_uuid:
    description:
      - Identifier of the pass. Defaults to the pending snapshot's transfer.
    required: false
    type: str
  pass_kind:
    description:
      - C(auto) is C(full) while no destination volumes exist, then C(delta).
        C(final) is the pass run after the source server was stopped.
    required: false
    default: auto
    choices: [auto, full, delta, final]
    type: str
  chunk_size:
    description:
      - blocksync chunk size in bytes.
    required: false
    default: 4194304
    type: int
  workers:
    description:
      - blocksync read-ahead hashing threads on each conversion host.
    required: false
    default: 4
    type: int
  parallel_disks:
    description:
      - Number of disks synchronised at the same time.
    required: false
    default: 4
    type: int
  assume_zero:
    description:
      - Skip reading destination volumes created by this pass and treat them as
        zero-filled. Only safe on backends that return zeros for never-written
        blocks (Ceph RBD, thin LVM, sparse files).
    required: false
    default: false
    type: bool
  python_interpreter:
    description:
      - Python 3.6+ interpreter on the conversion hosts.
    required: false
    default: python3
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
      - Timeout for OpenStack operations (volume creation, attachment), in seconds.
        The data transfer itself is not limited.
    required: false
    default: 1800
    type: int
"""

EXAMPLES = r"""
- name: Synchronise the pending snapshot into the destination volumes
  os_migrate.os_migrate.import_workload_warm_sync:
    cloud: dst
    data: "{{ item }}"
    conversion_host: "{{ os_dst_conversion_host_info.openstack_conversion_host }}"
    src_conversion_host_address: "{{ os_src_conversion_host_info.openstack_conversion_host.address }}"
    ssh_key_path: "{{ os_migrate_conversion_keypair_private_path }}"
    ssh_user: "{{ os_migrate_conversion_host_ssh_user }}"
    state_dir: "{{ os_migrate_data_dir }}/workload_warm"
    transfer_uuid: "{{ snapshot.transfer_uuid }}"
    pass_kind: auto
    chunk_size: 4194304
    workers: 4
  register: sync
"""

RETURN = r"""
sync_pass:
  description: The pass recorded in the warm state file.
  returned: success
  type: dict
  sample:
    number: 2
    kind: delta
    started_at: "2026-10-08T10:00:00.000Z"
    ended_at: "2026-10-08T10:04:12.512Z"
    bytes_scanned: 21474836480
    bytes_changed: 419430400
    bytes_transferred: 312475648
    duration_s: 252.512
volume_map:
  description: Source device path to the devices and volumes used by the pass.
  returned: success
  type: dict
  sample:
    "/dev/vda":
      source_id: 059635b7-451f-4a64-978a-7c2e9e4c15ff
      source_dev: /dev/vdc
      dest_id: 3b7a57d7-8210-47f9-b592-a6627ae52d13
      dest_dev: /dev/vdd
      name: migration-vm-boot
      size: 20
      bootable: true
      assume_zero: false
      progress: 100.0
block_device_mapping:
  description:
    - block_device_mapping_v2 of the destination volumes for
      M(os_migrate.os_migrate.import_workload_create_instance). No volume is
      deleted on termination.
  returned: success
  type: list
  elements: dict
  sample: [{'boot_index': 0, 'delete_on_termination': false, 'destination_type': 'volume',
          'device_name': 'vda', 'source_type': 'volume', 'uuid': '3b7a57d7-8210-47f9-b592-a6627ae52d13'}]
transfer_uuid:
  description: Identifier of the pass.
  returned: success
  type: str
  sample: 3e0a4a52-3c4e-4a55-9c55-1a3e1f6c4c0b
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import blocksync
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import os_auth
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import server
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.volume_common import (
    DEFAULT_TIMEOUT,
)
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.warm_migration import (
    DEFAULT_PARALLEL_DISKS,
    OpenstackWarmSync,
    WarmState,
)


def run_module():
    argument_spec = os_auth.openstack_full_argument_spec(
        data=dict(type="dict", required=True),
        conversion_host=dict(type="dict", required=True),
        ssh_key_path=dict(type="str", required=True, no_log=True),
        ssh_user=dict(type="str", required=True),
        state_dir=dict(type="str", required=True),
        src_conversion_host_address=dict(type="str", required=True),
        dst_conversion_host_address=dict(type="str", default=None),
        transfer_uuid=dict(type="str", default=None),
        pass_kind=dict(
            type="str", default="auto", choices=["auto", "full", "delta", "final"]
        ),
        chunk_size=dict(type="int", default=blocksync.DEFAULT_CHUNK_SIZE),
        workers=dict(type="int", default=blocksync.DEFAULT_WORKERS),
        parallel_disks=dict(type="int", default=DEFAULT_PARALLEL_DISKS),
        assume_zero=dict(type="bool", default=False),
        python_interpreter=dict(type="str", default="python3"),
        log_file=dict(type="str", default=None),
        state_file=dict(type="str", default=None),
        timeout=dict(type="int", default=DEFAULT_TIMEOUT),
    )

    module = AnsibleModule(argument_spec=argument_spec)
    params = module.params

    if not blocksync.MIN_CHUNK_SIZE <= params["chunk_size"] <= blocksync.MAX_CHUNK_SIZE:
        module.fail_json(
            msg="chunk_size must be between %d and %d bytes"
            % (blocksync.MIN_CHUNK_SIZE, blocksync.MAX_CHUNK_SIZE)
        )
    if not 1 <= params["workers"] <= blocksync.MAX_WORKERS:
        module.fail_json(msg="workers must be between 1 and %d" % blocksync.MAX_WORKERS)
    if params["parallel_disks"] < 1:
        module.fail_json(msg="parallel_disks must be at least 1")

    ser_server = server.Server.from_data(params["data"])
    server_id = ser_server.info()["id"]
    try:
        state = WarmState.load(params["state_dir"], server_id)
    except ValueError as err:
        module.fail_json(msg=str(err))
    if state.pending_snapshot is None:
        module.fail_json(
            msg="No pending snapshot for server %s: run "
            "import_workload_warm_snapshot with state=present first." % server_id
        )
    transfer_uuid = params["transfer_uuid"] or state.pending_snapshot["transfer_uuid"]

    conn = os_auth.get_connection(module)
    try:
        warm_sync = OpenstackWarmSync(
            conn,
            params["conversion_host"]["id"],
            params["ssh_key_path"],
            params["ssh_user"],
            transfer_uuid,
            ser_server,
            state,
            params["src_conversion_host_address"],
            conversion_host_address=params["dst_conversion_host_address"],
            chunk_size=params["chunk_size"],
            workers=params["workers"],
            parallel_disks=params["parallel_disks"],
            assume_zero=params["assume_zero"],
            python_interpreter=params["python_interpreter"],
            state_file=params["state_file"],
            log_file=params["log_file"],
            timeout=params["timeout"],
        )
        sync_pass = warm_sync.sync(params["pass_kind"])
        block_device_mapping = warm_sync.block_device_mapping()
    except Exception as err:  # pylint: disable=broad-except
        module.fail_json(
            msg="Warm sync of server %s failed: %s" % (server_id, err),
            transfer_uuid=transfer_uuid,
        )

    module.exit_json(
        changed=True,
        transfer_uuid=transfer_uuid,
        sync_pass=sync_pass,
        volume_map=warm_sync.volume_map,
        block_device_mapping=block_device_mapping,
    )


def main():
    run_module()


if __name__ == "__main__":
    main()
