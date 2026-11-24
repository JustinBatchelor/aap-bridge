"""A filtered copy must also exclude the project from the actual Phase 2 reader."""

import json
import subprocess
import sys
from pathlib import Path

from aap_migration.cli.commands.patch_projects import load_projects_with_deferred_scm


def test_phase2_reader_uses_filtered_projects_without_changing_original(tmp_path):
    original = tmp_path / "xformed"
    filtered = tmp_path / "without-ado"
    (original / "projects").mkdir(parents=True)
    projects = [
        {
            "_source_id": sid,
            "name": name,
            "scm_type": "",
            "scm_url": "",
            "_deferred_scm_details": {"scm_type": "git", "scm_url": "https://example.invalid/repo"},
        }
        for sid, name in ((10, "ADO"), (11, "ADO version"), (12, "Other"))
    ]
    (original / "projects/projects_0001.json").write_text(json.dumps(projects))
    (original / "metadata.json").write_text(
        json.dumps(
            {
                "transform_timestamp": "synthetic",
                "resource_types": {"projects": {"transformed_count": 3}},
            }
        )
    )
    script = Path(__file__).resolve().parents[2] / "tools/exclude_transformed_project.py"
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--input",
            str(original),
            "--output",
            str(filtered),
            "--project-name",
            "ADO",
            "--apply",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert [row["name"] for row in load_projects_with_deferred_scm(filtered)] == [
        "ADO version",
        "Other",
    ]
    assert [row["name"] for row in load_projects_with_deferred_scm(original)] == [
        "ADO",
        "ADO version",
        "Other",
    ]
