# Run a CLI command as a Job

Start with the [deployment README](readme.md). Run these commands from the repository
root, using the same `NAMESPACE`, `PYTHON`, and `RECORD` values as that installation.

## Prepare the Job once

```bash
mkdir -p deploy/openshift/local/cli/{job,evidence}
cp deploy/openshift/cli/example/job-kustomization.yaml deploy/openshift/local/cli/job/kustomization.yaml
${EDITOR:-vi} deploy/openshift/local/cli/job/kustomization.yaml
```

Set the namespace and image `newName`/`digest` to exactly those in the installation
Kustomization. Include the same [private CA patch](advanced.md#private-aap-certificates)
if used by the toolbox. The Job uses the existing configuration, Secrets, PVC and
initialization; do not apply the browser package or create another database.

```bash
oc kustomize deploy/openshift/local/cli/job > deploy/openshift/local/cli/job.yaml
oc apply --dry-run=client -f deploy/openshift/local/cli/job.yaml -o name
```

The output must contain only `Job/aap-bridge-cli-run`. Its default command validates
configuration. Review the image and command before proceeding.

## Stop the toolbox, then start the Job

Finish interactive work, then stop the toolbox and wait for its pod to disappear.
Scaling down during an active migration interrupts it. Verify no prior Job is active.
These are operator controls, not application-level locking.

```bash
oc -n "$NAMESPACE" get job aap-bridge-cli-run --ignore-not-found
oc -n "$NAMESPACE" scale deployment/aap-bridge-cli --replicas=0
oc -n "$NAMESPACE" wait --for=delete pod -l app=aap-bridge-cli,component=toolbox --timeout=5m
oc -n "$NAMESPACE" get pods -l app=aap-bridge-cli
oc create -f deploy/openshift/local/cli/job.yaml
oc -n "$NAMESPACE" logs -f job/aap-bridge-cli-run -c cli
oc -n "$NAMESPACE" get job aap-bridge-cli-run
```

The example defaults to `config validate`, has the same image/config/Secrets/PVC
and initialization, and uses `parallelism: 1`, `completions: 1`, `restartPolicy:
Never`, and `backoffLimit: 0`. For migration commands, explicitly change the CLI
container's `args` in a local Job patch and render/review again before creation.
Use the fixed Job name; completed Jobs remain for inspection and block a new
`create`. There is no TTL or automatic retry. A failed migration must be reviewed
before manual recovery; do not blindly retry imports.

Only after the Job is Complete or Failed **and all its pods have terminated** may
you resume toolbox mode. Review init failures too; a pending Job is not terminated.
Archive logs and status before deliberately deleting a reviewed completed Job:

```bash
oc -n "$NAMESPACE" logs job/aap-bridge-cli-run -c cli > deploy/openshift/local/cli/evidence/job.log
oc -n "$NAMESPACE" get job aap-bridge-cli-run -o yaml > deploy/openshift/local/cli/evidence/job.yaml
oc -n "$NAMESPACE" get pods -l job-name=aap-bridge-cli-run
# After confirming the Job and its pods have terminated:
oc -n "$NAMESPACE" delete job aap-bridge-cli-run --wait=true
oc -n "$NAMESPACE" scale deployment/aap-bridge-cli --replicas=1
oc -n "$NAMESPACE" rollout status deployment/aap-bridge-cli
```

Do not reapply the installation's `replicas: 1` while a Job is running. Namespace
admins can bypass these operational controls; this workflow does not add a lock.
