# Update exported credential data

Use `tools/update_exported_credential.py` to replace an exported credential's
`inputs.password` with a real password or personal access token (PAT). This is
useful for HTTPS Source Control credentials because the source API returns
`$encrypted$` in place of the secret.

Run the helper **after Export, before Transform and the credential's first Phase 1
import**. It edits local raw export files without contacting AAP. It requires
Python 3.6 or newer, the standard library, and an interactive terminal.

The helper changes only `inputs.password` on one credential per run. It does not
replace SSH keys, usernames, other secret fields, or all `$encrypted$` values.
For HTTPS repository access, ensure the exported username matches your repository's
authentication method and the source projects reference the intended credential.

## 1. Choose the raw export directory

Finish Export and stop other export, transform, and import operations while editing.
Run commands from the repository root. Pass the raw export root containing
`credentials/`, rather than a JSON chunk filename or the transformed directory:

```text
exports/
└── credentials/
    ├── credentials_0001.json
    └── credentials_0002.json
```

For an existing OpenShift CLI deployment, use the
[pod instructions][openshift-credentials] to copy and run the
helper on the artifact volume. No image rebuild is required.

## 2. Select a credential and enter its secret

For a local export:

```bash
python3 tools/update_exported_credential.py --export-dir ./exports
```

The helper lists credential names and **source IDs** across all credential chunks.
Enter an exact name or source ID. If names are duplicated, select by ID. Enter `q`
at the selection prompt, or press Ctrl-C, to cancel without writing changes.

```text
Exported credentials (source ID | name):
  12 | Private repo PAT
  18 | Machine credential
Credential source ID or exact name (q to cancel): Private repo PAT
Selected: Private repo PAT (source ID 12)
Repository password/PAT:
Repeat password/PAT:
```

Enter the same real password or PAT twice at the hidden prompts. The helper does
not display the current input values or the new secret. Do not pass secrets as
command arguments or pipe them into the script.

To select a known source ID directly while keeping secret entry interactive:

```bash
python3 tools/update_exported_credential.py --export-dir ./exports --credential-id 12
```

| Option | Meaning |
| --- | --- |
| `--export-dir PATH` | Raw export root; defaults to `/data/exports` when omitted. |
| `--credential-id ID` | Positive source credential ID; omit to select interactively. |
| `--help` | Show usage without editing files. |

On success, the helper prints the updated chunk path and its mode (`0600`). It
atomically replaces that chunk, preserving other records and fields, and retains
no backup or extra plaintext copy. The raw export now contains a real secret;
keep it and subsequent transformed artifacts private and out of Git. Repeat the
helper for each credential whose password needs replacement.

## 3. Transform, then import

Run Transform against the edited raw export. If you transformed before editing,
rerun Transform so the updated password reaches the data used by Import. For a
local CLI workflow with `./exports` and `./transformed`:

```bash
aap-bridge transform --input ./exports --output ./transformed
```

When ready to create target resources, run the first Phase 1 import using that
transformed directory:

```bash
aap-bridge import --input ./transformed --phase phase1
```

In the interactive menu, use **option 3 — Transform**, then **option 4 — Phase 1
import**. The supplied password is preserved through transformation and sent when
the target credential is created. Keep Phase 2 paused until all required target
SCM credentials are valid; remaining `$encrypted$` fields become temporary values
during transformation and cannot authenticate project syncs.

An export edit does not update credentials already imported into target AAP.
Bridge normally skips migrated credentials; update those secrets directly in
target AAP. If you recreated target AAP, reconcile Bridge's migration state before
importing again; this helper does not reset progress or repair target ID mappings.

If a fresh Export overwrites the edited data, the source API returns `$encrypted$`
again. Repeat the replacement and Transform before the first credential import.

## Troubleshooting

| Message or condition | Action |
| --- | --- |
| No `credentials_*.json` files found | Check `--export-dir` points to the raw root and that credentials were exported. |
| Missing or duplicate source ID | Check the selected source ID and ensure the export contains exactly one matching record. |
| No `inputs.password` field | Check the credential type; this helper requires an existing password field. |
| Interactive terminal or hidden input unavailable | Use a terminal; for OpenShift, run with `oc exec -it`. |
| Empty, `$encrypted$`, or mismatched secret | Rerun and enter the same non-empty real password or PAT twice. |
| Invalid JSON or symbolic-link chunk | Correct the raw export layout or regenerate it before retrying. |
| Export changed during secret entry | Stop other writers, check the current export, and rerun. The concurrent change is not overwritten. |

[openshift-credentials]: https://github.com/JustinBatchelor/aap-bridge/blob/feat/openshift-cli/deploy/openshift/cli/credentials.md
