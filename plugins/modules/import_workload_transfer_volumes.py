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
module: import_workload_transfer_volumes

short_description: Create destination volumes and transfer source data.

extends_documentation_fragment:
  - os_migrate.os_migrate.openstack

version_added: "2.9.0"

author: "OpenStack tenant migration tools (@os-migrate)"

description:
  - "Connect to the destination cloud to create new volumes, and copy data from the source cloud."

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
  src_conversion_host_address:
    description:
      - Require IP address of the source conversion host.
      - This is used by the destination conversion host to initiate data transfer.
    required: true
    type: str
  ssh_key_path:
    description:
      - Path to an SSH private key authorized on the destination cloud.
    required: true
    type: str
  ssh_user:
    description:
      - The SSH user to connect to the conversion hosts.
      - Provided by the import_workloads_export_volumes module.
    required: true
    type: str
  src_ssh_user:
    description:
      - The SSH user the destination conversion host logs in to the source conversion host as,
        for its hop to the source's exports. Defaults to I(ssh_user).
    required: false
    type: str
  state_file:
    description:
      - Path to store a transfer progress file for this conversion process.
    required: false
    type: str
  dst_conversion_host_address:
    description:
      - Optional IP address of the destination conversion host. Without this, the
        plugin will use the 'access_ipv4' property of the conversion host instance.
    required: false
    type: str
  transfer_uuid:
    description:
      - A UUID used to keep track of this tranfer's resources on the conversion hosts.
      - Provided by the import_workloads_export_volumes module.
    required: true
    type: str
  volume_map:
    description:
      - Dictionary providing information about the volumes to transfer.
      - Provided by the import_workloads_export_volumes module.
    required: true
    type: dict
  timeout:
    description:
      - Timeout for long running operations, in seconds.
    required: false
    default: 1800
    type: int
  use_nbdkit_direct:
    description:
      - When true, consume volume data directly from an nbdkit socket instead of using source conversion host.
      - Bypasses SSH forwarding and source conversion host export process.
    required: false
    default: false
    type: bool
  nbdkit_socket_uri:
    description:
      - NBD socket URI to connect to when use_nbdkit_direct is true.
      - Format can be nbd://host:port or nbd+unix:///path/to/socket or nbd+ssh://user@host/export.
      - Required when use_nbdkit_direct is true.
    required: false
    type: str
  nbdkit_export_name:
    description:
      - NBD export name to use when connecting to the nbdkit socket.
      - Optional, used when the nbdkit server requires an export name.
    required: false
    type: str
  preserve_volume_type:
    description:
      - Create the destination volumes with the serialized C(volume_type) instead
        of the destination's default type. Set it when the volume types in the
        workload data were already rewritten to destination types.
    required: false
    default: false
    type: bool
  warm_state_dir:
    description:
      - Directory of the warm state files, normally C({{ os_migrate_data_dir }}/workload_warm).
      - When set, each destination volume is recorded in the workload's state file
        C({warm_state_dir}/{source_server_id}.json) as soon as Cinder created it, before its
        data is copied - its C(dest_id), C(size), C(bootable) flag and C(name), as the warm
        path records its volumes, keeping what the file already holds. A transfer that fails
        or is cut short leaves the record of every volume it created, and a rollback with
        C(delete_dest_volumes) deletes exactly those.
    required: false
    type: str
"""

EXAMPLES = r"""
main.yml:

  - name: validate loaded resources
    os_migrate.os_migrate.validate_resource_files:
      paths:
        - "{{ os_migrate_data_dir }}/workloads.yml"
    register: workloads_file_validation
    when: import_workloads_validate_file

  - name: read workloads resource file
    os_migrate.os_migrate.read_resources:
      path: "{{ os_migrate_data_dir }}/workloads.yml"
    register: read_workloads

  - name: get source conversion host address
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

  - name: get destination conversion host address
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

  - name: import workloads
    include_tasks: workload.yml
    loop: "{{ read_workloads.resources }}"


workload.yml:

  - name: transfer volumes to destination
    os_migrate.os_migrate.import_workload_transfer_volumes:
      auth: "{{ os_migrate_dst_auth }}"
      auth_type: "{{ os_migrate_dst_auth_type | default(omit) }}"
      region_name: "{{ os_migrate_dst_region_name | default(omit) }}"
      validate_certs: "{{ os_migrate_dst_validate_certs | default(omit) }}"
      ca_cert: "{{ os_migrate_dst_ca_cert | default(omit) }}"
      client_cert: "{{ os_migrate_dst_client_cert | default(omit) }}"
      client_key: "{{ os_migrate_dst_client_key | default(omit) }}"
      data: "{{ item }}"
      conversion_host: "{{ os_dst_conversion_host_info.openstack_conversion_host }}"
      ssh_key_path: "{{ os_migrate_conversion_keypair_private_path }}"
      ssh_user: "{{ os_migrate_dst_conversion_host_ssh_user }}"
      src_ssh_user: "{{ os_migrate_src_conversion_host_ssh_user }}"
      transfer_uuid: "{{ exports.transfer_uuid }}"
      src_conversion_host_address: "{{ os_src_conversion_host_info.openstack_conversion_host.address }}"
      volume_map: "{{ exports.volume_map }}"
      state_file: "{{ os_migrate_data_dir }}/{{ item.params.name }}.state"
      log_file: "{{ os_migrate_data_dir }}/{{ item.params.name }}.log"
    register: volume_map
"""

RETURN = r"""
block_device_mapping:
  description:
    - A block_device_mapping_v2 structure for use in OpenStack's create_server().
    - Used to attach destination volumes to the new instance in the right order.
  returned: Only after successfully transferring volumes from the source cloud.
  type: dict
  sample: [{'boot_index': -1, 'delete_on_termination': false, 'destination_type': 'volume',
          'device_name': 'vdb', 'source_type': 'volume', 'uuid': '65d9f006-a4e2-46ba-a082-700549d8635a'}]
volume_map:
  description:
    - Updated mapping of source volume devices to NBD export URLs.
    - Takes the input volume_map and fills out the destination-specific fields.
  returned: Only after successfully transferring volumes from the source cloud.
  type: dict
  sample:
    "volume_map": {
        "/dev/vda": {
            "bootable": true,
            "dest_dev": "/dev/vdc",
            "dest_id": "3b7a57d7-8210-47f9-b592-a6627ae52d13",
            "image_id": null,
            "name": "migration-vm-boot",
            "port": 49196,
            "progress": 100.0,
            "size": 10,
            "snap_id": "564398da-3e39-462d-93aa-aa5b7ea8ea61",
            "source_dev": "/dev/vdat",
            "source_id": "059635b7-451f-4a64-978a-7c2e9e4c15ff",
            "url": "nbd://localhost:49180/9b8a64b3-c976-4103-b34e-995e4ab9f57b"
        }
    }

"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.os_migrate.os_migrate.plugins.module_utils import os_auth
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import server
from ansible_collections.os_migrate.os_migrate.plugins.module_utils import warm_migration

from ansible_collections.os_migrate.os_migrate.plugins.module_utils.volume_common import (
    OpenstackVolumeTransfer,
    DEFAULT_TIMEOUT,
    DiskIdentityError,
    volume_or_none,
)

import re
import time
import traceback


class OpenStackDestinationVolume(OpenstackVolumeTransfer):
    def __init__(
        self,
        openstack_connection,
        destination_conversion_host_id,
        ssh_key_path,
        ssh_user,
        transfer_uuid,
        source_conversion_host_address,
        volume_map,
        ser_server,
        destination_conversion_host_address=None,
        state_file=None,
        log_file=None,
        timeout=DEFAULT_TIMEOUT,
        use_nbdkit_direct=False,
        nbdkit_socket_uri=None,
        nbdkit_export_name=None,
        preserve_volume_type=False,
        warm_state_dir=None,
        src_ssh_user=None,
    ):

        super().__init__(
            openstack_connection,
            destination_conversion_host_id,
            ssh_key_path,
            ssh_user,
            transfer_uuid,
            conversion_host_address=destination_conversion_host_address,
            state_file=state_file,
            log_file=log_file,
            timeout=timeout,
            preserve_volume_type=preserve_volume_type,
            src_ssh_user=src_ssh_user,
        )

        # Required unique parameters:
        # source_conversion_host_address: Source conversion host IP address for
        #                                 direct connection from destination.
        # volume_map: Volume map returned by export_volumes module
        self.source_conversion_host_address = source_conversion_host_address
        self.volume_map = volume_map

        # Match for qemu_img progress percentage
        self.qemu_progress_re = re.compile(r"\((\d+\.\d+)/100%\)")

        # SSH tunnel process
        self.forwarding_process = None
        self.forwarding_process_command = None

        self.ser_server = ser_server

        # NBDkit direct socket mode
        self.use_nbdkit_direct = use_nbdkit_direct
        self.nbdkit_socket_uri = nbdkit_socket_uri
        self.nbdkit_export_name = nbdkit_export_name

        self.warm_state_dir = warm_state_dir

    def _destination_volume_created(self, path):
        """
        With warm_state_dir, record the volume Cinder just created in the
        workload's warm state file, before its data is copied: a transfer
        that fails or is cut short leaves the record of every volume it
        created, which a rollback deletes them by (Seamless Migrate SDD 6.3).
        """
        if self.warm_state_dir:
            server_id = self.ser_server.info()["id"]
            warm_migration.record_destination_volumes(
                self.warm_state_dir,
                server_id,
                {path: self.volume_map[path]},
                server_name=self.ser_server.params().get("name"),
            )

    def _before_destination_volumes_created(self):
        """
        With warm_state_dir, remove the volume an earlier attempt recorded for
        each device before any volume is created: an earlier volume stays
        recorded until it is gone, and no new volume waits unrecorded while
        the lock, a detach or a delete is waited for (Seamless Migrate SDD 6.3,
        review RV5-01).
        """
        if self.warm_state_dir:
            server_id = self.ser_server.info()["id"]
            for path in self.volume_map:
                self._remove_earlier_volume(path, server_id)

    def _remove_earlier_volume(self, path, server_id):
        """
        Remove the volume an earlier attempt recorded for ``path`` unless a
        server holds it: the cold path never reuses a volume, and one a server
        holds (a server the operator renamed rather than deleted) is that
        server's - it only leaves the record when the new volume is recorded.
        One only the destination conversion host holds (a transfer cut short)
        is the migration's own: detached from it under the attach lock. Each
        earlier volume removed is deleted - one Cinder is already deleting
        (the boot volume Nova deletes with its server) is not deleted again -
        and waited for until Cinder no longer has it. An earlier volume that
        cannot be removed fails the transfer with the record unchanged
        (Seamless Migrate SDD 6.3, review RV3-02, RV4-02, RV4-05, RV5-02).
        """
        state = warm_migration.WarmState.load(self.warm_state_dir, server_id)
        earlier_id = (state.dest_volumes.get(path) or {}).get("dest_id")
        if not earlier_id or earlier_id == self.volume_map[path]["dest_id"]:
            return
        volume = volume_or_none(self.conn, earlier_id)
        if volume is None:
            return  # gone already
        converter = self._converter()
        holders = [a["server_id"] for a in getattr(volume, "attachments", None) or []]
        if any(holder != converter.id for holder in holders):
            self.log.warning(
                "Keeping volume %s an earlier attempt made for %s: a server holds it",
                earlier_id,
                path,
            )
            return
        self.log.info("Deleting volume %s an earlier attempt left for %s", earlier_id, path)
        try:
            if holders:
                # a transfer cut short left it on the destination conversion host
                self._detach_volumes_from_destination_converter([earlier_id])
            if getattr(volume, "status", None) != "deleting":
                # Nova deletes a boot volume with its server, and Cinder refuses a second delete
                self._delete_earlier_volume(earlier_id)
            self._wait_until_gone(earlier_id)
        except Exception as err:
            raise RuntimeError(
                "Could not delete volume %s an earlier attempt left for %s: %s"
                % (earlier_id, path, err)
            ) from err

    def _delete_earlier_volume(self, volume_id):
        """
        Delete the earlier volume ``volume_id``. Nova, deleting a server whose
        boot volume goes with it, removes the attachment and then deletes the
        volume: one read `available` in between may begin going before this
        delete lands, which Cinder then refuses - it is waited for as one found
        `deleting` (Seamless Migrate SDD 6.3, review 6 lead).
        """
        try:
            self.conn.delete_volume(name_or_id=volume_id, wait=False)
        except Exception:  # pylint: disable=broad-except
            volume = volume_or_none(self.conn, volume_id)
            if volume is not None and getattr(volume, "status", None) != "deleting":
                raise
            self.log.info("Volume %s began going meanwhile", volume_id)

    def _wait_until_gone(self, volume_id):
        """
        Wait, within the transfer's timeout, until Cinder no longer has the
        earlier volume ``volume_id``: one that leaves `deleting` without going
        (VolumeIsBusy puts it back to `available`, or `error_deleting`) is one
        Cinder gave up deleting (Seamless Migrate SDD 6.3, review RV5-02).
        """
        for second in range(self.timeout):
            volume = volume_or_none(self.conn, volume_id)
            if volume is None:
                return
            status = getattr(volume, "status", None)
            if status != "deleting":
                raise RuntimeError("Cinder did not delete it: it is %s" % status)
            time.sleep(1)
        raise RuntimeError("it is still being deleted after %s s" % self.timeout)

    def _setup_nbdkit_disks_urls(self, nbdkit_disks):
        """
        Set up volume URLs from nbdkit_disks list (multi-disk support).
        Each disk has its own nbdkit process and URI.
        """
        self.log.info("Setting up multi-disk nbdkit URLs...")
        self.log.info("Number of disks: %d", len(nbdkit_disks))

        # Build volume_map from nbdkit_disks
        for disk in nbdkit_disks:
            device = disk["device"]
            uri = disk["uri"]
            size = disk["size"]
            bootable = disk.get("bootable", False)
            port = disk["port"]

            self.log.info("Disk %s: URI=%s, size=%dGB, bootable=%s", device, uri, size, bootable)

            # Update existing volume_map entry or create new one
            if device in self.volume_map:
                self.volume_map[device]["url"] = uri
                self.volume_map[device]["port"] = port
                self.volume_map[device]["size"] = size
                self.volume_map[device]["bootable"] = bootable
            else:
                # Create new entry if not in volume_map
                self.volume_map[device] = {
                    "url": uri,
                    "port": port,
                    "size": size,
                    "bootable": bootable,
                    "dest_dev": None,
                    "dest_id": None,
                    "image_id": None,
                    "name": f"{self.ser_server.params()['name']}-{device.split('/')[-1]}",
                    "progress": 0.0,
                    "snap_id": None,
                    "source_dev": None,
                    "source_id": None,
                }

    def transfer_exports(self):
        failed = False
        try:
            if self.use_nbdkit_direct:
                self.log.info("Using direct nbdkit socket mode with nbdcopy")

                # Check if using multi-disk mode (nbdkit_disks list)
                migration_params = self.ser_server.migration_params()
                nbdkit_disks = migration_params.get("nbdkit_disks")

                if nbdkit_disks:
                    # Multi-disk mode: use nbdkit_disks list
                    self.log.info("Multi-disk mode: %d disks", len(nbdkit_disks))
                    self._setup_nbdkit_disks_urls(nbdkit_disks)
                else:
                    # Legacy single-disk mode
                    self.log.info("Single-disk mode (legacy)")
                    self._setup_nbdkit_direct_urls(self.nbdkit_socket_uri, self.nbdkit_export_name)
            else:
                self.log.info("Using SSH forwarding mode with qemu-img convert")
                self._create_forwarding_process()
            self._create_destination_volumes()
            self._attach_destination_volumes()

            # Use nbdcopy for nbdkit direct mode, qemu-img convert otherwise
            if self.use_nbdkit_direct:
                self._convert_destination_volumes_nbdcopy()
            else:
                self._convert_destination_volumes()
            self._detach_destination_volumes()
        except BaseException:
            failed = True
            raise
        finally:
            if not self.use_nbdkit_direct:
                self._stop_forwarding_process()
                if failed:
                    self._release_ports_after_failure()
                else:
                    self._release_ports()

    def _release_ports_after_failure(self):
        """
        After a failed transfer, release its ports only if one ssh check reaches
        the conversion host now: one it cannot reach cannot update its port map,
        and a wait for its port lock would hold the stopped source another 15
        minutes. A release that fails is logged, never raised over the
        transfer's own error (Seamless Migrate SDD 6.6, review RV10-04, RV11-02).
        One that claimed no port releases none: the release waited a second
        whole window for a port lock whose wait just gave up (review RV12-02).
        """
        if not self.claimed_ports:
            return
        try:
            self.shell.test_ssh_connection()
        except Exception as err:  # pylint: disable=broad-except
            self.log.warning(
                "Ports %s stay claimed on the conversion host it cannot reach: %s",
                self.claimed_ports,
                err,
            )
            return
        try:
            self._release_ports()
        except Exception as err:  # pylint: disable=broad-except
            self.log.warning("Could not release ports %s: %s", self.claimed_ports, err)


def run_module():
    argument_spec = os_auth.openstack_full_argument_spec(
        data=dict(type="dict", required=True),
        conversion_host=dict(type="dict", required=True),
        ssh_key_path=dict(type="str", required=True, no_log=True),
        ssh_user=dict(type="str", required=True),
        src_ssh_user=dict(type="str", required=False, default=None),
        transfer_uuid=dict(type="str", required=True),
        src_conversion_host_address=dict(type="str", required=True),
        volume_map=dict(type="dict", required=True),
        dst_conversion_host_address=dict(type="str", default=None),
        state_file=dict(type="str", default=None),
        log_file=dict(type="str", default=None),
        timeout=dict(type="int", default=DEFAULT_TIMEOUT),
        use_nbdkit_direct=dict(type="bool", default=False),
        nbdkit_socket_uri=dict(type="str", default=None),
        nbdkit_export_name=dict(type="str", default=None),
        preserve_volume_type=dict(type="bool", default=False),
        warm_state_dir=dict(type="str", default=None),
    )

    result = dict(
        changed=False,
    )

    module = AnsibleModule(
        argument_spec=argument_spec,
    )

    conn = os_auth.get_connection(module)
    ser_server = server.Server.from_data(module.params["data"])
    params, info = ser_server.params_and_info()

    # Required parameters
    destination_conversion_host_id = module.params["conversion_host"]["id"]
    ssh_key_path = module.params["ssh_key_path"]
    ssh_user = module.params["ssh_user"]
    transfer_uuid = module.params["transfer_uuid"]
    source_conversion_host_address = module.params["src_conversion_host_address"]
    volume_map = module.params["volume_map"]

    # Optional parameters
    destination_conversion_host_address = module.params.get(
        "dst_conversion_host_address", None
    )
    state_file = module.params.get("state_file", None)
    log_file = module.params.get("log_file", None)
    timeout = module.params["timeout"]

    # NBDkit direct socket mode parameters
    migration_params = ser_server.migration_params()
    use_nbdkit_direct = module.params.get("use_nbdkit_direct", False) or migration_params.get("use_nbdkit_direct", False)
    nbdkit_socket_uri = module.params.get("nbdkit_socket_uri") or migration_params.get("nbdkit_socket_uri")
    nbdkit_export_name = module.params.get("nbdkit_export_name") or migration_params.get("nbdkit_export_name")
    nbdkit_disks = migration_params.get("nbdkit_disks")

    # Validate nbdkit parameters
    # Either nbdkit_disks (multi-disk mode) or nbdkit_socket_uri (legacy single-disk) is required
    if use_nbdkit_direct and not nbdkit_disks and not nbdkit_socket_uri:
        module.fail_json(msg="nbdkit_disks or nbdkit_socket_uri is required when use_nbdkit_direct is true")

    destination_host = OpenStackDestinationVolume(
        conn,
        destination_conversion_host_id,
        ssh_key_path,
        ssh_user,
        transfer_uuid,
        source_conversion_host_address,
        volume_map,
        ser_server,
        destination_conversion_host_address=destination_conversion_host_address,
        state_file=state_file,
        log_file=log_file,
        timeout=timeout,
        use_nbdkit_direct=use_nbdkit_direct,
        nbdkit_socket_uri=nbdkit_socket_uri,
        nbdkit_export_name=nbdkit_export_name,
        preserve_volume_type=module.params["preserve_volume_type"],
        warm_state_dir=module.params["warm_state_dir"],
        src_ssh_user=module.params["src_ssh_user"],
    )
    try:
        destination_host.transfer_exports()
    except DiskIdentityError as err:
        # by its own message, as the export reports its own: the message is what the control
        # plane classifies, and the check's cause - ssh's command line, with its ConnectTimeout -
        # would have this permanent failure retried; every other error leaves the module as
        # before (Seamless Migrate SDD 6.6, review RV15-01)
        module.fail_json(msg=str(err), exception=traceback.format_exc())

    block_device_mapping = []
    for path in sorted(destination_host.volume_map.keys()):
        name = path.split("/")[-1]
        uuid = destination_host.volume_map[path]["dest_id"]

        if path == "/dev/vda":
            entry = {
                "boot_index": 0,
                "delete_on_termination": True,
                "destination_type": "volume",
                "device_name": name,
                "source_type": "volume",
                "uuid": uuid,
            }
        else:
            entry = {
                "boot_index": -1,
                "delete_on_termination": False,
                "destination_type": "volume",
                "device_name": name,
                "source_type": "volume",
                "uuid": uuid,
            }
        block_device_mapping.append(entry)

    result["volume_map"] = destination_host.volume_map
    result["block_device_mapping"] = block_device_mapping

    module.exit_json(**result)


def main():
    run_module()


if __name__ == "__main__":
    main()
