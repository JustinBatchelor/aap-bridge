#!/usr/bin/env python3
"""Build API and UI sequentially at one Git commit; record immutable image references.

Requires the builds Kustomization to be applied first. Never deploys workloads.
"""

import argparse
import json
import re
import subprocess
import time
from pathlib import Path


def oc(namespace, *args):
    return subprocess.check_output(["oc", "-n", namespace, *args], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--commit", required=True, help="Published full Git commit SHA")
    parser.add_argument("--output", type=Path, required=True, help="New local JSON record")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("--commit must be a full lowercase Git commit SHA")
    if args.output.exists():
        parser.error("--output already exists; choose a new record to retain prior digests")
    records = []
    for component in ("api", "ui"):
        name = "aap-bridge-" + component
        build = oc(args.namespace, "start-build", name, "--commit=" + args.commit, "-o", "name")
        print(f"Started {build}", flush=True)
        deadline = time.monotonic() + 1900
        previous = None
        while True:
            data = json.loads(oc(args.namespace, "get", build, "-o", "json"))
            phase = data["status"]["phase"]
            if phase != previous:
                print(f"{build}: {phase}", flush=True)
                previous = phase
            if phase == "Complete":
                break
            if phase in ("Failed", "Error", "Cancelled") or time.monotonic() > deadline:
                raise SystemExit(
                    f"{build} did not complete; inspect with oc -n {args.namespace} logs {build}"
                )
            time.sleep(5)
        revision = data.get("spec", {}).get("revision", {}).get("git", {}).get("commit")
        if revision != args.commit:
            raise SystemExit(f"Unexpected source revision for {build}; refusing promotion output")
        status = data["status"]
        digest = status.get("output", {}).get("to", {}).get("imageDigest", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise SystemExit(f"Missing output digest for {build}")
        repository = status["outputDockerImageReference"].rsplit(":", 1)[0]
        records.append(
            {
                "name": name,
                "build": build,
                "commit": revision,
                "newName": repository,
                "digest": digest,
            }
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump({"namespace": args.namespace, "images": records}, stream, indent=2)
        stream.write("\n")
    print("images:")
    for record in records:
        print(
            f"  - name: {record['name']}\n    newName: {record['newName']}\n    digest: {record['digest']}"
        )
    print(f"Recorded {args.output}. No deployment was changed.")


if __name__ == "__main__":
    main()
