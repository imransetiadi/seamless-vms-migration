Import From Hypervisor Role
============================

This role exports, with `qemu-nbd`, the disks of workloads that have
`use_nbdkit_direct: true` in their migration parameters, directly on their
OpenStack hypervisors (compute nodes), so that the destination conversion host
can read them without a source conversion host.

## Purpose

This role is part of the nbdkit direct mode migration workflow. It:

1. Reads workloads from `workloads.yml`
2. Filters workloads with `use_nbdkit_direct: true`
3. For each workload (which must be SHUTOFF):
   - Connects to the hypervisor (from `hypervisor_hostname` in workload data)
   - Finds the instance disks (`disk`, `disk.eph*`)
   - Inspects each disk format with `qemu-img info`
   - Exports each disk with `qemu-nbd` on port `os_migrate_nbdkit_port + N`
   - Updates the workload file with the NBD URIs (`nbdkit_disks`)

## Workflow Order

```
1. export_workloads.yml      # Exports workloads with hypervisor_hostname
2. import_from_hypervisor.yml # Exports the disks with qemu-nbd (this role)
3. import_workloads.yml       # Migrates using the NBD URIs
```

## Usage

```bash
# 1. Export workloads
ansible-playbook export_workloads.yml -e os_migrate_data_dir=/data

# 2. Export the disks on the hypervisors
ansible-playbook import_from_hypervisor.yml -e os_migrate_data_dir=/data

# 3. Import workloads
ansible-playbook import_workloads.yml -e os_migrate_data_dir=/data
```

## Export security

The exports carry whole guest disks, so they are locked down by default:

* **Read-only**: `qemu-nbd --read-only` while `os_migrate_nbdkit_readonly` is
  true (the default). The migration never writes to the source disks.
* **Bound**: `qemu-nbd --bind {{ os_migrate_nbdkit_bind_address }}`, by default
  `127.0.0.1`, so the export is not reachable from the network.
* **One client**: `--shared=1`.
* **Quoted**: disk paths, instance IDs and every other value placed on the
  hypervisor's command line are shell-quoted.
* **Private files**: logs and PID files go to `os_migrate_nbdkit_log_dir`
  (default `/var/log/os-migrate-nbd`), created with mode 0700 and owned by the
  SSH user, instead of predictable names in `/tmp`.
* **Exact restart**: re-running the role stops the previous export of a port
  through that port's PID file, and only if the process recorded there is the
  `qemu-nbd` started with that PID file. A port used by anything else makes the
  role fail instead of killing processes by pattern. Exports started by older
  versions of this role (without a PID file) must be stopped by hand once.

### Choosing the protocol

* **`os_migrate_nbdkit_protocol: ssh`** (recommended): the conversion host
  reaches the export through SSH to the hypervisor
  (`nbd+ssh://{{ os_migrate_nbdkit_ssh_user }}@<hypervisor>:<port>`), so
  `qemu-nbd` only needs to listen on the loopback address. Keep the default
  `os_migrate_nbdkit_bind_address: 127.0.0.1`.
* **`os_migrate_nbdkit_protocol: tcp`** (the default, for compatibility): the
  conversion host connects to `nbd://<address>:<port>` directly, so `qemu-nbd`
  must listen on an address the conversion host can reach. Set
  `os_migrate_nbdkit_bind_address` to the hypervisor's **migration-network IP**,
  either globally or, with several hypervisors, as a host variable of each
  hypervisor in the inventory; the NBD URIs then use that address. With the
  loopback default the role fails early with an explanation. `0.0.0.0` (every
  interface, the URIs use the hypervisor name) restores the old behaviour and
  should only be used on an isolated migration network. NBD itself is neither
  encrypted nor authenticated.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `os_migrate_nbdkit_protocol` | `tcp` | `tcp` or `ssh`, see above |
| `os_migrate_nbdkit_bind_address` | `127.0.0.1` | address `qemu-nbd` listens on (host variables of the hypervisor win) |
| `os_migrate_nbdkit_readonly` | `true` | export the disks read-only |
| `os_migrate_nbdkit_port` | `10809` | port of the first disk (`_migration_params.nbdkit_port` overrides it per workload) |
| `os_migrate_nbdkit_ssh_user` | `stack` | user in `nbd+ssh://` URIs |
| `os_migrate_nbdkit_log_dir` | `/var/log/os-migrate-nbd` | private directory for logs and PID files on the hypervisor |
| `os_migrate_nbdkit_nova_instances_dir` | `/var/lib/nova/instances` | Nova instances directory on the hypervisor |

The SSH user on the hypervisors needs passwordless `sudo`, `qemu-img`,
`qemu-nbd` (with `--pid-file`, QEMU 4.1 or later) and `netstat`.
