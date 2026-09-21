"""Tests for deploy.sync_deploy_checkout's dirtiness check.

Real git repos in tmp_path, real subprocess calls — no Postgres, matching the
style of tests/test_gitops_fresh_base.py.
"""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyqs.pipeline.deploy import sync_deploy_checkout, web_swap_confirmed
from hyqs.pipeline.models import Job, JobStatus, Stage, Usage
from hyqs.pipeline.npm_audit import (
    AdvisoryEvidence,
    AuditScope,
    ClassifiedFinding,
    ComparisonStatus,
    NpmAuditReport,
    NpmProject,
)
from hyqs.pipeline.sast import SastFinding, SastScanResult
from hyqs.pipeline.stages import security as security_stage


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_origin_and_clone(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("config", "user.email", "test@example.com", cwd=origin)
    _git("config", "user.name", "Test", cwd=origin)
    (origin / "base.py").write_text("v1\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-q", "-m", "init", cwd=origin)

    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=clone)
    _git("config", "user.name", "Test", cwd=clone)
    return origin, clone


def test_sync_deploy_checkout_ignores_untracked_files(tmp_path):
    origin, clone = _make_origin_and_clone(tmp_path)

    # Another commit lands on origin after the clone, so there's something to
    # fast-forward to.
    (origin / "other.py").write_text("other\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-q", "-m", "advance origin", cwd=origin)

    # The pipeline's own untracked .env.secrets symlink, as created by
    # docker_deploy._ensure_env_secrets — must not block the sync.
    (clone / ".env.secrets").write_text("SECRET=1\n")

    asyncio.run(sync_deploy_checkout(clone, "main"))

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone, check=True, capture_output=True, text=True
    ).stdout.strip()
    origin_head = subprocess.run(
        ["git", "rev-parse", "main"], cwd=origin, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert head == origin_head
    assert (clone / ".env.secrets").exists()


def test_sync_deploy_checkout_blocks_on_modified_tracked_file(tmp_path):
    origin, clone = _make_origin_and_clone(tmp_path)

    (origin / "other.py").write_text("other\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-q", "-m", "advance origin", cwd=origin)

    (clone / "base.py").write_text("locally modified\n")

    with pytest.raises(RuntimeError, match="deploy checkout has uncommitted changes"):
        asyncio.run(sync_deploy_checkout(clone, "main"))

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone, check=True, capture_output=True, text=True
    ).stdout.strip()
    original_head = subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=origin, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert head == original_head


def test_sync_deploy_checkout_terminates_options_before_dash_prefixed_base(tmp_path):
    proc = AsyncMock()
    proc.communicate.return_value = (b"", b"")
    proc.returncode = 0

    with patch("hyqs.pipeline.deploy.asyncio.create_subprocess_exec", return_value=proc) as spawn:
        asyncio.run(sync_deploy_checkout(tmp_path, "-malicious"))

    argv = [call.args[3:] for call in spawn.call_args_list]
    assert ("checkout", "--end-of-options", "-malicious") in argv
    assert ("merge", "--ff-only", "--", "origin/-malicious") in argv


def test_web_swap_confirmed_true_for_release_sh_with_confirmation_marker():
    result = {
        "command": "bash deploy/release.sh",
        "output": (
            "hyqs-web@2.service (checked pids: 12345) is healthy (matching pid "
            "confirmed via http://127.0.0.1:8000/health)."
        ),
    }
    assert web_swap_confirmed(result) is True


def test_web_swap_confirmed_false_when_web_release_skipped():
    result = {
        "command": "bash deploy/release.sh",
        "output": "SKIP_WEB_RELEASE: all changed paths are limited to decisions, docs, or tests",
    }
    assert web_swap_confirmed(result) is False


def test_web_swap_confirmed_false_when_no_active_web_unit():
    result = {
        "command": "bash deploy/release.sh",
        "output": "no active hyqs-web* unit found; skipping web blue-green replacement",
    }
    assert web_swap_confirmed(result) is False


def test_web_swap_confirmed_false_for_pipeline_block_confirmation_only():
    result = {
        "command": "bash deploy/release.sh",
        "output": "hyqs-pipeline.service (checked pids: 12345) is healthy (fresh heartbeat confirmed).",
    }
    assert web_swap_confirmed(result) is False


def test_web_swap_confirmed_false_for_non_release_sh_command_even_if_text_matches():
    result = {
        "command": "docker compose up -d",
        "output": "app (checked pids: 12345) is healthy (matching pid confirmed via ...).",
    }
    assert web_swap_confirmed(result) is False


def _security_job() -> Job:
    return Job(
        id=2595,
        idea="security regression",
        repo_path="/fake/repo",
        chat_id=99,
        stage=Stage.REVIEW,
        status=JobStatus.RUNNING,
        project_id=7,
        attempts=0,
        branch="job-2595",
    )


def _security_runner(tmp_path) -> MagicMock:
    runner = MagicMock()
    runner.worktrees = tmp_path
    runner.timeout = 60
    runner.store = MagicMock()
    runner.notify = AsyncMock()
    runner._backend = MagicMock(return_value=MagicMock())
    runner._managed_repo = AsyncMock(return_value="/fake/repo")
    runner._event = MagicMock()
    runner._record_resource = MagicMock()
    runner._retry_or_fail = AsyncMock()
    return runner


def _run_security_stage(
    tmp_path,
    *,
    ai_verdict="pass",
    ai_findings=None,
    sast_findings=None,
    sast_error=None,
):
    job = _security_job()
    runner = _security_runner(tmp_path)
    (tmp_path / f"job-{job.id}").mkdir()
    ai_data = {
        "verdict": ai_verdict,
        "summary": f"AI {ai_verdict}",
        "findings": ai_findings or [],
    }
    scan = (
        AsyncMock(side_effect=sast_error)
        if sast_error is not None
        else AsyncMock(return_value=sast_findings or [])
    )
    ai_review = AsyncMock(return_value=(ai_data, Usage(input_tokens=1)))
    with (
        patch.object(security_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(security_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(
            security_stage.gitops,
            "numstat",
            AsyncMock(return_value=[{"path": "src/app.py"}]),
        ),
        patch.object(security_stage.sast, "run_sast_scan", scan),
        patch.object(security_stage, "run_guarded_gate", ai_review),
        patch.object(security_stage.github, "has_remote", AsyncMock(return_value=False)),
    ):
        asyncio.run(security_stage.run(runner, job))
    return runner, job, ai_review


def test_security_failure_detail_authorizes_only_canonical_sast_files():
    detail = {"verdict": "fail", "findings": []}
    sast_findings = [
        SastFinding("high", "one", "bandit", "./src/z.py", 1),
        SastFinding("high", "duplicate", "bandit", "src/z.py", 2),
        SastFinding("high", "two", "bandit", "src/a.py", 3),
        SastFinding("high", "ambiguous", "bandit", "../outside.py", 4),
        SastFinding("high", "malformed", "bandit", "", 5),
    ]
    ai_findings = [
        {"path": "src/ai.py", "note": "also mentions src/note.py"},
        {"path": ["src/not-a-string.py"], "summary": "src/summary.py"},
        {"note": "src/prose-only.py"},
    ]

    composed = security_stage.compose_security_failure_detail(detail, sast_findings, ai_findings)

    assert composed["authorized_gate_failure"] == {
        "gate": "security",
        "check_id": security_stage.SECURITY_CHECK_ID,
        "event_id": security_stage.SECURITY_CHECK_ID,
        "failing_paths": ["src/a.py", "src/z.py"],
        "categories": ["symbol_paths"],
    }


@pytest.mark.parametrize(
    ("ai_verdict", "sast_findings"),
    [
        ("pass", [SastFinding("high", "scanner finding", "bandit", "src/app.py", 4)]),
        ("fail", []),
    ],
)
def test_security_layers_independently_block_and_ai_always_runs(
    tmp_path, ai_verdict, sast_findings
):
    runner, job, ai_review = _run_security_stage(
        tmp_path,
        ai_verdict=ai_verdict,
        ai_findings=[{"severity": "high", "note": "AI finding", "path": "src/ai.py"}],
        sast_findings=sast_findings,
    )

    ai_review.assert_awaited_once()
    runner._retry_or_fail.assert_awaited_once()
    failed_event = runner._event.call_args
    retry_detail = runner._retry_or_fail.call_args.kwargs["failure_detail"]
    assert failed_event.args[2] == "failed"
    assert failed_event.kwargs["detail"] == retry_detail
    if ai_verdict == "pass":
        assert failed_event.kwargs["summary"] == (
            "Security gate failed: 1 deterministic scanner finding(s)."
        )
        assert job.security_review["ai_summary"] == "AI pass"
    assert "src/ai.py" not in retry_detail.get("authorized_gate_failure", {}).get(
        "failing_paths", []
    )


def test_security_scanner_exception_is_non_blocking_and_ai_controls_outcome(tmp_path):
    runner, job, ai_review = _run_security_stage(
        tmp_path,
        ai_verdict="pass",
        sast_error=RuntimeError("scanner crashed"),
    )

    ai_review.assert_awaited_once()
    runner._retry_or_fail.assert_not_awaited()
    assert job.stage == Stage.SECURITY


def test_security_pass_advances_and_notifies_with_job_identity(tmp_path):
    runner, job, ai_review = _run_security_stage(tmp_path)

    ai_review.assert_awaited_once()
    runner._retry_or_fail.assert_not_awaited()
    assert job.stage == Stage.SECURITY
    assert job.status == JobStatus.PENDING
    runner.store.save.assert_called_once_with(job)
    runner.notify.assert_awaited_once_with(
        job.chat_id,
        f"🔒 Job #{job.id}: security passed. Merging…",
        project_id=job.project_id,
        job_id=job.id,
    )


def test_security_npm_block_routes_exact_attributed_remediation_to_fix(tmp_path):
    project = NpmProject("web/package.json", "web/package-lock.json")
    evidence = AdvisoryEvidence(
        "GHSA-1234",
        "example",
        "high",
        "node_modules/example",
        AuditScope.DEVELOPMENT,
        "1.0.0",
        True,
        "1.0.1",
        project.manifest_path,
        project.lockfile_path,
        "example advisory",
        "Upgrade example to 1.0.1",
        ("npm audit --json", "npm ls example --all"),
    )
    npm_finding = ClassifiedFinding(ComparisonStatus.INTRODUCED, evidence, None, "introduced", True)
    scan = SastScanResult((), NpmAuditReport((project,), (npm_finding,)))

    runner, job, ai_review = _run_security_stage(
        tmp_path,
        ai_verdict="pass",
        sast_findings=scan,
    )

    ai_review.assert_awaited_once()
    runner._retry_or_fail.assert_awaited_once()
    detail = runner._retry_or_fail.call_args.kwargs["failure_detail"]
    assert detail["authorized_gate_failure"]["failing_paths"] == [
        "web/package-lock.json",
        "web/package.json",
    ]
    remediation = detail["remediation"][0]
    assert remediation["advisory_id"] == "GHSA-1234"
    assert remediation["package"] == "example"
    assert remediation["manifest_path"] == "web/package.json"
    assert remediation["lockfile_path"] == "web/package-lock.json"
    assert remediation["dependency_scope"] == "development"
    assert remediation["baseline_installed_version"] is None
    assert remediation["candidate_installed_version"] == "1.0.0"
    assert remediation["lowest_compatible_patched_version"] == "1.0.1"
    assert remediation["recommended_action"] == "Upgrade example to 1.0.1"
    assert remediation["verification_commands"] == [
        "npm audit --json",
        "npm ls example --all",
    ]
    assert job.security_review["findings"][-1]["comparison_status"] == "introduced"


def test_security_nonblocking_npm_baseline_and_removed_findings_remain_visible(tmp_path):
    project = NpmProject("web/package.json", "web/package-lock.json")
    evidence = AdvisoryEvidence(
        "GHSA-1234",
        "example",
        "high",
        "node_modules/example",
        AuditScope.PRODUCTION,
        "1.0.0",
        False,
        None,
        project.manifest_path,
        project.lockfile_path,
        "example advisory",
        "Replace example",
        ("npm audit --json",),
    )
    report = NpmAuditReport(
        (project,),
        (
            ClassifiedFinding(
                ComparisonStatus.UNCHANGED_BASELINE,
                evidence,
                evidence,
                "baseline debt",
                False,
            ),
            ClassifiedFinding(ComparisonStatus.REMOVED, None, evidence, "removed", False),
        ),
    )

    runner, job, _ = _run_security_stage(
        tmp_path,
        ai_verdict="pass",
        sast_findings=SastScanResult((), report),
    )

    runner._retry_or_fail.assert_not_awaited()
    runner.store.file_baseline_security_remediation.assert_called_once()
    call = runner.store.file_baseline_security_remediation.call_args
    assert call.args[0] == job.id
    assert [finding["advisory_id"] for finding in call.args[1]] == ["GHSA-1234"]
    assert call.args[2] == ["npm audit --json"]
    assert call.kwargs == {"blocking": False}
    assert [finding["comparison_status"] for finding in job.security_review["findings"]] == [
        "unchanged_baseline",
        "removed",
    ]


def test_security_blocking_baseline_uses_dependency_requeue_instead_of_fix(tmp_path):
    project = NpmProject("package.json", "package-lock.json")
    evidence = AdvisoryEvidence(
        "GHSA-Z",
        "production-package",
        "critical",
        "node_modules/production-package",
        AuditScope.PRODUCTION,
        "1.0.0",
        True,
        "2.0.0",
        project.manifest_path,
        project.lockfile_path,
        "critical production advisory",
        "Upgrade production-package",
        ("npm audit --omit=dev",),
    )
    finding = ClassifiedFinding(
        ComparisonStatus.UNCHANGED_BASELINE,
        evidence,
        evidence,
        "critical baseline policy",
        True,
    )
    remediation_job = MagicMock(id=7001)
    runner = _security_runner(tmp_path)
    runner.store.file_baseline_security_remediation.return_value = MagicMock(
        job=remediation_job,
        created=True,
        blocking_dependency_attached=True,
        advisory_ids=("GHSA-Z",),
    )
    job = _security_job()
    (tmp_path / f"job-{job.id}").mkdir()
    with (
        patch.object(security_stage.gitops, "default_branch", AsyncMock(return_value="main")),
        patch.object(security_stage.gitops, "fresh_base", AsyncMock(return_value="main")),
        patch.object(security_stage.gitops, "numstat", AsyncMock(return_value=[])),
        patch.object(
            security_stage.sast,
            "run_sast_scan",
            AsyncMock(return_value=SastScanResult((), NpmAuditReport((project,), (finding,)))),
        ),
        patch.object(
            security_stage,
            "run_guarded_gate",
            AsyncMock(
                return_value=(
                    {"verdict": "pass", "summary": "AI pass", "findings": []},
                    Usage(),
                )
            ),
        ),
        patch.object(security_stage.github, "has_remote", AsyncMock(return_value=False)),
    ):
        asyncio.run(security_stage.run(runner, job))

    runner.store.file_baseline_security_remediation.assert_called_once()
    assert runner.store.file_baseline_security_remediation.call_args.kwargs == {"blocking": True}
    runner._retry_or_fail.assert_not_awaited()
    assert runner._event.call_args.args[2] == "blocked"
    assert runner._event.call_args.kwargs["detail"]["baseline_remediation"]["job_id"] == 7001
