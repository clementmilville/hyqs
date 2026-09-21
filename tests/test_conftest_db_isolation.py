from __future__ import annotations

import os

import psycopg
import pytest

import conftest


def test_unique_test_db_name_differs_across_calls():
    first = conftest._unique_test_db_name("hyqs")
    second = conftest._unique_test_db_name("hyqs")
    assert first != second
    assert first.startswith("hyqs_test_")
    assert second.startswith("hyqs_test_")
    assert "_test_test_" not in first
    assert "_test_test_" not in second


def test_unique_test_db_name_avoids_doubled_test_suffix():
    name = conftest._unique_test_db_name("hyqs_test")
    assert "_test_test_" not in name


@pytest.mark.skipif(
    bool(os.environ.get("HYQS_TEST_DB_URL", "").strip()),
    reason="provisioning is a no-op when HYQS_TEST_DB_URL is set explicitly",
)
def test_provision_and_teardown_session_database_creates_and_drops_isolated_db():
    saved = dict(conftest._session_db)
    try:
        conftest._provision_session_database()
        created_db = conftest._session_db["created_db"]
        assert created_db
        admin_dsn = conftest._session_db["admin_dsn"]

        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (created_db,)
            ).fetchone()
        assert exists is not None

        conftest._teardown_session_database()

        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (created_db,)
            ).fetchone()
        assert exists is None
        assert conftest._session_db == {"dsn": None, "created_db": None, "admin_dsn": None}
    finally:
        conftest._session_db.clear()
        conftest._session_db.update(saved)
