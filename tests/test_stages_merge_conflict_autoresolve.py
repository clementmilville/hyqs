"""Behavior tests: the merge stage tries the deterministic pure-append
conflict auto-resolver (job #2272) BEFORE dispatching to the AI
``fix_conflict`` agent, at both places the stage currently routes straight to
``rn._rebase_conflict``: the in-lock base-advance loop, and the
GitHub-rejected-squash-merge conflict-recreation block.

Follows the fake-store/patched-gitops mocking pattern from
tests/test_stages_merge_phantom_conflict.py, additionally patching
``hyqs.pipeline.stages.merge.conflict_autoresolve.autoresolve_conflict``.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.conflict_autoresolve import AutoresolveResult
from hyqs.pipeline.github import PullRequest, PullRequestState
from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import Job, JobStatus, Stage
from hyqs.pipeline.stages.merge import run as merge_run

_NOT_MERGEABLE = GitResult(
    ok=False, stdout="", stderr="GH006: Protected branch: PR is not mergeable", code=1
)
_CONFLICT = GitResult(
    ok=False, stdout="", stderr="CONFLICT (content): Merge conflict in f.py", code=1
)
_OK = GitResult(ok=True, stdout="", stderr="", code=0)
_HEAD_OID = "d" * 40


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


def _make_rn(*, merge_inlock_steps: int = 2, phantom_max_attempts: int = 3) -> SimpleNamespace:
    rn = SimpleNamespace()
    rn.store = _FakeStore()
    rn.worktrees = Path("/fake/worktrees")
    rn.merge_lock_ttl = 300.0
    rn.merge_inlock_steps = merge_inlock_steps
    rn.rebase_max_attempts = 5
    rn.phantom_conflict_max_attempts = phantom_max_attempts
    rn.phantom_conflict_backoff = 0
    rn._managed_repo = AsyncMock(return_value="/fake/managed")
    rn._event = MagicMock()
    rn._fail = AsyncMock()
    rn._rebase_conflict = AsyncMock()
    rn.notify = AsyncMock()
    rn._record_resource = MagicMock()
    return rn


def _job(*, id: int = 1, owner: str = "worker-a") -> Job:
    return Job(
        id=id,
        idea="test idea",
        repo_path="/repo/proj",
        chat_id=1,
        stage=Stage.SECURITY,
        status=JobStatus.RUNNING,
        branch=f"hyqs/job-{id}",
        project_id=None,
        owner=owner,
    )


def _base_patches(stack: ExitStack, *, autoresolve_result: AutoresolveResult | None):
    """Patch every git/GitHub/AI call merge.run touches, common to all scenarios here."""
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
        patch("hyqs.pipeline.stages.merge.gitops.git", new_callable=AsyncMock, return_value=_OK)
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
            "hyqs.pipeline.stages.merge.gitops.head_info",
            new_callable=AsyncMock,
            return_value={"hash": "deadbeef", "subject": "x"},
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
            "hyqs.pipeline.stages.merge.github.reopen_pr",
            new_callable=AsyncMock,
            return_value=_OK,
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.github.pr_create",
            new_callable=AsyncMock,
            return_value=_OK,
        )
    )
    stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.github.pr_identity_is_merged",
            new_callable=AsyncMock,
            return_value=False,
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
    stack.enter_context(patch("hyqs.pipeline.stages.merge.asyncio.sleep", new_callable=AsyncMock))
    autoresolve_mock = stack.enter_context(
        patch(
            "hyqs.pipeline.stages.merge.conflict_autoresolve.autoresolve_conflict",
            new_callable=AsyncMock,
            return_value=autoresolve_result,
        )
    )
    return autoresolve_mock


def test_inlock_loop_autoresolve_ok_completes_merge_without_ai_fix():
    """(a) In-lock-loop conflict that autoresolve reports ok=True completes the
    merge and reaches Stage.DEPLOY without ever calling rn._rebase_conflict."""
    rn = _make_rn(merge_inlock_steps=2)
    job = _job()

    with ExitStack() as stack:
        autoresolve_mock = _base_patches(
            stack, autoresolve_result=AutoresolveResult(ok=True, files=["f.py"])
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.base_has_advanced",
                new_callable=AsyncMock,
                side_effect=[True, False],
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.merge_base_into_worktree",
                new_callable=AsyncMock,
                return_value=_CONFLICT,
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.push", new_callable=AsyncMock, return_value=_OK
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.github.merge_pr_squash",
                new_callable=AsyncMock,
                return_value=_OK,
            )
        )
        asyncio.run(merge_run(rn, job))

    autoresolve_mock.assert_awaited_once()
    rn._rebase_conflict.assert_not_called()
    rn._fail.assert_not_called()
    assert job.stage == Stage.DEPLOY


def test_inlock_loop_autoresolve_fail_falls_through_to_ai_fix_unchanged():
    """(c) autoresolve reporting ok=False at the in-lock-loop site results in
    rn._rebase_conflict being called with the same conflict_msg content as
    before this change (byte-for-byte unchanged AI-path behavior)."""
    rn = _make_rn(merge_inlock_steps=2)
    job = _job()

    with ExitStack() as stack:
        autoresolve_mock = _base_patches(
            stack, autoresolve_result=AutoresolveResult(ok=False, reason="not a pure append")
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.base_has_advanced",
                new_callable=AsyncMock,
                return_value=True,
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.merge_base_into_worktree",
                new_callable=AsyncMock,
                return_value=_CONFLICT,
            )
        )
        asyncio.run(merge_run(rn, job))

    autoresolve_mock.assert_awaited_once()
    rn._rebase_conflict.assert_awaited_once()
    (_, conflict_msg), _kwargs = rn._rebase_conflict.await_args
    expected_msg = (
        f"[merge-conflict] Conflict markers (<<<<<<<, =======, >>>>>>>) are present "
        f"in the worktree after merging main. Conflicted files:\n{_CONFLICT.stderr}\n\n"
        f"Resolve each file by editing out the conflict markers (keep the correct "
        f"merged code), ensure `git status` shows no UU entries, and make sure all "
        f"tests pass. Do NOT commit — the pipeline commits for you."
    )
    assert conflict_msg == expected_msg


def test_github_path_autoresolve_ok_recovers_via_existing_force_push_retry():
    """(b) GitHub-path conflict that autoresolve reports ok=True is recovered
    via the existing force-push+retry-squash mechanism (asserted via the same
    force_push_branch/pr_merge_squash mocks already used by the
    phantom-conflict tests) and rn._rebase_conflict is never called."""
    rn = _make_rn()
    job = _job()

    with ExitStack() as stack:
        autoresolve_mock = _base_patches(
            stack, autoresolve_result=AutoresolveResult(ok=True, files=["f.py"])
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.base_has_advanced",
                new_callable=AsyncMock,
                return_value=False,
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.github.merge_pr_squash",
                new_callable=AsyncMock,
                side_effect=[_NOT_MERGEABLE, _OK],
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.merge_base_into_worktree",
                new_callable=AsyncMock,
                return_value=_CONFLICT,
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops._is_merge_plumbing_error",
                return_value=False,
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.has_unmerged_paths",
                new_callable=AsyncMock,
                return_value=True,
            )
        )
        force_push_mock = stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.github.force_push_branch",
                new_callable=AsyncMock,
                return_value=_OK,
            )
        )
        asyncio.run(merge_run(rn, job))

    autoresolve_mock.assert_awaited_once()
    force_push_mock.assert_awaited_once()
    rn._rebase_conflict.assert_not_called()
    rn._fail.assert_not_called()
    assert job.stage == Stage.DEPLOY


def test_github_path_autoresolve_fail_falls_through_to_ai_fix_unchanged():
    """(c) autoresolve reporting ok=False at the GitHub-recovery site results
    in rn._rebase_conflict being called with the same conflict_msg content as
    before this change."""
    rn = _make_rn()
    job = _job()

    with ExitStack() as stack:
        autoresolve_mock = _base_patches(
            stack, autoresolve_result=AutoresolveResult(ok=False, reason="not a pure append")
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.base_has_advanced",
                new_callable=AsyncMock,
                return_value=False,
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.github.merge_pr_squash",
                new_callable=AsyncMock,
                return_value=_NOT_MERGEABLE,
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.merge_base_into_worktree",
                new_callable=AsyncMock,
                return_value=_CONFLICT,
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops._is_merge_plumbing_error",
                return_value=False,
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.has_unmerged_paths",
                new_callable=AsyncMock,
                return_value=True,
            )
        )
        force_push_mock = stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.github.force_push_branch",
                new_callable=AsyncMock,
                return_value=_OK,
            )
        )
        asyncio.run(merge_run(rn, job))

    autoresolve_mock.assert_awaited_once()
    force_push_mock.assert_not_called()
    rn._rebase_conflict.assert_awaited_once()
    (_, conflict_msg), _kwargs = rn._rebase_conflict.await_args
    expected_msg = (
        f"[merge-conflict] Conflict markers (<<<<<<<, =======, >>>>>>>) are present "
        f"in the worktree after GitHub rejected the squash-merge against main. "
        f"Conflicted files:\n{_CONFLICT.stderr}\n\n"
        f"Resolve each file by editing out the conflict markers (keep the correct "
        f"merged code), ensure `git status` shows no UU entries, and make sure all "
        f"tests pass. Do NOT commit — the pipeline commits for you."
    )
    assert conflict_msg == expected_msg


def test_phantom_conflict_never_invokes_autoresolve():
    """(d) A job whose local re-merge is clean (the existing phantom-conflict
    case — no genuine unmerged paths) never invokes conflict_autoresolve at
    all; that path is owned entirely by the existing phantom-conflict
    recovery, unaffected by this change."""
    rn = _make_rn()
    job = _job()

    with ExitStack() as stack:
        autoresolve_mock = _base_patches(stack, autoresolve_result=AutoresolveResult(ok=False))
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.base_has_advanced",
                new_callable=AsyncMock,
                return_value=False,
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.github.merge_pr_squash",
                new_callable=AsyncMock,
                side_effect=[_NOT_MERGEABLE, _OK],
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.merge_base_into_worktree",
                new_callable=AsyncMock,
                return_value=_OK,  # local re-merge is clean: phantom conflict
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops._is_merge_plumbing_error",
                return_value=False,
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.gitops.has_unmerged_paths",
                new_callable=AsyncMock,
                return_value=False,
            )
        )
        stack.enter_context(
            patch(
                "hyqs.pipeline.stages.merge.github.force_push_branch",
                new_callable=AsyncMock,
                return_value=_OK,
            )
        )
        asyncio.run(merge_run(rn, job))

    autoresolve_mock.assert_not_called()
    rn._rebase_conflict.assert_not_called()
    assert job.stage == Stage.DEPLOY
