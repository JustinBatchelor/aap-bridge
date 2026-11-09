# Deploy AAP Bridge on OpenShift

The supported OpenShift deployment workflow uses **BuildConfigs and ImageStreams**.
OpenShift builds the API and UI from one published Git commit, stores their images
in the integrated registry, and deploys explicitly selected immutable digests.
Follow the steps in order: **prepare the namespace → build images → configure the
overlay and Secrets → apply the application → verify readiness**. Successful
builds alone do not deploy Bridge. The local Compose workflow remains independent.

## Architecture and requirements

```text
HTTPS Route (re-encrypt) -> Service:443 -> OAuth proxy:8443
                                         -> nginx on 127.0.0.1:8080
                                            -> API on 127.0.0.1:8000
                                               -> PostgreSQL Service:5432
```

The proxy, UI and API share one pod. Only the proxy accepts pod-network traffic.
OAuth checks whether the user can `get` the `aap-bridge` Service in the deployment
namespace. All admitted users share connections, tokens, artifacts and migration
controls; this is not per-user isolation. Namespace administrators can also access
the workload directly using `oc exec` or port forwarding.

Requirements:

- OpenShift 4.x with integrated OAuth, ingress, and service-serving certificates.
- `oc`, Python 3 with PyYAML, registry credentials, and permission to create a project,
  BuildConfigs, ImageStreams, workloads, Secrets, Roles, NetworkPolicies and PVCs.
  Podman is optional for creating a dedicated vendor authentication file.
- An amd64 worker (the documented builds target linux/amd64).
- A dynamic storage class that permits writes by the SCC-assigned UID/fsGroup.
  Storage defaults to the cluster default class, with RWO claims: 10 GiB database
  and 20 GiB artifacts. NFS must support these permissions; do not solve storage
  permission errors by granting `anyuid` or adding a root init container.
- An available integrated image registry and outbound build access to GitHub,
  Red Hat registries, OS package repositories, PyPI, and npm. API outbound access
  to cluster OAuth/API endpoints and, when configured, the chosen AAP servers.

This is a single-instance installation, not HA. Keep one API process and one
replica. Updates use `Recreate`, causing downtime. In-flight migrations are not
resumed automatically; the API marks unfinished jobs failed after restarting.
Stop or finish jobs before upgrades. PostgreSQL also has one replica.

ImageStreams track image metadata, not image-layer storage. No DeploymentConfig,
Tekton installation, webhook, or automatic image-change rollout is required.
Both BuildConfigs have empty triggers and use the Docker build strategy (not a
Docker daemon on the worker). Build admission uses the cluster's standard build
service account policy; application pods retain their restricted security policy.
Do not grant application service accounts privileged SCC access to fix a build.

## 1. Prepare a build and application namespace

Run all commands from the repository root. Choose a namespace and a full commit
SHA already published in the BuildConfigs' Git repository. The example uses local
`main`; unpushed commits and local working-tree edits cannot be built this way.

```bash
NAMESPACE=aap-bridge
REVISION=$(git rev-parse main)
# Ensure REVISION is published in the Git repository configured below.
oc new-project "$NAMESPACE"
mkdir -p deploy/openshift/local/build-test
```

If the namespace already exists, inspect its ownership before applying anything.
`oc new-project` selects the new project in your CLI context. Use `oc create namespace`
instead if you need to preserve that context and have permission. All subsequent
commands use explicit `-n` arguments. Use a separate namespace for validation so
builds and fresh test data cannot affect the working
Bridge. For a new installation, choose its namespace here; the examples use
`aap-bridge-build-test` and an ignored `local/build-test` workspace throughout.
Do not copy application Secrets or database contents between instances.

The manifests default to the public JustinBatchelor/aap-bridge fork and a pinned
published commit. Customize `spec.source.git.uri` and `ref` with an overlay for
another fork. The helper's `--commit` explicitly selects the revision of each run.
Private repositories additionally require a Git source Secret; it is separate
from the registry pull Secret. Never put credentials in a Git URL or build args.

If the cluster cannot already pull Red Hat base images, create a **Red Hat-only**
Docker auth Secret named `vendor-pull` in this namespace. For example, authenticate
using a dedicated local auth file, then import it:

```bash
podman login --authfile deploy/openshift/local/build-test/vendor-auth.json registry.redhat.io
chmod 600 deploy/openshift/local/build-test/vendor-auth.json
oc create secret generic vendor-pull -n "$NAMESPACE" \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=deploy/openshift/local/build-test/vendor-auth.json
oc secrets link builder vendor-pull --for=pull -n "$NAMESPACE"
```

The registry auth file stays JSON because Podman and Kubernetes require Docker
authentication JSON; it is not a generated deployment manifest.

The namespace's normal builder account pushes to its own integrated registry
repository. Do not supply external push credentials, application tokens, or the
database encryption key to build pods. A separate registry Route is unnecessary.

```bash
oc kustomize deploy/openshift/builds > deploy/openshift/local/build-test/builds.yaml
oc apply -n "$NAMESPACE" --dry-run=server -f deploy/openshift/local/build-test/builds.yaml
oc apply -n "$NAMESPACE" -f deploy/openshift/local/build-test/builds.yaml
```

## 2. Build and record immutable outputs

```bash
python3 deploy/openshift/build-images.py \
  --namespace "$NAMESPACE" --commit "$REVISION" \
  --output deploy/openshift/local/build-test/images.yaml
```

The helper starts API then UI, verifies the source revision of each successful
Build, saves a YAML build record, and prints an `images:` Kustomize fragment. Both
Python generators require a `.yaml` or `.yml` output filename. The build record
is metadata, not a Kubernetes manifest: copy its image entries as shown below
rather than applying the record directly. It reads each Build's output
digest, not the current candidate tag, so a later build cannot silently change
the recorded result. It refuses to overwrite an existing record; use a new
filename for another run. On failure, investigate before rerunning (a rerun
builds both images). The helper never modifies a Deployment.

Each BuildConfig uses `Serial`, requests 250m CPU/512 MiB memory, limits 2 CPU/4 GiB,
and has a 30-minute completion deadline. Three successful and three failed Build
records are retained. Build history retention is not registry image pruning;
retain rollback digests and use existing cluster image-pruning procedures.

```bash
oc get builds -n "$NAMESPACE"
oc logs -f build/aap-bridge-api-1 -n "$NAMESPACE"
oc describe build/aap-bridge-api-1 -n "$NAMESPACE"
oc get imagestreams -n "$NAMESPACE"
```

Use the actual Build name printed by the helper. Git pinning identifies source,
but base images and Python dependencies can change between builds. Record and
deploy the tested output digest; do not assume a rebuild is identical. Git builds
do not include uncommitted local source changes.

## 3. Deploy and validate a fresh Bridge instance

This section creates the Bridge Deployment, PostgreSQL StatefulSet, PVCs, Services,
Route, service accounts, and access Role. The application overlay is separate from
the build package; applying only `deploy/openshift/builds` does not create these resources.

### 3.1. Create the application overlay

Continue from the repository root with the same `NAMESPACE` used for your builds.
Create a local overlay without overwriting an existing customization:

```bash
mkdir -p deploy/openshift/local/build-test/install
cp -n deploy/openshift/example/kustomization.yaml \
  deploy/openshift/local/build-test/install/kustomization.yaml
```

Print the API/UI entries from the successful build record. If you chose another
output filename in section 2, use that filename here:

```bash
python3 - <<'PY'
from pathlib import Path
import yaml

record = yaml.safe_load(Path('deploy/openshift/local/build-test/images.yaml').read_text())
print('images:')
for image in record['images']:
    print(f"  - name: {image['name']}")
    print(f"    newName: {image['newName']}")
    print(f"    digest: {image['digest']}")
PY
```

Resolve the OAuth proxy **component image** from this cluster's release:

```bash
RELEASE_IMAGE=$(oc get clusterversion version -o jsonpath='{.status.desired.image}')
OAUTH_PROXY_IMAGE=$(oc adm release info "$RELEASE_IMAGE" --image-for=oauth-proxy) &&
  printf 'newName: %s\ndigest: %s\n' \
    "${OAUTH_PROXY_IMAGE%@*}" "${OAUTH_PROXY_IMAGE##*@}"
```

Edit the copied `deploy/openshift/local/build-test/install/kustomization.yaml`.
The [example overlay](example/kustomization.yaml) already contains every required
image entry and patch; replace its placeholders as follows:

| Setting in the copied overlay | Value |
| --- | --- |
| `namespace` | The literal value of `$NAMESPACE` used for the builds |
| `resources` | Keep `../../../base` for this directory layout |
| API and UI `newName` / `digest` | Copy the values printed from `images.yaml` |
| OAuth proxy `newName` / `digest` | Copy the values printed by `oc adm release info` |
| PVC patch `value` | Your existing storage class; applies to both PVCs |
| Route patch `value` | An unused hostname under your cluster ingress domain |
| ServiceAccount patches | Keep `vendor-pull` on both accounts if you created that Secret; otherwise remove both patches |

Each digest includes its `sha256:` prefix. Use `digest` without `newTag` for the
three image replacements. The OAuth image must be the release's **oauth-proxy
component**, not the release payload itself. PostgreSQL remains vendor-sourced
through the base manifests. Kubernetes does not expand `$NAMESPACE` in this YAML;
enter the actual namespace name.

Choose an existing storage class with `oc get storageclass`. This patch applies
to both fresh PVCs (10 GiB PostgreSQL and 20 GiB artifacts). Choose an unused Route
hostname under your cluster's ingress domain, which you can inspect with:

```bash
oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}{"\n"}'
```

For example, use `aap-bridge-build-test.<ingress-domain>`. If the cluster already
provides vendor pull credentials and you did not create `vendor-pull`, omit the
two ServiceAccount patches. Same-namespace service accounts normally have
integrated-registry pull access; external application-registry credentials are unnecessary.

### 3.2. Create application Secrets once

Check whether you already completed this step:

```bash
oc get secret aap-bridge aap-bridge-db aap-bridge-oauth -n "$NAMESPACE"
```

If all three exist for this installation, keep them and proceed to section 3.3.
For a fresh installation with none of these Secrets, generate and create them:

```bash
python3 deploy/openshift/generate-secrets.py --namespace "$NAMESPACE" \
  --output deploy/openshift/local/build-test/secrets.yaml
oc create -n "$NAMESPACE" -f deploy/openshift/local/build-test/secrets.yaml
```

The generator requires Python 3 with PyYAML and refuses to overwrite an existing
file. Use a new output filename if one remains from a previous installation, and
use the same filename in `oc create`. If only some Secrets exist, resolve the
partial installation using its original generated file rather than regenerating
credentials for an existing database. Never replace an existing encryption key
just to redeploy images.

### 3.3. Render, validate, and deploy

```bash
oc kustomize deploy/openshift/local/build-test/install \
  > deploy/openshift/local/build-test/rendered.yaml
```

Inspect `rendered.yaml` before applying. Confirm the namespace, Route host, storage
class, and image references, with no placeholder values remaining. The API digest must
appear in `initialize-data`, `wait-for-database`, and `api`; the UI digest in `ui`.
There must be no `image.openshift.io/triggers` annotation or automatic deployment
trigger. A new candidate build alone must not change running pods.

```bash
oc apply -n "$NAMESPACE" --dry-run=server \
  -f deploy/openshift/local/build-test/rendered.yaml

# Continue only after validation succeeds.
oc apply -n "$NAMESPACE" \
  -f deploy/openshift/local/build-test/rendered.yaml

oc rollout status -n "$NAMESPACE" statefulset/aap-bridge-db --timeout=300s
oc rollout status -n "$NAMESPACE" deployment/aap-bridge --timeout=300s
oc get pods,pvc,route -n "$NAMESPACE"
oc get route aap-bridge -n "$NAMESPACE" \
  -o jsonpath='https://{.spec.host}{"\n"}'
```

Expect PostgreSQL to be 1/1 ready, Bridge to be 3/3 ready, and both PVCs to be Bound.
The service certificate controller creates `aap-bridge-tls`; do not generate that
Secret manually. If rollout fails, inspect events and the relevant container:

```bash
oc get events -n "$NAMESPACE" --sort-by=.metadata.creationTimestamp
oc logs -n "$NAMESPACE" deployment/aap-bridge -c wait-for-database
oc logs -n "$NAMESPACE" deployment/aap-bridge -c api
oc logs -n "$NAMESPACE" statefulset/aap-bridge-db -c postgresql
```

### 3.4. Verify readiness and browser access

Check the version list through nginx without contacting any AAP endpoint:

```bash
oc exec -n "$NAMESPACE" deployment/aap-bridge -c ui -- \
  curl --fail --silent http://127.0.0.1:8080/api/versions

ROUTE_HOST=$(oc get route aap-bridge -n "$NAMESPACE" -o jsonpath='{.spec.host}')
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  "https://$ROUTE_HOST/"
```

Expect a populated version list and HTTP 302 to cluster OAuth; the curl command
does not follow the redirect. Open the printed HTTPS URL for manual login. Users
need permission to get the `aap-bridge` Service in this namespace. Namespace
administrators already have that permission. To grant another existing user
application access, replace the username below:

```bash
oc create rolebinding aap-bridge-user -n "$NAMESPACE" \
  --role=aap-bridge-access --user=REPLACE_WITH_OPENSHIFT_USERNAME
```

At this point the instance is ready for manual login and connection configuration.
The checks above validate infrastructure without contacting source/target AAP
endpoints or running a migration. Browser login, connection tests, authenticated
WebSockets, and backup/restore should be verified separately before relying on the
instance. Saved connections are configured in the UI, not by build parameters.

## 4. Updates, manual promotion, and rollback

To build an update, select a new published commit and rerun section 2 with a new
output filename. Use the resulting digests in the application overlay and rerender
it before applying; no workstation image push is needed.

Complete active migrations before changing the working application; its `Recreate`
strategy causes downtime and restarts interrupt jobs. First record the current
application image references and retain the existing Secrets, database, and PVCs.

Only if deploying into a **different namespace** from the builds, grant the
consuming service account image-pull rights below. Skip this for the same-namespace
installation used in sections 1–3. Substitute both namespace names:

```bash
oc policy add-role-to-user system:image-puller \
  system:serviceaccount:APPLICATION_NAMESPACE:aap-bridge -n BUILD_NAMESPACE
```

Update both application digest entries in the destination overlay, render, review,
and apply that overlay. This updates both API init containers and the API/UI containers
together. Do not patch only the main API container. Verify rollout, readiness, and
the version endpoint. Building/promoting an ImageStreamTag is not a deployment.

To roll back an image-only release, restore the recorded previous API/UI references
in the overlay and reapply. This does not restore changed database data; changes
with schema/data migrations require their own backup and compatibility plan.

## 5. Application operation and troubleshooting

Connections are managed in the UI. `SOURCE__*` and `TARGET__*` environment variables
do not populate saved Web UI connections. All authorized users share the workspace.
For group access, customize `example/access.example.yaml` locally and apply it with
an explicit namespace. Do not grant application access to `system:authenticated`.
The browser OAuth flow needs no cluster-wide `auth-delegator` binding.

Build troubleshooting:

- Git clone failures: verify the repository, published commit, DNS/egress, and
  source Secret for private repositories.
- Base-image pull failures: verify the builder account's vendor pull credentials;
  the UI build pulls base images for both stages.
- Internal registry push failures: inspect registry health and builder permissions.
- Pending/failed builds: inspect Build/pod events for quota, admission, scheduling,
  OOM, or deadline failures. Adjust build resources without relaxing application SCCs.
- New images not running: builds do not trigger rollouts; update the overlay's
  digests and apply it as described in sections 3 and 4.


Use the events and container-log commands in section 3.3 to diagnose rollout
failures. For build failures, use the Build names and commands in section 2.

Check pod annotation `openshift.io/scc` and runtime `id`: workloads must use the
cluster's restricted SCC and assigned IDs, not an `anyuid` exception. The image
USER is only a non-root fallback. PVC permissions rely on SCC-assigned fsGroup;
no cluster-specific IDs are hardcoded.

- `ImagePullBackOff`: verify the recorded build digest, integrated-registry pull
  permissions, vendor credentials, registry reachability, and architecture.
- Pending PVC: verify default/selected StorageClass and provisioner events.
- Permission denied: inspect mount ownership and CSI fsGroup behavior. Fix storage
  provisioning rather than weakening the SCC.
- Waiting for database: inspect PostgreSQL logs and verify both Secrets describe
  the same database/user/password. Never paste Secret values into an issue.
- OAuth errors: verify Route admission, serving certificate, callback annotation,
  existing identity provider/login availability, and the user's Service-read RBAC.
- Route 503: check all three containers' readiness and ingress NetworkPolicy.
  The policy uses OpenShift's ingress namespace label; adapt it if your ingress
  topology uses different selectors. API/UI ports intentionally reject pod-IP
  connections; their probes execute locally inside their containers.

Ingress is restricted to the router on port 8443 and application-to-database
traffic on port 5432. Egress remains available for operator-selected AAP endpoints,
cluster authentication, and DNS. Environments with additional egress restrictions
must explicitly permit those destinations.

For private AAP CAs, create a ConfigMap containing a PEM trust bundle combining
the standard CA bundle with your private CA. Mount it read-only into the API and
set both `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` to that file through an overlay.
Keep connection TLS verification enabled. This is independent of Route TLS and
the OAuth proxy's cluster CA trust.

## 6. Persistence and backups

Use section 4 for image updates. ConfigMap content changes get a new generated
name and trigger a rollout. For Secret-only changes, deliberately restart the
application after updating Secrets:

```bash
oc rollout restart -n "$NAMESPACE" deployment/aap-bridge
```

Database password changes require coordinated database, Secret, and URL updates;
changing a Kubernetes Secret alone is not a generic database password rotation.
Do not regenerate all Secrets during upgrades. Rollback may require restoring the
database if an application upgrade changes its schema.

The artifact PVC holds exports, transformed data, schemas, reports, logs, and
backups. The database holds saved connections and job records. Memory-only live
log buffers are not durable. Back up all three of: database, artifacts, encryption
key. Backups can contain credentials and sensitive AAP configuration.

For a consistent backup, finish jobs, scale the application to zero, and use a
restricted local backup directory. Database dump example:

```bash
umask 077
mkdir -p deploy/openshift/local/backup
oc scale -n "$NAMESPACE" deployment/aap-bridge --replicas=0
oc wait -n "$NAMESPACE" --for=delete pod -l app=aap-bridge --timeout=180s
oc exec -n "$NAMESPACE" aap-bridge-db-0 -- sh -ec \
  'PGPASSWORD="$POSTGRESQL_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRESQL_USER" -d "$POSTGRESQL_DATABASE" -Fc' \
  > deploy/openshift/local/backup/database.dump
oc get secret -n "$NAMESPACE" aap-bridge aap-bridge-db aap-bridge-oauth -o yaml \
  > deploy/openshift/local/backup/secrets.yaml
```

Mount `aap-bridge-data` in a temporary non-root maintenance pod using the tested
API image, the same restricted SCC, and command `sleep infinity`. With the
application stopped, stream `tar -C /data -czf - .` through `oc exec` into a local
archive. Remove the maintenance pod, then restore the application replica to one.
Use your storage platform's snapshot procedure instead if it supplies suitable
consistent snapshots. Do not delete the PVC.

Restore into an isolated namespace first: retain the encryption key, restore the
database into an empty database with `pg_restore --no-owner --no-acl`, unpack the
artifact archive through the maintenance pod, and then start the application.
Ensure restored database credentials and the connection URL match the new
namespace's database. Confirm permissions and token decryption before production
use. Backup/restore execution is not part of the infrastructure-only smoke test.

## 7. Teardown

To stop the installation while preserving its data:

```bash
oc scale -n "$NAMESPACE" deployment/aap-bridge --replicas=0
oc scale -n "$NAMESPACE" statefulset/aap-bridge-db --replicas=0
```

To remove running workload definitions, delete the Deployment, StatefulSet,
Services and Route explicitly. Keep PVCs and Secrets for reuse. BuildConfigs and
ImageStreams can remain for future builds; remove those explicitly only when
retiring the build workflow. Build history retention does not prune registry storage.
Do not use `oc delete -k`, delete the namespace, or delete PVCs as routine teardown: those
operations remove data claims. Final data destruction requires a separate,
intentional action and consideration of the storage class's reclaim policy.

References: [Docker build strategy](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/builds_using_buildconfig/build-strategies),
[ImageStream deployment triggers](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/images/triggering-updates-on-imagestream-changes),
[OpenShift OAuth proxy](https://github.com/openshift/oauth-proxy),
[OpenShift image guidelines](https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/images/creating-images).
