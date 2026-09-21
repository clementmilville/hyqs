"""Regression tests for the pipeline loop-logic hardening pass.

Covers the failure-loop compositions found in the 2026-07 assessment:

1. Deploy failures must FAIL at stage DEPLOY (never detour into the FIX loop,
   whose worktree was removed at merge) so the supervisor's classifier can file
   the fix-forward job.
2. A stage timeout must not delete the worktree that the same-stage retry needs;
   post-PLAN stages get a reset to the last committed checkpoint instead.
3. Timeout-budget exhaustion classifies as genuine_code (escalate), not
   transient — a transient requeue cannot restore the spent timeout budget.
4. "fix produced no changes" on a NON-gate failure is its own judgment class
   (fix_no_changes), not stale_branch requeue-from-scratch.
5. Malformed review/security gate output re-runs the gate once instead of
   consuming a FIX credit.
6. merged_but_stuck jobs whose PR is NOT merged fall through to a bounded
   transient requeue instead of silently stalling FAILED forever.
7. MERGE_VERIFY claims like an agentic stage (provider pause + roster apply).
8. Transient requeues resume at the failed stage, preserving job.failure.
9. A PENDING job is terminal-failed only when its FAILED dependency has been
   genuinely dead-lettered. An archived dependency instead triggers an alert
   while the dependent stays PENDING for a later restore or re-point.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest

from hyqs.pipeline import FailureDetail, agents
from hyqs.pipeline.models import AgentTask, Job, JobStatus, Stage, Usage, stage_task
from hyqs.pipeline.runner import PipelineRunner
from hyqs.pipeline.stages import HANDLERS, fix
from hyqs.pipeline.stages.deploy import run as deploy_run
from hyqs.pipeline.store import ScopeAmendmentPersistenceResult
from hyqs.pipeline.supervisor import (
    FailureClass,
    _janitor_scan,
    _reconcile_blocked_dependents,
    classify_failure,
    remediate_blocked_dependent,
    remediate_merged_but_stuck,
    remediate_no_diff_build,
    remediate_transient,
)


def _make_job(
    job_id: int = 1,
    status: JobStatus = JobStatus.PENDING,
    archived: bool = False,
    chat_id: int = 5,
) -> Job:
    return Job(
        id=job_id,
        idea="dependent job",
        repo_path="/fake/repo",
        chat_id=chat_id,
        stage=Stage.QUEUED,
        status=status,
        archived=archived,
    )


def _make_jobs_store(
    pending_with_unsatisfied_deps: list[Job],
    unsatisfied_deps: dict[int, list[int]],
    deps_by_id: dict[int, Job],
    is_notified: bool = False,
) -> MagicMock:
    jobs = MagicMock()
    jobs.list_pending_with_unsatisfied_deps.return_value = pending_with_unsatisfied_deps
    jobs.get_unsatisfied_deps.side_effect = lambda job_id: unsatisfied_deps.get(job_id, [])
    jobs.get.side_effect = lambda dep_id: deps_by_id.get(dep_id)
    jobs.is_supervisor_notified.return_value = is_notified
    jobs.save = MagicMock()
    jobs.mark_supervisor_notified = MagicMock()
    jobs.record_supervisor_event = MagicMock()
    return jobs


def test_dependency_failed_and_notified_marks_dependent_failed():
    dependent = _make_job(job_id=1)
    dep = _make_job(job_id=2, status=JobStatus.FAILED)
    jobs = _make_jobs_store(
        pending_with_unsatisfied_deps=[dependent],
        unsatisfied_deps={1: [2]},
        deps_by_id={2: dep},
        is_notified=True,
    )
    jobs.has_supervisor_event.return_value = True
    notify = AsyncMock()

    asyncio.run(_reconcile_blocked_dependents(jobs, notify))

    jobs.save.assert_called_once()
    saved_job = jobs.save.call_args.args[0]
    assert saved_job.status == JobStatus.FAILED
    assert saved_job.failure.startswith("blocked: dependency #2")
    assert classify_failure(saved_job) == FailureClass.dependency_blocked
    assert saved_job.failure_code == "dependency_blocked"
    assert saved_job.retry_disposition == "await_dependency"


def test_typed_failure_wins_over_misleading_stale_branch_text():
    job = _make_job(status=JobStatus.FAILED)
    job.stage = Stage.PLAN
    job.branch = "job-2364"
    job.error = "unexpected error with connection"
    job.failed_step = "build"
    job.failure_code = "no_diff_verification_failed"
    job.failure_origin = "ai_gate"
    job.retry_disposition = "retry_build"
    job.failure_detail = {"verification": "rejected", "retry_attempt": 0}

    assert classify_failure(job) is FailureClass.no_diff_build_retry


def test_no_diff_build_recovery_requeues_plan_checkpoint_once():
    job = _make_job(status=JobStatus.FAILED)
    job.stage = Stage.PLAN
    job.failed_step = "build"
    job.failure_code = "no_diff_verification_failed"
    job.retry_disposition = "retry_build"
    job.failure_detail = {"verification": "rejected", "retry_attempt": 0}
    jobs = MagicMock()
    jobs.requeue_no_diff_build.return_value = True

    asyncio.run(remediate_no_diff_build(jobs, job))

    jobs.requeue_no_diff_build.assert_called_once_with(job.id)
    jobs.record_supervisor_event.assert_called_once()


def test_terminal_no_diff_build_is_not_requeued():
    job = _make_job(status=JobStatus.FAILED)
    job.stage = Stage.PLAN
    job.failed_step = "build"
    job.failure_code = "no_diff_verification_failed"
    job.retry_disposition = "terminal"
    job.failure_detail = {"verification": "rejected", "retry_attempt": 1}

    assert classify_failure(job) is FailureClass.genuine_code


def test_untyped_failure_keeps_legacy_stale_branch_classification():
    job = _make_job(status=JobStatus.FAILED)
    job.stage = Stage.PLAN
    job.branch = "legacy-branch"
    job.error = "unexpected error with connection"

    assert classify_failure(job) is FailureClass.stale_branch


@pytest.mark.parametrize(
    ("code", "origin", "disposition", "expected"),
    [
        ("provider_transport_error", "provider", "same_step", FailureClass.transient),
        ("provider_unavailable", "provider", "same_step", FailureClass.transient),
        ("stage_timeout_exhausted", "infrastructure", "human_review", FailureClass.genuine_code),
        ("isolation_violation", "infrastructure", "human_review", FailureClass.isolation_leak),
        ("merge_conflict", "candidate_code", "fix_worktree", FailureClass.merge_conflict_exhausted),
        ("dependency_blocked", "dependency", "await_dependency", FailureClass.dependency_blocked),
        (
            "deploy_environment_error",
            "deployment",
            "remediation_job",
            FailureClass.merged_but_stuck,
        ),
        ("gate_conflict", "ai_gate", "human_review", FailureClass.gate_conflict),
    ],
)
def test_typed_failure_dispositions_preserve_recovery(code, origin, disposition, expected):
    job = _make_job(status=JobStatus.FAILED)
    job.failure_code = code
    job.failure_origin = origin
    job.retry_disposition = disposition

    assert classify_failure(job) is expected


def test_dependency_archived_alerts_and_leaves_dependent_pending():
    dependent = _make_job(job_id=1)
    dep = _make_job(job_id=2, status=JobStatus.FAILED, archived=True)
    jobs = _make_jobs_store(
        pending_with_unsatisfied_deps=[dependent],
        unsatisfied_deps={1: [2]},
        deps_by_id={2: dep},
        is_notified=False,
    )
    jobs.get_meta.return_value = "0"
    notify = AsyncMock()

    asyncio.run(_reconcile_blocked_dependents(jobs, notify))

    jobs.save.assert_not_called()
    assert dependent.status == JobStatus.PENDING
    notify.assert_awaited_once()
    message = notify.await_args.args[1]
    assert "dependency #2" in message
    kwargs = notify.await_args.kwargs
    assert kwargs["event_type"] == "needs_attention"
    assert kwargs["reason"] == "archived_dependency"
    jobs.get_meta.assert_called_once()
    throttle_key, default = jobs.get_meta.call_args.args
    assert throttle_key.endswith(":2")
    assert default == "0"
    jobs.set_meta.assert_called_once()
    persisted_key, persisted_timestamp = jobs.set_meta.call_args.args
    assert persisted_key == throttle_key
    assert float(persisted_timestamp) > 0


def test_dependency_failed_but_not_yet_notified_leaves_dependent_untouched():
    dependent = _make_job(job_id=1)
    dep = _make_job(job_id=2, status=JobStatus.FAILED)
    jobs = _make_jobs_store(
        pending_with_unsatisfied_deps=[dependent],
        unsatisfied_deps={1: [2]},
        deps_by_id={2: dep},
        is_notified=False,
    )
    notify = AsyncMock()

    asyncio.run(_reconcile_blocked_dependents(jobs, notify))

    jobs.save.assert_not_called()
    notify.assert_not_called()


def test_no_candidates_is_a_no_op():
    jobs = _make_jobs_store(
        pending_with_unsatisfied_deps=[],
        unsatisfied_deps={},
        deps_by_id={},
    )
    notify = AsyncMock()

    asyncio.run(_reconcile_blocked_dependents(jobs, notify))

    jobs.save.assert_not_called()
    notify.assert_not_called()


def test_remediate_blocked_dependent_notifies_once():
    job = _make_job(job_id=1)
    jobs = MagicMock()
    jobs.is_supervisor_notified.return_value = False
    notify = AsyncMock()

    asyncio.run(remediate_blocked_dependent(jobs, job, 2, reason="failed", notify=notify))

    jobs.save.assert_called_once_with(job)
    assert job.status == JobStatus.FAILED
    assert job.failure == "blocked: dependency #2 failed and can no longer complete"
    notify.assert_called_once()
    jobs.mark_supervisor_notified.assert_called_once_with(job.id)


def _job(**kw) -> Job:
    defaults = dict(
        id=7,
        idea="test idea",
        repo_path="/fake/repo",
        chat_id=1,
        stage=Stage.TEST,
        status=JobStatus.FAILED,
        branch="hyqs/job-7",
    )
    defaults.update(kw)
    return Job(**defaults)


def _verification_backend(text: str) -> MagicMock:
    backend = MagicMock()
    backend.run = AsyncMock(return_value=SimpleNamespace(text=text, usage=Usage()))
    return backend


def test_agent_fix_renders_structured_dependency_evidence_deterministically(tmp_path):
    detail = {
        "remediation": [
            {
                "package": "example",
                "advisory_id": "GHSA-1234",
                "manifest_path": "web/package.json",
                "lockfile_path": "web/package-lock.json",
                "lowest_compatible_patched_version": "1.0.1",
                "recommended_action": "Upgrade example to 1.0.1",
                "verification_commands": ["npm audit --json", "npm ls example --all"],
            }
        ],
        "gate": "security",
        "output": "API_KEY=must-not-cross-provider-boundary",
    }
    backend = _verification_backend("fixed")

    asyncio.run(
        agents.fix(
            backend,
            "upgrade the vulnerable dependency",
            "security rejected: example is vulnerable",
            str(tmp_path),
            failure_detail=detail,
        )
    )

    prompt = backend.run.await_args.kwargs["prompt"]
    expected_json = """{
  "remediation": [
    {
      "advisory_id": "GHSA-1234",
      "lockfile_path": "web/package-lock.json",
      "lowest_compatible_patched_version": "1.0.1",
      "manifest_path": "web/package.json",
      "package": "example",
      "recommended_action": "Upgrade example to 1.0.1",
      "verification_commands": [
        "npm audit --json",
        "npm ls example --all"
      ]
    }
  ]
}"""
    assert f"<<<FAILURE_DETAIL_JSON>>>\n{expected_json}\n<<<END_FAILURE_DETAIL_JSON>>>" in prompt
    assert "Failure report:\n\nsecurity rejected: example is vulnerable" in prompt
    assert "Prefer structured dependency remediation entries" in prompt
    assert "must-not-cross-provider-boundary" not in prompt


def test_agent_fix_redacts_secrets_from_allowlisted_dependency_fields(tmp_path):
    detail = {
        "remediation": [
            {
                "package": "example",
                "advisory_id": "GHSA-1234",
                "manifest_path": "web/package.json",
                "lockfile_path": "web/package-lock.json",
                "recommended_action": "TOKEN=super-secret upgrade example",
                "verification_commands": ["npm audit --token=ghp_abcdefghijklmnopqrstuvwxyz"],
                "output": "PASSWORD=raw-diagnostic-secret",
            }
        ]
    }
    backend = _verification_backend("fixed")

    asyncio.run(
        agents.fix(
            backend,
            "upgrade the vulnerable dependency",
            "security rejected",
            str(tmp_path),
            failure_detail=detail,
        )
    )

    prompt = backend.run.await_args.kwargs["prompt"]
    assert "super-secret" not in prompt
    assert "abcdefghijklmnopqrstuvwxyz" not in prompt
    assert "raw-diagnostic-secret" not in prompt
    assert prompt.count("[REDACTED]") == 2


def test_agent_fix_ignores_general_structured_diagnostics(tmp_path):
    backend = _verification_backend("fixed")

    asyncio.run(
        agents.fix(
            backend,
            "repair the failure",
            "legacy prose failure",
            str(tmp_path),
            failure_detail={"output": "API_KEY=must-not-cross-provider-boundary"},
        )
    )

    prompt = backend.run.await_args.kwargs["prompt"]
    assert "Failure report:\n\nlegacy prose failure" in prompt
    assert "must-not-cross-provider-boundary" not in prompt
    assert "<<<FAILURE_DETAIL_JSON>>>" not in prompt


@pytest.mark.parametrize("failure_detail", [None, ["legacy", "detail"], "legacy detail"])
def test_agent_fix_legacy_detail_keeps_prose_without_structured_block(tmp_path, failure_detail):
    backend = _verification_backend("fixed")

    asyncio.run(
        agents.fix(
            backend,
            "repair the failure",
            "legacy prose failure",
            str(tmp_path),
            failure_detail=failure_detail,
        )
    )

    prompt = backend.run.await_args.kwargs["prompt"]
    assert "Failure report:\n\nlegacy prose failure" in prompt
    assert "<<<FAILURE_DETAIL_JSON>>>" not in prompt


def test_agent_fix_includes_plan_contract_and_failure_identity(tmp_path):
    backend = _verification_backend("fixed")
    plan = {
        "summary": "Keep lease rotation atomic",
        "stories": [
            {
                "id": "S1",
                "title": "Atomic rotation",
                "task": "Rotate the lease session atomically.",
                "acceptance": "Concurrent rotation cannot reuse the old session.",
            }
        ],
        "reuses": ["app.sessions.rotate"],
        "adds": ["app.sessions.RotationResult"],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "Internal session behavior.",
        },
        "target_files": ["app/sessions.py", "tests/test_sessions.py"],
    }

    asyncio.run(
        agents.fix(
            backend,
            "make rotation atomic",
            "review rejected non-atomic update",
            str(tmp_path),
            plan_data=plan,
            failed_step="review",
            failure_code="gate_failed",
            fix_attempt=2,
        )
    )

    prompt = backend.run.await_args.kwargs["prompt"]
    assert "## Complete planner implementation and regression scope" in prompt
    assert "Concurrent rotation cannot reuse the old session" in prompt
    assert "app.sessions.rotate" in prompt
    assert "app.sessions.RotationResult" in prompt
    assert "Original planned target files" in prompt
    assert "Failed step: review" in prompt
    assert "Failure code: gate_failed" in prompt
    assert "Fix attempt: 2" in prompt


def test_ordinary_fix_forwards_failure_detail_and_restarts_gate_progression(tmp_path):
    detail = {
        "gate": "security",
        "remediation": [{"package_name": "example", "advisory_id": "GHSA-1234"}],
    }
    job = _job(
        stage=Stage.FIX,
        status=JobStatus.RUNNING,
        failure="security rejected: vulnerable dependency",
        failure_detail=detail,
    )
    worktree = tmp_path / f"job-{job.id}"
    worktree.mkdir()
    runner = MagicMock()
    runner.worktrees = tmp_path
    runner.store = MagicMock()
    runner.timeout = 60
    runner.notify = AsyncMock()
    runner._event = MagicMock()
    runner._record_resource = MagicMock()
    runner._backend.return_value = MagicMock()
    runner._managed_repo = AsyncMock(return_value=tmp_path)

    with (
        patch.object(fix.agents, "fix", AsyncMock(return_value=("fixed", Usage()))) as agent_fix,
        patch.object(fix.gitops, "verify_worktree_identity", AsyncMock()),
        patch.object(fix.gitops, "dirty_paths", AsyncMock(return_value=set())),
        patch.object(fix.gitops, "tracked_files", AsyncMock(return_value=[])),
        patch.object(fix.gitops, "commit_all", AsyncMock(return_value=True)),
        patch.object(fix.gitops, "patch", AsyncMock(return_value="diff")),
        patch.object(fix.gitops, "head_info", AsyncMock(return_value={})),
        patch.object(fix.gitops, "numstat", AsyncMock(return_value=[])),
        patch.object(fix.github, "has_remote", AsyncMock(return_value=False)),
    ):
        asyncio.run(fix.run(runner, job))

    assert agent_fix.await_args.kwargs["failure_detail"] is detail
    assert agent_fix.await_args.kwargs["plan_data"] is job.plan
    assert agent_fix.await_args.kwargs["failed_step"] == (job.failed_step or "")
    assert agent_fix.await_args.kwargs["failure_code"] == (job.failure_code or "")
    assert agent_fix.await_args.kwargs["fix_attempt"] == job.attempts
    assert job.stage is Stage.LINT
    assert job.status is JobStatus.PENDING
    runner.store.save.assert_called_once_with(job)

    assert HANDLERS[Stage.LINT].__module__ == "hyqs.pipeline.stages.lint"
    assert HANDLERS[Stage.BUILD].__module__ == "hyqs.pipeline.stages.test"
    assert HANDLERS[Stage.TEST].__module__ == "hyqs.pipeline.stages.review"
    assert HANDLERS[Stage.REVIEW].__module__ == "hyqs.pipeline.stages.security"


def test_fix_omits_gate_history_from_agent_facing_failure_detail(tmp_path):
    """gate_history is pipeline-internal oscillation bookkeeping; the fixer
    prompt must never see it, only the gate-specific evidence."""
    detail = {
        "gate": "security",
        "gate_history": [["review", ["", []]]],
        "remediation": [{"package_name": "example", "advisory_id": "GHSA-1234"}],
    }
    job = _job(
        stage=Stage.FIX,
        status=JobStatus.RUNNING,
        failure="security rejected: vulnerable dependency",
        failure_detail=detail,
    )
    worktree = tmp_path / f"job-{job.id}"
    worktree.mkdir()
    runner = MagicMock()
    runner.worktrees = tmp_path
    runner.store = MagicMock()
    runner.timeout = 60
    runner.notify = AsyncMock()
    runner._event = MagicMock()
    runner._record_resource = MagicMock()
    runner._backend.return_value = MagicMock()
    runner._managed_repo = AsyncMock(return_value=tmp_path)

    with (
        patch.object(fix.agents, "fix", AsyncMock(return_value=("fixed", Usage()))) as agent_fix,
        patch.object(fix.gitops, "verify_worktree_identity", AsyncMock()),
        patch.object(fix.gitops, "dirty_paths", AsyncMock(return_value=set())),
        patch.object(fix.gitops, "tracked_files", AsyncMock(return_value=[])),
        patch.object(fix.gitops, "commit_all", AsyncMock(return_value=True)),
        patch.object(fix.gitops, "patch", AsyncMock(return_value="diff")),
        patch.object(fix.gitops, "head_info", AsyncMock(return_value={})),
        patch.object(fix.gitops, "numstat", AsyncMock(return_value=[])),
        patch.object(fix.github, "has_remote", AsyncMock(return_value=False)),
    ):
        asyncio.run(fix.run(runner, job))

    forwarded_detail = agent_fix.await_args.kwargs["failure_detail"]
    assert "gate_history" not in forwarded_detail
    assert forwarded_detail == {
        "gate": "security",
        "remediation": [{"package_name": "example", "advisory_id": "GHSA-1234"}],
    }
    # The job's own persisted failure_detail keeps gate_history intact.
    assert job.failure_detail["gate_history"] == [["review", ["", []]]]


def test_build_failure_records_actual_failed_step_while_checkpoint_stays_plan():
    runner = object.__new__(PipelineRunner)
    runner.store = MagicMock()
    runner.notify = AsyncMock()
    job = _job(stage=Stage.PLAN, status=JobStatus.RUNNING, branch="")
    job.executing_step = "build"

    asyncio.run(
        runner._fail(
            job,
            "verifier rejected candidate",
            failure_code="no_diff_verification_failed",
            failure_origin="ai_gate",
            retry_disposition="human_review",
        )
    )

    assert job.stage is Stage.PLAN
    assert job.failed_step == "build"
    assert job.executing_step is None
    runner.store.save.assert_called_once_with(job)


def test_retry_or_fail_preserves_structured_detail_when_routing_to_fix():
    runner = object.__new__(PipelineRunner)
    runner.store = MagicMock()
    runner.store.get_project.return_value = None
    runner.notify = AsyncMock()
    runner.max_attempts = 2
    job = _job(stage=Stage.REVIEW, status=JobStatus.RUNNING, branch="")
    job.executing_step = "review"
    detail: FailureDetail = {
        "gate": "review",
        "evidence": {"findings": ["missing authorization check"]},
    }

    asyncio.run(
        runner._retry_or_fail(
            job,
            "review rejected the authorization path",
            failure_detail=detail,
        )
    )

    assert detail == {
        "gate": "review",
        "evidence": {"findings": ["missing authorization check"]},
    }
    assert job.failure == "review rejected the authorization path"
    assert job.failure_code == "gate_failed"
    assert job.failure_origin == "ai_gate"
    assert job.retry_disposition == "fix_worktree"
    assert job.failure_detail == {
        **detail,
        "attempt": 1,
        "gate_history": [["review", ["", []]]],
    }
    assert job.attempts == 1
    assert job.failed_step == "review"
    assert job.executing_step is None
    assert job.stage is Stage.FIX
    assert job.status is JobStatus.PENDING
    runner.store.save.assert_called_once_with(job)
    runner.notify.assert_awaited_once()


def test_retry_or_fail_preserves_structured_detail_when_attempts_exhausted():
    runner = object.__new__(PipelineRunner)
    runner.store = MagicMock()
    runner.store.get_project.return_value = None
    runner.notify = AsyncMock()
    runner.max_attempts = 2
    job = _job(
        stage=Stage.TEST,
        status=JobStatus.RUNNING,
        branch="",
        attempts=2,
    )
    job.executing_step = "test"
    detail: FailureDetail = {
        "gate": "pytest",
        "evidence": {"failed_tests": ["test_private_route"]},
    }

    asyncio.run(
        runner._retry_or_fail(
            job,
            "test suite failed",
            failure_detail=detail,
        )
    )

    assert detail == {
        "gate": "pytest",
        "evidence": {"failed_tests": ["test_private_route"]},
    }
    assert job.error == "gave up after 2 fix attempt(s).\n\ntest suite failed"
    assert job.failure_code == "test_failed"
    assert job.failure_origin == "candidate_code"
    assert job.retry_disposition == "human_review"
    assert job.failure_detail == {**detail, "attempts": 2}
    assert job.attempts == 2
    assert job.failed_step == "test"
    assert job.executing_step is None
    assert job.stage is Stage.TEST
    assert job.status is JobStatus.FAILED
    runner.store.save.assert_called_once_with(job)
    runner.notify.assert_awaited_once()


def test_retry_or_fail_without_detail_preserves_attempt_only_shape():
    runner = object.__new__(PipelineRunner)
    runner.store = MagicMock()
    runner.store.get_project.return_value = None
    runner.notify = AsyncMock()
    runner.max_attempts = 2
    job = _job(stage=Stage.TEST, status=JobStatus.RUNNING, branch="")

    asyncio.run(runner._retry_or_fail(job, "test suite failed"))

    assert job.failure_detail == {
        "attempt": 1,
        "gate_history": [["review", ["", []]]],
    }
    assert job.failure == "test suite failed"
    assert job.failure_code == "gate_failed"
    assert job.failure_origin == "ai_gate"
    assert job.retry_disposition == "fix_worktree"
    assert job.attempts == 1
    assert job.stage is Stage.FIX
    assert job.status is JobStatus.PENDING
    runner.store.save.assert_called_once_with(job)
    runner.notify.assert_awaited_once()


def _security_fail_verdict() -> dict:
    return {
        "summary": "security rejected: banned symbol usage",
        "findings": [{"severity": "blocking", "note": "usage of banned symbol eval() detected"}],
    }


def _review_fail_verdict() -> dict:
    return {
        "summary": "review rejected: contract violation",
        "findings": [{"severity": "blocking", "note": "is_enabled contract violated"}],
    }


def test_retry_or_fail_halts_on_alternating_review_security_oscillation():
    """Replays job #3950: review and security gates keep flip-flopping on the
    same two findings — this must halt as gate_conflict instead of burning
    fix attempts forever."""
    runner = object.__new__(PipelineRunner)
    runner.store = MagicMock()
    runner.store.get_project.return_value = None
    runner.notify = AsyncMock()
    runner.max_attempts = 5
    job = _job(stage=Stage.REVIEW, status=JobStatus.RUNNING, branch="", attempts=0)

    job.executing_step = "security"
    job.security_review = _security_fail_verdict()
    asyncio.run(runner._retry_or_fail(job, "security rejected the candidate"))

    job.executing_step = "review"
    job.review = _review_fail_verdict()
    asyncio.run(runner._retry_or_fail(job, "review rejected the candidate"))

    job.executing_step = "security"
    job.security_review = _security_fail_verdict()
    asyncio.run(runner._retry_or_fail(job, "security rejected the candidate"))

    job.executing_step = "review"
    job.review = _review_fail_verdict()
    asyncio.run(runner._retry_or_fail(job, "review rejected the candidate"))

    assert job.attempts == 3
    assert job.status is JobStatus.FAILED
    assert job.failure_code == "gate_conflict"
    assert job.retry_disposition == "human_review"
    assert "review" in job.error
    assert "security" in job.error


def test_retry_or_fail_keeps_retrying_when_findings_genuinely_converge():
    """Four consecutive review/security failures with distinct finding text
    each time are genuine progress, not oscillation — the loop must keep
    retrying instead of halting on gate_conflict."""
    runner = object.__new__(PipelineRunner)
    runner.store = MagicMock()
    runner.store.get_project.return_value = None
    runner.notify = AsyncMock()
    runner.max_attempts = 5
    job = _job(stage=Stage.REVIEW, status=JobStatus.RUNNING, branch="", attempts=0)

    steps = ["security", "review", "security", "review"]
    for i, step in enumerate(steps):
        job.executing_step = step
        verdict = {
            "summary": f"{step} rejected: distinct finding {i}",
            "findings": [{"severity": "blocking", "note": f"unique finding text {i}"}],
        }
        if step == "security":
            job.security_review = verdict
        else:
            job.review = verdict
        asyncio.run(runner._retry_or_fail(job, f"{step} rejected the candidate"))
        assert job.failure_code != "gate_conflict"

    assert job.attempts == 4
    assert job.stage is Stage.FIX
    assert job.status is JobStatus.PENDING


# ---------------------------------------------------------------------------
# Classifier rules
# ---------------------------------------------------------------------------


def test_classify_timeout_exhaustion_is_genuine_code_not_transient_or_stale():
    """The runner's give-up message after timeout_attempts is spent must escalate.

    Before: "timed out" matched the transient keywords (or stage TEST matched
    stale_branch), so the janitor kept requeuing a job whose timeout budget was
    already spent — full pipeline reruns each dying on their first timeout.
    """
    job = _job(
        stage=Stage.TEST,
        error="stage review timed out 2 time(s) (>1800s each) without completing; giving up.",
    )
    assert classify_failure(job) is FailureClass.genuine_code


def test_classify_fix_no_changes_non_gate_failure():
    """Fixer/reviewer disagreement escalates instead of stale_branch full reruns."""
    job = _job(
        stage=Stage.FIX,
        error="fix attempt 2 produced no changes",
        failure="review rejected: needs different approach",
    )
    assert classify_failure(job) is FailureClass.fix_no_changes


def test_classify_gate_no_changes_still_wins_for_gate_failures():
    job = _job(
        stage=Stage.FIX,
        error="fix attempt 1 produced no changes",
        failure="lint failed (ruff check): E501 ...",
    )
    assert classify_failure(job) is FailureClass.gate_no_changes


def test_classify_deploy_stage_routes_to_merged_but_stuck():
    # Every DEPLOY-stage failure now routes to merged_but_stuck first (job
    # #610): the janitor verifies the PR is merged before ever looking at the
    # error/failure text for the env/attributable/config split.
    job = _job(stage=Stage.DEPLOY, error="", failure="deploy failed: module not found: x")
    assert classify_failure(job) is FailureClass.merged_but_stuck


# ---------------------------------------------------------------------------
# Remediations
# ---------------------------------------------------------------------------


def test_remediate_transient_requeues_at_failed_stage_preserving_failure():
    jobs = MagicMock()
    jobs.supervisor_requeue_count.return_value = 0
    job = _job(stage=Stage.REVIEW, error="unexpected error during review: boom", failure="prior")

    asyncio.run(remediate_transient(jobs, job, config=MagicMock()))

    jobs.requeue_job_at_stage.assert_called_once_with(job.id, Stage.REVIEW, failure="prior")
    jobs.requeue_job.assert_not_called()


@pytest.mark.skip(reason="hard file-scope gate removed; worktrees provide isolation")
def test_transient_requeue_preserves_scope_correction_marker_without_poisoning_ordinary_fix(
    store, tmp_path
):
    repo_path = f"/tmp/test-scope-correction-requeue-{uuid.uuid4()}"
    job = store.create(
        idea="remove an out-of-scope file",
        repo_path=repo_path,
        chat_id=1,
        source_meta={
            "scope": {"allowed_paths": ["hyqs/pipeline/store.py"]},
            "filing_channel": "supervisor",
        },
    )
    try:
        job.stage = Stage.FIX
        job.status = JobStatus.FAILED
        job.failure = "[out-of-lane] build scope preflight found hyqs/web/app.py"
        job.failure_code = "build_scope_preflight_failed"
        job.retry_disposition = "fix_worktree_without_budget"
        job.failure_detail = {"unexpected_paths": ["hyqs/web/app.py"]}
        store.save(job)
        assert store.mark_early_scope_correction_attempt(job.id) is True

        failed = store.get(job.id)
        asyncio.run(remediate_transient(store, failed, config=MagicMock()))
        reclaimed = store.get(job.id)

        assert reclaimed.status == JobStatus.PENDING
        assert reclaimed.failure_code is None
        assert reclaimed.failure_detail is None
        assert reclaimed.source_meta["filing_channel"] == "supervisor"
        assert reclaimed.source_meta["early_scope_correction"]["attempted_at"]

        rn = MagicMock()
        rn.worktrees = tmp_path
        rn.store = store
        rn.timeout = 60
        rn.notify = AsyncMock()
        rn._event = MagicMock()
        rn._record_resource = MagicMock()
        rn._fail = AsyncMock()
        rn._backend = MagicMock(return_value=MagicMock())
        rn._managed_repo = AsyncMock(return_value=repo_path)
        (tmp_path / f"job-{job.id}").mkdir()
        with (
            patch(
                "hyqs.pipeline.stages.fix.agents.fix",
                AsyncMock(return_value=("fixed ordinary retry", Usage())),
            ) as agent_fix,
            patch("hyqs.pipeline.stages.fix.gitops.tracked_files", AsyncMock(return_value=[])),
            patch("hyqs.pipeline.stages.fix.gitops.commit_all", AsyncMock(return_value=True)),
            patch("hyqs.pipeline.stages.fix.gitops.patch", AsyncMock(return_value="diff")),
            patch("hyqs.pipeline.stages.fix.gitops.head_info", AsyncMock(return_value={})),
            patch("hyqs.pipeline.stages.fix.gitops.numstat", AsyncMock(return_value=[])),
            patch("hyqs.pipeline.stages.fix.github.has_remote", AsyncMock(return_value=False)),
        ):
            asyncio.run(fix.run(rn, reclaimed))

        agent_fix.assert_awaited_once()
        rn._backend.assert_called_once()
        rn._fail.assert_not_awaited()
        persisted = store.get(job.id)
        assert persisted.stage == Stage.LINT
        assert persisted.source_meta["early_scope_correction"]["attempted_at"]
    finally:
        project = store.get_project(job.project_id)
        if project is not None:
            store.delete_project(project.id)


@pytest.mark.skip(reason="hard file-scope gate removed; worktrees provide isolation")
def test_authorized_scope_amendment_correlates_event_and_persists_before_reload(tmp_path):
    identity = {
        "gate": "test",
        "check_id": "pytest/project",
        "event_id": "failure/7",
        "failing_paths": ["tests/test_related.py"],
        "categories": ["coverage"],
    }
    job = _job(
        stage=Stage.FIX,
        failure_detail={"authorized_gate_failure": identity},
        source_meta={
            "scope": {"allowed_paths": ["src/feature.py"], "frozen": True},
            "scope_amendments": {"sequence": 0, "cumulative_paths": []},
        },
        plan={"target_files": ["src/feature.py"]},
        project_id=3,
    )
    worktree = tmp_path / "worktrees" / f"job-{job.id}"
    worktree.mkdir(parents=True)
    store = MagicMock()
    store.list_events.return_value = [
        {
            "id": 40,
            "stage": "test",
            "status": "failed",
            "detail": {"authorized_gate_failure": identity},
        },
        {
            "id": 41,
            "stage": "test",
            "status": "failed",
            "detail": {
                "authorized_gate_failure": {
                    **identity,
                    "event_id": "different/8",
                }
            },
        },
    ]
    store.list_active.return_value = []
    store.append_scope_amendment.return_value = ScopeAmendmentPersistenceResult(
        status="applied",
        sequence=1,
        newly_accepted_paths=("tests/test_related.py",),
        cumulative_amendment_paths=("tests/test_related.py",),
        cumulative_allowed_paths=("src/feature.py", "tests/test_related.py"),
    )
    rn = object.__new__(PipelineRunner)
    rn.store = store
    rn.worktrees = tmp_path / "worktrees"
    rn._event = MagicMock()

    with (
        patch(
            "hyqs.pipeline.runner.gitops.tracked_files",
            new_callable=AsyncMock,
            return_value=["src/feature.py", "tests/test_related.py"],
        ),
        patch(
            "hyqs.pipeline.runner.gitops.numstat",
            new_callable=AsyncMock,
            return_value=[{"path": "src/feature.py"}],
        ),
    ):
        assert asyncio.run(rn._apply_authorized_scope_amendment(job)) is True

    store.list_events.assert_called_once_with(job.id)
    persisted = store.append_scope_amendment.call_args
    assert persisted.kwargs["failure_event_id"] == "failure/7"
    assert persisted.kwargs["expected_prior_sequence"] == 0
    assert persisted.args[1].accepted_paths == ("tests/test_related.py",)
    audit = rn._event.call_args.kwargs["detail"]
    assert audit["check_id"] == "pytest/project"
    assert audit["sequence"] == 1
    assert audit["persistence_status"] == "applied"


@pytest.mark.parametrize(
    "events",
    [
        [],
        [
            {
                "stage": "test",
                "status": "failed",
                "detail": {
                    "authorized_gate_failure": {
                        "gate": "test",
                        "check_id": "pytest/project",
                        "event_id": "other",
                        "failing_paths": ["tests/test_related.py"],
                        "categories": ["coverage"],
                    }
                },
            }
        ],
    ],
)
@pytest.mark.skip(reason="hard file-scope gate removed; worktrees provide isolation")
def test_authorized_scope_amendment_missing_or_mismatched_event_fails_closed(tmp_path, events):
    identity = {
        "gate": "test",
        "check_id": "pytest/project",
        "event_id": "failure/7",
        "failing_paths": ["tests/test_related.py"],
        "categories": ["coverage"],
    }
    job = _job(stage=Stage.FIX, failure_detail={"authorized_gate_failure": identity})
    store = MagicMock()
    store.list_events.return_value = events
    rn = object.__new__(PipelineRunner)
    rn.store = store
    rn.worktrees = tmp_path
    rn._event = MagicMock()

    assert asyncio.run(rn._apply_authorized_scope_amendment(job)) is False
    store.append_scope_amendment.assert_not_called()
    assert rn._event.call_args.args[2] == "rejected"


@pytest.mark.skip(reason="hard file-scope gate removed; worktrees provide isolation")
def test_fix_reloads_applied_scope_before_managed_repo_and_backend(tmp_path):
    identity = {
        "gate": "test",
        "check_id": "pytest/project",
        "event_id": "failure/7",
        "failing_paths": ["tests/test_related.py"],
        "categories": ["coverage"],
    }
    original = _job(
        stage=Stage.FIX,
        failure="pytest failed",
        failure_detail={"authorized_gate_failure": identity, "diagnostic": {"exit": 1}},
    )
    refreshed = _job(
        stage=Stage.FIX,
        failure="pytest failed",
        failure_detail=original.failure_detail,
        source_meta={
            "scope": {
                "allowed_paths": ["src/feature.py", "tests/test_related.py"],
                "frozen": True,
            }
        },
    )
    (tmp_path / f"job-{original.id}").mkdir()
    order = []
    rn = MagicMock()
    rn.worktrees = tmp_path
    rn.store.get.side_effect = lambda job_id: order.append("get") or refreshed
    rn._apply_authorized_scope_amendment = AsyncMock(
        side_effect=lambda job: order.append("amend") or True
    )
    rn._managed_repo = AsyncMock(side_effect=lambda job: order.append("managed") or tmp_path)

    def backend(job):
        order.append("backend")
        assert job.source_meta["scope"]["allowed_paths"][-1] == "tests/test_related.py"
        raise RuntimeError("stop after backend ordering assertion")

    rn._backend.side_effect = backend

    with pytest.raises(RuntimeError, match="stop after backend"):
        asyncio.run(fix.run(rn, original))

    assert order == ["amend", "get", "managed", "backend"]
    assert original.failure_detail == {
        "authorized_gate_failure": identity,
        "diagnostic": {"exit": 1},
    }


@pytest.mark.skip(reason="hard file-scope gate removed; worktrees provide isolation")
def test_authorized_scope_amendment_limit_rejection_does_not_partially_widen(tmp_path):
    paths = [f"tests/test_related_{number}.py" for number in range(9)]
    identity = {
        "gate": "test",
        "check_id": "pytest/project",
        "event_id": "failure/limit",
        "failing_paths": paths,
        "categories": ["coverage"],
    }
    job = _job(
        stage=Stage.FIX,
        failure_detail={"authorized_gate_failure": identity},
        source_meta={"scope": {"allowed_paths": ["src/feature.py"], "frozen": True}},
        plan={"target_files": ["src/feature.py"]},
    )
    (tmp_path / f"job-{job.id}").mkdir()
    store = MagicMock()
    store.list_events.return_value = [
        {
            "stage": "test",
            "status": "failed",
            "detail": {"authorized_gate_failure": identity},
        }
    ]

    def persist(job_id, decision, **kwargs):
        assert decision.accepted == ()
        assert {item.reason_code for item in decision.rejected} >= {
            "per_cycle_limit",
            "limit_rejected_cycle",
        }
        return ScopeAmendmentPersistenceResult(
            status="applied",
            sequence=1,
            newly_accepted_paths=(),
            cumulative_amendment_paths=(),
            cumulative_allowed_paths=("src/feature.py",),
        )

    store.append_scope_amendment.side_effect = persist
    rn = object.__new__(PipelineRunner)
    rn.store = store
    rn.worktrees = tmp_path
    rn._event = MagicMock()

    with (
        patch(
            "hyqs.pipeline.runner.gitops.tracked_files",
            new_callable=AsyncMock,
            return_value=["src/feature.py", *paths],
        ),
        patch(
            "hyqs.pipeline.runner.gitops.numstat",
            new_callable=AsyncMock,
            return_value=[{"path": "src/feature.py"}],
        ),
    ):
        assert asyncio.run(rn._apply_authorized_scope_amendment(job)) is False

    assert rn._event.call_args.args[2] == "rejected"


def test_remediate_merged_but_stuck_returns_merged_state():
    jobs = MagicMock()
    job = _job(stage=Stage.REVIEW)

    with patch(
        "hyqs.pipeline.supervisor.github.pr_is_merged", new_callable=AsyncMock
    ) as pr_is_merged:
        pr_is_merged.return_value = True
        assert asyncio.run(remediate_merged_but_stuck(jobs, job, config=MagicMock())) is True
        jobs.reconcile_to_done.assert_called_once_with(job.id)

        pr_is_merged.return_value = False
        jobs.reset_mock()
        assert asyncio.run(remediate_merged_but_stuck(jobs, job, config=MagicMock())) is False
        jobs.reconcile_to_done.assert_not_called()


def test_janitor_routes_unmerged_stuck_job_to_transient_requeue():
    """A crash at the security stage (job.stage=REVIEW, PR not merged) must be
    requeued on the transient budget — not silently left FAILED forever."""
    job = _job(stage=Stage.REVIEW, error="unexpected error during review: boom")
    jobs = MagicMock()
    jobs.get_failed_jobs.return_value = [job]
    jobs.get_active_job_ids.return_value = []
    jobs.supervisor_requeue_count.return_value = 0

    with (
        patch(
            "hyqs.pipeline.supervisor.github.pr_is_merged",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "hyqs.pipeline.supervisor.gitops.gc_orphaned_artifacts",
            new_callable=AsyncMock,
        ),
    ):
        asyncio.run(_janitor_scan(jobs, config=MagicMock(), notify=AsyncMock()))

    jobs.requeue_job_at_stage.assert_called_once_with(job.id, Stage.REVIEW, failure="")


def test_janitor_reconciles_merged_deploy_stage_job_to_done():
    """Job #610: PR already merged and deployed, but the worker's DEPLOY-stage
    txn was killed by a schema-migration deadlock. Must reconcile to DONE
    without cascade-failing dependents — not requeue a fix-forward job."""
    job = _job(stage=Stage.DEPLOY, error="deploy failed: deadlock detected (SQLSTATE 40P01)")
    jobs = MagicMock()
    jobs.get_failed_jobs.return_value = [job]
    jobs.get_active_job_ids.return_value = []

    with (
        patch(
            "hyqs.pipeline.supervisor.github.pr_is_merged",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "hyqs.pipeline.supervisor.gitops.gc_orphaned_artifacts",
            new_callable=AsyncMock,
        ),
    ):
        asyncio.run(_janitor_scan(jobs, config=MagicMock(), notify=AsyncMock()))

    jobs.reconcile_to_done.assert_called_once_with(job.id)
    jobs.create.assert_not_called()
    jobs.save.assert_not_called()


def test_janitor_routes_unmerged_deploy_stage_attributable_to_fix_forward():
    """PR NOT merged + an attributable-pattern deploy error must still file
    the fix-forward job (remediate_deploy_failed), not a generic transient
    requeue."""
    job = _job(
        stage=Stage.DEPLOY,
        error="deploy failed: rollup failed — cannot find module './x'",
    )
    jobs = MagicMock()
    jobs.get_failed_jobs.return_value = [job]
    jobs.get_active_job_ids.return_value = []
    jobs.has_deploy_fix_job.return_value = False
    jobs.count_deploy_fix_jobs.return_value = 0
    fix_job = MagicMock()
    fix_job.id = 99
    jobs.create.return_value = fix_job

    with (
        patch(
            "hyqs.pipeline.supervisor.github.pr_is_merged",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "hyqs.pipeline.supervisor.gitops.gc_orphaned_artifacts",
            new_callable=AsyncMock,
        ),
    ):
        asyncio.run(_janitor_scan(jobs, config=MagicMock(), notify=AsyncMock()))

    jobs.create.assert_called_once()
    call_kwargs = jobs.create.call_args.kwargs
    assert call_kwargs["source_meta"]["deploy_fix_for"] == job.id
    jobs.reconcile_to_done.assert_not_called()


def test_janitor_routes_unmerged_deploy_stage_env_to_env_deploy_requeue():
    """PR NOT merged + an env-pattern deploy error must take the
    cleanup+requeue-at-DEPLOY path (remediate_env_deploy), not a generic
    transient requeue or a fix-forward job."""
    job = _job(
        stage=Stage.DEPLOY,
        error="deploy failed: no space left on device",
    )
    jobs = MagicMock()
    jobs.get_failed_jobs.return_value = [job]
    jobs.get_active_job_ids.return_value = []
    jobs.supervisor_requeue_count.return_value = 0

    with (
        patch(
            "hyqs.pipeline.supervisor.github.pr_is_merged",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "hyqs.pipeline.supervisor.gitops.gc_orphaned_artifacts",
            new_callable=AsyncMock,
        ),
    ):
        asyncio.run(_janitor_scan(jobs, config=MagicMock(), notify=AsyncMock()))

    jobs.requeue_job_at_stage.assert_called_once_with(job.id, Stage.DEPLOY)
    jobs.reconcile_to_done.assert_not_called()
    jobs.create.assert_not_called()


# ---------------------------------------------------------------------------
# Deploy stage: fail at DEPLOY, never detour into FIX
# ---------------------------------------------------------------------------


def test_deploy_failure_fails_at_deploy_stage_not_fix():
    """Attributable-looking deploy errors used to _retry_or_fail into a FIX stage
    that cannot run (worktree removed at merge) and hid the DEPLOY stage from
    the classifier. They must _fail at DEPLOY."""

    async def _run():
        store = MagicMock()
        store.acquire_deploy_lock = AsyncMock(return_value=True)
        store.get_project.return_value = MagicMock(deploy_config="")
        store.get_last_deploy.return_value = None

        rn = MagicMock()
        rn.store = store
        rn.config = MagicMock(
            pipeline_deploy_cmd="", projects_dir="/fake/projects", pipeline_web_service=""
        )
        rn.notify = AsyncMock()
        rn._managed_repo = AsyncMock(return_value="/fake/repo")
        rn._fail = AsyncMock()
        rn._retry_or_fail = AsyncMock()

        failed_result = {
            "deployed": False,
            "command": "docker compose up",
            "output": "cannot find module 'x' — rollup failed",
            "resource": None,
        }
        with (
            patch("hyqs.pipeline.stages.deploy.gitops.git", new_callable=AsyncMock) as g,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=failed_result,
            ),
        ):
            g.return_value = MagicMock(ok=True, stdout="abc\n", stderr="")
            job = _job(stage=Stage.DEPLOY, status=JobStatus.RUNNING, project_id=1)
            await deploy_run(rn, job)

        rn._fail.assert_called_once()
        rn._retry_or_fail.assert_not_called()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Fix stage: worktree missing after post-merge cleanup is a typed infrastructure
# failure, not an unclassifiable one (job #3970's cascade)
# ---------------------------------------------------------------------------


def test_fix_worktree_missing_fails_with_typed_infrastructure_failure(tmp_path):
    async def _run():
        rn = MagicMock()
        rn.worktrees = tmp_path / "worktrees"  # job-{id} subdir intentionally absent
        rn._managed_repo = AsyncMock(return_value="/fake/repo")
        rn._fail = AsyncMock()

        job = _job(stage=Stage.FIX, status=JobStatus.RUNNING)
        await fix.run(rn, job)

        rn._fail.assert_called_once_with(
            job,
            "fix stage: worktree missing after post-merge cleanup",
            failure_code="worktree_missing",
            failure_origin="infrastructure",
            retry_disposition="same_step",
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Runner: timeout keeps the worktree usable for the same-stage retry
# ---------------------------------------------------------------------------


def _make_runner(tmp_path) -> PipelineRunner:
    store = MagicMock()
    store.renew_lease = AsyncMock()
    store.worker_heartbeat = AsyncMock()
    config = SimpleNamespace(model="test-model", data_dir=str(tmp_path / "data"))
    return PipelineRunner(store, config, notify=AsyncMock())


def test_already_satisfied_sends_warning_toned_notification(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(stage=Stage.BUILD, status=JobStatus.RUNNING)

    async def _run():
        await rn._already_satisfied(job, started="2026-07-16T00:00:00Z", usage=Usage())

    asyncio.run(_run())

    assert job.status == JobStatus.DONE
    assert job.resolution == "already-satisfied"
    rn.notify.assert_called_once()
    _args, kwargs = rn.notify.call_args
    text = _args[1]
    assert "⚠️" in text
    assert "✅" not in text


def test_already_satisfied_verification_fail_closes(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(
        stage=Stage.BUILD,
        status=JobStatus.RUNNING,
        idea="Add GET /required",
        plan={"stories": [{"id": "S1", "acceptance": "GET /required returns 200"}]},
    )
    backend = _verification_backend(
        '<<<RESULT_JSON>>>{"verdict":"fail","evidence":"GET /required is absent"}<<<END_RESULT>>>'
    )

    async def _run():
        with (
            patch("hyqs.pipeline.runner.gitops.remove_worktree", new_callable=AsyncMock),
            patch(
                "hyqs.pipeline.runner.gitops.fresh_base",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.runner.gitops.create_detached_worktree",
                new_callable=AsyncMock,
                return_value=MagicMock(ok=True),
            ),
            patch.object(rn, "_already_satisfied", new_callable=AsyncMock) as complete,
        ):
            await rn._verify_already_satisfied(
                job,
                "2026-07-27T00:00:00Z",
                managed=tmp_path / "managed",
                worktree=tmp_path / "verify",
                base="main",
                backend=backend,
            )
            complete.assert_not_awaited()

    asyncio.run(_run())

    assert job.status == JobStatus.FAILED
    assert job.failed_step == "build"
    assert job.retry_disposition == "retry_build"
    assert job.failure_detail["retry_attempt"] == 0
    assert "GET /required is absent" in job.error
    event = rn.store.add_event.call_args_list[-1]
    assert event.kwargs["detail"]["verification"] == "failed"


def test_repeated_no_diff_verification_is_terminal(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(stage=Stage.PLAN, status=JobStatus.RUNNING, idea="Add GET /required")
    job.executing_step = "build"
    backend = _verification_backend(
        '<<<RESULT_JSON>>>{"verdict":"fail","evidence":"route remains absent"}<<<END_RESULT>>>'
    )

    async def _run():
        with (
            patch("hyqs.pipeline.runner.gitops.remove_worktree", new_callable=AsyncMock),
            patch(
                "hyqs.pipeline.runner.gitops.fresh_base",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.runner.gitops.create_detached_worktree",
                new_callable=AsyncMock,
                return_value=MagicMock(ok=True),
            ),
        ):
            await rn._verify_already_satisfied(
                job,
                "2026-07-27T00:00:00Z",
                managed=tmp_path / "managed",
                worktree=tmp_path / "verify",
                base="main",
                backend=backend,
                no_diff_retry=True,
            )

    asyncio.run(_run())

    assert job.status == JobStatus.FAILED
    assert job.failed_step == "build"
    assert job.retry_disposition == "terminal"
    assert job.failure_detail["retry_attempt"] == 1
    assert classify_failure(job) is FailureClass.genuine_code


def test_already_satisfied_verification_passes_fresh_checkout(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(stage=Stage.BUILD, status=JobStatus.RUNNING, idea="Expose health")
    backend = _verification_backend(
        '<<<RESULT_JSON>>>{"verdict":"pass","evidence":"health route exists"}<<<END_RESULT>>>'
    )
    worktree = tmp_path / "verify"

    async def _run():
        with (
            patch("hyqs.pipeline.runner.gitops.remove_worktree", new_callable=AsyncMock) as remove,
            patch(
                "hyqs.pipeline.runner.gitops.fresh_base",
                new_callable=AsyncMock,
                return_value="origin/main",
            ) as fresh,
            patch(
                "hyqs.pipeline.runner.gitops.create_detached_worktree",
                new_callable=AsyncMock,
                return_value=MagicMock(ok=True),
            ) as create,
            patch.object(rn, "_already_satisfied", new_callable=AsyncMock) as complete,
        ):
            await rn._verify_already_satisfied(
                job,
                "2026-07-27T00:00:00Z",
                managed=tmp_path / "managed",
                worktree=worktree,
                base="main",
                backend=backend,
            )
            remove.assert_awaited_once()
            fresh.assert_awaited_once_with(tmp_path / "managed", "main")
            create.assert_awaited_once_with(tmp_path / "managed", worktree, ref="origin/main")
            complete.assert_awaited_once()

    asyncio.run(_run())

    assert backend.run.call_args.kwargs["role"].value == "reviewer"
    assert backend.run.call_args.kwargs["cwd"] == str(worktree)


def test_timeout_resets_worktree_for_post_plan_stage(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(stage=Stage.TEST, status=JobStatus.RUNNING, repo_path=str(tmp_path / "repo"))
    worktree = rn.worktrees / f"job-{job.id}"
    worktree.mkdir(parents=True)

    async def _run():
        with (
            patch("hyqs.pipeline.runner.gitops.remove_worktree", new_callable=AsyncMock) as rm,
            patch(
                "hyqs.pipeline.runner.gitops.restore_gate_worktree", new_callable=AsyncMock
            ) as restore,
            patch.object(rn, "_advance", new_callable=AsyncMock, side_effect=asyncio.TimeoutError),
        ):
            try:
                await rn._run_leased("w1", job)
            except asyncio.TimeoutError:
                pass
            else:  # pragma: no cover
                raise AssertionError("TimeoutError should propagate")
            restore.assert_called_once()
            rm.assert_not_called()

    asyncio.run(_run())


def test_timeout_removes_worktree_for_plan_stage(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(stage=Stage.PLAN, status=JobStatus.RUNNING, repo_path=str(tmp_path / "repo"))

    async def _run():
        with (
            patch("hyqs.pipeline.runner.gitops.remove_worktree", new_callable=AsyncMock) as rm,
            patch(
                "hyqs.pipeline.runner.gitops.restore_gate_worktree", new_callable=AsyncMock
            ) as restore,
            patch.object(rn, "_advance", new_callable=AsyncMock, side_effect=asyncio.TimeoutError),
        ):
            try:
                await rn._run_leased("w1", job)
            except asyncio.TimeoutError:
                pass
            rm.assert_called_once()
            restore.assert_not_called()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Runner: transient Postgres errors (deadlock/serialization) are retried
# in-process instead of failing the job (job #610)
# ---------------------------------------------------------------------------


def test_run_stage_with_retry_recovers_from_transient_deadlock(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(stage=Stage.TEST, status=JobStatus.RUNNING, repo_path=str(tmp_path / "repo"))

    async def _run():
        with (
            patch.object(
                rn,
                "_advance",
                new_callable=AsyncMock,
                side_effect=[psycopg.errors.DeadlockDetected("deadlock detected"), None],
            ) as advance,
            patch("hyqs.pipeline.runner.asyncio.sleep", new_callable=AsyncMock),
        ):
            await rn._run_stage_with_retry("w1", job)
            assert advance.call_count == 2

    asyncio.run(_run())


def test_run_stage_with_retry_reraises_past_db_retry_cap(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(stage=Stage.TEST, status=JobStatus.RUNNING, repo_path=str(tmp_path / "repo"))

    async def _run():
        with (
            patch.object(
                rn,
                "_advance",
                new_callable=AsyncMock,
                side_effect=psycopg.errors.DeadlockDetected("deadlock detected"),
            ),
            patch("hyqs.pipeline.runner.asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(psycopg.errors.DeadlockDetected):
                await rn._run_stage_with_retry("w1", job)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Runner: a JobCancelled mid-stage leaves the job CANCELLED, never re-FAILED
# ---------------------------------------------------------------------------


def test_loop_swallows_job_cancelled_without_failing_or_saving(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(stage=Stage.BUILD, status=JobStatus.RUNNING)
    rn._fail = AsyncMock()

    async def _claim(*_args, **_kwargs):
        # Let this one pass through _run_stage_with_retry, then drain so the
        # loop exits cleanly after handling the JobCancelled.
        rn._drain.set()
        return job

    rn.store.claim = AsyncMock(side_effect=_claim)

    async def _run():
        with patch.object(
            rn,
            "_run_stage_with_retry",
            new_callable=AsyncMock,
            side_effect=agents.JobCancelled(job.id),
        ):
            await rn._loop("w1")

    asyncio.run(_run())

    rn.store.save.assert_not_called()
    rn._fail.assert_not_called()


# ---------------------------------------------------------------------------
# MERGE_VERIFY is an agentic stage for claim purposes
# ---------------------------------------------------------------------------


def test_merge_verify_maps_to_review_task():
    assert stage_task(Stage.MERGE_VERIFY) is AgentTask.REVIEW


# ---------------------------------------------------------------------------
# Gate reparse: malformed output re-runs the gate once, then fails closed
# ---------------------------------------------------------------------------

_VALID_REVIEW = (
    '<<<RESULT_JSON>>>\n{"verdict": "pass", "summary": "ok", "findings": []}\n<<<END_RESULT>>>'
)


def _agent_run(text: str):
    return SimpleNamespace(text=text, usage=Usage(input_tokens=1, output_tokens=1))


def test_review_reruns_gate_once_on_malformed_output():
    backend = MagicMock()
    backend.run = AsyncMock(side_effect=[_agent_run("not json at all"), _agent_run(_VALID_REVIEW)])

    data, usage = asyncio.run(agents.review(backend, "/nonexistent-worktree", "main"))

    assert backend.run.call_count == 2
    assert data["verdict"] == "pass"
    assert usage.input_tokens == 2  # both runs' usage accounted


def test_review_fails_closed_after_two_malformed_outputs():
    backend = MagicMock()
    backend.run = AsyncMock(side_effect=[_agent_run("garbage"), _agent_run("more garbage")])

    data, _usage = asyncio.run(agents.review(backend, "/nonexistent-worktree", "main"))

    assert backend.run.call_count == 2
    assert data["verdict"] == "fail"
    assert data["summary"].startswith("[ContractError]")


def test_security_reruns_gate_once_on_malformed_output():
    backend = MagicMock()
    backend.run = AsyncMock(side_effect=[_agent_run("garbage"), _agent_run(_VALID_REVIEW)])

    data, _usage = asyncio.run(agents.security(backend, "/nonexistent-worktree", "main"))

    assert backend.run.call_count == 2
    assert data["verdict"] == "pass"
