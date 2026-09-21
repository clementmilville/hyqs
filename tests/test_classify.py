"""Tests for the deterministic diff domain classifier."""

from __future__ import annotations

from hyqs.pipeline.classify import classify_diff, is_schema_touching


def test_classify_db_migration_path():
    assert classify_diff(["migrations/0001_init.py"]) == {"db"}


def test_classify_db_alembic_path():
    assert classify_diff(["alembic/versions/abc123_add_table.py"]) == {"db"}


def test_classify_db_sql_file():
    assert classify_diff(["schema.sql"]) == {"db"}


def test_classify_db_model_class_in_diff_text():
    diff_text = "+class User(SQLModel, table=True):\n+    id: int\n"
    assert classify_diff(["hyqs/pipeline/models.py"], diff_text) == {"db"}


def test_classify_frontend_jsx():
    assert classify_diff(["src/components/Foo.jsx"]) == {"frontend"}


def test_classify_frontend_css():
    assert classify_diff(["src/styles.css"]) == {"frontend"}


def test_classify_authz_auth_path():
    assert classify_diff(["hyqs/web/auth.py"]) == {"authz"}


def test_classify_authz_session_path():
    assert classify_diff(["hyqs/web/session_store.py"]) == {"authz"}


def test_classify_authz_middleware_path():
    assert classify_diff(["hyqs/web/middleware.py"]) == {"authz"}


def test_classify_authz_keyword_in_path():
    assert classify_diff(["hyqs/web/reset_password.py"]) == {"authz"}


def test_classify_infra_docker_compose():
    assert classify_diff(["deploy/docker-compose.yml"]) == {"infra"}


def test_classify_infra_dockerfile():
    assert classify_diff(["Dockerfile"]) == {"infra"}


def test_classify_infra_nginx():
    assert classify_diff(["deploy/setup-nginx-hyqs.sh"]) == {"infra"}


def test_classify_combination_of_domains():
    changed = [
        "migrations/0002_add_index.py",
        "src/components/Foo.tsx",
        "hyqs/web/oauth.py",
        "deploy/docker-compose.yml",
    ]
    assert classify_diff(changed) == {"db", "frontend", "authz", "infra"}


def test_classify_no_domain_match():
    assert classify_diff(["hyqs/pipeline/pricing.py", "tests/test_pricing.py"]) == set()


def test_classify_empty_input():
    assert classify_diff([]) == set()


def test_classify_never_raises_on_malformed_input():
    assert classify_diff(None) == set()
    assert classify_diff([None, 123, "hyqs/web/auth.py"]) == {"authz"}


def test_is_schema_touching_models_py():
    assert is_schema_touching(["hyqs/pipeline/models.py"]) is True


def test_is_schema_touching_migrations_dir():
    assert is_schema_touching(["migrations/0001_init.py"]) is True


def test_is_schema_touching_alembic_dir():
    assert is_schema_touching(["alembic/versions/abc123_add_table.py"]) is True


def test_is_schema_touching_sql_file():
    assert is_schema_touching(["schema.sql"]) is True


def test_is_schema_touching_main_py_with_migration_marker():
    diff_text = "+def _run_startup_migrations():\n+    pass\n"
    assert is_schema_touching(["hyqs/pipeline/main.py"], diff_text) is True


def test_is_schema_touching_main_py_with_on_startup_marker():
    diff_text = "+@app.on_startup\n+async def init():\n+    pass\n"
    assert is_schema_touching(["hyqs/web/main.py"], diff_text) is True


def test_is_schema_touching_main_py_without_marker_not_flagged():
    diff_text = "+def helper():\n+    pass\n"
    assert is_schema_touching(["hyqs/web/main.py"], diff_text) is False


def test_is_schema_touching_unrelated_files_not_flagged():
    assert is_schema_touching(["hyqs/pipeline/pricing.py", "tests/test_pricing.py"]) is False


def test_is_schema_touching_empty_input():
    assert is_schema_touching([]) is False


def test_is_schema_touching_extra_patterns_override():
    assert is_schema_touching(["hyqs/pipeline/schema_v2.py"]) is False
    assert (
        is_schema_touching(["hyqs/pipeline/schema_v2.py"], extra_patterns=["*schema_v2*"]) is True
    )
