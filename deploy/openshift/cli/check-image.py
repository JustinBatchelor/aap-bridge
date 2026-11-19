#!/usr/bin/env python3
"""Audit a local CLI image and smoke-test it with no network or credentials."""

import argparse
import json
import subprocess
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--uid", type=int, default=12345)
    args = parser.parse_args()
    if args.uid <= 0:
        parser.error("--uid must be non-root and mapped by your local container runtime")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    inventory = args.output_dir / "inventory.yaml"
    log = args.output_dir / "smoke.log"
    if inventory.exists() or log.exists():
        parser.error("Choose a new output directory; previous evidence is retained")
    inspect = json.loads(
        subprocess.check_output(["podman", "image", "inspect", args.image], text=True)
    )[0]
    # Use the inspected immutable local ID for every check.
    image = inspect["Id"]
    base = [
        "podman",
        "run",
        "--rm",
        "--network=none",
        "--user",
        f"{args.uid}:0",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs",
        "/tmp:rw,mode=1777",
        "--tmpfs",
        "/data:rw,mode=0770",
    ]
    for side in ("SOURCE", "TARGET"):
        for key, value in {
            "URL": f"https://{side.lower()}.example.com",
            "VERSION": "2.6",
            "TOKEN": "synthetic-token",
        }.items():
            base += ["-e", f"{side}_AAP_{key}={value}"]
    base += [
        "-e",
        "MIGRATION_STATE_DB_PATH=postgresql+psycopg2://bridge:synthetic-password@db:5432/bridge",
    ]

    def run(entrypoint, *command):
        return subprocess.check_output(
            [*base, "--entrypoint", entrypoint, image, *command],
            text=True,
            stderr=subprocess.STDOUT,
        )

    rpm = run("rpm", "-qa", "--qf", "%{NAME} %{VERSION}-%{RELEASE}.%{ARCH}\n").splitlines()
    packages = json.loads(run("python3.12", "-m", "pip", "list", "--format=json"))
    assert not any(line.split()[0].startswith(("nginx", "httpd", "nodejs", "npm")) for line in rpm)
    assert not (
        {p["name"].lower() for p in packages}
        & {"fastapi", "uvicorn", "starlette", "websockets", "python-multipart"}
    )
    smoke = run(
        "python3.12",
        "-c",
        """
import importlib.util, os, pathlib, shutil, subprocess
assert os.getuid() != 0
for name in ('nginx', 'httpd', 'node', 'nodejs', 'npm'):
    assert shutil.which(name) is None, name
for name in ('fastapi', 'uvicorn', 'starlette'):
    assert importlib.util.find_spec(name) is None, name
for path in ('/app/web', '/usr/share/nginx/html', '/var/www/html'):
    assert not pathlib.Path(path).exists(), path
for name in ('exports', 'xformed', 'schemas', 'reports', 'backups', 'logs'):
    path = pathlib.Path('/data') / name
    path.mkdir()
    (path / 'probe').write_text('ok')
for command in (['--version'], ['--help'], ['--config', '/etc/aap-bridge/config.yaml', 'config', 'validate']):
    result = subprocess.run(['aap-bridge', *command], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'synthetic-password' not in result.stdout + result.stderr
    print(result.stdout)
assert not pathlib.Path('/data/postgresql+psycopg2:').exists()
print('Arbitrary UID, read-only root filesystem, persistent paths, and offline config checks passed')
""",
    )
    with inventory.open("x") as stream:
        yaml.safe_dump(
            {
                "image": args.image,
                "imageID": image,
                "architecture": inspect["Architecture"],
                "uid": args.uid,
                "rpmPackages": sorted(rpm),
                "pythonPackages": packages,
            },
            stream,
            sort_keys=False,
        )
    with log.open("x") as stream:
        stream.write(smoke)
    print(f"Passed. Inventory: {inventory}; smoke results: {log}")


if __name__ == "__main__":
    main()
