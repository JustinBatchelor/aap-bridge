#!/usr/bin/env python3
"""Preview or copy transformed data excluding a project and dependent automation.

Python 3.6+, standard library only, no AAP access. Keep update_exported_credential.py
beside this helper for its strict JSON parser. Originals and migration state stay intact.
"""

import argparse
import copy
import json
import os
import shutil
import sys
from pathlib import Path

from update_exported_credential import ExportUpdateError, parse_json

ALIASES = {
    "inventories": "inventory",
    "constructed_inventories": "inventory",
    "smart_inventories": "inventory",
    "inventory_groups": "groups",
    "workflow_job_template_nodes": "workflow_nodes",
    "project": "projects",
    "job_template": "job_templates",
    "workflow_job_template": "workflow_job_templates",
    "inventory_source": "inventory_sources",
    "job": "job_templates",
    "workflow_job": "workflow_job_templates",
    "project_update": "projects",
    "inventory_update": "inventory_sources",
    "inventorysource": "inventory_sources",
    "jobtemplate": "job_templates",
    "workflowjobtemplate": "workflow_job_templates",
}
UJT_TYPES = ("projects", "job_templates", "workflow_job_templates", "inventory_sources")
GRAPH_TYPES = set(UJT_TYPES) | {
    "inventory",
    "groups",
    "hosts",
    "workflow_nodes",
    "schedules",
    "jobs",
}


def canonical(value):
    if not isinstance(value, str):
        return None
    value = value.rsplit(".", 1)[-1]
    return ALIASES.get(value, value)


def identifier(value, label):
    if isinstance(value, dict):
        value = value.get("id")
    if value is None:
        return None
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if type(value) is not int or value <= 0:
        raise ExportUpdateError(f"Invalid source ID in {label}.")
    return value


def source_id(record, label):
    return identifier(record.get("_source_id", record.get("id")), label)


def load_tree(root):
    if root.is_symlink() or not root.is_dir():
        raise ExportUpdateError("Input must be a real transformed directory.")
    snapshots, chunks, records = {}, {}, {}
    for directory, directories, files in os.walk(str(root), followlinks=False):
        parent = Path(directory)
        if any((parent / name).is_symlink() for name in directories):
            raise ExportUpdateError("Symbolic links are not accepted in transformed data.")
        for name in sorted(files):
            path = parent / name
            if path.is_symlink() or not path.is_file():
                raise ExportUpdateError("Only regular files are accepted in transformed data.")
            relative = path.relative_to(root)
            snapshots[relative] = path.read_bytes()
            if (
                len(relative.parts) != 2
                or not name.startswith(parent.name + "_")
                or path.suffix != ".json"
            ):
                continue
            rows = parse_json(snapshots[relative], str(relative))
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ExportUpdateError(f"Expected resource arrays in {relative}.")
            resource_type = canonical(parent.name)
            chunks[relative] = (resource_type, rows)
            for row in rows:
                if resource_type not in GRAPH_TYPES:
                    continue
                sid = source_id(row, str(relative))
                if sid is None:
                    raise ExportUpdateError(f"Missing source ID in {relative}.")
                key = (resource_type, sid)
                if key in records:
                    raise ExportUpdateError(f"Duplicate source ID for {resource_type}: {sid}.")
                records[key] = row
    metadata = parse_json(snapshots.get(Path("metadata.json"), b"null"), "metadata.json")
    if not isinstance(metadata, dict) or "transform_timestamp" not in metadata:
        raise ExportUpdateError("Input must contain transformed metadata.json, not raw exports.")
    if not isinstance(metadata.get("resource_types"), dict):
        raise ExportUpdateError("Transformed metadata must contain resource_types.")
    if metadata.get("excluded_project_source_ids"):
        raise ExportUpdateError(
            "Input is already filtered; use the original transformed directory."
        )
    return snapshots, chunks, records, metadata


def reference(resource_type, value, label):
    sid = identifier(value, label)
    return (canonical(resource_type), sid) if sid is not None else None


def ujt_reference(row, records, label):
    sid = identifier(row.get("unified_job_template"), label)
    if sid is None:
        return None
    explicit = row.get("_ujt_resource_type") or row.get("unified_job_template_type")
    related = row.get("related") or {}
    if not isinstance(related, dict):
        raise ExportUpdateError(f"Invalid related object in {label}.")
    url = related.get("unified_job_template")
    if not explicit and isinstance(url, str):
        parts = [part for part in url.split("/") if part]
        if len(parts) >= 2 and parts[-1].isdigit():
            explicit = parts[-2]
    summary = row.get("summary_fields") or {}
    if not isinstance(summary, dict):
        raise ExportUpdateError(f"Invalid summary object in {label}.")
    summary = summary.get("unified_job_template") or {}
    if not isinstance(summary, dict):
        raise ExportUpdateError(f"Invalid unified template summary in {label}.")
    explicit = explicit or summary.get("type") or summary.get("unified_job_type")
    if explicit:
        return (canonical(explicit), sid)
    if row.get("_approval_template") is not None:
        return ("workflow_approval_templates", sid)
    candidates = [(kind, sid) for kind in UJT_TYPES if (kind, sid) in records]
    if len(candidates) != 1:
        raise ExportUpdateError(f"Cannot unambiguously identify unified template in {label}.")
    return candidates[0]


def node_dependencies(node, records, label):
    if not isinstance(node, dict):
        raise ExportUpdateError(f"Invalid workflow node in {label}.")
    return {
        value
        for value in (
            ujt_reference(node, records, label),
            reference("inventory", node.get("inventory"), label),
        )
        if value is not None
    }


def build_graph(records):
    graph = {key: set() for key in records}
    inventory_keys = {key for key in records if key[0] == "inventory"}
    for key, row in records.items():
        kind, sid = key
        label = f"{kind} source {sid}"
        edges = graph[key]
        fields = []
        if kind == "inventory_sources":
            fields = [("projects", "source_project"), ("inventory", "inventory")]
        elif kind in ("job_templates", "jobs"):
            fields = [("projects", "project"), ("inventory", "inventory")]
            if kind == "jobs":
                fields += [("job_templates", "job_template"), ("projects", "_project_id")]
        elif kind in ("groups", "hosts", "workflow_job_templates", "schedules"):
            fields = [("inventory", "inventory")]
        elif kind == "workflow_nodes":
            fields = [("workflow_job_templates", "workflow_job_template")]
        for target_kind, field in fields:
            target = reference(target_kind, row.get(field), label)
            if target:
                edges.add(target)
        if kind == "inventory_sources":
            parent = reference("inventory", row.get("inventory"), label)
            if parent in graph:
                # Exclude the whole inventory if any of its content sources is excluded.
                graph[parent].add(key)
        if kind == "inventory":
            inputs = row.get("input_inventories") or []
            if not isinstance(inputs, list):
                raise ExportUpdateError(f"Invalid input_inventories in {label}.")
            for value in inputs:
                target = reference("inventory", value, label)
                if target:
                    edges.add(target)
            if row.get("kind") == "smart":
                # Arbitrary host_filter expressions cannot be proven independent.
                edges.update(inventory_keys - {key})
        if kind == "workflow_job_templates":
            nodes = row.get("_workflow_job_template_nodes", row.get("nodes", [])) or []
            if not isinstance(nodes, list):
                raise ExportUpdateError(f"Invalid workflow node list in {label}.")
            for node in nodes:
                edges.update(node_dependencies(node, records, label))
        elif kind == "workflow_nodes":
            dependencies = node_dependencies(row, records, label)
            edges.update(dependencies)
            parent = reference("workflow_job_templates", row.get("workflow_job_template"), label)
            if parent in graph:
                graph[parent].update(dependencies)
        elif kind == "schedules":
            target = ujt_reference(row, records, label)
            if target:
                edges.add(target)
    return graph


def grant_reference(grant, excluded):
    if not isinstance(grant, dict):
        raise ExportUpdateError("Invalid role grant object.")
    kind = canonical(
        grant.get("content_resource_type")
        or grant.get("resource_type")
        or grant.get("content_type")
    )
    value = grant.get("content_source_id", grant.get("resource_id", grant.get("object_id")))
    if value is None:
        return False
    sid = identifier(value, "role grant")
    if kind:
        return (kind, sid) in excluded
    # An untyped grant with a matching ID cannot be proven unrelated.
    return any(excluded_id == sid for _, excluded_id in excluded)


def prepare(root, project_name, project_id):
    snapshots, chunks, records, metadata = load_tree(root)
    projects = [(key, row) for key, row in records.items() if key[0] == "projects"]
    matches = [
        (key, row)
        for key, row in projects
        if (key[1] == project_id if project_id is not None else row.get("name") == project_name)
    ]
    if len(matches) != 1:
        raise ExportUpdateError(
            f"Expected one matching project; found {len(matches)}. Use its source ID if names repeat."
        )
    selected, selected_row = matches[0]
    graph = build_graph(records)
    excluded = {selected}
    reasons = {selected: "selected project"}
    while True:
        additions = {
            key: sorted(edges & excluded)
            for key, edges in graph.items()
            if key not in excluded and edges & excluded
        }
        if not additions:
            break
        for key, dependencies in additions.items():
            excluded.add(key)
            reasons[key] = "depends on " + ", ".join(f"{kind}:{sid}" for kind, sid in dependencies)
    report = {
        "project": {"source_id": selected[1], "name": selected_row.get("name")},
        "excluded": [
            {
                "resource_type": kind,
                "source_id": sid,
                "name": records[(kind, sid)].get("name", records[(kind, sid)].get("identifier")),
                "reason": reasons[(kind, sid)],
            }
            for kind, sid in sorted(excluded)
        ],
        "removed_role_grants": 0,
        "policy": "Exclude whole dependent inventories/workflows; smart inventories conservatively depend on all exported inventories. Static references only; custom variables and external automation require review.",
        "target_changes": "None. Existing target objects, jobs, and migration mappings are not modified.",
    }
    updated, counts, removed, file_counts = {}, {}, {}, {}
    for path, (kind, rows) in chunks.items():
        kept = []
        changed = False
        for row in rows:
            sid = source_id(row, str(path)) if kind in GRAPH_TYPES else None
            excluded_grant = kind in (
                "rbac",
                "role_user_assignments",
                "role_team_assignments",
            ) and grant_reference(row, excluded)
            if (kind, sid) in excluded or excluded_grant:
                changed = True
                removed[kind] = removed.get(kind, 0) + 1
                if excluded_grant:
                    report["excluded"].append(
                        {
                            "resource_type": kind,
                            "source_id": source_id(row, "role assignment"),
                            "name": row.get("name", row.get("content_object_name")),
                            "reason": "role assignment for an excluded resource",
                        }
                    )
                continue
            row_copy = copy.deepcopy(row)
            for field in ("_user_role_grants", "_team_role_grants"):
                if field not in row_copy:
                    continue
                grants = row_copy[field]
                if not isinstance(grants, list):
                    raise ExportUpdateError("Expected a list of principal role grants.")
                filtered = [grant for grant in grants if not grant_reference(grant, excluded)]
                dropped = len(grants) - len(filtered)
                if dropped:
                    row_copy[field] = filtered
                    report["removed_role_grants"] += dropped
                    changed = True
            kept.append(row_copy)
        counts[kind] = counts.get(kind, 0) + len(kept)
        file_counts[kind] = file_counts.get(kind, 0) + 1
        if changed:
            updated[path] = (json.dumps(kept, indent=2) + "\n").encode("utf-8")
    for kind, stats in metadata["resource_types"].items():
        if not isinstance(stats, dict):
            raise ExportUpdateError("Invalid resource metadata.")
        normalized = canonical(kind)
        if normalized in counts:
            stats["transformed_count"] = counts[normalized]
            stats["files"] = file_counts[normalized]
            stats["project_excluded_count"] = removed.get(normalized, 0)
            if "count" in stats:
                stats["count"] = counts[normalized]
    metadata["total_resources"] = sum(counts.values())
    metadata["excluded_project_source_ids"] = [selected[1]]
    updated[Path("metadata.json")] = (json.dumps(metadata, indent=2) + "\n").encode("utf-8")
    report["removed_counts"] = removed
    return snapshots, updated, report


def write_copy(root, output, snapshots, updated, report):
    if output.exists() or output.is_symlink():
        raise ExportUpdateError(
            "Output already exists; choose a new path. Nothing was overwritten."
        )
    # Reserve the new output exclusively; never merge with an old migration copy.
    output.mkdir(mode=0o700)
    try:
        for relative, original in snapshots.items():
            if (root / relative).is_symlink() or (root / relative).read_bytes() != original:
                raise ExportUpdateError(
                    "Input changed during filtering; stop other writers and retry."
                )
            path = output / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open("xb") as stream:
                os.chmod(str(path), 0o600)
                stream.write(updated.get(relative, original))
        report_path = output / "project-exclusion-report.json"
        with report_path.open("x", encoding="utf-8") as stream:
            os.chmod(str(report_path), 0o600)
            json.dump(report, stream, indent=2)
            stream.write("\n")
        if set(snapshots) != {path.relative_to(root) for path in root.rglob("*") if path.is_file()}:
            raise ExportUpdateError("Input file inventory changed during filtering.")
        for relative, original in snapshots.items():
            if (root / relative).is_symlink() or (root / relative).read_bytes() != original:
                raise ExportUpdateError(
                    "Input changed during filtering; stop other writers and retry."
                )
    except BaseException:
        shutil.rmtree(str(output))
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("/data/xformed"))
    parser.add_argument("--output", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--project-name")
    selection.add_argument("--project-id", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.input.is_symlink():
            raise ExportUpdateError("Input must not be a symbolic link.")
        root, output = args.input.resolve(), args.output.resolve()
        if root == output or root in output.parents or output in root.parents:
            raise ExportUpdateError(
                "Input and output must be separate, non-overlapping directories."
            )
        if output.exists() or args.output.is_symlink():
            raise ExportUpdateError("Output already exists; choose a new path.")
        snapshots, updated, report = prepare(root, args.project_name, args.project_id)
        for item in report["excluded"]:
            print(
                "Exclude {} source {} | {} | {}".format(
                    item["resource_type"],
                    item["source_id"],
                    item["name"] or "unnamed",
                    item["reason"],
                )
            )
        print("Removed counts: " + json.dumps(report["removed_counts"], sort_keys=True))
        print("Removed principal role grants: {}".format(report["removed_role_grants"]))
        print(report["policy"])
        if args.apply:
            write_copy(root, output, snapshots, updated, report)
            print(
                f"Filtered copy created: {output}. Original files, AAP objects, and migration state unchanged."
            )
        else:
            print(
                "Preview only; no files or AAP objects changed. Review every exclusion before --apply."
            )
        return 0
    except (ExportUpdateError, OSError) as error:
        detail = (
            (error.strerror or "filesystem operation failed")
            if isinstance(error, OSError)
            else str(error)
        )
        print(f"Error: {detail}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled; original files unchanged.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
