"""Tests for the alembic multi-head deploy remediation path in the supervisor janitor.

Uses AsyncMock/MagicMock plus real tmp_path filesystem fixtures for the alembic
versions directories — no Postgres required.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.models import Job, JobSource, JobStatus, Stage
from hyqs.pipeline.supervisor import (
    FailureClass,
    _classify_deploy_failure,
    _is_alembic_multi_head,
    _janitor_scan,
    remediate_alembic_multi_head,
)


def _write_migration(versions_dir: Path, filename: str, revision: str, down_revision) -> None:
    versions_dir.mkdir(parents=True, exist_ok=True)
    (versions_dir / filename).write_text(
        f"revision = {revision!r}\ndown_revision = {down_revision!r}\n"
    )


def _make_job(
    job_id: int = 1,
    stage: Stage = Stage.DEPLOY,
    failure: str = "Multiple head revisions are present for given argument 'head'",
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
    has_fix: bool = False,
    fix_count: int = 0,
    is_notified: bool = False,
) -> MagicMock:
    fix_job = MagicMock()
    fix_job.id = 99

    jobs = MagicMock()
    jobs.has_alembic_merge_fix_job.return_value = has_fix
    jobs.count_alembic_merge_fix_jobs.return_value = fix_count
    jobs.is_supervisor_notified.return_value = is_notified
    jobs.create.return_value = fix_job
    jobs.mark_supervisor_notified = MagicMock()
    jobs.record_supervisor_event = MagicMock()
    return jobs


def test_is_alembic_multi_head_matches_case_insensitively():
    assert _is_alembic_multi_head("Multiple head revisions are present") is True
    assert _is_alembic_multi_head("MULTIPLE HEAD REVISIONS ARE PRESENT for 'head'") is True
    assert _is_alembic_multi_head("some other error") is False


def test_classify_deploy_failure_prioritizes_alembic_multi_head_over_env_deploy():
    job = _make_job(
        failure='multiple head revisions are present; container name "/x" is already in use'
    )

    assert _classify_deploy_failure(job) == FailureClass.alembic_multi_head


def test_alembic_multi_head_creates_fix_job_scoped_to_generated_file(tmp_path):
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")

    job = _make_job(repo_path=str(tmp_path))
    jobs = _make_jobs_store()
    notify = AsyncMock()
    config = MagicMock()

    # Supervisor remediation only files and links the fix job; merge-stage PR
    # inspection and full-OID resolution remain outside this focused harness.
    with (
        patch("hyqs.pipeline.gitops.local_head_oid", new_callable=AsyncMock) as local_head_oid,
        patch(
            "hyqs.pipeline.github.inspect_branch_pr", new_callable=AsyncMock
        ) as inspect_branch_pr,
    ):
        asyncio.run(remediate_alembic_multi_head(jobs, job, config, notify))

    local_head_oid.assert_not_awaited()
    inspect_branch_pr.assert_not_awaited()

    jobs.create.assert_called_once()
    call_kwargs = jobs.create.call_args.kwargs
    assert call_kwargs["source"] == JobSource.SUPERVISOR
    assert call_kwargs["source_meta"]["alembic_merge_fix_for"] == job.id
    idea = call_kwargs["idea"]
    assert "Target files: alembic/versions/" in idea
    assert "_merge_heads.py" in idea
    assert "down_revision" in idea
    jobs.add_job_dependency.assert_called_once_with(job.id, 99)
    jobs.requeue_job_at_stage.assert_called_once_with(job.id, Stage.DEPLOY)
    notify.assert_called_once()


def test_alembic_multi_head_is_idempotent(tmp_path):
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")

    job = _make_job(repo_path=str(tmp_path))
    jobs = _make_jobs_store(has_fix=True)
    notify = AsyncMock()
    config = MagicMock()

    asyncio.run(remediate_alembic_multi_head(jobs, job, config, notify))

    jobs.create.assert_not_called()
    notify.assert_not_called()


def test_alembic_multi_head_respects_project_cap(tmp_path):
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")

    job = _make_job(repo_path=str(tmp_path))
    jobs = _make_jobs_store(has_fix=False, fix_count=3)
    notify = AsyncMock()
    config = MagicMock()

    asyncio.run(remediate_alembic_multi_head(jobs, job, config, notify, project_cap=3))

    jobs.create.assert_not_called()
    notify.assert_called_once()
    assert "cap" in notify.call_args.args[1].lower()


def test_alembic_merge_fix_job_failure_does_not_spawn():
    job = _make_job(source_meta={"alembic_merge_fix_for": 99})
    jobs = _make_jobs_store()
    notify = AsyncMock()
    config = MagicMock()

    asyncio.run(remediate_alembic_multi_head(jobs, job, config, notify))

    jobs.create.assert_not_called()
    notify.assert_called_once()
    assert "chain" in notify.call_args.args[1].lower()


def test_alembic_multi_head_no_conflicting_versions_dir_escalates(tmp_path):
    # A linear chain has a single head — nothing to merge.
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")

    job = _make_job(repo_path=str(tmp_path))
    jobs = _make_jobs_store()
    notify = AsyncMock()
    config = MagicMock()

    asyncio.run(remediate_alembic_multi_head(jobs, job, config, notify))

    jobs.create.assert_not_called()
    notify.assert_called_once()


@patch("hyqs.pipeline.supervisor.gitops.gc_orphaned_artifacts", new_callable=AsyncMock)
def test_janitor_scan_dispatches_alembic_multi_head(mock_gc, tmp_path):
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")

    job = _make_job(repo_path=str(tmp_path))
    jobs = _make_jobs_store()
    jobs.get_failed_jobs.return_value = [job]
    jobs.get_active_job_ids.return_value = set()
    notify = AsyncMock()
    config = MagicMock()

    asyncio.run(_janitor_scan(jobs, config, notify))

    jobs.create.assert_called_once()
    call_kwargs = jobs.create.call_args.kwargs
    assert call_kwargs["source_meta"]["alembic_merge_fix_for"] == job.id
