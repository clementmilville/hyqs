"""Unit tests for deterministic test-command detection in hyqs.pipeline.testing.

These tests mock ``shutil.which`` so no real ``uv``/``pytest`` invocation is
required. No Postgres or network needed.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import ANY, AsyncMock, patch

from hyqs.pipeline import testing

_FAKE_WORKER_DSN = "postgresql://hyqs:hyqs@localhost:5432/hyqs"  # pragma: allowlist secret


def _fake_which(name: str) -> str | None:
    return "/usr/bin/uv" if name == "uv" else None


def test_detect_test_command_no_extras_table_stays_plain(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

    with patch("hyqs.pipeline.testing.shutil.which", side_effect=_fake_which):
        cmd = testing.detect_test_command(tmp_path)

    assert cmd == ["uv", "run", "pytest", "-q"]


def test_detect_test_command_no_pyproject_at_all_stays_plain(tmp_path):
    (tmp_path / "tests").mkdir()

    with patch("hyqs.pipeline.testing.shutil.which", side_effect=_fake_which):
        cmd = testing.detect_test_command(tmp_path)

    assert cmd == ["python", "-m", "pytest", "-q"]


def test_detect_test_command_with_optional_dependencies_adds_all_extras(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n\n'
        "[project.optional-dependencies]\n"
        'dev = ["pytest", "pytest-asyncio", "testcontainers", "httpx"]\n'
    )

    with patch("hyqs.pipeline.testing.shutil.which", side_effect=_fake_which):
        cmd = testing.detect_test_command(tmp_path)

    assert cmd == ["uv", "run", "--all-extras", "pytest", "-q"]


def test_detect_test_command_malformed_pyproject_does_not_raise(tmp_path):
    (tmp_path / "pyproject.toml").write_text("not valid toml [[[")

    with patch("hyqs.pipeline.testing.shutil.which", side_effect=_fake_which):
        cmd = testing.detect_test_command(tmp_path)

    assert cmd == ["uv", "run", "pytest", "-q"]


def test_detect_test_execution_uses_nested_project_owning_changed_tests(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "pyproject.toml").write_text(
        '[project]\nname = "backend"\n\n[project.optional-dependencies]\ndev = ["pytest"]\n'
    )

    with patch("hyqs.pipeline.testing.shutil.which", side_effect=_fake_which):
        execution = testing.detect_test_execution(tmp_path, ["backend/tests/test_session_store.py"])

    assert execution == testing.TestExecution(
        ["uv", "run", "--all-extras", "pytest", "-q"], backend
    )


def test_run_tests_runs_nested_test_path_relative_to_project(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    backend = tmp_path / "backend"
    (backend / "tests").mkdir(parents=True)
    (backend / "pyproject.toml").write_text('[project]\nname = "backend"\n')
    (backend / "tests" / "test_session_store.py").write_text("def test_ok(): pass\n")

    with (
        patch("hyqs.pipeline.testing.shutil.which", side_effect=_fake_which),
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            new=AsyncMock(return_value=(0, "ok", None)),
        ) as measure,
    ):
        result = asyncio.run(
            testing.run_tests(tmp_path, changed_files=["backend/tests/test_session_store.py"])
        )

    measure.assert_awaited_once_with(
        ["uv", "run", "pytest", "-q", "tests/test_session_store.py"],
        cwd=str(backend),
        timeout=testing.TEST_TIMEOUT,
        log_sink=None,
        env=ANY,
    )
    assert result["passed"] is True


def test_uv_project_has_extras_true_when_declared(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "demo"\n\n[project.optional-dependencies]\ndev = ["pytest"]\n'
    )

    assert testing._uv_project_has_extras(pyproject) is True


def test_uv_project_has_extras_false_when_absent(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\n')

    assert testing._uv_project_has_extras(pyproject) is False


def test_uv_project_has_extras_false_when_file_missing(tmp_path):
    assert testing._uv_project_has_extras(tmp_path / "pyproject.toml") is False


def _make_frontend_root(tmp_path, rel_dir: str = "frontend") -> None:
    frontend_dir = tmp_path / rel_dir
    frontend_dir.mkdir(parents=True, exist_ok=True)
    (frontend_dir / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
    (frontend_dir / "src").mkdir(exist_ok=True)
    (frontend_dir / "src" / "auth.ts").write_text("export const x = 1;\n")


def test_run_tests_skips_backend_pytest_for_frontend_only_diff(tmp_path):
    _make_frontend_root(tmp_path)

    with (
        patch("hyqs.pipeline.testing.detect_test_execution") as mock_detect,
        patch("hyqs.pipeline.testing.resources.measure_subprocess") as mock_measure,
    ):
        result = asyncio.run(testing.run_tests(tmp_path, changed_files=["frontend/src/auth.ts"]))

    mock_detect.assert_not_called()
    mock_measure.assert_not_called()
    assert result == {
        "passed": True,
        "skipped": True,
        "command": "(skipped: frontend-only diff)",
        "resource": None,
    }


def test_run_tests_skips_backend_pytest_for_documentation_only_diff(tmp_path):
    (tmp_path / "docs" / "product").mkdir(parents=True)
    (tmp_path / "docs" / "product" / "roles.md").write_text("# Roles\n")

    with (
        patch("hyqs.pipeline.testing.detect_test_execution") as mock_detect,
        patch("hyqs.pipeline.testing.resources.measure_subprocess") as mock_measure,
    ):
        result = asyncio.run(testing.run_tests(tmp_path, changed_files=["docs/product/roles.md"]))

    mock_detect.assert_not_called()
    mock_measure.assert_not_called()
    assert result == {
        "passed": True,
        "skipped": True,
        "command": "(skipped: documentation-only diff)",
        "resource": None,
    }


def test_run_tests_does_not_skip_mixed_documentation_and_python_diff(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_widget.py").write_text("def test_placeholder(): pass\n")

    with (
        patch(
            "hyqs.pipeline.testing.detect_test_execution",
            return_value=testing.TestExecution(["pytest", "-q"], tmp_path),
        ),
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            new=AsyncMock(return_value=(0, "ok", None)),
        ) as mock_measure,
    ):
        result = asyncio.run(
            testing.run_tests(
                tmp_path,
                changed_files=["docs/widget.md", "src/widget.py"],
            )
        )

    mock_measure.assert_awaited_once_with(
        ["pytest", "-q", "tests/test_widget.py"],
        cwd=str(tmp_path),
        timeout=testing.TEST_TIMEOUT,
        log_sink=None,
        env=ANY,
    )
    assert result["passed"] is True


def test_run_tests_scopes_pytest_to_matching_backend_tests(tmp_path):
    test_dir = tmp_path / "backend" / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_auth_router.py").write_text("def test_placeholder(): pass\n")
    (test_dir / "test_unrelated.py").write_text("def test_placeholder(): pass\n")
    with (
        patch(
            "hyqs.pipeline.testing.detect_test_execution",
            return_value=testing.TestExecution(["pytest", "-q"], tmp_path),
        ) as mock_detect,
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            new=AsyncMock(return_value=(0, "ok", None)),
        ) as mock_measure,
    ):
        result = asyncio.run(
            testing.run_tests(tmp_path, changed_files=["backend/acme/auth/router.py"])
        )

    mock_detect.assert_called_once()
    mock_measure.assert_awaited_once_with(
        ["pytest", "-q", "backend/tests/unit/test_auth_router.py"],
        cwd=str(tmp_path),
        timeout=testing.TEST_TIMEOUT,
        log_sink=None,
        env=ANY,
    )
    assert result["passed"] is True
    assert result.get("skipped") is not True


def test_run_tests_runs_full_suite_when_shared_config_changed_alongside_frontend(tmp_path):
    _make_frontend_root(tmp_path)

    with (
        patch(
            "hyqs.pipeline.testing.detect_test_execution",
            return_value=testing.TestExecution(["pytest", "-q"], tmp_path),
        ) as mock_detect,
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            new=AsyncMock(return_value=(0, "ok", None)),
        ) as mock_measure,
    ):
        result = asyncio.run(
            testing.run_tests(
                tmp_path,
                changed_files=["frontend/src/auth.ts", "docker-compose.yml"],
            )
        )

    mock_detect.assert_called_once()
    mock_measure.assert_called_once()
    assert result.get("skipped") is not True


def test_run_tests_includes_changed_test_and_matching_source_test(tmp_path):
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_client.py").write_text("def test_placeholder(): pass\n")
    (test_dir / "test_explicit.py").write_text("def test_placeholder(): pass\n")

    with (
        patch(
            "hyqs.pipeline.testing.detect_test_execution",
            return_value=testing.TestExecution(["pytest", "-q"], tmp_path),
        ),
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            new=AsyncMock(return_value=(0, "ok", None)),
        ) as mock_measure,
    ):
        asyncio.run(
            testing.run_tests(
                tmp_path,
                changed_files=["src/client.py", "tests/test_explicit.py"],
            )
        )

    assert mock_measure.await_args.args[0] == [
        "pytest",
        "-q",
        "tests/test_client.py",
        "tests/test_explicit.py",
    ]


def test_run_tests_scopes_migration_change_to_contract_tests(tmp_path):
    migration = tmp_path / "backend" / "alembic" / "versions"
    migration.mkdir(parents=True)
    (migration / "0042_widget.py").write_text('revision = "0042"\n')
    test_dir = tmp_path / "backend" / "tests" / "unit"
    test_dir.mkdir(parents=True)
    for name in (
        "test_migrations.py",
        "test_baseline_migration.py",
        "test_widget.py",
        "test_mailer.py",
    ):
        (test_dir / name).write_text("def test_placeholder(): pass\n")

    selected = testing._scoped_pytest_files(tmp_path, ["backend/alembic/versions/0042_widget.py"])

    assert selected == [
        "backend/tests/unit/test_baseline_migration.py",
        "backend/tests/unit/test_migrations.py",
        "backend/tests/unit/test_widget.py",
    ]


def test_run_tests_falls_back_to_full_suite_when_no_test_matches(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_other.py").write_text("def test_placeholder(): pass\n")

    assert testing._scoped_pytest_files(tmp_path, ["src/widget.py"]) is None


def test_deployment_artifacts_use_explicit_contract_test_during_iteration(tmp_path):
    deploy = tmp_path / "deploy" / "vps"
    deploy.mkdir(parents=True)
    (deploy / "deploy.sh").write_text("#!/bin/sh\n")
    tests = tmp_path / "backend" / "tests" / "unit"
    tests.mkdir(parents=True)
    (tests / "test_docker_compose.py").write_text("def test_contract(): pass\n")

    selected = testing._scoped_pytest_files(
        tmp_path,
        ["deploy/vps/deploy.sh", "backend/tests/unit/test_docker_compose.py"],
    )

    assert selected == ["backend/tests/unit/test_docker_compose.py"]


def test_non_deployment_artifact_still_forces_full_suite(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "runtime.yml").write_text("enabled: true\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_runtime.py").write_text("def test_contract(): pass\n")

    assert (
        testing._scoped_pytest_files(
            tmp_path,
            ["config/runtime.yml", "tests/test_runtime.py"],
        )
        is None
    )


def test_run_tests_without_changed_files_runs_full_suite_unchanged(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    with (
        patch(
            "hyqs.pipeline.testing.detect_test_execution",
            return_value=testing.TestExecution(["pytest", "-q"], tmp_path),
        ) as mock_detect,
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            new=AsyncMock(return_value=(0, "ok", None)),
        ) as mock_measure,
    ):
        result = asyncio.run(testing.run_tests(tmp_path))

    mock_detect.assert_called_once()
    mock_measure.assert_called_once()
    assert result["passed"] is True
    assert result.get("skipped") is not True


def test_run_tests_classifies_subprocess_timeout(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    with (
        patch(
            "hyqs.pipeline.testing.detect_test_execution",
            return_value=testing.TestExecution(["pytest", "-q"], tmp_path),
        ),
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            new=AsyncMock(
                return_value=(
                    1,
                    f"tests reached 91%\n[timed out after {testing.TEST_TIMEOUT}s]",
                    None,
                )
            ),
        ),
    ):
        result = asyncio.run(testing.run_tests(tmp_path))

    assert result["passed"] is False
    assert result["timed_out"] is True


def test_run_tests_missing_worktree_returns_typed_infrastructure_failure(tmp_path):
    vanished = tmp_path / "does-not-exist"

    result = asyncio.run(testing.run_tests(vanished))

    assert result["passed"] is False
    assert result["command"] == "(none)"
    assert result["summary"].startswith("[worktree-missing]")


def test_run_tests_empty_worktree_returns_typed_infrastructure_failure(tmp_path):
    result = asyncio.run(testing.run_tests(tmp_path))

    assert result["passed"] is False
    assert result["command"] == "(none)"
    assert result["summary"].startswith("[worktree-missing]")


def test_run_tests_no_test_suite_message_unchanged_for_populated_worktree(tmp_path):
    (tmp_path / "README.md").write_text("not a test project")

    with patch("hyqs.pipeline.testing.detect_test_execution", return_value=None):
        result = asyncio.run(testing.run_tests(tmp_path))

    assert result["passed"] is False
    assert result["command"] == "(none)"
    assert result["summary"] == "No test suite detected — refusing to merge untested code."


# --- subprocess env scoping: worker secrets must never reach job-controlled code --


def test_run_tests_scopes_subprocess_env_to_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-xxx")  # pragma: allowlist secret
    monkeypatch.setenv("HYQS_DB_URL", "postgres://host/db")  # pragma: allowlist secret
    monkeypatch.setenv("DATABASE_URL", "postgres://host/db")  # pragma: allowlist secret
    monkeypatch.setenv("HYQS_TEST_DB_URL", "postgres://host/disposable_test_db")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    captured_env: dict[str, str] = {}

    async def fake_measure_subprocess(cmd, *, cwd, timeout, log_sink=None, env=None):
        captured_env.update(env or {})
        return 0, "ok", None

    with (
        patch(
            "hyqs.pipeline.testing.detect_test_execution",
            return_value=testing.TestExecution(["pytest", "-q"], tmp_path),
        ),
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            side_effect=fake_measure_subprocess,
        ),
    ):
        asyncio.run(testing.run_tests(tmp_path))

    assert captured_env.get("PATH") == "/usr/bin"
    assert "ANTHROPIC_API_KEY" not in captured_env
    # The production DSN must never reach the job-controlled subprocess (job
    # #4308); only the pre-scoped, disposable test DB URL is forwarded.
    assert "HYQS_DB_URL" not in captured_env
    assert "DATABASE_URL" not in captured_env
    assert captured_env.get("HYQS_TEST_DB_URL") == "postgres://host/disposable_test_db"


class _FakeDbConn:
    """Records executed SQL instead of hitting a real Postgres server."""

    def __init__(self, calls: list[str]):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, *args):
        self._calls.append(sql)


def _run_tests_with_fake_db(tmp_path, monkeypatch, *, measure_side_effect):
    """Run testing.run_tests() with psycopg.connect faked; return (env, sql calls)."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("HYQS_TEST_DB_URL", raising=False)
    monkeypatch.setenv("HYQS_DB_URL", _FAKE_WORKER_DSN)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    captured_env: dict[str, str] = {}
    sql_calls: list[str] = []

    def fake_connect(dsn, autocommit=False):
        assert autocommit is True
        return _FakeDbConn(sql_calls)

    async def fake_measure_subprocess(cmd, *, cwd, timeout, log_sink=None, env=None):
        captured_env.update(env or {})
        if isinstance(measure_side_effect, Exception):
            raise measure_side_effect
        return measure_side_effect

    with (
        patch(
            "hyqs.pipeline.testing.detect_test_execution",
            return_value=testing.TestExecution(["pytest", "-q"], tmp_path),
        ),
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            side_effect=fake_measure_subprocess,
        ),
        patch("hyqs.pipeline.testing.psycopg.connect", side_effect=fake_connect),
    ):
        asyncio.run(testing.run_tests(tmp_path))

    return captured_env, sql_calls


def test_run_tests_provisions_isolated_database_when_only_hyqs_db_url_set(tmp_path, monkeypatch):
    captured_env, sql_calls = _run_tests_with_fake_db(
        tmp_path, monkeypatch, measure_side_effect=(0, "ok", None)
    )

    assert "HYQS_DB_URL" not in captured_env
    assert "DATABASE_URL" not in captured_env
    isolated_dsn = captured_env.get("HYQS_TEST_DB_URL")
    assert isolated_dsn is not None
    assert isolated_dsn != _FAKE_WORKER_DSN
    assert any(sql.startswith("CREATE DATABASE") for sql in sql_calls)


def test_run_tests_drops_disposable_database_after_passing_run(tmp_path, monkeypatch):
    _captured_env, sql_calls = _run_tests_with_fake_db(
        tmp_path, monkeypatch, measure_side_effect=(0, "ok", None)
    )

    assert any(sql.startswith("DROP DATABASE") for sql in sql_calls)


def test_run_tests_drops_disposable_database_after_failing_run(tmp_path, monkeypatch):
    _captured_env, sql_calls = _run_tests_with_fake_db(
        tmp_path, monkeypatch, measure_side_effect=(1, "boom", None)
    )

    assert any(sql.startswith("CREATE DATABASE") for sql in sql_calls)
    assert any(sql.startswith("DROP DATABASE") for sql in sql_calls)


def test_run_tests_drops_disposable_database_after_subprocess_raises(tmp_path, monkeypatch):
    _captured_env, sql_calls = _run_tests_with_fake_db(
        tmp_path, monkeypatch, measure_side_effect=RuntimeError("subprocess exploded")
    )

    assert any(sql.startswith("CREATE DATABASE") for sql in sql_calls)
    assert any(sql.startswith("DROP DATABASE") for sql in sql_calls)


def test_run_tests_skips_provisioning_when_no_worker_dsn_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("HYQS_TEST_DB_URL", raising=False)
    monkeypatch.delenv("HYQS_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    captured_env: dict[str, str] = {}

    async def fake_measure_subprocess(cmd, *, cwd, timeout, log_sink=None, env=None):
        captured_env.update(env or {})
        return 0, "ok", None

    with (
        patch(
            "hyqs.pipeline.testing.detect_test_execution",
            return_value=testing.TestExecution(["pytest", "-q"], tmp_path),
        ),
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            side_effect=fake_measure_subprocess,
        ),
        patch("hyqs.pipeline.testing.psycopg.connect") as mock_connect,
    ):
        asyncio.run(testing.run_tests(tmp_path))

    assert "HYQS_TEST_DB_URL" not in captured_env
    mock_connect.assert_not_called()


def test_check_lockfile_sync_scopes_subprocess_env_to_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("POSTGRES_PASSWORD", "hostsecret")  # pragma: allowlist secret
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    (tmp_path / "uv.lock").write_text("")
    captured_env: dict[str, str] = {}

    async def fake_measure_subprocess(cmd, *, cwd, timeout, log_sink=None, env=None):
        captured_env.update(env or {})
        return 0, "ok", None

    with (
        patch("hyqs.pipeline.testing.shutil.which", side_effect=_fake_which),
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            side_effect=fake_measure_subprocess,
        ),
    ):
        result = asyncio.run(testing.check_lockfile_sync(tmp_path))

    assert result["passed"] is True
    assert captured_env.get("PATH") == "/usr/bin"
    assert "POSTGRES_PASSWORD" not in captured_env


def test_run_frontend_build_ignores_scripts_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-xxx")  # pragma: allowlist secret
    _make_frontend_root(tmp_path)
    captured_cmds: list[list[str]] = []
    captured_envs: list[dict[str, str]] = []

    async def fake_measure_subprocess(cmd, *, cwd, timeout, env=None):
        captured_cmds.append(cmd)
        captured_envs.append(env or {})
        return 0, "", None

    with (
        patch("hyqs.pipeline.testing.shutil.which", return_value="/usr/bin/npm"),
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            side_effect=fake_measure_subprocess,
        ),
    ):
        result = asyncio.run(testing.run_frontend_build(tmp_path, ["frontend/src/auth.ts"]))

    assert result["passed"] is True
    install_cmd = captured_cmds[0]
    assert install_cmd[:2] == ["npm", "ci"]
    assert "--ignore-scripts" in install_cmd
    for env in captured_envs:
        assert "ANTHROPIC_API_KEY" not in env


def test_run_frontend_build_allows_scripts_with_opt_in_marker_on_base_branch(tmp_path, monkeypatch):
    """The opt-in marker only takes effect when it already exists on the fetched
    base branch — never when merely present in the job's own worktree, since a
    malicious diff could otherwise add the marker itself to defeat
    --ignore-scripts before REVIEW/SECURITY ever see the diff.
    """
    monkeypatch.setenv("PATH", "/usr/bin")
    _make_frontend_root(tmp_path)
    captured_cmds: list[list[str]] = []

    async def fake_measure_subprocess(cmd, *, cwd, timeout, env=None):
        captured_cmds.append(cmd)
        return 0, "", None

    async def fake_git(repo, *args, timeout=None):
        assert args == ("cat-file", "-e", "main:frontend/.hyqs/allow-npm-scripts")
        return testing.gitops.GitResult(ok=True, stdout="", stderr="", code=0)

    with (
        patch("hyqs.pipeline.testing.shutil.which", return_value="/usr/bin/npm"),
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            side_effect=fake_measure_subprocess,
        ),
        patch("hyqs.pipeline.testing.gitops.default_branch", return_value="main"),
        patch("hyqs.pipeline.testing.gitops.fresh_base", return_value="main"),
        patch("hyqs.pipeline.testing.gitops.git", side_effect=fake_git),
    ):
        result = asyncio.run(testing.run_frontend_build(tmp_path, ["frontend/src/auth.ts"]))

    assert result["passed"] is True
    install_cmd = captured_cmds[0]
    assert "--ignore-scripts" not in install_cmd


def test_run_frontend_build_ignores_worktree_only_marker_not_on_base_branch(tmp_path, monkeypatch):
    """A marker file added in the job's own worktree (not yet on the base
    branch) must not enable lifecycle scripts — that's exactly the bypass a
    malicious diff would attempt.
    """
    monkeypatch.setenv("PATH", "/usr/bin")
    _make_frontend_root(tmp_path)
    marker_dir = tmp_path / "frontend" / ".hyqs"
    marker_dir.mkdir()
    (marker_dir / "allow-npm-scripts").write_text("")
    captured_cmds: list[list[str]] = []

    async def fake_measure_subprocess(cmd, *, cwd, timeout, env=None):
        captured_cmds.append(cmd)
        return 0, "", None

    async def fake_git(repo, *args, timeout=None):
        return testing.gitops.GitResult(ok=False, stdout="", stderr="not found", code=1)

    with (
        patch("hyqs.pipeline.testing.shutil.which", return_value="/usr/bin/npm"),
        patch(
            "hyqs.pipeline.testing.resources.measure_subprocess",
            side_effect=fake_measure_subprocess,
        ),
        patch("hyqs.pipeline.testing.gitops.default_branch", return_value="main"),
        patch("hyqs.pipeline.testing.gitops.fresh_base", return_value="main"),
        patch("hyqs.pipeline.testing.gitops.git", side_effect=fake_git),
    ):
        result = asyncio.run(testing.run_frontend_build(tmp_path, ["frontend/src/auth.ts"]))

    assert result["passed"] is True
    install_cmd = captured_cmds[0]
    assert "--ignore-scripts" in install_cmd


def test_benign_import_env_excludes_non_allowlisted_host_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-xxx")  # pragma: allowlist secret
    monkeypatch.setenv("HYQS_DB_URL", "postgres://host/db")

    env = testing._benign_import_env(tmp_path)

    assert env.get("PATH") == "/usr/bin"
    assert "ANTHROPIC_API_KEY" not in env
    assert "HYQS_DB_URL" not in env
    assert env["SECRET_KEY"] == "hyqs-import-smoke-placeholder"  # pragma: allowlist secret
