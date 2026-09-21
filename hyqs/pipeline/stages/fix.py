"""Fix stage: self-heal the worktree from a failure report (ordinary fixer or conflict resolver).

Stage handler for FIX → LINT. Extracted from PipelineRunner._advance; logic unchanged.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .. import agents, github, gitops, resources
from ..models import JobStatus, Stage, _now
from .build import _filter_transient_paths

if TYPE_CHECKING:
    from ..models import Job
    from ..runner import PipelineRunner

log = logging.getLogger("hyqs.runner")


async def run(rn: "PipelineRunner", job: "Job") -> None:
    worktree = rn.worktrees / f"job-{job.id}"
    started = _now()
    managed = await rn._managed_repo(job)
    if not worktree.exists():
        return await rn._fail(
            job,
            "fix stage: worktree missing after post-merge cleanup",
            failure_code="worktree_missing",
            failure_origin="infrastructure",
            retry_disposition="same_step",
        )
    await gitops.verify_worktree_identity(worktree, job.branch)
    backend = rn._backend(job)
    pre_dirty = await gitops.dirty_paths(Path(job.repo_path))

    def _fix_sink(line):
        rn.store.append_log(job.id, "fix", line, job.attempts)

    _fix_t0 = time.monotonic()
    if job.failure.startswith("[merge-conflict]"):
        # Conflict resolution is already scoped by `git status` (the UU set), so no
        # file manifest is needed here.
        summary, usage = await agents.fix_conflict(
            backend,
            job.idea,
            job.failure,
            str(worktree),
            timeout=rn.timeout,
            log_sink=_fix_sink,
            store=rn.store,
            job_id=job.id,
        )
    else:
        # Authoritative file map so the fixer edits what exists instead of guessing
        # paths and thrashing (same exploration fix as the plan/build stages).
        fix_file_manifest = ""
        try:
            files = await gitops.tracked_files(str(worktree))
            if files:
                fix_file_manifest = "\n".join(files)
        except Exception:
            log.warning("tracked_files failed for job %s; continuing", job.id, exc_info=True)
        fix_failure_detail = job.failure_detail
        if isinstance(fix_failure_detail, dict) and "gate_history" in fix_failure_detail:
            fix_failure_detail = {
                key: value for key, value in fix_failure_detail.items() if key != "gate_history"
            }
        summary, usage = await agents.fix(
            backend,
            job.idea,
            job.failure,
            str(worktree),
            timeout=rn.timeout,
            failure_detail=fix_failure_detail,
            plan_data=job.plan,
            failed_step=job.failed_step or "",
            failure_code=job.failure_code or "",
            fix_attempt=job.attempts,
            file_manifest=fix_file_manifest,
            log_sink=_fix_sink,
            store=rn.store,
            job_id=job.id,
        )
    await gitops.verify_worktree_identity(worktree, job.branch)
    leaked = _filter_transient_paths(await gitops.dirty_paths(Path(job.repo_path)) - pre_dirty)
    if leaked:
        raise gitops.IsolationViolationError(
            f"Fix agent wrote to shared checkout {job.repo_path!r} instead of "
            f"worktree {str(worktree)!r}: newly dirtied {sorted(leaked)}."
        )
    rn.store.record_usage("fix", usage, job.id)
    # cgroup fields null: AI work runs on provider, only local wall-time is captured
    rn._record_resource(
        job,
        "fix",
        resources.ResourceRecord(
            cpu_seconds=None,
            peak_rss_bytes=None,
            io_read_bytes=None,
            io_write_bytes=None,
            wall_seconds=time.monotonic() - _fix_t0,
            sampled_at=resources._now_iso(),
        ),
    )
    commit_message = f"Job #{job.id}: fix attempt {job.attempts}"
    committed = await gitops.commit_all(worktree, commit_message)
    if not committed:
        rn._event(
            job,
            "fix",
            "failed",
            started,
            summary=f"fix attempt {job.attempts} produced no changes",
            usage=usage,
            attempt=job.attempts,
        )
        return await rn._fail(job, f"fix attempt {job.attempts} produced no changes")
    fix_patch = await gitops.patch(worktree, "HEAD~1..HEAD")
    pr_url = ""
    if await github.has_remote(managed):
        pushed = await github.force_push_branch(worktree, job.branch)  # update the PR
        if not pushed.ok:
            return await rn._fail(
                job,
                f"fixed branch push failed: {pushed.stderr[:300]}",
                failure_code="provider_transport_error",
                failure_origin="gitops",
                retry_disposition="same_step",
                failure_detail={"operation": "force_push", "branch": job.branch},
            )
    detail = {
        "commit": await gitops.head_info(worktree),
        "files": await gitops.numstat(worktree, "HEAD~1..HEAD"),
        "patch": fix_patch[:80_000],
        "patch_truncated": len(fix_patch) > 80_000,
        "trigger": job.failure[:3000],
    }
    if pr_url:
        detail["pr_url"] = pr_url
    rn._event(
        job,
        "fix",
        "done",
        started,
        summary=summary,
        detail=detail,
        usage=usage,
        attempt=job.attempts,
    )
    if job.failure.startswith("[merge-conflict]"):
        # Conflict resolution: skip the full pipeline re-run; go straight to
        # MERGE_VERIFY which re-tests and re-reviews only the merge delta.
        job.stage = Stage.MERGE_VERIFY
        job.status = JobStatus.PENDING
        rn.store.save(job)
        await rn.notify(
            job.chat_id,
            f"🔁 Job #{job.id}: conflict resolved. Re-verifying merge delta…",
            project_id=job.project_id,
            job_id=job.id,
        )
    else:
        job.stage = Stage.LINT
        job.status = JobStatus.PENDING
        rn.store.save(job)
        pr_note = f"\n{pr_url}" if pr_url else ""
        await rn.notify(
            job.chat_id,
            f"🔁 Job #{job.id}: fix applied. Re-linting…{pr_note}",
            project_id=job.project_id,
            job_id=job.id,
        )
    return
