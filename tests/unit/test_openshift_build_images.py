"""Build orchestration must pin revisions and never implicitly deploy images."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def helper():
    path = Path(__file__).resolve().parents[2] / "deploy/openshift/build-images.py"
    spec = importlib.util.spec_from_file_location("build_images", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completed_build(name, commit):
    return {
        "spec": {"revision": {"git": {"commit": commit}}},
        "status": {
            "phase": "Complete",
            "output": {"to": {"imageDigest": "sha256:" + "b" * 64}},
            "outputDockerImageReference": f"registry:5000/test/{name}:candidate",
        },
    }


def test_sequential_builds_record_exact_outputs_without_deploying(helper, monkeypatch, tmp_path):
    commit = "a" * 40
    output = tmp_path / "images.json"
    calls = []

    def oc(namespace, *args):
        calls.append((namespace, args))
        if args[0] == "start-build":
            assert args[2] == "--commit=" + commit
            return "build/" + args[1] + "-1"
        assert args[0] == "get"  # No apply, patch, tag, or rollout operation.
        name = args[1].removeprefix("build/").removesuffix("-1")
        return json.dumps(completed_build(name, commit))

    monkeypatch.setattr(helper, "oc", oc)
    monkeypatch.setattr(
        "sys.argv",
        ["build-images", "--namespace", "test", "--commit", commit, "--output", str(output)],
    )
    helper.main()
    records = json.loads(output.read_text())["images"]
    assert [call[1][0] for call in calls] == ["start-build", "get", "start-build", "get"]
    assert all(call[0] == "test" for call in calls)
    assert [r["name"] for r in records] == ["aap-bridge-api", "aap-bridge-ui"]
    assert records[0]["newName"] == "registry:5000/test/aap-bridge-api"
    assert all(r["digest"] == "sha256:" + "b" * 64 for r in records)


@pytest.mark.parametrize("failure", ["phase", "revision", "digest"])
def test_failed_or_unverified_build_never_emits_promotion_record(
    helper, monkeypatch, tmp_path, failure
):
    commit = "a" * 40
    output = tmp_path / "images.json"
    data = completed_build("aap-bridge-api", commit)
    if failure == "phase":
        data["status"]["phase"] = "Failed"
    elif failure == "revision":
        data["spec"]["revision"]["git"]["commit"] = "c" * 40
    else:
        data["status"]["output"]["to"]["imageDigest"] = ""
    monkeypatch.setattr(
        helper,
        "oc",
        lambda namespace, *args: (
            "build/aap-bridge-api-1" if args[0] == "start-build" else json.dumps(data)
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["build-images", "--namespace", "test", "--commit", commit, "--output", str(output)],
    )
    with pytest.raises(SystemExit):
        helper.main()
    assert not output.exists()


def test_existing_record_is_not_overwritten(helper, monkeypatch, tmp_path):
    output = tmp_path / "images.json"
    output.write_text("previous output")
    monkeypatch.setattr(
        "sys.argv",
        ["build-images", "--namespace", "test", "--commit", "a" * 40, "--output", str(output)],
    )
    monkeypatch.setattr(helper, "oc", lambda *args: pytest.fail("No build should start"))
    with pytest.raises(SystemExit):
        helper.main()
    assert output.read_text() == "previous output"
