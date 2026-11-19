# Backups, upgrades, and cleanup

Use the [deployment README](readme.md) for installation and [Job guide](jobs.md)
for automated runs. Run commands from the repository root with `NAMESPACE` set.

## Back up

Complete migrations and keep Job mode inactive. With the toolbox running but no
CLI command active, back up state and artifacts together to protected local files:

```bash
umask 077
mkdir -p deploy/openshift/local/cli/backup
oc exec -n "$NAMESPACE" statefulset/aap-bridge-db -- sh -ec \
  'PGPASSWORD="$POSTGRESQL_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRESQL_USER" -d "$POSTGRESQL_DATABASE" -Fc' \
  > deploy/openshift/local/cli/backup/state.dump
oc exec -n "$NAMESPACE" deployment/aap-bridge-cli -c cli -- tar -C /data -czf - . \
  > deploy/openshift/local/cli/backup/artifacts.tar.gz
oc -n "$NAMESPACE" get secret aap-bridge aap-bridge-db aap-bridge-connections -o yaml \
  > deploy/openshift/local/cli/backup/secrets.yaml
```

Retain configuration, image/build records, and PostgreSQL version with the backup.
Restrict and encrypt backup storage; exports can contain customer information.
Restore into an isolated namespace and verify the database and artifact pair before
using it. Restore Secrets before starting PostgreSQL; use `pg_restore` against the
matching database version and restore `/data` while no migration process is active.

## Upgrade or roll back

For upgrades, finish jobs, take backups, build a newly published commit with CLI
mode, and update the installation overlay and any Job overlay to the new digest. Apply only
the installation while no Job runs. Preserve PVCs and existing Secrets. The toolbox
uses Recreate updates. For rollback, restore the previous digest to both overlays;
if database/schema compatibility changed, restore its matching state/artifact backup
as well. Image rollback alone does not undo migration effects on target AAP.

## Remove workloads

Workload removal preserves data:

```bash
# Finish active commands first. Review any Job before removing it.
oc -n "$NAMESPACE" delete deployment aap-bridge-cli
oc -n "$NAMESPACE" delete job aap-bridge-cli-run --ignore-not-found
oc -n "$NAMESPACE" delete statefulset aap-bridge-db
```

## Delete persistent data

PVC/Secret deletion is a **separate destructive operation**, only after backup and
explicit approval from the data owner:

```bash
oc -n "$NAMESPACE" delete pvc aap-bridge-data aap-bridge-db
oc -n "$NAMESPACE" delete secret aap-bridge aap-bridge-db aap-bridge-connections
```

Deleting the namespace deletes its remaining resources and PVCs too. Do not use
`oc delete -k` as a workload-only cleanup because the package includes PVCs.
