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
