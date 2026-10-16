<!-- Modified by the Seamless Migrate project, 2026. -->
The role conversion_host_content
install all the prerequisites in the conversion
hosts depending on the distribution.
In the case of using RHEL as the OS there is also
the posibility to configure the subscription
manager prior the packages install.

Supported conversion host images (`os_migrate_conversion_host_content_tasks`
in the role's defaults names the task file): RHEL (`rhel.yml`); CentOS Stream
9 and 10, Rocky Linux 9 and AlmaLinux 9 (`centos.yml`, with EPEL); Ubuntu
24.04 LTS or later (`ubuntu.yml`: `qemu-utils`, `nbdkit`, `libnbd-bin`,
`libguestfs-tools`). Any other OS fails the role. An Ubuntu cloud image logs
in as `ubuntu`, not `cloud-user`.

For further information about the role conversion_host_content refer to the
[official docs](https://os-migrate.github.io/os-migrate/roles/role-conversion_host_content.html).
