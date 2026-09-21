"""Focused supervisor priority-inheritance regressions.

These tests use mocked stores and temporary filesystem state only; they do not
require PostgreSQL.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import Job, JobStatus, Stage
from hyqs.pipeline.supervisor import (
    FailureClass,
    _alert_archived_dependency,
    _check_fleet_liveness,
    _janitor_scan,
    _reconcile_blocked_dependents,
    classify_failure,
    remediate_alembic_multi_head,
    remediate_dependency_blocked,
    remediate_deploy_failed,
    remediate_gate_no_changes,
    remediate_merge_github_transient,
    remediate_stale_branch,
    remediate_transient,
)


def _make_failed_job(*, repo_path: str, stage: Stage, failure: str) -> Job:
    return Job(
        id=41,
        idea="Fix the failing pipeline job",
        repo_path=repo_path,
        chat_id=5,
        stage=stage,
        status=JobStatus.FAILED,
        failure=failure,
        project_id=10,
        owner="host:123:0",
        priority=7,
    )


def test_classify_failure_gate_conflict_takes_precedence_over_human_review():
    job = Job(
        id=42,
        idea="Resolve conflicting gate findings",
        repo_path="/fake/repo",
        chat_id=5,
        project_id=10,
        status=JobStatus.FAILED,
        failure_code="gate_conflict",
        failure_origin="ai_gate",
        retry_disposition="human_review",
    )

    assert classify_failure(job) is FailureClass.gate_conflict


def _make_jobs() -> MagicMock:
    created_job = MagicMock(id=99)
    jobs = MagicMock()
    jobs.create.return_value = created_job
    jobs.supervisor_requeue_count.return_value = 0
    jobs.has_deploy_fix_job.return_value = False
    jobs.count_deploy_fix_jobs.return_value = 0
    jobs.has_alembic_merge_fix_job.return_value = False
    jobs.count_alembic_merge_fix_jobs.return_value = 0
    return jobs


def _write_migration(
    versions_dir: Path, filename: str, revision: str, down_revision: str | None
) -> None:
    versions_dir.mkdir(parents=True, exist_ok=True)
    (versions_dir / filename).write_text(
        f"revision = {revision!r}\ndown_revision = {down_revision!r}\n"
    )


@patch("hyqs.pipeline.supervisor.collision.check_symbol_collisions")
@patch("hyqs.pipeline.supervisor.gitops.numstat", new_callable=AsyncMock)
@patch("hyqs.pipeline.supervisor.gitops.git", new_callable=AsyncMock)
@patch("hyqs.pipeline.supervisor.gitops.fresh_base", new_callable=AsyncMock)
@patch("hyqs.pipeline.supervisor.gitops.default_branch", new_callable=AsyncMock)
def test_gate_no_changes_inherits_failing_job_priority(
    mock_default_branch,
    mock_fresh_base,
    mock_git,
    mock_numstat,
    mock_check,
    tmp_path,
):
    job = _make_failed_job(
        repo_path="/fake/repo",
        stage=Stage.BUILD,
        failure="[symbol-collision] found 1 conflict",
    )
    (tmp_path / "worktrees" / f"job-{job.id}").mkdir(parents=True)
    mock_default_branch.return_value = "main"
    mock_fresh_base.return_value = "main"
    mock_git.return_value = GitResult(ok=True, stdout="abc123\n", stderr="", code=0)
    mock_numstat.return_value = [{"path": "hyqs/pipeline/example.py"}]
    mock_check.return_value = ["attributable symbol collision"]
    jobs = _make_jobs()
    config = MagicMock(data_dir=tmp_path)

    asyncio.run(remediate_gate_no_changes(jobs, job, config, AsyncMock()))

    jobs.create.assert_called_once()
    assert jobs.create.call_args.kwargs["priority"] == job.priority


def test_deploy_failed_inherits_failing_job_priority():
    job = _make_failed_job(
        repo_path="/fake/repo",
        stage=Stage.DEPLOY,
        failure="module not found",
    )
    jobs = _make_jobs()

    asyncio.run(remediate_deploy_failed(jobs, job, MagicMock(), AsyncMock()))

    jobs.create.assert_called_once()
    assert jobs.create.call_args.kwargs["priority"] == job.priority


def test_alembic_multi_head_inherits_failing_job_priority(tmp_path):
    versions_dir = tmp_path / "alembic" / "versions"
    _write_migration(versions_dir, "root.py", "root", None)
    _write_migration(versions_dir, "branch_a.py", "branch_a", "root")
    _write_migration(versions_dir, "branch_b.py", "branch_b", "root")
    job = _make_failed_job(
        repo_path=str(tmp_path),
        stage=Stage.DEPLOY,
        failure="Multiple head revisions are present for given argument 'head'",
    )
    jobs = _make_jobs()

    asyncio.run(remediate_alembic_multi_head(jobs, job, MagicMock(), AsyncMock()))

    jobs.create.assert_called_once()
    assert jobs.create.call_args.kwargs["priority"] == job.priority


# --- remediate_merge_github_transient: backoff, requeue, dead-letter cap ------


def _make_merge_transient_job() -> Job:
    return _make_failed_job(
        repo_path="/fake/repo",
        stage=Stage.MERGE,
        failure="PR merge failed: non-200 OK status code: 503 Service Unavailable",
    )


def test_remediate_merge_github_transient_backs_off_within_window():
    job = _make_merge_transient_job()
    jobs = _make_jobs()
    jobs.supervisor_requeue_count.return_value = 1  # backoff = min(30*2**1, 600) = 60s
    jobs.list_supervisor_events_for_job.return_value = [
        {"action": "requeued", "failure_class": "transient", "ts": time.time() - 5},
    ]

    asyncio.run(remediate_merge_github_transient(jobs, job, MagicMock(), notify=AsyncMock()))

    jobs.requeue_job_at_stage.assert_not_called()
    jobs.increment_supervisor_requeue.assert_not_called()


def test_remediate_merge_github_transient_requeues_once_backoff_elapses():
    job = _make_merge_transient_job()
    jobs = _make_jobs()
    jobs.supervisor_requeue_count.return_value = 1
    jobs.list_supervisor_events_for_job.return_value = [
        {"action": "requeued", "failure_class": "transient", "ts": time.time() - 120},
    ]

    asyncio.run(remediate_merge_github_transient(jobs, job, MagicMock(), notify=AsyncMock()))

    jobs.requeue_job_at_stage.assert_called_once_with(job.id, Stage.MERGE, failure=job.failure)
    jobs.increment_supervisor_requeue.assert_called_once_with(job.id)
    requeued_calls = [
        call for call in jobs.record_supervisor_event.call_args_list if call.args[1] == "requeued"
    ]
    assert len(requeued_calls) == 1
    assert requeued_calls[0].args[2] == FailureClass.transient.value


def test_remediate_merge_github_transient_requeues_with_no_prior_events():
    job = _make_merge_transient_job()
    jobs = _make_jobs()
    jobs.supervisor_requeue_count.return_value = 0
    jobs.list_supervisor_events_for_job.return_value = []

    asyncio.run(remediate_merge_github_transient(jobs, job, MagicMock(), notify=AsyncMock()))

    jobs.requeue_job_at_stage.assert_called_once_with(job.id, Stage.MERGE, failure=job.failure)
    jobs.increment_supervisor_requeue.assert_called_once_with(job.id)


def test_remediate_merge_github_transient_dead_letters_at_cap():
    job = _make_merge_transient_job()
    jobs = _make_jobs()
    jobs.supervisor_requeue_count.return_value = 5
    jobs.has_supervisor_event.return_value = False
    jobs.is_supervisor_notified.return_value = False
    config = MagicMock(pipeline_incident_analyst_predeadletter=False)
    notify = AsyncMock()

    asyncio.run(
        remediate_merge_github_transient(jobs, job, config, dead_letter_cap=5, notify=notify)
    )

    jobs.requeue_job_at_stage.assert_not_called()
    jobs.increment_supervisor_requeue.assert_not_called()
    dead_letter_calls = [
        call
        for call in jobs.record_supervisor_event.call_args_list
        if call.args[1] == "dead_lettered"
    ]
    assert len(dead_letter_calls) == 1
    notify.assert_awaited_once()


def _make_pending_dependent(*, job_id: int) -> Job:
    return Job(
        id=job_id,
        idea="Dependent job",
        repo_path="/fake/repo",
        chat_id=5,
        status=JobStatus.PENDING,
        project_id=10,
    )


def _make_archived_dependency(*, dep_id: int, title: str = "Archived dependency") -> Job:
    return Job(
        id=dep_id,
        idea="Archived dependency job",
        repo_path="/fake/repo",
        chat_id=5,
        title=title,
        status=JobStatus.CANCELLED,
        archived=True,
        project_id=10,
    )


def test_reconcile_blocked_dependents_alerts_on_archived_dependency_without_failing_it():
    dependent = _make_pending_dependent(job_id=100)
    dep = _make_archived_dependency(dep_id=200)
    jobs = MagicMock()
    jobs.list_pending_with_unsatisfied_deps.return_value = [dependent]
    jobs.get_unsatisfied_deps.return_value = [dep.id]
    jobs.get.return_value = dep
    jobs.get_meta.return_value = "0"
    notify = AsyncMock()

    asyncio.run(_reconcile_blocked_dependents(jobs, notify))

    jobs.save.assert_not_called()
    notify.assert_awaited_once()
    _, message = notify.await_args.args
    kwargs = notify.await_args.kwargs
    assert kwargs["event_type"] == "needs_attention"
    assert kwargs["reason"] == "archived_dependency"
    assert f"#{dep.id}" in message


def test_alert_archived_dependency_first_call_notifies_and_sets_meta():
    dependent = _make_pending_dependent(job_id=101)
    dep = _make_archived_dependency(dep_id=201)
    jobs = _make_jobs()
    notify = AsyncMock()
    jobs.get_meta.return_value = "0"

    asyncio.run(_alert_archived_dependency(jobs, notify, dependent, dep))

    notify.assert_awaited_once()
    _, message = notify.await_args.args
    assert f"#{dep.id}" in message
    assert dep.title in message
    jobs.set_meta.assert_called_once()


def test_alert_archived_dependency_second_call_within_cooldown_is_noop():
    dependent = _make_pending_dependent(job_id=101)
    dep = _make_archived_dependency(dep_id=201)
    jobs = _make_jobs()
    notify = AsyncMock()
    jobs.get_meta.return_value = str(time.time())

    asyncio.run(_alert_archived_dependency(jobs, notify, dependent, dep))

    notify.assert_not_awaited()
    jobs.set_meta.assert_not_called()


def _make_dependency_blocked_job(*, dependency_id: int) -> Job:
    return Job(
        id=42,
        idea="Dependent job",
        repo_path="/fake/repo",
        chat_id=5,
        status=JobStatus.FAILED,
        project_id=10,
        failure_code="dependency_blocked",
        failure_detail={"dependency_id": dependency_id},
    )


def test_remediate_dependency_blocked_requeues_when_recorded_blocker_no_longer_applies():
    job = _make_dependency_blocked_job(dependency_id=7)
    jobs = _make_jobs()
    jobs.get_unsatisfied_deps.return_value = [8]  # a different, still-unsatisfied dependency
    jobs.dependency_block_still_applies.return_value = False

    asyncio.run(remediate_dependency_blocked(jobs, job, MagicMock(), notify=AsyncMock()))

    jobs.requeue_job.assert_called_once_with(job.id, zero_attempts=True)
    jobs.increment_supervisor_requeue.assert_called_once_with(job.id)


def test_remediate_dependency_blocked_stays_failed_when_recorded_blocker_still_unsatisfied():
    job = _make_dependency_blocked_job(dependency_id=7)
    jobs = _make_jobs()
    jobs.get_unsatisfied_deps.return_value = [7]
    jobs.dependency_block_still_applies.return_value = True

    asyncio.run(remediate_dependency_blocked(jobs, job, MagicMock(), notify=AsyncMock()))

    jobs.requeue_job.assert_not_called()
    jobs.increment_supervisor_requeue.assert_not_called()


# --- remediate_transient: worktree_missing self-heals via BUILD, other codes unchanged -----


def _make_worktree_missing_job() -> Job:
    return Job(
        id=41,
        idea="Fix the failing pipeline job",
        repo_path="/fake/repo",
        chat_id=5,
        stage=Stage.FIX,
        status=JobStatus.FAILED,
        failure="fix stage: worktree missing after post-merge cleanup",
        failure_code="worktree_missing",
        failure_origin="infrastructure",
        retry_disposition="same_step",
        project_id=10,
    )


def _make_unexpected_stage_error_job() -> Job:
    return Job(
        id=42,
        idea="Fix the failing pipeline job",
        repo_path="/fake/repo",
        chat_id=5,
        stage=Stage.DESIGN_REVIEW,
        status=JobStatus.FAILED,
        failure="unexpected error during design_review: boom",
        failure_code="unexpected_stage_error",
        failure_origin="infrastructure",
        retry_disposition="same_step",
        project_id=10,
    )


def test_classify_failure_worktree_missing_is_transient():
    job = _make_worktree_missing_job()
    assert classify_failure(job) is FailureClass.transient


def test_remediate_transient_reroutes_worktree_missing_to_build():
    job = _make_worktree_missing_job()
    jobs = _make_jobs()

    asyncio.run(remediate_transient(jobs, job, MagicMock(), dead_letter_cap=5))

    jobs.requeue_job_at_stage.assert_called_once_with(job.id, Stage.BUILD, failure=job.failure)
    jobs.increment_supervisor_requeue.assert_called_once_with(job.id)


def test_remediate_transient_unexpected_stage_error_resumes_at_own_stage():
    """Regression guard for #3971/#3976: every other transient code keeps
    requeuing at the job's own stage — only worktree_missing reroutes to BUILD."""
    job = _make_unexpected_stage_error_job()
    jobs = _make_jobs()

    asyncio.run(remediate_transient(jobs, job, MagicMock(), dead_letter_cap=5))

    jobs.requeue_job_at_stage.assert_called_once_with(
        job.id, Stage.DESIGN_REVIEW, failure=job.failure
    )
    jobs.increment_supervisor_requeue.assert_called_once_with(job.id)


def test_remediate_transient_dead_letters_at_cap_for_worktree_missing():
    job = _make_worktree_missing_job()
    jobs = _make_jobs()
    jobs.supervisor_requeue_count.return_value = 5
    jobs.has_supervisor_event.return_value = False
    jobs.is_supervisor_notified.return_value = False

    asyncio.run(
        remediate_transient(jobs, job, MagicMock(), dead_letter_cap=5, failure_class="transient")
    )

    jobs.requeue_job_at_stage.assert_not_called()
    jobs.increment_supervisor_requeue.assert_not_called()
    dead_letter_calls = [
        call
        for call in jobs.record_supervisor_event.call_args_list
        if call.args[1] == "dead_lettered"
    ]
    assert len(dead_letter_calls) == 1


def test_remediate_transient_dead_letters_at_cap_for_unexpected_stage_error():
    job = _make_unexpected_stage_error_job()
    jobs = _make_jobs()
    jobs.supervisor_requeue_count.return_value = 5
    jobs.has_supervisor_event.return_value = False
    jobs.is_supervisor_notified.return_value = False

    asyncio.run(
        remediate_transient(jobs, job, MagicMock(), dead_letter_cap=5, failure_class="transient")
    )

    jobs.requeue_job_at_stage.assert_not_called()
    jobs.increment_supervisor_requeue.assert_not_called()
    dead_letter_calls = [
        call
        for call in jobs.record_supervisor_event.call_args_list
        if call.args[1] == "dead_lettered"
    ]
    assert len(dead_letter_calls) == 1


def test_classify_failure_genuine_test_failure_never_transient():
    """Negative regression: a real test failure must never be auto-retried.

    retry_disposition="human_review" is what _retry_or_fail sets once the
    fix-attempt cap is exhausted for a genuine test/lint/gate failure — it
    must classify as genuine_code, never transient, so the janitor's
    ``elif cls == FailureClass.transient`` dispatch branch never routes it
    through remediate_transient.
    """
    job = Job(
        id=43,
        idea="Fix the failing pipeline job",
        repo_path="/fake/repo",
        chat_id=5,
        stage=Stage.FIX,
        status=JobStatus.FAILED,
        failure="tests failed (pytest):\nAssertionError: expected 200, got 500",
        failure_code="test_failed",
        failure_origin="candidate_code",
        retry_disposition="human_review",
        project_id=10,
    )

    cls = classify_failure(job)

    assert cls is FailureClass.genuine_code
    assert cls is not FailureClass.transient


def _make_meta_jobs() -> MagicMock:
    """A MagicMock JobStore backed by a real dict for get_meta/set_meta, so
    _check_fleet_liveness's dedup flag actually persists across calls."""
    meta: dict[str, str] = {}
    jobs = MagicMock()
    jobs.get_meta.side_effect = lambda key, default="0": meta.get(key, default)
    jobs.set_meta.side_effect = lambda key, value: meta.__setitem__(key, value)
    return jobs


def _busy_executor(worker_id: str = "w1") -> dict:
    return {"id": worker_id, "role": "executor", "status": "busy", "alive": True}


def _idle_executor(worker_id: str = "w2") -> dict:
    return {"id": worker_id, "role": "executor", "status": "idle", "alive": True}


def _make_active_job(job_id: int, *, updated_at: str) -> Job:
    return Job(
        id=job_id,
        idea="wedged job",
        repo_path="/fake/repo",
        chat_id=1,
        project_id=10,
        status=JobStatus.PENDING,
        updated_at=updated_at,
    )


def test_check_fleet_liveness_alerts_once_per_wedge_episode():
    stale_updated = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    job = _make_active_job(1, updated_at=stale_updated)
    jobs = _make_meta_jobs()
    jobs.list_workers.return_value = [_busy_executor()]
    jobs.list_active.return_value = [job]
    notify = AsyncMock()
    config = SimpleNamespace(pipeline_fleet_wedge_minutes=5)

    asyncio.run(_check_fleet_liveness(jobs, config, notify))

    notify.assert_awaited_once()
    assert notify.await_args.kwargs["event_type"] == "needs_attention"
    assert notify.await_args.kwargs["reason"] == "fleet_wedged"

    # Still wedged on a second consecutive scan: no duplicate alert.
    asyncio.run(_check_fleet_liveness(jobs, config, notify))
    notify.assert_awaited_once()


def test_check_fleet_liveness_realerts_after_wedge_clears():
    stale_updated = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    job = _make_active_job(1, updated_at=stale_updated)
    jobs = _make_meta_jobs()
    jobs.list_workers.return_value = [_busy_executor()]
    jobs.list_active.return_value = [job]
    notify = AsyncMock()
    config = SimpleNamespace(pipeline_fleet_wedge_minutes=5)

    asyncio.run(_check_fleet_liveness(jobs, config, notify))
    notify.assert_awaited_once()

    # Fleet recovers: an idle executor appears, clearing the dedup flag.
    jobs.list_workers.return_value = [_busy_executor(), _idle_executor()]
    asyncio.run(_check_fleet_liveness(jobs, config, notify))
    notify.assert_awaited_once()

    # Wedged again: a fresh alert must fire.
    jobs.list_workers.return_value = [_busy_executor()]
    asyncio.run(_check_fleet_liveness(jobs, config, notify))
    assert notify.await_count == 2


def test_check_fleet_liveness_no_alert_when_an_executor_is_idle():
    stale_updated = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    job = _make_active_job(2, updated_at=stale_updated)
    jobs = _make_meta_jobs()
    jobs.list_workers.return_value = [_busy_executor(), _idle_executor()]
    jobs.list_active.return_value = [job]
    notify = AsyncMock()
    config = SimpleNamespace(pipeline_fleet_wedge_minutes=5)

    asyncio.run(_check_fleet_liveness(jobs, config, notify))

    notify.assert_not_awaited()


def test_check_fleet_liveness_no_alert_when_stalest_job_is_recent():
    """Negative (critical) case: every executor is busy, but the stalest
    active job was updated well within the threshold — a legitimately busy,
    actively-advancing fleet must never alert."""
    recent_updated = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    job = _make_active_job(3, updated_at=recent_updated)
    jobs = _make_meta_jobs()
    jobs.list_workers.return_value = [_busy_executor()]
    jobs.list_active.return_value = [job]
    notify = AsyncMock()
    config = SimpleNamespace(pipeline_fleet_wedge_minutes=5)

    asyncio.run(_check_fleet_liveness(jobs, config, notify))

    notify.assert_not_awaited()


def test_janitor_scan_judgment_class_warns_only_on_first_not_yet_notified_pass(caplog):
    """job #3988: re-logging the same terminally-classified job every scan at
    WARNING is what buried an 8h fleet-wedge outage under ~493 identical
    lines. A judgment-class job already supervisor-notified must not warn
    again on a later scan."""
    job = Job(
        id=9,
        idea="idea",
        repo_path="/fake/repo",
        chat_id=1,
        project_id=10,
        stage=Stage.BUILD,
        status=JobStatus.FAILED,
        failure="unrecognized failure",
    )
    jobs = MagicMock()
    jobs.get_failed_jobs.return_value = [job]
    jobs.get_active_job_ids.return_value = set()
    jobs.get_git_artifact_protected_job_ids.return_value = set()
    jobs.prune_audit_log.return_value = 0
    jobs.reclaim_orphaned_worker_slots.return_value = []
    notify = AsyncMock()
    config = SimpleNamespace(pipeline_job_stale_hours=24)

    notified = {"value": False}
    jobs.is_supervisor_notified.side_effect = lambda job_id: notified["value"]

    def _mark_notified(job_id):
        notified["value"] = True

    jobs.mark_supervisor_notified.side_effect = _mark_notified

    with (
        patch("hyqs.pipeline.supervisor._repoint_split_orphans", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._check_job_staleness", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._check_dependency_cycles", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._check_fleet_liveness", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._reconcile_blocked_dependents", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._sweep_deploying_jobs", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor.gitops.gc_orphaned_artifacts", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._docker_sweep", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._docker_builder_prune", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._docker_image_prune", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._ai_diagnose_and_act", new=AsyncMock(return_value=False)),
        patch("hyqs.pipeline.supervisor.classify_failure", return_value=FailureClass.unknown),
        caplog.at_level("WARNING", logger="hyqs.supervisor"),
    ):
        asyncio.run(_janitor_scan(jobs, config, notify))
        asyncio.run(_janitor_scan(jobs, config, notify))

    warnings = [r for r in caplog.records if "needs human judgment" in r.message]
    assert len(warnings) == 1
    assert notify.await_count == 1


def test_remediate_stale_branch_cleans_up_managed_repo_not_repo_path():
    """job #3988: BUILD creates a project-scoped job's worktree/branch against
    the pipeline-owned managed clone, not job.repo_path — cleanup must target
    the same path or the branch survives the requeue and the retry
    deterministically re-fails with 'branch already exists'."""
    job = Job(
        id=10,
        idea="idea",
        repo_path="/fake/repo",
        chat_id=1,
        project_id=10,
        branch="hyqs/job-10",
        status=JobStatus.FAILED,
        failure="worktree missing",
    )
    jobs = MagicMock()
    jobs.supervisor_requeue_count.return_value = 0
    managed_repo = Path("/managed/repos/10")
    config = MagicMock(data_dir="/data")

    with (
        patch(
            "hyqs.pipeline.supervisor.gitops.git",
            new=AsyncMock(
                return_value=GitResult(
                    ok=True, stdout="git@github.com:org/repo.git\n", stderr="", code=0
                )
            ),
        ),
        patch(
            "hyqs.pipeline.supervisor.gitops.ensure_managed_repo",
            new=AsyncMock(return_value=managed_repo),
        ),
        patch("hyqs.pipeline.supervisor.gitops.remove_worktree", new=AsyncMock()) as mock_remove,
        patch(
            "hyqs.pipeline.supervisor.gitops.delete_local_branch", new=AsyncMock()
        ) as mock_delete,
    ):
        asyncio.run(remediate_stale_branch(jobs, job, config, notify=AsyncMock()))

    mock_remove.assert_awaited_once()
    assert mock_remove.await_args.args[0] == managed_repo
    mock_delete.assert_awaited_once()
    assert mock_delete.await_args.args[0] == managed_repo
