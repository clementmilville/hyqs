"""Merge-verify stage: lightweight re-verification after a merge-boundary event.

Runs TEST + (conditionally) scoped REVIEW/SECURITY over the merge delta only,
then returns to MERGE.  Called after:
- a merge-boundary conflict was resolved by fix_conflict (merge_delta_sha set)
- clean base advances exhausted the in-lock budget (merge_delta_sha=None)

The invariant: never re-review the full job diff — only the delta introduced by
the merge (merge_delta_sha..HEAD).  If the delta is empty or non-code,
REVIEW/SECURITY are skipped entirely.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from .. import agents, gitops, resources, sast, testing
from ..gate_guard import GateIsolationError, run_guarded_gate
from ..models import JobStatus, Stage, _now

if TYPE_CHECKING:
    from ..models import Job
    from ..runner import PipelineRunner

log = logging.getLogger("hyqs.runner")

_CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".sh",
    }
)


def _has_code_changes(numstat_entries: list[dict]) -> bool:
    for entry in numstat_entries:
        _, ext = os.path.splitext(entry.get("path", ""))
        if ext in _CODE_EXTENSIONS:
            return True
    return False


async def run(rn: "PipelineRunner", job: "Job") -> None:
    worktree = rn.worktrees / f"job-{job.id}"
    started = _now()

    # Resolve the merge-delta boundary before validation so TEST can use the
    # same change-aware policy as the ordinary test stage. A missing or empty
    # boundary remains fail-safe and retains the complete-suite gate.
    merge_delta_sha = job.merge_delta_sha
    effective_base = merge_delta_sha
    numstat_entries: list[dict] = []
    if merge_delta_sha is not None:
        # Refresh the boundary so changes merged by unrelated jobs do not leak
        # into either test selection or the scoped review/security gates.
        try:
            managed = await rn._managed_repo(job)
            base = await gitops.default_branch(managed)
            base = await gitops.fresh_base(worktree, base)
            mb = await gitops.git(worktree, "merge-base", base, "HEAD")
            if mb.ok and mb.stdout.strip():
                effective_base = mb.stdout.strip()
                job.merge_delta_sha = effective_base
                rn.store.save(job)
            else:
                log.warning(
                    "merge_verify: merge-base lookup failed for job %s; using stored sha %s",
                    job.id,
                    merge_delta_sha,
                )
        except Exception as exc:
            log.warning(
                "merge_verify: could not refresh merge-base for job %s (%s); using stored sha",
                job.id,
                exc,
            )
        try:
            numstat_entries = await gitops.numstat(worktree, f"{effective_base}..HEAD")
        except Exception as exc:
            log.warning("merge_verify: numstat failed for job %s: %s", job.id, exc)

    changed_files = [entry["path"] for entry in numstat_entries] or None

    # Step 1: run tests selected for the known merge delta. Unknown or empty
    # deltas deliberately pass None and preserve the complete-suite fallback.
    async def _test_sink(line: str) -> None:
        rn.store.append_log(job.id, "merge-verify", line, job.attempts)

    result = await testing.run_tests(
        str(worktree), changed_files=changed_files, log_sink=_test_sink
    )
    rn._record_resource(job, "merge-verify", result.get("resource"))
    if not result.get("passed"):
        rn._event(
            job,
            "merge-verify",
            "test-failed",
            started,
            summary=f"{result.get('command')} — failed",
            detail={"command": result.get("command", ""), "output": result.get("output", "")},
        )
        if result.get("timed_out"):
            return await rn._timed_out(job, timeout_seconds=testing.TEST_TIMEOUT)
        return await rn._retry_or_fail(
            job,
            f"tests failed after merge ({result.get('command')}):\n{result.get('summary', '')}",
        )

    # Step 2: scoped REVIEW/SECURITY (only if merge_delta_sha is set and code changed)
    if merge_delta_sha is not None:
        if _has_code_changes(numstat_entries):
            backend = rn._backend(job)
            changed_files = [e["path"] for e in numstat_entries]

            # Scoped review
            def _review_sink(line: str) -> None:
                rn.store.append_log(job.id, "merge-verify", line, job.attempts)

            _review_t0 = time.monotonic()
            try:
                review_data, r_usage = await run_guarded_gate(
                    "review",
                    agents.review,
                    str(worktree),
                    backend,
                    str(worktree),
                    effective_base,
                    timeout=rn.timeout,
                    log_sink=_review_sink,
                )
            except GateIsolationError as e:
                return await rn._retry_or_fail(job, str(e))
            rn.store.record_usage("merge-verify-review", r_usage, job.id)
            rn._record_resource(
                job,
                "merge-verify-review",
                resources.ResourceRecord(
                    cpu_seconds=None,
                    peak_rss_bytes=None,
                    io_read_bytes=None,
                    io_write_bytes=None,
                    wall_seconds=time.monotonic() - _review_t0,
                    sampled_at=resources._now_iso(),
                ),
            )
            verdict = review_data.get("verdict")
            if verdict != "pass":
                rn._event(
                    job,
                    "merge-verify",
                    "review-failed",
                    started,
                    summary=review_data.get("summary", ""),
                    usage=r_usage,
                )
                findings = "\n".join(
                    f"- [{f.get('severity', '?')}] {f.get('note', '')}"
                    for f in review_data.get("findings", [])
                )
                return await rn._retry_or_fail(
                    job,
                    f"merge-delta review rejected: {review_data.get('summary', '')}\n{findings}".strip(),
                )

            # Scoped security
            def _security_sink(line: str) -> None:
                rn.store.append_log(job.id, "merge-verify", line, job.attempts)

            try:
                scan_result = await sast.run_sast_scan(worktree, effective_base, changed_files)
            except Exception as exc:
                log.warning(
                    "merge_verify: sast scan failed for job %s (treated as skip): %s",
                    job.id,
                    exc,
                )
                scan_result = sast.SastScanResult()
            if isinstance(scan_result, list):
                scan_result = sast.SastScanResult(tuple(scan_result))
            sast_findings = list(scan_result.findings)

            _sec_t0 = time.monotonic()
            try:
                security_data, s_usage = await run_guarded_gate(
                    "security",
                    agents.security,
                    str(worktree),
                    backend,
                    str(worktree),
                    effective_base,
                    timeout=rn.timeout,
                    log_sink=_security_sink,
                )
            except GateIsolationError as e:
                return await rn._retry_or_fail(job, str(e))
            rn.store.record_usage("merge-verify-security", s_usage, job.id)
            rn._record_resource(
                job,
                "merge-verify-security",
                resources.ResourceRecord(
                    cpu_seconds=None,
                    peak_rss_bytes=None,
                    io_read_bytes=None,
                    io_write_bytes=None,
                    wall_seconds=time.monotonic() - _sec_t0,
                    sampled_at=resources._now_iso(),
                ),
            )

            ai_verdict = security_data.get("verdict")
            scanner_blocked = bool(sast_findings) or scan_result.npm_audit.blocked
            sec_verdict = "pass" if (ai_verdict == "pass" and not scanner_blocked) else "fail"

            if sec_verdict != "pass":
                rn._event(
                    job,
                    "merge-verify",
                    "security-failed",
                    started,
                    summary=security_data.get("summary", ""),
                    usage=s_usage,
                )
                parts = []
                if ai_verdict != "pass":
                    parts.append(f"AI security reviewer: {security_data.get('summary', '')}")
                if sast_findings:
                    sast_text = "\n".join(
                        f"- [{f.severity}] [{f.tool}] {f.note}"
                        + (f" ({f.file}:{f.line})" if f.file else "")
                        for f in sast_findings
                    )
                    parts.append(f"Deterministic scanner findings:\n{sast_text}")
                npm_findings = [
                    finding for finding in scan_result.npm_audit.findings if finding.blocks
                ]
                if npm_findings:
                    npm_text = "\n".join(
                        f"- [{finding.status.value}] {finding.reason}" for finding in npm_findings
                    )
                    parts.append(f"npm dependency comparison:\n{npm_text}")
                return await rn._retry_or_fail(
                    job,
                    ("merge-delta security rejected:\n" + "\n\n".join(parts)).strip(),
                )

    # All checks passed — clear delta sha, return to MERGE (via Stage.SECURITY → merge.run)
    job.merge_delta_sha = None
    job.stage = Stage.SECURITY
    job.status = JobStatus.PENDING
    rn.store.save(job)
    rn._event(job, "merge-verify", "done", started, summary="merge-delta verification passed")
    await rn.notify(
        job.chat_id,
        f"✅ Job #{job.id}: merge delta verified. Merging…",
        project_id=job.project_id,
        job_id=job.id,
    )
