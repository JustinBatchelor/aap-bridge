#!/usr/bin/env python3
"""Set a password/PAT in a raw credential export before transformation and first import.

Python 3.6+; standard library only. This edits local files and does not contact AAP.
"""

import argparse
import getpass
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path


class ExportUpdateError(Exception):
    """An export could not be safely updated."""


def positive_id(value):
    try:
        credential_id = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("credential ID must be a positive integer") from None
    if credential_id <= 0:
        raise argparse.ArgumentTypeError("credential ID must be a positive integer")
    return credential_id


def require_terminal():
    if not sys.stdin.isatty():
        raise ExportUpdateError(
            "An interactive terminal is required; use oc exec -it in OpenShift."
        )


def find_credential(export_dir, credential_id=None):
    """List exports and select one record by source ID or an unambiguous exact name."""
    files = sorted((export_dir / "credentials").glob("credentials_*.json"))
    if not files:
        raise ExportUpdateError("No credentials_*.json files found under the export directory.")
    entries = []
    for path in files:
        if path.is_symlink():
            raise ExportUpdateError("Credential export files must not be symbolic links.")
        original = path.read_bytes()
        try:
            records = json.loads(original.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ExportUpdateError(f"Invalid JSON in {path.name}.") from None
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ExportUpdateError(f"Expected a JSON array of credential objects in {path.name}.")
        entries.extend((path, original, records, credential) for credential in records)
    if credential_id is None:
        require_terminal()
        if not entries:
            raise ExportUpdateError("The export contains no credentials.")
        print("Exported credentials (source ID | name):")
        for _, _, _, credential in entries:
            print(
                "  {} | {}".format(
                    credential.get("id", "missing"), credential.get("name", "unnamed")
                )
            )
        while credential_id is None:
            selection = input("Credential source ID or exact name (q to cancel): ").strip()
            if selection.lower() == "q":
                raise EOFError
            if selection.isdecimal():
                chosen_id = int(selection)
                matches = [
                    entry
                    for entry in entries
                    if type(entry[3].get("id")) is int and entry[3]["id"] == chosen_id
                ]
            else:
                matches = [entry for entry in entries if entry[3].get("name") == selection]
            if not matches:
                print("No matching credential. Enter a listed source ID or exact name.")
            elif len(matches) > 1 and not selection.isdecimal():
                print("That name is duplicated. Select the source ID of the intended credential.")
            else:
                credential_id = matches[0][3].get("id")
                if type(credential_id) is not int or credential_id <= 0:
                    raise ExportUpdateError("The selected credential has an invalid source ID.")
    matches = [
        entry
        for entry in entries
        if type(entry[3].get("id")) is int and entry[3]["id"] == credential_id
    ]
    if len(matches) != 1:
        raise ExportUpdateError(
            f"Expected exactly one credential with source ID {credential_id}; found {len(matches)}. Check the ID and export."
        )
    path, original, records, credential = matches[0]
    inputs = credential.get("inputs")
    if not isinstance(inputs, dict) or "password" not in inputs:
        raise ExportUpdateError(
            "The selected credential has no inputs.password field. Check its credential type."
        )
    return path, original, records, credential


def read_password():
    """Require hidden interactive entry; never fall back to an echoed password."""
    require_terminal()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Repository password/PAT: ")
            confirmation = getpass.getpass("Repeat password/PAT: ")
    except getpass.GetPassWarning:
        raise ExportUpdateError("Hidden input is unavailable; no password was read.") from None
    if not password or password == "$encrypted$":
        raise ExportUpdateError("Enter a non-empty real password/PAT, not $encrypted$.")
    if password != confirmation:
        raise ExportUpdateError("Passwords/PATs did not match.")
    return password


def write_credential(path, original, records):
    """Replace one chunk atomically with mode 0600 and retain no extra secret copy."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent), delete=False
        ) as stream:
            temporary_path = stream.name
            json.dump(records, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or path.read_bytes() != original:
            raise ExportUpdateError(
                "The export changed while entering the password; no update was applied."
            )
        os.replace(temporary_path, str(path))
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--credential-id",
        type=positive_id,
        help="Select a source ID directly (default: list credentials and prompt for selection)",
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=Path("/data/exports"),
        help="Raw export root (default: /data/exports)",
    )
    args = parser.parse_args(argv)
    try:
        path, original, records, credential = find_credential(args.export_dir, args.credential_id)
        print(
            "Selected: {} (source ID {})".format(
                credential.get("name", "unnamed"), credential["id"]
            )
        )
        credential["inputs"]["password"] = read_password()
        write_credential(path, original, records)
    except (ExportUpdateError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Cancelled; export not updated.", file=sys.stderr)
        return 130
    print(f"Updated {path} (mode 0600). No AAP changes were made.")
    print(
        "Next: transform, then first-time Phase 1 import. Existing target credentials are not updated."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
