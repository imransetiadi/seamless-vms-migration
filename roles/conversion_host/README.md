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

## Injected host key

With `os_migrate_conversion_host_key_private_path` naming the private half of
an ed25519 key pair (an OpenSSH private key file, 0600), the role creates the
conversion host with that key as its SSH host key: the server's user data is
cloud-init's `ssh_keys` (`ed25519_private`, the file's text, and
`ed25519_public`, which `ssh-keygen -y` reads from the file) with
`ssh_deletekeys: true`: cloud-init deletes the image's host keys and writes
that key alone, so the host's key is known before anything connects to it. A
file `ssh-keygen` cannot read, or a key of another type, fails the role before
the server is created.
`deploy_conversion_hosts.yml` takes each side's key as
`os_migrate_src_conversion_host_key_private_path` and
`os_migrate_dst_conversion_host_key_private_path`.

```yaml
os_migrate_src_conversion_host_key_private_path: /path/to/src_host_key
os_migrate_dst_conversion_host_key_private_path: /path/to/dst_host_key
```

The role looks the host up by its name first. A server it finds keeps its own
key and gets no user data, whatever the variable says; without the variable no
server gets user data, as upstream.

The lookup and the server task run with `no_log`: the server task's arguments
hold the private key, and Nova returns a server's user data, base64-encoded, to
an admin credential, so their results could carry it back. Neither prints
anything at any verbosity, a failure's message included.

After each server task the role writes, beside its keys,
`{{ os_migrate_data_dir }}/conversion/hosts.json` (0600): the servers the
playbook run created and those it found, by their side's inventory name and
their Nova server id. It holds no key material:

```json
{
    "created": [{"alias": "os_migrate_conv_src", "id": "<server id>"}],
    "found": [{"alias": "os_migrate_conv_dst", "id": "<server id>"}]
}
```

A server is created when the lookup found none of its name and the server task
changed something, else found. Only a server under `created` holds the injected
key.

## Floating IP

With `os_migrate_conversion_manage_fip` (default `true`), the role gives the
conversion host a floating IP: the address
`os_migrate_conversion_floating_ip_address` names, else one the `floating_ip`
module takes on the external network
`os_migrate_conversion_external_network_name`. In that second case
`os_migrate_conversion_floating_ip_reuse` (default `true`) is the module's
`reuse`:

* `true`, as upstream: a floating IP the credential's project holds detached
  on that network when there is one, else a new one;
* `false`: always a new one, never one the project holds detached - a tenant's
  own floating IP, or an address reserved for a later migration.

A host that holds a floating IP already keeps it either way, and `reuse` plays
no part with an address. `deploy_conversion_hosts.yml` takes each side's value
as `os_migrate_src_conversion_floating_ip_reuse` and
`os_migrate_dst_conversion_floating_ip_reuse`:

```yaml
os_migrate_dst_conversion_floating_ip_reuse: false
```
