"""Regression tests: activation-followup jobs verify against host env, not the worktree.

Job #4337 failed terminally because ``_verify_already_satisfied`` checked out a
fresh worktree and asked an AI reviewer to find a gitignored repo-root ``.env``
that structurally can never be there. These tests prove the dispatch added in
``PipelineRunner._verify_already_satisfied`` routes ``source_meta.kind ==
"activation-followup"`` jobs to ``_verify_activation_followup`` instead, which
checks ``os.environ`` directly and never touches gitops or the backend — and
that a missing/absent value never produces a terminal failure.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.models import Job, JobStatus, Stage, Usage
from hyqs.pipeline.runner import PipelineRunner


def _job(**kw) -> Job:
    defaults = dict(
        id=4337,
        idea="Give each console user their own orchestrator chat",
        repo_path="/fake/repo",
        chat_id=1,
        stage=Stage.BUILD,
        status=JobStatus.RUNNING,
        owner="worker-a",
        provider="claude",
        plan={"stories": []},
    )
    defaults.update(kw)
    return Job(**defaults)


def _make_runner(tmp_path) -> PipelineRunner:
    store = MagicMock()
    config = SimpleNamespace(model="test-model", data_dir=str(tmp_path / "data"))
    return PipelineRunner(store, config, notify=AsyncMock())


def _activation_followup_job(**kw) -> Job:
    source_meta = {
        "kind": "activation-followup",
        "activation_location": "repo-root .env: HYQS_GITHUB_ORG",
        "expected_live_effect": "New project repos are created under HYQS_GITHUB_ORG.",
    }
    source_meta.update(kw.pop("source_meta", {}))
    return _job(source_meta=source_meta, **kw)


def _run_verify(rn: PipelineRunner, job: Job, backend=None):
    with (
        patch("hyqs.pipeline.runner.gitops.remove_worktree", new=AsyncMock()) as rm,
        patch("hyqs.pipeline.runner.gitops.fresh_base", new=AsyncMock()) as fb,
        patch("hyqs.pipeline.runner.gitops.create_detached_worktree", new=AsyncMock()) as cdw,
    ):
        backend = backend or MagicMock()
        backend.run = AsyncMock(
            return_value=SimpleNamespace(text="not a result block", usage=Usage())
        )
        asyncio.run(
            rn._verify_already_satisfied(
                job,
                "2026-09-21T00:00:00Z",
                managed=Path("/fake/managed"),
                worktree=Path("/fake/managed/wt"),
                base="main",
                backend=backend,
            )
        )
        return rm, fb, cdw, backend


def test_activation_followup_satisfied_marks_done_without_touching_worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("HYQS_GITHUB_ORG", "acme-corp")
    rn = _make_runner(tmp_path)
    job = _activation_followup_job()

    rm, fb, cdw, backend = _run_verify(rn, job)

    rm.assert_not_called()
    fb.assert_not_called()
    cdw.assert_not_called()
    backend.run.assert_not_called()
    assert job.status is JobStatus.DONE
    assert job.resolution == "already-satisfied"


def test_activation_followup_unsatisfied_fails_non_terminal(tmp_path, monkeypatch):
    monkeypatch.delenv("HYQS_GITHUB_ORG", raising=False)
    rn = _make_runner(tmp_path)
    job = _activation_followup_job()

    rm, fb, cdw, backend = _run_verify(rn, job)

    rm.assert_not_called()
    fb.assert_not_called()
    cdw.assert_not_called()
    backend.run.assert_not_called()
    assert job.status is JobStatus.FAILED
    assert job.failure_code == "activation_not_applied"
    assert job.retry_disposition == "human_review"
    assert job.retry_disposition != "terminal"


def test_activation_followup_unobservable_fails_non_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("HYQS_GITHUB_ORG", "acme-corp")
    rn = _make_runner(tmp_path)
    job = _activation_followup_job(
        source_meta={
            "activation_location": "hyqs/config.py FOO flag",
            "expected_live_effect": "widgets turn blue",
        }
    )

    rm, fb, cdw, backend = _run_verify(rn, job)

    rm.assert_not_called()
    fb.assert_not_called()
    cdw.assert_not_called()
    backend.run.assert_not_called()
    assert job.status is JobStatus.FAILED
    assert job.failure_code == "activation_unobservable"
    assert job.retry_disposition == "human_review"
    assert job.retry_disposition != "terminal"


def test_non_activation_followup_job_still_uses_worktree_checkout_path(tmp_path):
    rn = _make_runner(tmp_path)
    job = _job(source_meta=None)

    rm, fb, cdw, backend = _run_verify(rn, job)

    rm.assert_called_once()
    fb.assert_called_once()
    cdw.assert_called_once()
