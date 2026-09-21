"""Build stage: implement the plan in an isolated worktree, push the branch, open a PR.

Stage handler for PLAN → LINT. Extracted from PipelineRunner._advance; logic unchanged.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .. import agents, github, gitops, resources
from ..models import JobSource, JobStatus, Stage, _now
from ._common import _is_non_fast_forward

if TYPE_CHECKING:
    from ..models import Job
    from ..runner import PipelineRunner

log = logging.getLogger("hyqs.runner")

# Runtime droppings that are never an agent's work product — excluded from the
# isolation-leak diff (a '.db-journal' flickering during the build window killed
# three legit jobs as false-positive isolation leaks).
_TRANSIENT_SUFFIXES = ("-journal", "-wal", "-shm", ".tmp", ".pyc", ".swp", ".lock")


def _filter_transient_paths(paths: set[str]) -> set[str]:
    return {p for p in paths if not p.endswith(_TRANSIENT_SUFFIXES) and "__pycache__" not in p}


async def run(rn: "PipelineRunner", job: "Job") -> None:
    worktree = rn.worktrees / f"job-{job.id}"
    backend = rn._backend(job)
    started = _now()
    managed = await rn._managed_repo(job)
    job.branch = f"hyqs/job-{job.id}"
    if worktree.exists():
        await gitops.remove_worktree(managed, worktree)
    base = await gitops.default_branch(managed)
    base = await gitops.fresh_base(managed, base)
    res = await gitops.create_worktree(managed, job.branch, worktree, base=base)
    if not res.ok:
        rn._event(
            job,
            "build",
            "failed",
            started,
            summary=f"worktree create failed: {res.stderr[:300]}",
        )
        return await rn._fail(job, f"worktree create failed: {res.stderr}")
    source_meta = job.source_meta if isinstance(job.source_meta, dict) else {}
    remediation_source = source_meta.get("remediation_source")
    if job.source == JobSource.SUPERVISOR and isinstance(remediation_source, dict):
        try:
            await gitops.seed_remediation_worktree(
                managed,
                worktree,
                base=base,
                source_branch=str(remediation_source.get("branch") or ""),
                source_sha=str(remediation_source.get("sha") or ""),
            )
            rn.store.mark_remediation_source_captured(job.id)
            remediation_source["captured"] = True
        except (RuntimeError, ValueError) as exc:
            detail = {
                "source_branch": remediation_source.get("branch"),
                "source_sha": remediation_source.get("sha"),
                "reason": str(exc),
            }
            rn._event(
                job,
                "build",
                "failed",
                started,
                summary=f"remediation source seeding failed: {exc}",
                detail=detail,
            )
            return await rn._fail(
                job,
                f"remediation source seeding failed: {exc}",
                failure_code="remediation_source_invalid",
                failure_origin="deterministic_gate",
                retry_disposition="human_review",
                failure_detail=detail,
            )
    await gitops.verify_worktree_identity(worktree, job.branch)
    build_symbol_context = ""
    if job.project_id is not None:
        try:
            build_symbol_context = rn.store.get_symbol_index_text(job.project_id)
        except Exception:
            log.warning("symbol index get failed for job %s; continuing", job.id, exc_info=True)

    # Authoritative file map so the coder edits what exists instead of guessing
    # paths and thrashing (the exploration doom-loop; same fix as the plan stage).
    build_file_manifest = ""
    try:
        files = await gitops.tracked_files(str(worktree))
        if files:
            build_file_manifest = "\n".join(files)
    except Exception:
        log.warning("tracked_files failed for job %s; continuing", job.id, exc_info=True)

    def _build_sink(line):
        rn.store.append_log(job.id, "build", line, job.attempts)

    # Snapshot the shared checkout's pre-existing dirt so the isolation guard
    # below can scope to what THIS build newly wrote — a bare has_changes()
    # false-positives on unrelated pre-existing dirt (e.g. an untracked ops
    # script), failing every job on that checkout regardless of the agent.
    pre_dirty = await gitops.dirty_paths(Path(job.repo_path))

    retry_detail = job.failure_detail if isinstance(job.failure_detail, dict) else {}
    no_diff_retry = (
        job.failure_code == "no_diff_verification_failed"
        and job.retry_disposition == "retry_build"
        and retry_detail.get("retry_attempt") == 1
        and retry_detail.get("verification") == "rejected"
    )
    retry_evidence = str(retry_detail.get("evidence", "")) if no_diff_retry else ""

    _build_t0 = time.monotonic()
    summary, usage = await agents.build(
        backend,
        job.idea,
        job.plan or {},
        str(worktree),
        timeout=rn.timeout,
        symbol_context=build_symbol_context,
        file_manifest=build_file_manifest,
        log_sink=_build_sink,
        store=rn.store,
        job_id=job.id,
        no_diff_retry_evidence=retry_evidence,
    )
    await gitops.verify_worktree_identity(worktree, job.branch)
    rn.store.record_usage("build", usage, job.id)
    # cgroup fields null: AI work runs on provider, only local wall-time is captured
    rn._record_resource(
        job,
        "build",
        resources.ResourceRecord(
            cpu_seconds=None,
            peak_rss_bytes=None,
            io_read_bytes=None,
            io_write_bytes=None,
            wall_seconds=time.monotonic() - _build_t0,
            sampled_at=resources._now_iso(),
        ),
    )
    # S1: isolation leak detector — the build agent must not write to the shared
    # checkout. Scope to paths THIS build newly dirtied (post − pre); pre-existing
    # dirt is ignored so an unrelated untracked file doesn't fail every job.
    # Transient runtime droppings (SQLite journals, caches) are filtered too:
    # they flicker into existence when anything runs the app/tests near the
    # checkout during the build window, and are never something an agent "wrote".
    leaked = _filter_transient_paths(await gitops.dirty_paths(Path(job.repo_path)) - pre_dirty)
    if leaked:
        raise gitops.IsolationViolationError(
            f"Build agent wrote to shared checkout {job.repo_path!r} instead of "
            f"worktree {str(worktree)!r}: newly dirtied {sorted(leaked)}. "
            "Leaked changes have been preserved in the shared checkout for manual recovery."
        )
    committed, commit_record = await gitops.commit_all_measured(
        worktree, f"Job #{job.id}: {job.idea[:60]}"
    )
    if not committed:
        return await rn._verify_already_satisfied(
            job,
            started,
            managed=managed,
            worktree=worktree,
            base=base,
            backend=backend,
            no_diff_retry=no_diff_retry,
        )
    if no_diff_retry:
        job.failed_step = None
        job.failure_code = None
        job.failure_origin = None
        job.retry_disposition = None
        job.failure_detail = None
    patch = await gitops.patch(worktree, f"{base}...HEAD")
    files = await gitops.numstat(worktree, f"{base}...HEAD")
    detail = {
        "commit": await gitops.head_info(worktree),
        "files": files,
        "patch": patch[:80_000],
        "patch_truncated": len(patch) > 80_000,
    }
    # GitHub-native: push the branch and open a PR (the job IS a PR).
    gitops_record = commit_record
    pr_note = ""
    if await github.has_remote(managed):
        pushed, push_record = await gitops.push_branch_measured(worktree, job.branch)
        gitops_record = gitops_record + push_record
        if not pushed.ok and _is_non_fast_forward(pushed.stderr):
            # hyqs/job-N is a pipeline-owned branch; a divergent remote is a
            # stale leftover from a prior requeue. Overwrite it so GitHub
            # evaluates the branch we actually built — otherwise the PR stays
            # frozen on stale, conflicting commits (the divergence half of the
            # phantom-conflict loop: the local rebuild is clean but never
            # reaches GitHub because the non-force push is rejected).
            log.warning(
                "job %s: build push rejected (non-fast-forward); force-pushing stale pipeline branch %s",
                job.id,
                job.branch,
            )
            pushed, force_record = await gitops.push_branch_measured(
                worktree, job.branch, force=True
            )
            gitops_record = gitops_record + force_record
        if pushed.ok:
            body = f"Autonomous build by Hyqs for job #{job.id}.\n\n**Idea:** {job.idea}\n\n{(job.plan or {}).get('summary', '')}"
            await github.pr_create(
                worktree,
                job.branch,
                base,
                title=f"Job #{job.id}: {job.idea[:60]}",
                body=body,
            )
            detail["pr_url"] = await github.pr_url(worktree, job.branch)
            pr_note = f"\n{detail['pr_url']}" if detail.get("pr_url") else ""
        else:
            # Never advance to LINT as if pushed — a PR-less job cannot merge
            # and would only fail confusingly downstream. Fail loudly so the
            # janitor can classify (transient network errors get requeued).
            return await rn._fail(
                job, f"branch push failed and could not be recovered: {pushed.stderr[:300]}"
            )
    rn._record_resource(job, "gitops", gitops_record)
    rn._event(job, "build", "done", started, summary=summary, detail=detail, usage=usage)
    job.stage = Stage.LINT
    job.status = JobStatus.PENDING
    rn.store.save(job)
    await rn.notify(
        job.chat_id,
        f"🔨 Job #{job.id}: built & pushed. Linting…{pr_note}",
        project_id=job.project_id,
        job_id=job.id,
    )
    return
