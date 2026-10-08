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
