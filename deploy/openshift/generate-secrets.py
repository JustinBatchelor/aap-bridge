#!/usr/bin/env python3
"""Generate fresh-install Secrets into a private local YAML file; never overwrite."""

import argparse
import base64
import os
import re
import secrets
from pathlib import Path

import yaml

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--namespace", default="aap-bridge")
parser.add_argument(
    "--output", type=Path, default=Path(__file__).parent / "local/secrets.yaml"
)
args = parser.parse_args()
if args.output.suffix.lower() not in (".yaml", ".yml"):
    parser.error("--output must have a .yaml or .yml extension")
if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", args.namespace):
    parser.error("namespace must be a DNS label of at most 63 characters")
password = secrets.token_hex(32)


def secret(name, values):
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": args.namespace},
        "type": "Opaque",
        "stringData": values,
    }


items = [
    secret(
        "aap-bridge-db",
        {
            "POSTGRESQL_USER": "aap_bridge",
            "POSTGRESQL_DATABASE": "aap_bridge",
            "POSTGRESQL_PASSWORD": password,
            "POSTGRESQL_ADMIN_PASSWORD": secrets.token_hex(32),
        },
    ),
    secret(
        "aap-bridge",
        {
            "MIGRATION_STATE_DB_PATH": (
                f"postgresql+psycopg2://aap_bridge:{password}@aap-bridge-db:5432/aap_bridge"
            ),
            "AAP_BRIDGE_TOKEN_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        },
    ),
    secret(
        "aap-bridge-oauth",
        {"cookie-secret": base64.b64encode(os.urandom(32)).decode()},
    ),
]
args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
    yaml.safe_dump_all(items, f, sort_keys=False)
print(f"Wrote fresh-install Secrets to {args.output}; retain securely with database backups.")
