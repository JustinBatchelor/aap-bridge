# Set an exported repository credential's PAT

For the complete workflow, options, and troubleshooting, see
[Update Exported Credentials](../../../docs/user-guide/updating-exported-credentials.md).
The commands below run the helper against an existing OpenShift CLI deployment.

Use this after **Export**, before **Transform and the credential's first Phase 1
import**. For a Source Control credential using HTTPS, the repository PAT is stored
in `inputs.password`. Its username must match the repository's authentication method.

The helper edits the raw export only. It does not contact AAP or start project syncs.
If the credential was already imported, update it directly in target AAP instead;
Bridge normally skips previously imported credentials.

## Run in the existing Bridge pod

Use a checkout containing `tools/update_exported_credential.py` on your workstation.
Set `NAMESPACE` to the existing CLI deployment's namespace and stop export, transform,
and import operations while editing. From the repository root, copy the script
into the running Bridge pod; no image rebuild is needed:

```bash
oc -n "$NAMESPACE" exec -i deployment/aap-bridge-cli -c cli -- \
  sh -c 'umask 077; cat > /tmp/update_exported_credential.py' \
  < tools/update_exported_credential.py
```

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
