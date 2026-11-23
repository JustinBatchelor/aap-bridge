# Update exported credential data

Use `tools/update_exported_credential.py` to replace `$encrypted$` values under
exported credential `inputs`. Supply a plaintext credential dump for bulk replacement,
or select one credential and enter its password/PAT interactively.

Run the helper **after Export, before Transform and the credential's first Phase 1
import**. It edits local raw export files without contacting AAP. It requires
Python 3.6 or newer and the standard library. Only interactive mode needs a terminal.

Bulk mode handles passwords, tokens, SSH keys, and other encrypted input fields.
It preserves existing plaintext inputs and all metadata. For HTTPS repository access,
ensure the exported username matches your repository's authentication method and
the source projects reference the intended credential.

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

## 2. Bulk-fill from a plaintext credential dump

The supplied file must come from the **same source AAP instance** as the export.
It must be a JSON object with a `credentials` array. Each record needs an integer
source `id`, exact `name`, and an `inputs` object, for example:

```json
{
  "credentials": [
    {
      "id": 12,
      "name": "Private repo PAT",
      "inputs": {"password": "synthetic-example-only"}
    }
  ]
}
```

Extra metadata such as `organization`, `credential_type`, and `external_input_fields`
is allowed and is not copied over the export. Keep the actual plaintext dump outside
Git with restricted permissions. Preview first:

```bash
python3 tools/update_exported_credential.py \
  --credentials-file /secure/path/credentials.json --export-dir ./exports
```

The helper scans all credential chunks, matches by source ID, verifies exact names,
and reports matched field paths and unused supplied record counts. It never matches
by name alone or prints secret values. Every exact `$encrypted$` string under
`inputs`, including nested dictionaries/lists, requires a supplied non-empty string
at the same path. Missing, null, empty, non-string, or still-encrypted values block
all edits. Duplicate IDs, duplicate JSON keys, invalid records, and name mismatches
are also refused. Credentials without placeholders need no supplied record; unused
supplied records are allowed.

Apply only after preview succeeds:

```bash
python3 tools/update_exported_credential.py \
  --credentials-file /secure/path/credentials.json --export-dir ./exports --apply
```

Changed chunks are staged privately and replaced with mode `0600`; other resource
exports and existing plaintext values remain unchanged. The supplied file is read
only. Each chunk replacement is atomic; multiple replacements are **not** one
transaction. A write failure returns a nonzero status and lists completed/pending
chunks. Stop other writers and rerun safely to fill remaining placeholders. Validation
failures never modify exports. A successful rerun with no placeholders makes no edits.

`--credentials-file -` reads the dump from stdin without requiring a terminal. The
[OpenShift pod instructions][openshift-credentials] show how to stream the local file
into the pod without storing an extra copy. Bulk mode cannot be combined with
`--credential-id`, and `--apply` requires bulk mode.

## Alternative: select one credential and enter its password/PAT

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
| `--credentials-file PATH` | Bulk input JSON; `-` reads stdin. Defaults to preview. |
| `--apply` | Write fully validated bulk replacements. |
| `--help` | Show usage without editing files. |

On success, the helper prints the updated chunk path and its mode (`0600`). It
atomically replaces that chunk, preserving other records and fields, and retains
no backup or extra plaintext copy. The raw export now contains a real secret;
keep it and subsequent transformed artifacts private and out of Git. Repeat the
helper for each credential whose password needs replacement.

## 3. Transform, then import

Run Transform against the edited raw export. If you transformed before editing,
rerun Transform so the updated secrets reach the data used by Import. For a
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
import**. The supplied secrets are preserved through transformation and sent when
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
| Bulk: missing real value or mismatched name | Correct the supplied dump for the same source instance; all placeholders must resolve before any edits. |
| Bulk: writing stopped | Review completed/pending chunks, stop other writers, and rerun before transforming. |
| No `inputs.password` field | Interactive mode requires a password field; use bulk mode for other encrypted inputs. |
| Interactive terminal or hidden input unavailable | Use a terminal; for OpenShift, run with `oc exec -it`. |
| Empty, `$encrypted$`, or mismatched secret | Rerun and enter the same non-empty real password or PAT twice. |
| Invalid JSON or symbolic-link chunk | Correct the raw export layout or regenerate it before retrying. |
| Export changed during secret entry | Stop other writers, check the current export, and rerun. The concurrent change is not overwritten. |

[openshift-credentials]: https://github.com/JustinBatchelor/aap-bridge/blob/feat/openshift-cli/deploy/openshift/cli/credentials.md
