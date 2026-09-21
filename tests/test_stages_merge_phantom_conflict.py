"""Exact-SHA regression coverage for phantom-conflict PR recovery."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyqs.pipeline.github import PullRequest, PullRequestState
from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import Job, JobStatus, Stage
from hyqs.pipeline.stages.merge import run as merge_run

_NOT_MERGEABLE = GitResult(
    ok=False, stdout="", stderr="GH006: Protected branch: PR is not mergeable", code=1
)
_BASE_MODIFIED = GitResult(
    ok=False,
    stdout="",
    stderr="GraphQL: Base branch was modified. Review and try the merge again. (mergePullRequest)",
    code=1,
)
_OK = GitResult(ok=True, stdout="", stderr="", code=0)
_FAILED = GitResult(ok=False, stdout="", stderr="operation failed", code=1)
_GITHUB_OUTAGE = GitResult(
    ok=False,
    stdout="",
    stderr=(
        'non-200 OK status code: 503 Service Unavailable body: "{\\"message\\": '
        '\\"No server is currently available to service your request. Sorry about '
        'that. Please try resubmitting your request..."'
    ),
    code=1,
)
_HEAD_OID = "d" * 40
_STALE_OID = "c" * 40


class _FakeStore:
    def __init__(self):
        self.merge_locks: dict[str, str] = {}
        self.saved: list[Job] = []

    def try_acquire_merge_lock(self, repo_path, owner, now, ttl):
        current = self.merge_locks.get(repo_path)
        if current is None or current == owner:
            self.merge_locks[repo_path] = owner
            return True
        return False

    def release_merge_lock(self, repo_path, owner):
        if self.merge_locks.get(repo_path) == owner:
            del self.merge_locks[repo_path]

    def save(self, job):
        self.saved.append(job)


def _make_rn(*, max_attempts: int = 3) -> SimpleNamespace:
    rn = SimpleNamespace()
    rn.store = _FakeStore()
    rn.worktrees = Path("/fake/worktrees")
    rn.merge_lock_ttl = 300.0
    rn.merge_inlock_steps = 1
    rn.rebase_max_attempts = 5
    rn.phantom_conflict_max_attempts = max_attempts
    rn.phantom_conflict_backoff = 0
    rn._managed_repo = AsyncMock(return_value="/fake/managed")
    rn._event = MagicMock()
    rn._fail = AsyncMock()
    rn._rebase_conflict = AsyncMock()
    rn.notify = AsyncMock()
    rn._record_resource = MagicMock()
    rn.config = MagicMock()
    return rn


def _job(*, id: int = 1) -> Job:
    return Job(
        id=id,
        idea="test idea",
        repo_path="/repo/proj",
        chat_id=1,
        stage=Stage.SECURITY,
        status=JobStatus.RUNNING,
        branch=f"hyqs/job-{id}",
        project_id=None,
        owner="worker-a",
    )


def _pull_request(
    job: Job,
    *,
    state: PullRequestState = PullRequestState.OPEN,
    oid: str = _HEAD_OID,
    base: str = "main",
    head: str | None = None,
    number: int = 3374,
) -> PullRequest:
    return PullRequest(
        state=state,
        url=f"https://github.com/example/repo/pull/{number}",
        number=number,
        base_ref_name=base,
        head_ref_name=head or job.branch,
        head_ref_oid=oid,
    )


@contextmanager
def _merge_patches(
    job: Job,
    *,
    inspections: list[PullRequest],
    merge_results: list[GitResult] | None = None,
    merge_res: GitResult = _OK,
    unmerged: bool = False,
    reopen_result: GitResult = _OK,
    create_result: GitResult = _OK,
    force_push_result: GitResult = _OK,
):
    """Isolate HEAD identity from the finite generic-git plumbing sequence."""
    with ExitStack() as stack:

        def mocked(name, **kwargs):
            return stack.enter_context(patch(name, new_callable=AsyncMock, **kwargs))

        mocked("hyqs.pipeline.stages.merge.gitops.default_branch", return_value="main")
        mocked("hyqs.pipeline.stages.merge.github.has_remote", return_value=True)
        generic_git = mocked("hyqs.pipeline.stages.merge.gitops.git", return_value=_OK)
        mocked("hyqs.pipeline.stages.merge.gitops.base_has_advanced", return_value=False)
        local_head = mocked(
            "hyqs.pipeline.stages.merge.gitops.local_head_oid", return_value=_HEAD_OID
        )
        inspect = mocked(
            "hyqs.pipeline.stages.merge.github.inspect_branch_pr",
            side_effect=list(inspections),
        )
        reopen = mocked("hyqs.pipeline.stages.merge.github.reopen_pr", return_value=reopen_result)
        create = mocked("hyqs.pipeline.stages.merge.github.pr_create", return_value=create_result)
        mocked("hyqs.pipeline.stages.merge.gitops.remove_worktree", return_value=None)
        merged = mocked(
            "hyqs.pipeline.stages.merge.github.pr_identity_is_merged", return_value=False
        )
        merge = mocked(
            "hyqs.pipeline.stages.merge.github.merge_pr_squash",
            side_effect=list(merge_results or [_OK]),
        )
        mocked(
            "hyqs.pipeline.stages.merge.gitops.merge_base_into_worktree",
            return_value=merge_res,
        )
        stack.enter_context(
            patch("hyqs.pipeline.stages.merge.gitops._is_merge_plumbing_error", return_value=False)
        )
        mocked("hyqs.pipeline.stages.merge.gitops.has_unmerged_paths", return_value=unmerged)
        mocked(
            "hyqs.pipeline.stages.merge.gitops.head_info",
            return_value={"hash": "deadbeef", "subject": "x"},
        )
        force_push = mocked(
            "hyqs.pipeline.stages.merge.github.force_push_branch",
            return_value=force_push_result,
        )
        mocked("hyqs.pipeline.stages.merge.github.sync_base", return_value=_OK)
        stack.enter_context(
            patch("hyqs.pipeline.stages.merge.decisions.is_janitorial_job", return_value=True)
        )
        mocked(
            "hyqs.pipeline.stages.merge.impl_summary.generate_implementation_summary",
            return_value="a summary",
        )
        stack.enter_context(
            patch("hyqs.pipeline.stages.merge.build_backend", return_value=MagicMock())
        )
        mocked("hyqs.pipeline.stages.merge.asyncio.sleep")
        yield SimpleNamespace(
            git=generic_git,
            local_head=local_head,
            inspect=inspect,
            reopen=reopen,
            create=create,
            merged=merged,
            merge=merge,
            force_push=force_push,
        )


def _failed_event_detail(rn) -> dict:
    calls = [call for call in rn._event.call_args_list if call.args[2] == "failed"]
    assert calls
    return calls[-1].kwargs["detail"]


def test_closed_exact_sha_pr_reopens_and_merges_verified_identity():
    rn = _make_rn()
    job = _job(id=3374)
    initial = _pull_request(job)
    closed = _pull_request(job, state=PullRequestState.CLOSED)
    reopened = _pull_request(job)

    with _merge_patches(
        job,
        inspections=[initial, closed, reopened],
        merge_results=[_NOT_MERGEABLE, _OK],
    ) as mocks:
        asyncio.run(merge_run(rn, job))

    assert len(_HEAD_OID) == 40 and _HEAD_OID == _HEAD_OID.lower()
    assert job.stage == Stage.DEPLOY
    assert job.rebase_attempts == 0
    mocks.local_head.assert_awaited()
    mocks.reopen.assert_awaited_once_with("/fake/managed", closed)
    assert mocks.merge.await_args_list[-1].args[1] == reopened
    assert mocks.git.await_count == 6


def test_unknown_pr_creates_exact_sha_replacement():
    rn = _make_rn(max_attempts=1)
    job = _job()
    unknown = _pull_request(job, state=PullRequestState.UNKNOWN)
    replacement = _pull_request(job, number=3375)

    with _merge_patches(job, inspections=[unknown, unknown, replacement]) as mocks:
        asyncio.run(merge_run(rn, job))

    assert job.stage == Stage.DEPLOY
    assert job.rebase_attempts == 0
    mocks.create.assert_awaited_once()
    mocks.merge.assert_awaited_once_with("/fake/managed", replacement)


def test_failed_reopen_creates_and_merges_verified_replacement():
    rn = _make_rn(max_attempts=1)
    job = _job()
    closed = _pull_request(job, state=PullRequestState.CLOSED)
    replacement = _pull_request(job, number=3376)

    with _merge_patches(
        job,
        inspections=[closed, closed, replacement],
        reopen_result=_FAILED,
    ) as mocks:
        asyncio.run(merge_run(rn, job))

    mocks.reopen.assert_awaited_once_with("/fake/managed", closed)
    mocks.create.assert_awaited_once()
    mocks.merge.assert_awaited_once_with("/fake/managed", replacement)
    assert job.stage == Stage.DEPLOY
    assert job.rebase_attempts == 0


@pytest.mark.parametrize(
    "mismatch",
    [
        {"oid": _STALE_OID},
        {"base": "release"},
        {"head": "hyqs/job-other"},
    ],
)
def test_stale_or_mismatched_pr_identity_never_merges(mismatch):
    rn = _make_rn(max_attempts=2)
    job = _job()
    wrong = _pull_request(job, **mismatch)

    with _merge_patches(job, inspections=[wrong] * 5) as mocks:
        asyncio.run(merge_run(rn, job))

    rn._fail.assert_awaited_once()
    mocks.merge.assert_not_awaited()
    assert mocks.force_push.await_count == 2
    assert mocks.inspect.await_count == 4
    assert job.stage == Stage.SECURITY


@pytest.mark.parametrize(
    "malformed",
    [
        {"url": "https://example.com/example/repo/pull/3374"},
        {"url": "https://github.com/example/repo/pull/9999"},
        {"number": None},
    ],
)
def test_malformed_pr_identity_never_merges(malformed):
    rn = _make_rn(max_attempts=1)
    job = _job()
    values = _pull_request(job).__dict__ | malformed
    invalid = PullRequest(**values)

    with _merge_patches(job, inspections=[invalid, invalid, invalid]) as mocks:
        asyncio.run(merge_run(rn, job))

    rn._fail.assert_awaited_once()
    mocks.merge.assert_not_awaited()
    mocks.create.assert_awaited_once()
    assert job.rebase_attempts == 0


@pytest.mark.parametrize(
    ("reopen_result", "create_result", "force_result"),
    [(_FAILED, _FAILED, _OK), (_FAILED, _OK, _OK), (_OK, _OK, _FAILED)],
)
def test_recovery_operations_fail_within_attempt_bound(reopen_result, create_result, force_result):
    rn = _make_rn(max_attempts=1)
    job = _job()
    closed = _pull_request(job, state=PullRequestState.CLOSED)
    observations = [closed, closed, closed]

    with _merge_patches(
        job,
        inspections=observations,
        reopen_result=reopen_result,
        create_result=create_result,
        force_push_result=force_result,
    ) as mocks:
        asyncio.run(merge_run(rn, job))

    rn._fail.assert_awaited_once()
    mocks.merge.assert_not_awaited()
    assert mocks.force_push.await_count == 1
    assert job.rebase_attempts == 0


def test_known_closed_pr_is_not_blindly_merged_repeatedly():
    rn = _make_rn(max_attempts=3)
    job = _job()
    closed = _pull_request(job, state=PullRequestState.CLOSED)

    with _merge_patches(
        job,
        inspections=[closed] * 7,
        reopen_result=_FAILED,
        create_result=_FAILED,
    ) as mocks:
        asyncio.run(merge_run(rn, job))

    mocks.merge.assert_not_awaited()
    assert mocks.reopen.await_count == 3
    assert mocks.create.await_count == 1
    assert mocks.force_push.await_count == 3
    rn._fail.assert_awaited_once()


def test_recovery_evidence_records_verified_transitions():
    rn = _make_rn(max_attempts=1)
    job = _job()
    initial = _pull_request(job)
    closed = _pull_request(job, state=PullRequestState.CLOSED)
    reopened = _pull_request(job)

    with _merge_patches(
        job,
        inspections=[initial, closed, reopened],
        merge_results=[_NOT_MERGEABLE, _FAILED],
    ):
        asyncio.run(merge_run(rn, job))

    evidence = _failed_event_detail(rn)["github_evidence"]
    actions = [entry["action"] for entry in evidence]
    assert actions == [
        "initial_inspection",
        "merge",
        "force_push",
        "observe",
        "reopen",
        "inspect_after_reopen",
        "recovery_merge",
    ]
    assert evidence[-1]["url"] == reopened.url
    assert evidence[-1]["number"] == reopened.number


def test_genuine_conflict_is_never_force_pushed():
    rn = _make_rn()
    job = _job()
    identity = _pull_request(job)

    with _merge_patches(
        job,
        inspections=[identity],
        merge_results=[_NOT_MERGEABLE],
        merge_res=GitResult(ok=False, stdout="", stderr="conflict", code=1),
        unmerged=True,
    ) as mocks:
        asyncio.run(merge_run(rn, job))

    mocks.force_push.assert_not_awaited()
    rn._rebase_conflict.assert_awaited_once()
    assert job.rebase_attempts == 0


def test_base_branch_modified_recovers_via_phantom_conflict_path():
    # GitHub's "Base branch was modified" GraphQL rejection (a merge race, not a
    # real conflict) must enter the same worktree-recreate/phantom-conflict
    # recovery flow as a generic "not mergeable" response, not fail outright.
    rn = _make_rn(max_attempts=1)
    job = _job()
    identity = _pull_request(job)

    with _merge_patches(
        job,
        inspections=[identity, identity],
        merge_results=[_BASE_MODIFIED, _OK],
    ) as mocks:
        asyncio.run(merge_run(rn, job))

    rn._fail.assert_not_awaited()
    assert mocks.merge.await_count == 2
    assert job.stage == Stage.DEPLOY
    assert job.rebase_attempts == 0


def test_genuine_github_outage_fails_with_structured_evidence():
    # A real GitHub API outage (not a conflict/race) still fails the job, but
    # must carry structured github_evidence on failure_detail so the janitor's
    # classifier can recognize it as GitHub-API-transient rather than parsing
    # prose.
    rn = _make_rn(max_attempts=1)
    job = _job()
    identity = _pull_request(job)

    with _merge_patches(
        job,
        inspections=[identity],
        merge_results=[_GITHUB_OUTAGE],
    ):
        asyncio.run(merge_run(rn, job))

    rn._fail.assert_awaited_once()
    failure_detail = rn._fail.await_args.kwargs["failure_detail"]
    evidence = failure_detail["github_evidence"]
    assert any("503 Service Unavailable" in entry.get("stderr", "") for entry in evidence)
