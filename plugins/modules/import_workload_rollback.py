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
module: import_workload_rollback

short_description: Roll back the destination side of a migrated workload

extends_documentation_fragment:
  - os_migrate.os_migrate.openstack

version_added: "1.1.0"

author: "Seamless Migrate contributors"

description:
  - "Deletes the destination server recorded as C(destination_server_id) in the
    workload's warm state file C({state_dir}/{source_server_id}.json) and
    clears the record, so a later cutover can create it again."
  - "With I(delete_dest_volumes) the destination volumes are deleted too (the
    ones recorded in the warm state and every volume attached to the deleted
    server), and so is the warm state file. The file is kept while a source
    snapshot is still pending, so its temporary resources can be cleaned up
    with M(os_migrate.os_migrate.import_workload_warm_snapshot)."
  - "Starting the source server again is left to the caller."

options:
  data:
    description:
      - Data structure with server parameters as loaded from OS-Migrate workloads YAML file.
    required: true
    type: dict
  state_dir:
    description:
      - Directory of the warm state files, normally C({{ os_migrate_data_dir }}/workload_warm).
    required: true
    type: str
  delete_dest_volumes:
    description:
      - Also delete the destination volumes and the warm state.
    required: false
    default: false
    type: bool
  match_by_name:
    description:
      - When no destination server is recorded (for example a workload migrated
        cold), delete the only destination server whose name is exactly the
        workload's name. Fails if several servers have that name.
    required: false
    default: false
    type: bool
  dst_filters:
    description:
      - Additional filters for the destination server lookup by name (e.g. C(project_id)).
    required: false
    default: {}
    type: dict
  timeout:
    description:
      - Timeout for long running operations, in seconds.
    required: false
    default: 1800
    type: int
"""

EXAMPLES = r"""
- name: Delete the destination server of a workload
  os_migrate.os_migrate.import_workload_rollback:
    cloud: dst
    data: "{{ item }}"
    state_dir: "{{ os_migrate_data_dir }}/workload_warm"
    delete_dest_volumes: "{{ os_migrate_rollback_delete_dest_volumes }}"
"""

RETURN = r"""
deleted_server_id:
  description: ID of the deleted destination server.
  returned: success
  type: str
  sample: 2d2afe57-ace5-4187-8fca-5f10f9059ba1
deleted_volume_ids:
  description: IDs of the deleted destination volumes.
  returned: success
  type: list
  elements: str
  sample: [3b7a57d7-8210-47f9-b592-a6627ae52d13]
state_deleted:
  description: Whether the warm state file was removed.
  returned: success
  type: bool
  sample: true
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import os_auth
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import server
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.volume_common import (
    DEFAULT_TIMEOUT,
)
from ansible_collections.os_migrate.os_migrate.plugins.module_utils.warm_migration import (
    WarmRollback,
    WarmState,
)


def run_module():
    argument_spec = os_auth.openstack_full_argument_spec(
        data=dict(type="dict", required=True),
        state_dir=dict(type="str", required=True),
        delete_dest_volumes=dict(type="bool", default=False),
        match_by_name=dict(type="bool", default=False),
        dst_filters=dict(type="dict", default={}),
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

    conn = os_auth.get_connection(module)
    try:
        result = WarmRollback(
            conn,
            state,
            server_name=ser_server.params().get("name"),
            match_by_name=params["match_by_name"],
            dst_filters=params["dst_filters"],
            timeout=params["timeout"],
        ).run(delete_volumes=params["delete_dest_volumes"])
    except Exception as err:  # pylint: disable=broad-except
        module.fail_json(msg="Rollback of server %s failed: %s" % (server_id, err))

    module.exit_json(**result)


def main():
    run_module()


if __name__ == "__main__":
    main()
