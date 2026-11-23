# Fill exported credential secrets

For the complete workflow, options, and troubleshooting, see
[Update Exported Credentials](../../../docs/user-guide/updating-exported-credentials.md).
The commands below run the helper against an existing OpenShift CLI deployment.

Use this after **Export**, before **Transform and the credential's first Phase 1
import**. Bulk mode replaces `$encrypted$` input values from a plaintext JSON dump
of the same source AAP instance. It matches source IDs and requires matching names.

The helper edits the raw export only. It does not contact AAP or start project syncs.
If the credential was already imported, update it directly in target AAP instead;
Bridge normally skips previously imported credentials.

## Copy the helper into the existing Bridge pod

Use a checkout containing `tools/update_exported_credential.py` on your workstation.
Set `NAMESPACE` to the existing CLI deployment's namespace and stop export, transform,
and import operations while editing. From the repository root, copy the script
into the running Bridge pod; no image rebuild is needed:

```bash
export NAMESPACE=aap-bridge-cli

oc -n "$NAMESPACE" exec -i deployment/aap-bridge-cli -c cli -- \
  sh -c 'umask 077; cat > /tmp/update_exported_credential.py' \
  < tools/update_exported_credential.py
```

## Bulk-fill secrets from the supplied JSON

Keep the supplied JSON on your workstation, outside Git. Its format must be
`{"credentials": [...]}`, with each credential containing `id`, `name`, and
`inputs`. Stop export, transform, and import commands before proceeding.
Set the path to your actual file, then preview replacements:

```bash
export CREDENTIALS_FILE=/secure/path/credentials.json
chmod 600 "$CREDENTIALS_FILE"

oc -n "$NAMESPACE" exec -i deployment/aap-bridge-cli -c cli -- \
  python3.12 /tmp/update_exported_credential.py --credentials-file - \
  < "$CREDENTIALS_FILE"
```

The preview lists credential IDs, names, and field paths, without showing secrets
or editing exports. Every encrypted input must have a real, non-empty string in
the supplied file. Missing values or mismatched names block **all edits**.
Once the preview succeeds, apply:

```bash
oc -n "$NAMESPACE" exec -i deployment/aap-bridge-cli -c cli -- \
  python3.12 /tmp/update_exported_credential.py --credentials-file - --apply \
  < "$CREDENTIALS_FILE"
```

The file is streamed over stdin; no separate plaintext dump is stored in the pod.
Only exact `$encrypted$` values under credential `inputs` are replaced, including
passwords, tokens, and SSH keys. Existing plaintext inputs and metadata are preserved.
Unused supplied credentials are allowed and counted. Exported chunks now contain
plaintext secrets with mode `0600`; protect the artifact volume and backups.

Each changed chunk is replaced atomically, but the whole batch is not one filesystem
transaction. If writing fails, review the completed/pending chunk report, stop other
writers, and rerun; already-filled inputs remain untouched. The script exits nonzero
for validation or write failures. Do not proceed to Transform until it succeeds.

Then run **option 3 — Transform**, followed by **option 4 — Phase 1 import**.
Keep Phase 2 paused until all required target SCM credentials are valid. Fresh
exports restore `$encrypted$` placeholders, so repeat this process after re-exporting.

## Update one repository password/PAT interactively

For a Source Control credential using HTTPS, the repository PAT is stored in
`inputs.password`. Its username must match the repository's authentication method.
Run the helper interactively:

```bash
oc -n "$NAMESPACE" exec -it deployment/aap-bridge-cli -c cli -- \
  python3.12 /tmp/update_exported_credential.py
```

The script lists exported credential names and source IDs, then asks which credential
to update. Enter its exact name or source ID. Duplicate names require selection by
ID; `q` cancels. It displays no existing input values. For example:

```text
Exported credentials (source ID | name):
  12 | Private repo PAT
  18 | Machine credential
Credential source ID or exact name (q to cancel): Private repo PAT
Selected: Private repo PAT (source ID 12)
Repository password/PAT:
Repeat password/PAT:
```

Enter the PAT twice at the hidden prompts. Do not supply it in command arguments.
The helper finds the matching object across `/data/exports/credentials/credentials_*.json`,
updates only its password, and atomically replaces that chunk with mode 0600. Missing
or duplicate IDs, malformed files, empty/mismatched secrets, and detected concurrent
edits are refused. No backup or additional plaintext secret copy is retained by the helper.
The updated export itself now contains the real secret; keep it private.

Then run **menu option 3 — Transform**, followed by **option 4 — Phase 1 import**.
For a new credential, the real value is preserved through transformation and sent
when creating the target credential. Run this helper again if you overwrite the raw
export with a fresh export, which will contain `$encrypted$` again.

Ensure the intended source projects reference the new credential before exporting
those projects. Keep phase 2 paused until **all** required target SCM credentials
are valid; other `$encrypted$` fields are transformed into temporary values and can
cause failed authentication if used for project syncs.

If you recreated target AAP, reconcile Bridge's existing migration state before
importing again; this helper does not reset progress or repair old target ID mappings.

## Run against a local export

The script uses only the standard library and supports Python 3.6 or newer:

```bash
python3 tools/update_exported_credential.py --export-dir ./exports
```

Use `--export-dir` for the raw export root containing `credentials/`, rather than
the chunk filename. Keep export, transform, and import commands stopped while editing.
You can also pass `--credential-id 123` to select a known source ID directly; the
PAT is still entered at hidden prompts.
