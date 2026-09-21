"""Tests for the gate-fix remediation path in the supervisor janitor.

Uses AsyncMock/MagicMock — no Postgres required.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import Job, JobSource, JobStatus, Stage
from hyqs.pipeline.supervisor import remediate_gate_no_changes

_COLLISION_MSG = (
    "Symbol 'foo' (function) in module 'bar' duplicates existing symbol "
    "in 'baz' — signature: def foo():"
)


def _make_job(
    job_id: int = 1,
    stage: Stage = Stage.BUILD,
    failure: str = "[symbol-collision] found 1 conflict",
    project_id: int | None = 10,
    chat_id: int = 5,
    repo_path: str = "/fake/repo",
    owner: str = "host:123:0",
    idea: str = "Add a foo() helper function",
    priority: int = 17,
) -> Job:
    return Job(
        id=job_id,
        idea=idea,
        repo_path=repo_path,
        chat_id=chat_id,
        stage=stage,
        status=JobStatus.FAILED,
        failure=failure,
        project_id=project_id,
        owner=owner,
        priority=priority,
    )


def _make_jobs_store(requeue_count: int = 0, is_notified: bool = False) -> MagicMock:
    gate_fix_job = MagicMock()
    gate_fix_job.id = 99

    jobs = MagicMock()
    jobs.supervisor_requeue_count.return_value = requeue_count
    jobs.is_supervisor_notified.return_value = is_notified
    jobs.create.return_value = gate_fix_job
    jobs.mark_supervisor_notified = MagicMock()
    jobs.record_supervisor_event = MagicMock()
    jobs.increment_supervisor_requeue = MagicMock()
    jobs.requeue_job_at_stage = MagicMock()
    jobs.add_job_dependency = MagicMock()
    return jobs


@patch("hyqs.pipeline.supervisor.collision.check_symbol_collisions")
@patch("hyqs.pipeline.supervisor.gitops.numstat", new_callable=AsyncMock)
@patch("hyqs.pipeline.supervisor.gitops.git", new_callable=AsyncMock)
@patch("hyqs.pipeline.supervisor.gitops.fresh_base", new_callable=AsyncMock)
@patch("hyqs.pipeline.supervisor.gitops.default_branch", new_callable=AsyncMock)
def test_gate_fix_idea_includes_finding_detail(
    mock_default_branch, mock_fresh_base, mock_git, mock_numstat, mock_check, tmp_path
):
    job = _make_job()
    (tmp_path / "worktrees" / f"job-{job.id}").mkdir(parents=True)
    mock_default_branch.return_value = "main"
    mock_fresh_base.return_value = "main"
    mock_git.return_value = GitResult(ok=True, stdout="abc123\n", stderr="", code=0)
    mock_numstat.return_value = [{"path": "hyqs/pipeline/bar.py"}]
    mock_check.return_value = [_COLLISION_MSG]
    jobs = _make_jobs_store()
    notify = AsyncMock()
    config = MagicMock()
    config.data_dir = tmp_path

    asyncio.run(remediate_gate_no_changes(jobs, job, config, notify))

    jobs.create.assert_called_once()
    call_kwargs = jobs.create.call_args.kwargs
    assert _COLLISION_MSG in call_kwargs["idea"]
    assert call_kwargs["repo_path"] == job.repo_path
    assert call_kwargs["priority"] == job.priority
    assert call_kwargs["source"] == JobSource.SUPERVISOR
    assert call_kwargs["source_meta"]["kind"] == "gate-fix"
    jobs.add_job_dependency.assert_called_once_with(job.id, 99)
    jobs.requeue_job_at_stage.assert_called_once_with(job.id, Stage.BUILD)


@patch("hyqs.pipeline.supervisor.collision.check_symbol_collisions")
@patch("hyqs.pipeline.supervisor.gitops.numstat", new_callable=AsyncMock)
@patch("hyqs.pipeline.supervisor.gitops.git", new_callable=AsyncMock)
@patch("hyqs.pipeline.supervisor.gitops.fresh_base", new_callable=AsyncMock)
@patch("hyqs.pipeline.supervisor.gitops.default_branch", new_callable=AsyncMock)
def test_gate_fix_suppresses_when_not_attributable(
    mock_default_branch, mock_fresh_base, mock_git, mock_numstat, mock_check, tmp_path
):
    job = _make_job()
    (tmp_path / "worktrees" / f"job-{job.id}").mkdir(parents=True)
    mock_default_branch.return_value = "main"
    mock_fresh_base.return_value = "main"
    mock_git.return_value = GitResult(ok=True, stdout="abc123\n", stderr="", code=0)
    mock_numstat.return_value = [{"path": "hyqs/pipeline/bar.py"}]
    mock_check.return_value = []
    jobs = _make_jobs_store()
    notify = AsyncMock()
    config = MagicMock()
    config.data_dir = tmp_path

    asyncio.run(remediate_gate_no_changes(jobs, job, config, notify))

    jobs.create.assert_not_called()
    jobs.requeue_job_at_stage.assert_called_once_with(job.id, Stage.BUILD)


# job #2140: the symbol-collision gate re-check must be diff-aware too — the
# janitor's re-check re-derives the job's live changed-files set the same way
# the adjacent merge-delta branch does, so a job is never blamed for a
# collision it never touched.


@patch("hyqs.pipeline.supervisor.collision.check_symbol_collisions")
@patch("hyqs.pipeline.supervisor.gitops.numstat")
@patch("hyqs.pipeline.supervisor.gitops.git")
@patch("hyqs.pipeline.supervisor.gitops.fresh_base")
@patch("hyqs.pipeline.supervisor.gitops.default_branch")
def test_symbol_collision_recheck_passes_live_diff_to_check(
    mock_default_branch, mock_fresh_base, mock_git, mock_numstat, mock_check, tmp_path
):
    job = _make_job()
    (tmp_path / "worktrees" / f"job-{job.id}").mkdir(parents=True)
    mock_default_branch.return_value = "main"
    mock_fresh_base.return_value = "main"
    mock_git.return_value = GitResult(ok=True, stdout="abc123\n", stderr="", code=0)
    diff_files = ["hyqs/pipeline/new_module.py", "tests/test_new_module.py"]
    mock_numstat.return_value = [{"path": p} for p in diff_files]
    mock_check.return_value = []
    jobs = _make_jobs_store()
    notify = AsyncMock()
    config = MagicMock()
    config.data_dir = tmp_path

    asyncio.run(remediate_gate_no_changes(jobs, job, config, notify))

    mock_check.assert_called_once()
    assert mock_check.call_args.kwargs["changed_files"] == diff_files
    jobs.requeue_job_at_stage.assert_called_once_with(job.id, Stage.BUILD)


@patch("hyqs.pipeline.supervisor.collision.check_symbol_collisions")
@patch("hyqs.pipeline.supervisor.gitops.git")
@patch("hyqs.pipeline.supervisor.gitops.fresh_base")
@patch("hyqs.pipeline.supervisor.gitops.default_branch")
def test_symbol_collision_recheck_treats_merge_base_failure_as_non_attributable(
    mock_default_branch, mock_fresh_base, mock_git, mock_check, tmp_path
):
    job = _make_job()
    (tmp_path / "worktrees" / f"job-{job.id}").mkdir(parents=True)
    mock_default_branch.return_value = "main"
    mock_fresh_base.return_value = "main"
    mock_git.return_value = GitResult(ok=False, stdout="", stderr="no merge base", code=1)
    jobs = _make_jobs_store()
    notify = AsyncMock()
    config = MagicMock()
    config.data_dir = tmp_path

    asyncio.run(remediate_gate_no_changes(jobs, job, config, notify))

    mock_check.assert_not_called()
    jobs.create.assert_not_called()
    jobs.requeue_job_at_stage.assert_called_once_with(job.id, Stage.BUILD)
