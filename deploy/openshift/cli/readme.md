# CLI-only OpenShift deployment

This workflow builds and runs the Python CLI with bundled PostgreSQL. It does not
build or deploy nginx, Apache, Node/npm, the application frontend, OAuth proxy,
or an API server. The toolbox has no listener, Service, Route, or browser-access
Role. Use `oc exec` with your existing OpenShift identity and permissions.

Use a **fresh namespace**. This procedure does not convert or remove a Web UI
installation. The [browser workflow](../readme.md) remains separate. Connections,
artifacts, and migration state are shared by operators of this single-instance
CLI installation. Treat permission to execute in the pod as access to its AAP
credentials and data; grant it only to the intended operators.

## 1. Confirm prerequisites and the published revision

Run from the repository root. Use your fork, and publish the implementation commit
before starting a cluster build. The older browser BuildConfig revision
`de39ede2b2c493513a469aa7a4eccd9f5ed4e994` does not contain `Containerfile.cli`.

```bash
git remote get-url origin
git status --short --branch
export NAMESPACE=aap-bridge-cli-customer-test
export REVISION=$(git rev-parse HEAD)
git show "$REVISION:deploy/openshift/Containerfile.cli" > /dev/null
gh api "repos/JustinBatchelor/aap-bridge/commits/$REVISION" --jq .sha
oc whoami
oc config current-context
oc get nodes -L kubernetes.io/arch
oc get storageclass
oc auth can-i create projects
oc auth can-i create buildconfigs.build.openshift.io -n "$NAMESPACE"
```

Verify the remote is `JustinBatchelor/aap-bridge`, the published SHA is the intended
commit, and the current cluster is the intended non-production environment.
Confirm integrated registry availability, registry pull credentials, build
permissions, namespace quota, storage provisioning and expansion support, and DNS,
HTTPS, firewall and certificate trust to both AAP endpoints. The included build
selects **amd64**; edit the build overlay below for an arm64 cluster. Confirm the
PostgreSQL image supports that architecture too. The CLI and database PVC defaults
are 20 GiB and 10 GiB respectively, using the cluster default StorageClass.

```bash
oc new-project "$NAMESPACE"
umask 077
mkdir -p deploy/openshift/local/cli/{builds,install,job,evidence}
python3 -m venv .venv-cli-tools
.venv-cli-tools/bin/pip install PyYAML
export PYTHON="$PWD/.venv-cli-tools/bin/python"
```

Read the selected StorageClass reclaim policy before creating PVCs. Namespace
permissions and image pull access must be confirmed with the cluster administrator.
No cluster resources are created automatically by the repository's Python helpers.

## 2. Apply only CLI build resources

Authenticate to Red Hat's registry locally; do not put passwords in shell arguments
or Git. Create a private auth file suitable for `oc` using interactive login:

```bash
podman login --authfile deploy/openshift/local/cli/registry-auth.json registry.redhat.io
oc -n "$NAMESPACE" create secret generic vendor-pull \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=deploy/openshift/local/cli/registry-auth.json
oc -n "$NAMESPACE" secrets link builder vendor-pull --for=pull
cat > deploy/openshift/local/cli/builds/kustomization.yaml <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NAMESPACE
resources:
- ../../../cli/builds
EOF
oc kustomize deploy/openshift/local/cli/builds > deploy/openshift/local/cli/builds.yaml
oc apply --dry-run=client -f deploy/openshift/local/cli/builds.yaml -o name
oc apply -f deploy/openshift/local/cli/builds.yaml
```

The inventory must be exactly `BuildConfig/aap-bridge-cli` and
`ImageStream/aap-bridge-cli`. Do **not** apply `deploy/openshift/builds`, which builds
API/UI images. CLI triggers are empty and builds run serially. For arm64, add this
patch to the local build Kustomization before rendering:

```yaml
patches:
- target:
    kind: BuildConfig
    name: aap-bridge-cli
  patch: |-
    - op: replace
      path: /spec/nodeSelector/kubernetes.io~1arch
      value: arm64
```

For a private Git fork, configure an appropriate BuildConfig `sourceSecret` through
the local overlay; do not embed Git credentials in its URL.

## 3. Build and record the immutable CLI image

```bash
export RECORD="deploy/openshift/local/cli/images-$REVISION.yaml"
"$PYTHON" deploy/openshift/build-images.py --mode cli \
  --namespace "$NAMESPACE" --commit "$REVISION" --output "$RECORD"
cat "$RECORD"
oc -n "$NAMESPACE" get builds,imagestreams
```

Only `aap-bridge-cli` is started. The helper checks the completed build's exact source
SHA and digest before writing the existing YAML record format; it never deploys
workloads and refuses to overwrite a prior record. Inspect the build logs and image
metadata, and retain the record with your change approval. Use its `newName` and
`digest` in the installation and Job overlays. A mutable `candidate` tag is not a
promotion record. Rebuilds require a new output filename even for the same SHA.

## 4. Create private database and connection Secrets

For a **fresh installation only**:

```bash
"$PYTHON" deploy/openshift/generate-secrets.py --mode cli \
  --namespace "$NAMESPACE" --output deploy/openshift/local/cli/secrets.yaml
cp deploy/openshift/cli/connections.env.example deploy/openshift/local/cli/connections.env
chmod 600 deploy/openshift/local/cli/connections.env
${EDITOR:-vi} deploy/openshift/local/cli/connections.env
oc -n "$NAMESPACE" create -f deploy/openshift/local/cli/secrets.yaml
oc -n "$NAMESPACE" create secret generic aap-bridge-connections \
  --from-env-file=deploy/openshift/local/cli/connections.env
```

Fill in both URLs, AAP versions, and tokens. Env-file values are literal and unquoted.
Do not echo tokens or database URLs, commit these files, or paste them into logs.
Generated Secrets have mode 0600 and exclusive creation. CLI mode creates matching
PostgreSQL credentials and a state URL, with no OAuth cookies or Web UI encryption
key. Existing Secrets must be retained for upgrades; do not regenerate them while
reusing a database PVC. `create` intentionally refuses to overwrite existing Secrets.

TLS verification defaults to enabled. For a private AAP CA, create a ConfigMap from
a PEM bundle containing the required public and private trust roots, mount it
read-only at `/etc/aap-ca` in both toolbox and Job pods, and set `SSL_CERT_FILE` and
`REQUESTS_CA_BUNDLE` to `/etc/aap-ca/ca-bundle.pem` in the CLI container. Apply those
same patches to both local overlays. HTTPX uses `SSL_CERT_FILE`; Requests uses
`REQUESTS_CA_BUNDLE`. Do not disable verification to bypass missing trust.

For example, prepare the bundle locally, then create the ConfigMap and copy its
patch into both overlay directories:

```bash
oc -n "$NAMESPACE" create configmap aap-bridge-cli-ca --from-file=ca-bundle.pem=/secure/path/ca-bundle.pem
cp deploy/openshift/cli/example/ca-patch.yaml deploy/openshift/local/cli/install/ca-patch.yaml
cp deploy/openshift/cli/example/ca-patch.yaml deploy/openshift/local/cli/job/ca-patch.yaml
```

After copying the Kustomizations in the next step, append this entry to each one's
`patches` list (create the list in the Job overlay):

```yaml
- target:
    kind: Deployment|Job
    name: aap-bridge-cli.*
  path: ca-patch.yaml
```

The supplied patch adds the two trust environment variables. Merge it with any
other custom `env` entries instead of replacing them.

## 5. Render and inspect the installation

```bash
cp deploy/openshift/cli/example/kustomization.yaml deploy/openshift/local/cli/install/kustomization.yaml
cp deploy/openshift/cli/example/job-kustomization.yaml deploy/openshift/local/cli/job/kustomization.yaml
${EDITOR:-vi} deploy/openshift/local/cli/install/kustomization.yaml
${EDITOR:-vi} deploy/openshift/local/cli/job/kustomization.yaml
```

Set both namespaces to `$NAMESPACE` and both image entries to the exact `newName`
and `digest` from `$RECORD`. Set your chosen StorageClass in the installation
patch, or remove that patch to use the cluster default. To change PVC sizes, add
individual patches targeting `aap-bridge-db` and `aap-bridge-data` at
`/spec/resources/requests/storage`. Storage must support non-root SCC-assigned
UID/group access without root initialization. Keep the ServiceAccount pull-secret
patch if using the `vendor-pull` Secret created above.

The complete `cli/config.yaml` includes source/target settings, persistent `/data`
paths, and PostgreSQL state. To customize it, copy it into the local install directory
and merge this generator into the installation Kustomization:

```yaml
configMapGenerator:
- name: aap-bridge-cli-config
  behavior: replace
  files:
  - config.yaml
```

The ConfigMap name is stable so the separately rendered Job can use it. Complete
active commands, stop all CLI execution, and deliberately restart the toolbox after
configuration or Secret changes; existing process environments do not refresh.

```bash
oc kustomize deploy/openshift/local/cli/install > deploy/openshift/local/cli/install.yaml
oc kustomize deploy/openshift/local/cli/job > deploy/openshift/local/cli/job.yaml
"$PYTHON" - <<'PY'
from pathlib import Path
import yaml
for name in ('install', 'job'):
    docs = list(yaml.safe_load_all(Path(f'deploy/openshift/local/cli/{name}.yaml').read_text()))
    print(name, [(d['kind'], d['metadata']['name']) for d in docs])
    for d in docs:
        if d['kind'] in ('Deployment', 'StatefulSet', 'Job'):
            pod = d['spec']['template']['spec']
            for c in pod.get('initContainers', []) + pod['containers']:
                print(c['name'], c['image'])
                if c['image'].find('postgresql-15') < 0:
                    assert '@sha256:' in c['image'], 'CLI image must be immutable'
PY
oc apply --dry-run=client -f deploy/openshift/local/cli/install.yaml -o name
```

Expected installation: one CLI Deployment, one PostgreSQL StatefulSet, two PVCs,
two ServiceAccounts, two ingress NetworkPolicies, one CLI ConfigMap, and only the
PostgreSQL Service. There must be no UI/API/OAuth containers, browser Role, Route,
application Service, or BuildConfig/ImageStream in the installation. The separate
Job render contains only `Job/aap-bridge-cli-run`; **do not apply it yet**.

## 6. Install and verify manually

```bash
oc apply -f deploy/openshift/local/cli/install.yaml
oc -n "$NAMESPACE" get pvc,pods
oc -n "$NAMESPACE" rollout status statefulset/aap-bridge-db --timeout=10m
oc -n "$NAMESPACE" rollout status deployment/aap-bridge-cli --timeout=10m
oc -n "$NAMESPACE" get pods -l app=aap-bridge-cli \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.annotations.openshift\.io/scc}{"\n"}{end}'
oc exec -n "$NAMESPACE" deployment/aap-bridge-cli -c cli -- id
oc exec -n "$NAMESPACE" deployment/aap-bridge-cli -c cli -- \
  aap-bridge --config /etc/aap-bridge/config.yaml config validate
```

The non-root init containers create and check artifact directories, then authenticate
to PostgreSQL and run `SELECT 1` before the toolbox starts. Its main process is
`sleep infinity`; an `oc exec` session runs the CLI. All CLI containers drop
capabilities, use RuntimeDefault seccomp, and have read-only root filesystems plus
writable `/tmp` and `/data`. No pod UID is fixed. ServiceAccount tokens are not
mounted. No elevated SCC grants, root init container, or host networking are needed.

The CLI denies incoming pod-network traffic, and database ingress allows only CLI
pods in the same namespace. Outbound traffic is retained for DNS and AAP access;
this is not an egress allowlist. `oc exec` uses the Kubernetes control plane and
does not require an inbound application listener.

## 7. Interactive toolbox mode

```bash
oc exec -it -n "$NAMESPACE" deployment/aap-bridge-cli \
  -c cli -- aap-bridge --config /etc/aap-bridge/config.yaml
```

Use one interactive operator at a time. Follow the menu through export, transform,
secret completion, and import; consult the [migration workflow](../../../docs/user-guide/migration-workflow.md).
For automation inspect `aap-bridge --help` and each subcommand's help first. A lost
exec session is not proof a migration stopped: check running processes and state
before starting another operation. Configuration validation alone is not proof of
AAP connectivity. `config validate --check-connectivity` checks the target ping;
its source check currently only constructs the client. Perform an explicit source
read/export during the approved acceptance exercise.

## 8. Mutually exclusive Job mode

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

## Backups, upgrades, rollback, and cleanup

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

For upgrades, finish jobs, take backups, build a newly published commit with CLI
mode, and update both installation and Job overlays to the new digest. Apply only
the installation while no Job runs. Preserve PVCs and existing Secrets. The toolbox
uses Recreate updates. For rollback, restore the previous digest to both overlays;
if database/schema compatibility changed, restore its matching state/artifact backup
as well. Image rollback alone does not undo migration effects on target AAP.

Workload removal preserves data:

```bash
# Finish active commands first. Review any Job before removing it.
oc -n "$NAMESPACE" delete deployment aap-bridge-cli
oc -n "$NAMESPACE" delete job aap-bridge-cli-run --ignore-not-found
oc -n "$NAMESPACE" delete statefulset aap-bridge-db
```

PVC/Secret deletion is a **separate destructive operation**, only after backup and
explicit approval from the data owner:

```bash
oc -n "$NAMESPACE" delete pvc aap-bridge-data aap-bridge-db
oc -n "$NAMESPACE" delete secret aap-bridge aap-bridge-db aap-bridge-connections
```

Deleting the namespace deletes its remaining resources and PVCs too. Do not use
`oc delete -k` as a workload-only cleanup because the package includes PVCs.

## Existing images, disconnected use, and troubleshooting

Suppressing new UI builds does not erase existing registry images or node caches.
Inspect existing resources separately; these read-only commands do not remove them:

```bash
oc -n "$NAMESPACE" get builds,buildconfigs,imagestreams,deployments,statefulsets,jobs,services,routes
oc -n "$NAMESPACE" get pods -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.containers[*].image}{"\n"}{end}'
```

Registry garbage collection and node-cache removal are separate administrator tasks.
Do not delete shared images, existing browser workloads, or customer resources as
part of this CLI installation. ImageStream removal alone does not prove image bytes
have been removed from storage or nodes.

For disconnected clusters, mirror the UBI build base and PostgreSQL image, provide
an accessible Git source and Python package mirror/wheel source, and configure build
proxy/trust as required. Alternatively build the CLI in a connected environment,
mirror its approved immutable digest and PostgreSQL into the approved registry, and
skip the cluster build steps. Update installation and Job image replacements and
pull access together; preserve the source SHA and original/mirrored image records.
Do not mirror UI/API/OAuth images for this workflow.

For Pending pods inspect events, quotas, PVC binding and selected architecture.
For image pull failures check registry credentials and mirror references. For init
failures inspect `oc logs POD -c initialize-data` or `-c wait-for-database`; check PVC
SCC-group permissions, matching database Secrets, Service selectors and policies.
Do not solve NFS permission failures by granting privileged SCCs. For exec failures
check the operator's `pods/exec` permissions and rollout status. For TLS errors verify
CA mounts, bundle contents, hostname, and both AAP endpoints.

## Optional lab customization

Verify these values at deployment time: namespace `aap-bridge-lab`, StorageClass
`truenas-nfs`, operator `jbatchel`, and the lab's existing integrated image registry.
Use a new namespace such as `aap-bridge-cli-lab` if the lab namespace already contains
the Web UI. Select the CLI digest from the actual build output; do not hardcode a
registry hostname. No Route hostname or OAuth configuration is needed. Customer
instructions should continue to use a fresh customer-approved namespace.

## Local image checks and manual cluster acceptance

```bash
podman build -f deploy/openshift/Containerfile.cli -t localhost/aap-bridge-cli:test .
"$PYTHON" deploy/openshift/cli/check-image.py --image localhost/aap-bridge-cli:test \
  --output-dir deploy/openshift/local/cli/image-review
```

The checker uses an arbitrary non-root UID, read-only root filesystem, no network,
and synthetic credentials. It checks CLI help/version, config validation and writable
paths; rejects nginx/httpd, Node/npm, frontend assets and optional API packages;
and writes RPM/Python inventory for customer review. Inventory is not a vulnerability
scan. Scan the actual target-architecture digest with your approved scanner.
For unauthenticated local UBI access, add
`--build-arg UBI_IMAGE=registry.access.redhat.com/ubi9/ubi-minimal:latest` to the build.

Cluster acceptance remains manual:

- Confirm only the CLI and PostgreSQL images run; inspect init containers too.
- Confirm restricted SCC admission, assigned non-root UID, no token mount, and no elevated grants.
- Confirm both PVCs bind, artifact paths are writable, and database initialization passes.
- Confirm authorized `oc exec`, TLS trust and approved source/target AAP operations.
- Confirm no application Service/Route/listener exists and ingress policies select the expected pods.
- Confirm toolbox-to-Job handoff, fixed-name collision, failed Job review and no automatic retries.
- Write a test artifact and state record; restart workloads and verify persistence before migration.
- Restore a backup in an isolated namespace and verify state/artifacts together.

See [validation evidence](validation.md) for checks actually performed during implementation.
