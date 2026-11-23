"""Bulk credential replacement is offline, strict, and compatible with Python 3.6+."""

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

SCRIPT = Path(__file__).resolve().parents[2] / "tools/update_exported_credential.py"
SPEC = importlib.util.spec_from_file_location("bulk_export_editor", str(SCRIPT))
editor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(editor)


class BulkExportCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.exports = self.directory / "exports"
        self.credentials = self.exports / "credentials"
        self.credentials.mkdir(parents=True)
        self.first = self.credentials / "credentials_0001.json"
        self.second = self.credentials / "credentials_0002.json"
        self.source = self.directory / "plaintext.json"
        self.password = "synthetic-password-value"
        self.token = "synthetic-token-value"
        self.ssh_key = "synthetic-key-first-line\nsecond-line\n"
        self.first.write_text(
            json.dumps(
                [
                    {
                        "id": 1,
                        "name": "Repo",
                        "organization": 42,
                        "credential_type": 2,
                        "inputs": {"username": "preserve-me", "password": "$encrypted$"},
                    },
                    {"id": 2, "name": "No secrets", "inputs": {"password": "existing-value"}},
                ]
            )
        )
        self.second.write_text(
            json.dumps(
                [
                    {
                        "id": 3,
                        "name": "Machine",
                        "inputs": {
                            "ssh_key_data": "$encrypted$",
                            "ssh_key_unlock": "$encrypted$",
                            "nested": [{"token": "$encrypted$"}],
                        },
                    }
                ]
            )
        )
        self.document = {
            "credentials": [
                {
                    "id": 1,
                    "name": "Repo",
                    "organization": "Different representation",
                    "credential_type": {"name": "Source Control", "kind": "scm"},
                    "inputs": {"username": "do-not-copy", "password": self.password},
                },
                {
                    "id": 3,
                    "name": "Machine",
                    "inputs": {
                        "ssh_key_data": self.ssh_key,
                        "ssh_key_unlock": self.password,
                        "nested": [{"token": self.token}],
                    },
                },
                {"id": 99, "name": "Unused", "inputs": {}},
            ]
        }
        self.save_source()
        self.original = {path.name: path.read_bytes() for path in self.credentials.iterdir()}
        self.original_source = self.source.read_bytes()

    def save_source(self):
        self.source.write_text(json.dumps(self.document))

    def invoke(self, apply=True, stdin=False):
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            if stdin:
                stack.enter_context(
                    patch.object(editor.sys, "stdin", io.StringIO(self.source.read_text()))
                )
            args = [
                "--credentials-file",
                "-" if stdin else str(self.source),
                "--export-dir",
                str(self.exports),
            ]
            if apply:
                args.append("--apply")
            status = editor.main(args)
        self.output = output.getvalue() + errors.getvalue()
        for secret in (self.password, self.token, self.ssh_key, "existing-value"):
            self.assertNotIn(secret, self.output)
        return status

    def assert_unchanged(self):
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.credentials.iterdir()}, self.original
        )

    def test_preview_is_read_only_and_shows_matches_counts_and_unused_records(self):
        self.assertEqual(self.invoke(apply=False), 0)
        self.assert_unchanged()
        self.assertIn("Resolved fields: 4", self.output)
        self.assertIn("inputs.nested[0].token", self.output)
        self.assertIn("unused for replacements: 1", self.output)
        self.assertIn("Preview only", self.output)

    def test_apply_preserves_everything_except_placeholders_in_all_chunks(self):
        expected_first = json.loads(self.original[self.first.name].decode())
        expected_second = json.loads(self.original[self.second.name].decode())
        expected_first[0]["inputs"]["password"] = self.password
        expected_second[0]["inputs"] = self.document["credentials"][1]["inputs"]
        unrelated = self.exports / "projects"
        unrelated.mkdir()
        project = unrelated / "projects_0001.json"
        project.write_text('[{"unrelated": "$encrypted$"}]')
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(json.loads(self.first.read_text()), expected_first)
        self.assertEqual(json.loads(self.second.read_text()), expected_second)
        self.assertEqual(self.source.read_bytes(), self.original_source)
        self.assertEqual(project.read_text(), '[{"unrelated": "$encrypted$"}]')
        for path in (self.first, self.second):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list(self.credentials.iterdir())), 2)

    def test_stdin_works_without_an_interactive_terminal(self):
        self.assertEqual(self.invoke(stdin=True), 0)
        self.assertEqual(json.loads(self.first.read_text())[0]["inputs"]["password"], self.password)

    def test_real_cli_stdin_preview_then_apply(self):
        command = [
            sys.executable,
            str(SCRIPT),
            "--credentials-file",
            "-",
            "--export-dir",
            str(self.exports),
        ]
        for apply in (False, True):
            with self.subTest(apply=apply):
                result = subprocess.run(  # noqa: UP022 - capture_output requires Python 3.7
                    command + (["--apply"] if apply else []),
                    input=self.source.read_text(),
                    universal_newlines=True,  # noqa: UP021 - text requires Python 3.7
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                for secret in (self.password, self.token, self.ssh_key):
                    self.assertNotIn(secret, result.stdout + result.stderr)
                if not apply:
                    self.assert_unchanged()
        self.assertEqual(json.loads(self.first.read_text())[0]["inputs"]["password"], self.password)

    def test_rerun_is_a_noop_and_does_not_rotate_plaintext_values(self):
        self.assertEqual(self.invoke(), 0)
        updated = {path.name: path.read_bytes() for path in self.credentials.iterdir()}
        self.document["credentials"][0]["inputs"]["password"] = "not-a-rotation"
        self.save_source()
        self.assertEqual(self.invoke(), 0)
        self.assertIn("No credential input placeholders remain", self.output)
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.credentials.iterdir()}, updated
        )

    def test_missing_credential_blocks_all_writes(self):
        self.document["credentials"].pop(1)
        self.save_source()
        self.assertEqual(self.invoke(), 1)
        self.assertIn("3 | Machine: no real value for inputs.ssh_key_data", self.output)
        self.assert_unchanged()

    def test_missing_empty_null_nonstring_and_encrypted_replacements_block_all_writes(self):
        for value in ("", None, "$encrypted$", 123, False, {}, []):
            with self.subTest(value_type=type(value).__name__):
                self.document["credentials"][1]["inputs"]["ssh_key_data"] = value
                self.save_source()
                self.assertEqual(self.invoke(), 1)
                self.assert_unchanged()
        del self.document["credentials"][1]["inputs"]["ssh_key_data"]
        self.save_source()
        self.assertEqual(self.invoke(), 1)
        self.assert_unchanged()

    def test_nested_shape_mismatch_blocks_all_writes(self):
        self.document["credentials"][1]["inputs"]["nested"] = {}
        self.save_source()
        self.assertEqual(self.invoke(), 1)
        self.assertIn("inputs.nested[0].token", self.output)
        self.assert_unchanged()

    def test_same_id_with_different_name_is_refused(self):
        self.document["credentials"][0]["name"] = "Wrong instance credential"
        self.save_source()
        self.assertEqual(self.invoke(), 1)
        self.assertIn("supplied name does not match", self.output)
        self.assert_unchanged()

    def test_same_name_with_different_id_is_not_used_as_fallback(self):
        self.document["credentials"][0]["id"] = 50
        self.save_source()
        self.assertEqual(self.invoke(), 1)
        self.assertIn("1 | Repo: no real value", self.output)
        self.assert_unchanged()

    def test_duplicate_supplied_ids_are_refused(self):
        self.document["credentials"].append(self.document["credentials"][0])
        self.save_source()
        self.assertEqual(self.invoke(), 1)
        self.assertIn("Duplicate credential source ID", self.output)
        self.assert_unchanged()

    def test_duplicate_names_with_distinct_ids_are_valid(self):
        self.document["credentials"][1]["name"] = "Repo"
        records = json.loads(self.second.read_text())
        records[0]["name"] = "Repo"
        self.second.write_text(json.dumps(records))
        self.save_source()
        self.assertEqual(self.invoke(), 0)

    def test_duplicate_export_ids_within_and_across_chunks_are_refused(self):
        for same_chunk in (True, False):
            with self.subTest(same_chunk=same_chunk):
                records = json.loads(self.original[self.first.name].decode())
                if same_chunk:
                    self.first.write_text(json.dumps(records + records))
                else:
                    self.first.write_bytes(self.original[self.first.name])
                    self.second.write_text(json.dumps(records))
                before = {p.name: p.read_bytes() for p in self.credentials.iterdir()}
                self.assertEqual(self.invoke(), 1)
                self.assertIn("Duplicate credential source ID", self.output)
                self.assertEqual(
                    {p.name: p.read_bytes() for p in self.credentials.iterdir()}, before
                )

    def test_malformed_source_and_duplicate_json_keys_do_not_leak_contents(self):
        for value in (
            "not JSON " + self.password,
            '{"credentials": [], "credentials": []}',
            '{"credentials": [{"id": 1, "name": "Repo", "inputs": {"password": "first", "password": "second"}}]}',
            '{"credentials": NaN}',
            '"' + self.password + '"',
            "[]",
            "{}",
            '{"credentials": {}}',
        ):
            with self.subTest(length=len(value)):
                self.source.write_text(value)
                self.assertEqual(self.invoke(), 1)
                self.assert_unchanged()

    def test_invalid_source_record_shapes_and_ids_are_refused(self):
        for value in (
            123,
            {"id": True},
            {"id": -1},
            {"id": "1"},
            {"id": 1},
            {"id": 1, "name": "Repo", "inputs": []},
        ):
            with self.subTest(value_type=type(value).__name__):
                self.document["credentials"] = [value]
                self.save_source()
                self.assertEqual(self.invoke(), 1)
                self.assert_unchanged()

    def test_malformed_export_and_duplicate_keys_are_refused_without_writes(self):
        for contents in ("{}", "not JSON", "[123]", '[{"id": 1, "id": 2}]'):
            with self.subTest(length=len(contents)):
                self.second.write_text(contents)
                self.assertEqual(self.invoke(), 1)
                self.assertEqual(self.first.read_bytes(), self.original[self.first.name])
                self.assertEqual(self.second.read_text(), contents)

    def test_symlinked_supplied_file_and_chunk_are_refused(self):
        linked = self.directory / "linked.json"
        linked.symlink_to(self.source)
        with self.assertRaises(editor.ExportUpdateError):
            editor.bulk_update(str(linked), self.exports)
        self.second.unlink()
        self.second.symlink_to(self.first)
        self.assertEqual(self.invoke(), 1)
        self.assertIn("symbolic links", self.output)
        self.assertEqual(self.first.read_bytes(), self.original[self.first.name])

    def test_missing_files_return_a_safe_error(self):
        self.source.unlink()
        self.assertEqual(self.invoke(), 1)
        self.assert_unchanged()
        self.save_source()
        self.first.unlink()
        self.second.unlink()
        self.assertEqual(self.invoke(), 1)
        self.assertIn("No credentials_*.json", self.output)

    def test_first_replacement_failure_leaves_all_files_unchanged_and_cleans_staging(self):
        with patch.object(editor.os, "replace", side_effect=OSError("synthetic failure")):
            self.assertEqual(self.invoke(), 1)
        self.assertIn("Completed chunks: none", self.output)
        self.assertIn(self.first.name, self.output)
        self.assertIn(self.second.name, self.output)
        self.assert_unchanged()

    def test_staging_failure_happens_before_any_replacement(self):
        original_temporary = editor.tempfile.NamedTemporaryFile
        calls = []

        def fail_second_stage(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                raise OSError("synthetic staging failure")
            return original_temporary(*args, **kwargs)

        with patch.object(editor.tempfile, "NamedTemporaryFile", side_effect=fail_second_stage):
            self.assertEqual(self.invoke(), 1)
        self.assertIn("Completed chunks: none", self.output)
        self.assert_unchanged()

    def test_partial_write_reports_completed_and_pending_and_reruns_safely(self):
        original_replace = os.replace
        calls = []

        def fail_second_replace(source, destination):
            calls.append(destination)
            if len(calls) == 2:
                raise OSError("synthetic failure")
            return original_replace(source, destination)

        with patch.object(editor.os, "replace", side_effect=fail_second_replace):
            self.assertEqual(self.invoke(), 1)
        self.assertIn("Completed chunks: " + self.first.name, self.output)
        self.assertIn("Pending chunks: " + self.second.name, self.output)
        self.assertEqual(self.second.read_bytes(), self.original[self.second.name])
        self.assertEqual(len(list(self.credentials.iterdir())), 2)
        self.assertEqual(self.invoke(), 0)
        self.assertIn("Resolved fields: 3", self.output)

    def test_concurrent_change_before_replacements_prevents_all_writes(self):
        original_temporary = editor.tempfile.NamedTemporaryFile
        calls = []

        def change_during_staging(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                self.second.write_text("concurrent update")
            return original_temporary(*args, **kwargs)

        with patch.object(editor.tempfile, "NamedTemporaryFile", side_effect=change_during_staging):
            self.assertEqual(self.invoke(), 1)
        self.assertIn("Completed chunks: none", self.output)
        self.assertEqual(self.first.read_bytes(), self.original[self.first.name])
        self.assertEqual(self.second.read_text(), "concurrent update")
        self.assertEqual(len(list(self.credentials.iterdir())), 2)

    def test_incompatible_options_fail_before_reading_files(self):
        for args in (["--apply"], ["--credentials-file", "missing.json", "--credential-id", "1"]):
            with self.subTest(args=args):
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        editor.main(args)
                self.assertEqual(error.exception.code, 2)
        self.assert_unchanged()


if __name__ == "__main__":
    unittest.main()
