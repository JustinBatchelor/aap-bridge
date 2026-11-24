"""Offline project repair keeps secrets/state intact and is Python 3.6 compatible."""

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "tools/repair_deferred_projects.py"
SPEC = importlib.util.spec_from_file_location("project_repair", str(SCRIPT))
repairer = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SCRIPT.parent))
try:
    SPEC.loader.exec_module(repairer)
finally:
    sys.path.pop(0)


class DeferredProjectRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.projects = self.directory / "projects"
        self.projects.mkdir()
        self.first = self.projects / "projects_0001.json"
        self.second = self.projects / "projects_0002.json"
        self.record = {
            "_source_id": 330,
            "name": "synthetic-project",
            "organization": 4,
            "scm_type": "",
            "scm_url": "",
            "scm_track_submodules": True,
            "_deferred_scm_details": {
                "scm_type": "git",
                "scm_url": "https://example.invalid/repo.git",
                "credential": 7,
            },
        }
        self.first.write_text(json.dumps([self.record]))
        other = dict(self.record, _source_id=318, scm_track_submodules=False)
        self.second.write_text(json.dumps([other]))
        self.credentials = self.directory / "credentials"
        self.credentials.mkdir()
        self.secret_file = self.credentials / "credentials_0001.json"
        self.secret_file.write_text('[{"inputs":{"password":"synthetic-secret-preserve"}}]')
        self.metadata = self.directory / "metadata.json"
        self.metadata.write_text('{"keep":"metadata"}')
        self.original = {p.name: p.read_bytes() for p in self.projects.iterdir()}

    def invoke(self, apply=True):
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            args = ["--transform-dir", str(self.directory)]
            if apply:
                args.append("--apply")
            status = repairer.main(args)
        self.output = output.getvalue() + errors.getvalue()
        self.assertNotIn("synthetic-secret-preserve", self.output)
        self.assertNotIn("https://example.invalid/repo.git", self.output)
        return status

    def assert_unchanged(self):
        self.assertEqual({p.name: p.read_bytes() for p in self.projects.iterdir()}, self.original)

    def test_preview_is_read_only(self):
        self.assertEqual(self.invoke(apply=False), 0)
        self.assert_unchanged()
        self.assertIn("preserved enabled submodule settings 1", self.output)

    def test_apply_moves_true_and_false_options_without_changing_other_data(self):
        self.assertEqual(self.invoke(), 0)
        for path, expected_value in ((self.first, True), (self.second, False)):
            actual = json.loads(path.read_text())[0]
            expected = json.loads(self.original[path.name].decode())[0]
            expected["_deferred_scm_details"]["scm_track_submodules"] = expected.pop(
                "scm_track_submodules"
            )
            self.assertEqual(actual, expected)
            self.assertIs(actual["_deferred_scm_details"]["scm_track_submodules"], expected_value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            self.secret_file.read_text(), '[{"inputs":{"password":"synthetic-secret-preserve"}}]'
        )
        self.assertEqual(self.metadata.read_text(), '{"keep":"metadata"}')
        self.assertEqual(len(list(self.projects.iterdir())), 2)

    def test_rerun_does_not_change_repaired_chunks(self):
        self.assertEqual(self.invoke(), 0)
        repaired = {p.name: p.read_bytes() for p in self.projects.iterdir()}
        self.assertEqual(self.invoke(), 0)
        self.assertEqual({p.name: p.read_bytes() for p in self.projects.iterdir()}, repaired)
        self.assertIn("No repair needed", self.output)

    def test_missing_option_defaults_false_and_original_manual_projects_are_preserved(self):
        self.record.pop("scm_track_submodules")
        self.first.write_text(
            json.dumps([self.record, {"_source_id": 5, "scm_type": "", "scm_url": ""}])
        )
        self.assertEqual(self.invoke(), 0)
        records = json.loads(self.first.read_text())
        self.assertIs(records[0]["_deferred_scm_details"]["scm_track_submodules"], False)
        self.assertEqual(records[1], {"_source_id": 5, "scm_type": "", "scm_url": ""})

    def test_active_scm_url_type_credential_or_update_option_blocks_all_writes(self):
        for field, value in (
            ("scm_type", "git"),
            ("scm_url", "https://example.invalid/active.git"),
            ("credential", 7),
            ("scm_clean", True),
            ("scm_delete_on_update", True),
            ("scm_update_on_launch", True),
        ):
            with self.subTest(field=field):
                record = dict(self.record, _source_id=318)
                record[field] = value
                self.second.write_text(json.dumps([record]))
                before = self.second.read_bytes()
                self.assertEqual(self.invoke(), 1)
                self.assertEqual(self.first.read_bytes(), self.original[self.first.name])
                self.assertEqual(self.second.read_bytes(), before)

    def test_invalid_or_conflicting_submodule_values_are_refused(self):
        for value in ("true", None, 1):
            with self.subTest(value_type=type(value).__name__):
                record = dict(self.record, scm_track_submodules=value)
                self.first.write_text(json.dumps([record]))
                before = self.first.read_bytes()
                self.assertEqual(self.invoke(), 1)
                self.assertEqual(self.first.read_bytes(), before)
                self.assertEqual(self.second.read_bytes(), self.original[self.second.name])
        self.record["_deferred_scm_details"]["scm_track_submodules"] = False
        self.first.write_text(json.dumps([self.record]))
        self.assertEqual(self.invoke(), 1)
        self.assertIn("conflicting", self.output)

    def test_true_option_without_deferred_metadata_is_refused(self):
        self.record.pop("_deferred_scm_details")
        self.first.write_text(json.dumps([self.record]))
        self.assertEqual(self.invoke(), 1)
        self.assertIn("cannot preserve", self.output)

    def test_duplicate_source_ids_are_refused(self):
        self.second.write_text(self.first.read_text())
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.first.read_bytes(), self.original[self.first.name])

    def test_invalid_deferred_metadata_is_refused(self):
        for deferred in (None, [], {}, {"scm_type": "unsupported"}):
            with self.subTest(deferred_type=type(deferred).__name__):
                self.record["_deferred_scm_details"] = deferred
                self.first.write_text(json.dumps([self.record]))
                before = self.first.read_bytes()
                self.assertEqual(self.invoke(), 1)
                self.assertEqual(self.first.read_bytes(), before)
                self.assertEqual(self.second.read_bytes(), self.original[self.second.name])

    def test_malformed_chunks_and_duplicate_json_keys_are_refused(self):
        for contents in ("not JSON", "{}", "[123]", '[{"_source_id":1,"_source_id":2}]'):
            with self.subTest(length=len(contents)):
                self.second.write_text(contents)
                self.assertEqual(self.invoke(), 1)
                self.assertEqual(self.first.read_bytes(), self.original[self.first.name])
                self.assertEqual(self.second.read_text(), contents)

    def test_missing_empty_and_symlinked_chunks_are_refused(self):
        self.second.unlink()
        self.first.unlink()
        self.assertEqual(self.invoke(), 1)
        self.first.write_text("[]")
        self.assertEqual(self.invoke(), 1)
        self.second.symlink_to(self.first)
        self.assertEqual(self.invoke(), 1)

    def test_replace_failure_preserves_originals_and_removes_staging(self):
        with patch.object(
            repairer.apply_bulk.__globals__["os"],
            "replace",
            side_effect=OSError("synthetic failure"),
        ):
            self.assertEqual(self.invoke(), 1)
        self.assert_unchanged()

    def test_real_command_preview_then_apply(self):
        for apply in (False, True):
            with self.subTest(apply=apply):
                result = subprocess.run(  # noqa: UP022 - Python 3.6 compatibility
                    [sys.executable, str(SCRIPT), "--transform-dir", str(self.directory)]
                    + (["--apply"] if apply else []),
                    universal_newlines=True,  # noqa: UP021 - Python 3.6 compatibility
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("synthetic-secret-preserve", result.stdout + result.stderr)
                if not apply:
                    self.assert_unchanged()


if __name__ == "__main__":
    unittest.main()
