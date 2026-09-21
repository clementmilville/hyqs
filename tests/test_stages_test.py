"""Tests for the TEST stage's diff-aware symbol-collision gate.

No Postgres needed — mocks gitops, check_symbol_collisions, and the runner,
matching the pattern used by tests/test_stages_design_review.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.models import Job, JobStatus, Stage
from hyqs.pipeline.stages import AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT
from hyqs.pipeline.stages import test as test_stage


def _make_job(**kwargs) -> Job:
    base = dict(
        id=1,
        idea="test idea",
        repo_path="/fake/repo",
        chat_id=99,
        stage=Stage.TEST,
        status=JobStatus.RUNNING,
        project_id=1,
        attempts=0,
    )
    base.update(kwargs)
    return Job(**base)


def _make_runner(tmp_path) -> MagicMock:
    rn = MagicMock()
    rn.worktrees = tmp_path
    rn.timeout = 60
    rn.store = MagicMock()
    rn.notify = AsyncMock()
    rn._event = MagicMock()
    rn._record_resource = MagicMock()
    rn._retry_or_fail = AsyncMock()
    rn._timed_out = AsyncMock()
    return rn


def test_collision_check_called_with_changed_files_computed_before_it(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    (tmp_path / f"job-{job.id}").mkdir(parents=True)

    call_order: list[str] = []
    diff_files = ["hyqs/pipeline/new_module.py", "tests/test_new_module.py"]

    async def _fake_numstat(worktree, rng):
        call_order.append("numstat")
        return [{"path": p} for p in diff_files]

    def _fake_check(worktree, project_id, store, changed_files=None):
        call_order.append("check")
        assert changed_files == diff_files
        return ["dummy collision message"]

    with (
        patch.object(test_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "numstat", AsyncMock(side_effect=_fake_numstat)),
        patch.object(test_stage, "check_symbol_collisions", side_effect=_fake_check),
    ):
        asyncio.run(test_stage.run(rn, job))

    assert call_order == ["numstat", "check"]
    rn._retry_or_fail.assert_called_once()


def test_collision_check_gets_empty_changed_files_when_diff_computation_fails(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    (tmp_path / f"job-{job.id}").mkdir(parents=True)

    def _fake_check(worktree, project_id, store, changed_files=None):
        assert changed_files == []
        return ["dummy collision message"]

    with (
        patch.object(
            test_stage.gitops, "default_branch", AsyncMock(side_effect=RuntimeError("boom"))
        ),
        patch.object(test_stage, "check_symbol_collisions", side_effect=_fake_check),
    ):
        asyncio.run(test_stage.run(rn, job))

    rn._retry_or_fail.assert_called_once()


def test_alembic_heads_check_fails_job_before_tests_run(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    (tmp_path / f"job-{job.id}").mkdir(parents=True)

    diff_files = ["alembic/versions/b.py"]

    async def _fake_numstat(worktree, rng):
        return [{"path": p} for p in diff_files]

    with (
        patch.object(test_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "numstat", AsyncMock(side_effect=_fake_numstat)),
        patch.object(test_stage, "check_symbol_collisions", return_value=[]),
        patch.object(
            test_stage,
            "check_alembic_head_collisions",
            return_value=["'alembic/versions' has 2 alembic heads"],
        ),
        patch.object(test_stage.testing, "run_import_smoke") as run_import_smoke,
    ):
        asyncio.run(test_stage.run(rn, job))

    rn._retry_or_fail.assert_called_once()
    run_import_smoke.assert_not_called()


def test_alembic_heads_check_passes_when_no_collisions(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    (tmp_path / f"job-{job.id}").mkdir(parents=True)

    diff_files = ["alembic/versions/b.py"]

    async def _fake_numstat(worktree, rng):
        return [{"path": p} for p in diff_files]

    async def _fake_run_tests(worktree, *, changed_files=None, log_sink=None):
        return {"passed": True, "command": "pytest -q", "summary": "ok", "resource": None}

    with (
        patch.object(test_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "numstat", AsyncMock(side_effect=_fake_numstat)),
        patch.object(test_stage, "check_symbol_collisions", return_value=[]),
        patch.object(test_stage, "check_alembic_head_collisions", return_value=[]),
        patch.object(
            test_stage.testing,
            "run_import_smoke",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(
            test_stage.testing,
            "run_frontend_build",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(test_stage.invariants, "run_invariants", AsyncMock(return_value=[])),
        patch.object(test_stage.testing, "run_tests", side_effect=_fake_run_tests),
    ):
        asyncio.run(test_stage.run(rn, job))

    rn._retry_or_fail.assert_not_called()


def test_alembic_heads_check_is_noop_when_diff_touches_no_alembic_files(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    (tmp_path / f"job-{job.id}").mkdir(parents=True)

    diff_files = ["hyqs/pipeline/new_module.py"]

    async def _fake_numstat(worktree, rng):
        return [{"path": p} for p in diff_files]

    async def _fake_run_tests(worktree, *, changed_files=None, log_sink=None):
        return {"passed": True, "command": "pytest -q", "summary": "ok", "resource": None}

    with (
        patch.object(test_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "numstat", AsyncMock(side_effect=_fake_numstat)),
        patch.object(test_stage, "check_symbol_collisions", return_value=[]),
        patch.object(
            test_stage.testing,
            "run_import_smoke",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(
            test_stage.testing,
            "run_frontend_build",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(test_stage.invariants, "run_invariants", AsyncMock(return_value=[])),
        patch.object(test_stage.testing, "run_tests", side_effect=_fake_run_tests),
    ):
        asyncio.run(test_stage.run(rn, job))

    rn._retry_or_fail.assert_not_called()


def test_run_tests_called_with_changed_files_computed_before_it(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    (tmp_path / f"job-{job.id}").mkdir(parents=True)

    diff_files = ["hyqs/pipeline/new_module.py", "tests/test_new_module.py"]

    async def _fake_numstat(worktree, rng):
        return [{"path": p} for p in diff_files]

    async def _fake_run_tests(worktree, *, changed_files=None, log_sink=None):
        assert changed_files == diff_files
        return {"passed": True, "command": "pytest -q", "summary": "ok", "resource": None}

    with (
        patch.object(test_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "numstat", AsyncMock(side_effect=_fake_numstat)),
        patch.object(
            test_stage.testing,
            "run_import_smoke",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(
            test_stage.testing,
            "run_frontend_build",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(test_stage, "check_symbol_collisions", return_value=[]),
        patch.object(test_stage.invariants, "run_invariants", AsyncMock(return_value=[])),
        patch.object(test_stage.testing, "run_tests", side_effect=_fake_run_tests),
    ):
        asyncio.run(test_stage.run(rn, job))

    rn._retry_or_fail.assert_not_called()


def test_run_tests_gets_empty_changed_files_when_diff_computation_fails(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    (tmp_path / f"job-{job.id}").mkdir(parents=True)

    async def _fake_run_tests(worktree, *, changed_files=None, log_sink=None):
        assert changed_files == []
        return {"passed": True, "command": "pytest -q", "summary": "ok", "resource": None}

    with (
        patch.object(
            test_stage.gitops, "default_branch", AsyncMock(side_effect=RuntimeError("boom"))
        ),
        patch.object(
            test_stage.testing,
            "run_import_smoke",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(
            test_stage.testing,
            "run_frontend_build",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(test_stage, "check_symbol_collisions", return_value=[]),
        patch.object(test_stage.invariants, "run_invariants", AsyncMock(return_value=[])),
        patch.object(test_stage.testing, "run_tests", side_effect=_fake_run_tests),
    ):
        asyncio.run(test_stage.run(rn, job))

    rn._retry_or_fail.assert_not_called()


def test_import_smoke_failure_shares_authorized_evidence_with_retry(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    (tmp_path / f"job-{job.id}").mkdir()
    changed = ["src/main.py", "tests/test_main.py"]

    with (
        patch.object(test_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(
            test_stage.gitops,
            "numstat",
            AsyncMock(return_value=[{"path": path} for path in changed]),
        ),
        patch.object(test_stage, "check_symbol_collisions", return_value=[]),
        patch.object(test_stage, "check_alembic_head_collisions", return_value=[]),
        patch.object(
            test_stage.testing,
            "run_import_smoke",
            AsyncMock(
                return_value={
                    "passed": False,
                    "command": "python -c import-main",
                    "summary": "broken import",
                }
            ),
        ),
    ):
        asyncio.run(test_stage.run(rn, job))

    event_detail = rn._event.call_args.kwargs["detail"]
    retry_detail = rn._retry_or_fail.call_args.kwargs["failure_detail"]
    assert event_detail == retry_detail
    assert event_detail["authorized_gate_failure"] == {
        "gate": "test",
        "check_id": "test.import-smoke",
        "event_id": "test.import-smoke",
        "failing_paths": changed,
        "categories": ["imports"],
    }
    assert event_detail["command"] == "python -c import-main"


def test_test_failure_with_excessive_paths_is_non_authorizing(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    (tmp_path / f"job-{job.id}").mkdir()
    changed = [f"src/file_{number}.py" for number in range(AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT + 1)]

    with (
        patch.object(test_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(
            test_stage.gitops,
            "numstat",
            AsyncMock(return_value=[{"path": path} for path in changed]),
        ),
        patch.object(test_stage, "check_symbol_collisions", return_value=[]),
        patch.object(test_stage, "check_alembic_head_collisions", return_value=[]),
        patch.object(
            test_stage.testing,
            "run_import_smoke",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(
            test_stage.testing,
            "run_frontend_build",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(
            test_stage.testing,
            "run_tests",
            AsyncMock(
                return_value={
                    "passed": False,
                    "command": "pytest",
                    "summary": "src/not-authority.py failed",
                    "output": "traceback",
                    "resource": None,
                }
            ),
        ),
    ):
        asyncio.run(test_stage.run(rn, job))

    detail = rn._retry_or_fail.call_args.kwargs["failure_detail"]
    assert "authorized_gate_failure" not in detail
    assert detail == {
        "command": "pytest",
        "output": "traceback",
        "check_id": "test.execution",
        "event_id": "test.execution",
    }


def test_test_timeout_uses_timeout_budget_instead_of_fix_loop(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    (tmp_path / f"job-{job.id}").mkdir()

    with (
        patch.object(test_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(test_stage.gitops, "numstat", AsyncMock(return_value=[])),
        patch.object(test_stage, "check_symbol_collisions", return_value=[]),
        patch.object(test_stage, "check_alembic_head_collisions", return_value=[]),
        patch.object(
            test_stage.testing,
            "run_import_smoke",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(
            test_stage.testing,
            "run_frontend_build",
            AsyncMock(return_value={"passed": True, "skipped": True}),
        ),
        patch.object(
            test_stage.testing,
            "run_tests",
            AsyncMock(
                return_value={
                    "passed": False,
                    "timed_out": True,
                    "command": "pytest -q",
                    "summary": "[timed out after 900s]",
                    "output": "[timed out after 900s]",
                    "resource": None,
                }
            ),
        ),
    ):
        asyncio.run(test_stage.run(rn, job))

    rn._timed_out.assert_awaited_once_with(job, timeout_seconds=test_stage.testing.TEST_TIMEOUT)
    rn._retry_or_fail.assert_not_awaited()
