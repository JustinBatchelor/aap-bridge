"""Project exclusion filters a private copy without AAP access or state resets."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "tools/exclude_transformed_project.py"
SPEC = importlib.util.spec_from_file_location("project_exclusion", str(SCRIPT))
excluder = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SCRIPT.parent))
try:
    SPEC.loader.exec_module(excluder)
finally:
    sys.path.pop(0)


def row(sid, name=None, **fields):
    return dict({"_source_id": sid, "name": name or f"object-{sid}"}, **fields)


class ProjectExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "xformed"
        self.root.mkdir()
        self.output = self.base / "without-ado"
        self.data = {
            "projects": [
                row(
                    10, "ADO", _deferred_scm_details={"scm_url": "https://example.invalid/private"}
                ),
                row(11, "ADO version"),
                row(12, "Safe"),
            ],
            "inventory": [
                row(100, kind=""),
                row(101, kind=""),
                row(102, kind="constructed", input_inventories=[100]),
                row(103, kind="smart", host_filter="name__icontains=example"),
                row(104, kind="constructed", input_inventories=[101]),
            ],
            "inventory_sources": [
                row(20, source_project=10, inventory=100),
                row(21, source_project=12, inventory=100),
                row(22, source_project=12, inventory=101),
            ],
            "job_templates": [
                row(30, project=10, inventory=101),
                row(31, project=12, inventory=100),
                row(32, project=12, inventory=101),
                row(33, project=11, inventory=101),
            ],
            "workflow_job_templates": [
                row(
                    40,
                    _workflow_job_template_nodes=[
                        {"unified_job_template": 30},
                        {"unified_job_template": 32},
                    ],
                ),
                row(41, _workflow_job_template_nodes=[{"unified_job_template": 40}]),
                row(42, _workflow_job_template_nodes=[{"unified_job_template": 32}]),
                row(43, inventory=100),
                row(44),
                row(
                    45,
                    _workflow_job_template_nodes=[
                        {"unified_job_template": 999, "_approval_template": {"name": "approve"}}
                    ],
                ),
                row(
                    46,
                    _workflow_job_template_nodes=[
                        {"unified_job_template": 10, "_ujt_resource_type": "projects"}
                    ],
                ),
                row(
                    47,
                    _workflow_job_template_nodes=[{"unified_job_template": 32, "inventory": 100}],
                ),
            ],
            "workflow_nodes": [
                row(50, workflow_job_template=44, unified_job_template=30),
                row(51, workflow_job_template=44, unified_job_template=32),
            ],
            "schedules": [
                row(60, unified_job_template=10, _ujt_resource_type="projects"),
                row(61, unified_job_template=30, _ujt_resource_type="job_templates"),
                row(62, unified_job_template=41, _ujt_resource_type="workflow_job_templates"),
                row(63, unified_job_template=20, _ujt_resource_type="inventory_sources"),
                row(64, unified_job_template=32, _ujt_resource_type="job_templates", enabled=False),
                row(65, unified_job_template=22, _ujt_resource_type="inventory_sources"),
                row(
                    66,
                    unified_job_template=42,
                    _ujt_resource_type="workflow_job_templates",
                    inventory=100,
                ),
            ],
            "groups": [row(70, inventory=100), row(71, inventory=101)],
            "hosts": [row(80, inventory=100), row(81, inventory=101)],
            "credentials": [row(7, inputs={"password": "synthetic-secret-never-log"})],
            "organizations": [row(1)],
            "users": [
                row(
                    90,
                    _user_role_grants=[
                        {"content_resource_type": "projects", "content_source_id": 10},
                        {"content_resource_type": "job_templates", "content_source_id": 32},
                    ],
                )
            ],
            "teams": [
                row(
                    91,
                    _team_role_grants=[
                        {
                            "content_resource_type": "workflow_job_templates",
                            "content_source_id": 41,
                        },
                        {"content_resource_type": "projects", "content_source_id": 12},
                    ],
                )
            ],
            "role_user_assignments": [
                row(92, content_type="awx.project", object_id=10),
                row(93, content_type="awx.jobtemplate", object_id=32),
            ],
            "role_team_assignments": [
                row(94, content_type="awx.workflowjobtemplate", object_id=41)
            ],
            "rbac": [
                {"resource_type": "projects", "resource_id": 10, "user": 90},
                {"resource_type": "projects", "resource_id": 12, "user": 90},
            ],
        }
        self.write_tree()
        self.original = self.snapshot()

    def write_tree(self):
        for kind, rows in self.data.items():
            directory = self.root / kind
            directory.mkdir(exist_ok=True)
            (directory / (kind + "_0001.json")).write_text(json.dumps(rows))
        metadata = {
            "transform_timestamp": "synthetic",
            "source_version": "2.4",
            "target_version": "2.7",
            "resource_types": {
                kind: {
                    "count": len(rows),
                    "transformed_count": len(rows),
                    "input_count": len(rows),
                    "files": 1,
                }
                for kind, rows in self.data.items()
            },
        }
        (self.root / "metadata.json").write_text(json.dumps(metadata))

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def invoke(self, apply=True, selection=None):
        out, err = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            args = ["--input", str(self.root), "--output", str(self.output)] + (
                selection or ["--project-name", "ADO"]
            )
            if apply:
                args.append("--apply")
            status = excluder.main(args)
        self.text = out.getvalue() + err.getvalue()
        self.assertNotIn("synthetic-secret-never-log", self.text)
        self.assertNotIn("https://example.invalid/private", self.text)
        return status

    def kept(self, kind):
        return json.loads((self.output / kind / (kind + "_0001.json")).read_text())

    def ids(self, kind):
        return {r["_source_id"] for r in self.kept(kind)}

    def test_preview_reports_dependencies_without_creating_output(self):
        self.assertEqual(self.invoke(apply=False), 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.snapshot(), self.original)
        for kind, sid in (
            ("projects", 10),
            ("job_templates", 30),
            ("inventory", 100),
            ("workflow_job_templates", 41),
            ("schedules", 62),
        ):
            self.assertIn(f"Exclude {kind} source {sid}", self.text)

    def test_filtered_copy_excludes_transitive_automation_but_preserves_shared_resources(self):
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.ids("projects"), {11, 12})
        self.assertEqual(self.ids("inventory"), {101, 104})
        self.assertEqual(self.ids("inventory_sources"), {22})
        self.assertEqual(self.ids("job_templates"), {32, 33})
        self.assertEqual(self.ids("workflow_job_templates"), {42, 45})
        self.assertEqual(self.ids("workflow_nodes"), set())
        self.assertEqual(self.ids("schedules"), {64, 65})
        self.assertEqual(self.ids("groups"), {71})
        self.assertEqual(self.ids("hosts"), {81})
        self.assertEqual(self.ids("role_user_assignments"), {93})
        self.assertEqual(self.ids("role_team_assignments"), set())
        self.assertEqual(len(self.kept("rbac")), 1)
        self.assertEqual(
            self.kept("users")[0]["_user_role_grants"],
            [{"content_resource_type": "job_templates", "content_source_id": 32}],
        )
        self.assertEqual(
            self.kept("teams")[0]["_team_role_grants"],
            [{"content_resource_type": "projects", "content_source_id": 12}],
        )
        for kind in ("credentials", "organizations"):
            self.assertEqual(
                (self.output / kind / (kind + "_0001.json")).read_bytes(),
                self.original[kind + "/" + kind + "_0001.json"],
            )
        self.assertFalse(self.kept("schedules")[0]["enabled"])
        self.assertEqual(self.snapshot(), self.original)

    def test_private_output_and_metadata_counts(self):
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for path in self.output.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        metadata = json.loads((self.output / "metadata.json").read_text())
        self.assertEqual(metadata["excluded_project_source_ids"], [10])
        self.assertEqual(metadata["resource_types"]["projects"]["count"], 2)
        self.assertEqual(metadata["resource_types"]["job_templates"]["transformed_count"], 2)
        self.assertEqual(metadata["resource_types"]["job_templates"]["input_count"], 4)
        report = json.loads((self.output / "project-exclusion-report.json").read_text())
        self.assertEqual(report["removed_role_grants"], 2)
        self.assertEqual(report["project"]["name"], "ADO")
        self.assertEqual(report["removed_counts"]["workflow_job_templates"], 6)

    def test_exact_name_does_not_exclude_ado_version(self):
        self.assertEqual(self.invoke(selection=["--project-id", "10"]), 0)
        self.assertIn(11, self.ids("projects"))

    def test_ambiguous_or_missing_name_refuses_all_output(self):
        self.data["projects"][1]["name"] = "ADO"
        self.write_tree()
        self.assertEqual(self.invoke(), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.invoke(selection=["--project-name", "missing"]), 1)
        self.assertFalse(self.output.exists())

    def test_existing_output_is_preserved(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("preserve")
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(marker.read_text(), "preserve")
        self.assertEqual(self.snapshot(), self.original)

    def test_overlapping_input_output_is_refused(self):
        for output in (self.root, self.root / "nested", self.base):
            with self.subTest(path=str(output)):
                self.output = output
                self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.snapshot(), self.original)

    def test_source_symlink_or_invalid_json_refuses_output(self):
        path = self.root / "projects/projects_0001.json"
        for contents in ("not JSON", "{}", '[{"id":1,"id":2}]'):
            with self.subTest(length=len(contents)):
                path.write_text(contents)
                self.assertEqual(self.invoke(), 1)
                self.assertFalse(self.output.exists())
        path.unlink()
        path.symlink_to(self.root / "metadata.json")
        self.assertEqual(self.invoke(), 1)
        self.assertFalse(self.output.exists())

    def test_untyped_ambiguous_workflow_node_refuses_output(self):
        self.data["job_templates"].append(row(10, project=12, inventory=101))
        self.data["workflow_job_templates"][0]["_workflow_job_template_nodes"][0][
            "unified_job_template"
        ] = 10
        self.write_tree()
        self.assertEqual(self.invoke(), 1)
        self.assertIn("unambiguously", self.text)
        self.assertFalse(self.output.exists())

    def test_explicit_workflow_reference_resolves_overlapping_ids(self):
        self.data["job_templates"].append(row(10, project=12, inventory=101))
        self.data["workflow_job_templates"][0]["_workflow_job_template_nodes"][0] = {
            "unified_job_template": 10,
            "related": {"unified_job_template": "/api/v2/projects/10/"},
        }
        self.write_tree()
        self.assertEqual(self.invoke(), 0)
        self.assertNotIn(40, self.ids("workflow_job_templates"))

    def test_unrelated_workflow_cycle_is_preserved(self):
        self.data["workflow_job_templates"] += [
            row(200, _workflow_job_template_nodes=[{"unified_job_template": 201}]),
            row(201, _workflow_job_template_nodes=[{"unified_job_template": 200}]),
        ]
        self.write_tree()
        self.assertEqual(self.invoke(), 0)
        self.assertTrue({200, 201}.issubset(self.ids("workflow_job_templates")))

    def test_raw_or_already_filtered_metadata_is_refused(self):
        metadata = self.root / "metadata.json"
        metadata.write_text('{"resource_types": {}}')
        self.assertEqual(self.invoke(), 1)
        self.assertFalse(self.output.exists())
        self.write_tree()
        self.assertEqual(self.invoke(), 0)
        self.root = self.output
        self.output = self.base / "second-copy"
        self.assertEqual(self.invoke(), 1)
        self.assertIn("already filtered", self.text)

    def test_write_failure_cleans_only_new_output(self):
        with patch.object(excluder.os, "chmod", side_effect=OSError("synthetic failure")):
            self.assertEqual(self.invoke(), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.snapshot(), self.original)

    def test_input_change_during_copy_aborts_and_cleans_output(self):
        real_chmod = os.chmod
        changed = []
        original_path = self.root / "projects/projects_0001.json"

        def mutate_input(*args, **kwargs):
            if not changed:
                changed.append(True)
                original_path.write_text("concurrent update")
            return real_chmod(*args, **kwargs)

        with patch.object(excluder.os, "chmod", side_effect=mutate_input):
            self.assertEqual(self.invoke(), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(original_path.read_text(), "concurrent update")

    def test_real_cli_preview_and_apply(self):
        command = [
            sys.executable,
            str(SCRIPT),
            "--input",
            str(self.root),
            "--output",
            str(self.output),
            "--project-name",
            "ADO",
        ]
        for apply in (False, True):
            with self.subTest(apply=apply):
                result = subprocess.run(  # noqa: UP022 - Python 3.6 compatibility
                    command + (["--apply"] if apply else []),
                    universal_newlines=True,  # noqa: UP021 - Python 3.6 compatibility
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("synthetic-secret-never-log", result.stdout + result.stderr)
                self.assertEqual(self.output.exists(), apply)


if __name__ == "__main__":
    unittest.main()
