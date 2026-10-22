#!/usr/bin/python
# Modified by the Seamless Migrate project, 2026.


from __future__ import absolute_import, division, print_function

__metaclass__ = type

ANSIBLE_METADATA = {
    "metadata_version": "1.1",
    "status": ["preview"],
    "supported_by": "community",
}

DOCUMENTATION = r"""
---
module: import_workload_create_instance

short_description: Create NBD exports of OpenStack volumes

extends_documentation_fragment:
  - os_migrate.os_migrate.openstack

version_added: "2.9.0"

author: "OpenStack tenant migration tools (@os-migrate)"

description:
  - "Take an instance from an OS-Migrate YAML structure, and export its volumes over NBD."

options:
  auth:
    description:
      - Required if 'cloud' param not used
    required: false
    type: dict
  auth_type:
    description:
      - Auth type plugin for destination OpenStack cloud. Can be omitted if using password authentication.
    required: false
    type: str
  region_name:
    description:
      - Destination OpenStack region name. Can be omitted if using default region.
    required: false
    type: str
  cloud:
    description:
      - Cloud resource from clouds.yml
      - Required if 'auth' param not used.
    required: false
    type: raw
  validate_certs:
    description:
      - Validate HTTPS certificates when logging in to OpenStack.
    required: false
    type: bool
  data:
    description:
      - Data structure with server parameters as loaded from OS-Migrate workloads YAML file.
    required: true
    type: dict
  block_device_mapping:
    description:
      - A block_device_mapping_v2 structure from the transfer_volumes module.
      - Used to attach destination volumes to the new instance in the right order.
    required: true
    type: list
    elements: dict
  warm_state_dir:
    description:
      - Directory of the warm migration state files (warm cutover).
      - When set, the new server's ID is recorded as C(destination_server_id)
        in the source server's warm state file as soon as Nova accepts the
        request, and a server already recorded there is returned instead of
        creating a second one.
    required: false
    type: str
  finish_only:
    description:
      - Only finish the destination server the warm state in
        I(warm_state_dir) records - wait for it to be ACTIVE and add the
        floating IPs it lacks - and never create one (a warm cutover's
        re-run). When the state records no server, or one Nova no longer
        has, the module fails and names the rollback.
    required: false
    type: bool
    default: false
"""

EXAMPLES = r"""
main.yml:
  - name: Validate loaded resources
    os_migrate.os_migrate.validate_resource_files:
      paths:
        - "{{ os_migrate_data_dir }}/workloads.yml"
    register: workloads_file_validation
    when: import_workloads_validate_file

  - name: Read workloads resource file
    os_migrate.os_migrate.read_resources:
      path: "{{ os_migrate_data_dir }}/workloads.yml"
    register: read_workloads

  - name: Get source conversion host address
    os_migrate.os_migrate.os_conversion_host_info:
      auth:
        auth_url: https://src-osp:13000/v3
        username: migrate
        password: migrate
        project_domain_id: default
        project_name: migration-source
        user_domain_id: default
      server_id: ce4dda96-5d8e-4b67-aee2-9845cdc943fe
    register: os_src_conversion_host_info

  - name: Get destination conversion host address
    os_migrate.os_migrate.os_conversion_host_info:
      auth:
        auth_url: https://dest-osp:13000/v3
        username: migrate
        password: migrate
        project_domain_id: default
        project_name: migration-destination
        user_domain_id: default
      server_id: 2d2afe57-ace5-4187-8fca-5f10f9059ba1
    register: os_dst_conversion_host_info

  - name: Import workloads
    include_tasks: workload.yml
    loop: "{{ read_workloads.resources }}"


workload.yml:
  - block:
      - name: Preliminary setup for workload import
        os_migrate.os_migrate.import_workload_prelim:
          auth:
            auth_url: https://dest-osp:13000/v3
            username: migrate
            password: migrate
            project_domain_id: default
            project_name: migration-destination
            user_domain_id: default
          validate_certs: false
          src_conversion_host: "{{ os_src_conversion_host_info.openstack_conversion_host }}"
          src_auth:
            auth_url: https://src-osp:13000/v3
            username: migrate
            password: migrate
            project_domain_id: default
            project_name: migration-source
            user_domain_id: default
          src_validate_certs: false
          data: "{{ item }}"
          data_dir: "{{ os_migrate_data_dir }}"
        register: prelim

      - debug:
          msg:
            - "{{ prelim.server_name }} log file: {{ prelim.log_file }}"
            - "{{ prelim.server_name }} progress file: {{ prelim.state_file }}"
        when: prelim.changed

      - name: Expose source volumes
        os_migrate.os_migrate.import_workload_export_volumes:
          auth: "{{ os_migrate_src_auth }}"
          auth_type: "{{ os_migrate_src_auth_type|default(omit) }}"
          region_name: "{{ os_migrate_src_region_name|default(omit) }}"
          validate_certs: "{{ os_migrate_src_validate_certs|default(omit) }}"
          ca_cert: "{{ os_migrate_src_ca_cert|default(omit) }}"
          client_cert: "{{ os_migrate_src_client_cert|default(omit) }}"
          client_key: "{{ os_migrate_src_client_key|default(omit) }}"
          conversion_host:
            "{{ os_src_conversion_host_info.openstack_conversion_host }}"
          data: "{{ item }}"
          log_file: "{{ os_migrate_data_dir }}/{{ prelim.server_name }}.log"
          state_file: "{{ os_migrate_data_dir }}/{{ prelim.server_name }}.state"
          ssh_key_path: "{{ os_migrate_conversion_keypair_private_path }}"
        register: exports
        when: prelim.changed

      - name: Transfer volumes to destination
        os_migrate.os_migrate.import_workload_transfer_volumes:
          auth: "{{ os_migrate_dst_auth }}"
          auth_type: "{{ os_migrate_dst_auth_type|default(omit) }}"
          region_name: "{{ os_migrate_dst_region_name|default(omit) }}"
          validate_certs: "{{ os_migrate_dst_validate_certs|default(omit) }}"
          ca_cert: "{{ os_migrate_dst_ca_cert|default(omit) }}"
          client_cert: "{{ os_migrate_dst_client_cert|default(omit) }}"
          client_key: "{{ os_migrate_dst_client_key|default(omit) }}"
          data: "{{ item }}"
          conversion_host:
            "{{ os_dst_conversion_host_info.openstack_conversion_host }}"
          ssh_key_path: "{{ os_migrate_conversion_keypair_private_path }}"
          transfer_uuid: "{{ exports.transfer_uuid }}"
          src_conversion_host_address:
            "{{ os_src_conversion_host_info.openstack_conversion_host.address }}"
          volume_map: "{{ exports.volume_map }}"
          state_file: "{{ os_migrate_data_dir }}/{{ prelim.server_name }}.state"
          log_file: "{{ os_migrate_data_dir }}/{{ prelim.server_name }}.log"
        register: transfer
        when: prelim.changed

      - name: Create destination instance
        os_migrate.os_migrate.import_workload_create_instance:
          auth: "{{ os_migrate_dst_auth }}"
          auth_type: "{{ os_migrate_dst_auth_type|default(omit) }}"
          region_name: "{{ os_migrate_dst_region_name|default(omit) }}"
          validate_certs: "{{ os_migrate_dst_validate_certs|default(omit) }}"
          ca_cert: "{{ os_migrate_dst_ca_cert|default(omit) }}"
          client_cert: "{{ os_migrate_dst_client_cert|default(omit) }}"
          client_key: "{{ os_migrate_dst_client_key|default(omit) }}"
          data: "{{ item }}"
          block_device_mapping: "{{ transfer.block_device_mapping }}"
        register: os_migrate_destination_instance
        when: prelim.changed

    rescue:
      - fail:
          msg: "Failed to import {{ item.params.name }}!"
"""

RETURN = r"""
server_id:
  description: The ID of the newly created server.
  returned: On successful creation of migrated server on destination cloud.
  type: str
  sample: 059635b7-451f-4a64-978a-7c2e9e4c15ff
floating_ips:
  description:
    - What became of each floating IP of the server (Seamless Migrate SDD 7.2).
    - C(outcome) is C(kept) (already on the port), C(attached) (a detached floating IP of the
      address), C(created) (a new one, with a new address) or C(skipped) (I(floating_ip_mode) skip).
  returned: When the server was created or finished.
  type: list
  elements: dict
  sample:
    - address: 172.20.9.135
      fixed_ip_address: 192.168.20.7
      outcome: created
      new_address: 172.20.9.200
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import os_auth
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import server
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import (
    warm_migration,
)


def _destination_recorder(state_dir, source_id, server_name):
    """Callback recording the new server in the warm state (before waiting)."""

    def record(sdk_server):
        warm_migration.record_destination_server(
            state_dir, source_id, sdk_server.id, server_name
        )

    return record


def run_module():
    argument_spec = os_auth.openstack_full_argument_spec(
        data=dict(type="dict", required=True),
        block_device_mapping=dict(type="list", required=True, elements="dict"),
        warm_state_dir=dict(type="str", default=None),
        finish_only=dict(type="bool", default=False),
    )

    result = dict(
        changed=False,
    )

    module = AnsibleModule(
        argument_spec=argument_spec,
        required_if=[("finish_only", True, ("warm_state_dir",))],
    )

    conn = os_auth.get_connection(module)
    block_device_mapping = module.params["block_device_mapping"]

    ser_server = server.Server.from_data(module.params["data"])

    on_create = None
    recorded = None
    warm_state_dir = module.params["warm_state_dir"]
    if warm_state_dir:
        source_id = ser_server.info()["id"]
        server_name = ser_server.params().get("name")
        try:
            state = warm_migration.WarmState.load(warm_state_dir, source_id)
        except ValueError as err:
            module.fail_json(msg=str(err))
        recorded = state.destination_server_id
        if state.destination_server_id:
            existing = conn.compute.find_server(
                state.destination_server_id, ignore_missing=True
            )
            if existing is not None:
                # created by an earlier run that may have failed while the server
                # booted or got its floating IPs: finish it, never create another
                ser_server.finish(conn, existing)
                module.exit_json(
                    changed=False,
                    server_id=existing.id,
                    msg="Destination server %s was already created." % existing.id,
                    floating_ips=getattr(ser_server, "floating_ip_report", []),
                )

        on_create = _destination_recorder(warm_state_dir, source_id, server_name)

    if module.params["finish_only"]:
        # a re-run finishes what its warm state records and never creates: the server may have
        # vanished since the role looked it up, and a create would build one without the
        # synchronised volumes and record it (Seamless Migrate SDD 6.4, audit A-07)
        module.fail_json(
            msg="%s: %s. Run rollback_workloads.yml (it deletes the server and starts the "
            "source again) and cut the workload over once more."
            % (
                ser_server.params().get("name"),
                "the recorded destination server %s was not found" % recorded
                if recorded
                else "the warm state records no destination server to finish",
            )
        )

    sdk_server = ser_server.create(conn, block_device_mapping, on_create=on_create)
    # Some info (e.g. flavor ID) will only become available after the
    # server is in ACTIVE state, we need to wait for it.
    sdk_server = conn.compute.wait_for_server(sdk_server, failures=["ERROR"], wait=600)
    dst_ser_server = server.Server.from_sdk(conn, sdk_server)

    if sdk_server:
        result["changed"] = True
        result["server"] = dst_ser_server.data
        result["server_id"] = sdk_server.id
        # what became of each floating IP (Seamless Migrate SDD 7.2, plan task N6b)
        result["floating_ips"] = getattr(ser_server, "floating_ip_report", [])

    module.exit_json(**result)


def main():
    run_module()


if __name__ == "__main__":
    main()
