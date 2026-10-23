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
module: import_workload_dst_failure_cleanup

short_description: Clean up temporary volumes after a workload migration

extends_documentation_fragment:
  - os_migrate.os_migrate.openstack

version_added: "2.9.0"

author: "OpenStack tenant migration tools (@os-migrate)"

description:
  - Remove any volumes associated with a failed workload migration in the destination cloud.

options:
  auth:
    description:
      - Required if 'cloud' param not used.
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
      - Required if 'auth' param not used
    required: false
    type: raw
  validate_certs:
    description:
      - Validate HTTPS certificates when logging in to OpenStack.
    required: false
    type: bool
  conversion_host:
    description:
      - Dictionary with information about the destination conversion host (address, status, name, id)
    required: true
    type: dict
  data:
    description:
      - Data structure with server parameters as loaded from OS-Migrate workloads YAML file.
    required: true
    type: dict
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
  ssh_key_path:
    description:
      - Path to an SSH private key authorized on the destination cloud.
    required: true
    type: str
  ssh_user:
    description:
      - The SSH user to connect to the conversion hosts.
    required: true
    type: str
  ssh_known_hosts:
    description:
      - A known_hosts file that pins the conversion hosts' keys under their aliases,
        C(os_migrate_conv_src) and C(os_migrate_conv_dst) (Seamless Migrate SDD 6.6).
        With it, ssh and scp to the conversion host check its key strictly, and a
        refused key fails the module at once.
      - Unset or empty, the module connects as upstream does (C(StrictHostKeyChecking=no)).
    required: false
    type: path
  transfer_uuid:
    description:
      - A UUID used to keep track of this tranfer's resources on the conversion hosts.
      - Provided by the import_workloads_export_volumes module.
    required: true
    type: str
  volume_map:
    description:
      - Dictionary providing information about the volumes to transfer.
      - Provided by the import_workloads_transfer_volumes module.
      - A transfer that failed returns none; I(warm_state_dir) then names the volumes.
    required: false
    type: dict
  warm_state_dir:
    description:
      - Directory of the workloads' state files. Without I(volume_map), the destination volumes
        the workload's record holds - the transfer recorded each as soon as Cinder created it -
        are the ones cleaned up.
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
rescue:
  - name: clean up in the destination cloud after migration failure
    os_migrate.os_migrate.import_workload_dst_failure_cleanup:
      auth: "{{ os_migrate_src_auth }}"
      auth_type: "{{ os_migrate_src_auth_type|default(omit) }}"
      region_name: "{{ os_migrate_src_region_name|default(omit) }}"
      validate_certs: "{{ os_migrate_src_validate_certs|default(omit) }}"
      ca_cert: "{{ os_migrate_src_ca_cert|default(omit) }}"
      client_cert: "{{ os_migrate_src_client_cert|default(omit) }}"
      client_key: "{{ os_migrate_src_client_key|default(omit) }}"
      data: "{{ item }}"
      conversion_host:
        "{{ os_src_conversion_host_info.openstack_conversion_host }}"
      ssh_key_path: "{{ os_migrate_conversion_keypair_private_path }}"
      ssh_user: "{{ os_migrate_conversion_host_ssh_user }}"
      transfer_uuid: "{{ exports.transfer_uuid }}"
      volume_map: "{{ exports.volume_map }}"
      state_file: "{{ os_migrate_data_dir }}/{{ prelim.server_name }}.state"
      log_file: "{{ os_migrate_data_dir }}/{{ prelim.server_name }}.log"
    when:
      - prelim.changed
      - os_migrate_workload_cleanup_on_failure

  - fail:
      msg: "Failed to import {{ item.params.name }}!"
"""

RETURN = r"""
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import os_auth
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import server
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import warm_migration

from ansible_collections.os_migrate.os_migrate.plugins.module_utils.volume_common import (
    DEFAULT_TIMEOUT,
    ConversionHostUnreachable,
    DetachStuck,
    LockUnavailable,
    OpenStackVolumeBase,
    gave_up_waiting,
)


class OpenStackDstFailureCleanup(OpenStackVolumeBase):
    """Removes volumes after a failed migration from the destination cloud."""

    SIDE = "destination"

    def __init__(
        self,
        openstack_connection,
        destination_conversion_host_id,
        ssh_key_path,
        ssh_user,
        transfer_uuid,
        volume_map,
        state_file=None,
        log_file=None,
        timeout=DEFAULT_TIMEOUT,
        ssh_known_hosts=None,
    ):

        super().__init__(
            openstack_connection,
            destination_conversion_host_id,
            ssh_key_path,
            ssh_user,
            transfer_uuid,
            state_file=state_file,
            log_file=log_file,
            timeout=timeout,
            ssh_known_hosts=ssh_known_hosts,
        )
        self.volume_map = volume_map

    def _check_ssh(self):
        """The one SSH check at the start, its answer kept rather than raised: the conversion host
        may be what failed the transfer, and the API alone still deletes the volumes no server
        holds (Seamless Migrate SDD 6.6, audit A-08)."""
        try:
            self.shell.test_ssh_connection()
        except Exception as err:  # pylint: disable=broad-except
            self.log.warning(
                "The destination conversion host cannot be reached (%s): the volumes it holds "
                "stay for the rollback, which deletes them by the record",
                err,
            )
            self.reachable = False
        else:
            self.reachable = True

    def delete_migrated_volumes(self):
        """Detach the volumes from the destination conversion host - under its attach lock, only
        when the check reached it - then delete those no server holds (SDD 6.6, audit A-08). Best
        effort: what it cannot detach or delete is logged and left for the rollback, which
        deletes it by the record; it never fails the rescue (reviews RV32B-02, RV32B-05). A wait
        that gives up ends its phase: each later volume would wait it out again, the cold source
        stopped all the while (review RV34B-01). Returns the ids of the volumes it left."""
        if self.reachable:
            for volume_id in [mapping["dest_id"] for mapping in self.volume_map.values()]:
                try:
                    self._detach_volumes_from_destination_converter(volume_ids=[volume_id])
                except (ConversionHostUnreachable, LockUnavailable, DetachStuck) as err:
                    # the host lost, the lock another conversion holds, a detach Nova does not
                    # end: every other detach would wait it out again
                    self.log.warning(
                        "Detaching from the destination conversion host gave up (%s): what it "
                        "holds stays for the rollback, which deletes it by the record",
                        err,
                    )
                    break
                except Exception as err:  # pylint: disable=broad-except
                    self.log.warning(
                        "Volume %s could not be detached (%s): it stays for the rollback",
                        volume_id,
                        err,
                    )
        return self._delete_volumes_best_effort()

    def _delete_volumes_best_effort(self):
        """Delete each volume of the map no server holds; one Cinder does not delete - or one
        still attached - is logged and left. A deletion Cinder does not finish within the timeout
        ends the deletions: the volumes not yet deleted are left, no deletion sent for them
        (review RV34B-01)."""
        self.log.info("Deleting migrated volumes from destination.")
        left = []
        stalled = False
        for mapping in self.volume_map.values():
            volume_id = mapping["dest_id"]
            try:
                volume = self._get_volume_maybe(volume_id)
                if not volume:
                    continue
                if volume.attachments:
                    self.log.warning(
                        "Volume %s is still attached: it stays for the rollback", volume_id
                    )
                    left.append(volume_id)
                    continue
                if stalled:
                    self.log.warning(
                        "Volume %s is not deleted - a deletion before it did not finish in time: "
                        "it stays for the rollback",
                        volume_id,
                    )
                    left.append(volume_id)
                    continue
                self.log.info("Deleting volume %s.", volume_id)
                self.conn.delete_volume(volume_id, timeout=self.timeout, wait=True)
            except Exception as err:  # pylint: disable=broad-except
                self.log.warning(
                    "Volume %s could not be deleted (%s): it stays for the rollback",
                    volume_id,
                    err,
                )
                left.append(volume_id)
                if gave_up_waiting(err):
                    # Cinder did not finish it in time: every other deletion would wait it out again
                    stalled = True
        return left


def run_module():
    argument_spec = os_auth.openstack_full_argument_spec(
        data=dict(type="dict", required=True),
        conversion_host=dict(type="dict", required=True),
        ssh_key_path=dict(type="str", required=True, no_log=True),
        ssh_user=dict(type="str", required=True),
        ssh_known_hosts=dict(type="path", required=False, default=None),
        transfer_uuid=dict(type="str", required=True),
        volume_map=dict(type="dict", default=None),
        warm_state_dir=dict(type="str", default=None),
        state_file=dict(type="str", default=None),
        log_file=dict(type="str", default=None),
        timeout=dict(type="int", default=DEFAULT_TIMEOUT),
    )

    result = dict(
        changed=False,
    )

    module = AnsibleModule(
        argument_spec=argument_spec,
        required_one_of=[("volume_map", "warm_state_dir")],
    )

    conn = os_auth.get_connection(module)
    src = server.Server.from_data(module.params["data"])
    params, info = src.params_and_info()

    # Required parameters
    destination_conversion_host_id = module.params["conversion_host"]["id"]
    ssh_key_path = module.params["ssh_key_path"]
    ssh_user = module.params["ssh_user"]
    transfer_uuid = module.params["transfer_uuid"]
    volume_map = module.params["volume_map"]
    if volume_map is None:
        # a transfer that failed returns no map: the volumes it recorded as Cinder created them
        # (Seamless Migrate SDD 6.3, 6.6, audit A-08)
        try:
            state = warm_migration.WarmState.load(module.params["warm_state_dir"], info["id"])
        except ValueError as err:
            # best effort, as below: the rollback reads the same record and says what it finds
            result["volumes_left"] = []
            result["msg"] = "The destination cleanup cannot read the record: %s" % err
            module.exit_json(**result)
        volume_map = {
            device: {"dest_id": entry["dest_id"]}
            for device, entry in state.dest_volumes.items()
            if entry.get("dest_id")
        }

    # Optional parameters
    state_file = module.params.get("state_file", None)
    log_file = module.params.get("log_file", None)
    timeout = module.params["timeout"]

    # best effort: the rescue goes on and the transfer's failure is what the run reports - this
    # module never fails, and nothing it met is printed (Seamless Migrate SDD 6.6, RV32B-02)
    try:
        failure_cleanup = OpenStackDstFailureCleanup(
            conn,
            destination_conversion_host_id,
            ssh_key_path,
            ssh_user,
            transfer_uuid,
            volume_map,
            state_file=state_file,
            log_file=log_file,
            timeout=timeout,
            ssh_known_hosts=module.params["ssh_known_hosts"],
        )
        result["volumes_left"] = failure_cleanup.delete_migrated_volumes()
    except Exception as err:  # pylint: disable=broad-except
        result["volumes_left"] = sorted(mapping["dest_id"] for mapping in volume_map.values())
        result["msg"] = "The destination cleanup gave up: %s" % err

    module.exit_json(**result)


def main():
    run_module()


if __name__ == "__main__":
    main()
