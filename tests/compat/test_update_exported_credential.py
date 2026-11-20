"""Exercise the credential export editor under Python 3.6+ without AAP access."""

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "tools/update_exported_credential.py"
SPEC = importlib.util.spec_from_file_location("export_editor", str(SCRIPT))
editor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(editor)


class ExportCredentialEditorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.credentials = self.directory / "credentials"
        self.credentials.mkdir()
        self.first = self.credentials / "credentials_0001.json"
        self.second = self.credentials / "credentials_0002.json"
        self.first.write_text(
            json.dumps([{"id": 1, "name": "Unchanged", "inputs": {"password": "$encrypted$"}}])
        )
        self.second.write_text(
            json.dumps(
                [
                    {
                        "id": 2,
                        "name": "Private repo",
                        "inputs": {"username": "scm-user", "password": "$encrypted$"},
                    },
                    {"id": 3, "name": "Other", "inputs": {"password": "$encrypted$"}},
                ]
            )
        )
        self.original = {p.name: p.read_bytes() for p in self.credentials.iterdir()}
        self.password = "synthetic-test-value"

    def invoke(self, values=None, interactive=True, credential_id="2", selections=None):
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(editor.sys.stdin, "isatty", return_value=interactive))
            prompt = stack.enter_context(
                patch.object(
                    editor.getpass, "getpass", side_effect=values or [self.password, self.password]
                )
            )
            stack.enter_context(patch("builtins.input", side_effect=selections or []))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            args = ["--export-dir", str(self.directory)]
            if credential_id is not None:
                args.extend(["--credential-id", credential_id])
            status = editor.main(args)
        self.assertNotIn(self.password, output.getvalue() + errors.getvalue())
        self.output = output.getvalue()
        return status, prompt.call_count

    def assert_unchanged(self):
        self.assertEqual(
            {p.name: p.read_bytes() for p in self.credentials.iterdir()}, self.original
        )

    def test_updates_only_selected_record_atomically_with_private_permissions(self):
        self.second.chmod(0o644)
        status, prompts = self.invoke()
        self.assertEqual((status, prompts), (0, 2))
        self.assertEqual(self.first.read_bytes(), self.original[self.first.name])
        expected = json.loads(self.original[self.second.name].decode())
        expected[0]["inputs"]["password"] = self.password
        self.assertEqual(json.loads(self.second.read_text()), expected)
        self.assertEqual(self.second.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list(self.credentials.iterdir())), 2)

    def test_duplicate_id_refused_before_requesting_secret(self):
        self.first.write_text(self.second.read_text())
        status, prompts = self.invoke()
        self.assertEqual((status, prompts), (1, 0))
        self.assertEqual(self.first.read_text(), self.second.read_text())

    def test_missing_credential_refused_before_requesting_secret(self):
        self.assertEqual(self.invoke(credential_id="999"), (1, 0))
        self.assert_unchanged()

    def test_invalid_input_files_refused_before_requesting_secret(self):
        for contents in ("not JSON", "{}", "[123]", '[{"id": 2, "inputs": {}}]'):
            with self.subTest(contents=contents):
                self.second.write_text(contents)
                self.assertEqual(self.invoke(), (1, 0))
                self.assertEqual(self.second.read_text(), contents)

    def test_invalid_or_mismatched_secret_does_not_change_files(self):
        for values in (("", ""), ("$encrypted$", "$encrypted$"), (self.password, "different")):
            with self.subTest(values=bool(values[0])):
                self.assertEqual(self.invoke(values=values)[0], 1)
                self.assert_unchanged()

    def test_noninteractive_input_cannot_echo_secret(self):
        self.assertEqual(self.invoke(interactive=False), (1, 0))
        self.assert_unchanged()

    def test_hidden_input_failure_does_not_fall_back_to_echo(self):
        self.assertEqual(self.invoke(values=editor.getpass.GetPassWarning())[0], 1)
        self.assert_unchanged()

    def test_cancel_leaves_export_unchanged(self):
        for exception in (EOFError(), KeyboardInterrupt()):
            with self.subTest(exception=type(exception).__name__):
                self.assertEqual(self.invoke(values=exception)[0], 130)
                self.assert_unchanged()

    def test_failed_replace_leaves_original_and_removes_temporary_secret(self):
        with patch.object(editor.os, "replace", side_effect=OSError("fixture replacement failure")):
            self.assertEqual(self.invoke()[0], 1)
        self.assert_unchanged()

    def test_concurrent_edit_is_not_overwritten(self):
        def enter_secret(prompt):
            self.second.write_text("changed by another process")
            return self.password

        self.assertEqual(self.invoke(values=enter_secret)[0], 1)
        self.assertEqual(self.second.read_text(), "changed by another process")
        self.assertEqual(len(list(self.credentials.iterdir())), 2)

    def test_symbolic_link_is_not_followed(self):
        target = self.directory / "actual.json"
        self.second.rename(target)
        self.second.symlink_to(target)
        self.assertEqual(self.invoke(), (1, 0))
        self.assertEqual(target.read_bytes(), self.original[self.second.name])

    def test_missing_export_is_reported_without_secret_prompt(self):
        self.first.unlink()
        self.second.unlink()
        self.assertEqual(self.invoke(), (1, 0))

    def test_invalid_id_is_rejected(self):
        for value in ("0", "-1", "not-an-id"):
            with self.subTest(value=value), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    editor.main(["--credential-id", value])
                self.assertEqual(error.exception.code, 2)
        self.assert_unchanged()

    def test_interactive_selection_by_name_finds_later_chunk(self):
        self.assertEqual(self.invoke(credential_id=None, selections=["Private repo"]), (0, 2))
        self.assertIn("2 | Private repo", self.output)
        self.assertIn("1 | Unchanged", self.output)
        self.assertNotIn("$encrypted$", self.output)
        self.assertEqual(
            json.loads(self.second.read_text())[0]["inputs"]["password"], self.password
        )
        self.assertEqual(self.first.read_bytes(), self.original[self.first.name])

    def test_interactive_selection_by_id(self):
        self.assertEqual(self.invoke(credential_id=None, selections=["3"]), (0, 2))
        data = json.loads(self.second.read_text())
        self.assertEqual(data[0]["inputs"]["password"], "$encrypted$")
        self.assertEqual(data[1]["inputs"]["password"], self.password)

    def test_duplicate_names_require_id_selection(self):
        data = json.loads(self.first.read_text())
        data[0]["name"] = "Private repo"
        self.first.write_text(json.dumps(data))
        before = self.first.read_bytes()
        self.assertEqual(self.invoke(credential_id=None, selections=["Private repo", "2"]), (0, 2))
        self.assertIn("name is duplicated", self.output)
        self.assertEqual(self.first.read_bytes(), before)
        self.assertEqual(
            json.loads(self.second.read_text())[0]["inputs"]["password"], self.password
        )

    def test_unknown_selection_can_be_corrected(self):
        self.assertEqual(self.invoke(credential_id=None, selections=["missing", "", "2"]), (0, 2))
        self.assertIn("No matching credential", self.output)

    def test_interactive_cancel_does_not_prompt_for_pat_or_change_exports(self):
        self.assertEqual(self.invoke(credential_id=None, selections=["q"]), (130, 0))
        self.assert_unchanged()

    def test_interactive_nonterminal_refused(self):
        self.assertEqual(self.invoke(credential_id=None, interactive=False), (1, 0))
        self.assert_unchanged()

    def test_interactive_duplicate_id_does_not_prompt_for_pat(self):
        self.first.write_text(self.second.read_text())
        before = self.first.read_bytes()
        self.assertEqual(self.invoke(credential_id=None, selections=["2"]), (1, 0))
        self.assertEqual(self.first.read_bytes(), before)
        self.assertEqual(self.second.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
