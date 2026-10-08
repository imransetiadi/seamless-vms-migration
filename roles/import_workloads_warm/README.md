Import Workloads Warm Role
==========================

Warm migration of OpenStack workloads: the disks of a running server are
copied in **pre-copy passes** while it keeps running, and only the **final
delta** is transferred after it is stopped, so the downtime depends on how
much changed since the last pass rather than on the disk size.

Each pass snapshots the source volumes (forced, the server is never stopped
or detached), creates temporary volumes from the snapshots and attaches them
to the source conversion host. The destination volumes (created on the first
pass, reused afterwards) are attached to the destination conversion host,
where `blocksync receive` compares both sides chunk by chunk (BLAKE2b-128,
4 MiB chunks by default) and pulls only the changed chunks from
`blocksync send` on the source conversion host, over the same SSH link the
cold migration uses. Every pass ends with a verification of a manifest digest
over all chunk digests.

The role is used by three playbooks; `import_workloads_warm_action` selects
the task file:

| Playbook | Per filtered workload |
|---|---|
| `import_workloads_precopy.yml` | snapshot → sync (`auto`: `full` on the first pass, then `delta`) → remove the snapshot |
| `import_workloads_cutover.yml` | stop the source (wait for SHUTOFF) → snapshot → sync (`final`) → remove the snapshot → create the destination server from the synchronised volumes (`import_workload_create_instance`) and record its ID |
| `rollback_workloads.yml` | delete the recorded destination server (and, with `os_migrate_rollback_delete_dest_volumes`, the destination volumes and the warm state) → start the source server |

Run `export_workloads.yml` first, as for `import_workloads.yml`. Pre-copy can
run any number of times; a cutover that already created the destination
server is a no-op, and the cutover refuses to stop a source whose name is
already used by a destination server it did not create.

## Warm state file

`{{ os_migrate_warm_state_dir }}/{source_server_id}.json` (by default
`{{ os_migrate_data_dir }}/workload_warm/`), written atomically with mode 0600:

```json
{ "server_id": "…", "server_name": "…", "transfer_uuid": "…",
  "dest_volumes": { "/dev/vda": {"dest_id": "…", "size": 20, "bootable": true, "name": "…"} },
  "passes": [ { "number": 1, "kind": "full", "started_at": "…", "ended_at": "…",
                "bytes_scanned": 0, "bytes_changed": 0, "bytes_transferred": 0, "duration_s": 0.0 } ],
  "pending_snapshot": null,
  "destination_server_id": null }
```

`pending_snapshot` lists the temporary snapshots, volumes and images of a
pass in progress. Every resource is recorded before it is waited on, so a
pass interrupted at any point is cleaned up by the rescue of the role, by the
next pass, or by the rollback. Per-disk progress of a pass is written to the
usual os-migrate progress file `workload_logs/{name}.state` (`{device: pct}`),
and the module log to `workload_logs/{name}.log`.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `os_migrate_warm_chunk_size` | `4194304` | blocksync chunk size in bytes |
| `os_migrate_warm_workers` | `4` | read-ahead hashing threads per conversion host and disk |
| `os_migrate_rollback_delete_dest_volumes` | `false` | rollback also deletes the destination volumes (recorded ones and those attached to the deleted server) and the warm state |
| `os_migrate_warm_parallel_disks` | `4` | disks of a workload synchronised at the same time |
| `os_migrate_warm_assume_zero` | `false` | skip reading destination volumes created by the first pass; only for backends that return zeros for never-written blocks (Ceph RBD, thin LVM) |
| `os_migrate_workloads_preserve_volume_type` | `false` | create destination volumes with the serialized `volume_type` (rewrite it to a destination type in the workload data first) instead of the destination default; also honoured by `import_workloads` |
| `os_migrate_warm_python_interpreter` | `python3` | Python 3.6+ on the conversion hosts (`/usr/libexec/platform-python` on RHEL 8) |
| `os_migrate_warm_state_dir` | `{{ os_migrate_data_dir }}/workload_warm` | warm state files |
| `os_migrate_rollback_match_by_name` | `false` | rollback of a workload without a warm state (e.g. migrated cold): delete the only destination server named exactly like the workload |
| `os_migrate_workloads_filter` | `[{regex: .*}]` | workloads to process (name filter, as for `import_workloads`) |
| `os_migrate_workload_cleanup_on_failure` | `true` | remove the temporary snapshot when a pass fails |
| `os_migrate_workload_boot_volume_prefix` | `os-migrate-` | name prefix of the temporary snapshots, volumes and images |
| `os_migrate_timeout` | `1800` | timeout of OpenStack operations (not of the data transfer) |

The usual os-migrate variables apply as well: `os_migrate_data_dir`,
`os_migrate_src_auth`/`os_migrate_dst_auth` and the other cloud settings,
and the conversion host variables (`os_migrate_src_conversion_host_name`,
`os_migrate_dst_conversion_host_name`, `os_migrate_conversion_host_ssh_user`,
`os_migrate_conversion_keypair_private_path`).

## Requirements

* Conversion hosts in both clouds, deployed and linked with
  `deploy_conversion_hosts.yml` (the destination host reaches the source host
  over SSH with the link key of `os_migrate_conversion_host_ssh_user`), with
  passwordless `sudo` and Python 3.6+.
* Workloads with `data_copy: true` and without `use_nbdkit_direct`.
* No multi-attach volumes. Image-booted servers follow `boot_disk_copy`: with
  it every pass snapshots the server to a temporary image (slower), without
  it only the data volumes are synchronised and the destination boots from
  the image.
* As in the cold path, destination volumes take the source volume's
  editable parameters (the boot volume takes `boot_volume_params`) and,
  unless `os_migrate_workloads_preserve_volume_type` is set, the
  destination's default volume type.

Multi-volume pre-copy snapshots are not atomic; this is harmless because the
final pass runs after the source is shut down.
