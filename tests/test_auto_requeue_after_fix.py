"""No-human-in-the-loop requeue: failed jobs re-run automatically once their fix lands.

Three paths, all using the existing dependency gate in claim():
- AI analyst file_fix_job: original is requeued immediately, dependency-gated on the fix.
- Deploy fix-forward: original requeued at DEPLOY behind the fix (coalesces to DONE).
- dependency_blocked jobs auto-requeue once every dependency has landed.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyqs.config import Config
from hyqs.pipeline import gitops
from hyqs.pipeline.collision import QueueSurveyResult
from hyqs.pipeline.models import Job, JobSource, JobStatus, Stage
from hyqs.pipeline.store import RemediationLineage, SupervisorRemediationResult
from hyqs.pipeline.supervisor import (
    MAX_AUTOMATED_REMEDIATION_DEPTH,
    FailureClass,
    _ai_diagnose_and_act,
    _janitor_scan,
    _reconcile_blocked_dependents,
    _split_from_saved_plan,
    remediate_dependency_blocked,
    remediate_deploy_failed,
    remediate_stale_branch,
    remediate_transient,
)


def _job(**kw) -> Job:
    defaults = dict(
        id=543, idea="i", repo_path="/r", chat_id=1, stage=Stage.BUILD, status=JobStatus.FAILED
    )
    defaults.update(kw)
    return Job(**defaults)


def test_dependency_cascade_requires_durable_dead_letter_event():
    root = _job(id=1, failure_code="test_failed")
    dependent = _job(id=2, status=JobStatus.PENDING, stage=Stage.QUEUED)
    jobs = MagicMock()
    jobs.list_pending_with_unsatisfied_deps.return_value = [dependent]
    jobs.get_unsatisfied_deps.return_value = [root.id]
    jobs.get.return_value = root
    jobs.has_supervisor_event.return_value = False

    asyncio.run(_reconcile_blocked_dependents(jobs, AsyncMock()))

    jobs.save.assert_not_called()
    jobs.has_supervisor_event.assert_called_once_with(root.id, "dead_lettered")

    jobs.has_supervisor_event.return_value = True
    asyncio.run(_reconcile_blocked_dependents(jobs, AsyncMock()))

    jobs.save.assert_called_once_with(dependent)
    assert dependent.status is JobStatus.FAILED
    assert dependent.failure_code == "dependency_blocked"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_seed_remediation_worktree_uses_recorded_sha_not_later_branch_tip(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "hyqs/job-10")
    (repo / "candidate.txt").write_text("recorded\n")
    _git(repo, "add", "candidate.txt")
    _git(repo, "commit", "-m", "failed candidate")
    recorded_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "later.txt").write_text("later tip\n")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-m", "later branch movement")
    _git(repo, "checkout", "main")
    worktree = tmp_path / "remediation"

    created = asyncio.run(gitops.create_worktree(repo, "hyqs/job-11", worktree, base="main"))
    assert created.ok
    asyncio.run(
        gitops.seed_remediation_worktree(
            repo,
            worktree,
            base="main",
            source_branch="hyqs/job-10",
            source_sha=recorded_sha,
        )
    )

    assert (worktree / "candidate.txt").read_text() == "recorded\n"
    assert not (worktree / "later.txt").exists()


def test_seed_remediation_worktree_rejects_sha_outside_recorded_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "hyqs/job-20")
    (repo / "one.txt").write_text("one\n")
    _git(repo, "add", "one.txt")
    _git(repo, "commit", "-m", "one")
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "unrelated")
    (repo / "other.txt").write_text("other\n")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "other")
    unrelated_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    worktree = tmp_path / "remediation"
    assert asyncio.run(gitops.create_worktree(repo, "hyqs/job-21", worktree, base="main")).ok

    with pytest.raises(ValueError, match="does not belong"):
        asyncio.run(
            gitops.seed_remediation_worktree(
                repo,
                worktree,
                base="main",
                source_branch="hyqs/job-20",
                source_sha=unrelated_sha,
            )
        )


def test_saved_plan_split_supersedes_repeated_plan_failure():
    jobs = MagicMock()
    jobs.create.side_effect = [MagicMock(id=701), MagicMock(id=702)]
    notify = AsyncMock()
    job = _job(
        stage=Stage.PLAN,
        plan={
            "stories": [
                {
                    "id": "S1",
                    "title": "Store audit rows",
                    "task": "Add persistence",
                    "acceptance": "Rows are durable",
                    "target_files": ["backend/audit.py"],
                    "depends_on": [],
                },
                {
                    "id": "S2",
                    "title": "Expose audit API",
                    "task": "Add endpoint",
                    "acceptance": "Endpoint returns rows",
                    "target_files": ["backend/api.py"],
                    "depends_on": ["S1"],
                },
            ]
        },
    )

    assert asyncio.run(_split_from_saved_plan(jobs, job, notify)) is True

    assert jobs.create.call_args_list[1].kwargs["depends_on"] == [701]
    jobs.repoint_split_dependents.assert_called_once_with(job.id, [701, 702])
    jobs.supersede_failed_with_split.assert_called_once_with(job.id, [701, 702])
    notify.assert_awaited_once()


def test_saved_plan_split_subdivides_single_oversized_story():
    jobs = MagicMock()
    jobs.create.side_effect = [MagicMock(id=701), MagicMock(id=702), MagicMock(id=703)]
    notify = AsyncMock()
    job = _job(
        stage=Stage.PLAN,
        plan={
            "stories": [
                {
                    "id": "S1",
                    "title": "Adopt routers",
                    "task": "Update all routers",
                    "acceptance": "All routers use the service",
                    "target_files": [f"backend/router_{index}.py" for index in range(9)],
                    "depends_on": [],
                },
                {
                    "id": "S2",
                    "title": "Verify integration",
                    "task": "Add integration coverage",
                    "acceptance": "Integration passes",
                    "target_files": ["backend/tests/test_integration.py"],
                    "depends_on": ["S1"],
                },
            ]
        },
    )

    assert asyncio.run(_split_from_saved_plan(jobs, job, notify)) is True

    calls = jobs.create.call_args_list
    assert len(calls[0].kwargs["source_meta"]["story_id"]) > 0
    assert len(calls[0].kwargs["idea"].split("Target files: ", 1)[1].split(", ")) == 8
    assert calls[1].kwargs["depends_on"] == [701]
    assert calls[2].kwargs["depends_on"] == [702]
    jobs.supersede_failed_with_split.assert_called_once_with(job.id, [701, 702, 703])


def test_transient_dead_letter_event_is_idempotent_across_scans():
    jobs = MagicMock()
    jobs.supervisor_requeue_count.return_value = 5
    jobs.has_supervisor_event.side_effect = [False, True]
    job = _job()
    config = Config(model="sonnet", permission_mode="acceptEdits")

    asyncio.run(remediate_transient(jobs, job, config, dead_letter_cap=5))
    asyncio.run(remediate_transient(jobs, job, config, dead_letter_cap=5))

    dead_letters = [
        call
        for call in jobs.record_supervisor_event.call_args_list
        if call.args[1] == "dead_lettered"
    ]
    assert len(dead_letters) == 1


def test_analyst_fix_job_gates_and_requeues_original():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.create.return_value = MagicMock(id=605)
    job = _job(error="gave up after 5 fix attempt(s).")

    diagnosis = (
        {
            "diagnosis": "gate deadlock",
            "confidence": 0.85,
            "action": {"type": "file_fix_job", "idea": "x" * 80},
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), AsyncMock())
        )

    assert ran
    jobs.add_job_dependency.assert_called_once_with(job.id, 605)
    jobs.set_active_remediation.assert_called_once_with(job.id, 605)
    jobs.requeue_job.assert_called_once_with(job.id, zero_attempts=True)


def test_analyst_repairs_existing_branch_in_place_with_audited_scope_expansion():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.supervisor_requeue_count.return_value = 1
    job = _job(
        branch="hyqs/job-543",
        failure="configuration consumer failed",
        source_meta={"scope": {"allowed_paths": ["src/config.py"], "frozen": True}},
    )
    diagnosis = (
        {
            "diagnosis": "the original change missed a directly related consumer",
            "confidence": 0.95,
            "action": {
                "type": "repair_in_place",
                "idea": "Update the directly related configuration consumer and regression test.",
                "allowed_paths": ["src/config.py", "tests/test_config.py"],
            },
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), AsyncMock())
        )

    assert ran
    assert job.source_meta["scope"]["allowed_paths"] == [
        "src/config.py",
        "tests/test_config.py",
    ]
    assert job.source_meta["in_place_repairs"][-1]["added_paths"] == ["tests/test_config.py"]
    jobs.save.assert_called_once_with(job)
    jobs.increment_supervisor_requeue.assert_called_once_with(job.id)
    requeue = jobs.requeue_job_at_stage.call_args
    assert requeue.args == (job.id, Stage.FIX)
    assert requeue.kwargs["failure"].startswith("configuration consumer failed")
    assert "Update the directly related configuration consumer" in requeue.kwargs["failure"]
    jobs.create.assert_not_called()
    jobs.create_supervisor_remediation.assert_not_called()
    assert any(
        call.args[1] == "scope_expanded" for call in jobs.record_supervisor_event.call_args_list
    )


def test_analyst_fix_job_skipped_when_human_remediation_live():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = 900
    jobs.get.return_value = MagicMock(id=900, status=JobStatus.RUNNING, source=JobSource.MCP)
    notify = AsyncMock()
    job = _job(error="gave up after 5 fix attempt(s).")

    diagnosis = (
        {
            "diagnosis": "gate deadlock",
            "confidence": 0.85,
            "action": {"type": "file_fix_job", "idea": "x" * 80},
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), notify)
        )

    assert ran
    jobs.create.assert_not_called()
    jobs.requeue_job.assert_not_called()
    notify.assert_awaited()
    notify_message = notify.call_args.args[1]
    assert f"job #{job.id}" in notify_message
    assert "job #900" in notify_message


def test_analyst_fix_of_fix_inherits_lineage_depth():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.resolve_remediation_lineage.return_value = RemediationLineage(100, 1)
    created = MagicMock(id=605)
    jobs.create_supervisor_remediation.return_value = SupervisorRemediationResult(
        [created], RemediationLineage(100, 2)
    )
    diagnosis = (
        {
            "diagnosis": "the first remediation failed",
            "confidence": 0.9,
            "action": {"type": "file_fix_job", "idea": "x" * 80},
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        asyncio.run(
            _ai_diagnose_and_act(
                jobs, _job(id=200), FailureClass.genuine_code, MagicMock(), AsyncMock()
            )
        )

    jobs.create_supervisor_remediation.assert_called_once()
    jobs.requeue_job.assert_called_once_with(200, zero_attempts=True)


def test_analyst_fix_hands_exact_source_branch_and_sha_to_store(tmp_path):
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.resolve_remediation_lineage.return_value = RemediationLineage(543, 0)
    jobs.create_supervisor_remediation.return_value = SupervisorRemediationResult(
        [MagicMock(id=605)], RemediationLineage(543, 1)
    )
    source_dir = tmp_path / "worktrees" / "job-543"
    source_dir.mkdir(parents=True)
    job = _job(branch="hyqs/job-543", error="builder failed")
    source_sha = "b" * 40
    diagnosis = (
        {
            "diagnosis": "candidate needs a focused repair",
            "confidence": 0.9,
            "action": {"type": "file_fix_job", "idea": "x" * 80},
        },
        MagicMock(),
    )
    config = MagicMock(data_dir=str(tmp_path))
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
        patch(
            "hyqs.pipeline.supervisor.gitops.git",
            new=AsyncMock(
                side_effect=[
                    gitops.GitResult(True, source_sha, "", 0),
                    gitops.GitResult(True, job.branch, "", 0),
                ]
            ),
        ),
        patch("hyqs.pipeline.supervisor.gitops.tracked_files", new=AsyncMock(return_value=[])),
    ):
        asyncio.run(_ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, config, AsyncMock()))

    call = jobs.create_supervisor_remediation.call_args
    assert call.kwargs["source_branch"] == job.branch
    assert call.kwargs["source_sha"] == source_sha


def test_analyst_depth_cap_escalates_without_filing():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.resolve_remediation_lineage.return_value = RemediationLineage(
        100, MAX_AUTOMATED_REMEDIATION_DEPTH
    )
    diagnosis = (
        {
            "diagnosis": "depth exhausted",
            "confidence": 0.9,
            "action": {"type": "file_fix_job", "idea": "x" * 80},
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        asyncio.run(
            _ai_diagnose_and_act(
                jobs, _job(id=200), FailureClass.genuine_code, MagicMock(), AsyncMock()
            )
        )

    jobs.create_supervisor_remediation.assert_not_called()
    event = next(
        call
        for call in jobs.record_supervisor_event.call_args_list
        if call.args[1] == "remediation_escalated"
    )
    detail = json.loads(event.kwargs["detail"])
    assert detail["remediation_root_job_id"] == 100
    assert detail["limit"] == MAX_AUTOMATED_REMEDIATION_DEPTH


def test_analyst_fix_job_idea_carries_covered_story_text_verbatim():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.create.return_value = MagicMock(id=605)
    story = {
        "id": "S1",
        "title": "Add helper",
        "task": "exact verbatim task text for S1",
        "acceptance": "exact verbatim acceptance text for S1",
        "target_files": ["hyqs/pipeline/remediation.py"],
    }
    job = _job(
        error="gave up after 5 fix attempt(s).",
        plan={"stories": [story]},
    )

    diagnosis = (
        {
            "diagnosis": "gate deadlock",
            "confidence": 0.85,
            "action": {"type": "file_fix_job", "idea": "x" * 80, "covers_stories": ["S1"]},
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), AsyncMock())
        )

    assert ran
    create_kwargs = jobs.create.call_args.kwargs
    assert f"job #{job.id}" in create_kwargs["idea"]
    assert story["task"] in create_kwargs["idea"]
    assert story["acceptance"] in create_kwargs["idea"]
    assert create_kwargs["source_meta"]["covers_stories"] == ["S1"]
    jobs.add_job_dependency.assert_called_once_with(job.id, 605)
    jobs.set_active_remediation.assert_called_once_with(job.id, 605)
    jobs.requeue_job.assert_called_once_with(job.id, zero_attempts=True)


def test_analyst_fix_jobs_chain_gates_and_requeues_original():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.create.side_effect = [MagicMock(id=610), MagicMock(id=611)]
    job = _job(error="gave up after 5 fix attempt(s).")

    diagnosis = (
        {
            "diagnosis": "needs two sequential jobs",
            "confidence": 0.85,
            "action": {
                "type": "file_fix_jobs",
                "jobs": [
                    {"idea": "x" * 80, "depends_on": []},
                    {"idea": "y" * 80, "depends_on": [0]},
                ],
            },
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), AsyncMock())
        )

    assert ran
    assert jobs.create.call_count == 2
    second_call_kwargs = jobs.create.call_args_list[1].kwargs
    assert second_call_kwargs["depends_on"] == [610]
    jobs.add_job_dependency.assert_called_once_with(job.id, 611)
    jobs.set_active_remediation.assert_called_once_with(job.id, 611)
    jobs.requeue_job.assert_called_once_with(job.id, zero_attempts=True)


def test_analyst_fix_jobs_chain_idea_carries_covered_story_text_verbatim():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.create.side_effect = [MagicMock(id=610), MagicMock(id=611)]
    story = {
        "id": "S1",
        "title": "Add helper",
        "task": "exact verbatim chain task text for S1",
        "acceptance": "exact verbatim chain acceptance text for S1",
        "target_files": ["hyqs/pipeline/remediation.py"],
    }
    job = _job(
        error="gave up after 5 fix attempt(s).",
        plan={"stories": [story]},
    )

    diagnosis = (
        {
            "diagnosis": "needs two sequential jobs",
            "confidence": 0.85,
            "action": {
                "type": "file_fix_jobs",
                "jobs": [
                    {"idea": "x" * 80, "depends_on": [], "covers_stories": ["S1"]},
                    {"idea": "y" * 80, "depends_on": [0]},
                ],
            },
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), AsyncMock())
        )

    assert ran
    first_call_kwargs = jobs.create.call_args_list[0].kwargs
    second_call_kwargs = jobs.create.call_args_list[1].kwargs
    assert f"job #{job.id}" in first_call_kwargs["idea"]
    assert story["task"] in first_call_kwargs["idea"]
    assert story["acceptance"] in first_call_kwargs["idea"]
    assert first_call_kwargs["source_meta"]["covers_stories"] == ["S1"]
    assert second_call_kwargs["source_meta"]["covers_stories"] == []
    jobs.add_job_dependency.assert_called_once_with(job.id, 611)
    jobs.set_active_remediation.assert_called_once_with(job.id, 611)
    jobs.requeue_job.assert_called_once_with(job.id, zero_attempts=True)


def test_analyst_fix_jobs_chain_skipped_when_human_remediation_live():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = 900
    jobs.get.return_value = MagicMock(id=900, status=JobStatus.PENDING, source=JobSource.UI)
    notify = AsyncMock()
    job = _job(error="gave up after 5 fix attempt(s).")

    diagnosis = (
        {
            "diagnosis": "needs two sequential jobs",
            "confidence": 0.85,
            "action": {
                "type": "file_fix_jobs",
                "jobs": [
                    {"idea": "x" * 80, "depends_on": []},
                    {"idea": "y" * 80, "depends_on": [0]},
                ],
            },
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), notify)
        )

    assert ran
    jobs.create.assert_not_called()
    jobs.requeue_job.assert_not_called()
    notify.assert_awaited()
    notify_message = notify.call_args.args[1]
    assert f"job #{job.id}" in notify_message
    assert "job #900" in notify_message


def test_analyst_fix_jobs_chain_auto_serializes_overlapping_files():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.create.side_effect = [MagicMock(id=610), MagicMock(id=611)]
    job = _job(error="gave up after 5 fix attempt(s).")

    diagnosis = (
        {
            "diagnosis": "needs two jobs touching the same file",
            "confidence": 0.85,
            "action": {
                "type": "file_fix_jobs",
                "jobs": [
                    {
                        "idea": "fix a\n\nFiles: hyqs/pipeline/remediation.py " + "x" * 40,
                        "depends_on": [],
                    },
                    {
                        "idea": "fix b\n\nFiles: hyqs/pipeline/remediation.py " + "y" * 40,
                        "depends_on": [],
                    },
                ],
            },
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), AsyncMock())
        )

    assert ran
    assert jobs.create.call_count == 2
    second_call_kwargs = jobs.create.call_args_list[1].kwargs
    assert second_call_kwargs["depends_on"] == [610]
    jobs.add_job_dependency.assert_called_once_with(job.id, 611)


def test_analyst_fix_jobs_chain_disjoint_files_stay_parallel():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.create.side_effect = [MagicMock(id=610), MagicMock(id=611)]
    job = _job(error="gave up after 5 fix attempt(s).")

    diagnosis = (
        {
            "diagnosis": "needs two independent jobs",
            "confidence": 0.85,
            "action": {
                "type": "file_fix_jobs",
                "jobs": [
                    {
                        "idea": "fix a\n\nFiles: hyqs/pipeline/remediation.py " + "x" * 40,
                        "depends_on": [],
                    },
                    {
                        "idea": "fix b\n\nFiles: hyqs/web/app.py " + "y" * 40,
                        "depends_on": [],
                    },
                ],
            },
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), AsyncMock())
        )

    assert ran
    assert jobs.create.call_count == 2
    second_call_kwargs = jobs.create.call_args_list[1].kwargs
    assert second_call_kwargs["depends_on"] == []
    jobs.add_job_dependency.assert_called_once_with(job.id, 611)


def test_analyst_fix_jobs_chain_three_way_overlap_forms_linear_chain():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.create.side_effect = [MagicMock(id=610), MagicMock(id=611), MagicMock(id=612)]
    job = _job(error="gave up after 5 fix attempt(s).")

    same_file = "Files: hyqs/pipeline/remediation.py"
    diagnosis = (
        {
            "diagnosis": "needs three mutually-overlapping jobs",
            "confidence": 0.85,
            "action": {
                "type": "file_fix_jobs",
                "jobs": [
                    {"idea": f"fix a\n\n{same_file} " + "x" * 40, "depends_on": []},
                    {"idea": f"fix b\n\n{same_file} " + "y" * 40, "depends_on": []},
                    {"idea": f"fix c\n\n{same_file} " + "z" * 40, "depends_on": []},
                ],
            },
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), AsyncMock())
        )

    assert ran
    assert jobs.create.call_count == 3
    call_list = jobs.create.call_args_list
    assert call_list[0].kwargs["depends_on"] == []
    assert call_list[1].kwargs["depends_on"] == [610]
    assert call_list[2].kwargs["depends_on"] == [610, 611]
    jobs.add_job_dependency.assert_called_once_with(job.id, 612)


def test_analyst_fix_job_chains_behind_overlapping_active_queue_job():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.resolve_remediation_lineage.return_value = RemediationLineage(100, 0)
    jobs.create.return_value = MagicMock(id=605)
    jobs.survey_active_job_queue.return_value = QueueSurveyResult(
        overlaps={"0": [42, 100, 543]}, unknown_target_file_jobs=[100, 543]
    )
    job = _job(error="gave up after 5 fix attempt(s).", project_id=7)

    diagnosis = (
        {
            "diagnosis": "gate deadlock",
            "confidence": 0.85,
            "action": {
                "type": "file_fix_job",
                "idea": "fix it\n\nFiles: hyqs/pipeline/foo.py " + "x" * 40,
            },
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), AsyncMock())
        )

    assert ran
    jobs.survey_active_job_queue.assert_called_once_with(
        7,
        [
            {
                "key": "0",
                "title": "fix it\n\nFiles: hyqs/pipeline/foo.py " + "x" * 40,
                "target_files": ["hyqs/pipeline/foo.py"],
            }
        ],
    )
    create_kwargs = jobs.create.call_args.kwargs
    assert create_kwargs["depends_on"] == [42]
    assert create_kwargs["source_meta"]["queue_survey"] == {
        "overlapping_job_ids": [42, 100, 543],
        "unknown_target_file_jobs": [100, 543],
        "excluded_lineage_job_ids": [100, 543],
        "depends_on_job_ids": [42],
    }
    remediation_specs = jobs.create_supervisor_remediation.call_args.args[1]
    assert remediation_specs[0]["depends_on_job_ids"] == [42]
    jobs.add_job_dependency.assert_called_once_with(job.id, 605)
    jobs.set_active_remediation.assert_called_once_with(job.id, 605)
    jobs.requeue_job.assert_called_once_with(job.id, zero_attempts=True)


def test_analyst_fix_job_no_queue_dependency_when_no_overlap():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.create.return_value = MagicMock(id=605)
    jobs.survey_active_job_queue.return_value = QueueSurveyResult(
        overlaps={"0": []}, unknown_target_file_jobs=[]
    )
    job = _job(error="gave up after 5 fix attempt(s).", project_id=7)

    diagnosis = (
        {
            "diagnosis": "gate deadlock",
            "confidence": 0.85,
            "action": {
                "type": "file_fix_job",
                "idea": "fix it\n\nFiles: hyqs/pipeline/foo.py " + "x" * 40,
            },
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), AsyncMock())
        )

    assert ran
    create_kwargs = jobs.create.call_args.kwargs
    assert create_kwargs["depends_on"] == []
    assert create_kwargs["source_meta"]["queue_survey"] == {
        "overlapping_job_ids": [],
        "unknown_target_file_jobs": [],
        "excluded_lineage_job_ids": [543],
        "depends_on_job_ids": [],
    }
    jobs.add_job_dependency.assert_called_once_with(job.id, 605)


def test_analyst_fix_job_chains_behind_unknown_scope_active_job():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.create.return_value = MagicMock(id=605)
    jobs.survey_active_job_queue.return_value = QueueSurveyResult(
        overlaps={"0": []}, unknown_target_file_jobs=[99]
    )
    job = _job(error="gave up after 5 fix attempt(s).", project_id=7)

    diagnosis = (
        {
            "diagnosis": "gate deadlock",
            "confidence": 0.85,
            "action": {
                "type": "file_fix_job",
                "idea": "fix it\n\nFiles: hyqs/pipeline/foo.py " + "x" * 40,
            },
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), AsyncMock())
        )

    assert ran
    create_kwargs = jobs.create.call_args.kwargs
    assert create_kwargs["depends_on"] == [99]
    assert create_kwargs["source_meta"]["queue_survey"] == {
        "overlapping_job_ids": [],
        "unknown_target_file_jobs": [99],
        "excluded_lineage_job_ids": [543],
        "depends_on_job_ids": [99],
    }
    jobs.add_job_dependency.assert_called_once_with(job.id, 605)


def test_analyst_fix_jobs_chain_combines_queue_survey_with_intra_chain_deps():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.create.side_effect = [MagicMock(id=610), MagicMock(id=611)]
    jobs.survey_active_job_queue.return_value = QueueSurveyResult(
        overlaps={"0": [42], "1": []}, unknown_target_file_jobs=[]
    )
    job = _job(error="gave up after 5 fix attempt(s).", project_id=7)

    diagnosis = (
        {
            "diagnosis": "needs two sequential jobs",
            "confidence": 0.85,
            "action": {
                "type": "file_fix_jobs",
                "jobs": [
                    {"idea": "fix a\n\nFiles: hyqs/pipeline/foo.py " + "x" * 40, "depends_on": []},
                    {"idea": "fix b\n\nFiles: hyqs/web/app.py " + "y" * 40, "depends_on": []},
                ],
            },
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), AsyncMock())
        )

    assert ran
    call_list = jobs.create.call_args_list
    assert call_list[0].kwargs["depends_on"] == [42]
    assert call_list[1].kwargs["depends_on"] == []
    jobs.add_job_dependency.assert_called_once_with(job.id, 611)


def test_deploy_fix_forward_gates_original_at_deploy():
    jobs = MagicMock()
    jobs.supervisor_requeue_count.return_value = 0
    jobs.has_deploy_fix_job.return_value = False
    jobs.count_deploy_fix_jobs.return_value = 0
    jobs.create.return_value = MagicMock(id=700)
    job = _job(stage=Stage.DEPLOY, error="deploy failed: rollup failed", source_meta={})

    asyncio.run(remediate_deploy_failed(jobs, job, MagicMock(), AsyncMock()))

    jobs.create.assert_called_once()
    jobs.add_job_dependency.assert_called_once_with(job.id, 700)
    jobs.requeue_job_at_stage.assert_called_once_with(job.id, Stage.DEPLOY)


def test_dependency_blocked_requeues_when_deps_landed():
    jobs = MagicMock()
    jobs.get_unsatisfied_deps.return_value = []  # all deps DONE now
    jobs.supervisor_requeue_count.return_value = 0
    job = _job(failure="blocked: dependency #570 failed and can no longer complete")

    asyncio.run(remediate_dependency_blocked(jobs, job, MagicMock(), AsyncMock()))

    jobs.requeue_job.assert_called_once_with(job.id, zero_attempts=True)


def test_dependency_blocked_left_alone_while_dep_unsatisfied():
    jobs = MagicMock()
    jobs.get_unsatisfied_deps.return_value = [570]
    job = _job(failure="blocked: dependency #570 failed and can no longer complete")

    asyncio.run(remediate_dependency_blocked(jobs, job, MagicMock(), AsyncMock()))

    jobs.requeue_job.assert_not_called()


def test_dependency_blocked_left_terminal_when_manually_resolved():
    jobs = MagicMock()
    job = _job(
        failure="blocked: dependency #570 failed and can no longer complete",
        resolution="resolved",
    )

    asyncio.run(remediate_dependency_blocked(jobs, job, MagicMock(), AsyncMock()))

    jobs.get_unsatisfied_deps.assert_not_called()
    jobs.requeue_job.assert_not_called()


def test_dependency_blocked_returns_to_pending_when_original_blocker_resolved():
    jobs = MagicMock()
    jobs.get_unsatisfied_deps.return_value = [999]  # a different, still-pending dependency
    jobs.dependency_block_still_applies.return_value = False
    jobs.supervisor_requeue_count.return_value = 0
    job = _job(failure_code="dependency_blocked", failure_detail={"dependency_id": 570})

    asyncio.run(remediate_dependency_blocked(jobs, job, MagicMock(), AsyncMock()))

    jobs.requeue_job.assert_called_once_with(job.id, zero_attempts=True)


def test_dependency_blocked_dead_letters_at_cap():
    jobs = MagicMock()
    jobs.get_unsatisfied_deps.return_value = []
    jobs.supervisor_requeue_count.return_value = 5
    jobs.is_supervisor_notified.return_value = False
    notify = AsyncMock()
    job = _job(failure="blocked: dependency #570 failed and can no longer complete")

    asyncio.run(remediate_dependency_blocked(jobs, job, MagicMock(), notify))

    jobs.requeue_job.assert_not_called()
    notify.assert_awaited()


def test_dependency_blocked_exhausted_routes_to_analyst_instead_of_dead_letter():
    jobs = MagicMock()
    jobs.get_unsatisfied_deps.return_value = []
    jobs.supervisor_requeue_count.return_value = 5
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.create.return_value = MagicMock(id=605)
    notify = AsyncMock()
    job = _job(failure="blocked: dependency #570 failed and can no longer complete")
    config = Config(model="sonnet", permission_mode="acceptEdits")

    diagnosis = (
        {
            "diagnosis": "dependency chain masked an actual code bug",
            "confidence": 0.9,
            "action": {"type": "file_fix_job", "idea": "x" * 80},
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        asyncio.run(remediate_dependency_blocked(jobs, job, config, notify, dead_letter_cap=5))

    jobs.requeue_job.assert_called_once_with(job.id, zero_attempts=True)
    dead_letter_calls = [
        call
        for call in jobs.record_supervisor_event.call_args_list
        if call.args[1] == "dead_lettered"
    ]
    assert dead_letter_calls == []


def test_dependency_blocked_below_cap_never_consults_analyst():
    jobs = MagicMock()
    jobs.get_unsatisfied_deps.return_value = []
    jobs.supervisor_requeue_count.return_value = 0
    job = _job(failure="blocked: dependency #570 failed and can no longer complete")
    config = Config(model="sonnet", permission_mode="acceptEdits")

    with patch(
        "hyqs.pipeline.supervisor._consult_analyst_before_dead_letter", new_callable=AsyncMock
    ) as consult:
        asyncio.run(remediate_dependency_blocked(jobs, job, config, AsyncMock(), dead_letter_cap=5))

    consult.assert_not_awaited()
    jobs.requeue_job.assert_called_once_with(job.id, zero_attempts=True)


def test_dependency_blocked_dead_letter_consult_prompt_includes_class_and_exhaustion():
    jobs = MagicMock()
    jobs.get_unsatisfied_deps.return_value = []
    jobs.supervisor_requeue_count.return_value = 5
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    job = _job(failure="blocked: dependency #570 failed and can no longer complete")
    config = Config(model="sonnet", permission_mode="acceptEdits")

    diagnosis = (
        {"diagnosis": "low confidence", "confidence": 0.1, "action": {"type": "escalate"}},
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ) as mock_diagnose,
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        asyncio.run(remediate_dependency_blocked(jobs, job, config, AsyncMock(), dead_letter_cap=5))

    failure_class_arg = mock_diagnose.call_args.args[2]
    assert "dependency_blocked" in failure_class_arg
    assert "exhausted" in failure_class_arg
    assert "5/5" in failure_class_arg


def test_dependency_blocked_declined_diagnosis_still_dead_letters():
    jobs = MagicMock()
    jobs.get_unsatisfied_deps.return_value = []
    jobs.supervisor_requeue_count.return_value = 5
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.is_supervisor_notified.return_value = False
    job = _job(failure="blocked: dependency #570 failed and can no longer complete")
    config = Config(model="sonnet", permission_mode="acceptEdits")

    diagnosis = (
        {"diagnosis": "unclear cause", "confidence": 0.1, "action": {"type": "escalate"}},
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        asyncio.run(remediate_dependency_blocked(jobs, job, config, AsyncMock(), dead_letter_cap=5))

    jobs.requeue_job.assert_not_called()
    jobs.create.assert_not_called()
    dead_letter_calls = [
        call
        for call in jobs.record_supervisor_event.call_args_list
        if call.args[1] == "dead_lettered"
    ]
    assert len(dead_letter_calls) == 1


def test_dependency_blocked_diagnosis_job_cap_still_bounds_predeadletter_path():
    jobs = MagicMock()
    jobs.get_unsatisfied_deps.return_value = []
    jobs.supervisor_requeue_count.return_value = 5
    jobs.is_supervisor_notified.return_value = False
    jobs.count_supervisor_events.return_value = 6  # already at the per-job diagnosis cap
    job = _job(failure="blocked: dependency #570 failed and can no longer complete")
    config = Config(
        model="sonnet", permission_mode="acceptEdits", pipeline_incident_analyst_job_cap=6
    )

    with patch("hyqs.pipeline.providers.build_backend") as mock_build_backend:
        asyncio.run(remediate_dependency_blocked(jobs, job, config, AsyncMock(), dead_letter_cap=5))

    mock_build_backend.assert_not_called()
    dead_letter_calls = [
        call
        for call in jobs.record_supervisor_event.call_args_list
        if call.args[1] == "dead_lettered"
    ]
    assert len(dead_letter_calls) == 1


def test_stale_branch_exhausted_routes_to_analyst_instead_of_dead_letter():
    jobs = MagicMock()
    jobs.supervisor_requeue_count.return_value = 5
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    jobs.has_ai_fix_job.return_value = False
    jobs.get_active_remediation.return_value = None
    jobs.create.return_value = MagicMock(id=605)
    notify = AsyncMock()
    job = _job()
    config = Config(model="sonnet", permission_mode="acceptEdits")

    diagnosis = (
        {
            "diagnosis": "branch cleanup wasn't the real issue",
            "confidence": 0.9,
            "action": {"type": "file_fix_job", "idea": "x" * 80},
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        asyncio.run(remediate_stale_branch(jobs, job, config, dead_letter_cap=5, notify=notify))

    jobs.requeue_job.assert_called_once_with(job.id, zero_attempts=True)
    dead_letter_calls = [
        call
        for call in jobs.record_supervisor_event.call_args_list
        if call.args[1] == "dead_lettered"
    ]
    assert dead_letter_calls == []


def test_remediate_transient_dead_letters_without_notify_when_notify_none():
    """The two out-of-scope call sites that omit ``notify`` must keep working:
    the pre-dead-letter analyst consult is skipped whenever notify is None."""
    jobs = MagicMock()
    jobs.supervisor_requeue_count.return_value = 5
    job = _job()
    config = Config(model="sonnet", permission_mode="acceptEdits")

    asyncio.run(remediate_transient(jobs, job, config, dead_letter_cap=5))

    jobs.requeue_job_at_stage.assert_not_called()
    dead_letter_calls = [
        call
        for call in jobs.record_supervisor_event.call_args_list
        if call.args[1] == "dead_lettered"
    ]
    assert len(dead_letter_calls) == 1


def test_analyst_diagnosis_uses_configured_model_not_hardcoded():
    """The analyst backend must read config.pipeline_incident_analyst_model, not a
    hardcoded literal — proves the model choice is a declared setting."""
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    job = _job()
    config = Config(
        model="sonnet",
        permission_mode="acceptEdits",
        pipeline_incident_analyst_model="sentinel-analyst-model-xyz",
    )

    diagnosis = (
        {"diagnosis": "low confidence", "confidence": 0.1, "action": {"type": "escalate"}},
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend") as mock_build_backend,
    ):
        asyncio.run(_ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, config, AsyncMock()))

    mock_build_backend.assert_called_once_with(
        "claude", "sentinel-analyst-model-xyz", config=config
    )


def test_analyst_diagnosis_falls_back_to_codex_when_claude_paused():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = {"claude"}
    jobs.list_events.return_value = []
    job = _job()
    config = Config(
        model="sonnet", permission_mode="acceptEdits", codex_model="sentinel-codex-model"
    )
    diagnosis = (
        {"diagnosis": "low confidence", "confidence": 0.1, "action": {"type": "escalate"}},
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend") as mock_build_backend,
    ):
        asyncio.run(_ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, config, AsyncMock()))

    mock_build_backend.assert_called_once_with("codex", "sentinel-codex-model", config=config)


def test_analyst_diagnosis_records_deferral_when_every_provider_paused():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = {"claude", "codex"}
    job = _job()

    ran = asyncio.run(
        _ai_diagnose_and_act(
            jobs,
            job,
            FailureClass.genuine_code,
            Config(model="sonnet", permission_mode="acceptEdits"),
            AsyncMock(),
        )
    )

    assert not ran
    jobs.record_supervisor_event.assert_called_once()
    assert jobs.record_supervisor_event.call_args.args[1] == "analyst_deferred"


def test_config_from_env_defaults_incident_analyst_model_to_sonnet(monkeypatch):
    monkeypatch.delenv("HYQS_PIPELINE_INCIDENT_ANALYST_MODEL", raising=False)

    config = Config.from_env()

    assert config.pipeline_incident_analyst_model == "sonnet"


def test_config_from_env_incident_analyst_model_overridable(monkeypatch):
    monkeypatch.setenv("HYQS_PIPELINE_INCIDENT_ANALYST_MODEL", "opus")

    config = Config.from_env()

    assert config.pipeline_incident_analyst_model == "opus"


def test_config_from_env_incident_analyst_provider_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("HYQS_PIPELINE_INCIDENT_ANALYST_PROVIDER", raising=False)

    assert Config.from_env().pipeline_incident_analyst_provider == "auto"


def test_config_from_env_incident_analyst_provider_is_overridable(monkeypatch):
    monkeypatch.setenv("HYQS_PIPELINE_INCIDENT_ANALYST_PROVIDER", "codex")

    assert Config.from_env().pipeline_incident_analyst_provider == "codex"


def test_config_from_env_defaults_incident_analyst_scan_budget(monkeypatch):
    monkeypatch.delenv("HYQS_PIPELINE_INCIDENT_ANALYST_SCAN_BUDGET", raising=False)

    config = Config.from_env()

    assert config.pipeline_incident_analyst_scan_budget == 8


def test_config_from_env_incident_analyst_scan_budget_overridable(monkeypatch):
    monkeypatch.setenv("HYQS_PIPELINE_INCIDENT_ANALYST_SCAN_BUDGET", "20")

    config = Config.from_env()

    assert config.pipeline_incident_analyst_scan_budget == 20


def test_config_from_env_defaults_incident_analyst_job_cap(monkeypatch):
    monkeypatch.delenv("HYQS_PIPELINE_INCIDENT_ANALYST_JOB_CAP", raising=False)

    config = Config.from_env()

    assert config.pipeline_incident_analyst_job_cap == 6


def test_config_from_env_incident_analyst_job_cap_overridable(monkeypatch):
    monkeypatch.setenv("HYQS_PIPELINE_INCIDENT_ANALYST_JOB_CAP", "15")

    config = Config.from_env()

    assert config.pipeline_incident_analyst_job_cap == 15


def test_config_from_env_defaults_incident_analyst_predeadletter_enabled(monkeypatch):
    monkeypatch.delenv("HYQS_PIPELINE_INCIDENT_ANALYST_PREDEADLETTER_DISABLED", raising=False)

    config = Config.from_env()

    assert config.pipeline_incident_analyst_predeadletter is True


def test_config_from_env_incident_analyst_predeadletter_disabled_by_env(monkeypatch):
    monkeypatch.setenv("HYQS_PIPELINE_INCIDENT_ANALYST_PREDEADLETTER_DISABLED", "1")

    config = Config.from_env()

    assert config.pipeline_incident_analyst_predeadletter is False


def test_analyst_skipped_when_diagnosis_cap_reached():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 3
    job = _job()
    config = Config(
        model="sonnet", permission_mode="acceptEdits", pipeline_incident_analyst_job_cap=3
    )

    with patch("hyqs.pipeline.providers.build_backend") as mock_build_backend:
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, config, AsyncMock())
        )

    assert ran is False
    mock_build_backend.assert_not_called()


def test_analyst_runs_below_configured_job_cap():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 2
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    job = _job()
    config = Config(
        model="sonnet", permission_mode="acceptEdits", pipeline_incident_analyst_job_cap=3
    )

    diagnosis = (
        {"diagnosis": "low confidence", "confidence": 0.1, "action": {"type": "escalate"}},
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend") as mock_build_backend,
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, config, AsyncMock())
        )

    assert ran is True
    mock_build_backend.assert_called_once()


def test_janitor_scan_stops_ai_diagnoses_after_scan_budget_exhausted():
    jobs = MagicMock()
    jobs.get_failed_jobs.return_value = [_job(id=i) for i in range(1, 5)]
    jobs.get_active_job_ids.return_value = set()
    jobs.is_supervisor_notified.return_value = True
    notify = AsyncMock()
    config = Config(
        model="sonnet", permission_mode="acceptEdits", pipeline_incident_analyst_scan_budget=2
    )

    with (
        patch("hyqs.pipeline.supervisor._repoint_split_orphans", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._check_job_staleness", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._check_dependency_cycles", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._reconcile_blocked_dependents", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._sweep_deploying_jobs", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor.gitops.gc_orphaned_artifacts", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._docker_sweep", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._docker_builder_prune", new=AsyncMock()),
        patch("hyqs.pipeline.supervisor._docker_image_prune", new=AsyncMock()),
        patch(
            "hyqs.pipeline.supervisor.classify_failure",
            return_value=FailureClass.genuine_code,
        ),
        patch(
            "hyqs.pipeline.supervisor._ai_diagnose_and_act",
            new=AsyncMock(return_value=True),
        ) as ai_diagnose,
    ):
        asyncio.run(_janitor_scan(jobs, config, notify))

    assert ai_diagnose.await_count == 2


def test_analyst_skipped_when_all_providers_paused():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = {"claude", "codex"}
    job = _job()

    with patch("hyqs.pipeline.providers.build_backend") as mock_build_backend:
        ran = asyncio.run(
            _ai_diagnose_and_act(
                jobs,
                job,
                FailureClass.genuine_code,
                Config(model="sonnet", permission_mode="acceptEdits"),
                AsyncMock(),
            )
        )

    assert ran is False
    mock_build_backend.assert_not_called()


def test_analyst_low_confidence_does_not_execute_action():
    jobs = MagicMock()
    jobs.count_supervisor_events.return_value = 0
    jobs.paused_providers.return_value = set()
    jobs.list_events.return_value = []
    notify = AsyncMock()
    job = _job()

    diagnosis = (
        {
            "diagnosis": "unclear cause",
            "confidence": 0.4,
            "action": {"type": "file_fix_job", "idea": "x" * 80},
        },
        MagicMock(),
    )
    with (
        patch(
            "hyqs.pipeline.incident_analyst.diagnose",
            new_callable=AsyncMock,
            return_value=diagnosis,
        ),
        patch("hyqs.pipeline.providers.build_backend"),
    ):
        ran = asyncio.run(
            _ai_diagnose_and_act(jobs, job, FailureClass.genuine_code, MagicMock(), notify)
        )

    assert ran
    jobs.create.assert_not_called()
    jobs.requeue_job.assert_not_called()
    notify.assert_awaited()
    notify_message = notify.call_args.args[1]
    assert "escalate (no automated action)" in notify_message


def test_fresh_requeue_clears_stale_failure_text(store):
    """zero_attempts requeue must clear failure — a stale 'blocked: dependency #N'
    prefix would misclassify the job's next unrelated failure as dependency_blocked."""
    from hyqs.pipeline.models import _now

    now = _now()
    with store._pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO jobs(idea, title, repo_path, chat_id, stage, status, failure, "
            "created_at, updated_at) VALUES ('t','t',%s,0,'queued','failed',"
            "'blocked: dependency #1 failed', %s, %s) RETURNING id",
            (f"/tmp/requeue-test-{uuid.uuid4()}", now, now),
        ).fetchone()
        jid = int(row["id"])
    try:
        store.requeue_job(jid, zero_attempts=True)
        j = store.get(jid)
        assert j.failure == ""
        assert j.status == JobStatus.PENDING
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id=%s", (jid,))
