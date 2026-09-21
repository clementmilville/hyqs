"""Tests for the DEPLOYING-job reconcile fix (job #727).

Covers:
- gitops.is_ancestor (S1)
- deploy.reconcile_deploying_job, the shared ancestor-aware finalize helper (S2)
- PipelineRunner._reconcile_startup's use of the helper (S3)
- supervisor._sweep_deploying_jobs, the periodic janitor catch-up (S4)

Uses AsyncMock/MagicMock throughout — no Postgres required.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline import deploy
from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import Job, JobStatus, Stage
from hyqs.pipeline.runner import PipelineRunner
from hyqs.pipeline.supervisor import _sweep_deploying_jobs

DEPLOYED = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HEAD = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
NEWER = "cccccccccccccccccccccccccccccccccccccccc"


def _git_result(ok: bool, stdout: str = "") -> GitResult:
    return GitResult(ok=ok, stdout=stdout, stderr="" if ok else "boom", code=0 if ok else 1)


def _make_job(
    deployed_commit: str | None = DEPLOYED,
    project_id: int | None = 10,
    lease_until: float = 0.0,
    source_meta: dict | None = None,
) -> Job:
    return Job(
        id=7,
        idea="self-deploy",
        repo_path="/fake/repo",
        chat_id=99,
        stage=Stage.DEPLOY,
        status=JobStatus.DEPLOYING,
        deployed_commit=deployed_commit,
        project_id=project_id,
        lease_until=lease_until,
        source_meta=source_meta,
    )


# ---------------------------------------------------------------------------
# S1 - gitops.is_ancestor
# ---------------------------------------------------------------------------


def test_is_ancestor_true_on_clean_exit():
    with patch("hyqs.pipeline.gitops.git", AsyncMock(return_value=_git_result(True))) as m:
        assert asyncio.run(deploy.gitops.is_ancestor("/fake/repo", DEPLOYED, HEAD)) is True
    m.assert_awaited_once_with(
        "/fake/repo", "merge-base", "--is-ancestor", "--end-of-options", DEPLOYED, HEAD
    )


def test_is_ancestor_false_on_nonzero_exit():
    with patch("hyqs.pipeline.gitops.git", AsyncMock(return_value=_git_result(False))):
        assert asyncio.run(deploy.gitops.is_ancestor("/fake/repo", DEPLOYED, HEAD)) is False


def test_is_ancestor_same_commit_is_true():
    with patch("hyqs.pipeline.gitops.git", AsyncMock(return_value=_git_result(True))):
        assert asyncio.run(deploy.gitops.is_ancestor("/fake/repo", DEPLOYED, DEPLOYED)) is True


# ---------------------------------------------------------------------------
# S2 - deploy.reconcile_deploying_job
# ---------------------------------------------------------------------------


def _make_store(prev_sha: str | None = None) -> MagicMock:
    store = MagicMock()
    store.get_last_deploy.return_value = {"deployed_commit": prev_sha} if prev_sha else None
    store.finalize_deploying_job = MagicMock()
    store.record_deploy = MagicMock()
    store.release_schema_lock = MagicMock()
    store.add_event = MagicMock()
    store.advance_promotion_deployed = MagicMock()
    return store


def test_reconcile_finalizes_when_commit_equals_head():
    job = _make_job(deployed_commit=DEPLOYED)
    store = _make_store(prev_sha=None)
    with (
        patch(
            "hyqs.pipeline.deploy.gitops.git", AsyncMock(return_value=_git_result(True, DEPLOYED))
        ),
        patch("hyqs.pipeline.deploy.gitops.is_ancestor", AsyncMock(return_value=True)) as anc,
    ):
        result = asyncio.run(deploy.reconcile_deploying_job(store, job))

    assert result is True
    anc.assert_awaited_once_with("/fake/repo", DEPLOYED, DEPLOYED)
    store.finalize_deploying_job.assert_called_once_with(job.id, deployed_commit=DEPLOYED)
    store.record_deploy.assert_called_once_with(
        10, DEPLOYED, None, "pipeline_job", job_id=job.id, environment_id=None
    )
    store.release_schema_lock.assert_called_once_with(10, "job-7")
    store.add_event.assert_called_once()
    store.advance_promotion_deployed.assert_not_called()


def test_reconcile_finalizes_when_commit_is_strict_ancestor_of_head():
    """The regression case: a later job's merge moved HEAD past this job's commit."""
    job = _make_job(deployed_commit=DEPLOYED)
    store = _make_store(prev_sha=None)
    with (
        patch("hyqs.pipeline.deploy.gitops.git", AsyncMock(return_value=_git_result(True, HEAD))),
        patch("hyqs.pipeline.deploy.gitops.is_ancestor", AsyncMock(return_value=True)),
    ):
        result = asyncio.run(deploy.reconcile_deploying_job(store, job))

    assert result is True
    store.finalize_deploying_job.assert_called_once_with(job.id, deployed_commit=HEAD)
    store.record_deploy.assert_called_once_with(
        10, HEAD, None, "pipeline_job", job_id=job.id, environment_id=None
    )
    store.release_schema_lock.assert_called_once_with(10, "job-7")


def test_reconcile_leaves_deploying_when_commit_not_ancestor():
    """Diverged/rolled-back HEAD: stay DEPLOYING rather than falsely finalize."""
    job = _make_job(deployed_commit=DEPLOYED)
    store = _make_store()
    with (
        patch("hyqs.pipeline.deploy.gitops.git", AsyncMock(return_value=_git_result(True, HEAD))),
        patch("hyqs.pipeline.deploy.gitops.is_ancestor", AsyncMock(return_value=False)),
    ):
        result = asyncio.run(deploy.reconcile_deploying_job(store, job))

    assert result is False
    store.finalize_deploying_job.assert_not_called()
    store.release_schema_lock.assert_not_called()
    store.record_deploy.assert_not_called()


def test_reconcile_skips_record_deploy_when_prev_deploy_is_newer():
    """Never regress the deploys table to an older sha than what's already recorded."""
    job = _make_job(deployed_commit=DEPLOYED)
    store = _make_store(prev_sha=NEWER)

    async def _is_ancestor(repo, ancestor, ref):
        if ancestor == DEPLOYED and ref == HEAD:
            return True
        if ancestor == HEAD and ref == NEWER:
            return True  # NEWER is strictly ahead of the current HEAD
        return False

    with (
        patch("hyqs.pipeline.deploy.gitops.git", AsyncMock(return_value=_git_result(True, HEAD))),
        patch("hyqs.pipeline.deploy.gitops.is_ancestor", AsyncMock(side_effect=_is_ancestor)),
    ):
        result = asyncio.run(deploy.reconcile_deploying_job(store, job))

    assert result is True
    store.finalize_deploying_job.assert_called_once_with(job.id, deployed_commit=HEAD)
    store.release_schema_lock.assert_called_once_with(10, "job-7")
    store.record_deploy.assert_not_called()


def test_reconcile_returns_false_when_no_deployed_commit():
    job = _make_job(deployed_commit=None)
    store = _make_store()
    result = asyncio.run(deploy.reconcile_deploying_job(store, job))
    assert result is False
    store.finalize_deploying_job.assert_not_called()


def test_reconcile_returns_false_when_head_unreadable():
    job = _make_job(deployed_commit=DEPLOYED)
    store = _make_store()
    with patch("hyqs.pipeline.deploy.gitops.git", AsyncMock(return_value=_git_result(False))):
        result = asyncio.run(deploy.reconcile_deploying_job(store, job))
    assert result is False
    store.finalize_deploying_job.assert_not_called()


def test_reconcile_advances_promotion_deployed_for_linked_job():
    """A promotion-linked job (source_meta carries environment_id/release_id)
    must call advance_promotion_deployed with those exact ids after finalize."""
    job = _make_job(deployed_commit=DEPLOYED, source_meta={"environment_id": 3, "release_id": 4})
    store = _make_store(prev_sha=None)
    with (
        patch(
            "hyqs.pipeline.deploy.gitops.git", AsyncMock(return_value=_git_result(True, DEPLOYED))
        ),
        patch("hyqs.pipeline.deploy.gitops.is_ancestor", AsyncMock(return_value=True)),
    ):
        result = asyncio.run(deploy.reconcile_deploying_job(store, job))

    assert result is True
    store.record_deploy.assert_called_once_with(
        10, DEPLOYED, None, "pipeline_job", job_id=job.id, environment_id=3
    )
    store.advance_promotion_deployed.assert_called_once_with(job.id, 3, 4)


def test_reconcile_unlinked_job_never_advances_promotion_deployed():
    """No source_meta: reconcile behaves exactly as before, no promotion call."""
    job = _make_job(deployed_commit=DEPLOYED, source_meta=None)
    store = _make_store(prev_sha=None)
    with (
        patch(
            "hyqs.pipeline.deploy.gitops.git", AsyncMock(return_value=_git_result(True, DEPLOYED))
        ),
        patch("hyqs.pipeline.deploy.gitops.is_ancestor", AsyncMock(return_value=True)),
    ):
        result = asyncio.run(deploy.reconcile_deploying_job(store, job))

    assert result is True
    store.advance_promotion_deployed.assert_not_called()


# ---------------------------------------------------------------------------
# S3 - PipelineRunner._reconcile_startup uses the shared helper
# ---------------------------------------------------------------------------


def test_reconcile_startup_finalizes_superseded_deploying_job():
    job = _make_job(deployed_commit=DEPLOYED)
    store = MagicMock()
    store.stuck_at_merge.return_value = []
    store.get_deploying_jobs.return_value = [job]
    store.reconcile_agent_tasks.return_value = []

    rn = MagicMock()
    rn.store = store
    rn.notify = AsyncMock()

    with patch(
        "hyqs.pipeline.runner.deploy.reconcile_deploying_job", AsyncMock(return_value=True)
    ) as helper:
        asyncio.run(PipelineRunner._reconcile_startup(rn))

    helper.assert_awaited_once_with(store, job)
    rn.notify.assert_awaited_once()
    assert "verified live" in rn.notify.await_args.args[1]


def test_reconcile_startup_leaves_job_deploying_when_not_verified():
    job = _make_job(deployed_commit=DEPLOYED)
    store = MagicMock()
    store.stuck_at_merge.return_value = []
    store.get_deploying_jobs.return_value = [job]
    store.reconcile_agent_tasks.return_value = []

    rn = MagicMock()
    rn.store = store
    rn.notify = AsyncMock()

    with patch(
        "hyqs.pipeline.runner.deploy.reconcile_deploying_job", AsyncMock(return_value=False)
    ):
        asyncio.run(PipelineRunner._reconcile_startup(rn))

    rn.notify.assert_not_awaited()


# ---------------------------------------------------------------------------
# S4 - supervisor._sweep_deploying_jobs
# ---------------------------------------------------------------------------


def test_sweep_finalizes_expired_lease_job_with_ancestor_commit():
    job = _make_job(deployed_commit=DEPLOYED, lease_until=time.time() - 3600)
    jobs = MagicMock()
    jobs.get_deploying_jobs.return_value = [job]
    notify = AsyncMock()

    with patch(
        "hyqs.pipeline.supervisor.deploy.reconcile_deploying_job", AsyncMock(return_value=True)
    ) as helper:
        asyncio.run(_sweep_deploying_jobs(jobs, notify))

    helper.assert_awaited_once_with(jobs, job)
    notify.assert_awaited_once()


def test_sweep_skips_job_with_live_lease():
    job = _make_job(deployed_commit=DEPLOYED, lease_until=time.time() + 3600)
    jobs = MagicMock()
    jobs.get_deploying_jobs.return_value = [job]
    notify = AsyncMock()

    with patch("hyqs.pipeline.supervisor.deploy.reconcile_deploying_job", AsyncMock()) as helper:
        asyncio.run(_sweep_deploying_jobs(jobs, notify))

    helper.assert_not_awaited()
    notify.assert_not_awaited()


def test_sweep_leaves_non_ancestor_job_deploying():
    job = _make_job(deployed_commit=DEPLOYED, lease_until=time.time() - 3600)
    jobs = MagicMock()
    jobs.get_deploying_jobs.return_value = [job]
    notify = AsyncMock()

    with patch(
        "hyqs.pipeline.supervisor.deploy.reconcile_deploying_job", AsyncMock(return_value=False)
    ):
        asyncio.run(_sweep_deploying_jobs(jobs, notify))

    notify.assert_not_awaited()
