"""Review stage: AI review of the diff against the plan + reuse contract.

Stage handler for TEST → REVIEW. Extracted from PipelineRunner._advance; logic unchanged.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .. import agents, decisions, github, gitops, personas, resources
from ..classify import classify_diff
from ..gate_guard import GateIsolationError, run_guarded_gate
from ..models import JobStatus, Stage, _now

if TYPE_CHECKING:
    from ..models import Job
    from ..runner import PipelineRunner

log = logging.getLogger("hyqs.runner")

_FRONTEND_ROOTS = {"static", "frontend", "templates", "src"}
_FRONTEND_EXTS = {".ts", ".tsx", ".vue", ".jsx", ".js", ".html", ".css"}
_BACKEND_SURFACE_PATTERNS = {
    "router",
    "routes",
    "endpoints",
    "views",
    "api",
    "models",
    "schemas",
    "serializers",
}


def _check_ui_impact(plan_data: dict, changed_files: list[str], worktree: Path) -> str:
    """Return a warning string when a backend surface changed with no frontend change and no reason.

    Returns '' in all other cases and on any exception (fail-open).
    """
    try:
        has_frontend_root = any((worktree / root).is_dir() for root in _FRONTEND_ROOTS) or any(
            f.suffix.lower() in {".ts", ".tsx", ".vue", ".jsx", ".html"}
            for f in worktree.iterdir()
            if f.is_file()
        )
        if not has_frontend_root:
            return ""

        def _is_backend_surface(path: str) -> bool:
            parts = [p.lower() for p in Path(path).parts]
            return any(pattern in part for part in parts for pattern in _BACKEND_SURFACE_PATTERNS)

        if not any(_is_backend_surface(f) for f in changed_files):
            return ""

        def _is_frontend_file(path: str) -> bool:
            p = Path(path)
            if p.suffix.lower() in _FRONTEND_EXTS:
                return True
            return any(part.lower() in _FRONTEND_ROOTS for part in p.parts)

        if any(_is_frontend_file(f) for f in changed_files):
            return ""

        reason = plan_data.get("ui_impact", {}).get("no_ui_change_reason", "")
        if reason:
            return ""

        return (
            "Backend surface changed (routes/models/schemas) with no frontend change "
            "and no stated reason (ui_impact.no_ui_change_reason). "
            "Verify the UI does not need updating."
        )
    except Exception:
        return ""


async def run(rn: "PipelineRunner", job: "Job") -> None:
    worktree = rn.worktrees / f"job-{job.id}"
    backend = rn._backend(job)
    started = _now()
    managed = await rn._managed_repo(job)
    base = await gitops.default_branch(managed)
    base = await gitops.fresh_base(worktree, base)
    review_symbol_context = ""
    if job.project_id is not None:
        try:
            review_symbol_context = rn.store.get_symbol_index_text(job.project_id)
        except Exception:
            log.warning(
                "symbol index get failed for review of job %s; continuing",
                job.id,
                exc_info=True,
            )

    def _review_sink(line):
        rn.store.append_log(job.id, "review", line, job.attempts)

    decision_digest = ""
    try:
        decision_digest = decisions.load_digest(str(worktree))
    except Exception:
        log.warning(
            "decision digest load failed for review of job %s; continuing",
            job.id,
            exc_info=True,
        )

    try:
        # job #1938: repeated out-of-lane false positives were suspected to come
        # from this triple-dot diff misattributing files from a main commit that
        # landed after a gitops.merge_base_into_worktree cycle (the MERGE stage's
        # in-lock loop / conflict recovery). tests/test_scope_gate_merge_staleness.py
        # drives this exact fresh_base + numstat("...") call path across single-
        # and multi-cycle merge histories and does NOT reproduce a leak — main's
        # merge-base always lands on the job's own last-merged-in commit, so a
        # later main advance is correctly excluded. Don't re-walk this path when
        # triaging a future false positive; look at the job's real git history.
        numstat_rows = await gitops.numstat(worktree, f"{base}...HEAD")
        changed_files = [row["path"] for row in numstat_rows]
    except Exception:
        changed_files = []
    conventions_changed = "CONVENTIONS.md" in changed_files

    try:
        persona_checklist = personas.build_checklist(classify_diff(changed_files))
    except Exception:
        persona_checklist = ""

    # job.review still holds the PRIOR attempt's review here (it's overwritten
    # below). Feed it back so a re-review converges on the issues it already
    # raised instead of surfacing fresh nitpicks each pass.
    prior_review = job.review if isinstance(job.review, dict) else None
    is_fix_job = bool(
        job.source_meta and (job.source_meta.get("ai_fix_for") or job.source_meta.get("fix_for"))
    )
    _review_t0 = time.monotonic()
    try:
        review_data, usage = await run_guarded_gate(
            "review",
            agents.review,
            str(worktree),
            backend,
            str(worktree),
            base,
            timeout=rn.timeout,
            symbol_context=review_symbol_context,
            plan_data=job.plan,
            prior_review=prior_review,
            decision_digest=decision_digest,
            persona_checklist=persona_checklist,
            idea=job.idea,
            title=job.title,
            conventions_changed=conventions_changed,
            is_fix_job=is_fix_job,
            log_sink=_review_sink,
            store=rn.store,
            job_id=job.id,
        )
    except GateIsolationError as e:
        return await rn._retry_or_fail(job, str(e))
    rn.store.record_usage("review", usage, job.id)
    # cgroup fields null: AI work runs on provider, only local wall-time is captured
    rn._record_resource(
        job,
        "review",
        resources.ResourceRecord(
            cpu_seconds=None,
            peak_rss_bytes=None,
            io_read_bytes=None,
            io_write_bytes=None,
            wall_seconds=time.monotonic() - _review_t0,
            sampled_at=resources._now_iso(),
        ),
    )
    try:
        warning_msg = _check_ui_impact(job.plan or {}, changed_files, worktree)
        if warning_msg:
            rn.store.add_event(job.id, "review", "warning", summary=warning_msg)
            rn.store.append_log(job.id, "review", f"[ui-impact] {warning_msg}", job.attempts)
    except Exception:
        pass
    job.review = review_data
    verdict = review_data.get("verdict")
    detail = {"verdict": verdict, "findings": review_data.get("findings", [])}
    if await github.has_remote(managed) and job.branch:
        icon = "✅ pass" if verdict == "pass" else "❌ changes requested"
        findings_md = "\n".join(
            f"- **{f.get('severity', '?')}**: {f.get('note', '')}"
            for f in review_data.get("findings", [])
        )
        comment = f"### 🔎 Hyqs review — {icon}\n\n{review_data.get('summary', '')}\n\n{findings_md}".strip()
        await github.pr_comment(worktree, job.branch, comment)
    if verdict != "pass":
        rn._event(
            job,
            "review",
            "failed",
            started,
            summary=review_data.get("summary", ""),
            detail=detail,
            usage=usage,
        )
        findings = "\n".join(
            f"- [{f.get('severity', '?')}] {f.get('note', '')}"
            for f in review_data.get("findings", [])
        )
        return await rn._retry_or_fail(
            job, f"review rejected: {review_data.get('summary', '')}\n{findings}".strip()
        )
    rn._event(
        job,
        "review",
        "done",
        started,
        summary=review_data.get("summary", ""),
        detail=detail,
        usage=usage,
    )
    job.stage = Stage.REVIEW
    job.status = JobStatus.PENDING
    rn.store.save(job)
    await rn.notify(
        job.chat_id,
        f"🔎 Job #{job.id}: review passed. Running security check…",
        project_id=job.project_id,
        job_id=job.id,
    )
    return
