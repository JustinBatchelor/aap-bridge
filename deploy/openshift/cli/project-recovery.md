# Recover projects rejected during Manual creation

Use this for the `scm_track_submodules: Update options must be set to false for
manual projects` error. Older Transform code left this option on the Manual project
payload. The fixed code defers it and restores it with the other SCM settings in
Phase 2. The local repair helper preserves the original option in existing
transformed projects without rerunning Export or Transform or changing credentials.

This procedure keeps repository syncs paused. Finish or stop all Bridge commands and
Jobs first. Cancel target AAP project updates that are still running/pending while
investigating authentication failures. Do not run Cleanup, reset the whole database,
or force-reimport all Phase 1 resources.

## 1. Get the fix and start the toolbox

From the repository root on your workstation:

```bash
git switch feat/openshift-cli
git pull --ff-only
export NAMESPACE=aap-bridge-cli
export LOCAL=deploy/openshift/local/cli
oc config current-context

oc -n "$NAMESPACE" scale deployment/aap-bridge-cli --replicas=1
oc -n "$NAMESPACE" rollout status deployment/aap-bridge-cli --timeout=10m
```

The toolbox starts with `sleep infinity`; it does not resume the interrupted import.
Keep existing Secrets, PVCs, and the current target connection settings.

## 2. Back up tracking and transformed artifacts

Run commands one at a time; stop if any command fails. These private backups can
contain secrets:

```bash
umask 077
mkdir -p "$LOCAL/backup"
export RECOVERY_STAMP=$(date +%Y%m%d-%H%M%S)

oc -n "$NAMESPACE" exec statefulset/aap-bridge-db -- sh -ec \
  'PGPASSWORD="$POSTGRESQL_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRESQL_USER" -d "$POSTGRESQL_DATABASE" -Fc' \
  > "$LOCAL/backup/project-state-$RECOVERY_STAMP.dump"

oc -n "$NAMESPACE" exec deployment/aap-bridge-cli -c cli -- \
  tar -C /data -czf - xformed \
  > "$LOCAL/backup/project-artifacts-$RECOVERY_STAMP.tar.gz"
```

## 3. Load the fixed Python code without rebuilding the image

Pulling Git updates your workstation only. The following copies the fixed Python
package and helpers into writable `/tmp`; it does not alter the installed image or
copy frontend assets. Stop if either side of the transfer fails:

```bash
set -o pipefail
oc -n "$NAMESPACE" exec deployment/aap-bridge-cli -c cli -- \
  mkdir -p /tmp/aap-bridge-project-fix

tar --exclude='__pycache__' --exclude='*.pyc' -cf - \
  src/aap_migration tools/repair_deferred_projects.py tools/update_exported_credential.py |
  oc -n "$NAMESPACE" exec -i deployment/aap-bridge-cli -c cli -- \
    tar --no-same-owner -C /tmp/aap-bridge-project-fix -xf -
```

Verify the override is loading the fixed transformer:

```bash
oc -n "$NAMESPACE" exec deployment/aap-bridge-cli -c cli -- \
  env PYTHONPATH=/tmp/aap-bridge-project-fix/src python3.12 -c '
import inspect
from aap_migration.migration.transformer import ProjectTransformer
assert "scm_track_submodules" in inspect.getsource(ProjectTransformer)
print(inspect.getfile(ProjectTransformer))
'
```

Expect a path beneath `/tmp/aap-bridge-project-fix/src/`. This override is temporary:
re-copy it after any pod restart and keep the `PYTHONPATH` prefix on subsequent
Bridge commands. A normal menu invocation continues using the old installed code.
For a permanent fix, follow the [upgrade procedure](operations.md#upgrade-or-roll-back)
to build the published fixed commit and select its new immutable image digest.

## 4. Preview and repair existing transformed project files

```bash
oc -n "$NAMESPACE" exec deployment/aap-bridge-cli -c cli -- \
  python3.12 /tmp/aap-bridge-project-fix/tools/repair_deferred_projects.py
```

The helper checks every project has inactive SCM fields. For deferred projects it
moves the legacy `scm_track_submodules` option into `_deferred_scm_details`, preserving
its original boolean value. Invalid, conflicting, or active SCM payloads stop the
operation before any writes. It displays IDs/counts only, never URLs or secrets.
After a successful preview:

```bash
oc -n "$NAMESPACE" exec deployment/aap-bridge-cli -c cli -- \
  python3.12 /tmp/aap-bridge-project-fix/tools/repair_deferred_projects.py --apply
```

Only project chunks are changed, with private atomic replacements; credential files,
metadata, and database mappings are untouched. Each replacement is atomic, but the
whole batch is not a transaction: on a write failure, review the completed/pending
report and rerun safely before importing. There is no AAP access or sync in this step.

## 5. Recreate missing projects only

The following refreshes tracking for **all projects**, re-matches existing projects
by name/organization, and creates only missing projects as Manual. It does not patch
existing projects' SCM settings or retry their failed syncs. All other resource
tracking, including credential mappings, is preserved:

```bash
oc -n "$NAMESPACE" exec -it deployment/aap-bridge-cli -c cli -- \
  env PYTHONPATH=/tmp/aap-bridge-project-fix/src \
  aap-bridge --config /etc/aap-bridge/config.yaml \
  --log-file /data/logs/project-recovery.log \
  import --input /data/xformed --phase phase1 \
  --resource-type projects --skip-dependencies --force-reimport
```

Keep all three project-recovery options. `--force-reimport` changes tracking even
before confirmation, so do not combine it with `--dry-run` as a preview.
Existing projects must retain their exported names/organizations for matching.
If two projects were missing and everything else matches, expect two creations
and the remaining projects skipped. Stop on errors; do not reset the whole run.

In target AAP, confirm the recovered projects exist as Manual, without repository
URLs or SCM credentials. Their new target IDs are saved for Phase 2. If deletion
also removed dependent resources, project recovery alone does not restore those.

## Before resuming Phase 2

Inspect and resolve the three independent failed project syncs. Verify credentials
and repository access with the admins before allowing more attempts. Do not rerun
Export or Transform just to perform this repair, and do not enable schedules yet.

An `SSL certificate problem: self-signed certificate in certificate chain` error
comes from Git in AAP's project-update environment. Bridge's `verify_ssl` setting
does not control it. Obtain the repository's verified issuing CA chain from the
admins and configure trust for the environment that actually performs project
updates; changing gateway trust alone is not proof that this environment trusts it.
See the [Operator CA bundle documentation][operator-ca]. If another project using
the same credential succeeds, compare the existing jobs' exact URL scheme/host/port,
execution environment, execution node, and timestamps before assuming their trust
settings are identical. The TLS failure alone does not validate the credential.

When approved to resume, use the fixed image or the temporary source override:

```bash
oc -n "$NAMESPACE" exec -it deployment/aap-bridge-cli -c cli -- \
  env PYTHONPATH=/tmp/aap-bridge-project-fix/src \
  aap-bridge --config /etc/aap-bridge/config.yaml \
  import --input /data/xformed --phase phase2
```

This last command **can start repository syncs**; it is not part of the Manual
project recovery. The fixed patcher restores the saved submodule option and checks
it when deciding whether an existing project's SCM configuration already matches.

[operator-ca]: https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/install-assembly_installing_controller_operator
