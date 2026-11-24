# Deploy the CLI on OpenShift

This installs the **AAP Bridge CLI and PostgreSQL**. No UI, nginx, Node/npm, OAuth
proxy, or API server is built or deployed. You open the CLI through `oc exec`.

Use a fresh namespace with a working default StorageClass. You need `oc`, Python 3.6 or newer,
and a cluster with working registry access and connectivity to both AAP systems.
Defaults: **10 GiB database + 20 GiB artifacts**. For a different StorageClass or
private AAP certificates, see [deployment options](advanced.md) before step 5.

Run the following from the repository root, in the same terminal. This is a first
installation procedure; if you already completed a step, continue from there.
Preserve existing Secrets and PVCs when [upgrading](operations.md#upgrade-or-roll-back).

## 1. Set up

Use your fork's published CLI branch and confirm the cluster before creating resources:

```bash
git switch feat/openshift-cli
git pull --ff-only
oc config current-context

export NAMESPACE=aap-bridge-cli
export REVISION=$(git rev-parse HEAD)
export CLI=deploy/openshift/cli
export LOCAL=deploy/openshift/local/cli

oc new-project "$NAMESPACE"
umask 077
mkdir -p "$LOCAL/install"
python3 -m venv .venv-cli-tools
.venv-cli-tools/bin/python -m pip install -r deploy/openshift/requirements-tools.txt
export PYTHON="$PWD/.venv-cli-tools/bin/python"
```

The local helpers support Python 3.6; migrations run with Python 3.12 inside the
container. The requirements file selects a compatible PyYAML release.

Change `NAMESPACE` to the name you want. If you already created that CLI-only
namespace, skip `oc new-project`.

## 2. Use the cluster's registry access

If the cluster's global pull secret already has valid access to `registry.redhat.io`,
continue to step 3. No extra registry Secret or ServiceAccount patch is needed.
If pulls fail authentication, see [optional registry credentials](advanced.md#optional-registry-credentials).

## 3. Build the CLI image

```bash
oc kustomize "$CLI/builds" > "$LOCAL/builds.yaml"
oc -n "$NAMESPACE" apply -f "$LOCAL/builds.yaml"

export RECORD="$LOCAL/images-$REVISION.yaml"
"$PYTHON" deploy/openshift/build-images.py --mode cli \
  --namespace "$NAMESPACE" --commit "$REVISION" --output "$RECORD"
cat "$RECORD"
```

This builds only `aap-bridge-cli`, with **no node selector**, and records the verified
commit and image digest. If you already built this revision, reuse its record;
choose a new record filename if rebuilding. Do not apply the browser `builds` directory.

## 4. Add your AAP connections

Generate the database credentials, then edit the connection file with your source
and target **URLs, AAP versions, and tokens**. Use unquoted values.

```bash
"$PYTHON" deploy/openshift/generate-secrets.py --mode cli \
  --namespace "$NAMESPACE" --output "$LOCAL/secrets.yaml"
cp "$CLI/connections.env.example" "$LOCAL/connections.env"
chmod 600 "$LOCAL/connections.env"
${EDITOR:-vi} "$LOCAL/connections.env"

oc -n "$NAMESPACE" create -f "$LOCAL/secrets.yaml"
oc -n "$NAMESPACE" create secret generic aap-bridge-connections \
  --from-env-file="$LOCAL/connections.env"
```

These files contain credentials: keep them private. Do not regenerate database
credentials for an existing installation. TLS verification is enabled by default.

## 5. Deploy

Create the installation settings using the image digest from step 3:

```bash
IMAGE=$("$PYTHON" -c 'import sys,yaml; i=yaml.safe_load(open(sys.argv[1]))["images"][0]; print(i["newName"]+"@"+i["digest"])' "$RECORD")
cat > "$LOCAL/install/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NAMESPACE
resources:
- ../../../cli
images:
- name: aap-bridge-cli
  newName: ${IMAGE%@*}
  digest: ${IMAGE#*@}
EOF
```

If needed, add your [StorageClass or private CA settings](advanced.md) now. Then
render, review the resource list, and apply:

```bash
oc kustomize "$LOCAL/install" > "$LOCAL/install.yaml"
oc apply --dry-run=client -f "$LOCAL/install.yaml" -o name
oc apply -f "$LOCAL/install.yaml"
oc -n "$NAMESPACE" rollout status statefulset/aap-bridge-db --timeout=10m
oc -n "$NAMESPACE" rollout status deployment/aap-bridge-cli --timeout=10m
```

Expect CLI and PostgreSQL workloads, two PVCs, configuration, service accounts,
network policies, and only a database Service. There should be no Route or UI/API/OAuth
workload. If startup fails, see [troubleshooting](advanced.md#existing-images-disconnected-use-and-troubleshooting).

## 6. Open the CLI

Validate your configuration, then launch the interactive menu:

```bash
oc exec -n "$NAMESPACE" deployment/aap-bridge-cli -c cli -- \
  aap-bridge --config /etc/aap-bridge/config.yaml config validate

oc exec -n "$NAMESPACE" deployment/aap-bridge-cli -c cli -- \
  aap-bridge --config /etc/aap-bridge/config.yaml \
  config validate --check-connectivity

oc exec -it -n "$NAMESPACE" deployment/aap-bridge-cli -c cli -- \
  aap-bridge --config /etc/aap-bridge/config.yaml
```

Use one operator at a time. Configuration validation does not verify AAP connectivity;
complete the [acceptance checklist](advanced.md#local-image-checks-and-manual-cluster-acceptance)
before migration. Follow the [migration workflow](../../../docs/user-guide/migration-workflow.md)
for export, transform, and import.

## When you need more

- [Fill exported secrets from a credential dump or enter one PAT](credentials.md).
- [Recover projects rejected during Manual creation](project-recovery.md).
- [Run a command as a Job](jobs.md) — stop the toolbox first; never run both at once.
- [Back up, upgrade, roll back, or remove the installation](operations.md).
- [Storage, private certificates, lab settings, and troubleshooting](advanced.md).
- [Local validation results and image package inventory](validation.md).
