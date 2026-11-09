# Deploy AAP Bridge on OpenShift

This package runs the Web UI and API behind OpenShift OAuth, with bundled
PostgreSQL 15 and persistent migration artifacts. It is a fresh installation:
local `.env` files and connections are not imported. The existing local
Containerfiles and Compose workflow remain independent.

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
- `oc`, Podman, Python 3 with PyYAML, registry credentials, and permission to create a project,
  namespaced workloads, Secrets, Roles, NetworkPolicies and PVCs.
- An amd64 worker (the documented builds target linux/amd64).
- A dynamic storage class that permits writes by the SCC-assigned UID/fsGroup.
  Storage defaults to the cluster default class, with RWO claims: 10 GiB database
  and 20 GiB artifacts. NFS must support these permissions; do not solve storage
  permission errors by granting `anyuid` or adding a root init container.
- Image pulls from Quay and Red Hat registries. API outbound access to the cluster
  OAuth/API endpoints and, when later configured, the chosen AAP servers.

This is a single-instance installation, not HA. Keep one API process and one
replica. Updates use `Recreate`, causing downtime. In-flight migrations are not
resumed automatically; the API marks unfinished jobs failed after restarting.
Stop or finish jobs before upgrades. PostgreSQL also has one replica.

## 1. Build and publish application images

Alternatively, [build images inside OpenShift using BuildConfigs and ImageStreams](builds/readme.md).
That optional workflow uses a pinned Git commit, the integrated registry, and
manual digest promotion. The application architecture and Quay workflow below
remain unchanged.

Run from the repository root. The separate Containerfiles use explicit COPY
inputs. `.dockerignore` also excludes `.env` variants, local overlays, credentials,
backups, and validation records. Do not add credentials under `src`, `config`, or
`web`, which are copied into builds.

Authenticate to `registry.redhat.io` for postgresql container image pull later on

```bash
podman login registry.redhat.io
```

(optional) If you need to build the images 
```bash
REVISION=$(git rev-parse --short=12 HEAD)
API_IMAGE=quay.io/jbatchel/aap-bridge-api
UI_IMAGE=quay.io/jbatchel/aap-bridge-ui
podman build --platform linux/amd64 -f deploy/openshift/Containerfile.api \
  -t "$API_IMAGE:$REVISION" .
podman build --platform linux/amd64 -f deploy/openshift/Containerfile.ui \
  -t "$UI_IMAGE:$REVISION" .
podman push --digestfile /tmp/aap-api.digest "$API_IMAGE:$REVISION"
podman push --digestfile /tmp/aap-ui.digest "$UI_IMAGE:$REVISION"
cat /tmp/aap-api.digest /tmp/aap-ui.digest
```

Record the digests; do not reuse revision tags for different image contents.
Dependencies are resolved at build time, so the digest identifies the tested
artifact even if rebuilding the same source later resolves newer dependencies.
Check Quay repository visibility and test an anonymous pull using an empty
Podman auth file. PostgreSQL and OAuth proxy images stay in their vendor
registries; this procedure does not republish them.

## 2. Prepare namespace, overlay and Secrets

Choose a project and confirm the target cluster before applying anything:

```bash
NAMESPACE=aap-bridge
oc new-project "$NAMESPACE"
mkdir -p deploy/openshift/local/install
cp deploy/openshift/example/kustomization.yaml deploy/openshift/local/install/
```

`namespace.yaml` is also provided for the default `aap-bridge` name. Namespace
creation is intentionally separate from workload application and deletion.

Edit the copied kustomization:

- Set `namespace` to your project.
- Change `resources: ../base` to `../../base` for this local directory.
- Select each application image with either `newTag` or `digest`, as shown below.
  The example uses the published source-revision tag `810925561e26` for both
  application images. Update it when publishing a newer revision.
- Set the `oauth-proxy` replacement to the OAuth proxy **component image** from
  the cluster's release. The release payload itself is not an OAuth proxy image:

```bash
RELEASE_IMAGE=$(oc get clusterversion version -o jsonpath='{.status.desired.image}')
OAUTH_PROXY_IMAGE=$(oc adm release info "$RELEASE_IMAGE" --image-for=oauth-proxy) &&
  printf 'newName: %s\ndigest: %s\n' \
    "${OAUTH_PROXY_IMAGE%@*}" "${OAUTH_PROXY_IMAGE##*@}"
```

Copy the printed `newName` and `digest` lines under `- name: oauth-proxy`,
indenting them to match the other image fields. Remove any existing `newTag`
from that entry; never put `sha256:...` in `newTag`.
Also pin PostgreSQL by adding an image replacement whose
`name` is `registry.redhat.io/rhel9/postgresql-15` and whose `digest` is the
verified vendor digest. A registry-authenticated Podman pull followed by
`podman image inspect --format '{{.RepoDigests}}' IMAGE` provides that digest.

### Choose a version tag or a digest

Leave in place unless you are targeting a new containerimage hosted in a different repo+tag

```yaml
# Version/tag: renders quay.io/jbatchel/aap-bridge-api:810925561e26
images:
  - name: aap-bridge-api
    newName: quay.io/jbatchel/aap-bridge-api
    newTag: "810925561e26"
```

The `name` field must match the image in the base manifests: `aap-bridge-api`,
`aap-bridge-ui`, `oauth-proxy`, or `registry.redhat.io/rhel9/postgresql-15`.
For example, `name: postgresql-15` does not match this package's PostgreSQL image.

After editing the overlay, regenerate `local/rendered.yaml` before applying it;
the previously rendered file does not update automatically. No image rebuild is
needed when selecting an already-published tag or digest. Applying the new
Deployment image references creates a replacement pod automatically.

Optionally add strategic merge patches under `patches` for:

- Route `aap-bridge`: `spec.host` (otherwise OpenShift generates it).
- PVCs `aap-bridge-db` and `aap-bridge-data`: `spec.storageClassName` and capacity.
- Deployment or StatefulSet resource requests/limits for your migration size.

For example, a PVC override in the kustomization is:

```yaml
patches:
  - target:
      kind: PersistentVolumeClaim
    patch: |-
      - op: add
        path: /spec/storageClassName
        value: YOUR_STORAGE_CLASS
```

Generate Secrets once for a new installation. The script creates a mode-0600
multi-document YAML file, refuses to overwrite it, and does not print secret values.
Use the project virtual environment if your system Python does not provide PyYAML:

```bash
python3 deploy/openshift/generate-secrets.py --namespace "$NAMESPACE"
oc create -n "$NAMESPACE" -f deploy/openshift/local/secrets.yaml
```

`secrets.example.yaml` describes required keys and is deliberately excluded from
Kustomize. Never apply its placeholders. The generated database URL explicitly selects `postgresql+psycopg2://`, matching
the installed driver, and uses a hexadecimal password to avoid URL escaping problems. For manually supplied
credentials, percent-encode the username/password in the URL.

Retain the encryption key securely with database backups. Replacing it makes
existing encrypted AAP tokens unreadable. This is not a key-rotation tool.

If vendor images need a pull secret, use Podman's default Linux authentication
file after running `podman login registry.redhat.io`:

```bash
oc create -n "$NAMESPACE" secret generic vendor-pull \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="${XDG_RUNTIME_DIR}/containers/auth.json"
```

This copies every registry entry in that file, including Quay credentials if
stored there. To include only vendor pull credentials, use a separate auth file
instead. If you logged in with `--authfile` or `REGISTRY_AUTH_FILE`, substitute
that path in the command.

Declare `imagePullSecrets: [{name: vendor-pull}]` in overlay patches for both
ServiceAccounts (`aap-bridge` and `aap-bridge-db`). Public application images need no pull credentials.
Clusters with suitable global vendor pull credentials need no additional secret.

## 3. Validate, deploy and grant access

```bash
oc kustomize deploy/openshift/local/install > deploy/openshift/local/rendered.yaml
oc apply -n "$NAMESPACE" --dry-run=server -f deploy/openshift/local/rendered.yaml
oc apply -n "$NAMESPACE" -f deploy/openshift/local/rendered.yaml
oc rollout status -n "$NAMESPACE" statefulset/aap-bridge-db --timeout=300s
oc rollout status -n "$NAMESPACE" deployment/aap-bridge --timeout=300s
oc get -n "$NAMESPACE" pods,pvc,route
```

The service certificate controller creates `aap-bridge-tls`. The Route uses the
cluster ingress certificate externally and verifies the service certificate
internally. No certificate or private key belongs in Git.

The API init container waits for an authenticated database query before startup.
Another non-root init container creates artifact directories on the mounted
volume. The application creates its database tables on startup.

To grant application access, copy `example/access.example.yaml` into the local
directory, replace its group name with an existing authorized OpenShift group,
and apply it with `-n "$NAMESPACE"`. 

```bash
cp deploy/openshift/example/access.example.yaml deploy/openshift/local/install/access-groups.yaml
## update deploy/openshift/local/install/access-groups.yaml
oc apply -f deploy/openshift/local/install/access-groups.yaml
```


Alternatively grant a specific existing user:

```bash
oc create rolebinding aap-bridge-user -n "$NAMESPACE" \
  --role=aap-bridge-access --user=YOUR_OPENSHIFT_USER
```

Do not grant access to `system:authenticated`. Existing namespace readers may
already satisfy the Service-read check; review namespace RBAC accordingly.
The proxy performs a user SubjectAccessReview and needs no cluster-wide
`auth-delegator` binding for this browser login flow.

```bash
ROUTE_HOST=$(oc get route aap-bridge -n "$NAMESPACE" -o jsonpath='{.spec.host}')
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' \
  "https://$ROUTE_HOST/"
```

Expect a redirect to OpenShift OAuth. Do not use `-k` to conceal certificate
problems. Logging in and configuring connections are separate application tasks.
Connections are managed through the UI; `SOURCE__*` and `TARGET__*` environment
variables do not populate saved Web UI connections.

## 4. Infrastructure checks and troubleshooting

```bash
oc get pods -n "$NAMESPACE" -o wide
oc get events -n "$NAMESPACE" --sort-by=.lastTimestamp
oc logs -n "$NAMESPACE" deployment/aap-bridge -c api --tail=50
oc logs -n "$NAMESPACE" deployment/aap-bridge -c oauth-proxy --tail=50
oc logs -n "$NAMESPACE" statefulset/aap-bridge-db --tail=50
oc exec -n "$NAMESPACE" deployment/aap-bridge -c api -- python3.12 -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/versions').status)"
oc exec -n "$NAMESPACE" deployment/aap-bridge -c ui -- \
  curl --fail --silent --output /dev/null http://127.0.0.1:8080/
```

Check pod annotation `openshift.io/scc` and runtime `id`: workloads must use the
cluster's restricted SCC and assigned IDs, not an `anyuid` exception. The image
USER is only a non-root fallback. PVC permissions rely on SCC-assigned fsGroup;
no cluster-specific IDs are hardcoded.

- `ImagePullBackOff`: verify image digest, public Quay access, vendor credentials,
  registry reachability and architecture.
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

## 5. Updates, persistence and backups

Finish migration jobs before updates. Rebuild/push changed images with a new
revision tag, update digests, validate, apply, and wait for rollout. ConfigMap
content changes get a new generated name and trigger a rollout. For Secret-only
changes, deliberately restart the application after updating Secrets:

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

## 6. Teardown

To stop the installation while preserving its data:

```bash
oc scale -n "$NAMESPACE" deployment/aap-bridge --replicas=0
oc scale -n "$NAMESPACE" statefulset/aap-bridge-db --replicas=0
```

To remove running workload definitions, delete the Deployment, StatefulSet,
Services and Route explicitly. Keep PVCs and Secrets for reuse. Do not use
`oc delete -k`, delete the namespace, or delete PVCs as routine teardown: those
operations remove data claims. Final data destruction requires a separate,
intentional action and consideration of the storage class's reclaim policy.

## Validation boundary

Infrastructure acceptance covers image build/pull, restricted admission, PVC
binding/writes, PostgreSQL/API/nginx readiness, TLS and unauthenticated OAuth
redirects. It does not log into the UI, create connections, contact AAP instances,
or run migrations. Browser login, authenticated WebSockets, connection tests,
and actual backup/restore remain separate follow-up checks.

References: [OpenShift OAuth proxy](https://github.com/openshift/oauth-proxy),
[OpenShift image guidelines](https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/images/creating-images).
