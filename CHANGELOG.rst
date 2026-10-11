Changelog
=========

This project follows a simple changelog to satisfy collection metadata validation.

1.0.2
-----

- Maintenance updates for CI, linting, and sanity checks.

1.0.3
-----

- Change openstacksdk version in AEE to 4.5.0 to stop github CI from failing

1.0.4
-----

- Remove mention to OCP in the readme
- Revert make file change for submodule
- Add pylint, create bindep in root repo and remove useless dep in galaxy.yml
- Remove community.general dependency
- Cert checks
- update checksum to usedforsecurity=false

1.0.5
-----

- Add documentation for stringFilter module
- Add galaxy ignore file and fix links in the README
- Move import on top the module

1.1.0
-----

- Warm migration of OpenStack workloads (Seamless Migrate, to RHOSO 18.0): the
  disks of a running server are copied in snapshot pre-copy passes and only the
  final chunk delta is transferred after it is stopped.
- Add ``blocksync`` (``plugins/module_utils/blocksync.py``), a stdlib-only
  BLAKE2b chunk delta-sync engine with a verified manifest digest per pass, and
  ``tests/perf/bench_blocksync.py``.
- Add the modules ``import_workload_warm_snapshot``,
  ``import_workload_warm_sync`` and ``import_workload_rollback``, the role
  ``import_workloads_warm`` and the playbooks ``import_workloads_precopy.yml``,
  ``import_workloads_cutover.yml`` and ``rollback_workloads.yml``.
- ``import_workload_create_instance`` can record the new server in the warm
  state (``warm_state_dir``) and then never creates it twice.
- ``import_from_hypervisor`` exports disks read-only, bound to
  ``os_migrate_nbdkit_bind_address`` (default ``127.0.0.1``) with
  ``--shared=1``, quotes paths, keeps logs and PID files in a private directory
  and stops a previous export only through its PID file. With
  ``os_migrate_nbdkit_protocol: tcp``, set ``os_migrate_nbdkit_bind_address``
  to the hypervisor's migration-network IP (the role refuses the loopback
  default); exports started by 1.0.5 must be stopped by hand once.
- The cold path's nbdkit export of source volumes is read-only.
- ``blocksync`` recognises all-zero chunks with a byte count before hashing them
  (about 2.5× faster on never-written regions of thin volumes; chunks with data
  are unaffected).
- The source of the conversion hosts' security group rules is configurable
  (``os_migrate_conversion_secgroup_remote_ip_prefix``).
- ``os_migrate_workloads_preserve_volume_type`` (default ``false``) makes the
  cold path and ``import_workload_warm_sync`` create destination volumes with
  the serialized ``volume_type`` instead of the destination default type.
- Progress files are replaced atomically; destination volume parameters are
  shared by the cold and warm paths.
- Remove the unused ``sshpass`` from bindep.
- Resource files are now written with ``os_migrate_version: 1.1.0``: re-export
  data exported with 1.0.5.
- Warm migration (``import_workloads_warm``, ``blocksync``): ``assume_zero``
  is never applied to a final pass; a rollback with
  ``os_migrate_rollback_delete_dest_volumes`` keeps a destination volume that
  is still attached to a server other than the destination conversion host
  (``kept_volume_ids``, new ``conversion_host`` option of
  ``import_workload_rollback``); a cutover re-run fails instead of skipping a
  recorded destination server that is in ``ERROR`` or gone; recorded
  destination volumes without a source device are reported.
- ``import_workload_warm_sync`` reuses a recorded destination volume safely: a
  bootable one is marked bootable again (a pass interrupted between the create
  and the flag left it non-bootable, and every cutover then failed after the
  source was stopped), one still ``creating`` is waited for, and one in
  ``error`` is deleted and created again.
- ``import_workload_prelim`` names the progress and log files after the VM
  through ``filesystem.workload_file_stem`` (``%``, ``/`` and NUL written
  ``%25``, ``%2F``, ``%00``): a VM name such as ``../x`` or ``/etc/x`` no
  longer makes the modules write outside ``workload_logs``. Other names keep
  their file names.
- Attach locks on the conversion hosts name their owner as
  ``{migrator hostname}:{pid}``; before it waits, a module removes a lock whose
  owner ran on its own host and is gone (a module killed by a cancel or a
  timeout), instead of treating every owner as alive. A lock of another host,
  or one written by an earlier version, is left alone.
- ``import_workload_prelim`` treats the VM name as a literal when it looks for
  a destination server of the same name (``re.escape``, then only an equal
  name counts): ``web.01`` was skipped as already migrated because of an
  unrelated ``web-01``, and a name such as ``app(`` failed.
- The conversion-host SSH runs with ``ServerAliveInterval=15`` and
  ``ServerAliveCountMax=4``: a silent network partition fails a pass after
  about a minute instead of keeping the source stopped for about two hours.
- A warm cutover re-run finishes the recorded destination server instead of
  only reporting it: ``import_workload_create_instance`` waits for it to be
  ``ACTIVE`` and adds the floating IPs it lacks (a first attempt may have
  failed after Nova created the server). Floating IP creation keeps one
  already on the server's port for that fixed address, never a second.
- ``port_creation_mode: neutron`` recreates each port with its MAC, every fixed
  IP and its security groups. The export records each port's
  ``security_group_refs`` and ``is_port_security_enabled``, and the port is
  created with them and with the description ``os-migrate:{source port id}``,
  never with ``binding_profile`` (admin-only, host-specific). A re-run uses
  again the unbound port an earlier attempt created, and refuses any other port
  with the MAC or a port exported without its security groups (Nova applies a
  server's security groups only to the ports it creates). The default ``nova``
  mode is unchanged, and data exported before stays valid.
  ``import_workload_rollback`` deletes those ports
  (``deleted_port_ids``), also when the cutover failed before the server
  existed. Nova keeps such ports when their server is deleted later.
- A fixed IP reference without ``subnet_ref`` names only its address: Nova
  gets it as ``fixed_ip`` and Neutron as ``{ip_address}``, and Neutron picks
  the subnet of the port's network that holds it. Seamless Migrate writes such
  references for a per-VM NIC override (a VM's port moved to another network
  or given other addresses). Exported references keep their ``subnet_ref``.
