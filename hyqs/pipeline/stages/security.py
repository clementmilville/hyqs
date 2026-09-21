"""Security stage: AI security pass + deterministic SAST over the diff.

Both layers run on every security stage and both must pass. Either can block.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .. import agents, decisions, github, gitops, personas, resources, sast
from ..classify import classify_diff
from ..collision import normalize_scope_amendment_path
from ..gate_guard import GateIsolationError, run_guarded_gate
from ..models import JobStatus, Stage, _now
from ..npm_audit import AdvisoryEvidence, ClassifiedFinding, NpmAuditReport, NpmProject

if TYPE_CHECKING:
    from ..models import Job
    from ..runner import PipelineRunner

log = logging.getLogger("hyqs.runner")

SECURITY_CHECK_ID = "security.findings"


def compose_security_failure_detail(
    detail: dict[str, object],
    sast_findings: list[sast.SastFinding],
    ai_findings: list[dict[str, object]],
    npm_findings: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Attach bounded path authority from deterministic SAST finding files only."""
    from . import compose_authorized_gate_failure_detail_if_valid

    candidates = [finding.file for finding in sast_findings]
    for finding in npm_findings or []:
        if finding.get("blocks"):
            candidates.extend(
                path
                for path in (finding.get("manifest_path"), finding.get("lockfile_path"))
                if isinstance(path, str)
            )
    failing_paths = sorted(
        {
            normalized
            for candidate in candidates
            if isinstance(candidate, str)
            if (normalized := normalize_scope_amendment_path(candidate)) is not None
        }
    )
    return compose_authorized_gate_failure_detail_if_valid(
        {**detail, "remediation": [f for f in (npm_findings or []) if f.get("blocks")]},
        Stage.SECURITY,
        SECURITY_CHECK_ID,
        SECURITY_CHECK_ID,
        failing_paths,
        ["symbol_paths"],
    )


def _npm_finding_dict(finding: ClassifiedFinding, project: NpmProject | None) -> dict:
    candidate = finding.candidate
    baseline = finding.baseline
    evidence: AdvisoryEvidence | None = candidate or baseline
    manifest_path = (
        evidence.manifest_path if evidence else (project.manifest_path if project else "")
    )
    lockfile_path = (
        evidence.lockfile_path if evidence else (project.lockfile_path if project else "")
    )
    return {
        "tool": "npm-audit",
        "comparison_status": finding.status.value,
        "blocks": finding.blocks,
        "advisory_id": evidence.advisory_id if evidence else None,
        "package": evidence.package if evidence else None,
        "dependency_path": evidence.dependency_path if evidence else None,
        "dependency_scope": evidence.scope.value if evidence else "unknown",
        "manifest_path": manifest_path,
        "lockfile_path": lockfile_path,
        "baseline_installed_version": baseline.installed_version if baseline else None,
        "candidate_installed_version": candidate.installed_version if candidate else None,
        "baseline_severity": baseline.severity if baseline else None,
        "candidate_severity": candidate.severity if candidate else None,
        "fix_available": evidence.fix_available if evidence else None,
        "lowest_compatible_patched_version": (
            evidence.lowest_compatible_patched_version if evidence else None
        ),
        "reason": finding.reason,
        "recommended_action": (
            evidence.recommended_action
            if evidence
            else "Restore npm audit infrastructure and rerun the dependency comparison"
        ),
        "verification_commands": list(evidence.verification_commands)
        if evidence
        else [
            f"npm audit --json --package-lock-only --prefix {project_dir}"
            for project_dir in ([str(Path(manifest_path).parent)] if manifest_path else ["."])
        ],
        "severity": evidence.severity if evidence else "high",
        "note": (
            f"[npm-audit] {finding.status.value}: "
            f"{evidence.actionable_evidence if evidence else finding.reason}"
        ),
        "file": lockfile_path,
    }


def _npm_finding_dicts(report: NpmAuditReport) -> list[dict]:
    evidenced_projects = {
        (evidence.manifest_path, evidence.lockfile_path)
        for finding in report.findings
        if (evidence := finding.candidate or finding.baseline) is not None
    }
    indeterminate_projects = iter(
        project
        for project in report.projects
        if (project.manifest_path, project.lockfile_path) not in evidenced_projects
    )
    result = []
    for finding in report.findings:
        evidence = finding.candidate or finding.baseline
        project = (
            NpmProject(evidence.manifest_path, evidence.lockfile_path)
            if evidence
            else next(indeterminate_projects, None)
        )
        result.append(_npm_finding_dict(finding, project))
    return result


def _baseline_remediation_evidence(
    npm_findings: list[dict],
) -> tuple[list[dict], list[str]]:
    findings = sorted(
        (
            dict(finding)
            for finding in npm_findings
            if finding.get("comparison_status") == "unchanged_baseline"
            and isinstance(finding.get("advisory_id"), str)
            and finding["advisory_id"].strip()
        ),
        key=lambda finding: finding["advisory_id"],
    )
    commands = list(
        dict.fromkeys(
            command
            for finding in findings
            for command in finding.get("verification_commands", [])
            if isinstance(command, str) and command.strip()
        )
    )
    return findings, commands


async def run(rn: "PipelineRunner", job: "Job") -> None:
    worktree = rn.worktrees / f"job-{job.id}"
    backend = rn._backend(job)
    started = _now()
    managed = await rn._managed_repo(job)
    base = await gitops.default_branch(managed)
    base = await gitops.fresh_base(worktree, base)

    def _security_sink(line):
        rn.store.append_log(job.id, "security", line, job.attempts)

    # Resolve changed files for diff-scoped SAST
    try:
        numstat_entries = await gitops.numstat(worktree, f"{base}...HEAD")
        changed_files = [e["path"] for e in numstat_entries]
    except Exception as exc:
        log.warning("security: could not resolve changed files: %s", exc)
        changed_files = []

    # Layer 1: deterministic SAST scanners (diff-scoped)
    try:
        scan_result = await sast.run_sast_scan(worktree, base, changed_files)
    except Exception as exc:
        log.warning("security: sast scan raised unexpected exception: %s", exc)
        projects = sast.discover_npm_projects(worktree, changed_files)
        scan_result = sast.SastScanResult(
            (),
            NpmAuditReport(
                projects,
                tuple(
                    sast.ClassifiedFinding(
                        sast.ComparisonStatus.INDETERMINATE,
                        None,
                        None,
                        f"deterministic scan failed: {exc}",
                        True,
                    )
                    for _project in projects
                ),
            ),
        )
    # Transitional tolerance for callers/tests that still provide the old list.
    if isinstance(scan_result, list):
        scan_result = sast.SastScanResult(tuple(scan_result))
    sast_findings = list(scan_result.findings)
    npm_finding_dicts = _npm_finding_dicts(scan_result.npm_audit)
    baseline_findings, baseline_verification = _baseline_remediation_evidence(npm_finding_dicts)

    try:
        persona_checklist = personas.build_checklist(classify_diff(changed_files))
    except Exception:
        persona_checklist = ""

    decision_digest = ""
    try:
        decision_digest = decisions.load_digest(str(worktree))
    except Exception:
        log.warning(
            "decision digest load failed for security review of job %s; continuing",
            job.id,
            exc_info=True,
        )

    # Layer 2: AI security reviewer (always runs, regardless of SAST result)
    prior_security = job.security_review if isinstance(job.security_review, dict) else None
    _security_t0 = time.monotonic()
    try:
        security_data, usage = await run_guarded_gate(
            "security",
            agents.security,
            str(worktree),
            backend,
            str(worktree),
            base,
            timeout=rn.timeout,
            prior_security=prior_security,
            persona_checklist=persona_checklist,
            idea=job.idea,
            plan_data=job.plan,
            decision_digest=decision_digest,
            log_sink=_security_sink,
            store=rn.store,
            job_id=job.id,
        )
    except GateIsolationError as e:
        return await rn._retry_or_fail(job, str(e))
    rn.store.record_usage("security", usage, job.id)
    rn._record_resource(
        job,
        "security",
        resources.ResourceRecord(
            cpu_seconds=None,
            peak_rss_bytes=None,
            io_read_bytes=None,
            io_write_bytes=None,
            wall_seconds=time.monotonic() - _security_t0,
            sampled_at=resources._now_iso(),
        ),
    )

    # Merge SAST findings into security_data so the PR comment and event detail are complete
    ai_findings = security_data.get("findings", [])
    sast_finding_dicts = [
        {
            "severity": f.severity,
            "note": f"[{f.tool}] {f.note}" + (f" ({f.file}:{f.line})" if f.file else ""),
            "file": f.file,
        }
        for f in sast_findings
    ]
    all_findings = ai_findings + sast_finding_dicts + npm_finding_dicts
    security_data = dict(security_data)
    security_data["findings"] = all_findings

    ai_verdict = security_data.get("verdict")
    ai_summary = security_data.get("summary", "")

    # Either layer failing blocks the job
    scanner_blocked = bool(sast_findings) or scan_result.npm_audit.blocked
    verdict = "pass" if (ai_verdict == "pass" and not scanner_blocked) else "fail"
    security_data["verdict"] = verdict
    if verdict != "pass":
        failure_sources = []
        if ai_verdict != "pass":
            failure_sources.append("AI security review")
        if sast_findings:
            failure_sources.append(f"{len(sast_findings)} deterministic scanner finding(s)")
        blocking_npm_count = sum(bool(finding.get("blocks")) for finding in npm_finding_dicts)
        if blocking_npm_count:
            failure_sources.append(f"{blocking_npm_count} dependency finding(s)")
        security_data["ai_summary"] = ai_summary
        security_data["summary"] = "Security gate failed: " + ", ".join(failure_sources) + "."
    job.security_review = security_data

    detail = {"verdict": verdict, "findings": all_findings}
    if await github.has_remote(managed) and job.branch:
        icon = "✅ pass" if verdict == "pass" else "❌ changes requested"
        findings_md = "\n".join(
            f"- **{f.get('severity', '?')}**: {f.get('note', '')}" for f in all_findings
        )
        comment = f"### 🔒 Hyqs security — {icon}\n\n{security_data.get('summary', '')}\n\n{findings_md}".strip()
        await github.pr_comment(worktree, job.branch, comment)

    non_baseline_npm_blocked = any(
        finding.get("blocks") and finding.get("comparison_status") != "unchanged_baseline"
        for finding in npm_finding_dicts
    )
    baseline_result = None
    if baseline_findings:
        baseline_blocking = (
            any(finding.get("blocks") for finding in baseline_findings)
            and ai_verdict == "pass"
            and not sast_findings
            and not non_baseline_npm_blocked
        )
        baseline_result = rn.store.file_baseline_security_remediation(
            job.id,
            baseline_findings,
            baseline_verification,
            blocking=baseline_blocking,
        )
        detail["baseline_remediation"] = {
            "job_id": baseline_result.job.id,
            "created": baseline_result.created,
            "advisory_ids": list(baseline_result.advisory_ids),
            "blocking_dependency_attached": baseline_result.blocking_dependency_attached,
        }

    baseline_only_block = bool(baseline_result and baseline_result.blocking_dependency_attached)
    if baseline_only_block:
        rn._event(
            job,
            "security",
            "blocked",
            started,
            summary="Critical production baseline debt is awaiting remediation.",
            detail=detail,
            usage=usage,
        )
        return

    if verdict != "pass":
        detail = compose_security_failure_detail(
            detail, sast_findings, ai_findings, npm_finding_dicts
        )
        rn._event(
            job,
            "security",
            "failed",
            started,
            summary=security_data.get("summary", ""),
            detail=detail,
            usage=usage,
        )
        # Build actionable failure message listing scanner findings explicitly
        ai_findings_text = "\n".join(
            f"- [{f.get('severity', '?')}] {f.get('note', '')}" for f in ai_findings
        )
        sast_text = "\n".join(
            f"- [{f.severity}] [{f.tool}] {f.note}" + (f" ({f.file}:{f.line})" if f.file else "")
            for f in sast_findings
        )
        parts = []
        if ai_verdict != "pass":
            parts.append(f"AI security reviewer: {ai_summary}")
            if ai_findings_text:
                parts.append(ai_findings_text)
        if sast_text:
            parts.append(f"Deterministic scanner findings:\n{sast_text}")
        npm_text = "\n".join(
            f"- [{finding['comparison_status']}] {finding['reason']}"
            for finding in npm_finding_dicts
            if finding["blocks"]
        )
        if npm_text:
            parts.append(f"npm dependency comparison:\n{npm_text}")
        return await rn._retry_or_fail(
            job,
            ("security rejected:\n" + "\n\n".join(parts)).strip(),
            failure_detail=detail,
        )

    rn._event(
        job,
        "security",
        "done",
        started,
        summary=security_data.get("summary", ""),
        detail=detail,
        usage=usage,
    )
    job.stage = Stage.SECURITY
    job.status = JobStatus.PENDING
    rn.store.save(job)
    await rn.notify(
        job.chat_id,
        f"🔒 Job #{job.id}: security passed. Merging…",
        project_id=job.project_id,
        job_id=job.id,
    )
    return
