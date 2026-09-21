import os
import uuid

import psycopg
import pytest

from hyqs.pipeline.store import JobStore, set_db_actor

# Populated by _provision_session_database() at pytest_configure time. When
# 'dsn' is set, _test_dsn() returns it for the whole session instead of the
# shared '_test' database — this is what lets two pipeline jobs' pytest runs
# execute concurrently against the same Postgres server without deadlocking
# on each other's schema-touching tests.
_session_db: dict = {"dsn": None, "created_db": None, "admin_dsn": None}


_DEFAULT_BASE_DSN = "postgresql://hyqs:hyqs@localhost:5432/hyqs"


def _base_dsn() -> str:
    """The DSN used to provision (and fall back to) test databases.

    HYQS_TEST_DB_ADMIN_URL is preferred and is the only one a pipeline job gets:
    a job's test subprocess is deliberately not given HYQS_DB_URL, which owns the
    shared control plane. HYQS_DB_URL/DATABASE_URL still work for a developer
    running the suite by hand in a checkout that has a .env.
    """
    for var in ("HYQS_TEST_DB_ADMIN_URL", "HYQS_DB_URL", "DATABASE_URL"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return _DEFAULT_BASE_DSN


def _unique_test_db_name(root_dbname: str) -> str:
    if root_dbname.endswith("_test"):
        root_dbname = root_dbname[: -len("_test")]
    return f"{root_dbname}_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"


def _provision_session_database() -> None:
    """Create a uniquely-named Postgres database for this pytest session.

    Best-effort: if the connecting role can't CREATE DATABASE (e.g. local dev
    without createdb rights), leave _session_db['dsn'] unset so _test_dsn()
    falls back to the shared '<root>_test' database.
    """
    if os.environ.get("HYQS_TEST_DB_URL", "").strip():
        return  # explicit DSN wins — no provisioning, no teardown
    base = _base_dsn()
    info = {k: v for k, v in psycopg.conninfo.conninfo_to_dict(base).items() if v is not None}
    root_dbname = info.get("dbname") or "hyqs"
    unique_db = _unique_test_db_name(root_dbname)
    try:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{unique_db}"')
    except psycopg.Error as e:
        print(
            f"conftest: could not provision isolated test database "
            f"'{unique_db}' ({e}); falling back to the shared "
            f"'{root_dbname}_test' database"
        )
        if base == _DEFAULT_BASE_DSN:
            # No DSN was in the environment at all, so the placeholder above was
            # used and almost certainly cannot authenticate. Say so plainly:
            # otherwise every DB-backed test fails with a bare "password
            # authentication failed" and the reader has no way to tell that the
            # cause is a missing environment variable rather than their own code.
            print(
                "conftest: NO test-database DSN was found in the environment "
                "(HYQS_TEST_DB_ADMIN_URL / HYQS_DB_URL / DATABASE_URL), so the "
                "built-in placeholder was used. Every DB-backed test below will "
                "fail to connect. If this is a pipeline job, the worktree has no "
                ".env and HYQS_TEST_DB_ADMIN_URL must be set on the host and "
                "allowlisted in hyqs/pipeline/resources.py."
            )
        return
    isolated_info = dict(info)
    isolated_info["dbname"] = unique_db
    _session_db["dsn"] = psycopg.conninfo.make_conninfo(**isolated_info)
    _session_db["created_db"] = unique_db
    _session_db["admin_dsn"] = base


def _teardown_session_database() -> None:
    created_db = _session_db.get("created_db")
    if not created_db:
        return
    admin_dsn = _session_db["admin_dsn"]
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            try:
                conn.execute(f'DROP DATABASE IF EXISTS "{created_db}" WITH (FORCE)')
            except psycopg.Error:
                conn.execute(f'DROP DATABASE IF EXISTS "{created_db}"')
    except psycopg.Error as e:
        print(f"conftest: could not drop isolated test database '{created_db}' ({e})")
    finally:
        _session_db["dsn"] = None
        _session_db["created_db"] = None
        _session_db["admin_dsn"] = None


def pytest_configure(config):
    _provision_session_database()


def pytest_sessionfinish(session, exitstatus):
    _teardown_session_database()


def _test_dsn() -> str:
    """The tests' Postgres DSN — ALWAYS a dedicated *_test database.

    Tests used to run against whatever HYQS_DB_URL pointed at — the production
    database. A pipeline job's test once auto-created a fake project ("repo",
    /fake/repo) plus 128 junk jobs there. Now: HYQS_TEST_DB_URL wins when set;
    otherwise pytest_configure() provisions a uniquely-named database for this
    session (see _provision_session_database) so concurrent pipeline jobs
    never share relations; if that provisioning wasn't possible (or is still
    pending), fall back to a shared "_test"-suffixed database on the same
    server, created if missing (JobStore creates the schema itself on first
    connect).
    """
    if _session_db["dsn"]:
        return _session_db["dsn"]
    explicit = os.environ.get("HYQS_TEST_DB_URL", "").strip()
    if explicit:
        return explicit
    base = _base_dsn()
    info = {k: v for k, v in psycopg.conninfo.conninfo_to_dict(base).items() if v is not None}
    dbname = info.get("dbname") or "hyqs"
    if not dbname.endswith("_test"):
        test_db = f"{dbname}_test"
        with psycopg.connect(base, autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (test_db,)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{test_db}"')
        info["dbname"] = test_db
    return psycopg.conninfo.make_conninfo(**info)


@pytest.fixture
def store():
    set_db_actor("test:pytest")
    s = JobStore(_test_dsn())
    yield s
    s.close()
