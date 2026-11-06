"""Regression coverage for migration database URLs and file paths."""

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

from aap_migration.config import StateConfig
from aap_migration.migration.database import normalize_database_url
from aap_migration.migration.state import MigrationState


@pytest.mark.parametrize(
    "value,expected",
    [
        ("postgresql+psycopg2://user:password@db/bridge", None),
        ("postgresql://user:password@db/bridge", None),
        ("mysql+pymysql://user:password@db/bridge", None),
        ("sqlite+pysqlite:///:memory:", None),
        ("sqlite:///:memory:", None),
        ("./migration_state.db", "sqlite:///./migration_state.db"),
        ("/data/migration_state.db", "sqlite:////data/migration_state.db"),
        (r"C:\data\migration_state.db", r"sqlite:///C:\data\migration_state.db"),
    ],
)
def test_database_url_normalization(value, expected):
    expected = expected or value
    assert normalize_database_url(value) == expected
    with patch("aap_migration.migration.state.init_database") as initialize:
        state = MigrationState(StateConfig(db_path=value))
    assert state.database_url == expected
    assert initialize.call_args.args[0] == expected


def test_explicit_postgresql_driver_loads_without_connecting():
    """Use the installed driver without contacting a database or AAP endpoint."""
    url = normalize_database_url("postgresql+psycopg2://user:password@db/bridge")
    engine = create_engine(url)
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg2"
    finally:
        engine.dispose()
