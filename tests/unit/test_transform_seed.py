import pytest

from aap_migration.cli.commands.transform import (
    credential_types_support_managed_filter,
    is_builtin_credential_type,
)
from aap_migration.migration.transformer import ProjectTransformer


def test_credential_types_support_managed_filter() -> None:
    assert credential_types_support_managed_filter("2.3") is True
    assert credential_types_support_managed_filter("2.6") is True
    assert credential_types_support_managed_filter("1.0") is False
    assert credential_types_support_managed_filter("2.2") is False


def test_is_builtin_credential_type() -> None:
    assert is_builtin_credential_type({"managed": True, "name": "Machine"}) is True
    assert is_builtin_credential_type({"namespace": "tower", "name": "Ansible Tower"}) is True
    assert is_builtin_credential_type({"name": "Custom"}) is False


def test_project_transformer_defers_git_refspec() -> None:
    transformer = ProjectTransformer()

    transformed = transformer.transform_resource(
        "projects",
        {
            "id": 1,
            "name": "pull-request-project",
            "organization": 2,
            "scm_type": "git",
            "scm_url": "https://example.com/repo.git",
            "scm_refspec": "+refs/pull/*:refs/remotes/origin/pr/*",
        },
    )

    assert transformed["_deferred_scm_details"]["scm_refspec"] == (
        "+refs/pull/*:refs/remotes/origin/pr/*"
    )
    assert "scm_refspec" not in transformed


@pytest.mark.parametrize("track_submodules", [True, False])
def test_project_transformer_defers_submodule_option(track_submodules: bool) -> None:
    transformed = ProjectTransformer().transform_resource(
        "projects",
        {
            "id": 330,
            "name": "manual-import-regression",
            "organization": 2,
            "scm_type": "git",
            "scm_url": "https://example.com/repo.git",
            "scm_track_submodules": track_submodules,
            "scm_clean": True,
            "scm_delete_on_update": True,
            "scm_update_on_launch": True,
            "credential": 7,
        },
    )
    assert transformed["scm_type"] == ""
    assert transformed["scm_url"] == ""
    assert transformed["_deferred_scm_details"]["scm_track_submodules"] is track_submodules
    for field in (
        "scm_track_submodules",
        "scm_clean",
        "scm_delete_on_update",
        "scm_update_on_launch",
        "credential",
    ):
        assert field not in transformed


def test_project_transformer_preserves_submodules_when_deferral_disabled() -> None:
    transformed = ProjectTransformer(defer_project_sync=False).transform_resource(
        "projects",
        {
            "id": 330,
            "name": "immediate-scm",
            "organization": 2,
            "scm_type": "git",
            "scm_url": "https://example.com/repo.git",
            "scm_track_submodules": True,
        },
    )
    assert transformed["scm_track_submodules"] is True
    assert "_deferred_scm_details" not in transformed
