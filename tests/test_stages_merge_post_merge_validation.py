"""Regression tests for the post-merge validation gate wired into the MERGE
stage (job #4077 postmortem): a real base advance absorbed during merge must
be validated (lint/compile + test rerun + same-hunk overlap check) against
the already-merged worktree before the irreversible push/squash-merge call.
A fast-forward-equivalent merge (base never advanced) must skip validation
entirely.

Follows the fake-store/patched-gitops mocking pattern from
tests/test_stages_merge_conflict_autoresolve.py.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline import resources
from hyqs.pipeline.github import PullRequest, PullRequestState
from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.merge_validate import MergeValidationResult
from hyqs.pipeline.models import Job, JobStatus, Stage
from hyqs.pipeline.stages.merge import run as merge_run

_OK = GitResult(ok=True, stdout="", stderr="", code=0)
_HEAD_OID = "d" * 40
_MERGE_BASE_SHA = "c" * 40


def _git_side_effect(_repo, *args, **_kwargs):
    if args and args[0] == "merge-base":
        return GitResult(ok=True, stdout=f"{_MERGE_BASE_SHA}\n", stderr="", code=0)
    return _OK


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


def _make_rn(*, merge_inlock_steps: int = 2) -> SimpleNamespace:
    rn = SimpleNamespace()
    rn.store = _FakeStore()
    rn.worktrees = Path("/fake/worktrees")
    rn.merge_lock_ttl = 300.0
    rn.merge_inlock_steps = merge_inlock_steps
    rn.rebase_max_attempts = 5
    rn.phantom_conflict_max_attempts = 3
    rn.phantom_conflict_backoff = 0
    rn._managed_repo = AsyncMock(return_value="/fake/managed")
    rn._event = MagicMock()
    rn._fail = AsyncMock()
    rn._rebase_conflict = AsyncMock()
    rn._retry_or_fail = AsyncMock()
    rn.notify = AsyncMock()
    rn._record_resource = MagicMock()
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


def _base_patches(stack: ExitStack, *, base_has_advanced_effect) -> dict[str, AsyncMock]:
    """Patch every git/GitHub/AI call merge.run touches, common to all scenarios here."""
    mocks: dict[str, AsyncMock] = {}
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.gitops.default_branch",
            new_callable=AsyncMock,
            return_value="main",
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.github.has_remote",
            new_callable=AsyncMock,
            return_value=True,
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.gitops.git",
            new_callable=AsyncMock,
            side_effect=_git_side_effect,
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.gitops.base_has_advanced",
            new_callable=AsyncMock,
            side_effect=base_has_advanced_effect,
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.gitops.merge_base_into_worktree",
            new_callable=AsyncMock,
            return_value=_OK,
        )
    )
    stack.enter_context(
        patch("hyqs.pipeline.stages.merge.gitops.push", new_callable=AsyncMock, return_value=_OK)
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.gitops.patch",
            new_callable=AsyncMock,
            side_effect=lambda _repo, rng: f"diff for {rng}",
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.gitops.numstat",
            new_callable=AsyncMock,
            return_value=[{"path": "f.py", "added": 1, "deleted": 1}],
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.gitops.head_info",
            new_callable=AsyncMock,
            return_value={"hash": "deadbeef", "subject": "x"},
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.gitops.local_head_oid",
            new_callable=AsyncMock,
            return_value=_HEAD_OID,
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.gitops.remove_worktree",
            new_callable=AsyncMock,
            return_value=None,
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.github.inspect_branch_pr",
            new_callable=AsyncMock,
            side_effect=lambda _repo, branch, **_kwargs: PullRequest(
                state=PullRequestState.OPEN,
                url="https://github.com/example/repo/pull/1",
                number=1,
                base_ref_name="main",
                head_ref_name=branch,
                head_ref_oid=_HEAD_OID,
            ),
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.github.pr_identity_is_merged",
            new_callable=AsyncMock,
            return_value=False,
        )
    )
    mocks["merge_pr_squash"] = stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.github.merge_pr_squash",
            new_callable=AsyncMock,
            return_value=_OK,
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.github.sync_base", new_callable=AsyncMock, return_value=_OK
        )
    )
    stack.enter_context(
        patch("hyqs.pipeline.stages.merge.decisions.is_janitorial_job", return_value=True)
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.impl_summary.generate_implementation_summary",
            new_callable=AsyncMock,
            return_value="a summary",
        )
    )
    stack.enter_context(patch("hyqs.pipeline.stages.merge.build_backend", return_value=MagicMock()))
    mocks["validate_merge_result"] = stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.merge_validate.validate_merge_result",
            new_callable=AsyncMock,
        )
    )
    return mocks


def test_fast_forward_merge_skips_validation_entirely():
    """(a) base never advances this attempt: validate_merge_result is never
    called and merge_pr_squash proceeds normally."""
    rn = _make_rn()
    job = _job()

    with ExitStack() as stack:
        mocks = _base_patches(stack, base_has_advanced_effect=[False])
        asyncio.run(merge_run(rn, job))

    mocks["validate_merge_result"].assert_not_called()
    mocks["merge_pr_squash"].assert_awaited_once()
    rn._rebase_conflict.assert_not_called()
    rn._retry_or_fail.assert_not_called()
    assert job.stage == Stage.DEPLOY


def test_real_merge_with_passing_validation_proceeds_to_squash_merge():
    """(b) base advances and validate_merge_result reports passed=True: the
    merge proceeds to merge_pr_squash exactly as before."""
    rn = _make_rn(merge_inlock_steps=2)
    job = _job()

    with ExitStack() as stack:
        mocks = _base_patches(stack, base_has_advanced_effect=[True, False])
        mocks["validate_merge_result"].return_value = MergeValidationResult(passed=True)
        asyncio.run(merge_run(rn, job))

    mocks["validate_merge_result"].assert_awaited_once()
    assert mocks["validate_merge_result"].await_args.kwargs["run_tests"] is True
    mocks["merge_pr_squash"].assert_awaited_once()
    rn._rebase_conflict.assert_not_called()
    rn._retry_or_fail.assert_not_called()
    assert job.stage == Stage.DEPLOY


def test_overlap_warnings_route_to_conflict_recovery_not_squash_merge():
    """(c) base advances and validate_merge_result reports overlap_warnings:
    merge_pr_squash is never called, and the runner's conflict-recovery
    routing is invoked with both patches present in the failure message."""
    rn = _make_rn(merge_inlock_steps=2)
    job = _job()

    with ExitStack() as stack:
        mocks = _base_patches(stack, base_has_advanced_effect=[True, False])
        mocks["validate_merge_result"].return_value = MergeValidationResult(
            passed=False,
            overlap_warnings=["job and base both modified 'f.py' around original line 3"],
        )
        asyncio.run(merge_run(rn, job))

    mocks["merge_pr_squash"].assert_not_called()
    rn._retry_or_fail.assert_not_called()
    rn._rebase_conflict.assert_awaited_once()
    (_, failure_msg), _kwargs = rn._rebase_conflict.await_args
    assert failure_msg.startswith("[merge-conflict]")
    assert "job and base both modified 'f.py'" in failure_msg
    assert f"diff for {_MERGE_BASE_SHA}..HEAD" in failure_msg
    assert f"diff for {_MERGE_BASE_SHA}..main" in failure_msg


def test_lint_or_test_failure_without_overlap_routes_to_retry_or_fail():
    """(d) base advances and validate_merge_result reports a lint/test
    failure with no overlap: merge_pr_squash is never called, and the
    runner's retry/fail routing is invoked with failure_detail populated."""
    rn = _make_rn(merge_inlock_steps=2)
    job = _job()

    with ExitStack() as stack:
        mocks = _base_patches(stack, base_has_advanced_effect=[True, False])
        mocks["validate_merge_result"].return_value = MergeValidationResult(
            passed=False,
            lint_errors=["ruff: E999 SyntaxError"],
            test_result={"passed": False, "summary": "1 failed"},
        )
        asyncio.run(merge_run(rn, job))

    mocks["merge_pr_squash"].assert_not_called()
    rn._rebase_conflict.assert_not_called()
    rn._retry_or_fail.assert_awaited_once()
    (_, failure_text), kwargs = rn._retry_or_fail.await_args
    assert "ruff: E999 SyntaxError" in failure_text
    failure_detail = kwargs["failure_detail"]
    assert failure_detail["lint_errors"] == ["ruff: E999 SyntaxError"]
    assert failure_detail["test_result"] == {"passed": False, "summary": "1 failed"}
    assert failure_detail["fingerprint"]


def test_lint_or_test_failure_with_unserializable_resource_stays_json_safe():
    """(e) job #4263 postmortem: a test_result carrying a non-JSON-serialisable
    ResourceRecord under 'resource' must not crash the stage — the record is
    recorded through rn._record_resource and stripped from failure_detail."""
    rn = _make_rn(merge_inlock_steps=2)
    job = _job()
    record = resources.ResourceRecord(
        cpu_seconds=1.0,
        peak_rss_bytes=100,
        io_read_bytes=0,
        io_write_bytes=0,
        wall_seconds=2.0,
        sampled_at="2026-09-07T08:56:00+00:00",
    )

    with ExitStack() as stack:
        mocks = _base_patches(stack, base_has_advanced_effect=[True, False])
        mocks["validate_merge_result"].return_value = MergeValidationResult(
            passed=False,
            lint_errors=["ruff: E999 SyntaxError"],
            test_result={
                "passed": False,
                "summary": "1 failed",
                "command": "pytest -q",
                "output": "...",
                "resource": record,
            },
        )
        asyncio.run(merge_run(rn, job))

    mocks["merge_pr_squash"].assert_not_called()
    rn._rebase_conflict.assert_not_called()
    rn._retry_or_fail.assert_awaited_once()
    _, kwargs = rn._retry_or_fail.await_args
    failure_detail = kwargs["failure_detail"]
    json.dumps(failure_detail)
    assert "resource" not in failure_detail["test_result"]
    rn._record_resource.assert_called_once_with(job, "merge-validate", record)
