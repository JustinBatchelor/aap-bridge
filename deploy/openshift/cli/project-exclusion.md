# Skip a project and its dependent automation

Use this when you deliberately want to finish the migration without a project
such as **ADO**. The helper previews exclusions, then creates a separate filtered
copy of `/data/xformed`. Original files, credentials, database mappings, and
existing target objects stay intact. It makes no AAP requests.

This procedure assumes the excluded inventories and templates are unused on the
target. It does not delete existing objects, cancel their updates, or prevent
someone or an external system from launching them. Finish or stop other Bridge
imports and Jobs first. Keep target schedules disabled until your cutover.

## 1. Copy the helper and current Python code

Run from the repository root on your workstation, checking the customer cluster
context before scaling the existing toolbox:

```bash
git switch feat/openshift-cli
git pull --ff-only
export NAMESPACE=aap-bridge-cli
oc config current-context
oc -n "$NAMESPACE" scale deployment/aap-bridge-cli --replicas=1
oc -n "$NAMESPACE" rollout status deployment/aap-bridge-cli --timeout=10m

set -o pipefail
oc -n "$NAMESPACE" exec deployment/aap-bridge-cli -c cli -- \
  mkdir -p /tmp/aap-bridge-project-fix
tar --exclude='__pycache__' --exclude='*.pyc' -cf - \
  src/aap_migration tools/exclude_transformed_project.py tools/update_exported_credential.py |
  oc -n "$NAMESPACE" exec -i deployment/aap-bridge-cli -c cli -- \
    tar --no-same-owner -C /tmp/aap-bridge-project-fix -xf -
```

The toolbox starts with `sleep infinity`; scaling up does not restart an import.
This transfer copies Python code only. Re-copy it after a pod restart. The
`PYTHONPATH` override below uses the current Phase 2 project fixes without an image
rebuild. Keep your existing configuration, Secrets, and persistent volumes.

## 2. Preview the excluded objects

```bash
oc -n "$NAMESPACE" exec deployment/aap-bridge-cli -c cli -- \
  python3.12 /tmp/aap-bridge-project-fix/tools/exclude_transformed_project.py \
  --input /data/xformed --output /data/xformed-without-ado --project-name ADO
```

Review **every exclusion**, not just the project count. Matching is exact: `ADO`
does not match `ADO version`. If names repeat, use `--project-id` with the **source
project ID**, not an update-job ID. Preview does not create the output directory.

The helper excludes static dependencies transitively:

- Inventory sources using the project, their entire inventories, and the hosts,
  groups, sources, and templates that depend on those inventories.
- Job templates using the project; whole workflows containing an excluded
  template or inventory, including workflows that call those workflows.
- Related schedules, workflow nodes, and resource-specific role assignments.

Shared credentials, organizations, users, and teams remain. Only their grants to
excluded objects are removed. Constructed inventories follow their input inventory
references. Smart inventories are excluded conservatively when any inventory is
excluded, because their host filters can select across inventories. This can
exclude more automation than ADO alone; review the preview accordingly.

Custom variables, playbook content, and external launchers need your review;
the helper cannot infer their dependencies. Ambiguous template references or
invalid JSON stop the operation instead of guessing.

## 3. Create the filtered copy

Only after accepting the preview:

```bash
oc -n "$NAMESPACE" exec deployment/aap-bridge-cli -c cli -- \
  python3.12 /tmp/aap-bridge-project-fix/tools/exclude_transformed_project.py \
  --input /data/xformed --output /data/xformed-without-ado --project-name ADO --apply

oc -n "$NAMESPACE" exec deployment/aap-bridge-cli -c cli -- \
  cat /data/xformed-without-ado/project-exclusion-report.json
```

Stop if creation fails. The output must be new and separate from the input;
existing output is never overwritten. Files use mode `0600` and directories
`0700`. The copy includes plaintext credentials already present in transformed
data: keep it on the private artifact volume and out of Git.

The report contains IDs, names, reasons, and counts, without credential values or
SCM URLs. Normal failures clean up the new output. If the pod is killed during
copying, choose a fresh output path and review/remove the incomplete copy manually.
Always filter the original transformed directory, not an already-filtered copy.

## 4. Resume Phase 2 with the filtered input

```bash
oc -n "$NAMESPACE" exec -it deployment/aap-bridge-cli -c cli -- \
  env PYTHONPATH=/tmp/aap-bridge-project-fix/src \
  aap-bridge --config /etc/aap-bridge/config.yaml \
  import --input /data/xformed-without-ado --phase phase2
```

Do not use `--force-reimport` or reset tracking. Keep the explicit `--input` path
on subsequent retries; the interactive menu uses the original configured path.
There is no need to repeat Prep, Export, Transform, or Phase 1 for this exclusion.

This import skips the excluded definitions, including ADO's project-sync check.
**Other included projects and inventory sources can still sync**, and other import
failures still stop the run normally. Existing ADO objects and previously queued
updates on the target are unaffected. This is an import filter, not network
containment or an application-level lock.

For local verification, run the helper tests without contacting AAP:

```bash
python3 -m unittest discover -s tests/compat -p 'test_exclude_transformed_project.py' -v
```

The helper supports Python 3.6 and newer using the standard library. Keep
`update_exported_credential.py` beside it for strict JSON parsing. Cluster execution
and the actual customer import require your manual validation.
