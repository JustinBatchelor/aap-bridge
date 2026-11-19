# CLI deployment options and troubleshooting

Start with the [deployment README](readme.md). These options are only needed when
your environment differs from its defaults. Run commands from the repository root
with `NAMESPACE` and `PYTHON` set as in that README.

## Environment prerequisites

Confirm permissions to create builds and workloads, integrated registry availability,
quota, storage provisioning and access to Git, Python packages and container registries.
The build has no node selector; its architecture must match the deployment nodes.
The PostgreSQL image must support that architecture too. The commit passed to the
build helper must be published in your fork and contain `Containerfile.cli`; the
older browser revision `de39ede2b2c493513a469aa7a4eccd9f5ed4e994` cannot build it.

For a private Git fork, set the BuildConfig's `source.sourceSecret` using an appropriate
Git authentication Secret. Do not put credentials in the repository URL.

## Optional registry credentials

Use the cluster's existing global pull secret when it has access to the required
images. A separate namespace Secret is only needed when that access is unavailable
and your cluster's registry policy permits namespace credentials. For mirrored
registries, have the administrator configure the global credentials instead.

If needed, create a private registry Secret using an interactive Podman login:

```bash
umask 077
podman login --authfile deploy/openshift/local/cli/registry-auth.json registry.redhat.io
oc -n "$NAMESPACE" create secret generic vendor-pull \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=deploy/openshift/local/cli/registry-auth.json
```

After the CLI BuildConfig exists, configure it to use that Secret before retrying
the build. For a PostgreSQL pull failure, link it to the database ServiceAccount
after the installation resources exist:

```bash
oc -n "$NAMESPACE" set build-secret --pull bc/aap-bridge-cli vendor-pull
oc -n "$NAMESPACE" secrets link aap-bridge-db vendor-pull --for=pull
```

Skip creation if the Secret already exists. Do not print or commit its credentials.

## StorageClass and PVC sizes

The default is the cluster's default StorageClass with 10 GiB for PostgreSQL and
20 GiB for artifacts. Before applying the installation, add this `patches` section
to `deploy/openshift/local/cli/install/kustomization.yaml` (append the entry if the
list already exists):

```yaml
patches:
- target:
    kind: PersistentVolumeClaim
  patch: |-
    - op: add
      path: /spec/storageClassName
      value: truenas-nfs
```

Replace `truenas-nfs` with your class. For sizes, add separate patches targeting
PVC `aap-bridge-db` or `aap-bridge-data` at `/spec/resources/requests/storage`.
Storage must allow SCC-assigned non-root UID/group access. Check reclaim policy
and expansion support; do not change an existing PVC's StorageClass in place.
Rerender the installation after editing its overlay.

## Private AAP certificates

TLS verification defaults to enabled. For a private AAP CA, create a ConfigMap from
a PEM bundle containing the required public and private trust roots, mount it
read-only at `/etc/aap-ca` in both toolbox and Job pods, and set `SSL_CERT_FILE` and
`REQUESTS_CA_BUNDLE` to `/etc/aap-ca/ca-bundle.pem` in the CLI container. Apply those
same patches to both local overlays. HTTPX uses `SSL_CERT_FILE`; Requests uses
`REQUESTS_CA_BUNDLE`. Do not disable verification to bypass missing trust.

For example, prepare the bundle locally, then create the ConfigMap and copy its
patch into the installation overlay (and the Job overlay if using Jobs):

```bash
mkdir -p deploy/openshift/local/cli/{install,job}
oc -n "$NAMESPACE" create configmap aap-bridge-cli-ca --from-file=ca-bundle.pem=/secure/path/ca-bundle.pem
cp deploy/openshift/cli/example/ca-patch.yaml deploy/openshift/local/cli/install/ca-patch.yaml
cp deploy/openshift/cli/example/ca-patch.yaml deploy/openshift/local/cli/job/ca-patch.yaml
```

Add this `patches` section to each overlay (append the entry if its list already exists):

```yaml
patches:
- target:
    kind: Deployment|Job
    name: aap-bridge-cli.*
  path: ca-patch.yaml
```

The supplied patch adds the two trust environment variables. Merge it with any
other custom `env` entries instead of replacing them.

## Custom CLI configuration

The complete `cli/config.yaml` includes source/target settings, persistent `/data`
paths, and PostgreSQL state. To customize it, copy it into the local install directory
and add this generator to the installation Kustomization:

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

## Pod behavior and access

Connections, artifacts, and migration state are shared by operators of this installation.
Grant pod exec access only to operators trusted with its AAP credentials and data.

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

Configuration validation is offline. `config validate --check-connectivity` checks
the target ping, but its source check currently only constructs the client. Confirm
an approved source read/export before migration. If an exec session disconnects,
check running processes and migration state before starting another operation.

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

Cluster acceptance remains manual. For SCC and identity inspection:

```bash
oc -n "$NAMESPACE" get pods -l app=aap-bridge-cli \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.annotations.openshift\.io/scc}{"\n"}{end}'
oc exec -n "$NAMESPACE" deployment/aap-bridge-cli -c cli -- id
```

Acceptance checklist:

- Confirm only the CLI and PostgreSQL images run; inspect init containers too.
- Confirm restricted SCC admission, assigned non-root UID, no token mount, and no elevated grants.
- Confirm both PVCs bind, artifact paths are writable, and database initialization passes.
- Confirm authorized `oc exec`, TLS trust and approved source/target AAP operations.
- Confirm no application Service/Route/listener exists and ingress policies select the expected pods.
- Confirm toolbox-to-Job handoff, fixed-name collision, failed Job review and no automatic retries.
- Write a test artifact and state record; restart workloads and verify persistence before migration.
- Restore a backup in an isolated namespace and verify state/artifacts together.

See [validation evidence](validation.md) for checks actually performed during implementation.
