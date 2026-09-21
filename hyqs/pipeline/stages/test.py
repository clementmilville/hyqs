"""Test stage: install deps, run the frontend build if present, run the test suite.

Stage handler for BUILD → TEST. Extracted from PipelineRunner._advance; logic unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import gitops, invariants, testing
from ..collision import check_alembic_head_collisions, check_symbol_collisions
from ..models import JobStatus, Stage, _now

if TYPE_CHECKING:
    from ..models import Job
    from ..runner import PipelineRunner

log = logging.getLogger("hyqs.runner")

_IMPORT_SMOKE_ID = "test.import-smoke"
_FRONTEND_BUILD_ID = "test.frontend-build"
_TEST_EXECUTION_ID = "test.execution"
_INVARIANTS_ID = "test.invariants"


def _failure_detail(
    detail: dict[str, object], check_id: str, paths: list[str], category: str
) -> dict[str, object]:
    from . import compose_authorized_gate_failure_detail_if_valid

    return compose_authorized_gate_failure_detail_if_valid(
        detail, Stage.TEST, check_id, check_id, paths, [category]
    )


async def run(rn: "PipelineRunner", job: "Job") -> None:
    worktree = rn.worktrees / f"job-{job.id}"
    started = _now()

    _fb_base = ""
    try:
        _fb_base = await gitops.default_branch(job.repo_path)
        _fb_base = await gitops.fresh_base(worktree, _fb_base)
        _fb_ns = await gitops.numstat(worktree, f"{_fb_base}...HEAD")
        _frontend_changed_files = [row["path"] for row in _fb_ns]
    except Exception:
        _frontend_changed_files = []

    if job.project_id is not None:
        try:
            collisions = check_symbol_collisions(
                worktree, job.project_id, rn.store, changed_files=_frontend_changed_files
            )
            if collisions:
                msg = "[symbol-collision] " + "\n".join(collisions)
                rn._event(job, "symbol-collision", "failed", started, summary=msg[:400])
                return await rn._retry_or_fail(job, msg)
        except Exception:
            log.warning("symbol collision check failed for job %s; skipping", job.id, exc_info=True)

    try:
        alembic_collisions = check_alembic_head_collisions(worktree, _frontend_changed_files)
        if alembic_collisions:
            msg = "[alembic-heads] " + "\n".join(alembic_collisions)
            rn._event(job, "alembic-heads", "failed", started, summary=msg[:400])
            return await rn._retry_or_fail(job, msg)
    except Exception:
        log.warning(
            "alembic head-collision check failed for job %s; skipping", job.id, exc_info=True
        )

    smoke = await testing.run_import_smoke(str(worktree))
    if not smoke.get("passed") and not smoke.get("skipped"):
        detail = _failure_detail(
            {"command": smoke.get("command", ""), "output": smoke.get("output", "")},
            _IMPORT_SMOKE_ID,
            _frontend_changed_files,
            "imports",
        )
        rn._event(
            job,
            "import-smoke",
            "failed",
            started,
            summary=smoke.get("summary", "import smoke failed"),
            detail=detail,
        )
        return await rn._retry_or_fail(
            job,
            f"[import-smoke] the app entry module failed to import "
            f"({smoke.get('command')}):\n{smoke.get('summary', '')}\n"
            "Fix the import-time error (missing import, undefined name, syntax) — "
            "this would crash the app at startup.",
            failure_detail=detail,
        )

    fb_result = await testing.run_frontend_build(str(worktree), _frontend_changed_files)
    if not fb_result.get("passed") and not fb_result.get("skipped"):
        detail = _failure_detail(
            {
                "command": fb_result.get("command", ""),
                "output": fb_result.get("output", ""),
            },
            _FRONTEND_BUILD_ID,
            _frontend_changed_files,
            "coverage_paths",
        )
        rn._event(
            job,
            "frontend-build",
            "failed",
            started,
            summary=fb_result.get("summary", "frontend build failed"),
            detail=detail,
        )
        return await rn._retry_or_fail(
            job,
            f"frontend build failed ({fb_result.get('command')}):\n{fb_result.get('summary', '')}",
            failure_detail=detail,
        )

    async def _test_sink(line: str) -> None:
        rn.store.append_log(job.id, job.stage.value, line, job.attempts)

    result = await testing.run_tests(
        str(worktree), changed_files=_frontend_changed_files, log_sink=_test_sink
    )
    rn._record_resource(job, "test", result.get("resource"))
    log.info("job %s tests via `%s`: passed=%s", job.id, result["command"], result["passed"])
    detail = {"command": result.get("command", ""), "output": result.get("output", "")}
    if not result.get("passed"):
        failure_detail = _failure_detail(
            detail, _TEST_EXECUTION_ID, _frontend_changed_files, "coverage_paths"
        )
        rn._event(
            job,
            "test",
            "failed",
            started,
            summary=f"{result.get('command')} — failed",
            detail=failure_detail,
        )
        if result.get("timed_out"):
            return await rn._timed_out(job, timeout_seconds=testing.TEST_TIMEOUT)
        return await rn._retry_or_fail(
            job,
            f"tests failed ({result.get('command')}):\n{result.get('summary', '')}",
            failure_detail=failure_detail,
        )

    invariant_results = await invariants.run_invariants(worktree, _fb_base, _frontend_changed_files)
    if invariant_results:
        failed = [r for r in invariant_results if r.status == "fail"]
        if failed:
            invariant_detail = _failure_detail(
                {
                    "checks": [
                        {"name": r.name, "status": r.status, "output": r.output}
                        for r in invariant_results
                    ]
                },
                _INVARIANTS_ID,
                _frontend_changed_files,
                "coverage_paths",
            )
            rn._event(
                job,
                "invariants",
                "failed",
                started,
                summary=f"{len(failed)} invariant check(s) violated",
                detail=invariant_detail,
            )
            failure_text = "\n\n".join(
                f"[{r.name}] invariant violated:\n{r.output}" for r in failed
            )
            return await rn._retry_or_fail(job, failure_text, failure_detail=invariant_detail)
        rn._event(
            job,
            "invariants",
            "done",
            started,
            summary=f"{len(invariant_results)} invariant check(s) passed",
            detail={
                "checks": [
                    {"name": r.name, "status": r.status, "output": r.output}
                    for r in invariant_results
                ]
            },
        )

    rn._event(
        job,
        "test",
        "done",
        started,
        summary=f"{result.get('command')} — passed",
        detail=detail,
    )
    job.stage = Stage.TEST
    job.status = JobStatus.PENDING
    rn.store.save(job)
    await rn.notify(
        job.chat_id,
        f"✅ Job #{job.id}: tests passed. Reviewing…",
        project_id=job.project_id,
        job_id=job.id,
    )
    return
