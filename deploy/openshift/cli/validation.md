# CLI implementation validation

Validated locally on 2026-11-19 from fork main `3e7410d`, with the CLI changes on
`feat/openshift-cli` in `batchelor.live/aap-bridge`.

## Passed locally

- Full Python suite: **359 passed, 87 skipped**. Skips are missing versioned AAP
  fixture data; no live AAP migration was performed.
- CLI and browser build-helper tests: CLI starts exactly one build, verifies the
  source SHA and digest, rejects failures, retains prior output, and never deploys.
- Secret generation: matching database URL/password, mode 0600, overwrite refusal,
  and no OAuth/encryption fields in CLI mode; browser defaults remain covered.
- Offline Kustomize rendering: independent CLI build, toolbox and Job inventories;
  namespace/digest/storage patches, selectors, mounts, Secrets, security settings,
  network policies and Job controls. Rendered existing browser build and deployment
  resources are semantically identical before and after shared-resource extraction.
- Synthetic CLI configuration loads with TLS verification enabled and `/data` paths;
  validate/show do not disclose the database password or treat its URL as a path.
- Local **linux/arm64** CLI image built with Python 3.12 using the public UBI base
  override `registry.access.redhat.com/ubi9/ubi-minimal:latest`. The recipe defaults
  to the authenticated `registry.redhat.io` base. No image was pushed.
- Image smoke checks passed as UID 12345, group 0, without network, with all
  capabilities dropped, a read-only root filesystem and writable `/tmp` and `/data`.
  CLI version/help/config validation and artifact directory writes succeeded.
- No nginx/httpd RPMs, Node/npm executables or RPMs, application frontend paths,
  or optional FastAPI/Uvicorn/Starlette/WebSocket/multipart distributions were found.
- An idle `sleep infinity` container accepted a CLI exec session and shut down;
  the temporary smoke container was removed.
- Changed Python formatting/lint, Markdown lint, changelog checks, strict MkDocs
  build and Git whitespace checks passed.

The checked [RPM and Python package inventory](validation/image-inventory.yaml)
records the immutable local image ID, architecture and package versions. It contains
no customer credentials. Re-run `check-image.py` against the actual promoted image
and retain the new inventory separately; this record is not a vulnerability scan.

## Awaiting operator deployment

No cluster build, image push, workload deployment, or customer-environment change
was performed. An amd64 image, authenticated Red Hat registry build, actual registry
pull permissions, restricted-SCC admission, PVC/NFS permissions, PostgreSQL runtime
connectivity, private-CA trust, actual AAP reads/migrations, Job handoff and restart
persistence require operator validation. The manual acceptance checklist is in the
[advanced guide](advanced.md#local-image-checks-and-manual-cluster-acceptance). Local tests do not establish OpenShift runtime behavior.
