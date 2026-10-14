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
module: import_workload_record_volumes

short_description: Record the destination volumes a cold transfer created

version_added: "1.1.0"

author: "Seamless Migrate contributors"

description:
  - "Records the destination volumes that
    M(os_migrate.os_migrate.import_workload_transfer_volumes) created for a
    workload - each device of its C(volume_map) with a C(dest_id), with its
    C(size), C(bootable) flag and C(name), as the warm path records its
    volumes - in the workload's warm state file
    C({state_dir}/{source_server_id}.json), keeping what the file already
    holds."
  - "A rollback with C(delete_dest_volumes) then deletes exactly these
    volumes: a cold retry never reuses them, as the transfer creates new
    volumes each time, and a volume attached to the destination server later
    is not the migration's."
  - "A C(volume_map) entry without a C(dest_id) is not recorded, and a map
    with none writes nothing. Changes no cloud."

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
  volume_map:
    description:
      - The C(volume_map) that M(os_migrate.os_migrate.import_workload_transfer_volumes) returned.
    required: true
    type: dict
"""

EXAMPLES = r"""
- name: Record the destination volumes for a rollback
  os_migrate.os_migrate.import_workload_record_volumes:
    data: "{{ item }}"
    state_dir: "{{ os_migrate_data_dir }}/workload_warm"
    volume_map: "{{ transfer.volume_map }}"
  when: transfer is defined and 'volume_map' in transfer
"""

RETURN = r"""
dest_volume_ids:
  description: IDs of the destination volumes recorded for the workload.
  returned: success
  type: list
  elements: str
  sample: [3b7a57d7-8210-47f9-b592-a6627ae52d13]
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import server
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import warm_migration


def run_module():
    module = AnsibleModule(
        argument_spec=dict(
            data=dict(type="dict", required=True),
            state_dir=dict(type="str", required=True),
            volume_map=dict(type="dict", required=True),
        ),
    )
    params = module.params

    ser_server = server.Server.from_data(params["data"])
    try:
        changed, volume_ids = warm_migration.record_destination_volumes(
            params["state_dir"],
            ser_server.info()["id"],
            params["volume_map"],
            server_name=ser_server.params().get("name"),
        )
    except ValueError as err:
        module.fail_json(msg=str(err))

    module.exit_json(changed=changed, dest_volume_ids=volume_ids)


def main():
    run_module()


if __name__ == "__main__":
    main()
