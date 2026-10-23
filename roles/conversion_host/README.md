<!-- Modified by the Seamless Migrate project, 2026. -->
The role conversion_host
deploys all the resources required to do a
workload migration. This includes the conversion
hosts networks, subnets, routers, security groups,
security group rules, keypairs, and the conversion
hosts itself.

For further information about the role conversion_host refer to the
[official docs](https://os-migrate.github.io/os-migrate/roles/role-conversion_host.html).

## Security group

The conversion hosts get a security group (`os_migrate_conversion_secgroup_name`)
allowing SSH (TCP 22) and ICMP from `os_migrate_conversion_secgroup_remote_ip_prefix`.
The default, `0.0.0.0/0`, keeps the historical behaviour and exposes the hosts'
SSH service to every source that can route to them.

Operators should set it to the narrowest CIDR that covers the hosts that connect
to the conversion hosts:

* the migrator, i.e. the host running the playbooks (the Seamless control plane);
* the peer conversion host, because the destination conversion host opens SSH to
  the source conversion host to transfer the disks.

```yaml
os_migrate_conversion_secgroup_remote_ip_prefix: 192.0.2.0/24
```

When the migrator and the peer conversion host are not in one CIDR, use a
prefix that covers both (or manage the security group rules yourself).

## Pinned host keys

With `os_migrate_conversion_host_known_hosts` naming a known_hosts file, the
inventory this role builds (`conv_host_inventory.yml`) checks each conversion
host's key strictly against that file alone, under the host's inventory name,
`os_migrate_conv_src` or `os_migrate_conv_dst` (`HostKeyAlias`), so a floating
IP that moves changes nothing. The file holds each host's keys under its name,
plain or hashed (`ssh-keygen -H`); `ssh-keygen -F` reads their types for
`HostKeyAlgorithms` (an RSA key's are `rsa-sha2-512,rsa-sha2-256`), and a file
it cannot read fails the inventory. Ansible's ssh, scp and sftp get the options
through `ansible_ssh_common_args`, and the host gets
`ansible_ssh_host_key_checking: true`: Ansible's own host key checking setting
cannot undo them. The roles that run the migration modules pass the same file
to each as `ssh_known_hosts`. A key the file does not pin is refused at once,
never learned.

```yaml
os_migrate_conversion_host_known_hosts: /path/to/known_hosts
```

Unset or empty, the hosts are added as upstream adds them, with
`-o StrictHostKeyChecking=no`.
