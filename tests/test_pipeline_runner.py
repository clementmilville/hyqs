"""Regression tests for stage-level provider failover orchestration (job #2870).

``PipelineRunner._pause`` used to separately pause a provider and ``job.save``
the job back to PENDING. It now routes exclusively through the atomic
``JobStore.record_provider_failover`` contract: the source-provider pause, the
same-stage requeue with lease/agent/provider cleared, and the durable
``provider_failover`` event either all land together or (on a lost ownership
race) none do. These tests cover both provider directions, the immediate-
failover-vs-waiting notification split, both-providers-paused/incompatible-
alternate behavior, assignment clearing, retry-budget preservation, and a
stale worker's lost-ownership race — with mocked notify/claim boundaries and
the real store where routing state (roster capability, pause gate) matters.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyqs.pipeline.github import PullRequest, PullRequestState
from hyqs.pipeline.limits import ProviderUnavailable, pause_until
from hyqs.pipeline.models import Job, JobStatus, ProviderFailoverTransition, Stage
from hyqs.pipeline.runner import PipelineRunner


def _job(**kw) -> Job:
    defaults = dict(
        id=7,
        idea="test idea",
        repo_path="/fake/repo",
        chat_id=1,
        stage=Stage.PLAN,
        status=JobStatus.RUNNING,
        owner="worker-a",
        provider="claude",
    )
    defaults.update(kw)
    return Job(**defaults)


def _make_runner(tmp_path, store=None) -> PipelineRunner:
    store = store or MagicMock()
    store.worker_heartbeat = AsyncMock()
    store.renew_lease = AsyncMock()
    config = SimpleNamespace(model="test-model", data_dir=str(tmp_path / "data"))
    return PipelineRunner(store, config, notify=AsyncMock())


def _transition(**kw) -> ProviderFailoverTransition:
    defaults = dict(applied=True, job_id=7, stage=Stage.PLAN, source_provider="claude", event_id=1)
    defaults.update(kw)
    return ProviderFailoverTransition(**defaults)


def _startup_reconcile_store(job: Job) -> MagicMock:
    store = MagicMock()
    store.stuck_at_merge.return_value = [job]
    store.get_deploying_jobs.return_value = []
    store.reconcile_agent_tasks.return_value = []
    store.list_split_parents_with_pending_dependents.return_value = []
    store.list_projects.return_value = []
    return store


def test_test_command_timeout_uses_explicit_timeout_and_separate_budget(tmp_path):
    rn = _make_runner(tmp_path)
    rn.max_timeouts = 2
    job = _job(stage=Stage.TEST, status=JobStatus.RUNNING, project_id=None)

    asyncio.run(rn._timed_out(job, timeout_seconds=900))

    assert job.status is JobStatus.PENDING
    assert job.stage is Stage.TEST
    assert job.timeout_attempts == 1
    assert job.attempts == 0
    assert job.failure_code == "stage_timeout"
    assert job.retry_disposition == "same_step"
    assert job.failure_detail == {"attempts": 1, "timeout_seconds": 900}

    job.status = JobStatus.RUNNING
    asyncio.run(rn._timed_out(job, timeout_seconds=900))

    assert job.status is JobStatus.FAILED
    assert job.timeout_attempts == 2
    assert job.attempts == 0
    assert job.failure_code == "stage_timeout_exhausted"
    assert job.failure_detail == {"attempts": 2, "timeout_seconds": 900}


def test_startup_reconcile_advances_only_verified_merged_pr_identity(tmp_path):
    job = _job(branch="job/7", stage=Stage.MERGE, project_id=None)
    store = _startup_reconcile_store(job)
    rn = _make_runner(tmp_path, store)
    pull_request = PullRequest(
        state=PullRequestState.MERGED,
        url="https://github.com/acme/repo/pull/17",
        number=17,
        base_ref_name="main",
        head_ref_name="job/7",
        head_ref_oid="a" * 40,
    )

    with (
        patch.object(rn, "_managed_repo", new_callable=AsyncMock, return_value=job.repo_path),
        patch(
            "hyqs.pipeline.runner.gitops.default_branch",
            new_callable=AsyncMock,
            return_value="main",
        ),
        patch(
            "hyqs.pipeline.runner.github.inspect_branch_pr",
            new_callable=AsyncMock,
            return_value=pull_request,
        ) as inspect_branch_pr,
        patch(
            "hyqs.pipeline.runner.github.pr_identity_is_merged",
            new_callable=AsyncMock,
            return_value=True,
        ) as pr_identity_is_merged,
        patch("hyqs.pipeline.runner.github.sync_base", new_callable=AsyncMock),
        patch(
            "hyqs.pipeline.runner.supervisor.unblock_ready_dependents",
            new_callable=AsyncMock,
        ) as unblock_ready_dependents,
    ):
        asyncio.run(rn._reconcile_startup())

    inspect_branch_pr.assert_awaited_once_with(job.repo_path, "job/7", base="main")
    pr_identity_is_merged.assert_awaited_once_with(job.repo_path, pull_request)
    assert job.stage is Stage.DONE
    assert job.status is JobStatus.DONE
    store.save.assert_called_once_with(job)
    unblock_ready_dependents.assert_awaited_once()


@pytest.mark.parametrize(
    "pull_request",
    [
        PullRequest(state=PullRequestState.MISSING),
        PullRequest(state=PullRequestState.UNKNOWN),
        PullRequest(
            state=PullRequestState.OPEN,
            url="https://github.com/acme/repo/pull/17",
            number=17,
            base_ref_name="main",
            head_ref_name="job/7",
            head_ref_oid="a" * 40,
        ),
        PullRequest(
            state=PullRequestState.CLOSED,
            url="https://github.com/acme/repo/pull/17",
            number=17,
            base_ref_name="main",
            head_ref_name="job/7",
            head_ref_oid="a" * 40,
        ),
    ],
)
def test_startup_reconcile_leaves_non_authoritative_pr_untouched(tmp_path, pull_request):
    job = _job(branch="job/7", stage=Stage.MERGE, project_id=23)
    store = _startup_reconcile_store(job)
    rn = _make_runner(tmp_path, store)
    identity_check = AsyncMock(return_value=True)

    with (
        patch.object(rn, "_managed_repo", new_callable=AsyncMock, return_value="/managed"),
        patch(
            "hyqs.pipeline.runner.gitops.default_branch",
            new_callable=AsyncMock,
            return_value="main",
        ),
        patch(
            "hyqs.pipeline.runner.github.inspect_branch_pr",
            new_callable=AsyncMock,
            return_value=pull_request,
        ),
        patch("hyqs.pipeline.runner.github.pr_identity_is_merged", identity_check),
        patch(
            "hyqs.pipeline.runner.supervisor.unblock_ready_dependents",
            new_callable=AsyncMock,
        ) as unblock_ready_dependents,
    ):
        asyncio.run(rn._reconcile_startup())

    assert job.stage is Stage.MERGE
    assert job.status is JobStatus.RUNNING
    store.save.assert_not_called()
    store.release_schema_lock.assert_not_called()
    unblock_ready_dependents.assert_not_awaited()
    identity_check.assert_not_awaited()


def test_startup_reconcile_requires_merged_identity_confirmation(tmp_path):
    job = _job(branch="job/7", stage=Stage.MERGE, project_id=23)
    store = _startup_reconcile_store(job)
    rn = _make_runner(tmp_path, store)
    pull_request = PullRequest(
        state=PullRequestState.MERGED,
        url="https://github.com/acme/repo/pull/17",
        number=17,
        base_ref_name="main",
        head_ref_name="job/7",
        head_ref_oid="a" * 40,
    )

    with (
        patch.object(rn, "_managed_repo", new_callable=AsyncMock, return_value="/managed"),
        patch(
            "hyqs.pipeline.runner.gitops.default_branch",
            new_callable=AsyncMock,
            return_value="main",
        ),
        patch(
            "hyqs.pipeline.runner.github.inspect_branch_pr",
            new_callable=AsyncMock,
            return_value=pull_request,
        ),
        patch(
            "hyqs.pipeline.runner.github.pr_identity_is_merged",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        asyncio.run(rn._reconcile_startup())

    assert job.stage is Stage.MERGE
    assert job.status is JobStatus.RUNNING
    store.save.assert_not_called()
    store.release_schema_lock.assert_not_called()


# ---------------------------------------------------------------------------
# _pause routes exclusively through the atomic store contract (mocked store)
# ---------------------------------------------------------------------------


def test_pause_calls_record_provider_failover_with_job_identity(tmp_path):
    rn = _make_runner(tmp_path)
    rn.store.record_provider_failover = AsyncMock(
        return_value=_transition(alternate_available=True)
    )
    job = _job()
    exc = ProviderUnavailable("claude", resets_at=int(time.time()) + 120)

    asyncio.run(rn._pause(job, exc))

    rn.store.record_provider_failover.assert_awaited_once()
    args = rn.store.record_provider_failover.await_args.args
    kwargs = rn.store.record_provider_failover.await_args.kwargs
    assert args[0] == job.id
    assert args[1] == "worker-a"
    assert args[2] is Stage.PLAN
    assert args[3] == "claude"
    assert args[4] == pause_until(exc.resets_at, rn.limit_backoff)
    assert kwargs["project_id"] == job.project_id
    assert kwargs["failed_step"] == "build"  # PLAN's next step, per _exec_step
    # The store call is the single atomic write — no separate pause/save.
    rn.store.set_provider_pause.assert_not_called()
    rn.store.save.assert_not_called()


def test_pause_never_touches_functional_retry_budgets(tmp_path):
    rn = _make_runner(tmp_path)
    rn.store.record_provider_failover = AsyncMock(
        return_value=_transition(alternate_available=False)
    )
    job = _job(attempts=2, rebase_attempts=1, timeout_attempts=1, plan_reask_attempts=1)
    exc = ProviderUnavailable("claude", resets_at=None)

    asyncio.run(rn._pause(job, exc))

    assert job.attempts == 2
    assert job.rebase_attempts == 1
    assert job.timeout_attempts == 1
    assert job.plan_reask_attempts == 1


def test_pause_codex_to_claude_direction_is_provider_neutral(tmp_path):
    """No hard-coded Claude/Codex branching: any exc.provider is forwarded verbatim."""
    rn = _make_runner(tmp_path)
    rn.store.record_provider_failover = AsyncMock(
        return_value=_transition(source_provider="codex", alternate_available=True)
    )
    job = _job(provider="codex")
    exc = ProviderUnavailable("codex", resets_at=int(time.time()) + 60)

    asyncio.run(rn._pause(job, exc))

    args = rn.store.record_provider_failover.await_args.args
    assert args[3] == "codex"


def test_pause_notifies_immediate_failover_when_alternate_available(tmp_path):
    rn = _make_runner(tmp_path)
    rn.store.record_provider_failover = AsyncMock(
        return_value=_transition(alternate_available=True)
    )
    job = _job()
    exc = ProviderUnavailable("claude", resets_at=int(time.time()) + 120)

    asyncio.run(rn._pause(job, exc))

    rn.notify.assert_awaited_once()
    text = rn.notify.await_args.args[1]
    assert "alternate provider is available" in text
    assert "resuming immediately" in text
    assert "then resuming where I left off" not in text


def test_pause_notifies_waiting_when_no_alternate_available(tmp_path):
    rn = _make_runner(tmp_path)
    rn.store.record_provider_failover = AsyncMock(
        return_value=_transition(alternate_available=False)
    )
    job = _job()
    exc = ProviderUnavailable("claude", resets_at=int(time.time()) + 120)

    asyncio.run(rn._pause(job, exc))

    rn.notify.assert_awaited_once()
    text = rn.notify.await_args.args[1]
    assert "alternate provider is available" not in text
    assert "resuming immediately" not in text
    assert "then resuming where I left off" in text


def test_pause_lost_ownership_is_a_no_op_and_does_not_notify(tmp_path):
    rn = _make_runner(tmp_path)
    rn.store.record_provider_failover = AsyncMock(return_value=_transition(applied=False))
    job = _job()
    exc = ProviderUnavailable("claude", resets_at=int(time.time()) + 120)

    asyncio.run(rn._pause(job, exc))

    rn.notify.assert_not_called()


# ---------------------------------------------------------------------------
# _loop: ProviderUnavailable is handled exclusively by _pause — it never
# falls into the generic failure or FIX-retry paths (job #2870, S2).
# ---------------------------------------------------------------------------


def test_loop_routes_provider_unavailable_to_pause_only(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(status=JobStatus.RUNNING)
    rn._pause = AsyncMock()
    rn._fail = AsyncMock()
    rn._retry_or_fail = AsyncMock()
    exc = ProviderUnavailable("codex", resets_at=None)

    async def _claim(*_args, **_kwargs):
        rn._drain.set()
        return job

    rn.store.claim = AsyncMock(side_effect=_claim)

    async def _run():
        with patch.object(rn, "_run_stage_with_retry", new_callable=AsyncMock, side_effect=exc):
            await rn._loop("w1")

    asyncio.run(_run())

    rn._pause.assert_awaited_once_with(job, exc)
    rn._fail.assert_not_called()
    rn._retry_or_fail.assert_not_called()
    rn.store.save.assert_not_called()


# ---------------------------------------------------------------------------
# Real-store integration: routing state (roster capability + pause gate)
# actually lives in Postgres, so exercise _pause against it directly.
# ---------------------------------------------------------------------------


def _real_runner(store, tmp_path) -> PipelineRunner:
    config = SimpleNamespace(model="test-model", data_dir=str(tmp_path / "data"))
    return PipelineRunner(store, config, notify=AsyncMock())


def _default_agent_id(store, project_id: int) -> int:
    return store.list_agents(project_id)[0].id  # the seeded 'Claude (default)' agent


@pytest.fixture
def clean_provider_pauses(store):
    """Reset claude/codex pauses before AND after: these tests share one
    physical Postgres 'meta' table (no per-test isolation for it), so a pause
    one test sets would otherwise leak into every later test in this run."""
    store.set_provider_pause("claude", 0.0)
    store.set_provider_pause("codex", 0.0)
    yield
    store.set_provider_pause("claude", 0.0)
    store.set_provider_pause("codex", 0.0)


def _cleanup(store, repo_path: str) -> None:
    with store._pool.connection() as conn:
        conn.execute(
            "DELETE FROM job_dependencies WHERE job_id IN "
            "(SELECT id FROM jobs WHERE repo_path=%s) OR depends_on_job_id IN "
            "(SELECT id FROM jobs WHERE repo_path=%s)",
            (repo_path, repo_path),
        )
        conn.execute("DELETE FROM jobs WHERE repo_path=%s", (repo_path,))
        row = conn.execute("SELECT id FROM projects WHERE repo_path=%s", (repo_path,)).fetchone()
        if row:
            conn.execute("DELETE FROM project_agents WHERE project_id=%s", (row["id"],))
            conn.execute("DELETE FROM projects WHERE id=%s", (row["id"],))


def test_pause_real_store_claude_to_codex_notifies_immediate_failover(
    store, tmp_path, clean_provider_pauses
):
    job = None
    try:
        job = store.create(idea="failover me", repo_path=f"/tmp/test-pr-{uuid.uuid4()}", chat_id=1)
        claude_agent_id = _default_agent_id(store, job.project_id)
        store.create_agent(job.project_id, "Codex", "codex", "gpt-5")
        job.stage = Stage.PLAN
        job.status = JobStatus.RUNNING
        job.owner = "worker-a"
        job.provider = "claude"
        job.agent_id = claude_agent_id
        job.lease_until = time.time() + 90
        store.save(job)

        rn = _real_runner(store, tmp_path)
        exc = ProviderUnavailable("claude", resets_at=int(time.time()) + 120)
        asyncio.run(rn._pause(job, exc))

        rn.notify.assert_awaited_once()
        assert "resuming immediately" in rn.notify.await_args.args[1]

        reloaded = store.get(job.id)
        assert reloaded.status == JobStatus.PENDING
        assert reloaded.stage == Stage.PLAN
        assert reloaded.owner == ""
        assert reloaded.agent_id is None
        assert reloaded.provider == ""
        assert reloaded.failure_code == "provider_unavailable"
        assert reloaded.failure_origin == "provider"
        assert reloaded.retry_disposition == "same_step"
        assert store.paused_providers(time.time()) == {"claude"}
    finally:
        if job is not None:
            _cleanup(store, job.repo_path)


def test_pause_real_store_no_alternate_notifies_waiting(store, tmp_path, clean_provider_pauses):
    job = None
    try:
        job = store.create(idea="failover me", repo_path=f"/tmp/test-pr-{uuid.uuid4()}", chat_id=1)
        claude_agent_id = _default_agent_id(store, job.project_id)
        job.stage = Stage.PLAN
        job.status = JobStatus.RUNNING
        job.owner = "worker-a"
        job.provider = "claude"
        job.agent_id = claude_agent_id
        job.lease_until = time.time() + 90
        store.save(job)

        rn = _real_runner(store, tmp_path)
        exc = ProviderUnavailable("claude", resets_at=int(time.time()) + 120)
        asyncio.run(rn._pause(job, exc))

        rn.notify.assert_awaited_once()
        text = rn.notify.await_args.args[1]
        assert "resuming immediately" not in text
        assert "then resuming where I left off" in text

        reloaded = store.get(job.id)
        assert reloaded.status == JobStatus.PENDING
    finally:
        if job is not None:
            _cleanup(store, job.repo_path)


def test_pause_real_store_both_providers_paused_notifies_waiting(
    store, tmp_path, clean_provider_pauses
):
    job = None
    try:
        job = store.create(idea="failover me", repo_path=f"/tmp/test-pr-{uuid.uuid4()}", chat_id=1)
        claude_agent_id = _default_agent_id(store, job.project_id)
        store.create_agent(job.project_id, "Codex", "codex", "gpt-5")
        store.set_provider_pause("codex", time.time() + 3600)
        job.stage = Stage.PLAN
        job.status = JobStatus.RUNNING
        job.owner = "worker-a"
        job.provider = "claude"
        job.agent_id = claude_agent_id
        job.lease_until = time.time() + 90
        store.save(job)

        rn = _real_runner(store, tmp_path)
        exc = ProviderUnavailable("claude", resets_at=int(time.time()) + 120)
        asyncio.run(rn._pause(job, exc))

        text = rn.notify.await_args.args[1]
        assert "then resuming where I left off" in text
    finally:
        if job is not None:
            _cleanup(store, job.repo_path)


def test_pause_real_store_incompatible_alternate_task_notifies_waiting(
    store, tmp_path, clean_provider_pauses
):
    job = None
    try:
        job = store.create(idea="failover me", repo_path=f"/tmp/test-pr-{uuid.uuid4()}", chat_id=1)
        claude_agent_id = _default_agent_id(store, job.project_id)
        # PLAN's next task is BUILD; Codex here can only review.
        store.create_agent(job.project_id, "Codex", "codex", "gpt-5", allowed_tasks=["review"])
        job.stage = Stage.PLAN
        job.status = JobStatus.RUNNING
        job.owner = "worker-a"
        job.provider = "claude"
        job.agent_id = claude_agent_id
        job.lease_until = time.time() + 90
        store.save(job)

        rn = _real_runner(store, tmp_path)
        exc = ProviderUnavailable("claude", resets_at=int(time.time()) + 120)
        asyncio.run(rn._pause(job, exc))

        text = rn.notify.await_args.args[1]
        assert "then resuming where I left off" in text
    finally:
        if job is not None:
            _cleanup(store, job.repo_path)


def test_pause_real_store_codex_to_claude_direction(store, tmp_path, clean_provider_pauses):
    job = None
    try:
        job = store.create(idea="failover me", repo_path=f"/tmp/test-pr-{uuid.uuid4()}", chat_id=1)
        codex_agent = store.create_agent(job.project_id, "Codex", "codex", "gpt-5")
        job.stage = Stage.PLAN
        job.status = JobStatus.RUNNING
        job.owner = "worker-a"
        job.provider = "codex"
        job.agent_id = codex_agent.id
        job.lease_until = time.time() + 90
        store.save(job)

        rn = _real_runner(store, tmp_path)
        exc = ProviderUnavailable("codex", resets_at=int(time.time()) + 120)
        asyncio.run(rn._pause(job, exc))

        assert "resuming immediately" in rn.notify.await_args.args[1]
        assert store.paused_providers(time.time()) == {"codex"}
        reloaded = store.get(job.id)
        assert reloaded.status == JobStatus.PENDING
        assert reloaded.provider == ""
    finally:
        if job is not None:
            _cleanup(store, job.repo_path)


# ---------------------------------------------------------------------------
# _retry_or_fail: consecutive-failure signature comparison (job #2972).
#
# A job that keeps failing the *same way* should keep spending its normal
# retry budget. A job whose two most recent consecutive failures have
# genuinely different structured signatures is escalated to a human early —
# before the attempt cap is exhausted — since blind retrying was never going
# to converge. A job's very first failure has no predecessor to diverge
# from, so it always retries normally.
# ---------------------------------------------------------------------------


def test_retry_or_fail_same_signature_continues_retrying_within_budget(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(
        stage=Stage.BUILD,
        status=JobStatus.RUNNING,
        project_id=None,
        attempts=1,
        failed_step="test",
        failure_code="test_failed",
        failure_detail={"check_id": "test.execution", "event_id": "test.execution", "attempt": 1},
    )

    asyncio.run(
        rn._retry_or_fail(
            job,
            "tests failed again",
            failure_detail={
                "check_id": "test.execution",
                "event_id": "test.execution",
                "command": "pytest",
                "output": "a totally different traceback this time",
            },
        )
    )

    assert job.status == JobStatus.PENDING
    assert job.stage == Stage.FIX
    assert job.attempts == 2
    assert job.retry_disposition == "fix_worktree"
    rn.store.save.assert_called_once_with(job)
    rn.notify.assert_awaited_once()


def test_retry_or_fail_diverging_signature_continues_within_budget(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(
        stage=Stage.BUILD,
        status=JobStatus.RUNNING,
        project_id=None,
        attempts=1,
        failed_step="test",
        failure_code="test_failed",
        failure_detail={"check_id": "test.execution", "event_id": "test.execution", "attempt": 1},
    )

    asyncio.run(
        rn._retry_or_fail(
            job,
            "a completely different check failed this time",
            failure_detail={
                "check_id": "test.invariants",
                "event_id": "test.invariants",
                "command": "invariants",
                "output": "unrelated failure",
            },
        )
    )

    assert job.attempts == 2
    assert job.status == JobStatus.PENDING
    assert job.stage == Stage.FIX
    assert job.retry_disposition == "fix_worktree"
    assert job.failure_code == "test_failed"
    assert job.failure_detail["attempt"] == 2


def test_retry_or_fail_first_failure_never_escalates_early(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(
        stage=Stage.BUILD,
        status=JobStatus.RUNNING,
        project_id=None,
        attempts=0,
        failed_step=None,
        failure_code=None,
        failure_detail=None,
    )

    asyncio.run(
        rn._retry_or_fail(
            job,
            "tests failed",
            failure_detail={"check_id": "test.execution", "event_id": "test.execution"},
        )
    )

    assert job.status == JobStatus.PENDING
    assert job.stage == Stage.FIX
    assert job.attempts == 1
    assert job.retry_disposition == "fix_worktree"


# ---------------------------------------------------------------------------
# _retry_or_fail: worktree-missing short-circuit (jobs #3970/#3981).
#
# testing.run_tests marks a vanished/empty worktree with a "[worktree-missing]"
# summary (an infrastructure fault, not untested code). _retry_or_fail must
# short-circuit that signal straight to a typed transient failure instead of
# spending a FIX-attempt — resuming into FIX with no worktree can't converge.
# Every other test failure must keep going through the ordinary FIX-loop path.
# ---------------------------------------------------------------------------


def test_retry_or_fail_worktree_missing_short_circuits_without_spending_attempt(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(
        stage=Stage.BUILD,
        status=JobStatus.RUNNING,
        project_id=None,
        attempts=0,
        failed_step=None,
        failure_code=None,
        failure_detail=None,
    )

    asyncio.run(
        rn._retry_or_fail(
            job,
            "tests failed ((none)):\n[worktree-missing] worktree is missing or empty "
            "— cannot run tests (infrastructure fault, not an untested-code gap).",
        )
    )

    assert job.status == JobStatus.FAILED
    assert job.failure_code == "worktree_missing"
    assert job.failure_origin == "infrastructure"
    assert job.retry_disposition == "same_step"
    assert job.attempts == 0  # bypassed the FIX-attempt loop entirely
    rn.store.save.assert_called_once_with(job)


def test_retry_or_fail_genuine_test_failure_still_routes_through_fix_loop(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(
        stage=Stage.BUILD,
        status=JobStatus.RUNNING,
        project_id=None,
        attempts=0,
        failed_step=None,
        failure_code=None,
        failure_detail=None,
    )

    asyncio.run(
        rn._retry_or_fail(job, "tests failed (pytest):\nAssertionError: expected 200, got 500")
    )

    assert job.status == JobStatus.PENDING
    assert job.stage == Stage.FIX
    assert job.attempts == 1
    assert job.failure_code == "test_failed"
    assert job.retry_disposition == "fix_worktree"


def test_retry_or_fail_forged_marker_in_real_test_output_does_not_bypass_fix_loop(tmp_path):
    """A candidate test that prints "[worktree-missing]" must not forge the
    transient short-circuit — the marker only short-circuits when it appears in
    the exact "tests failed ((none)):\\n[worktree-missing]" prefix that only our
    own worktree-missing code path can produce (job #3981 security finding)."""
    rn = _make_runner(tmp_path)
    job = _job(
        stage=Stage.BUILD,
        status=JobStatus.RUNNING,
        project_id=None,
        attempts=0,
        failed_step=None,
        failure_code=None,
        failure_detail=None,
    )

    asyncio.run(
        rn._retry_or_fail(
            job,
            "tests failed (pytest tests/test_evil.py):\n"
            "AssertionError: [worktree-missing] worktree is missing or empty "
            "— cannot run tests (infrastructure fault, not an untested-code gap).",
        )
    )

    assert job.status == JobStatus.PENDING
    assert job.stage == Stage.FIX
    assert job.attempts == 1
    assert job.failure_code == "test_failed"
    assert job.retry_disposition == "fix_worktree"


# ---------------------------------------------------------------------------
# _retry_or_fail: REVIEW/SECURITY gate-oscillation detection (job #3969).
#
# A job whose REVIEW and SECURITY gates alternate on identical findings is
# never going to converge by retrying — the two gates are enforcing
# mutually exclusive demands. That pattern halts the job for a human ruling
# instead of burning the fix-attempt budget. Genuinely diverging findings,
# even across the same two alternating gates, are real progress and must
# keep retrying normally.
# ---------------------------------------------------------------------------

_REVIEW_A = {
    "summary": "must add strict input validation",
    "findings": [{"severity": "high", "note": "tighten the regex"}],
}
_SECURITY_B = {
    "summary": "must relax input validation",
    "findings": [{"severity": "high", "note": "loosen the regex"}],
}


def test_retry_or_fail_gate_oscillation_halts_on_fourth_call(tmp_path):
    rn = _make_runner(tmp_path)
    rn.max_attempts = 10
    job = _job(
        stage=Stage.TEST,
        status=JobStatus.RUNNING,
        project_id=None,
        attempts=0,
        failed_step=None,
        failure_code=None,
        failure_detail=None,
        review=_REVIEW_A,
        security_review=_SECURITY_B,
    )

    for step in ("review", "security", "review"):
        job.executing_step = step
        job.status = JobStatus.RUNNING
        asyncio.run(rn._retry_or_fail(job, f"{step} gate failed"))
        assert job.status == JobStatus.PENDING
        assert job.stage == Stage.FIX
        assert job.retry_disposition == "fix_worktree"

    assert job.attempts == 3
    attempts_before_fourth_call = job.attempts

    job.executing_step = "security"
    job.status = JobStatus.RUNNING
    asyncio.run(rn._retry_or_fail(job, "security gate failed"))

    assert job.status == JobStatus.FAILED
    assert job.failure_code == "gate_conflict"
    assert job.retry_disposition == "human_review"
    assert job.failure_origin == "ai_gate"
    assert job.attempts == attempts_before_fourth_call
    assert "review" in job.error
    assert "security" in job.error
    assert _REVIEW_A["summary"] in job.error
    assert _SECURITY_B["summary"] in job.error


def test_retry_or_fail_diverging_gate_findings_never_conflict(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(
        stage=Stage.BUILD,
        status=JobStatus.RUNNING,
        project_id=None,
        attempts=0,
        failed_step=None,
        failure_code=None,
        failure_detail=None,
    )
    verdicts = [
        {"summary": f"finding round {i}", "findings": [{"severity": "high", "note": f"issue {i}"}]}
        for i in range(4)
    ]

    for i, step in enumerate(("review", "security", "review")):
        job.executing_step = step
        job.status = JobStatus.RUNNING
        if step == "review":
            job.review = verdicts[i]
        else:
            job.security_review = verdicts[i]
        asyncio.run(rn._retry_or_fail(job, f"{step} gate failed"))
        assert job.status == JobStatus.PENDING
        assert job.stage == Stage.FIX
        assert job.failure_code != "gate_conflict"
        assert job.attempts == i + 1

    job.executing_step = "security"
    job.status = JobStatus.RUNNING
    job.security_review = verdicts[3]
    asyncio.run(rn._retry_or_fail(job, "security gate failed"))

    assert job.failure_code != "gate_conflict"
    assert job.status == JobStatus.FAILED
    assert job.attempts == rn.max_attempts


def test_retry_or_fail_test_failure_never_gets_gate_history(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(
        stage=Stage.BUILD,
        status=JobStatus.RUNNING,
        project_id=None,
        attempts=1,
        failed_step="test",
        failure_code="test_failed",
        failure_detail={"check_id": "test.execution", "event_id": "test.execution", "attempt": 1},
    )

    asyncio.run(
        rn._retry_or_fail(
            job,
            "tests failed again",
            failure_detail={"check_id": "test.execution", "event_id": "test.execution"},
        )
    )

    assert job.status == JobStatus.PENDING
    assert "gate_history" not in job.failure_detail


def test_pause_real_store_lost_ownership_race_is_a_no_op(store, tmp_path, clean_provider_pauses):
    job = None
    try:
        job = store.create(idea="failover me", repo_path=f"/tmp/test-pr-{uuid.uuid4()}", chat_id=1)
        claude_agent_id = _default_agent_id(store, job.project_id)
        job.stage = Stage.PLAN
        job.status = JobStatus.RUNNING
        job.owner = "worker-a"
        job.provider = "claude"
        job.agent_id = claude_agent_id
        job.lease_until = time.time() + 90
        store.save(job)

        # A newer worker already reclaimed this job's lease and re-claimed it
        # under a different owner — our in-memory `job` (still "worker-a") is
        # now stale.
        with store._pool.connection() as conn:
            conn.execute("UPDATE jobs SET owner=%s WHERE id=%s", ("worker-z", job.id))

        rn = _real_runner(store, tmp_path)
        exc = ProviderUnavailable("claude", resets_at=int(time.time()) + 120)
        asyncio.run(rn._pause(job, exc))

        rn.notify.assert_not_called()
        reloaded = store.get(job.id)
        assert reloaded.status == JobStatus.RUNNING
        assert reloaded.owner == "worker-z"
        assert store.paused_providers(time.time()) == set()
    finally:
        if job is not None:
            _cleanup(store, job.repo_path)
