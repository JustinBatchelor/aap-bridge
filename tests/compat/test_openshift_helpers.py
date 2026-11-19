"""Run with Python 3.6+ and PyYAML: python -m unittest discover -s tests/compat.

Fake oc/Podman executables exercise real subprocess calls without cluster access
or images. This suite does not import the Python 3.12 application.
"""

# Keep subprocess calls usable on Python 3.6.
# ruff: noqa: UP021, UP022

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class DeploymentHelperCompatibility(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.trace = self.directory / "commands.jsonl"
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        self.env["HELPER_TEST_TRACE"] = str(self.trace)

    def executable(self, name, source):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + source)
        path.chmod(0o700)

    def run_helper(self, script, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "deploy/openshift" / script), *map(str, args)],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

    def test_build_modes_and_preserved_output(self):
        self.executable(
            "oc",
            """
import json, os, sys
args = sys.argv[1:]
with open(os.environ['HELPER_TEST_TRACE'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
assert args[:2] == ['-n', 'fixture']
if args[2] == 'start-build':
    assert args[4] == '--commit=' + 'a' * 40
    print('build/' + args[3] + '-1')
else:
    assert args[2] == 'get'
    name = args[3][len('build/'):-2]
    print(json.dumps({
        'spec': {'revision': {'git': {'commit': 'a' * 40}}},
        'status': {'phase': 'Complete',
                   'output': {'to': {'imageDigest': 'sha256:' + 'b' * 64}},
                   'outputDockerImageReference': 'registry:5000/fixture/' + name + ':candidate'}
    }))
""",
        )
        for mode, names in (
            ("cli", ["aap-bridge-cli"]),
            ("web", ["aap-bridge-api", "aap-bridge-ui"]),
        ):
            with self.subTest(mode=mode):
                output = self.directory / (mode + ".yaml")
                args = ["--namespace", "fixture", "--commit", "a" * 40, "--output", output]
                if mode == "cli":
                    args += ["--mode", "cli"]
                self.trace.write_text("")
                result = self.run_helper("build-images.py", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = yaml.safe_load(output.read_text())
                self.assertEqual([r["name"] for r in record["images"]], names)
                for image in record["images"]:
                    self.assertEqual(image["commit"], "a" * 40)
                    self.assertEqual(image["digest"], "sha256:" + "b" * 64)
                commands = [json.loads(line) for line in self.trace.read_text().splitlines()]
                self.assertEqual([c[2] for c in commands], ["start-build", "get"] * len(names))
                previous = output.read_text()
                trace = self.trace.read_text()
                self.assertNotEqual(self.run_helper("build-images.py", *args).returncode, 0)
                self.assertEqual(output.read_text(), previous)
                self.assertEqual(self.trace.read_text(), trace)

    def test_secret_modes_permissions_and_consistency(self):
        for mode in ("cli", "web"):
            with self.subTest(mode=mode):
                output = self.directory / (mode + "-secrets.yaml")
                args = ["--namespace", "fixture", "--output", output]
                if mode == "cli":
                    args += ["--mode", "cli"]
                result = self.run_helper("generate-secrets.py", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                previous = output.read_text()
                docs = {d["metadata"]["name"]: d for d in yaml.safe_load_all(previous)}
                self.assertEqual(len(docs), 2 if mode == "cli" else 3)
                password = docs["aap-bridge-db"]["stringData"]["POSTGRESQL_PASSWORD"]
                state = docs["aap-bridge"]["stringData"]
                self.assertEqual(
                    state["MIGRATION_STATE_DB_PATH"],
                    "postgresql+psycopg2://aap_bridge:"
                    + password
                    + "@aap-bridge-db:5432/aap_bridge",
                )
                self.assertEqual("AAP_BRIDGE_TOKEN_ENCRYPTION_KEY" in state, mode == "web")
                self.assertNotIn(password, result.stdout + result.stderr)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                self.assertNotEqual(self.run_helper("generate-secrets.py", *args).returncode, 0)
                self.assertEqual(output.read_text(), previous)

    def test_image_checker_host_orchestration(self):
        self.executable(
            "podman",
            """
import json, sys
args = sys.argv[1:]
if args[:2] == ['image', 'inspect']:
    print(json.dumps([{'Id': 'sha256:' + 'c' * 64, 'Architecture': 'arm64'}]))
else:
    assert args[0] == 'run' and '--network=none' in args
    index = args.index('--entrypoint')
    entrypoint = args[index + 1]
    assert args[index + 2] == 'sha256:' + 'c' * 64
    command = args[index + 3:]
    if entrypoint == 'rpm':
        print('python3.12 3.12-fixture')
    elif command[:3] == ['-m', 'pip', 'list']:
        print(json.dumps([{'name': 'aap-bridge', 'version': '0.1.0'}]))
    else:
        assert entrypoint == 'python3.12' and command[0] == '-c'
        print('fixture image smoke result')
""",
        )
        output = self.directory / "review"
        args = ["--image", "fixture-image", "--output-dir", output]
        result = self.run_helper("cli/check-image.py", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = yaml.safe_load((output / "inventory.yaml").read_text())
        self.assertEqual(record["imageID"], "sha256:" + "c" * 64)
        self.assertEqual((output / "smoke.log").read_text(), "fixture image smoke result\n")
        self.assertNotEqual(self.run_helper("cli/check-image.py", *args).returncode, 0)


if __name__ == "__main__":
    unittest.main()
