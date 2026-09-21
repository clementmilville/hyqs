"""Leader-elected Supervisor worker with deterministic failure classification and janitor loop.

Architecture:
- Exactly one Supervisor is "leader" across the whole fleet, elected via a Postgres
  session-level advisory lock (_LOCK_SUPERVISOR).  When the leader process dies the
  connection drops, Postgres auto-releases the lock, and a standby Supervisor takes over
  within one election-check interval (~30 s).
- The Supervisor runs a deterministic classifier (classify_failure) and a janitor that
  periodically scans FAILED jobs and applies safe remediations — no AI, no human.
- Classes needing judgment (genuine_code, merge_conflict_exhausted, fix_no_changes,
  isolation_leak, gate_conflict, unknown) are logged and the chat owner is notified;
  the job is left alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import enum
import json
import logging
import os
import re
import socket
import time
import typing
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from . import collision, deploy, docker_deploy, github, gitops, linting, remediation, sast
from .models import Job, JobSource, JobStatus, Stage
from .store import (
    JobStore,
    RemediationLineage,
    SupervisorRemediationResult,
    set_db_actor,
)

log = logging.getLogger("hyqs.supervisor")
MAX_AUTOMATED_REMEDIATION_DEPTH = 2


class Notify(typing.Protocol):
    async def __call__(
        self,
        chat_id: int,
        text: str,
        project_id: int | None = None,
        job_id: int | None = None,
        event_type: str | None = None,
        reason: str | None = None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# S2 – Failure classifier
# ---------------------------------------------------------------------------


class FailureClass(str, enum.Enum):
    transient = "transient"
    stale_branch = "stale_branch"
    orphaned_worktree = "orphaned_worktree"
    merged_but_stuck = "merged_but_stuck"
    merge_conflict_exhausted = "merge_conflict_exhausted"
    alembic_multi_head = "alembic_multi_head"
    attributable_deploy = "attributable_deploy"
    config_deploy = "config_deploy"
    env_deploy = "env_deploy"
    gate_no_changes = "gate_no_changes"
    fix_no_changes = "fix_no_changes"
    gate_conflict = "gate_conflict"
    isolation_leak = "isolation_leak"
    genuine_code = "genuine_code"
    dependency_blocked = "dependency_blocked"
    no_diff_build_retry = "no_diff_build_retry"
    unknown = "unknown"


_ATTRIBUTABLE_DEPLOY_PATTERNS: tuple[str, ...] = (
    "failed to resolve import",
    "cannot find module",
    "missing package",
    "rollup failed",
    "could not resolve",
    "module not found",
    "typeerror",
    "esbuild",
    "vite",
)

# Infra/permission signals that the job's diff can never fix. Checked before
# _ATTRIBUTABLE_DEPLOY_PATTERNS so a broad pattern like "build failed" (which
# wraps *all* docker failures) doesn't accidentally force self-heal on a
# permission-denied or daemon-not-running error.
_INFRA_DEPLOY_PATTERNS: tuple[str, ...] = (
    "permission denied",
    "/var/run/docker.sock",
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "no space left on device",
    "denied: requested access to the resource is denied",
    "toomanyrequests",
    "connection refused",
)


# Environment signatures the job's diff can never fix AND that a deterministic
# cleanup + redeploy CAN: filing a [deploy-fix] code job for these (the old
# config_deploy route) is pointless — three such jobs burned the per-project cap
# on a zombie-container name conflict. These route to remediate_env_deploy.
_ENV_DEPLOY_PATTERNS: tuple[str, ...] = (
    "is already in use by container",
    "could not determine probe container ip",
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "no space left on device",
    "toomanyrequests",
    "connection refused",
)

_CONTAINER_CONFLICT_RE = re.compile(r'container name "/?([\w.-]+)" is already in use')

# Alembic's own error when >1 head revision exists and no explicit target was
# given ("Multiple head revisions are present for given argument 'head'").
_ALEMBIC_MULTI_HEAD_PATTERN = "multiple head revisions are present"


def _is_alembic_multi_head(text: str) -> bool:
    return _ALEMBIC_MULTI_HEAD_PATTERN in text.lower()


def _is_env_deploy(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in _ENV_DEPLOY_PATTERNS)


def _is_attributable_deploy(text: str) -> bool:
    lower = text.lower()
    if any(p in lower for p in _INFRA_DEPLOY_PATTERNS):
        return False
    return any(p in lower for p in _ATTRIBUTABLE_DEPLOY_PATTERNS)


# GitHub API failure signals that no job diff can ever fix: HTTP 5xx from the
# GitHub API, secondary rate limiting, and abuse-detection throttling. Matched
# on the structured shape of the GitHub API's own failure text (not generic
# substrings like "503" or "modified", which would also match unrelated
# application errors — e.g. a test asserting on a 503 in its own output).
_GITHUB_API_TRANSIENT_PATTERNS: tuple[str, ...] = (
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway",
    "no server is currently available",
    "secondary rate limit",
    "abuse detection",
)


def _github_evidence_text(job: Job) -> str:
    """The text a GitHub-API-transient check should scan for ``job``.

    Prefers the structured ``github_evidence`` merge.py attaches to
    ``failure_detail`` (a list of dicts carrying a ``stderr`` key) over the
    free-form job.error/job.failure prose, which can be stale — mirrors why
    ``_classify_deploy_failure`` prefers job.error over job.failure. Falls back
    to prose for legacy rows recorded before merge.py started propagating
    ``github_evidence``.
    """
    detail = job.failure_detail if isinstance(job.failure_detail, dict) else {}
    evidence = detail.get("github_evidence")
    if isinstance(evidence, list) and evidence:
        return " ".join(
            str(entry.get("stderr", "")) for entry in evidence if isinstance(entry, dict)
        )
    return job.error or job.failure or ""


def _is_github_api_transient(job: Job) -> bool:
    lower = _github_evidence_text(job).lower()
    return any(pattern in lower for pattern in _GITHUB_API_TRANSIENT_PATTERNS)


def _classify_merge_failure(job: Job) -> FailureClass:
    """Pure: split a MERGE-stage failure into GitHub-API-transient vs unknown.

    Only called from the janitor once classify_failure()'s merged_but_stuck
    check (rule 5) has already confirmed the PR is NOT merged — mirrors
    ``_classify_deploy_failure``'s calling convention (see _janitor_scan).
    Never reached by a real merge conflict: rule 1 (merge_conflict_exhausted)
    matches "[merge-conflict]" in job.failure before rule 5 ever runs, so a
    genuine conflict is never reclassified as transient here.
    """
    if _is_github_api_transient(job):
        return FailureClass.transient
    return FailureClass.unknown


def _classify_deploy_failure(job: Job) -> FailureClass:
    """Pure: split a DEPLOY-stage failure into env/attributable/config_deploy.

    Only called from the janitor once classify_failure()'s merged_but_stuck
    check has already confirmed the PR is NOT merged (see _janitor_scan).
    Weighs job.error over job.failure — job.failure only as a fallback when
    error is empty — because job.failure can be stale: job #610's jobs.failure
    still read a security-rejection message from an earlier fix attempt while
    the actual deploy-stage error (a schema-migration deadlock) was recorded
    only in job.error.
    """
    if job.failure_code == "deploy_candidate_error":
        return FailureClass.attributable_deploy
    if job.failure_code == "deploy_environment_error":
        return FailureClass.env_deploy
    text = job.error or job.failure or ""
    if _is_alembic_multi_head(text):
        return FailureClass.alembic_multi_head
    if _is_env_deploy(text):
        return FailureClass.env_deploy
    if _is_attributable_deploy(text):
        return FailureClass.attributable_deploy
    return FailureClass.config_deploy


_GATE_PREFIXES = (
    "[symbol-collision]",
    "lint failed",
    "[lockfile-drift]",
    "merge-delta security rejected",
    "merge-delta review rejected",
)
_GATE_REQUEUE_STAGE: dict[str, Stage] = {
    "[symbol-collision]": Stage.BUILD,
    "lint failed": Stage.LINT,
    "[lockfile-drift]": Stage.LINT,
    "merge-delta security rejected": Stage.MERGE_VERIFY,
    "merge-delta review rejected": Stage.MERGE_VERIFY,
}


def _is_gate_failure(failure: str) -> bool:
    return any(p in failure for p in _GATE_PREFIXES)


_BLOCKED_DEPENDENCY_PREFIX = "blocked: dependency #"


def classify_failure(job: Job, *, rebase_max_attempts: int = 5) -> FailureClass:
    """Pure function: map a FAILED job's state onto a FailureClass.

    Priority order matters — each check is an elif so higher-priority signals
    always win, even if lower-priority patterns are also present.
    """
    typed = _classify_typed_failure(job)
    if typed is not None:
        return typed

    failure = job.failure or ""
    error = (job.error or "").lower()

    # 0. dependency_blocked: a propagated terminal failure written by
    # _reconcile_blocked_dependents. Checked first, before every other rule, because
    # this failure must never be requeued from scratch — the dependency it names is
    # dead, so re-running would just rediscover the same unrecoverable blocker.
    if failure.startswith(_BLOCKED_DEPENDENCY_PREFIX):
        return FailureClass.dependency_blocked

    # 1. merge_conflict_exhausted: any [merge-conflict] failure that reached FAILED.
    # The runner retries rebases internally (status stays PENDING, not FAILED) on a
    # dedicated rebase_attempts budget, so a conflict marker only survives into a
    # FAILED job once automated resolution has already given up. Such a job must
    # NEVER be requeued-from-scratch as a stale_branch (rule 6) — that resets the
    # counters and re-hits the same conflict forever (jobs #188/#190/#199 looped
    # 40-57×). Escalate to a human instead. rebase_max_attempts is retained for
    # signature stability but no longer gates the classification.
    if "[merge-conflict]" in failure:
        return FailureClass.merge_conflict_exhausted

    # 2. isolation_leak: agent escaped the worktree into the shared checkout.
    # Match on "shared checkout" — the phrase common to BOTH leak messages: the
    # build-stage guard's "Build agent wrote to shared checkout … instead of
    # worktree …" (which contains no "isolation") and the already-satisfied
    # guard's "… shared checkout is dirty — probable isolation leak". Keying on
    # "isolation" alone silently misclassified the build-time leak as
    # stale_branch (rule 6) and requeued it from scratch; re-running build
    # re-triggered the same deterministic leak until the stale_branch cap
    # dead-lettered it (Job #266 looped 5× this way). isolation_leak is a
    # judgment class — it is escalated to a human, never requeued, because the
    # leak is deterministic and requeue-from-scratch cannot heal it.
    if "shared checkout" in error or "isolation" in error:
        return FailureClass.isolation_leak

    # 2.4. deploy-stage failure: verify merged-PR state FIRST, ahead of every
    # other deploy-specific pattern match (including the deadlock shortcut
    # below). Job #610: PR #68 was merged and job #622 had already deployed
    # the commit, but the worker's txn was killed by a schema-migration
    # deadlock (SQLSTATE 40P01) racing JobStore._ensure_schema on startup; the
    # job was wrongly marked FAILED and _reconcile_blocked_dependents then
    # terminally failed the 11 dependent design-chain jobs #611-#621. Routing
    # every DEPLOY-stage failure through merged_but_stuck first means a merged
    # PR always reconciles to DONE regardless of *why* the deploy stage
    # errored (deadlock, container conflict, module-not-found, ...). When the
    # PR is NOT merged, the janitor falls back to _classify_deploy_failure()
    # for the env/attributable/config split (see _janitor_scan).
    if job.stage == Stage.DEPLOY:
        return FailureClass.merged_but_stuck

    # 2.5. transient Postgres error (deadlock/serialization failure): Postgres
    # killed one txn of a valid, non-buggy pair — the code being deployed is not
    # at fault. Only reachable for non-DEPLOY stages now; DEPLOY-stage jobs are
    # already routed to merged_but_stuck by rule 2.4 above.
    combined = (failure + " " + error).lower()
    if "deadlock detected" in combined or "serialization failure" in combined:
        return FailureClass.transient

    # 3.5. gate_no_changes: deterministic gate fired but fix agent found nothing to change —
    # strongly signals the gate is wrong, not the code (see Job #76, M2 self-healing)
    if "produced no changes" in error and _is_gate_failure(failure):
        return FailureClass.gate_no_changes

    # 3.6. fix_no_changes: fixer found nothing to change for a NON-gate failure
    # (reviewer/fixer disagreement). Requeue-from-scratch would deterministically
    # repeat the disagreement — up to 5 full pipeline runs via the stale_branch
    # path before dead-letter — so escalate to a human instead.
    if "produced no changes" in error:
        return FailureClass.fix_no_changes

    # 4. genuine_code: runner gave up — fix attempts exhausted ("gave up after")
    # or the stage-timeout budget exhausted ("without completing; giving up").
    # The timeout message must NOT fall through to rule 8's "timed out" keyword:
    # a transient requeue can't reset the spent timeout budget, so each rerun
    # would die on its first timeout after a full pipeline re-run.
    if "gave up after" in error or "giving up" in error:
        return FailureClass.genuine_code

    # 5. merged_but_stuck: PR may already be merged; Supervisor verifies via GitHub
    if job.stage in {Stage.REVIEW, Stage.MERGE}:
        return FailureClass.merged_but_stuck

    # 6. stale_branch: worker died mid-build with a live worktree
    if job.branch and job.stage in {Stage.PLAN, Stage.BUILD, Stage.TEST, Stage.FIX}:
        return FailureClass.stale_branch

    # 7. orphaned_worktree: branch set but job never left QUEUED
    if job.branch and job.stage == Stage.QUEUED:
        return FailureClass.orphaned_worktree

    # 8. transient: network/timeout blips
    transient_keywords = ["unexpected error", "timeout", "connection", "timed out"]
    if any(kw in error for kw in transient_keywords):
        return FailureClass.transient

    # 9. unknown: catch-all
    return FailureClass.unknown


def _classify_typed_failure(job: Job) -> FailureClass | None:
    """Classify new rows without consulting mutable checkpoint/text state.

    Returning ``None`` is the rolling-deployment compatibility boundary: legacy
    rows continue through the unchanged English-pattern classifier below.
    """
    code = job.failure_code
    if not code:
        return None
    if code == "dependency_blocked":
        return FailureClass.dependency_blocked
    if code == "merge_conflict":
        return FailureClass.merge_conflict_exhausted
    if code == "isolation_violation":
        return FailureClass.isolation_leak
    if code == "stage_timeout_exhausted":
        return FailureClass.genuine_code
    if code == "no_diff_verification_failed":
        if (
            job.failed_step == "build"
            and job.retry_disposition == "retry_build"
            and isinstance(job.failure_detail, dict)
            and job.failure_detail.get("retry_attempt") == 0
        ):
            return FailureClass.no_diff_build_retry
        return (
            FailureClass.genuine_code
            if job.retry_disposition == "terminal"
            else FailureClass.unknown
        )
    if code in {"deploy_environment_error", "deploy_candidate_error"}:
        return FailureClass.merged_but_stuck
    if job.retry_disposition == "same_step" or code in {
        "provider_transport_error",
        "provider_unavailable",
        "stage_timeout",
        "unexpected_stage_error",
    }:
        return FailureClass.transient
    if code == "gate_conflict":
        return FailureClass.gate_conflict
    if job.retry_disposition in {"terminal", "human_review"}:
        return FailureClass.genuine_code
    if code in {"test_failed", "lint_failed", "gate_failed"}:
        return FailureClass.genuine_code
    return FailureClass.unknown


# ---------------------------------------------------------------------------
# S3 – Janitor remediation helpers
# ---------------------------------------------------------------------------


def _record_dead_letter_once(jobs: JobStore, job_id: int, failure_class: str, detail: str) -> None:
    """Persist the terminal decision once; later scans must remain quiet."""
    if jobs.has_supervisor_event(job_id, "dead_lettered") is True:
        return
    jobs.record_supervisor_event(job_id, "dead_lettered", failure_class, detail=detail)


async def _managed_repo_for_job(jobs: JobStore, job: Job, config) -> Path:
    """The pipeline-owned managed-clone repo path backing ``job``'s worktree.

    Mirrors ``PipelineRunner._managed_repo``: for a project-scoped job, BUILD
    creates its worktree against this managed-clone path (via
    ``gitops.create_worktree``), not ``job.repo_path`` — so cleanup must target
    the same path, or a stale branch survives a requeue and the retry
    deterministically re-fails with "branch already exists" (job #3988, jobs
    #3903/#3985). Falls back to ``Path(job.repo_path)`` when the job has no
    ``project_id`` or its repo has no ``origin`` remote (offline/local repos
    keep today's behavior).
    """
    project_id = getattr(job, "project_id", None)
    if not project_id:
        return Path(job.repo_path)
    res = await gitops.git(job.repo_path, "remote", "get-url", "origin")
    if not res.ok or not res.stdout.strip():
        return Path(job.repo_path)
    origin_url = res.stdout.strip()
    return await gitops.ensure_managed_repo(Path(config.data_dir), project_id, origin_url)


async def remediate_stale_branch(
    jobs: JobStore,
    job: Job,
    config,
    *,
    failure_class: str = "stale_branch",
    dead_letter_cap: int = 5,
    notify: Notify | None = None,
) -> None:
    """Clean up a stale worktree + branch, then re-queue from scratch.

    Requeues reset the fix/rebase counters (``zero_attempts=True``), so this path
    has NO per-job retry budget of its own — a job that keeps re-failing here would
    loop forever (jobs #188/#190/#199 were requeued 40-57× before this cap existed).
    The supervisor_requeue_count gives it a dead-letter ceiling: past the cap, stop
    requeuing, escalate once, and leave the job FAILED for a human.
    """
    count = jobs.supervisor_requeue_count(job.id)
    if count >= dead_letter_cap:
        log.warning(
            "janitor: %s job %s hit dead-letter cap (%d/%d), escalating",
            failure_class,
            job.id,
            count,
            dead_letter_cap,
        )
        if await _consult_analyst_before_dead_letter(
            jobs,
            job,
            config,
            notify,
            cls=FailureClass(failure_class),
            count=count,
            cap=dead_letter_cap,
        ):
            return
        if not jobs.is_supervisor_notified(job.id):
            if notify is not None:
                await notify(
                    job.chat_id,
                    f"⚠️ Job #{job.id} keeps failing after re-queue ({count}/{dead_letter_cap} "
                    f"{failure_class} attempts) — giving up: {(job.failure or job.error or '')[:120]}",
                    project_id=job.project_id,
                    job_id=job.id,
                    event_type="needs_attention",
                    reason=failure_class,
                )
            jobs.mark_supervisor_notified(job.id)
        try:
            _record_dead_letter_once(
                jobs, job.id, failure_class, f"cap={dead_letter_cap} count={count}"
            )
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        return
    worktree = Path(config.data_dir) / "worktrees" / f"job-{job.id}"
    managed_repo = await _managed_repo_for_job(jobs, job, config)
    try:
        await gitops.remove_worktree(managed_repo, worktree)
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.debug("janitor: remove_worktree job %s (best-effort): %s", job.id, exc)
    if job.branch:
        try:
            await gitops.delete_local_branch(managed_repo, job.branch)
        except Exception as exc:
            log.debug("janitor: delete_local_branch job %s (best-effort): %s", job.id, exc)
    jobs.increment_supervisor_requeue(job.id)
    jobs.requeue_job(job.id, zero_attempts=True)
    try:
        jobs.record_supervisor_event(
            job.id,
            "requeued",
            failure_class,
            detail=f"branch={job.branch} attempt {count + 1}/{dead_letter_cap}",
        )
    except Exception as exc:
        log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
    log.info(
        "janitor: requeued stale_branch/orphaned job %s (attempt %d/%d)",
        job.id,
        count + 1,
        dead_letter_cap,
    )


async def remediate_merged_but_stuck(
    jobs: JobStore, job: Job, config, *, failure_class: str = "merged_but_stuck"
) -> bool:
    """If the PR is already merged on GitHub, reconcile the local job state to DONE.

    Returns True when the job was reconciled (PR merged). False means the PR is
    NOT merged — the caller must route the job elsewhere (transient requeue for
    REVIEW/MERGE, or the env/attributable/config deploy split for DEPLOY):
    leaving it alone stalls it FAILED forever with no notification while this
    scan re-polls GitHub every cycle.

    For a DEPLOY-stage job that IS reconciled to DONE here, no second deploy
    path is needed: _auto_deploy_scan already polls each project's
    origin/<base> tip against the last recorded deployed_commit every scan and
    enqueues a deploy-only job whenever they differ, so any residual need to
    actually deploy the merged commit is covered automatically.
    """
    if not job.branch:
        return False
    merged = await github.pr_is_merged(job.repo_path, job.branch)
    if merged:
        jobs.reconcile_to_done(job.id)
        try:
            jobs.record_supervisor_event(
                job.id, "reconciled", failure_class, detail=f"branch={job.branch}"
            )
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        log.info("janitor: reconciled merged_but_stuck job %s to DONE", job.id)
        return True
    log.debug("janitor: job %s PR not merged, routing to transient requeue", job.id)
    return False


async def remediate_merge_github_transient(
    jobs: JobStore,
    job: Job,
    config,
    *,
    dead_letter_cap: int = 5,
    failure_class: str = "transient",
    notify: Notify | None = None,
) -> None:
    """Back off and retry a MERGE-stage failure caused by a GitHub API transient
    (5xx / secondary rate limit / abuse detection / merge-race "base branch was
    modified"), up to dead_letter_cap times.

    An immediate retry would likely re-hit the same outage or race, so this
    waits an exponential backoff window — matching the scale merge.py's own
    not-mergeable retry already uses (30s doubling, capped at 600s) — since the
    last matching requeue before trying again. Reuses the existing
    supervisor_requeue_count counter and dead-letter cap rather than a new
    budget, so a persistently failing merge still escalates instead of looping.
    """
    count = jobs.supervisor_requeue_count(job.id)
    if count >= dead_letter_cap:
        log.warning(
            "janitor: %s job %s hit dead-letter cap (%d/%d), escalating",
            failure_class,
            job.id,
            count,
            dead_letter_cap,
        )
        if await _consult_analyst_before_dead_letter(
            jobs,
            job,
            config,
            notify,
            cls=FailureClass(failure_class),
            count=count,
            cap=dead_letter_cap,
        ):
            return
        if not jobs.is_supervisor_notified(job.id):
            if notify is not None:
                await notify(
                    job.chat_id,
                    f"⚠️ Job #{job.id} keeps failing to merge after {count}/{dead_letter_cap} "
                    f"GitHub-transient retries — giving up: {(job.failure or job.error or '')[:120]}",
                    project_id=job.project_id,
                    job_id=job.id,
                    event_type="needs_attention",
                    reason=failure_class,
                )
            jobs.mark_supervisor_notified(job.id)
        try:
            _record_dead_letter_once(
                jobs, job.id, failure_class, f"cap={dead_letter_cap} count={count}"
            )
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        return

    backoff = min(30 * (2**count), 600)
    last_requeue_ts: float | None = None
    for event in reversed(jobs.list_supervisor_events_for_job(job.id)):
        if event.get("action") == "requeued" and event.get("failure_class") == failure_class:
            last_requeue_ts = event.get("ts")
            break
    if last_requeue_ts is not None and time.time() - last_requeue_ts < backoff:
        log.debug(
            "janitor: %s job %s backing off (%.0fs remaining of %.0fs)",
            failure_class,
            job.id,
            backoff - (time.time() - last_requeue_ts),
            backoff,
        )
        return

    jobs.increment_supervisor_requeue(job.id)
    jobs.requeue_job_at_stage(job.id, job.stage, failure=job.failure or "")
    try:
        jobs.record_supervisor_event(
            job.id,
            "requeued",
            failure_class,
            detail=f"attempt {count + 1}/{dead_letter_cap} backoff={backoff:.0f}s",
        )
    except Exception as exc:
        log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
    log.info(
        "janitor: requeued %s job %s (attempt %d/%d)",
        failure_class,
        job.id,
        count + 1,
        dead_letter_cap,
    )


async def remediate_transient(
    jobs: JobStore,
    job: Job,
    config,
    *,
    dead_letter_cap: int = 5,
    failure_class: str = "transient",
    notify: Notify | None = None,
) -> None:
    """Re-queue a transiently failed job at its failed stage, up to dead_letter_cap times.

    Resumes at ``job.stage`` (every handler is resume-safe by design) rather than
    from QUEUED: a transient blip at SECURITY must not cost a full re-plan/re-build.
    ``failure`` is preserved so a FIX-stage resume still has the fixer's input.
    """
    count = jobs.supervisor_requeue_count(job.id)
    if count >= dead_letter_cap:
        log.warning(
            "janitor: transient job %s hit dead-letter cap (%d/%d), leaving alone",
            job.id,
            count,
            dead_letter_cap,
        )
        if await _consult_analyst_before_dead_letter(
            jobs,
            job,
            config,
            notify,
            cls=FailureClass(failure_class),
            count=count,
            cap=dead_letter_cap,
        ):
            return
        try:
            _record_dead_letter_once(
                jobs, job.id, failure_class, f"cap={dead_letter_cap} count={count}"
            )
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        return
    jobs.increment_supervisor_requeue(job.id)
    # worktree_missing can never self-heal by resuming in place: the worktree
    # it needs no longer exists. Reroute to BUILD, which recreates it, instead
    # of resuming at the failed stage where it would fail identically until
    # dead-letter. Every other transient code keeps resuming at job.stage.
    resume_stage = Stage.BUILD if job.failure_code == "worktree_missing" else job.stage
    jobs.requeue_job_at_stage(job.id, resume_stage, failure=job.failure or "")
    try:
        jobs.record_supervisor_event(
            job.id,
            "requeued",
            failure_class,
            detail=f"attempt {count + 1}/{dead_letter_cap}",
        )
    except Exception as exc:
        log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
    log.info(
        "janitor: requeued transient job %s (attempt %d/%d)",
        job.id,
        count + 1,
        dead_letter_cap,
    )


async def remediate_no_diff_build(jobs: JobStore, job: Job) -> None:
    """Requeue a verifier-rejected no-diff BUILD exactly once at its PLAN checkpoint."""
    if jobs.requeue_no_diff_build(job.id):
        jobs.record_supervisor_event(
            job.id,
            "requeued",
            FailureClass.no_diff_build_retry.value,
            detail="failed_step=build retry_attempt=1/1",
        )
        log.info("janitor: requeued no-diff BUILD job %s (attempt 1/1)", job.id)


async def remediate_gate_no_changes(
    jobs: JobStore,
    job: Job,
    config,
    notify: Notify,
    *,
    auto_recovery_cap: int = 3,
) -> None:
    """Re-check gate attribution in the still-intact worktree.

    Not attributable → suppress the finding and requeue the job past the gate.
    Attributable but unfixable → escalate + create a gate-fix job
    that the failed job depends on (it re-runs automatically once the gate is fixed).
    Capped at auto_recovery_cap to prevent infinite loops.
    """
    failure = job.failure or ""

    count = jobs.supervisor_requeue_count(job.id)
    if count >= auto_recovery_cap:
        log.warning(
            "janitor: gate_no_changes job %s hit auto-recovery cap (%d/%d), escalating",
            job.id,
            count,
            auto_recovery_cap,
        )
        if not jobs.is_supervisor_notified(job.id):
            await notify(
                job.chat_id,
                f"⚠️ Job #{job.id} hit gate auto-recovery cap ({count}/{auto_recovery_cap}): {failure[:120]}",
                project_id=job.project_id,
                job_id=job.id,
                event_type="needs_attention",
                reason="gate_no_changes",
            )
            jobs.mark_supervisor_notified(job.id)
        try:
            jobs.record_supervisor_event(
                job.id,
                "escalated",
                "gate_no_changes",
                detail=f"cap={auto_recovery_cap} count={count}",
            )
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        return

    gate_prefix = next((p for p in _GATE_PREFIXES if p in failure), None)
    if gate_prefix is None:
        log.warning(
            "janitor: gate_no_changes job %s: no gate prefix in failure %r", job.id, failure
        )
        return

    requeue_stage = _GATE_REQUEUE_STAGE[gate_prefix]

    worktree = Path(config.data_dir) / "worktrees" / f"job-{job.id}"
    if not worktree.exists():
        log.warning("janitor: gate_no_changes job %s: worktree missing, escalating", job.id)
        if not jobs.is_supervisor_notified(job.id):
            await notify(
                job.chat_id,
                f"⚠️ Job #{job.id}: gate re-check skipped — worktree missing, needs human review",
                project_id=job.project_id,
                job_id=job.id,
                event_type="needs_attention",
                reason="gate_no_changes",
            )
            jobs.mark_supervisor_notified(job.id)
        try:
            jobs.record_supervisor_event(
                job.id, "escalated", "gate_no_changes", detail="worktree_missing"
            )
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        return

    _MERGE_DELTA_PREFIXES = frozenset(
        ("merge-delta security rejected", "merge-delta review rejected")
    )

    attributable_msgs: list[str] = []
    try:
        if gate_prefix == "[symbol-collision]":
            base = await gitops.default_branch(job.repo_path)
            base = await gitops.fresh_base(worktree, base)
            mb = await gitops.git(worktree, "merge-base", base, "HEAD")
            effective_base = mb.stdout.strip() if mb.ok and mb.stdout.strip() else None
            if effective_base:
                ns = await gitops.numstat(worktree, f"{effective_base}..HEAD")
                changed_files_live = [row["path"] for row in ns]
                attributable_msgs = collision.check_symbol_collisions(
                    worktree, job.project_id, jobs, changed_files=changed_files_live
                )
            else:
                log.warning(
                    "janitor: symbol-collision gate_no_changes job %s: merge-base failed,"
                    " treating as non-attributable",
                    job.id,
                )
        elif gate_prefix in _MERGE_DELTA_PREFIXES:
            # Re-run SAST with the live merge-base to distinguish stale-base false positives
            # (findings from other jobs' code) from genuine findings in this job's contribution.
            base = await gitops.default_branch(job.repo_path)
            base = await gitops.fresh_base(worktree, base)
            mb = await gitops.git(worktree, "merge-base", base, "HEAD")
            effective_base = mb.stdout.strip() if mb.ok and mb.stdout.strip() else None
            if effective_base:
                ns = await gitops.numstat(worktree, f"{effective_base}..HEAD")
                changed_files_live = [row["path"] for row in ns]
                scan_result = await sast.run_sast_scan(worktree, effective_base, changed_files_live)
                if isinstance(scan_result, list):
                    scan_result = sast.SastScanResult(tuple(scan_result))
                attributable_msgs = [
                    f"[{f.severity}] [{f.tool}] {f.note}"
                    + (f" ({f.file}:{f.line})" if f.file else "")
                    for f in scan_result.findings
                ]
                attributable_msgs.extend(
                    f"[{finding.status.value}] [npm-audit] {finding.reason}"
                    for finding in scan_result.npm_audit.findings
                    if finding.blocks
                )
            else:
                log.warning(
                    "janitor: merge-delta gate_no_changes job %s: merge-base failed,"
                    " treating as non-attributable",
                    job.id,
                )
        else:
            try:
                base = await gitops.default_branch(job.repo_path)
                base = await gitops.fresh_base(worktree, base)
                ns = await gitops.numstat(worktree, f"{base}...HEAD")
                changed_files: list[str] | None = [row["path"] for row in ns] or None
            except Exception:
                changed_files = None
            result = await linting.run_lint(worktree, changed_files=changed_files)
            if not result.get("passed", True):
                attributable_msgs = [result.get("summary", "lint failed")]
    except Exception as exc:
        log.warning("janitor: gate_no_changes job %s: re-check error: %s", job.id, exc)
        if not jobs.is_supervisor_notified(job.id):
            await notify(
                job.chat_id,
                f"⚠️ Job #{job.id}: gate re-check error ({gate_prefix}): {exc}",
                project_id=job.project_id,
                job_id=job.id,
                event_type="needs_attention",
                reason="gate_no_changes",
            )
            jobs.mark_supervisor_notified(job.id)
        try:
            jobs.record_supervisor_event(
                job.id, "escalated", "gate_no_changes", detail=f"recheck_error={exc}"
            )
        except Exception as exc2:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc2)
        return

    if not attributable_msgs:
        jobs.increment_supervisor_requeue(job.id)
        jobs.requeue_job_at_stage(job.id, requeue_stage)
        detail = f"prefix={gate_prefix} stage={requeue_stage.value} attempt={count + 1}/{auto_recovery_cap}"
        try:
            jobs.record_supervisor_event(job.id, "suppressed", "gate_no_changes", detail=detail)
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        await notify(
            job.chat_id,
            f"ℹ️ Job #{job.id}: gate finding not attributable to diff — suppressed and requeued at {requeue_stage.value}",
            project_id=job.project_id,
            job_id=job.id,
        )
        log.info(
            "janitor: gate_no_changes job %s suppressed (prefix=%s, attempt %d/%d)",
            job.id,
            gate_prefix,
            count + 1,
            auto_recovery_cap,
        )
    else:
        finding_detail = "\n".join(attributable_msgs)[:2000]
        idea = (
            f"[gate-fix] {gate_prefix} in {job.repo_path}\n\n"
            f"Original idea (truncated): {job.idea[:200]}\n\n"
            f"Gate finding(s):\n{finding_detail}\n\n"
            f"Fix the root cause so the gate passes."
        )
        gate_fix = jobs.create(
            idea=idea,
            repo_path=job.repo_path,
            chat_id=job.chat_id,
            priority=job.priority,
            source=JobSource.SUPERVISOR,
            source_actor=job.owner,
            source_meta={
                "parent_job_id": job.id,
                "host": job.owner.split(":")[0] if job.owner else "",
                "kind": "gate-fix",
            },
        )
        jobs.add_job_dependency(job.id, gate_fix.id)
        jobs.requeue_job_at_stage(job.id, requeue_stage)
        jobs.mark_supervisor_notified(job.id)
        detail = f"prefix={gate_prefix} gate_fix_job={gate_fix.id}"
        try:
            jobs.record_supervisor_event(
                job.id, "gate_fix_created", "gate_no_changes", detail=detail
            )
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        await notify(
            job.chat_id,
            f"⚠️ Job #{job.id}: gate finding attributable — created gate-fix job #{gate_fix.id}. "
            f"Job #{job.id} will re-run automatically once the gate is fixed.",
            project_id=job.project_id,
            job_id=job.id,
        )
        log.info(
            "janitor: gate_no_changes job %s escalated, gate-fix job %s created",
            job.id,
            gate_fix.id,
        )


async def remediate_env_deploy(
    jobs: JobStore,
    job: Job,
    config,
    notify: Notify,
    *,
    dead_letter_cap: int = 5,
    failure_class: str = "env_deploy",
) -> None:
    """Environment-caused deploy failure: clean the environment, requeue at DEPLOY.

    These signatures (container-name conflict, dead probe, docker daemon, disk,
    registry throttle) can never be fixed by a code job — the old route filed
    [deploy-fix] jobs that all died on the same zombie container and burned the
    per-project fix cap. Deterministic cleanup + bounded redeploy instead.
    """
    count = jobs.supervisor_requeue_count(job.id)
    if count >= dead_letter_cap:
        if await _consult_analyst_before_dead_letter(
            jobs,
            job,
            config,
            notify,
            cls=FailureClass(failure_class),
            count=count,
            cap=dead_letter_cap,
        ):
            return
        if not jobs.is_supervisor_notified(job.id):
            await notify(
                job.chat_id,
                f"⚠️ Job #{job.id}: deploy environment still broken after "
                f"{count} cleanup+redeploy attempts — needs human attention: "
                f"{(job.error or '')[:150]}",
                project_id=job.project_id,
                job_id=job.id,
                event_type="needs_attention",
                reason=failure_class,
            )
            jobs.mark_supervisor_notified(job.id)
        with contextlib.suppress(Exception):
            _record_dead_letter_once(jobs, job.id, failure_class, f"cap={dead_letter_cap}")
        return

    text = (job.failure or "") + " " + (job.error or "")
    cleaned: list[str] = []
    # Container-name conflict: remove the conflicting container iff it is NOT
    # owned by a compose project (i.e. legacy/stray) — compose-owned containers
    # are compose's to replace.
    m = _CONTAINER_CONFLICT_RE.search(text)
    if m:
        name = m.group(1)
        try:
            if await docker_deploy._container_unowned_by_compose(name):
                await docker_deploy._remove_container(name)
                cleaned.append(f"removed unowned container {name}")
        except Exception:
            log.warning("janitor: env_deploy container cleanup failed", exc_info=True)
    # Dead probe container from a crashed prior deploy holds the probe name.
    if "probe container" in text.lower() and job.repo_path:
        probe = docker_deploy._container_name(job.repo_path) + "-probe"
        with contextlib.suppress(Exception):
            await docker_deploy._remove_container(probe)
            cleaned.append(f"removed probe {probe}")

    jobs.increment_supervisor_requeue(job.id)
    jobs.requeue_job_at_stage(job.id, Stage.DEPLOY)
    detail = f"cleaned={cleaned or 'nothing'} attempt {count + 1}/{dead_letter_cap}"
    with contextlib.suppress(Exception):
        jobs.record_supervisor_event(job.id, "env_cleaned_requeued", failure_class, detail=detail)
    await notify(
        job.chat_id,
        f"🧹 Job #{job.id}: deploy blocked by environment ({(job.error or '')[:100]}…) — "
        f"cleaned up ({', '.join(cleaned) or 'nothing to clean'}) and requeued the deploy "
        f"(attempt {count + 1}/{dead_letter_cap}).",
        project_id=job.project_id,
        job_id=job.id,
    )
    log.info("janitor: env_deploy job %s: %s", job.id, detail)


async def remediate_deploy_failed(
    jobs: JobStore,
    job: Job,
    config,
    notify: Notify,
    *,
    project_cap: int = 3,
) -> None:
    """File a fresh fix-forward pipeline job for a terminally-failed deploy.

    All decisions are deterministic (no AI). Guards:
    a. loop-guard: if this job is itself an auto-fix, stop the chain.
    b. dedup: one auto-fix per failed job id.
    c. cap: at most project_cap deploy-fix jobs per project.
    """
    # a. Loop-guard: this job is already a supervisor-created deploy fix.
    if (job.source_meta or {}).get("deploy_fix_for"):
        if not jobs.is_supervisor_notified(job.id):
            await notify(
                job.chat_id,
                f"⚠️ Job #{job.id} (auto deploy-fix) also failed at deploy — "
                f"stopping fix chain, needs human attention",
                project_id=job.project_id,
                job_id=job.id,
                event_type="needs_attention",
                reason="deploy_failed",
            )
            jobs.mark_supervisor_notified(job.id)
        try:
            jobs.record_supervisor_event(
                job.id, "escalated", "deploy_failed", detail="deploy_fix_chain_stopped"
            )
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        return

    # b. Dedup: only one auto-fix per failed job.
    if jobs.has_deploy_fix_job(job.id):
        log.debug("janitor: deploy-fix job already exists for job %s, skipping", job.id)
        return

    # c. Per-project cap.
    if job.project_id is not None and jobs.count_deploy_fix_jobs(job.project_id) >= project_cap:
        if not jobs.is_supervisor_notified(job.id):
            await notify(
                job.chat_id,
                f"⚠️ Job #{job.id}: deploy-fix cap ({project_cap}) reached for this project "
                f"— needs human attention",
                project_id=job.project_id,
                job_id=job.id,
                event_type="needs_attention",
                reason="deploy_failed",
            )
            jobs.mark_supervisor_notified(job.id)
        try:
            jobs.record_supervisor_event(
                job.id, "cap_hit", "deploy_failed", detail=f"cap={project_cap}"
            )
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        return

    # d. Create fix-forward job.
    deploy_error = (job.failure or job.error or "")[:2000]
    idea = (
        f"[deploy-fix] Fix deploy failure from job #{job.id}.\n\n"
        f"Original idea (truncated): {job.idea[:200]}\n\n"
        f"Deploy error/logs:\n{deploy_error}\n\n"
        f"Fix the root cause so the deploy succeeds. "
        f"Run the full pipeline (plan→build→…→deploy) landing the fix on current main."
    )
    fix_job = jobs.create(
        idea=idea,
        repo_path=job.repo_path,
        chat_id=job.chat_id,
        epic_id=job.epic_id,
        priority=job.priority,
        source=JobSource.SUPERVISOR,
        source_actor=job.owner,
        source_meta={"deploy_fix_for": job.id, "failed_idea": job.idea[:200]},
    )

    # No human in the loop: requeue the original at DEPLOY, gated behind the fix
    # job via a dependency. Once the fix lands (its own pipeline run deploys the
    # merged fix), the original's deploy coalesces on the now-live sha and the
    # job closes as DONE instead of lingering as failed history.
    jobs.add_job_dependency(job.id, fix_job.id)
    jobs.requeue_job_at_stage(job.id, Stage.DEPLOY)

    # e. Record event.
    try:
        jobs.record_supervisor_event(
            job.id, "deploy_fix_created", "deploy_failed", detail=f"fix_job={fix_job.id}"
        )
    except Exception as exc:
        log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)

    # f. Notify owner.
    await notify(
        job.chat_id,
        f"ℹ️ Job #{job.id} failed at deploy — created fix-forward job #{fix_job.id} "
        f"to address the root cause.",
        project_id=job.project_id,
        job_id=job.id,
    )
    log.info("janitor: created deploy-fix job %s for failed deploy job %s", fix_job.id, job.id)


async def remediate_alembic_multi_head(
    jobs: JobStore,
    job: Job,
    config,
    notify: Notify,
    *,
    project_cap: int = 3,
) -> None:
    """File a merge-revision fix job for a deploy failed on multiple alembic heads.

    Structurally mirrors remediate_deploy_failed, but the fix job is scoped to
    exactly one generated migration file (not a free-form code fix):
    a. loop-guard: if this job is itself an alembic-merge fix, stop the chain.
    b. dedup: one auto-fix per failed job id.
    c. cap: at most project_cap alembic-merge-fix jobs per project.
    d. locate the versions dir with >1 real head; none found -> notify + return.
    e. build the merge-revision file and file a job to write it verbatim.
    f. gate the original job behind the fix via a dependency, requeue at DEPLOY.
    """
    # a. Loop-guard: this job is already a supervisor-created alembic-merge fix.
    if (job.source_meta or {}).get("alembic_merge_fix_for"):
        if not jobs.is_supervisor_notified(job.id):
            await notify(
                job.chat_id,
                f"⚠️ Job #{job.id} (auto alembic-merge-fix) also failed at deploy — "
                f"stopping fix chain, needs human attention",
                project_id=job.project_id,
                job_id=job.id,
                event_type="needs_attention",
                reason="alembic_multi_head",
            )
            jobs.mark_supervisor_notified(job.id)
        try:
            jobs.record_supervisor_event(
                job.id, "escalated", "alembic_multi_head", detail="alembic_merge_fix_chain_stopped"
            )
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        return

    # b. Dedup: only one auto-fix per failed job.
    if jobs.has_alembic_merge_fix_job(job.id):
        log.debug("janitor: alembic-merge-fix job already exists for job %s, skipping", job.id)
        return

    # c. Per-project cap.
    if (
        job.project_id is not None
        and jobs.count_alembic_merge_fix_jobs(job.project_id) >= project_cap
    ):
        if not jobs.is_supervisor_notified(job.id):
            await notify(
                job.chat_id,
                f"⚠️ Job #{job.id}: alembic-merge-fix cap ({project_cap}) reached for this "
                f"project — needs human attention",
                project_id=job.project_id,
                job_id=job.id,
                event_type="needs_attention",
                reason="alembic_multi_head",
            )
            jobs.mark_supervisor_notified(job.id)
        try:
            jobs.record_supervisor_event(
                job.id, "cap_hit", "alembic_multi_head", detail=f"cap={project_cap}"
            )
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        return

    # d. Locate a versions dir with more than one real head.
    versions_dir: Path | None = None
    heads: set[str] = set()
    if job.repo_path:
        for candidate in Path(job.repo_path).glob("**/alembic/versions"):
            candidate_heads = collision.discover_alembic_heads(candidate)
            if len(candidate_heads) > 1:
                versions_dir = candidate
                heads = candidate_heads
                break

    if versions_dir is None:
        log.warning(
            "janitor: alembic_multi_head job %s: no versions dir with >1 head found", job.id
        )
        if not jobs.is_supervisor_notified(job.id):
            await notify(
                job.chat_id,
                f"⚠️ Job #{job.id}: deploy failed on multiple alembic heads, but no "
                f"conflicting versions directory could be located — needs human attention",
                project_id=job.project_id,
                job_id=job.id,
                event_type="needs_attention",
                reason="alembic_multi_head",
            )
            jobs.mark_supervisor_notified(job.id)
        try:
            jobs.record_supervisor_event(
                job.id, "escalated", "alembic_multi_head", detail="no_versions_dir_found"
            )
        except Exception as exc:
            log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
        return

    # e. Build the merge-revision file and file a job scoped to writing it verbatim.
    filename, content = remediation.generate_alembic_merge_revision(sorted(heads))
    relative_path = (versions_dir / filename).relative_to(job.repo_path)
    idea = (
        f"[alembic-merge-fix] Resolve multiple alembic heads from job #{job.id}.\n\n"
        f"Original idea (truncated): {job.idea[:200]}\n\n"
        f"Target files: {relative_path}\n\n"
        f"```python\n{content}\n```\n\n"
        f"Write this file verbatim at the path above — do not modify its contents."
    )
    fix_job = jobs.create(
        idea=idea,
        repo_path=job.repo_path,
        chat_id=job.chat_id,
        epic_id=job.epic_id,
        priority=job.priority,
        source=JobSource.SUPERVISOR,
        source_actor=job.owner,
        source_meta={"alembic_merge_fix_for": job.id, "failed_idea": job.idea[:200]},
    )

    # f. No human in the loop: requeue the original at DEPLOY, gated behind the
    # fix job via a dependency.
    jobs.add_job_dependency(job.id, fix_job.id)
    jobs.requeue_job_at_stage(job.id, Stage.DEPLOY)

    # g. Record event.
    try:
        jobs.record_supervisor_event(
            job.id,
            "alembic_merge_fix_created",
            "alembic_multi_head",
            detail=f"fix_job={fix_job.id}",
        )
    except Exception as exc:
        log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)

    # h. Notify owner.
    await notify(
        job.chat_id,
        f"ℹ️ Job #{job.id} failed at deploy — multiple alembic heads detected. Created "
        f"fix-forward job #{fix_job.id} to write the merge revision.",
        project_id=job.project_id,
        job_id=job.id,
    )
    log.info(
        "janitor: created alembic-merge-fix job %s for failed deploy job %s", fix_job.id, job.id
    )


async def remediate_dependency_blocked(
    jobs: JobStore,
    job: Job,
    config,
    notify: Notify,
    *,
    dead_letter_cap: int = 5,
    failure_class: str = "dependency_blocked",
) -> None:
    """Auto-requeue a dependency-blocked job once every dependency has landed.

    _reconcile_blocked_dependents fails a job when its dependency dies; if that
    dependency (or its replacement fix) later completes, no human should have to
    requeue the dependent by hand — job #543 needed exactly that today. Jobs
    whose deps are still unsatisfied are left alone.
    """
    if job.resolution:
        # A terminal job may be manually resolved (or resolved by a successful
        # replacement) so its dependents can continue.  It must not itself be
        # resurrected when its own prerequisites later become satisfied.
        return
    unsatisfied = jobs.get_unsatisfied_deps(job.id)
    if unsatisfied and jobs.dependency_block_still_applies(job):
        return  # still genuinely blocked on its recorded dependency
    chain_valid_again = bool(unsatisfied)
    count = jobs.supervisor_requeue_count(job.id)
    if count >= dead_letter_cap:
        if await _consult_analyst_before_dead_letter(
            jobs,
            job,
            config,
            notify,
            cls=FailureClass(failure_class),
            count=count,
            cap=dead_letter_cap,
        ):
            return
        if not jobs.is_supervisor_notified(job.id):
            await notify(
                job.chat_id,
                f"⚠️ Job #{job.id} keeps failing after its dependencies landed "
                f"({count}/{dead_letter_cap} requeues) — giving up.",
                project_id=job.project_id,
                job_id=job.id,
                event_type="needs_attention",
                reason=failure_class,
            )
            jobs.mark_supervisor_notified(job.id)
        with contextlib.suppress(Exception):
            _record_dead_letter_once(jobs, job.id, failure_class, f"cap={dead_letter_cap}")
        return
    jobs.increment_supervisor_requeue(job.id)
    jobs.requeue_job(job.id, zero_attempts=True)
    status_phrase = "chain valid again" if chain_valid_again else "deps satisfied"
    with contextlib.suppress(Exception):
        jobs.record_supervisor_event(
            job.id,
            "requeued",
            failure_class,
            detail=f"{status_phrase}; attempt {count + 1}/{dead_letter_cap}",
        )
    notify_phrase = (
        "its dependency chain is valid again"
        if chain_valid_again
        else "its blocking dependencies have landed"
    )
    await notify(
        job.chat_id,
        f"🔁 Job #{job.id}: {notify_phrase} — requeued automatically.",
        project_id=job.project_id,
        job_id=job.id,
    )
    log.info("janitor: dependency_blocked job %s requeued (%s)", job.id, status_phrase)


async def unblock_ready_dependents(jobs: JobStore, job_id: int, config, notify: Notify) -> None:
    """Re-check a job's dependents the moment it reaches DONE.

    remediate_dependency_blocked only ran from the periodic janitor scan
    (~120s cadence), so a dependent stayed FAILED for up to two minutes after
    its blocking dependency actually recovered — job #1410 stayed blocked
    after #1422 was retried and completed. Calling this immediately at every
    DONE call site heals the graph without waiting for the next scan; the
    janitor scan remains a backstop for anything this misses.
    """
    for dep in jobs.get_dependent_jobs(job_id):
        if (
            dep.status == JobStatus.FAILED
            and classify_failure(dep) == FailureClass.dependency_blocked
        ):
            await remediate_dependency_blocked(jobs, dep, config, notify)


async def remediate_blocked_dependent(
    jobs: JobStore,
    job: Job,
    dep_id: int,
    *,
    reason: str,
    notify: Notify,
) -> None:
    """Fail a job whose dependency has reached an unrecoverable terminal state.

    claim()/claimable() only clear a dependency once it reaches DONE, so a
    dependency that ends up FAILED-with-no-retry or archived would otherwise
    leave the dependent pending forever. Mirrors the dead-letter notify pattern
    in remediate_stale_branch: notify the owner once, tracked via
    is_supervisor_notified/mark_supervisor_notified.
    """
    job.status = JobStatus.FAILED
    job.failure = f"blocked: dependency #{dep_id} {reason} and can no longer complete"
    job.executing_step = None
    job.failed_step = "dependency"
    job.failure_code = "dependency_blocked"
    job.failure_origin = "dependency"
    job.retry_disposition = "await_dependency"
    job.failure_detail = {"dependency_id": dep_id, "reason": reason}
    jobs.save(job)
    try:
        jobs.record_supervisor_event(
            job.id,
            "blocked_by_dependency",
            "dependency_blocked",
            detail=f"dep_id={dep_id} reason={reason}",
        )
    except Exception as exc:
        log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
    if not jobs.is_supervisor_notified(job.id):
        await notify(
            job.chat_id,
            f"⚠️ Job #{job.id} can never run — dependency #{dep_id} {reason} and can "
            f"no longer complete. Marked FAILED.",
            project_id=job.project_id,
            job_id=job.id,
        )
        jobs.mark_supervisor_notified(job.id)
    log.info(
        "janitor: job %s marked FAILED — blocked on dependency %s (%s)", job.id, dep_id, reason
    )


async def _repoint_split_orphans(jobs: JobStore, config, notify: Notify) -> None:
    """Repoint dependents still edged to a superseded-by-split parent, every pass.

    ``list_split_parents_with_pending_dependents``/``repoint_split_dependents``
    previously only ran once, from ``runner.py``'s ``_reconcile_startup`` at
    process boot. A dependent whose edge went stale *after* startup (e.g. the
    split parent is archived later, or a split child dead-letters after boot)
    had no periodic path back to a live edge, so it fell into
    ``_reconcile_blocked_dependents``'s ``dep.archived`` branch and stayed
    pending on a dead dependency even though a live split lineage existed.
    Running this every janitor pass, before that check, means the
    ``archived`` branch only ever fires for a genuinely replacement-less
    dependency.
    """
    for entry in jobs.list_split_parents_with_pending_dependents():
        parent_id = entry["parent_id"]
        child_ids = entry["child_ids"]
        repointed = jobs.repoint_split_dependents(parent_id, child_ids)
        for dep_id in repointed:
            with contextlib.suppress(Exception):
                jobs.add_event(
                    dep_id,
                    "plan",
                    "info",
                    summary=f"dependency #{parent_id} re-pointed to split children {child_ids}",
                    detail={"repointed_from": parent_id, "repointed_to": child_ids},
                )
            job = jobs.get(dep_id)
            if (
                job is not None
                and job.status == JobStatus.FAILED
                and classify_failure(job) == FailureClass.dependency_blocked
            ):
                await remediate_dependency_blocked(jobs, job, config, notify)


async def _reconcile_blocked_dependents(jobs: JobStore, notify: Notify) -> None:
    """Fail or alert on PENDING jobs whose dependency has become terminal.

    A dependency still eligible for automatic or human-directed remediation is
    left alone. A dependency FAILED with a durable ``dead_lettered`` supervisor
    decision is genuinely terminal and fails the dependent. An archived
    dependency is not: a human may restore or re-point it, so the dependent is
    only alerted (throttled) and stays PENDING — it heals on its own the next
    time ``remediate_dependency_blocked`` sees the recorded blocker no longer
    applies. Notifications are deliberately insufficient: they can precede an
    analyst repair/requeue, which caused job #3515's entire chain to fail while
    recovery was active. ``_janitor_scan`` runs ``_repoint_split_orphans``
    before this function on every pass, so the ``dep.archived`` branch below
    only fires for a dependency with no live/completed split lineage.
    """
    for job in jobs.list_pending_with_unsatisfied_deps():
        for dep_id in jobs.get_unsatisfied_deps(job.id):
            dep = jobs.get(dep_id)
            if dep is None:
                continue
            if dep.archived:
                await _alert_archived_dependency(jobs, notify, job, dep)
                break
            dependency_terminal = dep.failure_code == "dependency_blocked" or (
                jobs.has_supervisor_event(dep.id, "dead_lettered") is True
            )
            if dep.status == JobStatus.FAILED and dependency_terminal:
                await remediate_blocked_dependent(jobs, job, dep_id, reason="failed", notify=notify)
                break


_JOB_CONTAINER_RE = re.compile(r"^job[-_]?(\d+)")


def _sweep_candidates(containers: list[tuple[str, str]], active_job_ids: set[int]) -> list[str]:
    """Pure: which container names the docker sweep should remove.

    - ``jobN…`` containers (agent-created test containers): removed when job N
      is no longer active. A crash-looping one for an inactive job sat
      restarting every 45s for hours (job403-test-accountant).
    - ``…-probe…`` containers: removed when not currently Up — a live probe
      belongs to an in-flight deploy and is left alone; anything exited,
      created, or restart-looping is debris from a crashed deploy.
    """
    out: list[str] = []
    for name, status in containers:
        m = _JOB_CONTAINER_RE.match(name)
        if m:
            if int(m.group(1)) not in active_job_ids:
                out.append(name)
            continue
        if (name.endswith("-probe") or "-probe-" in name) and not status.startswith("Up"):
            out.append(name)
    return out


async def _docker_sweep(jobs: JobStore) -> None:
    """Remove stray job/probe containers left behind by agents and crashed deploys."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "ps",
            "-a",
            "--format",
            "{{.Names}}\t{{.Status}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return
    except Exception:  # noqa: BLE001 - docker may not be installed
        return
    containers = []
    for line in out.decode(errors="replace").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            containers.append((parts[0].strip(), parts[1].strip()))
    candidates = _sweep_candidates(containers, jobs.get_active_job_ids())
    for name in candidates:
        log.warning("janitor: removing stray container %r", name)
        await docker_deploy._remove_container(name)


def _image_prune_candidates(images: list[str], active_job_ids: set[int]) -> list[str]:
    """Pure: which job-tagged image names the janitor should ``docker image rm``.

    Mirrors ``_sweep_candidates``: an image named ``job-<N>-...`` (e.g.
    ``job-403-frontend``) is a candidate once job N is no longer active.
    Non job-prefixed images (``postgres:16``, ``hyqs-web``) are never touched.
    """
    out: list[str] = []
    for name in images:
        m = _JOB_CONTAINER_RE.match(name)
        if m and int(m.group(1)) not in active_job_ids:
            out.append(name)
    return out


_BUILD_CACHE_PRUNE_META_KEY = "docker_builder_prune:last_pruned_at"
_BUILD_CACHE_PRUNE_INTERVAL_SECONDS = 6 * 3600  # heavier than the 120s scan cadence


def _builder_prune_due(last_pruned: str, now: float, *, interval_seconds: float) -> bool:
    """Pure timestamp check: is it time to run ``docker builder prune`` again?"""
    try:
        last_ts = float(last_pruned)
    except ValueError:
        return True
    return (now - last_ts) >= interval_seconds


async def _docker_builder_prune(jobs: JobStore) -> None:
    """Prune the docker build cache on an interval, filtered by age.

    The build cache grew unbounded to 133.2GB and drove host disk usage to
    82% before this existed. ``until=72h`` skips cache from an in-flight or
    very recent build so this never yanks something mid-use.
    """
    last = jobs.get_meta(_BUILD_CACHE_PRUNE_META_KEY, "0")
    if not _builder_prune_due(
        last, time.time(), interval_seconds=_BUILD_CACHE_PRUNE_INTERVAL_SECONDS
    ):
        return
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "builder",
                "prune",
                "-af",
                "--filter",
                "until=72h",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            if proc.returncode == 0:
                log.info(
                    "janitor: docker builder prune reclaimed: %s",
                    out.decode(errors="replace").strip(),
                )
        except Exception:  # noqa: BLE001 - docker may not be installed / best-effort
            log.exception("janitor: docker builder prune subprocess failed")
    finally:
        # Record the attempt (success or failure) so a failing docker call
        # doesn't retry every 120s.
        jobs.set_meta(_BUILD_CACHE_PRUNE_META_KEY, str(time.time()))


async def _docker_image_prune(jobs: JobStore) -> None:
    """Remove job-<N>-* images once job N is no longer active."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "images",
            "--format",
            "{{.Repository}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return
    except Exception:  # noqa: BLE001 - docker may not be installed
        return
    images = [line.strip() for line in out.decode(errors="replace").splitlines() if line.strip()]
    candidates = _image_prune_candidates(images, jobs.get_active_job_ids())
    for name in candidates:
        log.warning("janitor: removing stray image %r", name)
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "image",
                "rm",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception:  # noqa: BLE001 - best-effort image removal
            log.exception("janitor: failed to remove image %r", name)


async def _run_analyst_diagnosis(
    jobs: JobStore,
    job: Job,
    cls: "FailureClass",
    config,
    notify: Notify,
    *,
    note: str | None = None,
) -> tuple[bool, bool]:
    """Run the AI incident analyst on one escalated job; execute a whitelisted action.

    Advisory only: the analyst recommends, this function validates against the
    closed menu and executes deterministically. Skipped when Claude is paused
    (the control plane never depends on an unavailable provider), when the job
    was already diagnosed, or when confidence is low.

    ``note``, when given, is prefixed onto the failure-class evidence handed to
    the analyst (e.g. to state a deterministic remediation already ran and
    failed) without changing the recorded ``cls`` identity used for events.

    Returns ``(ran, productive)``: ``ran`` is True when a diagnosis RAN
    (successful or not), mirroring this function's historical return value, so
    the caller can budget per scan. ``productive`` is True only when the
    analyst's action actually resolves the *current* job (a requeue_at_stage
    under its own requeue budget, or a filed fix that reaches
    ``jobs.requeue_job(job.id, zero_attempts=True)``).
    """
    from . import incident_analyst
    from .providers import build_backend

    job_cap = int(getattr(config, "pipeline_incident_analyst_job_cap", 6) or 6)
    if jobs.count_supervisor_events(job.id, "ai_diagnosed") >= job_cap:
        return False, False
    paused = jobs.paused_providers(time.time())
    configured_provider = (
        str(getattr(config, "pipeline_incident_analyst_provider", "auto") or "auto").strip().lower()
    )
    if configured_provider == "auto":
        provider = next(
            (candidate for candidate in ("claude", "codex") if candidate not in paused), None
        )
    else:
        provider = configured_provider if configured_provider not in paused else None
    if provider is None:
        with contextlib.suppress(Exception):
            jobs.record_supervisor_event(
                job.id,
                "analyst_deferred",
                cls.value,
                detail=json.dumps(
                    {
                        "reason": "no_healthy_provider",
                        "configured_provider": configured_provider,
                        "paused_providers": sorted(paused),
                    }
                ),
            )
        return False, False

    events = jobs.list_events(job.id)
    model = (
        config.pipeline_incident_analyst_model
        if provider == "claude"
        else str(getattr(config, "codex_model", "") or "")
    )
    backend = build_backend(provider, model, config=config)
    failure_class_evidence = f"{cls.value} — {note}" if note else cls.value
    try:
        data, usage = await asyncio.wait_for(
            incident_analyst.diagnose(backend, job, failure_class_evidence, events), timeout=600
        )
    except Exception as exc:  # noqa: BLE001 - malformed output / timeout = no action
        with contextlib.suppress(Exception):
            jobs.record_supervisor_event(
                job.id, "ai_diagnosed", cls.value, detail=f"diagnosis_failed={exc}"
            )
        return True, False
    with contextlib.suppress(Exception):
        jobs.record_usage("incident-analyst", usage, job.id)

    action = data.get("action") or {}
    confidence = float(data.get("confidence") or 0.0)
    diagnosis = str(data.get("diagnosis") or "")[:600]
    ok, why = incident_analyst.validate_action(action)
    detail = json.dumps(
        {"diagnosis": diagnosis, "action": action, "confidence": confidence, "valid": ok}
    )[:3500]
    with contextlib.suppress(Exception):
        jobs.record_supervisor_event(job.id, "ai_diagnosed", cls.value, detail=detail)

    executed = "escalate (no automated action)"
    gave_up = False
    productive = False
    if ok and confidence >= 0.7 and action.get("type") == "requeue_at_stage":
        if jobs.supervisor_requeue_count(job.id) < 5:
            jobs.increment_supervisor_requeue(job.id)
            jobs.requeue_job_at_stage(
                job.id, incident_analyst.stage_from_action(action), failure=job.failure or ""
            )
            executed = f"requeued at {action['stage']}"
            productive = True
    elif ok and confidence >= 0.7 and action.get("type") == "repair_in_place":
        if not job.branch:
            executed = "escalated: in-place repair requires an existing branch"
        elif jobs.supervisor_requeue_count(job.id) >= 5:
            executed = "escalated: in-place repair retry budget exhausted"
            gave_up = True
        else:
            source_meta = copy.deepcopy(job.source_meta or {})
            scope = dict(source_meta.get("scope") or {})
            existing_paths = list(scope.get("allowed_paths") or [])
            added_paths = [path for path in action["allowed_paths"] if path not in existing_paths]
            scope["allowed_paths"] = [*existing_paths, *added_paths]
            scope["frozen"] = True
            source_meta["scope"] = scope
            history = list(source_meta.get("in_place_repairs") or [])
            history.append(
                {
                    "failure_class": cls.value,
                    "idea": action["idea"],
                    "added_paths": added_paths,
                }
            )
            source_meta["in_place_repairs"] = history[-5:]
            job.source_meta = source_meta
            jobs.save(job)
            jobs.increment_supervisor_requeue(job.id)
            repair_failure = (
                f"{job.failure or job.error or 'Remediation required.'}\n\n"
                f"Supervisor-authorized in-place repair:\n{action['idea']}"
            )
            jobs.requeue_job_at_stage(job.id, Stage.FIX, failure=repair_failure)
            with contextlib.suppress(Exception):
                jobs.record_supervisor_event(
                    job.id,
                    "scope_expanded",
                    cls.value,
                    detail=json.dumps({"added_paths": added_paths, "stage": "fix"}),
                )
            executed = f"continued #{job.id} at fix with audited scope expansion" + (
                f" ({len(added_paths)} path(s))" if added_paths else ""
            )
            productive = True
    elif ok and confidence >= 0.7 and action.get("type") in ("file_fix_job", "file_fix_jobs"):
        source_branch = job.branch or ""
        source_sha = ""
        if source_branch:
            source_worktree = Path(config.data_dir) / "worktrees" / f"job-{job.id}"
            source_context = source_worktree if source_worktree.exists() else Path(job.repo_path)
            source_head = await gitops.git(source_context, "rev-parse", "HEAD")
            recorded_branch = await gitops.git(source_context, "branch", "--show-current")
            if (
                not source_head.ok
                or not re.fullmatch(r"[0-9a-fA-F]{40}", source_head.stdout)
                or not recorded_branch.ok
                or recorded_branch.stdout != source_branch
            ):
                jobs.record_supervisor_event(
                    job.id,
                    "remediation_source_unavailable",
                    cls.value,
                    detail=json.dumps(
                        {
                            "source_branch": source_branch,
                            "reason": "failed candidate branch or immutable SHA unavailable",
                        }
                    ),
                )
                return True, False
            source_sha = source_head.stdout
        lineage = jobs.resolve_remediation_lineage(job.id)
        # Keep lightweight/older JobStore test doubles compatible while the
        # real store always returns the typed lineage.
        if not isinstance(lineage, RemediationLineage):
            lineage = RemediationLineage(job.id, 0)
        attempted_depth = lineage.depth + 1
        if attempted_depth > MAX_AUTOMATED_REMEDIATION_DEPTH:
            escalation = {
                "incident_job_id": job.id,
                "remediation_root_job_id": lineage.root_job_id,
                "current_depth": lineage.depth,
                "attempted_depth": attempted_depth,
                "limit": MAX_AUTOMATED_REMEDIATION_DEPTH,
                "reason": "automated_remediation_depth_exhausted",
            }
            jobs.record_supervisor_event(
                job.id, "remediation_escalated", cls.value, detail=json.dumps(escalation)
            )
            executed = (
                f"escalated for human judgment: remediation root #{lineage.root_job_id} "
                f"reached depth limit {MAX_AUTOMATED_REMEDIATION_DEPTH}"
            )
            gave_up = True
        else:
            entries = (
                action["jobs"]
                if action["type"] == "file_fix_jobs"
                else [{"idea": action["idea"], "covers_stories": action.get("covers_stories")}]
            )
            augmented_deps = remediation.compute_chain_dependencies(entries)
            entry_target_files = [
                collision.extract_scope_from_idea(entry.get("idea", "")) for entry in entries
            ]
            queue_survey = (
                jobs.survey_active_job_queue(
                    job.project_id,
                    [
                        {
                            "key": str(index),
                            "title": entries[index].get("idea", "")[:80],
                            "target_files": files,
                        }
                        for index, files in enumerate(entry_target_files)
                    ],
                )
                if job.project_id is not None
                else None
            )
            queue_survey_data = queue_survey.to_dict() if queue_survey is not None else None
            lineage_excluded_job_ids = sorted({job.id, lineage.root_job_id})
            try:
                tracked = await gitops.tracked_files(job.repo_path)
            except Exception:
                log.warning(
                    "tracked_files failed while preparing remediation for job %s",
                    job.id,
                    exc_info=True,
                )
                tracked = []
            symbol_index = (
                jobs.get_symbol_index_names(job.project_id) if job.project_id is not None else {}
            )
            specs = []
            for index, entry in enumerate(entries):
                evidence = remediation.collect_remediation_candidate_evidence(
                    parent_idea=job.idea,
                    parent_plan=job.plan,
                    covers_stories=entry.get("covers_stories"),
                    failure_text=job.failure or job.error or "",
                    failure_detail=job.failure_detail,
                    reviews=[job.review, job.security_review, job.design_review],
                    events=events,
                )
                implication_text = " ".join(
                    (
                        cls.value,
                        job.stage.value,
                        job.failed_step or "",
                        job.failure_code or "",
                        job.failure_origin or "",
                        json.dumps(job.failure_detail or {}, sort_keys=True),
                        json.dumps(job.review or {}, sort_keys=True),
                    )
                ).lower()
                layer_paths = {
                    "execution_layer": ["hyqs/pipeline/runner.py", "hyqs/pipeline/agents.py"],
                    "persistence_layer": ["hyqs/pipeline/store.py", "hyqs/pipeline/models.py"],
                    "requeue_layer": ["hyqs/pipeline/supervisor.py", "hyqs/pipeline/store.py"],
                }
                if any(word in implication_text for word in ("execute", "runner", "stage")):
                    evidence["execution_layer"] = layer_paths["execution_layer"]
                if any(
                    word in implication_text
                    for word in ("persist", "database", "store", "postgres", "save")
                ):
                    evidence["persistence_layer"] = layer_paths["persistence_layer"]
                if any(
                    word in implication_text
                    for word in ("requeue", "retry", "lease", "dependency", "blocked")
                ):
                    evidence["requeue_layer"] = layer_paths["requeue_layer"]
                candidates = collision.build_planning_candidates(
                    tracked,
                    symbol_index,
                    job.idea,
                    evidence_paths=evidence,
                )
                source_meta = {
                    "ai_fix_for": job.id,
                    "failure_class": cls.value,
                    "chain_index": index,
                    "covers_stories": entry.get("covers_stories") or [],
                    "remediation_root_job_id": lineage.root_job_id,
                    "remediation_depth": attempted_depth,
                }
                queue_dependency_ids: list[int] = []
                if queue_survey_data is not None:
                    overlapping_ids = sorted(queue_survey_data["overlaps"].get(str(index)) or [])
                    unknown_scope_ids = sorted(queue_survey_data["unknown_target_file_jobs"])
                    queue_dependency_ids = sorted(
                        (set(overlapping_ids) | set(unknown_scope_ids))
                        - set(lineage_excluded_job_ids)
                    )
                    source_meta["queue_survey"] = {
                        "overlapping_job_ids": overlapping_ids,
                        "unknown_target_file_jobs": unknown_scope_ids,
                        "excluded_lineage_job_ids": lineage_excluded_job_ids,
                        "depends_on_job_ids": queue_dependency_ids,
                    }
                specs.append(
                    {
                        "idea": remediation.build_ai_fix_idea(
                            job.id,
                            job.stage.value,
                            job.failure or job.error or "",
                            entry["idea"],
                            (job.plan or {}).get("stories"),
                            entry.get("covers_stories"),
                            candidates,
                        ),
                        "source_meta": source_meta,
                        "depends_on_indexes": augmented_deps[index],
                        "depends_on_job_ids": queue_dependency_ids,
                    }
                )
            create_kwargs = {"failure_class": cls.value}
            if source_branch and source_sha:
                create_kwargs.update(source_branch=source_branch, source_sha=source_sha)
            result = jobs.create_supervisor_remediation(job.id, specs, **create_kwargs)
            if not isinstance(result, SupervisorRemediationResult):
                active_id = jobs.get_active_remediation(job.id)
                active_job = jobs.get(active_id) if active_id is not None else None
                if active_job is not None and active_job.status not in (
                    JobStatus.DONE,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                ):
                    result = SupervisorRemediationResult(
                        [], lineage, active_remediation_job_id=active_id
                    )
                else:
                    created = []
                    for spec in specs:
                        depends_on = [created[index].id for index in spec["depends_on_indexes"]]
                        depends_on.extend(spec.get("depends_on_job_ids") or [])
                        depends_on = sorted(set(depends_on))
                        created_job = jobs.create(
                            idea=spec["idea"],
                            repo_path=job.repo_path,
                            chat_id=job.chat_id,
                            epic_id=job.epic_id,
                            priority=50,
                            source=JobSource.SUPERVISOR,
                            source_actor="incident-analyst",
                            source_meta=spec["source_meta"],
                            depends_on=depends_on,
                        )
                        created.append(created_job)
                    jobs.add_job_dependency(job.id, created[-1].id)
                    jobs.set_active_remediation(job.id, created[-1].id)
                    result = SupervisorRemediationResult(created, lineage)
            if result.active_remediation_job_id is not None:
                conflict = {
                    "incident_job_id": job.id,
                    "remediation_root_job_id": result.lineage.root_job_id,
                    "current_depth": result.lineage.depth,
                    "attempted_depth": attempted_depth,
                    "limit": MAX_AUTOMATED_REMEDIATION_DEPTH,
                    "reason": "active_remediation_exists",
                    "active_remediation_job_id": result.active_remediation_job_id,
                }
                jobs.record_supervisor_event(
                    job.id, "remediation_escalated", cls.value, detail=json.dumps(conflict)
                )
                executed = remediation.format_conflict_reason(
                    job.id, result.active_remediation_job_id
                )
            elif result.jobs:
                jobs.requeue_job(job.id, zero_attempts=True)
                productive = True
                created = result.jobs
                if len(created) == 1:
                    executed = (
                        f"filed fix job #{created[0].id}; #{job.id} auto-requeues when it lands"
                    )
                else:
                    executed = (
                        f"filed fix chain #{created[0].id}..#{created[-1].id} "
                        f"({len(created)} jobs); #{job.id} auto-requeues when the chain lands"
                    )
    elif not ok:
        log.info("janitor: analyst action rejected for job %s: %s", job.id, why)

    await notify(
        job.chat_id,
        f"🩺 Job #{job.id} AI diagnosis ({cls.value}, confidence {confidence:.0%}): "
        f"{diagnosis[:300]}\n→ {executed}",
        project_id=job.project_id,
        job_id=job.id,
        event_type="needs_attention" if gave_up else None,
        reason=cls.value if gave_up else None,
    )
    return True, productive


async def _ai_diagnose_and_act(
    jobs: JobStore, job: Job, cls: "FailureClass", config, notify: Notify
) -> bool:
    """Run the AI incident analyst on one escalated job; execute a whitelisted action.

    Thin wrapper around ``_run_analyst_diagnosis`` preserving the historical
    contract: returns True when a diagnosis RAN (successful or not).
    """
    ran, _productive = await _run_analyst_diagnosis(jobs, job, cls, config, notify)
    return ran


async def _consult_analyst_before_dead_letter(
    jobs: JobStore,
    job: Job,
    config,
    notify: Notify,
    *,
    cls: "FailureClass",
    count: int,
    cap: int,
) -> bool:
    """Give the AI incident analyst one look before a deterministic remediation
    path dead-letters ``job``, since the deterministic hypothesis has been
    positively disproven by now. Returns True only when the analyst's action
    productively resolved the job (so the caller should skip dead-lettering).
    Never raises: any failure here (disabled toggle, broken config, provider
    error) falls back to the existing, unchanged dead-letter path.
    """
    if notify is None:
        return False
    if jobs.has_supervisor_event(job.id, "dead_lettered") is True:
        return False
    if job.stage is Stage.PLAN and await _split_from_saved_plan(jobs, job, notify):
        return True
    if not getattr(config, "pipeline_incident_analyst_predeadletter", True):
        return False
    note = f"deterministic remediation ({cls.value}) exhausted after {count}/{cap} attempts"
    try:
        _ran, productive = await _run_analyst_diagnosis(jobs, job, cls, config, notify, note=note)
    except Exception:  # noqa: BLE001 - never block the existing dead-letter path
        log.exception("janitor: pre-dead-letter analyst consult failed for job %s", job.id)
        return False
    return productive


async def _split_from_saved_plan(jobs: JobStore, job: Job, notify: Notify) -> bool:
    """Supersede a repeatedly failing PLAN job using its last validated plan."""
    from .stages.plan import _MAX_PLAN_TARGET_FILES, _compute_split_chains

    stories = (job.plan or {}).get("stories") or []
    if not stories or (
        len(stories) == 1 and len(stories[0].get("target_files") or []) <= _MAX_PLAN_TARGET_FILES
    ):
        return False

    terminal_ids: dict[str, str] = {}
    for story in stories:
        story_id = story.get("id", "?")
        file_count = len(story.get("target_files") or [])
        batch_count = max(1, (file_count + _MAX_PLAN_TARGET_FILES - 1) // _MAX_PLAN_TARGET_FILES)
        terminal_ids[story_id] = story_id if batch_count == 1 else f"{story_id}.{batch_count}"
    bounded_stories: list[dict] = []
    for story in stories:
        story_id = story.get("id", "?")
        files = list(story.get("target_files") or [])
        batches = [
            files[index : index + _MAX_PLAN_TARGET_FILES]
            for index in range(0, len(files), _MAX_PLAN_TARGET_FILES)
        ] or [[]]
        for index, batch in enumerate(batches, 1):
            bounded = copy.deepcopy(story)
            if len(batches) > 1:
                bounded["id"] = f"{story_id}.{index}"
                bounded["title"] = f"{story.get('title', '')} — batch {index}/{len(batches)}"
            bounded["target_files"] = batch
            if index == 1:
                bounded["depends_on"] = [
                    terminal_ids.get(dependency, dependency)
                    for dependency in story.get("depends_on") or []
                ]
            else:
                bounded["depends_on"] = [f"{story_id}.{index - 1}"]
            bounded_stories.append(bounded)
    bounded_plan = copy.deepcopy(job.plan or {})
    bounded_plan["stories"] = bounded_stories
    components = _compute_split_chains(bounded_plan)
    if not components:
        return False
    created: list[Job] = []
    for component in components:
        ids_by_story: dict[str, int] = {}
        for story in component["stories"]:
            story_id = story.get("id", "?")
            dependency_ids = [
                ids_by_story[predecessor]
                for predecessor in component["predecessors"].get(story_id, [])
            ]
            child = jobs.create(
                idea=(
                    f"{story.get('title', '')}\n\n"
                    f"Task: {story.get('task', '')}\n"
                    f"Acceptance: {story.get('acceptance', '')}\n"
                    f"Target files: {', '.join(story.get('target_files') or [])}"
                ),
                repo_path=job.repo_path,
                chat_id=job.chat_id,
                epic_id=job.epic_id,
                source=JobSource.SUPERVISOR,
                source_actor="saved-plan-split",
                source_meta={"split_from": job.id, "story_id": story_id},
                depends_on=dependency_ids or None,
            )
            ids_by_story[story_id] = child.id
            created.append(child)
    child_ids = [child.id for child in created]
    jobs.repoint_split_dependents(job.id, child_ids)
    if not jobs.supersede_failed_with_split(job.id, child_ids):
        raise RuntimeError(f"failed to supersede PLAN job #{job.id} after saved-plan split")
    jobs.record_supervisor_event(
        job.id,
        "saved_plan_split",
        FailureClass.transient.value,
        detail=json.dumps({"child_job_ids": child_ids}),
    )
    await notify(
        job.chat_id,
        f"✂️ Job #{job.id}: repeated planning failed; reused its saved plan and split it "
        f"into jobs #{child_ids[0]}..#{child_ids[-1]}.",
        project_id=job.project_id,
        job_id=job.id,
    )
    return True


async def _sweep_deploying_jobs(jobs: JobStore, notify: Notify) -> None:
    """Finalize DEPLOYING jobs stranded between pipeline restarts.

    ``_reconcile_startup`` only runs once at process boot, so a self-deploy job
    superseded by a later merge — or one whose lease simply expires without a
    restart happening at all — stays wedged in DEPLOYING forever unless
    something re-checks it periodically. This runs on the regular janitor
    cadence and reuses the same ancestor-aware finalize logic as startup.
    """
    for job in jobs.get_deploying_jobs():
        if not job.deployed_commit or job.lease_until > time.time():
            continue
        try:
            finalized = await deploy.reconcile_deploying_job(jobs, job)
        except Exception:
            log.exception("janitor: error reconciling stranded DEPLOYING job %s", job.id)
            continue
        if finalized:
            log.info(
                "janitor: reconciled stranded DEPLOYING job %s to DONE (lease expired)", job.id
            )
            await notify(
                job.chat_id,
                f"✅ Job #{job.id}: deployment verified live. Done.",
                project_id=job.project_id,
                job_id=job.id,
            )
            await unblock_ready_dependents(jobs, job.id, None, notify)


_STALE_JOB_NOTIFY_PREFIX = "stale_job_notified:"
_DEPENDENCY_CYCLE_NOTIFY_PREFIX = "dependency_cycle_notified:"
_DEPENDENCY_CYCLE_COOLDOWN_SECONDS = 12 * 3600
_ARCHIVED_DEPENDENCY_NOTIFY_PREFIX = "archived_dependency_notified:"
_ARCHIVED_DEPENDENCY_COOLDOWN_SECONDS = 12 * 3600
_DEPENDENCY_CYCLE_REPAIR_BUDGET = 25  # per-scan cap on edge removals
_CYCLE_REPAIR_RULE_REMEDIATION = "remediation_own_incident"
_CYCLE_REPAIR_RULE_QUEUE_SURVEY = "queue_survey_edge"
# Keys, in priority order, whose presence on a job's source_meta identifies it
# as an automated fix filed for the job id stored under that key.
_REMEDIATION_FIX_FOR_KEYS: tuple[str, ...] = (
    "ai_fix_for",
    "deploy_fix_for",
    "alembic_merge_fix_for",
    "fix_for",
    "fixes_job_id",
)


def _dependency_cycles(graph: dict[int, list[int]]) -> list[tuple[int, ...]]:
    """Return deterministic cyclic strongly connected components in O(V+E)."""
    reverse = {job_id: [] for job_id in graph}
    for job_id, dependencies in graph.items():
        for dependency_id in dependencies:
            reverse[dependency_id].append(job_id)

    visited: set[int] = set()
    finish_order: list[int] = []
    for start in sorted(graph):
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, 0)]
        while stack:
            job_id, index = stack[-1]
            dependencies = graph[job_id]
            if index < len(dependencies):
                dependency_id = dependencies[index]
                stack[-1] = (job_id, index + 1)
                if dependency_id not in visited:
                    visited.add(dependency_id)
                    stack.append((dependency_id, 0))
                continue
            finish_order.append(job_id)
            stack.pop()

    visited.clear()
    cycles: list[tuple[int, ...]] = []
    for start in reversed(finish_order):
        if start in visited:
            continue
        component: list[int] = []
        visited.add(start)
        stack = [start]
        while stack:
            job_id = stack.pop()
            component.append(job_id)
            for dependent_id in reverse[job_id]:
                if dependent_id not in visited:
                    visited.add(dependent_id)
                    stack.append(dependent_id)
        members = tuple(sorted(component))
        if len(members) > 1 or members[0] in graph[members[0]]:
            cycles.append(members)
    return sorted(cycles)


def _remediation_incident_id(source_meta: dict) -> int | None:
    """The incident job id this job's metadata says it is an automated fix for, if any."""
    for meta_key in _REMEDIATION_FIX_FOR_KEYS:
        value = source_meta.get(meta_key)
        if value is None:
            continue
        try:
            incident_id = int(value)
        except (TypeError, ValueError):
            continue
        if incident_id > 0:
            return incident_id
    return None


def _queue_survey_dependency_ids(source_meta: dict) -> set[int]:
    """The dependency ids this job's own queue survey chose to depend on, if any."""
    queue_survey = source_meta.get("queue_survey")
    if not isinstance(queue_survey, dict):
        return set()
    raw_ids = queue_survey.get("depends_on_job_ids")
    if not isinstance(raw_ids, list):
        return set()
    ids: set[int] = set()
    for raw_id in raw_ids:
        try:
            ids.add(int(raw_id))
        except (TypeError, ValueError):
            continue
    return ids


def _find_repairable_cycle_edge(
    cycle: tuple[int, ...],
    graph: dict[int, list[int]],
    jobs_by_id: dict[int, Job],
) -> tuple[int, int, str] | None:
    """The single deterministic, provenance-proven-safe edge to remove from ``cycle``.

    Only two edge classes ever qualify: a remediation job's edge onto its own
    incident, and a queue-survey-derived edge the dependent job's own metadata
    recorded. Returns (dependent_job_id, dependency_job_id, rule), preferring
    the remediation class and, within a class, the lowest dependent job id.
    Returns None if no edge in the cycle matches either class — callers must
    fail closed rather than remove an unproven or caller-declared edge.
    """
    members = set(cycle)
    remediation_candidates: list[tuple[int, int]] = []
    survey_candidates: list[tuple[int, int]] = []
    for job_id in cycle:
        job = jobs_by_id.get(job_id)
        if job is None:
            continue
        source_meta = job.source_meta or {}
        cycle_dependencies = set(graph.get(job_id, ())) & members
        if not cycle_dependencies:
            continue
        incident_id = _remediation_incident_id(source_meta)
        if incident_id is not None and incident_id in cycle_dependencies:
            remediation_candidates.append((job_id, incident_id))
        for dependency_id in _queue_survey_dependency_ids(source_meta) & cycle_dependencies:
            survey_candidates.append((job_id, dependency_id))
    if remediation_candidates:
        dependent_id, dependency_id = min(remediation_candidates)
        return dependent_id, dependency_id, _CYCLE_REPAIR_RULE_REMEDIATION
    if survey_candidates:
        dependent_id, dependency_id = min(survey_candidates)
        return dependent_id, dependency_id, _CYCLE_REPAIR_RULE_QUEUE_SURVEY
    return None


async def _repair_dependency_cycle(
    jobs: JobStore,
    notify: Notify,
    jobs_by_id: dict[int, Job],
    cycle: tuple[int, ...],
    dependent_id: int,
    dependency_id: int,
    rule: str,
) -> None:
    """Remove one proven-safe edge and leave an auditable trail behind it."""
    jobs.remove_job_dependency(dependent_id, dependency_id)
    detail = json.dumps(
        {
            "cycle": list(cycle),
            "removed_edge": {"job_id": dependent_id, "depends_on_job_id": dependency_id},
            "rule": rule,
        }
    )
    jobs.record_supervisor_event(dependent_id, "dependency_cycle_repaired", rule, detail=detail)
    dependent = jobs_by_id.get(dependent_id)
    members = ", ".join(
        f"Job #{job_id}: {jobs_by_id[job_id].title or '(untitled)'}"
        for job_id in cycle
        if job_id in jobs_by_id
    )
    await notify(
        dependent.chat_id if dependent is not None else 0,
        (
            f"🔧 Dependency cycle auto-repaired: removed job #{dependent_id}'s dependency "
            f"on #{dependency_id} ({rule}). Cycle was: {members}."
        ),
        project_id=dependent.project_id if dependent is not None else None,
        job_id=dependent_id,
        event_type="dependency_cycle_repaired",
        reason=rule,
    )
    log.warning(
        "janitor: auto-repaired dependency cycle %s by removing job %s's dependency on %s (%s)",
        cycle,
        dependent_id,
        dependency_id,
        rule,
    )


async def _alert_dependency_cycle(
    jobs: JobStore, notify: Notify, jobs_by_id: dict[int, Job], cycle: tuple[int, ...]
) -> None:
    """Alert at most every 12h for a cycle that has no provenance-safe repair."""
    key = f"{_DEPENDENCY_CYCLE_NOTIFY_PREFIX}{'-'.join(map(str, cycle))}"
    try:
        last_ts = float(jobs.get_meta(key, "0"))
    except ValueError:
        last_ts = 0.0
    if time.time() - last_ts < _DEPENDENCY_CYCLE_COOLDOWN_SECONDS:
        return
    jobs.set_meta(key, str(time.time()))
    representative = jobs_by_id[cycle[0]]
    members = ", ".join(
        f"Job #{job_id}: {jobs_by_id[job_id].title or '(untitled)'}" for job_id in cycle
    )
    await notify(
        representative.chat_id,
        f"🚨 Dependency cycle detected: {members}. Remove one dependency edge to unblock it.",
        project_id=representative.project_id,
        job_id=representative.id,
        event_type="needs_attention",
        reason="dependency_cycle",
    )
    log.warning("janitor: active dependency cycle detected among jobs %s", cycle)


async def _alert_archived_dependency(jobs: JobStore, notify: Notify, job: Job, dep: Job) -> None:
    """Alert at most every 12h that ``job`` is blocked on an archived dependency.

    Archiving a dependency does not make its dependents unrecoverable — a
    human may re-point or replace the archived job — so the dependent stays
    PENDING instead of being failed. Throttled per archived dependency so N
    dependents of the same archived job produce exactly one notification.
    """
    key = f"{_ARCHIVED_DEPENDENCY_NOTIFY_PREFIX}{dep.id}"
    try:
        last_ts = float(jobs.get_meta(key, "0"))
    except (TypeError, ValueError):
        last_ts = 0.0
    if time.time() - last_ts < _ARCHIVED_DEPENDENCY_COOLDOWN_SECONDS:
        return
    jobs.set_meta(key, str(time.time()))
    await notify(
        job.chat_id,
        f"⚠️ Job #{job.id} is waiting on dependency #{dep.id} ({dep.title or '(untitled)'}), "
        f"which was archived — it will stay pending until the dependency is restored or "
        f"re-pointed.",
        project_id=job.project_id,
        job_id=job.id,
        event_type="needs_attention",
        reason="archived_dependency",
    )
    log.warning(
        "janitor: job %s blocked on archived dependency %s — alerted, not failed", job.id, dep.id
    )


def _build_active_dependency_graph(jobs: JobStore) -> tuple[dict[int, list[int]], dict[int, Job]]:
    active_jobs = jobs.list_active(limit=1_000_000, status="active")
    jobs_by_id = {job.id: job for job in active_jobs}
    active_ids = set(jobs_by_id)
    graph = {
        job_id: sorted(dep_id for dep_id in jobs.get_dependencies(job_id) if dep_id in active_ids)
        for job_id in sorted(active_ids)
    }
    return graph, jobs_by_id


async def _check_dependency_cycles(jobs: JobStore, notify: Notify) -> None:
    """Auto-repair provenance-safe dependency cycles; alert on whatever remains.

    Each repair removes exactly one deterministically chosen edge from one
    cycle, then the whole graph is rebuilt and re-scanned from scratch before
    another edge is considered — never multiple edges off one stale view.
    """
    repairs_left = _DEPENDENCY_CYCLE_REPAIR_BUDGET
    graph, jobs_by_id = _build_active_dependency_graph(jobs)
    cycles = _dependency_cycles(graph)
    while cycles and repairs_left > 0:
        repaired = False
        for cycle in cycles:
            edge = _find_repairable_cycle_edge(cycle, graph, jobs_by_id)
            if edge is None:
                continue
            dependent_id, dependency_id, rule = edge
            await _repair_dependency_cycle(
                jobs, notify, jobs_by_id, cycle, dependent_id, dependency_id, rule
            )
            repairs_left -= 1
            repaired = True
            break
        if not repaired:
            break
        graph, jobs_by_id = _build_active_dependency_graph(jobs)
        cycles = _dependency_cycles(graph)

    for cycle in cycles:
        await _alert_dependency_cycle(jobs, notify, jobs_by_id, cycle)


async def _check_job_staleness(jobs: JobStore, notify: Notify, stale_hours: float) -> None:
    """Alert at most every 12h for each active job older than ``stale_hours``."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    for job in jobs.list_active(limit=1_000_000, status="active"):
        try:
            created = datetime.fromisoformat(job.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_h = (now - created).total_seconds() / 3600
        except (AttributeError, TypeError, ValueError):
            continue
        if age_h < stale_hours:
            continue
        key = f"{_STALE_JOB_NOTIFY_PREFIX}{job.id}"
        try:
            last_ts = float(jobs.get_meta(key, "0"))
        except ValueError:
            last_ts = 0.0
        if time.time() - last_ts < 12 * 3600:
            continue
        jobs.set_meta(key, str(time.time()))
        title = f": {job.title}" if job.title else ""
        epic = f"epic #{job.epic_id}" if job.epic_id is not None else "no epic"
        await notify(
            job.chat_id,
            f"🚨 Job #{job.id}{title} has been active for {age_h:.0f}h ({epic}) and may be stuck.",
            project_id=job.project_id,
            job_id=job.id,
            event_type="needs_attention",
            reason="job_stale",
        )
        log.warning(
            "janitor: job %s in epic %s has been active for %.0fh",
            job.id,
            job.epic_id,
            age_h,
        )


_FLEET_WEDGE_META_KEY = "fleet_wedge_active"
# Liveness window for the `workers` table itself — deliberately independent of
# pipeline_lease_ttl (this is a coarse "is the row still being heartbeated at
# all" check, not the lease-renewal cadence).
_FLEET_LIVENESS_STALE_AFTER = 300.0


async def _check_fleet_liveness(jobs: JobStore, config, notify: Notify) -> None:
    """Alert once per wedge episode when the fleet has zero idle capacity and
    no active job has advanced a stage in a comfortably long window.

    ``reclaim_orphaned_worker_slots`` already frees a slot whose job_id no
    longer matches a running job; this catches what that reclaim cannot fix —
    a slot that legitimately still owns a RUNNING/DEPLOYING job whose stage
    hung and never returned (job #3988: the heartbeat kept renewing
    status='busy' for ~8h while nothing advanced). Deliberately keyed on
    job.updated_at, never on worker heartbeat freshness: a hung stage's
    heartbeat renews right on schedule, which is exactly what made the
    original outage invisible in the fleet view.
    """
    now = time.time()
    workers = jobs.list_workers(now, _FLEET_LIVENESS_STALE_AFTER)
    executors = [w for w in workers if w.get("role") == "executor" and w.get("alive")]
    if not executors or any(w.get("status") == "idle" for w in executors):
        jobs.set_meta(_FLEET_WEDGE_META_KEY, "0")
        return

    from datetime import datetime, timezone

    active = jobs.list_active(limit=1_000_000, status="active")
    oldest_updated = None
    for job in active:
        try:
            updated = datetime.fromisoformat(job.updated_at)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
        except (AttributeError, TypeError, ValueError):
            continue
        if oldest_updated is None or updated < oldest_updated:
            oldest_updated = updated
    if oldest_updated is None:
        jobs.set_meta(_FLEET_WEDGE_META_KEY, "0")
        return

    stalest_seconds = (datetime.now(timezone.utc) - oldest_updated).total_seconds()
    threshold_seconds = float(getattr(config, "pipeline_fleet_wedge_minutes", 5) or 5) * 60
    if stalest_seconds < threshold_seconds:
        jobs.set_meta(_FLEET_WEDGE_META_KEY, "0")
        return

    if jobs.get_meta(_FLEET_WEDGE_META_KEY, "0") == "1":
        return  # already alerted this episode; wait for it to clear before re-alerting
    jobs.set_meta(_FLEET_WEDGE_META_KEY, "1")
    log.error(
        "janitor: fleet wedged — %d executor(s) busy, no active job has advanced in %.0fs",
        len(executors),
        stalest_seconds,
    )
    await notify(
        0,
        f"🚨 Fleet wedged: all {len(executors)} executor slot(s) are busy but no job has "
        f"advanced a stage in {stalest_seconds / 60:.0f}m. Check worker slots vs job status.",
        project_id=None,
        job_id=None,
        event_type="needs_attention",
        reason="fleet_wedged",
    )


async def _janitor_scan(jobs: JobStore, config, notify: Notify) -> None:
    """One pass: classify every FAILED job and apply safe remediations."""
    reclaimed = list(jobs.reclaim_orphaned_worker_slots())
    if reclaimed:
        log.warning("janitor: reclaimed %d orphaned worker slot(s): %s", len(reclaimed), reclaimed)
    await _check_fleet_liveness(jobs, config, notify)
    await _repoint_split_orphans(jobs, config, notify)
    for project in jobs.list_projects():
        jobs.reconcile_auto_dependencies(project["id"])
    stale_hours = float(getattr(config, "pipeline_job_stale_hours", 24) or 24)
    await _check_job_staleness(jobs, notify, stale_hours)
    await _check_dependency_cycles(jobs, notify)

    failed_jobs = jobs.get_failed_jobs()
    # Per-scan budget: caps how many analyst diagnoses this pass may run. This
    # value times _janitor_loop's scan_interval (120s default) is the worst-case
    # rate of analyst invocations — the number an operator should reason about
    # when tuning cost. Raising this frequency ceiling changes only how often the
    # analyst may speak, not what it's allowed to do: the confidence threshold,
    # remediation depth ceiling, requeue ceiling, closed action menu, and the
    # claude-provider-pause skip in _ai_diagnose_and_act are all unchanged.
    ai_diagnoses_left = int(getattr(config, "pipeline_incident_analyst_scan_budget", 8) or 8)
    for job in failed_jobs:
        cls = classify_failure(job)
        try:
            if cls in (FailureClass.stale_branch, FailureClass.orphaned_worktree):
                await remediate_stale_branch(
                    jobs, job, config, failure_class=cls.value, notify=notify
                )
            elif cls == FailureClass.merged_but_stuck:
                reconciled = await remediate_merged_but_stuck(
                    jobs, job, config, failure_class=cls.value
                )
                if reconciled:
                    await unblock_ready_dependents(jobs, job.id, config, notify)
                if not reconciled:
                    if job.stage == Stage.DEPLOY:
                        # PR not merged: fall back to the deploy-specific
                        # env/attributable/config split (not the generic
                        # transient requeue) so container conflicts still get
                        # cleaned up and attributable failures still file a
                        # fix-forward job.
                        deploy_cls = _classify_deploy_failure(job)
                        if deploy_cls == FailureClass.alembic_multi_head:
                            await remediate_alembic_multi_head(jobs, job, config, notify)
                        elif deploy_cls == FailureClass.env_deploy:
                            await remediate_env_deploy(jobs, job, config, notify)
                        else:
                            await remediate_deploy_failed(jobs, job, config, notify)
                    elif (
                        job.stage == Stage.MERGE
                        and _classify_merge_failure(job) == FailureClass.transient
                    ):
                        # PR not merged: a GitHub API 5xx/secondary-rate-limit or a
                        # merge-race "base branch was modified" — neither says
                        # anything about this job's code. Back off and retry on
                        # its own bounded budget instead of the generic transient
                        # path so we can compute a GitHub-outage-appropriate backoff.
                        await remediate_merge_github_transient(jobs, job, config, notify=notify)
                    else:
                        # PR not merged: this is a crash at the review/security/merge
                        # boundary, not a merged-but-unrecorded job. Requeue at the
                        # failed stage on the transient dead-letter budget.
                        await remediate_transient(
                            jobs, job, config, failure_class=cls.value, notify=notify
                        )
            elif cls == FailureClass.transient:
                await remediate_transient(jobs, job, config, failure_class=cls.value, notify=notify)
            elif cls == FailureClass.no_diff_build_retry:
                await remediate_no_diff_build(jobs, job)
            elif cls == FailureClass.gate_no_changes:
                await remediate_gate_no_changes(jobs, job, config, notify)
            elif cls == FailureClass.dependency_blocked:
                await remediate_dependency_blocked(jobs, job, config, notify)
            else:
                # Judgment class: log and alert the chat owner once; do not mutate the job.
                # The warning fires only on the first, not-yet-notified pass — logging it
                # unconditionally on every scan is what buried an 8h fleet-wedge outage
                # under ~493 identical "needs human judgment" lines (job #3988).
                if not jobs.is_supervisor_notified(job.id):
                    log.warning(
                        "janitor: job %s classified as %s at stage %s — needs human judgment",
                        job.id,
                        cls.value,
                        job.stage.value,
                    )
                    await notify(
                        job.chat_id,
                        f"⚠️ Job #{job.id} needs attention: {cls.value} failure at {job.stage.value}",
                        project_id=job.project_id,
                        job_id=job.id,
                        event_type="needs_attention",
                        reason=cls.value,
                    )
                    jobs.mark_supervisor_notified(job.id)
                    try:
                        jobs.record_supervisor_event(
                            job.id,
                            "escalated",
                            cls.value,
                            detail=f"stage={job.stage.value}",
                        )
                    except Exception as exc:
                        log.debug("janitor: record_supervisor_event job %s failed: %s", job.id, exc)
                else:
                    log.debug(
                        "janitor: job %s still classified as %s at stage %s (already notified)",
                        job.id,
                        cls.value,
                        job.stage.value,
                    )
                # AI incident analyst: advisory diagnosis for judgment classes, executed
                # only through the deterministic whitelist. Best-effort and budgeted —
                # a broken/paused provider must never break the janitor.
                if ai_diagnoses_left > 0:
                    try:
                        acted = await _ai_diagnose_and_act(jobs, job, cls, config, notify)
                        if acted:
                            ai_diagnoses_left -= 1
                    except Exception:
                        log.exception("janitor: AI diagnosis failed for job %s", job.id)
        except Exception:
            log.exception("janitor: error remediating job %s (class=%s)", job.id, cls.value)

    await _reconcile_blocked_dependents(jobs, notify)
    await _sweep_deploying_jobs(jobs, notify)

    active_ids = jobs.get_active_job_ids()
    if not isinstance(active_ids, set):
        active_ids = set(active_ids)
    protected_ids = jobs.get_git_artifact_protected_job_ids()
    if not isinstance(protected_ids, set):
        protected_ids = set()
    await gitops.gc_orphaned_artifacts(config, active_ids | protected_ids)

    try:
        pruned = jobs.prune_audit_log(days=90)
        if pruned:
            log.info("janitor: pruned %d audit_log rows older than 90 days", pruned)
    except Exception:
        log.exception("janitor: audit_log prune failed")

    try:
        await _docker_sweep(jobs)
    except Exception:
        log.exception("janitor: docker sweep failed")

    try:
        await _docker_builder_prune(jobs)
    except Exception:
        log.exception("janitor: docker builder prune failed")

    try:
        await _docker_image_prune(jobs)
    except Exception:
        log.exception("janitor: docker image prune failed")


async def _janitor_loop(
    jobs: JobStore, config, notify: Notify, *, scan_interval: int = 120
) -> None:
    """Run _janitor_scan every scan_interval seconds, forever."""
    set_db_actor(f"supervisor:{socket.gethostname()}:{os.getpid()}")
    while True:
        try:
            await _janitor_scan(jobs, config, notify)
        except Exception:
            log.exception("janitor: unexpected scan error")
        await asyncio.sleep(scan_interval)


_STALE_DEPLOY_NOTIFY_PREFIX = "stale_deploy_notified:"


async def _check_deploy_staleness(
    jobs: JobStore, notify: Notify, project_id: int, deployed_at: str, stale_hours: float
) -> None:
    """Alert (deduped, at most every 12h) when a project serves stale code too long.

    Deploy failures each produce a failed job, but nothing used to say "this app
    has been trailing origin/main for hours" — ledger-app froze for 11h on a
    container-name conflict with no fleet-level signal.
    """
    try:
        from datetime import datetime, timezone

        deployed = datetime.fromisoformat(deployed_at)
        age_h = (datetime.now(timezone.utc) - deployed).total_seconds() / 3600
    except (TypeError, ValueError):
        return
    if age_h < stale_hours:
        return
    key = f"{_STALE_DEPLOY_NOTIFY_PREFIX}{project_id}"
    last = jobs.get_meta(key, "0")
    try:
        last_ts = float(last)
    except ValueError:
        last_ts = 0.0
    if time.time() - last_ts < 12 * 3600:
        return
    jobs.set_meta(key, str(time.time()))
    await notify(
        0,
        f"🚨 Project {project_id}: origin advanced but the live deploy is "
        f"{age_h:.0f}h old — deploys appear stuck. Check failed deploy jobs / "
        f"container state.",
        project_id=project_id,
        event_type="needs_attention",
        reason="deploy_stale",
    )
    log.warning(
        "auto_deploy: project %s serving a %.0fh-old deploy while origin is ahead",
        project_id,
        age_h,
    )


async def _auto_deploy_scan(jobs: JobStore, config, notify: Notify | None = None) -> None:
    """Check every auto_deploy environment for undeployed commits on origin/<base>
    and enqueue deploy jobs. Promotion-driven environments (auto_deploy=FALSE) are
    never touched here — they deploy only via promote_release."""
    stale_hours = float(getattr(config, "pipeline_deploy_stale_hours", 6) or 6)
    for env in jobs.list_auto_deploy_environments():
        project = jobs.get_project(env.project_id)
        if project is None or not project.repo_path:
            continue
        project_id = project.id
        repo_path = project.repo_path
        try:
            await gitops.git(repo_path, "fetch", "origin")
        except Exception as exc:
            log.debug("auto_deploy: git fetch failed for project %s: %s", project_id, exc)
            continue
        try:
            base = await gitops.default_branch(repo_path)
            result = await gitops.git(repo_path, "rev-parse", f"origin/{base}")
            if not result.ok:
                log.debug(
                    "auto_deploy: rev-parse origin/%s failed for project %s", base, project_id
                )
                continue
            tip = result.stdout.strip()
        except Exception as exc:
            log.debug("auto_deploy: rev-parse failed for project %s: %s", project_id, exc)
            continue
        last = jobs.get_last_deploy(project_id, environment_id=env.id)
        deployed_commit = (last or {}).get("deployed_commit", "")
        if tip != deployed_commit and notify is not None and last:
            await _check_deploy_staleness(
                jobs, notify, project_id, (last or {}).get("deployed_at", ""), stale_hours
            )
        if tip == deployed_commit:
            log.debug(
                "auto_deploy: project %s env %s already deployed at %s, skipping",
                project_id,
                env.id,
                tip[:12],
            )
            continue
        if jobs.get_active_project_job(project_id) is not None:
            log.debug(
                "auto_deploy: project %s has an active pipeline job that will ship this "
                "advance itself, skipping",
                project_id,
            )
            continue
        # By the time control reaches here with an unchanged tip, any prior attempt
        # at this (env, tip) must have failed: an in-flight attempt is caught by
        # the active-job guard above, and a successful one advances deployed_commit,
        # short-circuiting at the `tip == deployed_commit` check above. So counting
        # creates per (env, tip) and capping is a correct dedupe — a new commit
        # produces a new tip, a new meta key, and a fresh counter starting at 0.
        attempts_key = f"auto_deploy_attempts:{env.id}:{tip}"
        try:
            attempts = int(jobs.get_meta(attempts_key, "0") or 0)
        except ValueError:
            attempts = 0
        max_attempts = int(getattr(config, "pipeline_auto_deploy_max_attempts", 3) or 3)
        if attempts >= max_attempts:
            log.warning(
                "auto_deploy: project %s env %s suppressing further auto-deploys at tip %s "
                "after %d failed attempts",
                project_id,
                env.id,
                tip[:12],
                attempts,
            )
            continue
        jobs.create(
            idea=f"Auto-deploy: origin/{base} advanced",
            repo_path=repo_path,
            chat_id=0,
            initial_stage=Stage.DEPLOY,
            source=JobSource.SUPERVISOR,
            source_actor="auto-deploy",
            source_meta={"environment_id": env.id},
        )
        jobs.set_meta(attempts_key, str(attempts + 1))
        log.info(
            "auto_deploy: enqueued deploy-only job for project %s env %s (tip=%s deployed=%s)",
            project_id,
            env.id,
            tip[:12],
            deployed_commit[:12] if deployed_commit else "none",
        )


async def _auto_deploy_loop(
    jobs: JobStore, config, notify: Notify | None = None, *, interval: int
) -> None:
    """Run _auto_deploy_scan every interval seconds, forever."""
    set_db_actor(f"supervisor:{socket.gethostname()}:{os.getpid()}")
    while True:
        try:
            await _auto_deploy_scan(jobs, config, notify)
        except Exception:
            log.exception("auto_deploy: unexpected scan error")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# S4 – SupervisorRunner (leader election + janitor orchestration)
# ---------------------------------------------------------------------------


class SupervisorRunner:
    """Fleet-wide supervisor: exactly one instance is leader at any time.

    Leader election uses a Postgres session-level advisory lock on a dedicated
    connection.  When the leader process dies, the connection closes and the lock
    auto-releases within the TCP keep-alive window, allowing a standby to take over.
    """

    def __init__(self, jobs: JobStore, config, notify: Notify) -> None:
        self.jobs = jobs
        self.config = config
        self.notify = notify
        self._leader = False
        self._tasks: list[asyncio.Task] = []
        self._lock_conn: psycopg.Connection | None = None
        self._worker_id = f"{socket.gethostname()}:{os.getpid()}:supervisor"

    @property
    def is_leader(self) -> bool:
        return self._leader

    async def start(self) -> None:
        # Open a dedicated long-lived connection for the session-level advisory lock.
        # This connection is never returned to the pool; it stays open until stop().
        # Use run_in_executor so TCP/TLS setup doesn't block the event loop.
        loop = asyncio.get_running_loop()
        self._lock_conn = await loop.run_in_executor(
            None,
            lambda: psycopg.connect(self.jobs._dsn, autocommit=True, row_factory=dict_row),
        )
        self._tasks = [asyncio.create_task(self._election_loop(), name="hyqs-supervisor-election")]

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        self._tasks.clear()
        if self._lock_conn is not None:
            with contextlib.suppress(Exception):
                self.jobs.release_supervisor_lock(self._lock_conn)
            with contextlib.suppress(Exception):
                self._lock_conn.close()
            self._lock_conn = None
        with contextlib.suppress(Exception):
            self.jobs.worker_offline(self._worker_id)

    async def _election_loop(self) -> None:
        set_db_actor(f"supervisor:{self._worker_id}")
        # Note: we do not re-check the advisory lock inside the leader heartbeat loop.
        # If _lock_conn silently drops, Postgres auto-releases the lock and a standby
        # can acquire it, briefly creating two concurrent janitors.  This is acceptable
        # because all janitor operations are idempotent (requeue is guarded by status,
        # reconcile_to_done checks AND status='failed', notify deduplicates via meta).
        host = socket.gethostname()
        pid = os.getpid()
        while True:
            acquired = self.jobs.try_acquire_supervisor_lock(self._lock_conn)
            if acquired:
                self._leader = True
                log.info("supervisor %s elected leader", self._worker_id)
                await self.jobs.worker_heartbeat(
                    self._worker_id,
                    host,
                    pid,
                    status="leader",
                    role="supervisor",
                    now=time.time(),
                )
                janitor = asyncio.create_task(
                    _janitor_loop(self.jobs, self.config, self.notify),
                    name="hyqs-supervisor-janitor",
                )
                self._tasks.append(janitor)
                auto_deploy: asyncio.Task | None = None
                if self.config.pipeline_auto_deploy:
                    auto_deploy = asyncio.create_task(
                        _auto_deploy_loop(
                            self.jobs,
                            self.config,
                            self.notify,
                            interval=self.config.pipeline_auto_deploy_interval,
                        ),
                        name="hyqs-supervisor-auto-deploy",
                    )
                    self._tasks.append(auto_deploy)
                try:
                    while True:
                        await asyncio.sleep(30)
                        await self.jobs.worker_heartbeat(
                            self._worker_id,
                            host,
                            pid,
                            status="leader",
                            role="supervisor",
                            now=time.time(),
                        )
                except asyncio.CancelledError:
                    janitor.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await janitor
                    if auto_deploy is not None:
                        auto_deploy.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await auto_deploy
                    raise
                except Exception:
                    janitor.cancel()
                    with contextlib.suppress(BaseException):
                        await janitor
                    if auto_deploy is not None:
                        auto_deploy.cancel()
                        with contextlib.suppress(BaseException):
                            await auto_deploy
                    raise
                finally:
                    self._leader = False
            else:
                self._leader = False
                log.debug("supervisor %s standing by (lock held elsewhere)", self._worker_id)
                await self.jobs.worker_heartbeat(
                    self._worker_id,
                    host,
                    pid,
                    status="standby",
                    role="supervisor",
                    now=time.time(),
                )
                await asyncio.sleep(30)
