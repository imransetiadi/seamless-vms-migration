Warm workload migration
=======================

Warm migration copies the disks of a *running* server in **pre-copy passes**
and transfers only the **final delta** after the server is stopped. The
downtime therefore depends on how much the guest wrote since the last pass,
not on the disk size. It is an addition of Seamless Migrate (collection
1.1.0) to the cold ``import_workloads.yml`` path, which stops the server
before the whole copy.

How a pass works
----------------

#. Every volume of the workload is snapshotted (forced; the server keeps
   running and nothing is detached), a temporary volume is created from each
   snapshot and attached to the **source** conversion host.
#. The destination volumes are created on the first pass (same name, size,
   bootability and editable parameters as the cold path would use) and are
   attached to the **destination** conversion host for the duration of the
   pass.
#. On the destination host ``blocksync receive`` hashes both sides chunk by
   chunk (BLAKE2b-128 over 4 MiB chunks by default) and pulls only the
   changed chunks from ``blocksync send`` on the source host, over the same
   SSH link the cold migration uses. All-zero chunks are recognised without
   hashing; a manifest digest over every chunk digest is verified at the end
   of each pass.
#. The temporary snapshots and volumes are removed, and the pass is appended
   to the *warm state file*.

The first pass is a ``full`` pass (every non-zero chunk of the source is
sent); later passes are ``delta`` passes; the cutover runs a ``final`` pass
after the source server was stopped and then creates the destination server
from the synchronised volumes.

Playbooks
---------

Run ``export_workloads.yml`` first, exactly as for the cold path, then:

``import_workloads_precopy.yml``
    snapshot → sync (``auto``: ``full`` on the first pass, then ``delta``) →
    remove the snapshot. It can run any number of times while the server keeps
    serving; a scheduler such as the Seamless Migrate control plane repeats it
    until the delta converges below a threshold.

``import_workloads_cutover.yml``
    stop the source server (wait for ``SHUTOFF``) → snapshot → sync
    (``final``) → remove the snapshot → create the destination server with
    ``import_workload_create_instance`` and record its id in the warm state. A
    cutover whose destination server already exists is a no-op, and the
    cutover refuses to stop a source whose name is already used by a
    destination server it did not create.

``rollback_workloads.yml``
    delete the recorded destination server (and, with
    ``os_migrate_rollback_delete_dest_volumes: true``, the destination volumes
    and the warm state) → start the source server again. For workloads without
    a warm state (for example a cold migration) set
    ``os_migrate_rollback_match_by_name: true`` to delete the only destination
    server named exactly like the workload.

Warm state file
---------------

``{{ os_migrate_warm_state_dir }}/{source_server_id}.json`` (by default
``{{ os_migrate_data_dir }}/workload_warm/``), written atomically with mode
``0600``::

    { "server_id": "…", "server_name": "…", "transfer_uuid": "…",
      "dest_volumes": { "/dev/vda": {"dest_id": "…", "size": 20, "bootable": true, "name": "…"} },
      "passes": [ { "number": 1, "kind": "full", "started_at": "…", "ended_at": "…",
                    "bytes_scanned": 0, "bytes_changed": 0, "bytes_transferred": 0,
                    "duration_s": 0.0 } ],
      "pending_snapshot": null,
      "destination_server_id": null }

``pending_snapshot`` lists the temporary snapshots, volumes and images of a
pass in progress. Every resource is recorded before it is waited on, so a
pass interrupted at any point is cleaned up by the rescue of the role, by the
next pass, or by the rollback. Per-disk progress is written to the usual
progress file ``workload_logs/{name}.state`` and the module log to
``workload_logs/{name}.log``.

Variables
---------

.. list-table::
   :header-rows: 1
   :widths: 34 22 44

   * - Variable
     - Default
     - Meaning
   * - ``os_migrate_warm_chunk_size``
     - ``4194304``
     - blocksync chunk size in bytes
   * - ``os_migrate_warm_workers``
     - ``4``
     - read-ahead hashing threads per conversion host and disk
   * - ``os_migrate_warm_parallel_disks``
     - ``4``
     - disks of a workload synchronised at the same time
   * - ``os_migrate_warm_assume_zero``
     - ``false``
     - skip reading destination volumes created by the first pass; only for
       backends that return zeros for never-written blocks (Ceph RBD, thin LVM)
   * - ``os_migrate_workloads_preserve_volume_type``
     - ``false``
     - create destination volumes with the serialized ``volume_type``
       (rewrite it to a destination type in the workload data first) instead
       of the destination default; also honoured by ``import_workloads``
   * - ``os_migrate_rollback_delete_dest_volumes``
     - ``false``
     - the rollback also deletes the destination volumes and the warm state
   * - ``os_migrate_rollback_match_by_name``
     - ``false``
     - rollback of a workload without a warm state: delete the only
       destination server named exactly like the workload
   * - ``os_migrate_warm_python_interpreter``
     - ``python3``
     - Python 3.6+ on the conversion hosts (``/usr/libexec/platform-python``
       on RHEL 8)
   * - ``os_migrate_warm_state_dir``
     - ``{{ os_migrate_data_dir }}/workload_warm``
     - warm state files
   * - ``os_migrate_workloads_filter``
     - ``[{regex: .*}]``
     - workloads to process (name filter, as for ``import_workloads``)
   * - ``os_migrate_workload_cleanup_on_failure``
     - ``true``
     - remove the temporary snapshot when a pass fails
   * - ``os_migrate_workload_boot_volume_prefix``
     - ``os-migrate-``
     - name prefix of the temporary snapshots, volumes and images
   * - ``os_migrate_timeout``
     - ``1800``
     - timeout of OpenStack operations (not of the data transfer)

The usual os-migrate variables apply as well: ``os_migrate_data_dir``,
``os_migrate_src_auth``/``os_migrate_dst_auth`` and the other cloud settings,
and the conversion host variables (``os_migrate_src_conversion_host_name``,
``os_migrate_dst_conversion_host_name``, ``os_migrate_conversion_host_ssh_user``,
``os_migrate_conversion_keypair_private_path``).

Requirements and limits
-----------------------

* Conversion hosts in both clouds, deployed and linked with
  ``deploy_conversion_hosts.yml`` (the destination host reaches the source
  host over SSH with the link key of ``os_migrate_conversion_host_ssh_user``),
  with passwordless ``sudo`` and Python 3.6+.
* Workloads with ``data_copy: true`` and without ``use_nbdkit_direct``.
* No multi-attach volumes. Image-booted servers follow ``boot_disk_copy``:
  with it every pass snapshots the server to a temporary image (slower),
  without it only the data volumes are synchronised and the destination
  boots from the image.
* Destination volumes take the source volume's editable parameters (the boot
  volume takes ``boot_volume_params``) and, unless
  ``os_migrate_workloads_preserve_volume_type`` is set, the destination's
  default volume type.
* Multi-volume pre-copy snapshots are not atomic; this is harmless because
  the final pass runs after the source is shut down.

Downtime
--------

The final pass has to read every chunk of every disk once on both sides
(the *scan*), then transfer the chunks that changed since the previous pass.
With the defaults the scan runs at a few hundred MiB/s per disk stream and the
disks of one workload are scanned in parallel, so the downtime of a workload
whose largest disk is 100 GiB is a few minutes plus the fixed costs of
stopping, snapshotting and booting. ``tests/perf/bench_blocksync.py`` measures
the scan rate of a given host; the Seamless Migrate control plane uses that
rate, the measured write rate of the guest and the link speed to estimate the
downtime of every workload before the cutover is approved.
