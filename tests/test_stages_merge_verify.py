"""Tests for change-aware validation at the merge-delta boundary."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.models import Job, JobStatus, Stage
from hyqs.pipeline.stages import merge_verify


def test_merge_verify_passes_known_delta_to_test_selector(tmp_path):
    job = Job(
        id=42,
        idea="document the contract",
        repo_path="/fake/repo",
        chat_id=99,
        stage=Stage.MERGE_VERIFY,
        status=JobStatus.RUNNING,
        project_id=7,
        merge_delta_sha="old-base",
    )
    worktree = tmp_path / "job-42"
    worktree.mkdir()
    runner = MagicMock()
    runner.worktrees = tmp_path
    runner.store = MagicMock()
    runner.notify = AsyncMock()
    runner._managed_repo = AsyncMock(return_value=tmp_path)
    runner._event = MagicMock()
    runner._record_resource = MagicMock()

    merge_base = MagicMock(ok=True, stdout="fresh-base\n")
    run_tests = AsyncMock(
        return_value={
            "passed": True,
            "skipped": True,
            "command": "(skipped: documentation-only diff)",
            "resource": None,
        }
    )
    with (
        patch.object(merge_verify.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(merge_verify.gitops, "fresh_base", AsyncMock(return_value="origin/main")),
        patch.object(merge_verify.gitops, "git", AsyncMock(return_value=merge_base)),
        patch.object(
            merge_verify.gitops,
            "numstat",
            AsyncMock(return_value=[{"path": "docs/product/roles.md"}]),
        ),
        patch.object(merge_verify.testing, "run_tests", run_tests),
    ):
        asyncio.run(merge_verify.run(runner, job))

    run_tests.assert_awaited_once()
    assert run_tests.await_args.kwargs["changed_files"] == ["docs/product/roles.md"]
    assert job.merge_delta_sha is None
    assert job.stage is Stage.SECURITY
    assert job.status is JobStatus.PENDING
