"""Behavior tests: the per-project schema lock serializes schema-touching jobs
from the merge boundary onward (job #700 postmortem — ledger-app outage
came from two schema jobs landing out of order).

Uses a minimal hand-written fake store (dict-based lock state, no Postgres)
so these tests exercise stages/merge.run's control flow in isolation. Every
git/GitHub call is mocked; the local-merge path is steered to fail fast via
merge_branch_measured so the test never depends on real repo state — only
whether the schema/merge lock gate was reached.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import Job, JobStatus, Stage
from hyqs.pipeline.stages.merge import run as merge_run


class _FakeStore:
    def __init__(self):
        self.schema_locks: dict[int, str] = {}
        self.merge_locks: dict[str, str] = {}
        self.merge_lock_calls: list[tuple] = []
        self.schema_lock_calls: list[tuple] = []

    def get_project(self, project_id):
        return SimpleNamespace(deploy_config="")

    def try_acquire_schema_lock(self, project_id, owner, now, ttl):
        self.schema_lock_calls.append((project_id, owner))
        current = self.schema_locks.get(project_id)
        if current is None or current == owner:
            self.schema_locks[project_id] = owner
            return True
        return False

    def release_schema_lock(self, project_id, owner):
        if self.schema_locks.get(project_id) == owner:
            del self.schema_locks[project_id]

    def try_acquire_merge_lock(self, repo_path, owner, now, ttl):
        self.merge_lock_calls.append((repo_path, owner))
        current = self.merge_locks.get(repo_path)
        if current is None or current == owner:
            self.merge_locks[repo_path] = owner
            return True
        return False

    def release_merge_lock(self, repo_path, owner):
        if self.merge_locks.get(repo_path) == owner:
            del self.merge_locks[repo_path]

    def save(self, job):
        pass


def _make_rn() -> SimpleNamespace:
    rn = SimpleNamespace()
    rn.store = _FakeStore()
    rn.worktrees = Path("/fake/worktrees")
    rn.merge_lock_ttl = 300.0
    rn.schema_lock_ttl = 300.0
    rn.merge_inlock_steps = 1
    rn._managed_repo = AsyncMock(return_value="/fake/managed")
    rn._event = MagicMock()
    rn._fail = AsyncMock()
    rn.notify = AsyncMock()
    return rn


def _job(*, id: int, project_id: int, repo_path: str, branch: str, owner: str = "worker-a") -> Job:
    return Job(
        id=id,
        idea="test idea",
        repo_path=repo_path,
        chat_id=1,
        stage=Stage.SECURITY,
        status=JobStatus.RUNNING,
        branch=branch,
        project_id=project_id,
        owner=owner,
    )


@contextmanager
def _patched_gitops(changed_files: list[str]):
    """Mock every git/GitHub call merge.run touches, steering the local-merge
    path to fail fast (via merge_branch_measured) right after the merge lock
    is taken — the tests only care whether the schema/merge lock gate ran."""
    with (
        patch(
            "hyqs.pipeline.stages.merge.gitops.default_branch",
            new_callable=AsyncMock,
            return_value="main",
        ),
        patch(
            "hyqs.pipeline.stages.merge.gitops.numstat",
            new_callable=AsyncMock,
            return_value=[{"path": f, "added": 1, "deleted": 0} for f in changed_files],
        ),
        patch(
            "hyqs.pipeline.stages.merge.gitops.patch",
            new_callable=AsyncMock,
            return_value="",
        ),
        patch(
            "hyqs.pipeline.stages.merge.github.has_remote",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "hyqs.pipeline.stages.merge.gitops.local_head_oid",
            new_callable=AsyncMock,
        ) as local_head_oid,
        patch(
            "hyqs.pipeline.stages.merge.github.inspect_branch_pr",
            new_callable=AsyncMock,
        ) as inspect_branch_pr,
        patch(
            "hyqs.pipeline.stages.merge.gitops.base_has_advanced",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "hyqs.pipeline.stages.merge.gitops.merge_branch_measured",
            new_callable=AsyncMock,
            return_value=(GitResult(ok=False, stdout="", stderr="boom", code=1), None),
        ),
    ):
        yield local_head_oid, inspect_branch_pr


def test_second_schema_job_waits_then_proceeds_after_first_terminates():
    rn = _make_rn()
    store = rn.store
    job_a = _job(id=1, project_id=100, repo_path="/repo/proj100", branch="hyqs/job-1")
    job_b = _job(id=2, project_id=100, repo_path="/repo/proj100", branch="hyqs/job-2")

    with _patched_gitops(["hyqs/pipeline/models.py"]) as identity_mocks:
        asyncio.run(merge_run(rn, job_a))
    identity_mocks[0].assert_not_awaited()
    identity_mocks[1].assert_not_awaited()
    # job A acquired the schema lock and proceeded to the merge-lock gate.
    assert store.schema_locks.get(100) == "job-1"
    assert len(store.merge_lock_calls) == 1

    with _patched_gitops(["hyqs/pipeline/models.py"]):
        asyncio.run(merge_run(rn, job_b))
    # job B could not acquire the still-held schema lock: requeued instead of
    # ever reaching the merge lock.
    assert job_b.status == JobStatus.PENDING
    assert job_b.owner == ""
    assert job_b.rebase_retry_after is not None
    assert len(store.merge_lock_calls) == 1

    # job A reaches a terminal state (its lock is released by _fail/deploy in
    # the real runner; simulate that here).
    store.release_schema_lock(100, "job-1")

    with _patched_gitops(["hyqs/pipeline/models.py"]):
        asyncio.run(merge_run(rn, job_b))
    assert store.schema_locks.get(100) == "job-2"
    assert len(store.merge_lock_calls) == 2


def test_two_non_schema_jobs_same_project_never_touch_schema_locks():
    rn = _make_rn()
    store = rn.store
    job_a = _job(id=3, project_id=200, repo_path="/repo/proj200", branch="hyqs/job-3")
    job_b = _job(id=4, project_id=200, repo_path="/repo/proj200", branch="hyqs/job-4")

    with _patched_gitops(["hyqs/pipeline/pricing.py"]):
        asyncio.run(merge_run(rn, job_a))
    with _patched_gitops(["hyqs/pipeline/pricing.py"]):
        asyncio.run(merge_run(rn, job_b))

    assert store.schema_lock_calls == []
    assert store.schema_locks == {}
    assert len(store.merge_lock_calls) == 2


def test_schema_jobs_for_different_projects_both_proceed_immediately():
    rn = _make_rn()
    store = rn.store
    job_a = _job(id=5, project_id=300, repo_path="/repo/proj300", branch="hyqs/job-5")
    job_b = _job(id=6, project_id=400, repo_path="/repo/proj400", branch="hyqs/job-6")

    with _patched_gitops(["hyqs/pipeline/models.py"]):
        asyncio.run(merge_run(rn, job_a))
    with _patched_gitops(["hyqs/pipeline/models.py"]):
        asyncio.run(merge_run(rn, job_b))

    assert store.schema_locks.get(300) == "job-5"
    assert store.schema_locks.get(400) == "job-6"
    assert len(store.merge_lock_calls) == 2
