"""Tests for the deploy-fix remediation path in the supervisor janitor.

Uses AsyncMock/MagicMock — no Postgres required.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.models import Job, JobSource, JobStatus, Stage
from hyqs.pipeline.supervisor import _janitor_scan, remediate_deploy_failed


def _make_job(
    job_id: int = 1,
    stage: Stage = Stage.DEPLOY,
    failure: str = "module not found",
    source_meta: dict | None = None,
    project_id: int | None = 10,
    chat_id: int = 5,
    repo_path: str = "/fake/repo",
    owner: str = "host:123:0",
    idea: str = "Fix the thing",
    epic_id: int | None = None,
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
        source_meta=source_meta,
        epic_id=epic_id,
    )


def _make_jobs_store(
    has_deploy_fix: bool = False,
    deploy_fix_count: int = 0,
    is_notified: bool = False,
) -> MagicMock:
    fix_job = MagicMock()
    fix_job.id = 99

    jobs = MagicMock()
    jobs.has_deploy_fix_job.return_value = has_deploy_fix
    jobs.count_deploy_fix_jobs.return_value = deploy_fix_count
    jobs.is_supervisor_notified.return_value = is_notified
    jobs.create.return_value = fix_job
    jobs.mark_supervisor_notified = MagicMock()
    jobs.record_supervisor_event = MagicMock()
    return jobs


def test_deploy_failed_creates_fix_forward_job():
    job = _make_job()
    jobs = _make_jobs_store(has_deploy_fix=False, deploy_fix_count=0)
    notify = AsyncMock()
    config = MagicMock()

    asyncio.run(remediate_deploy_failed(jobs, job, config, notify))

    jobs.create.assert_called_once()
    call_kwargs = jobs.create.call_args.kwargs
    assert call_kwargs["source"] == JobSource.SUPERVISOR
    assert call_kwargs["source_meta"]["deploy_fix_for"] == job.id
    notify.assert_called_once()


def test_deploy_failed_is_idempotent():
    job = _make_job()
    jobs = _make_jobs_store(has_deploy_fix=True)
    notify = AsyncMock()
    config = MagicMock()

    asyncio.run(remediate_deploy_failed(jobs, job, config, notify))

    jobs.create.assert_not_called()
    notify.assert_not_called()


def test_deploy_failed_respects_project_cap():
    job = _make_job()
    jobs = _make_jobs_store(has_deploy_fix=False, deploy_fix_count=3)
    notify = AsyncMock()
    config = MagicMock()

    asyncio.run(remediate_deploy_failed(jobs, job, config, notify, project_cap=3))

    jobs.create.assert_not_called()
    notify.assert_called_once()
    assert "cap" in notify.call_args.args[1].lower()


def test_deploy_fix_job_failure_does_not_spawn():
    job = _make_job(source_meta={"deploy_fix_for": 99})
    jobs = _make_jobs_store()
    notify = AsyncMock()
    config = MagicMock()

    asyncio.run(remediate_deploy_failed(jobs, job, config, notify))

    jobs.create.assert_not_called()
    notify.assert_called_once()
    assert "chain" in notify.call_args.args[1].lower()


def test_config_deploy_triggers_fix_forward():
    # config_deploy class: infra-looking failure (no attributable pattern)
    # remediate_deploy_failed is called for both classes; verify happy-path here too
    job = _make_job(failure="permission denied accessing /var/run/docker.sock")
    jobs = _make_jobs_store(has_deploy_fix=False, deploy_fix_count=0)
    notify = AsyncMock()
    config = MagicMock()

    asyncio.run(remediate_deploy_failed(jobs, job, config, notify))

    jobs.create.assert_called_once()
    call_kwargs = jobs.create.call_args.kwargs
    assert call_kwargs["source"] == JobSource.SUPERVISOR
    assert call_kwargs["source_meta"]["deploy_fix_for"] == job.id
    notify.assert_called_once()


@patch("hyqs.pipeline.supervisor.gitops.gc_orphaned_artifacts", new_callable=AsyncMock)
def test_janitor_scan_dispatches_deploy_failed(mock_gc):
    job = _make_job(failure="module not found")  # matches attributable_deploy pattern
    jobs = _make_jobs_store(has_deploy_fix=False, deploy_fix_count=0)
    jobs.get_failed_jobs.return_value = [job]
    jobs.get_active_job_ids.return_value = set()
    notify = AsyncMock()
    config = MagicMock()

    asyncio.run(_janitor_scan(jobs, config, notify))

    jobs.create.assert_called_once()
    call_kwargs = jobs.create.call_args.kwargs
    assert call_kwargs["source_meta"]["deploy_fix_for"] == job.id
