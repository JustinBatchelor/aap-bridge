# Build application images inside OpenShift

This optional workflow builds the API and UI from a published Git commit using
OpenShift BuildConfigs, stores images in the integrated registry, and tracks
outputs in ImageStreams. It reuses the OpenShift Containerfiles and application
base. The Podman/Quay workflow remains supported.

| Concern | Podman/Quay workflow | BuildConfig/ImageStream workflow |
| --- | --- | --- |
| Build execution | Local workstation | OpenShift build pods |
| Source | Working tree, including local edits | Explicit published Git commit |
| Storage | Public Quay repositories | Integrated registry, namespace access controls |
| Tracking | Registry tags and digests | Build records, ImageStreams, and output digests |
| Deployment | Kustomize image references | Kustomize immutable internal image references |
| Resource cost | Workstation CPU/disk | Cluster CPU/memory and registry storage |
| Promotion | Manual | Manual; builds do not restart the application |

ImageStreams track image metadata, not image-layer storage. No DeploymentConfig,
Tekton installation, webhook, or automatic image-change rollout is required.
Both BuildConfigs have empty triggers and use the Docker build strategy (not a
Docker daemon on the worker). Build admission uses the cluster's standard build
service account policy; application pods retain their restricted security policy.
Do not grant application service accounts privileged SCC access to fix a build.

## 1. Prepare a separate build/validation namespace

Run from the repository root. You need `oc`, Python 3, namespace/build permissions,
an available integrated registry, amd64 workers, and outbound access to GitHub,
registry.redhat.io, package repositories, PyPI, and npm. No local Podman build is
required. Registry storage and cluster capacity must accommodate both images.

```bash
NAMESPACE=aap-bridge-build-test
REVISION=$(git rev-parse main)
# Ensure REVISION is published in the Git repository configured below.
oc create namespace "$NAMESPACE"
mkdir -p deploy/openshift/local/build-test
```

If the namespace already exists, inspect its ownership before applying anything.
Use explicit `-n` arguments; do not change your current CLI project. Use a separate
namespace for validation so builds and fresh test data cannot affect the working
Bridge. Do not copy application Secrets or database contents between instances.

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

The namespace's normal builder account pushes to its own integrated registry
repository. Do not supply Quay push credentials, application tokens, or the
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
  --output deploy/openshift/local/build-test/images.json
```

The helper starts API then UI, verifies the source revision of each successful
Build, and prints an `images:` Kustomize fragment. It reads each Build's output
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

## 3. Validate a fresh Bridge instance

Follow [the deployment guide](../readme.md#2-prepare-namespace-overlay-and-secrets)
to create a new local application overlay in this namespace. Set its resources
path to the existing base and replace the two application image entries with the
helper's printed digest entries. Do not include `newTag` in those entries.

For an overlay at `deploy/openshift/local/build-test/install`, the base path is
`../../../base`. Keep build resources separate from the application overlay so
application teardown does not remove build outputs. Set a unique Route host,
storage class, and fresh PVCs. Generate new Secrets with a unique output path:

```bash
python3 deploy/openshift/generate-secrets.py --namespace "$NAMESPACE" \
  --output deploy/openshift/local/build-test/secrets.yaml
oc create -n "$NAMESPACE" -f deploy/openshift/local/build-test/secrets.yaml
```

Use the OAuth proxy component digest from this cluster's release and the vendor
PostgreSQL image. If using `vendor-pull`, add it to the two application ServiceAccounts
through overlay patches as described in the main guide. Same-namespace application
service accounts normally have integrated registry pull access; verify that access
and the namespace's default service-account registry configuration before rollout.

Render and inspect all image substitutions before applying. The API digest must
appear in `initialize-data`, `wait-for-database`, and `api`; the UI digest in `ui`.
There must be no `image.openshift.io/triggers` annotation or automatic deployment
trigger. A new candidate build alone must not change running pods.

Validate server-side admission, build completion, image pulls, assigned UIDs,
PVC binding/writes, database readiness, probes, internal API/nginx responses,
and external Route TLS/OAuth redirect. Check `/api/versions` for populated choices.
Test migration-state initialization against only the fresh database with
`postgresql+psycopg2://`, and confirm credential redaction. Audit source inputs and
final application files for private artifacts. Do not run AAP migrations or contact
source/target endpoints as part of infrastructure validation.

Keep a local dated record of the Git commit, Build names, output digests, workload
status, Route URL, and checks. Browser login, connection configuration, authenticated
WebSockets, migration execution, and backup/restore remain manual follow-up tests.

## 4. Later manual promotion and rollback

Complete active migrations before changing the working application; its `Recreate`
strategy causes downtime and restarts interrupt jobs. First record the current
application image references and retain the existing Secrets, database, and PVCs.

When pulling from a separate build namespace, grant only the consuming application's
service account image-pull rights in the build namespace (substitute both names):

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
No source dependency or application schema change is part of this workflow.

## Troubleshooting and cleanup

- **Git clone failure:** verify the configured URL and published commit, DNS/egress,
  or source Secret for a private repository.
- **Base image pull denied:** verify the builder service account's Red Hat pull
  Secret. UI builds pull two base images; both stages need access.
- **Internal push/pull denied:** check registry health, builder permissions, service
  account pull access, and cross-namespace `system:image-puller` grants.
- **Pending/failed build:** inspect Build and pod events for quota, admission,
  scheduling, OOM, or deadline failures; adjust only build resources as needed.
- **Stale runtime image:** compare rendered digests with recorded Build outputs;
  do not expect candidate-tag updates to restart a digest-pinned Deployment.

Leave a review instance running until its owner requests cleanup. For data-preserving
teardown, remove the test Deployment, StatefulSet, and Route by name while retaining
PVCs, Secrets, BuildConfigs, and ImageStreams. Do not use namespace deletion or
`oc delete -k` as a data-preserving operation. Deleting test PVCs later can leave
backing storage behind under a Retain policy; follow the existing storage runbook.

References: [Docker build strategy](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/builds_using_buildconfig/build-strategies)
and [ImageStream deployment triggers](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/images/triggering-updates-on-imagestream-changes).
