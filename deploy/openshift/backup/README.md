# Backups of the cluster deployment

What the control plane cannot recreate lives in two places: the **PostgreSQL database** (providers,
plans, migrations, recommendations, policies, users and every event — the audit trail) and the
**`seamless-data` PVC** (Ansible run directories and the warm state the executor records for the
migrations in flight). Credentials live in Kubernetes Secrets, never in either.

This overlay backs up the database. The data PVC is covered by the cluster's volume snapshots or
backup product (Velero / OADP); see "The data PVC" below.

## Apply

```bash
oc apply -k deploy/openshift                 # first: the namespace, Secrets, database and control plane
oc apply -k deploy/openshift/backup
oc -n seamless-migrate get cronjob seamless-pg-backup
oc -n seamless-migrate create job --from=cronjob/seamless-pg-backup backup-now   # a first dump right away
oc -n seamless-migrate logs -f job/backup-now
```

Every night at 01:17 UTC the CronJob runs `pg_dump -Fc` from the database's own image into the
`seamless-backups` PVC as `seamless-<UTC stamp>.dump` (written to `.part` first, then renamed, so a
failed run leaves no half dump) and deletes dumps older than `BACKUP_KEEP_DAYS` (14). The pod runs
under the restricted security context like the rest of the namespace and is admitted by its own
NetworkPolicies (`networkpolicy.yaml`).

Choose a storage class for the PVC that is **encrypted at rest** and lives **outside the database
volume's failure domain**; a dump holds plan data. Copy dumps off the cluster regularly
(`oc cp`, or a sidecar to object storage) — the PVC protects against a database failure, not against
losing the cluster.

With an external PostgreSQL (the `seamless-db` Secret's `SEAMLESS_DB_URL` points elsewhere), patch
`POSTGRESQL_HOST` in `cronjob.yaml` and `restore-job.yaml` to its address and add that address to the
egress NetworkPolicy.

## Restore

Order: **Secrets → database → data PVC**, with the control plane stopped.

1. Stop the control plane so nothing writes while rows are replaced:
   `oc -n seamless-migrate scale deploy/seamless --replicas=0`.
2. Make sure the Secrets exist (`seamless-db`, `seamless-tokens`, and `seamless-clouds`,
   `seamless-vmware`, `seamless-ai` if used): a restored database refers to providers whose
   credentials are in them.
3. Pick a dump: `oc -n seamless-migrate create job --from=cronjob/seamless-pg-backup list-dumps`
   and read its log, or mount the PVC in a debug pod.
4. Edit `restore-job.yaml`: set `BACKUP_FILE` to the dump's name. Apply it and follow its log:

   ```bash
   oc -n seamless-migrate apply -f deploy/openshift/backup/restore-job.yaml
   oc -n seamless-migrate logs -f job/seamless-pg-restore
   oc -n seamless-migrate delete job seamless-pg-restore
   ```

   `pg_restore --clean --if-exists --no-owner --no-privileges` replaces the dump's tables and keeps the
   roles' grants (Security.md §9.4: the owner role owns the schema, the application role has its
   grants). If the restore is into a new database created by the owner role, start the control plane
   once as the owner role so `create_schema()` adds any index the dump predates (SDD §11).
5. Restore the data PVC from its snapshot if the migrations in flight matter (a plan whose VMs are
   `completed` but not `finalized` needs its warm state for a rollback; a plan that is `finalized`
   or `draft` does not).
6. Start the control plane: `oc -n seamless-migrate scale deploy/seamless --replicas=1`, then check
   `GET /api/v1/health` reports `db: ok` and the dashboard lists the plans you expect. Migrations
   that were in flight at backup time come back as the orchestrator re-validates them (SDD §8).

## Restore drill

Quarterly, and before every upgrade (RELEASING.md "Upgrading"): restore the latest dump into a scratch
namespace (`oc new-project seamless-drill`, the Secrets, `postgres-statefulset.yaml`,
`postgres-service.yaml`, this overlay's PVC with the dump copied in, `restore-job.yaml`), start the
control plane there with `SEAMLESS_DEMO=false`, and compare `GET /api/v1/stats` with production's.
Record the date, the dump used and the result in QASuite §13.5's log.

## The data PVC

`seamless-data` holds the executor's run directories and the recorded warm state. Snapshot it with the
cluster's `VolumeSnapshot` class, or include the namespace in Velero / OADP with the PVC's data
(`defaultVolumesToFsBackup` or a CSI snapshot). Take the snapshot while no cutover is running
(plans paused), and restore it in step 5 above only when migrations in flight have to survive.

## Compose

The local stack keeps its one-line `pg_dump` recipe in `deploy/compose/README.md`.
