"""Regression tests for the merge stage's activation-note/follow-up-job
filing logic (job #4099): after a squash-merge, a plan carrying
``activation.config_change_required=True`` must file exactly one follow-up
job describing the required activation step and append the activation
instructions to the merged job's implementation summary; a plan with
``config_change_required=False`` must append a "none required" note without
filing anything; and a legacy plan (``job.plan is None``) must leave the
implementation summary untouched.

Follows the fake-store/patched-gitops mocking pattern from
tests/test_stages_merge_post_merge_validation.py (fast-forward path:
``base_has_advanced_effect=[False]``).
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.github import PullRequest, PullRequestState
from hyqs.pipeline.gitops import GitResult
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
        self.created: list[dict] = []
        self.events: list[tuple[tuple, dict]] = []
        self._next_id = 1000

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

    def create(self, **kwargs) -> Job:
        self.created.append(kwargs)
        self._next_id += 1
        return Job(id=self._next_id, **kwargs)

    def add_event(self, *args, **kwargs):
        self.events.append((args, kwargs))


def _make_rn(*, merge_inlock_steps: int = 2) -> SimpleNamespace:
    rn = SimpleNamespace()
    rn.store = _FakeStore()
    rn.config = MagicMock()
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


def _job(*, id: int = 1, plan: dict | None = None) -> Job:
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
        plan=plan,
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


def test_config_change_required_files_followup_job_and_appends_note():
    rn = _make_rn()
    job = _job(
        plan={
            "activation": {
                "config_change_required": True,
                "activation_location": "hyqs/config.py FOO flag",
                "expected_live_effect": "widgets turn blue",
            }
        }
    )

    with ExitStack() as stack:
        _base_patches(stack, base_has_advanced_effect=[False])
        asyncio.run(merge_run(rn, job))

    assert len(rn.store.created) == 1
    source_meta = rn.store.created[0]["source_meta"]
    assert source_meta["activation_follow_up_of"] == job.id
    assert source_meta["activation_location"] == "hyqs/config.py FOO flag"
    assert source_meta["expected_live_effect"] == "widgets turn blue"
    assert "hyqs/config.py FOO flag" in job.implementation_summary
    assert "widgets turn blue" in job.implementation_summary
    assert job.stage == Stage.DEPLOY


def test_config_change_not_required_appends_none_required_note_and_skips_create():
    rn = _make_rn()
    job = _job(
        plan={
            "activation": {
                "config_change_required": False,
                "activation_location": "",
                "expected_live_effect": "",
            }
        }
    )

    with ExitStack() as stack:
        _base_patches(stack, base_has_advanced_effect=[False])
        asyncio.run(merge_run(rn, job))

    assert rn.store.created == []
    assert "none required" in job.implementation_summary.lower()
    assert job.stage == Stage.DEPLOY


def test_legacy_plan_none_leaves_summary_untouched():
    rn = _make_rn()
    job = _job()

    with ExitStack() as stack:
        _base_patches(stack, base_has_advanced_effect=[False])
        asyncio.run(merge_run(rn, job))

    assert rn.store.created == []
    assert job.implementation_summary == "a summary"
    assert job.stage == Stage.DEPLOY
