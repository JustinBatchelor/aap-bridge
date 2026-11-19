"""Offline CLI resource contracts and configuration regression checks."""

import copy
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from aap_migration.cli.main import cli
from aap_migration.config import load_config_from_yaml

ROOT = Path(__file__).resolve().parents[2]
OPENSHIFT = ROOT / "deploy/openshift"


def render(path):
    binary = shutil.which("oc") or shutil.which("kubectl")
    if not binary:
        pytest.skip("Offline render requires oc or kubectl")
    return list(
        yaml.safe_load_all(subprocess.check_output([binary, "kustomize", str(path)], text=True))
    )


def test_cli_build_inventory():
    docs = render(OPENSHIFT / "cli/builds")
    assert {(d["kind"], d["metadata"]["name"]) for d in docs} == {
        ("BuildConfig", "aap-bridge-cli"),
        ("ImageStream", "aap-bridge-cli"),
    }
    bc = next(d["spec"] for d in docs if d["kind"] == "BuildConfig")
    assert bc["triggers"] == [] and bc["runPolicy"] == "Serial"
    assert "nodeSelector" not in bc
    assert (
        bc["strategy"]["dockerStrategy"]["dockerfilePath"] == "deploy/openshift/Containerfile.cli"
    )
    assert bc["source"]["git"]["uri"] == "https://github.com/JustinBatchelor/aap-bridge.git"
    web = render(OPENSHIFT / "builds")
    assert {d["metadata"]["name"] for d in web} == {"aap-bridge-api", "aap-bridge-ui"}


def test_cli_and_job_resources():
    docs = render(OPENSHIFT / "cli")
    by_id = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    assert set(by_id) == {
        ("Deployment", "aap-bridge-cli"),
        ("StatefulSet", "aap-bridge-db"),
        ("PersistentVolumeClaim", "aap-bridge-db"),
        ("PersistentVolumeClaim", "aap-bridge-data"),
        ("Service", "aap-bridge-db"),
        ("ServiceAccount", "aap-bridge-cli"),
        ("ServiceAccount", "aap-bridge-db"),
        ("ConfigMap", "aap-bridge-cli-config"),
        ("NetworkPolicy", "aap-bridge-cli"),
        ("NetworkPolicy", "aap-bridge-db"),
    }
    deployment = by_id["Deployment", "aap-bridge-cli"]["spec"]
    assert deployment["replicas"] == 1 and deployment["strategy"]["type"] == "Recreate"
    assert deployment["selector"]["matchLabels"] == deployment["template"]["metadata"]["labels"]
    pod = deployment["template"]["spec"]
    job_docs = render(OPENSHIFT / "cli/job")
    assert len(job_docs) == 1
    job = job_docs[0]
    assert job["kind"] == "Job" and job["metadata"]["name"] == "aap-bridge-cli-run"
    js = job["spec"]
    assert js["parallelism"] == js["completions"] == 1 and js["backoffLimit"] == 0
    assert "ttlSecondsAfterFinished" not in js
    jp = js["template"]["spec"]
    assert jp["restartPolicy"] == "Never"
    assert jp["initContainers"] == pod["initContainers"]
    assert jp["volumes"] == pod["volumes"]
    assert pod["containers"][0]["command"] == ["sleep"]
    assert pod["containers"][0]["args"] == ["infinity"]
    assert jp["containers"][0]["args"] == [
        "--config",
        "/etc/aap-bridge/config.yaml",
        "config",
        "validate",
    ]
    for spec in (pod, jp):
        assert spec["automountServiceAccountToken"] is False
        assert spec["serviceAccountName"] == "aap-bridge-cli"
        assert "runAsUser" not in spec["securityContext"]
        for c in spec["containers"] + spec["initContainers"]:
            assert c["image"] == "aap-bridge-cli" and "ports" not in c
            sec = c["securityContext"]
            assert sec["runAsNonRoot"] and sec["readOnlyRootFilesystem"]
            assert not sec["allowPrivilegeEscalation"]
            assert sec["capabilities"]["drop"] == ["ALL"]
            assert sec["seccompProfile"]["type"] == "RuntimeDefault"
            assert "runAsUser" not in sec
            assert set(c["resources"]) == {"requests", "limits"}
            assert {e["secretRef"]["name"] for e in c["envFrom"]} == {
                "aap-bridge",
                "aap-bridge-connections",
            }
            assert next(m for m in c["volumeMounts"] if m["name"] == "config")["readOnly"]
    deny = by_id["NetworkPolicy", "aap-bridge-cli"]["spec"]
    assert deny["ingress"] == [] and deny["policyTypes"] == ["Ingress"]
    allow = by_id["NetworkPolicy", "aap-bridge-db"]["spec"]["ingress"]
    assert allow == [
        {
            "from": [{"podSelector": {"matchLabels": {"app": "aap-bridge-cli"}}}],
            "ports": [{"protocol": "TCP", "port": 5432}],
        }
    ]
    assert (
        by_id["PersistentVolumeClaim", "aap-bridge-data"]["spec"]["resources"]["requests"][
            "storage"
        ]
        == "20Gi"
    )
    assert (
        by_id["PersistentVolumeClaim", "aap-bridge-db"]["spec"]["resources"]["requests"]["storage"]
        == "10Gi"
    )


def test_config_load_and_validate_without_aap_or_secret_disclosure(monkeypatch, tmp_path):
    for side in ("SOURCE", "TARGET"):
        monkeypatch.setenv(f"{side}_AAP_URL", f"https://{side.lower()}.example.com")
        monkeypatch.setenv(f"{side}_AAP_VERSION", "2.6")
        monkeypatch.setenv(f"{side}_AAP_TOKEN", "synthetic-token")
    monkeypatch.setenv(
        "MIGRATION_STATE_DB_PATH", "postgresql+psycopg2://bridge:synthetic-password@db:5432/bridge"
    )
    config = load_config_from_yaml(OPENSHIFT / "cli/config.yaml")
    assert config.source.verify_ssl and config.target.verify_ssl
    assert config.paths.export_dir == "/data/exports"
    assert config.source.token == "synthetic-token"
    assert config.state.db_path.startswith("postgresql+psycopg2://")
    # Keep this host-side test independent of host /data permissions.
    data = copy.deepcopy(yaml.safe_load((OPENSHIFT / "cli/config.yaml").read_text()))
    data["logging"]["file"] = str(tmp_path / "migration.log")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    monkeypatch.chdir(tmp_path)
    for command in ("validate", "show"):
        result = CliRunner().invoke(cli, ["--config", str(path), "config", command])
        assert result.exit_code == 0, result.output
        assert "synthetic-password" not in result.output
        assert "synthetic-token" not in result.output
    assert not (tmp_path / "postgresql+psycopg2:").exists()


def test_example_overlays_resolve_same_immutable_image_and_namespace():
    import tempfile

    local = OPENSHIFT / "local"
    local.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=local) as temporary:
        rendered = []
        for mode, example in (("install", "kustomization.yaml"), ("job", "job-kustomization.yaml")):
            directory = Path(temporary) / mode
            directory.mkdir()
            text = (OPENSHIFT / "cli/example" / example).read_text()
            text = text.replace("namespace: aap-bridge-cli", "namespace: synthetic-cli")
            text = text.replace("REPLACE_WITH_CLI_REPOSITORY", "registry.example.com/test/cli")
            text = text.replace("REPLACE_WITH_CLI_SHA256_DIGEST", "sha256:" + "a" * 64)
            text = text.replace("REPLACE_WITH_STORAGE_CLASS", "synthetic-storage")
            data = yaml.safe_load(text)
            data.setdefault("patches", []).append(
                {
                    "target": {"kind": "Deployment|Job", "name": "aap-bridge-cli.*"},
                    "path": "ca-patch.yaml",
                }
            )
            (directory / "ca-patch.yaml").write_text(
                (OPENSHIFT / "cli/example/ca-patch.yaml").read_text()
            )
            (directory / "kustomization.yaml").write_text(yaml.safe_dump(data))
            rendered.extend(render(directory))
        for document in rendered:
            assert document["metadata"]["namespace"] == "synthetic-cli"
            if document["kind"] in ("Deployment", "Job"):
                pod = document["spec"]["template"]["spec"]
                assert {e["name"] for e in pod["containers"][0]["env"]} == {
                    "SSL_CERT_FILE",
                    "REQUESTS_CA_BUNDLE",
                }
                assert (
                    next(v for v in pod["volumes"] if v["name"] == "aap-ca")["configMap"]["name"]
                    == "aap-bridge-cli-ca"
                )
                for container in pod["containers"] + pod["initContainers"]:
                    assert container["image"] == "registry.example.com/test/cli@sha256:" + "a" * 64
            elif document["kind"] == "PersistentVolumeClaim":
                assert document["spec"]["storageClassName"] == "synthetic-storage"
            elif document["kind"] == "ServiceAccount":
                assert document["imagePullSecrets"] == [{"name": "vendor-pull"}]
