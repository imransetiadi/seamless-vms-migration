<!-- Modified by the Seamless Migrate project, 2026. -->
The role import_workloads
will validate the serialized data of
the exported workloads, and it will read the exported
metadata.
Then it will start the actual workload migration, it will
first check that both conversion hosts are reachable, then
it will export the selected source guests by exposing its volumes,
transfering them to the destination tenant, and creating a new guest booting
from the recently imported volume.

It records each workload it copies for a rollback, in the state file the warm
path keeps (`{{ os_migrate_warm_state_dir }}/{source_server_id}.json`, by
default under `{{ os_migrate_data_dir }}/workload_warm/`): the volumes its
transfer created, as soon as they exist, and the new server, as soon as Nova
accepts it. `rollback_workloads.yml` with
`os_migrate_rollback_delete_dest_volumes: true` then deletes exactly those
volumes; a cold retry never reuses them, as the transfer creates new ones.

For further information about the role import_workloads refer to the
[official docs](https://os-migrate.github.io/os-migrate/roles/role-import_workloads.html).
