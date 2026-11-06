"""Tests for logging utilities."""

import pytest

from aap_migration.utils.logging import redact_database_url


def test_redact_database_url_masks_postgresql_password():
    url = "postgresql://aap_migration_user:redhat@localhost:5432/aap_migration"
    assert redact_database_url(url) == "postgresql://***@localhost:5432/aap_migration"


def test_redact_database_url_leaves_sqlite_unchanged():
    url = "sqlite:///data/state.db"
    assert redact_database_url(url) == url


def test_redact_database_url_leaves_url_without_credentials_unchanged():
    url = "postgresql://localhost:5432/aap_migration"
    assert redact_database_url(url) == url


def test_redact_database_url_masks_mysql_password():
    url = "mysql://dbuser:s3cret@db.example.com/mydb"
    assert redact_database_url(url) == "mysql://***@db.example.com/mydb"


def test_redact_database_url_masks_explicit_driver_credentials():
    url = "postgresql+psycopg2://dbuser:password@db:5432/bridge"
    assert redact_database_url(url) == "postgresql+psycopg2://***@db:5432/bridge"


@pytest.mark.parametrize("driver", ["postgresql", "postgresql+psycopg2", "mysql+pymysql"])
def test_redact_database_url_hides_credentials_in_malformed_sqlite_path(driver):
    url = f"sqlite:///{driver}://dbuser:password@db:5432/bridge"
    assert redact_database_url(url) == "[REDACTED]"


def test_redact_database_url_preserves_explicit_sqlite_driver():
    url = "sqlite+pysqlite:///data/state.db"
    assert redact_database_url(url) == url
