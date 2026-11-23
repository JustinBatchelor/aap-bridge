#!/usr/bin/env python3
"""Fill secrets in raw credential exports before transformation and first import.

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


def parse_json(contents, label):
    """Reject ambiguous JSON without including its potentially secret contents in errors."""

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("non-JSON constant")

    try:
        return json.loads(
            contents, object_pairs_hook=unique_object, parse_constant=invalid_constant
        )
    except (ValueError, UnicodeError):
        raise ExportUpdateError(f"Invalid JSON or duplicate object keys in {label}.") from None


def index_credentials(records, label):
    if not isinstance(records, list):
        raise ExportUpdateError(f"Expected a credential array in {label}.")
    indexed = {}
    for record in records:
        if not isinstance(record, dict):
            raise ExportUpdateError(f"Expected credential objects in {label}.")
        credential_id = record.get("id")
        if type(credential_id) is not int or credential_id <= 0:
            raise ExportUpdateError(f"Invalid credential source ID in {label}.")
        if credential_id in indexed:
            raise ExportUpdateError(f"Duplicate credential source ID {credential_id} in {label}.")
        if not isinstance(record.get("name"), str) or not record["name"]:
            raise ExportUpdateError(f"Missing credential name for source ID {credential_id}.")
        if not isinstance(record.get("inputs"), dict):
            raise ExportUpdateError(f"Expected inputs object for source ID {credential_id}.")
        indexed[credential_id] = record
    return indexed


def encrypted_paths(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from encrypted_paths(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from encrypted_paths(child, path + (index,))
    elif value == "$encrypted$":
        yield path


def field_name(path):
    return "inputs" + "".join(f"[{part}]" if isinstance(part, int) else "." + part for part in path)


def replacement_at(inputs, path):
    value = inputs
    for part in path:
        if isinstance(part, int):
            if not isinstance(value, list) or part >= len(value):
                return None
        elif not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def apply_bulk(chunks):
    """Stage all changed chunks first; individual replacements are atomic, the batch is not."""
    staged = []
    completed = []
    try:
        for path, original, records in chunks:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(path.parent), delete=False
            ) as stream:
                staged.append((path, original, stream.name))
                json.dump(records, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        # Check the entire batch before the first replacement, then again before each write.
        for path, original, _ in staged:
            if path.is_symlink() or path.read_bytes() != original:
                raise ExportUpdateError("An export changed during validation; stop other writers.")
        for path, original, temporary_path in staged:
            if path.is_symlink() or path.read_bytes() != original:
                raise ExportUpdateError("An export changed during writing; stop other writers.")
            os.replace(temporary_path, str(path))
            completed.append(path.name)
            print(f"Updated {path.name} (mode 0600).")
    except (OSError, ExportUpdateError, KeyboardInterrupt) as error:
        pending = [path.name for path, _, _ in chunks if path.name not in completed]
        detail = (
            (error.strerror or "filesystem operation failed")
            if isinstance(error, OSError)
            else str(error)
        )
        raise ExportUpdateError(
            "Writing stopped: {}. Completed chunks: {}. Pending chunks: {}. "
            "Stop other writers and rerun to process remaining placeholders.".format(
                detail or "interrupted",
                ", ".join(completed) or "none",
                ", ".join(pending) or "none",
            )
        ) from None
    finally:
        for _, _, temporary_path in staged:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)


def bulk_update(credentials_file, export_dir, apply=False):
    if credentials_file == "-":
        contents = sys.stdin.read()
    else:
        source_path = Path(credentials_file)
        if source_path.is_symlink():
            raise ExportUpdateError("The supplied credential file must not be a symbolic link.")
        contents = source_path.read_bytes()
    document = parse_json(contents, "the supplied credential file")
    if not isinstance(document, dict) or "credentials" not in document:
        raise ExportUpdateError('Expected a JSON object containing a "credentials" array.')
    supplied = index_credentials(document["credentials"], "the supplied credential file")
    files = sorted((export_dir / "credentials").glob("credentials_*.json"))
    if not files:
        raise ExportUpdateError("No credentials_*.json files found under the export directory.")
    chunks, seen, used, issues = [], set(), set(), []
    resolved = 0
    for path in files:
        if path.is_symlink():
            raise ExportUpdateError("Credential export files must not be symbolic links.")
        original = path.read_bytes()
        records = parse_json(original, path.name)
        indexed = index_credentials(records, path.name)
        duplicates = seen.intersection(indexed)
        if duplicates:
            raise ExportUpdateError(
                "Duplicate credential source IDs across export chunks: {}.".format(
                    ", ".join(str(value) for value in sorted(duplicates))
                )
            )
        seen.update(indexed)
        changed = False
        for credential_id, record in indexed.items():
            candidate = supplied.get(credential_id)
            identity = "{} | {}".format(credential_id, record["name"])
            if candidate is not None and candidate["name"] != record["name"]:
                issues.append(f"{identity}: supplied name does not match this source ID")
                continue
            for field_path in list(encrypted_paths(record["inputs"])):
                replacement = replacement_at(candidate["inputs"], field_path) if candidate else None
                if (
                    not isinstance(replacement, str)
                    or not replacement
                    or replacement == "$encrypted$"
                ):
                    issues.append(f"{identity}: no real value for {field_name(field_path)}")
                    continue
                target = record["inputs"]
                for part in field_path[:-1]:
                    target = target[part]
                target[field_path[-1]] = replacement
                print(f"Matched {identity}: {field_name(field_path)}")
                used.add(credential_id)
                resolved += 1
                changed = True
        if changed:
            chunks.append((path, original, records))
    print(
        f"Resolved fields: {resolved}. Changed chunks: {len(chunks)}. "
        f"Supplied credentials unused for replacements: {len(set(supplied) - used)}."
    )
    if issues:
        for issue in issues:
            print(f"Unresolved: {issue}", file=sys.stderr)
        raise ExportUpdateError("Validation failed; no export files were changed.")
    if not apply:
        print("Preview only; no files changed. Rerun with --apply to write replacements.")
    elif chunks:
        apply_bulk(chunks)
        print("All credential input placeholders resolved. No AAP changes were made.")
        print("Next: transform, then first-time Phase 1 import.")
    else:
        print("No credential input placeholders remain; no files changed.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--credentials-file",
        help='Bulk JSON file containing {"credentials": [...]} (- reads stdin); preview by default',
    )
    parser.add_argument("--apply", action="store_true", help="Write validated bulk replacements")
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
    if args.credentials_file is not None and args.credential_id is not None:
        parser.error("--credentials-file cannot be combined with --credential-id")
    if args.apply and args.credentials_file is None:
        parser.error("--apply requires --credentials-file")
    try:
        if args.credentials_file is not None:
            return bulk_update(args.credentials_file, args.export_dir, args.apply)
        path, original, records, credential = find_credential(args.export_dir, args.credential_id)
        print(
            "Selected: {} (source ID {})".format(
                credential.get("name", "unnamed"), credential["id"]
            )
        )
        credential["inputs"]["password"] = read_password()
        write_credential(path, original, records)
    except (ExportUpdateError, OSError) as error:
        detail = (
            (error.strerror or "filesystem operation failed")
            if isinstance(error, OSError)
            else str(error)
        )
        print(f"Error: {detail}", file=sys.stderr)
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
