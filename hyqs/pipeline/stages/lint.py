"""Lint stage: lockfile sync + diff-scoped lint/format of the job's own changes.

Stage handler for LINT → BUILD. Extracted from PipelineRunner._advance; logic unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import github, gitops, linting, testing
from ..models import JobStatus, Stage, _now

if TYPE_CHECKING:
    from ..models import Job
    from ..runner import PipelineRunner

log = logging.getLogger("hyqs.runner")

_LOCKFILE_DRIFT_ID = "lint.lockfile-drift"
_LINT_DIAGNOSTICS_ID = "lint.diagnostics"


def _failure_detail(
    detail: dict[str, object], check_id: str, paths: list[str]
) -> dict[str, object]:
    from . import compose_authorized_gate_failure_detail_if_valid

    return compose_authorized_gate_failure_detail_if_valid(
        detail, Stage.LINT, check_id, check_id, paths, ["coverage_paths"]
    )


async def run(rn: "PipelineRunner", job: "Job") -> None:
    worktree = rn.worktrees / f"job-{job.id}"
    started = _now()
    managed = await rn._managed_repo(job)
    if not worktree.exists():
        return await rn._fail(job, "lint stage: worktree is missing")

    lf = await testing.check_lockfile_sync(str(worktree))
    if not lf.get("passed") and not lf.get("skipped"):
        lockfile_paths = [
            path for path in ("pyproject.toml", "uv.lock") if (worktree / path).is_file()
        ]
        detail = _failure_detail(
            {"command": lf.get("command", ""), "output": lf.get("output", "")},
            _LOCKFILE_DRIFT_ID,
            lockfile_paths,
        )
        rn._event(
            job,
            "lockfile-drift",
            "failed",
            started,
            summary=lf.get("summary", ""),
            detail=detail,
        )
        return await rn._retry_or_fail(
            job,
            f"[lockfile-drift] {lf.get('command', 'uv lock --check')} reported lockfile out of sync with its manifest.\n{lf.get('summary', '')}\nFix: run the lock command (e.g. `uv lock`) and commit the updated lockfile.",
            failure_detail=detail,
        )

    async def _lint_sink(line: str) -> None:
        rn.store.append_log(job.id, job.stage.value, line, job.attempts)

    # Scope the gate to THIS job's diff vs the base branch: a job is
    # responsible for the files it changed, not the repo's pre-existing
    # lint debt. Without this, a whole-repo `ruff check .` fails every job
    # over errors it never introduced (the un-healable gate trap).
    try:
        base = await gitops.default_branch(managed)
        base = await gitops.fresh_base(worktree, base)
        changed = [f["path"] for f in await gitops.numstat(worktree, f"{base}...HEAD")]
    except Exception:
        log.warning(
            "lint: could not compute changed files for job %s; skipping lint",
            job.id,
            exc_info=True,
        )
        changed = []
    result = await linting.run_lint(str(worktree), changed_files=changed, log_sink=_lint_sink)
    rn._record_resource(job, "lint", result.get("resource"))
    if result["auto_fixed"]:
        await gitops.commit_all(worktree, f"Job #{job.id}: lint auto-fix")
        if await github.has_remote(managed):
            await github.force_push_branch(worktree, job.branch)
    log.info("job %s lint: passed=%s auto_fixed=%s", job.id, result["passed"], result["auto_fixed"])
    if not result["passed"]:
        detail = _failure_detail(
            {
                "command": result.get("lint_commands", []),
                "output": result.get("output", ""),
            },
            _LINT_DIAGNOSTICS_ID,
            changed,
        )
        rn._event(
            job,
            "lint",
            "failed",
            started,
            summary=result.get("summary", "lint failed"),
            detail=detail,
        )
        return await rn._retry_or_fail(
            job,
            f"lint failed ({', '.join(' '.join(c) for c in result.get('lint_commands', []))}):"
            f"\n{result.get('summary', '')}",
            failure_detail=detail,
        )
    rn._event(
        job,
        "lint",
        "done",
        started,
        summary=result.get("summary", "(no lint config detected)"),
    )
    job.stage = Stage.BUILD
    job.status = JobStatus.PENDING
    rn.store.save(job)
    await rn.notify(
        job.chat_id,
        f"🧹 Job #{job.id}: lint passed. Testing…",
        project_id=job.project_id,
        job_id=job.id,
    )
    return
