import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline import linting
from hyqs.pipeline.linting import _is_tool_failure
from hyqs.pipeline.models import Job, JobStatus, Stage
from hyqs.pipeline.stages import lint as lint_stage


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")


def test_is_tool_failure_true_for_missing_ruff_executable():
    output = "Failed to find executable ruff: No such file or directory\nRunning as unit: ..."
    assert _is_tool_failure(1, output) is True


def test_is_tool_failure_true_for_existing_marker_regression():
    output = "bash: ruff: command not found"
    assert _is_tool_failure(1, output) is True


def test_is_tool_failure_false_for_genuine_violation():
    output = "file.py:1:1: E501 line too long"
    assert _is_tool_failure(1, output) is False


def test_run_cmd_scopes_subprocess_env_to_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-xxx")  # pragma: allowlist secret
    monkeypatch.setenv("HYQS_DB_URL", "postgres://host/db")

    captured_env: dict[str, str] = {}

    async def fake_measure_subprocess(cmd, cwd, timeout, log_sink=None, env=None):
        captured_env.update(env or {})
        return 0, "ok", None

    with patch.object(linting.resources, "measure_subprocess", fake_measure_subprocess):
        exit_code, out, _rec = asyncio.run(
            linting._run_cmd(["ruff", "check", "."], cwd=str(tmp_path))
        )

    assert exit_code == 0
    assert captured_env.get("PATH") == "/usr/bin"
    assert "ANTHROPIC_API_KEY" not in captured_env
    assert "HYQS_DB_URL" not in captured_env


def test_lockfile_drift_shares_manifest_evidence_with_retry(tmp_path):
    worktree = tmp_path / "job-1"
    worktree.mkdir()
    (worktree / "pyproject.toml").write_text("[project]\n")
    (worktree / "uv.lock").write_text("")
    job = Job(
        id=1,
        idea="idea",
        repo_path="/repo",
        chat_id=1,
        stage=Stage.LINT,
        status=JobStatus.RUNNING,
    )
    rn = MagicMock()
    rn.worktrees = tmp_path
    rn._managed_repo = AsyncMock(return_value="/repo")
    rn._retry_or_fail = AsyncMock()

    with patch.object(
        lint_stage.testing,
        "check_lockfile_sync",
        AsyncMock(
            return_value={
                "passed": False,
                "command": "uv lock --check",
                "summary": "drift",
            }
        ),
    ):
        asyncio.run(lint_stage.run(rn, job))

    event_detail = rn._event.call_args.kwargs["detail"]
    assert event_detail == rn._retry_or_fail.call_args.kwargs["failure_detail"]
    assert event_detail["check_id"] == "lint.lockfile-drift"
    assert event_detail["event_id"] == "lint.lockfile-drift"
    assert "authorized_gate_failure" not in event_detail


def test_lint_failure_uses_changed_files_not_diagnostic_prose(tmp_path):
    worktree = tmp_path / "job-1"
    worktree.mkdir()
    job = Job(
        id=1,
        idea="idea",
        repo_path="/repo",
        chat_id=1,
        stage=Stage.LINT,
        status=JobStatus.RUNNING,
    )
    rn = MagicMock()
    rn.worktrees = tmp_path
    rn._managed_repo = AsyncMock(return_value="/repo")
    rn._retry_or_fail = AsyncMock()
    rn._record_resource = MagicMock()
    changed = ["src/app.py"]

    with (
        patch.object(
            lint_stage.testing,
            "check_lockfile_sync",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(lint_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(lint_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(lint_stage.gitops, "numstat", AsyncMock(return_value=[{"path": changed[0]}])),
        patch.object(
            lint_stage.linting,
            "run_lint",
            AsyncMock(
                return_value={
                    "passed": False,
                    "auto_fixed": False,
                    "summary": "../unsafe.py: lint error",
                    "output": "../unsafe.py:1:1 error",
                    "lint_commands": [["ruff", "check", "src/app.py"]],
                    "resource": None,
                }
            ),
        ),
    ):
        asyncio.run(lint_stage.run(rn, job))

    detail = rn._retry_or_fail.call_args.kwargs["failure_detail"]
    assert detail["authorized_gate_failure"]["failing_paths"] == changed
    assert detail["output"] == "../unsafe.py:1:1 error"


# --- ruff safe-fix pass: command order, scoping, --unsafe-fixes ban ------


def test_detect_entries_command_order_and_no_unsafe_fixes(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
    (tmp_path / "app.py").write_text("x = 1\n")

    entries = linting._detect_entries(tmp_path, changed_files=["app.py"])

    assert len(entries) == 1
    entry = entries[0]
    py_path = str(tmp_path / "app.py")
    assert len(entry["format"]) == 1
    assert entry["format"][0][-2:] == ["format", py_path]
    assert entry["fix"][0][-3:] == ["check", "--fix", py_path]
    assert entry["lint"][0][-2:] == ["check", py_path]
    for cmd in (*entry["format"], *entry["fix"], *entry["lint"]):
        assert "--unsafe-fixes" not in cmd


def test_detect_entries_fix_scoped_to_changed_python_files(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "other.py").write_text("y = 2\n")

    entries = linting._detect_entries(tmp_path, changed_files=["app.py"])

    fix_cmd = entries[0]["fix"][0]
    assert str(tmp_path / "app.py") in fix_cmd
    assert str(tmp_path / "other.py") not in fix_cmd


def test_detect_entries_fix_empty_when_no_python_files_changed(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
    (tmp_path / "app.py").write_text("x = 1\n")

    entries = linting._detect_entries(tmp_path, changed_files=["README.md"])

    assert entries == []


def test_run_lint_issues_format_fix_lint_commands_in_order(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    calls: list[list[str]] = []

    async def fake_run_cmd(cmd, *, cwd, log_sink=None):
        calls.append(cmd)
        return 0, "", None

    with patch.object(linting, "_run_cmd", fake_run_cmd):
        result = asyncio.run(linting.run_lint(str(tmp_path), changed_files=["app.py"]))

    assert len(calls) == 3
    py_path = str(tmp_path / "app.py")
    assert calls[0][-2:] == ["format", py_path]
    assert calls[1][-3:] == ["check", "--fix", py_path]
    assert calls[2][-2:] == ["check", py_path]
    assert not any("--unsafe-fixes" in c for c in calls)
    assert result["fix_commands"] == [calls[1]]


def test_run_lint_detects_fix_only_mutation_via_git_status(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "pyproject.toml").write_text("[tool.ruff]\n")
    (repo / "app.py").write_text("import os\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")

    async def fake_run_cmd(cmd, *, cwd, log_sink=None):
        # Simulate `ruff check --fix` silently applying a safe fix (e.g. an
        # import-only fix) that doesn't print a "reformatted" marker.
        if "--fix" in cmd:
            (repo / "app.py").write_text("import os  # fixed\n")
        return 0, "", None

    with patch.object(linting, "_run_cmd", fake_run_cmd):
        result = asyncio.run(linting.run_lint(str(repo), changed_files=["app.py"]))

    assert result["auto_fixed"] is True


def test_run_lint_auto_fixed_false_when_no_mutation(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "pyproject.toml").write_text("[tool.ruff]\n")
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")

    async def fake_run_cmd(cmd, *, cwd, log_sink=None):
        return 0, "", None

    with patch.object(linting, "_run_cmd", fake_run_cmd):
        result = asyncio.run(linting.run_lint(str(repo), changed_files=["app.py"]))

    assert result["auto_fixed"] is False


# --- lint stage: auto_fixed drives commit + force-push --------------------


def test_lint_stage_commits_and_force_pushes_on_auto_fixed(tmp_path):
    worktree = tmp_path / "job-7"
    worktree.mkdir()
    job = Job(
        id=7,
        idea="idea",
        repo_path="/repo",
        chat_id=1,
        branch="job-7",
        stage=Stage.LINT,
        status=JobStatus.RUNNING,
    )
    rn = MagicMock()
    rn.worktrees = tmp_path
    rn._managed_repo = AsyncMock(return_value="/repo")
    rn._record_resource = MagicMock()
    rn.notify = AsyncMock()
    changed = ["src/app.py"]

    with (
        patch.object(
            lint_stage.testing,
            "check_lockfile_sync",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(lint_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(lint_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(lint_stage.gitops, "numstat", AsyncMock(return_value=[{"path": changed[0]}])),
        patch.object(
            lint_stage.linting,
            "run_lint",
            AsyncMock(
                return_value={
                    "passed": True,
                    "auto_fixed": True,
                    "summary": "ok",
                    "output": "",
                    "lint_commands": [],
                    "resource": None,
                }
            ),
        ),
        patch.object(lint_stage.gitops, "commit_all", AsyncMock(return_value=True)) as commit_all,
        patch.object(lint_stage.github, "has_remote", AsyncMock(return_value=True)),
        patch.object(lint_stage.github, "force_push_branch", AsyncMock()) as force_push,
    ):
        asyncio.run(lint_stage.run(rn, job))

    commit_all.assert_awaited_once()
    assert commit_all.call_args.args[0] == worktree
    assert "lint auto-fix" in commit_all.call_args.args[1]
    force_push.assert_awaited_once_with(worktree, job.branch)


def test_lint_stage_still_fails_when_final_check_reports_violations_after_fix(tmp_path):
    worktree = tmp_path / "job-8"
    worktree.mkdir()
    job = Job(
        id=8,
        idea="idea",
        repo_path="/repo",
        chat_id=1,
        branch="job-8",
        stage=Stage.LINT,
        status=JobStatus.RUNNING,
    )
    rn = MagicMock()
    rn.worktrees = tmp_path
    rn._managed_repo = AsyncMock(return_value="/repo")
    rn._retry_or_fail = AsyncMock()
    rn._record_resource = MagicMock()
    changed = ["src/app.py"]

    with (
        patch.object(
            lint_stage.testing,
            "check_lockfile_sync",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(lint_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(lint_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(lint_stage.gitops, "numstat", AsyncMock(return_value=[{"path": changed[0]}])),
        patch.object(
            lint_stage.linting,
            "run_lint",
            AsyncMock(
                return_value={
                    "passed": False,
                    "auto_fixed": True,
                    "summary": "src/app.py: lint error",
                    "output": "src/app.py:1:1 error",
                    "lint_commands": [["ruff", "check", "src/app.py"]],
                    "resource": None,
                }
            ),
        ),
        patch.object(lint_stage.gitops, "commit_all", AsyncMock(return_value=True)),
        patch.object(lint_stage.github, "has_remote", AsyncMock(return_value=False)),
    ):
        asyncio.run(lint_stage.run(rn, job))

    detail = rn._retry_or_fail.call_args.kwargs["failure_detail"]
    assert detail["authorized_gate_failure"]["failing_paths"] == changed
    assert detail["output"] == "src/app.py:1:1 error"
