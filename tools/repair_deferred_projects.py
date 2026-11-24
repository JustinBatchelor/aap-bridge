#!/usr/bin/env python3
"""Repair legacy transformed Manual projects without contacting AAP (Python 3.6+).

Preview by default. Keep update_exported_credential.py beside this script for its
shared strict JSON reader and atomic private-file writer.
"""

import argparse
import sys
from pathlib import Path

from update_exported_credential import ExportUpdateError, apply_bulk, parse_json


def repair(transform_dir, apply=False):
    files = sorted((transform_dir / "projects").glob("projects_*.json"))
    if not files:
        raise ExportUpdateError("No transformed projects_*.json files found.")
    chunks, seen = [], set()
    checked = deferred_count = preserved_true = 0
    for path in files:
        if path.is_symlink():
            raise ExportUpdateError("Project chunks must not be symbolic links.")
        original = path.read_bytes()
        records = parse_json(original, path.name)
        if not isinstance(records, list):
            raise ExportUpdateError("Expected an array of transformed project objects.")
        changed = False
        for project in records:
            if not isinstance(project, dict):
                raise ExportUpdateError("Expected transformed project objects.")
            source_id = project.get("_source_id", project.get("id"))
            if type(source_id) is not int or source_id <= 0 or source_id in seen:
                raise ExportUpdateError("Missing, invalid, or duplicated project source ID.")
            seen.add(source_id)
            checked += 1
            # Refuse active SCM payloads; this helper must never prepare a live SCM create.
            for field in ("scm_type", "scm_url", "credential"):
                if project.get(field) not in (None, ""):
                    raise ExportUpdateError(
                        f"Source {source_id}: active {field}; no files changed."
                    )
            for field in ("scm_clean", "scm_delete_on_update", "scm_update_on_launch"):
                if project.get(field):
                    raise ExportUpdateError(
                        f"Source {source_id}: active {field}; no files changed."
                    )
            if "_deferred_scm_details" not in project:
                if project.get("scm_track_submodules"):
                    raise ExportUpdateError(
                        f"Source {source_id}: no deferred SCM details; cannot preserve submodule setting."
                    )
                continue
            deferred = project["_deferred_scm_details"]
            if not isinstance(deferred, dict) or deferred.get("scm_type") not in (
                "git",
                "svn",
                "archive",
            ):
                raise ExportUpdateError(f"Source {source_id}: invalid deferred SCM details.")
            deferred_count += 1
            original_value = deferred.get(
                "scm_track_submodules", project.get("scm_track_submodules", False)
            )
            if type(original_value) is not bool:
                raise ExportUpdateError(f"Source {source_id}: submodule setting must be boolean.")
            if "scm_track_submodules" in project:
                active = project["scm_track_submodules"]
                if type(active) is not bool or (active and not original_value):
                    raise ExportUpdateError(f"Source {source_id}: conflicting submodule settings.")
            preserved_true += int(original_value)
            if "scm_track_submodules" not in deferred or "scm_track_submodules" in project:
                deferred["scm_track_submodules"] = original_value
                project.pop("scm_track_submodules", None)
                changed = True
                print(
                    f"Repair source {source_id}: defer submodule setting; Manual payload stays inactive."
                )
        if changed:
            chunks.append((path, original, records))
    if not checked:
        raise ExportUpdateError("No transformed projects found.")
    print(
        f"Checked {checked} projects; deferred {deferred_count}; "
        f"preserved enabled submodule settings {preserved_true}; changed chunks {len(chunks)}."
    )
    if not apply:
        print("Preview only; no files changed. Rerun with --apply after reviewing.")
    elif chunks:
        apply_bulk(chunks)
        print(
            "Repaired transformed projects only. No credentials, migration state, or AAP objects changed."
        )
    else:
        print("No repair needed; no files changed.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transform-dir", type=Path, default=Path("/data/xformed"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        return repair(args.transform_dir, args.apply)
    except (ExportUpdateError, OSError) as error:
        detail = (
            (error.strerror or "filesystem operation failed")
            if isinstance(error, OSError)
            else str(error)
        )
        print(f"Error: {detail}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled before writing.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
