"""The pipeline runner: a 24/7 loop that advances jobs through the stages.

One stage per step, persisting after each so an interrupted run (or a machine
reboot) resumes exactly where it left off. The runner owns all git plumbing and
enforces the merge gate (tests pass AND review verdict == pass).

Autonomy:
- **Self-heal**: a failing TEST or REVIEW doesn't end the job — its failure is
  fed to a FIX agent that edits the worktree, then we re-test and re-review,
  bounded by ``pipeline_max_attempts``.
- **Rate-limit pause**: if a stage hits the Anthropic limit, the runner reverts
  the job to PENDING at its current stage and idles until the limit resets.
  This whole gate is deterministic Python — no AI, because AI is exactly what's
  unavailable when limited. The pause time is persisted, so a reboot mid-limit
  doesn't lose it or waste a call rediscovering it.
- **Robustness**: any unexpected stage error fails the job (with notice) instead
  of stranding it in RUNNING.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import subprocess
import time
import typing
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

import structlog

from . import (
    activation,
    agents,
    contracts,
    deploy,
    deployer_state,
    github,
    gitops,
    hosts,
    stages,
    supervisor,
)
from .collision import (
    AUTHORIZED_AMENDMENT_GATES,
    SCOPE_AMENDMENT_CUMULATIVE_LIMIT,
    ScopeAmendmentDecision,
    ScopeAmendmentRejected,
    evaluate_scope_amendment,
    normalize_scope_amendment_path,
)
from .limits import ProviderUnavailable, pause_until
from .models import (
    Job,
    JobStatus,
    ResourceUsage,
    Stage,
    Usage,
    _now,
    gate_conflict_detected,
    gate_finding_fingerprint,
    lock_owner_id,
)
from .providers import Role, build_backend
from .store import JobStore, _is_db_error, is_transient_db_error, set_db_actor

log = logging.getLogger("hyqs.runner")

FailureDetail = Mapping[str, object]

# gate_failed steps whose findings alternate between mutually-exclusive
# demands rather than converging — REVIEW and SECURITY are the only two
# gates whose verdicts can genuinely conflict with each other's requirements.
_GATE_CONFLICT_GATES = ("review", "security")


class Notify(typing.Protocol):
    async def __call__(
        self,
        chat_id: int,
        text: str,
        project_id: int | None = None,
        job_id: int | None = None,
    ) -> None: ...


def _check_not_in_git_repo(path: Path) -> None:
    """Raise ValueError if path is nested inside a git repository.

    Walks ancestor directories looking for a .git entry. Catches the original
    bug where worktrees lived under <repo>/data/... and escaping the worktree
    root resolved back to the parent repo's working tree.
    """
    current = path.resolve()
    while True:
        git_entry = current / ".git"
        if git_entry.is_file() or (git_entry / "HEAD").is_file():
            raise ValueError(
                f"data_dir must not be inside repo_path: {path} is under git repo at {current}"
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


class PipelineRunner:
    def __init__(
        self,
        store: JobStore,
        config,
        notify: Notify,
        drain: asyncio.Event | None = None,
        stage_allowlist: set[Stage] | None = None,
        deployer_mode: bool = False,
    ) -> None:
        self.store = store
        self.config = config
        self.notify = notify
        # A constrained worker (e.g. a standalone deploy host) that only claims
        # stages in this set. None (default) claims any stage, preserving
        # today's full-fleet behavior.
        self.stage_allowlist = stage_allowlist
        # True for a deployer-mode worker: it must never construct an AI backend
        # (defense-in-depth on top of stage_allowlist, since deploy stage
        # handlers never call _backend() anyway) and it records its own
        # claim/deploy activity into local deployer_state files.
        self.deployer_mode = deployer_mode
        self.model = getattr(config, "pipeline_model", None) or config.model
        self.max_attempts = getattr(config, "pipeline_max_attempts", 3)
        self.timeout = getattr(config, "pipeline_stage_timeout", 1800)
        # How many times a single stage may hit the wall-clock timeout before the
        # job is failed instead of silently reclaimed. Without this cap a stage
        # that never converges (e.g. a planner doom-loop) times out, gets
        # reclaimed, restarts from scratch, and burns tokens forever.
        self.max_timeouts = max(1, getattr(config, "pipeline_max_timeouts", 2))
        self.limit_backoff = getattr(config, "pipeline_limit_backoff", 600)
        # How many jobs this process runs in parallel. Stages are all async I/O
        # (Claude stream, git/test subprocesses), so N loops genuinely overlap.
        # Run several processes too — they coordinate through the shared db.
        self.concurrency = max(1, getattr(config, "pipeline_concurrency", 1))
        # A short lease, renewed by a heartbeat, so a crashed worker's job is
        # reclaimed within ~lease_ttl rather than stranded for a whole stage.
        self.lease_ttl = float(getattr(config, "pipeline_lease_ttl", 90))
        # How long one repo's merge may hold its serialization lock before a
        # crashed holder is reclaimed. Merges are quick; this is just a safety net.
        self.merge_lock_ttl = float(getattr(config, "pipeline_merge_lock_ttl", 300))
        # How long one project's schema-job serialization lock may be held before a
        # crashed holder is reclaimed. Held merge->deploy, so longer than the merge lock.
        self.schema_lock_ttl = float(getattr(config, "pipeline_schema_lock_ttl", 300))
        self.rebase_max_attempts = max(1, getattr(config, "pipeline_rebase_max_attempts", 5))
        # Bounded retries for a stage that hits a transient Postgres error
        # (deadlock/serialization failure) — these don't spend the fix-attempt
        # budget since the code being deployed isn't at fault (job #610).
        self.max_db_retries = max(1, getattr(config, "pipeline_max_db_retries", 3))
        # Max clean base-advances absorbed inside the merge lock before releasing to MERGE_VERIFY.
        self.merge_inlock_steps = max(1, getattr(config, "pipeline_merge_inlock_steps", 3))
        # Bounded retries for the phantom-conflict recovery loop (job #1238): a
        # clean local re-merge but a GitHub "not mergeable" verdict means the
        # remote PR branch is just stale, not conflicted — force-refresh it and
        # re-poll this many times before giving up. Doesn't spend rebase_attempts.
        self.phantom_conflict_max_attempts = max(
            1, getattr(config, "pipeline_phantom_conflict_max_attempts", 3)
        )
        self.phantom_conflict_backoff = float(
            getattr(config, "pipeline_phantom_conflict_backoff", 5)
        )
        self.worktrees = Path(config.data_dir) / "worktrees"
        _check_not_in_git_repo(self.worktrees)
        # Shared npm cache for all pipeline subprocesses (npm ci / npm test /
        # frontend builds): without it every fresh worktree re-downloads its
        # whole dependency tree. npm reads the lowercase env var.
        os.environ.setdefault("npm_config_cache", str(Path(config.data_dir) / "npm-cache"))
        self._host = socket.gethostname()
        self._base_id = f"{self._host}:{os.getpid()}"
        # This worker's enrolled host id (None => local/default host), used to
        # pin DEPLOY-stage claims to a matching-host worker. Unset by default,
        # which preserves today's single-host, claim-anywhere behavior.
        self._host_id = hosts.resolve_worker_host_id(
            store, getattr(config, "pipeline_host_name", "")
        )
        self._tasks: list[asyncio.Task] = []
        self._origin_cache: dict[int, str] = {}
        # Shared drain event: when set, each worker loop stops claiming new jobs
        # and exits after its current in-flight stage finishes naturally.
        self._drain: asyncio.Event = drain if drain is not None else asyncio.Event()

    async def _managed_repo(self, job: Job) -> Path:
        """Return the pipeline-owned bare-clone path for this job's project.

        Falls back to Path(job.repo_path) when the job has no project_id or the
        repo has no 'origin' remote (offline / local repos keep current behaviour).
        """
        project_id = getattr(job, "project_id", None)
        if not project_id:
            return Path(job.repo_path)
        origin_url = self._origin_cache.get(project_id)
        if origin_url is None:
            res = await gitops.git(job.repo_path, "remote", "get-url", "origin")
            if not res.ok or not res.stdout.strip():
                return Path(job.repo_path)
            origin_url = res.stdout.strip()
            self._origin_cache[project_id] = origin_url
        return await gitops.ensure_managed_repo(Path(self.config.data_dir), project_id, origin_url)

    async def start(self) -> None:
        # No global RUNNING reset: with multiple workers/processes that would rip
        # jobs out from under live peers. Each claim reclaims only leases that
        # have actually expired (a dead worker). An active per-provider pause
        # lives in the db (meta) and is honored by claim automatically.
        await self._reconcile_startup()
        try:
            active_ids = self.store.get_active_job_ids()
            protected_ids = self.store.get_git_artifact_protected_job_ids()
            await gitops.gc_orphaned_artifacts(
                self.config,
                active_ids | protected_ids,
                repos_dir=Path(self.config.data_dir) / "repos",
            )
        except Exception:
            log.warning("startup GC failed, continuing", exc_info=True)
        try:
            epicless_ids = self.store.list_epicless_jobs()
            if epicless_ids:
                log.warning(
                    "startup: %d job(s) have no epic_id — ids: %s",
                    len(epicless_ids),
                    epicless_ids,
                )
        except Exception:
            log.warning("startup epicless-jobs check failed, continuing", exc_info=True)
        for i in range(self.concurrency):
            wid = f"{self._base_id}:{i}"
            self._tasks.append(asyncio.create_task(self._loop(wid), name=f"hyqs-worker-{i}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def drain(self, timeout: float) -> None:
        """Stop claiming new jobs; wait for in-flight stages to finish naturally.

        After *timeout* seconds, any slot still running is force-cancelled with a
        warning. The teardown sequence (close_notifier, jobs.close) is left to the
        caller — identical to what happens after stop().
        """
        self._drain.set()
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        for t in pending:
            log.warning("drain timeout: force-cancelling worker slot %s", t.get_name())
            t.cancel()
        for t in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()

    async def _reconcile_startup(self) -> None:
        """Advance jobs that were stranded before DONE.

        Called once before workers start so a crashed or restarted process never
        leaves merged work stuck in RUNNING/FAILED at the REVIEW/MERGE boundary,
        or leaves a self-update deploy stuck in DEPLOYING after the pipeline restarts.
        """
        candidates = self.store.stuck_at_merge()
        log.info("reconcile: %d job(s) stuck at merge boundary", len(candidates))
        for job in candidates:
            try:
                if not job.branch:
                    log.debug("reconcile: job %s has no branch, skipping", job.id)
                    continue
                managed = await self._managed_repo(job)
                base = await gitops.default_branch(managed)
                pull_request = await github.inspect_branch_pr(managed, job.branch, base=base)
                if pull_request.state is not github.PullRequestState.MERGED:
                    log.debug(
                        "reconcile: job %s PR identity is %s, leaving alone",
                        job.id,
                        pull_request.state.value,
                    )
                    continue
                merged = await github.pr_identity_is_merged(managed, pull_request)
                if not merged:
                    log.debug("reconcile: job %s PR not merged, leaving alone", job.id)
                    continue
                try:
                    await github.sync_base(managed, base)
                except Exception as e:
                    log.warning(
                        "reconcile: sync_base failed for job %s (best-effort): %s", job.id, e
                    )
                job.stage = Stage.DONE
                job.status = JobStatus.DONE
                self.store.save(job)
                await supervisor.unblock_ready_dependents(
                    self.store, job.id, self.config, self.notify
                )
                if job.project_id:
                    self.store.release_schema_lock(job.project_id, lock_owner_id(job.id))
                log.info("reconcile: job %s advanced to DONE (PR already merged)", job.id)
            except Exception:
                log.exception("reconcile: error processing job %s; skipping", job.id)

        deploying_jobs = self.store.get_deploying_jobs()
        log.info("reconcile: %d job(s) in DEPLOYING state", len(deploying_jobs))
        for job in deploying_jobs:
            try:
                if not job.deployed_commit:
                    log.warning("reconcile: job %s has no deployed_commit, skipping", job.id)
                    continue
                finalized = await deploy.reconcile_deploying_job(self.store, job)
                if finalized:
                    await self.notify(
                        job.chat_id,
                        f"✅ Job #{job.id}: deployment verified live. Done.",
                        project_id=job.project_id,
                        job_id=job.id,
                    )
                    log.info(
                        "reconcile: job %s finalized to DONE (deployed_commit %s verified live)",
                        job.id,
                        job.deployed_commit,
                    )
                else:
                    log.warning(
                        "reconcile: job %s deployed_commit=%s not verified live on current HEAD; "
                        "leaving DEPLOYING",
                        job.id,
                        job.deployed_commit,
                    )
            except Exception:
                log.exception("reconcile: error processing DEPLOYING job %s; skipping", job.id)

        backfilled = self.store.reconcile_agent_tasks()
        for r in backfilled:
            log.info(
                "reconcile: agent %r (id=%d) backfilled tasks: %s",
                r["name"],
                r["agent_id"],
                r["added"],
            )

        split_orphans = self.store.list_split_parents_with_pending_dependents()
        log.info("reconcile: %d split parent(s) with pending dependents", len(split_orphans))
        for entry in split_orphans:
            try:
                parent_id = entry["parent_id"]
                child_ids = entry["child_ids"]
                repointed = self.store.repoint_split_dependents(parent_id, child_ids)
                for dep_id in repointed:
                    self.store.add_event(
                        dep_id,
                        "plan",
                        "info",
                        summary=(
                            f"dependency #{parent_id} re-pointed to split children "
                            f"{child_ids} (startup reconcile)"
                        ),
                        detail={"repointed_from": parent_id, "repointed_to": child_ids},
                    )
                log.info(
                    "reconcile: split parent %s repointed %d dependent(s) onto %s",
                    parent_id,
                    len(repointed),
                    child_ids,
                )
            except Exception:
                log.exception("reconcile: error repointing split parent %s; skipping", entry)

        for project in self.store.list_projects():
            try:
                changes = self.store.reconcile_auto_dependencies(project["id"])
                if changes:
                    log.info(
                        "reconcile: minimized %d auto dependency edge(s) for project %s",
                        len(changes),
                        project["id"],
                    )
            except Exception:
                log.exception(
                    "reconcile: error minimizing auto dependencies for project %s; skipping",
                    project["id"],
                )

    async def _idle(self, worker_id: str) -> None:
        await self.store.worker_heartbeat(
            worker_id,
            self._host,
            os.getpid(),
            status="idle",
            now=time.time(),
            job_id=None,
            stage="",
            provider="",
            started_at=time.time(),
        )

    def _record_local_claim(self, job: Job) -> None:
        """Note this claim in the local deployer_state files.

        Only a deployer-mode worker touches these local files — full-fleet
        workers (deployer_mode=False, the default) never write them.
        """
        if not self.deployer_mode or not job.project_id:
            return
        env = self.store.get_or_create_default_environment(job.project_id)
        deployer_state.record_claim(
            self.config.data_dir, job_id=job.id, project_id=job.project_id, environment_id=env.id
        )

    async def _loop(self, worker_id: str) -> None:
        set_db_actor(f"worker:{worker_id}")
        log.info(
            "worker %s started (model=%s, max_attempts=%s)",
            worker_id,
            self.model,
            self.max_attempts,
        )
        await self._idle(worker_id)
        db_backoff = 2.0
        try:
            while True:
                if self._drain.is_set():
                    await self.store.worker_heartbeat(
                        worker_id,
                        self._host,
                        os.getpid(),
                        status="draining",
                        now=time.time(),
                        job_id=None,
                        stage="",
                        provider="",
                        started_at=time.time(),
                    )
                    break
                try:
                    job = await self.store.claim(
                        worker_id,
                        time.time(),
                        self.lease_ttl,
                        worker_host_id=self._host_id,
                        stage_allowlist=self.stage_allowlist,
                    )
                    db_backoff = 2.0  # successful DB call — reset backoff
                    if job is None:
                        await self._idle(worker_id)
                        await asyncio.sleep(3)
                        continue
                    set_db_actor(f"worker:{worker_id} job:{job.id}")
                    self._record_local_claim(job)
                    # Announce what this worker is now doing (powers the C2 view).
                    await self.store.worker_heartbeat(
                        worker_id,
                        self._host,
                        os.getpid(),
                        status="busy",
                        now=time.time(),
                        job_id=job.id,
                        stage=_exec_step(job.stage),
                        provider=job.provider,
                        started_at=time.time(),
                    )
                    try:
                        with structlog.contextvars.bound_contextvars(
                            job_id=job.id, stage=job.stage.value, project_id=job.project_id
                        ):
                            await self._run_stage_with_retry(worker_id, job)
                        # Fast-path: keep this job while its next step is
                        # deterministic (lint/test/merge/deploy) — skip the
                        # requeue → claim-poll → cold-start round trip. Agentic
                        # steps, cancelled jobs, rebase backoffs, and drain all
                        # return None and fall back to the normal claim loop.
                        while not self._drain.is_set():
                            nxt = await self.store.claim_fastpath(
                                job.id,
                                worker_id,
                                time.time(),
                                self.lease_ttl,
                                worker_host_id=self._host_id,
                                stage_allowlist=self.stage_allowlist,
                            )
                            if nxt is None:
                                break
                            job = nxt
                            set_db_actor(f"worker:{worker_id} job:{job.id}")
                            self._record_local_claim(job)
                            await self.store.worker_heartbeat(
                                worker_id,
                                self._host,
                                os.getpid(),
                                status="busy",
                                now=time.time(),
                                job_id=job.id,
                                stage=_exec_step(job.stage),
                                provider=job.provider,
                                started_at=time.time(),
                            )
                            with structlog.contextvars.bound_contextvars(
                                job_id=job.id, stage=job.stage.value, project_id=job.project_id
                            ):
                                await self._run_stage_with_retry(worker_id, job)
                    except ProviderUnavailable as exc:
                        await self._pause(job, exc)
                    except gitops.IsolationViolationError as exc:
                        log.error("isolation violation for job %s: %s", job.id, exc)
                        await self._fail(
                            job,
                            str(exc),
                            failure_code="isolation_violation",
                            failure_origin="infrastructure",
                            retry_disposition="human_review",
                        )
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError:
                        # Worktree already cleaned (removed or reset) by _run_leased.
                        # Count the timeout against a bounded budget: past the cap we
                        # fail the job instead of leaving it to be reclaimed forever
                        # (an un-converging stage would otherwise loop indefinitely).
                        await self._timed_out(job)
                    except agents.JobCancelled:
                        # The DB row is already CANCELLED from the original cancel_job
                        # call; existing archive/teardown flows handle worktree/branch
                        # cleanup later. Don't re-mark it FAILED.
                        log.info(
                            "job %s cancelled mid-stage (%s); leaving as cancelled",
                            job.id,
                            job.stage.value,
                        )
                    except Exception as exc:  # noqa: BLE001 - never strand a job in RUNNING
                        log.exception("stage %s crashed for job %s", job.stage.value, job.id)
                        await self._fail(
                            job,
                            f"unexpected error during {job.stage.value}: {exc}",
                            failure_code="unexpected_stage_error",
                            failure_origin="infrastructure",
                            retry_disposition="same_step",
                            failure_detail={"exception_type": type(exc).__name__},
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep the worker alive
                    if _is_db_error(exc):
                        log.warning("DB unavailable, retrying in %.0fs", db_backoff)
                        await asyncio.sleep(db_backoff)
                        db_backoff = min(db_backoff * 2, 60.0)
                    else:
                        db_backoff = 2.0
                        log.exception("worker loop error")
                        await asyncio.sleep(3)
        finally:
            # Clean exit: drop this worker from the fleet view.
            with contextlib.suppress(Exception):
                self.store.worker_offline(worker_id)

    async def _run_leased(self, worker_id: str, job: Job) -> None:
        """Advance one stage while a heartbeat keeps the job lease + worker fresh.

        Investigated root cause of job #3988's 8h fleet wedge: the heartbeat's
        ``finally: hb.cancel()`` below is correct and does fire on every normal
        return/exception out of ``self._advance(job)``. The gap is upstream —
        ``self._advance`` awaits the agent SDK call under a per-call
        ``asyncio.wait_for`` bound (see providers.py); when that bound expires,
        ``wait_for`` cancels the inner call, which asks the SDK to tear down its
        subprocess via ``gen.aclose()``. If that close itself never completes
        (observed as "Control request timeout: initialize" after a network
        blip left a dead pipe), the cancellation never resolves, so
        ``asyncio.wait_for`` never returns, ``_advance`` blocks forever, and
        this method's ``finally`` never runs — the heartbeat keeps renewing
        ``status='busy'`` indefinitely for a job that may already be
        PENDING/FAILED elsewhere. The stage-timeout budget
        (``pipeline_stage_timeout``) is intentionally left unchanged here; it
        is out of scope for this fix. The durable backstop for this class of
        hang is not a fix to the hang itself but a fleet-wide, job-status-keyed
        reclaim: see ``JobStore.reclaim_orphaned_worker_slots`` and
        ``supervisor._check_fleet_liveness``, which recover an orphaned slot
        (and alert once, loudly) without touching a genuinely running job.
        """
        interval = max(5.0, self.lease_ttl / 3)
        worktree = self.worktrees / f"job-{job.id}"
        managed = await self._managed_repo(job)

        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(interval)
                now = time.time()
                try:
                    await self.store.renew_lease(job.id, job.owner, now + self.lease_ttl)
                    await self.store.worker_heartbeat(
                        worker_id,
                        self._host,
                        os.getpid(),
                        status="busy",
                        now=now,
                        job_id=job.id,
                        stage=_exec_step(job.stage),
                        provider=job.provider,
                    )
                except Exception as exc:
                    if _is_db_error(exc):
                        log.warning("heartbeat DB error, lease will age: %s", exc)
                    else:
                        raise

        hb = asyncio.create_task(_heartbeat())
        try:
            await self._advance(job)
        except asyncio.TimeoutError:
            # A timed-out stage may have left partial agent edits behind. Up to
            # PLAN the retry doesn't need the worktree (build recreates it), so
            # drop it; every later stage resumes from the last committed
            # checkpoint, so reset the tree instead — deleting it would make the
            # timeout retry a guaranteed worktree-missing failure.
            with contextlib.suppress(Exception):
                if job.stage in (Stage.QUEUED, Stage.PLAN):
                    await gitops.remove_worktree(managed, worktree)
                elif worktree.exists():
                    await gitops.restore_gate_worktree(worktree)
            raise
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb

    async def _run_stage_with_retry(self, worker_id: str, job: Job) -> None:
        """Run one stage, retrying in-process on a transient Postgres error.

        A deadlock or serialization failure means Postgres killed one txn of a
        valid pair — the code being deployed isn't at fault (job #610). Retry a
        bounded number of times with a short backoff instead of counting it as
        a job failure or spending the fix-attempt budget. job.attempts/status/
        stage are never touched here: an exhausted retry falls through unchanged
        to the caller's existing generic failure handling.
        """
        attempt = 0
        while True:
            try:
                await self._run_leased(worker_id, job)
                return
            except Exception as exc:
                if not is_transient_db_error(exc):
                    raise
                attempt += 1
                if attempt >= self.max_db_retries:
                    log.warning(
                        "job %s: transient DB error at %s exceeded retry cap (%s/%s): %s",
                        job.id,
                        job.stage.value,
                        attempt,
                        self.max_db_retries,
                        exc,
                    )
                    raise
                backoff = 0.5 * (2 ** (attempt - 1))
                log.warning(
                    "job %s: transient DB error at %s (attempt %s/%s), retrying in %.1fs: %s",
                    job.id,
                    job.stage.value,
                    attempt,
                    self.max_db_retries,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)

    # --- control-plane: pause + fail (no AI) ---------------------------
    async def _pause(self, job: Job, exc: ProviderUnavailable) -> None:
        """A provider hit its limit: atomically pause it and re-queue the job.

        Routes through ``JobStore.record_provider_failover`` — the single
        source of truth for this transition — instead of separately pausing
        the provider and saving the job, so the source-provider pause, the
        job's return to PENDING at the same stage with its lease/agent/
        provider cleared, and the durable ``provider_failover`` event either
        all land or none do. This never spends a fix/retry-budget field: only
        this provider is paused (in the shared db, so every worker/process
        honors it); jobs routed to other providers — and deterministic steps —
        keep flowing. All deterministic Python: AI is exactly what's
        unavailable. A lost ownership race (a stale lease already reclaimed by
        a newer worker) makes the transition a safe no-op — never overwrite
        current job state.
        """
        until = pause_until(exc.resets_at, self.limit_backoff)
        transition = await self.store.record_provider_failover(
            job.id,
            job.owner,
            job.stage,
            exc.provider,
            until,
            time.time(),
            project_id=job.project_id,
            failed_step=job.executing_step or _exec_step(job.stage),
        )
        if not transition.applied:
            log.info(
                "job %s: lost ownership before recording %s failover at %s; no-op",
                job.id,
                exc.provider,
                job.stage.value,
            )
            return
        log.warning(
            "%s rate limited at %s; pausing it until %s", exc.provider, job.stage.value, _fmt(until)
        )
        if transition.alternate_available:
            message = (
                f"⏸️➡️ Job #{job.id}: {exc.provider} hit its limit at {job.stage.value}. "
                f"Pausing {exc.provider} until ~{_fmt(until)}; an alternate provider is "
                "available, so I'm resuming immediately."
            )
        else:
            message = (
                f"⏸️ Job #{job.id}: {exc.provider} hit its limit at {job.stage.value}. "
                f"Pausing {exc.provider} until ~{_fmt(until)}, then resuming where I left off."
            )
        await self.notify(
            job.chat_id,
            message,
            project_id=job.project_id,
            job_id=job.id,
        )

    async def _timed_out(self, job: Job, *, timeout_seconds: int | None = None) -> None:
        """A stage hit the wall-clock timeout: count it, requeue, or give up.

        Timeouts get their own budget (``max_timeouts``), separate from FIX and
        rebase retries, because a stage that never finishes (e.g. a planner that
        keeps re-exploring instead of emitting its plan) would otherwise time
        out, be reclaimed, restart from scratch, and burn tokens forever. Below
        the cap we requeue at the same stage (status=PENDING so it's re-claimed
        promptly rather than after lease-expiry); at the cap we fail the job.
        """
        effective_timeout = timeout_seconds or self.timeout
        job.timeout_attempts += 1
        if job.timeout_attempts >= self.max_timeouts:
            return await self._fail(
                job,
                f"stage {job.stage.value} timed out {job.timeout_attempts} time(s) "
                f"(>{effective_timeout}s each) without completing; giving up.",
                failure_code="stage_timeout_exhausted",
                failure_origin="infrastructure",
                retry_disposition="human_review",
                failure_detail={
                    "attempts": job.timeout_attempts,
                    "timeout_seconds": effective_timeout,
                },
            )
        job.failed_step = job.executing_step or _exec_step(job.stage)
        job.failure_code = "stage_timeout"
        job.failure_origin = "infrastructure"
        job.retry_disposition = "same_step"
        job.failure_detail = {
            "attempts": job.timeout_attempts,
            "timeout_seconds": effective_timeout,
        }
        job.executing_step = None
        job.status = JobStatus.PENDING  # leave job.stage as-is so we resume here
        job.owner = ""
        job.lease_until = 0.0
        self.store.save(job)
        log.warning(
            "stage %s timed out for job %s (timeout %s/%s); worktree cleaned, requeued",
            job.stage.value,
            job.id,
            job.timeout_attempts,
            self.max_timeouts,
        )
        await self.notify(
            job.chat_id,
            f"⏱️ Job #{job.id}: {job.stage.value} timed out "
            f"(attempt {job.timeout_attempts}/{self.max_timeouts}) — retrying…",
            project_id=job.project_id,
            job_id=job.id,
        )

    async def _already_satisfied(self, job: Job, started: str, usage) -> None:
        """Finalize a reviewer-verified no-diff request as already satisfied.

        The isolation leak is already checked (diff-scoped) in the build stage
        and independent target-branch verification is complete before we get
        here. Do not call this directly for an unverified builder response.
        """
        self._event(
            job,
            "build",
            "done",
            started,
            summary="feature already implemented — nothing to build",
            detail={"resolution": "already-satisfied"},
            usage=usage,
        )
        job.stage = Stage.DONE
        job.status = JobStatus.DONE
        job.resolution = "already-satisfied"
        self.store.save(job)
        await supervisor.unblock_ready_dependents(self.store, job.id, self.config, self.notify)
        log.warning("job %s: already satisfied — no diff produced, marking DONE", job.id)
        await self.notify(
            job.chat_id,
            f"⚠️ Job #{job.id}: feature already implemented — nothing to build. Marking done without a diff.",
            project_id=job.project_id,
            job_id=job.id,
        )

    async def _verify_already_satisfied(
        self,
        job: Job,
        started: str,
        *,
        managed: Path,
        worktree: Path,
        base: str,
        backend,
        no_diff_retry: bool = False,
    ) -> None:
        """Fail closed unless a reviewer verifies the request on fresh target code."""
        # This verifier is part of the BUILD operation even though the durable
        # checkpoint remains PLAN until a valid build diff exists.
        job.executing_step = "build"
        source_meta = job.source_meta if isinstance(job.source_meta, dict) else {}
        if source_meta.get("kind") == "activation-followup":
            # An activation-followup confirms a host-config switch (typically a
            # gitignored repo-root .env var) was flipped live. The worktree can
            # structurally never contain that file, so route around the
            # checkout/AI-reviewer path entirely and check this process's own
            # resolved environment instead (see hyqs.pipeline.activation).
            return await self._verify_activation_followup(job, started, source_meta)
        try:
            await gitops.remove_worktree(managed, worktree)
            target_base = await gitops.fresh_base(managed, base)
            created = await gitops.create_detached_worktree(managed, worktree, ref=target_base)
        except Exception as exc:  # noqa: BLE001 - checkout uncertainty must fail closed
            message = f"already-satisfied verification checkout failed: {exc}"
            self._event(
                job,
                "build",
                "failed",
                started,
                summary=message,
                detail={"verification": "failed"},
            )
            return await self._fail(
                job,
                message,
                failure_code="no_diff_verification_failed",
                failure_origin="deterministic_gate",
                retry_disposition="human_review",
                failure_detail={"verification": "checkout"},
            )
        if not created.ok:
            message = f"already-satisfied verification checkout failed: {created.stderr[:300]}"
            self._event(
                job,
                "build",
                "failed",
                started,
                summary=message,
                detail={"verification": "failed"},
            )
            return await self._fail(
                job,
                message,
                failure_code="no_diff_verification_failed",
                failure_origin="deterministic_gate",
                retry_disposition="human_review",
                failure_detail={"verification": "checkout"},
            )

        acceptance = "\n".join(
            f"- {story.get('id', '?')}: {story.get('acceptance', '')}"
            for story in (job.plan or {}).get("stories", [])
        )
        prompt = (
            "Independently verify whether the ORIGINAL JOB REQUEST is already fully "
            "satisfied by the current checkout. This checkout is a freshly synchronized "
            f"copy of target branch '{target_base}'; there is intentionally no candidate "
            "diff. Inspect the code and run focused read-only checks as needed. Do not "
            "trust or refer to the build agent's claims.\n\n"
            f"Original job request:\n{job.idea}\n\n"
            f"Acceptance criteria:\n{acceptance or '(none supplied)'}\n\n"
            "Return pass only if every requested behavior and acceptance criterion is "
            "demonstrably present. Otherwise return fail. Include concise, concrete "
            "evidence naming the inspected route, symbol, behavior, or test."
        )
        append_system = """\
You are an independent reviewer deciding whether a no-diff build is genuinely already
satisfied by target-branch code. Fail closed when behavior is absent or cannot be
verified. Do not modify files. End with:
<<<RESULT_JSON>>>
{"verdict": "pass" | "fail", "evidence": "concise concrete evidence"}
<<<END_RESULT>>>"""
        try:
            run = await backend.run(
                prompt=prompt,
                cwd=str(worktree),
                role=Role.REVIEWER,
                append_system=append_system,
                timeout=self.timeout,
            )
            self.store.record_usage("already-satisfied-verification", run.usage, job.id)
            result = contracts.parse_already_satisfied_verification(run.text)
        except Exception as exc:  # noqa: BLE001 - any unavailable/malformed verifier fails closed
            message = f"already-satisfied verification unavailable or malformed: {exc}"
            self._event(
                job,
                "build",
                "failed",
                started,
                summary=message,
                detail={"verification": "failed"},
            )
            return await self._fail(
                job,
                message,
                failure_code="no_diff_verification_failed",
                failure_origin="ai_gate",
                retry_disposition="human_review",
                failure_detail={"verification": "unavailable"},
            )

        evidence = result["evidence"].strip()[: agents.NO_DIFF_RETRY_EVIDENCE_LIMIT]
        if result["verdict"] == "fail":
            message = f"already-satisfied verification failed: {evidence}"
            self._event(
                job,
                "build",
                "failed",
                started,
                summary=message,
                detail={"verification": "failed", "evidence": evidence},
                usage=run.usage,
            )
            return await self._fail(
                job,
                message,
                failure_code="no_diff_verification_failed",
                failure_origin="ai_gate",
                retry_disposition="terminal" if no_diff_retry else "retry_build",
                failure_detail={
                    "verification": "rejected",
                    "evidence": evidence,
                    "retry_attempt": 1 if no_diff_retry else 0,
                    "outcome": "terminal" if no_diff_retry else "retry_build",
                },
            )

        self._event(
            job,
            "build",
            "info",
            started,
            summary=f"already-satisfied verification passed: {evidence}",
            detail={"verification": "passed", "evidence": evidence},
            usage=run.usage,
        )
        await self._already_satisfied(job, started, run.usage)

    async def _verify_activation_followup(
        self, job: Job, started: str, source_meta: Mapping[str, object]
    ) -> None:
        """Verify a ``.env``-anchored activation against this process's own environment.

        The follow-up names the config it expects flipped; check it against
        ``os.environ`` (populated by ``hyqs.config``'s ``load_dotenv()`` at
        import time in the parent pipeline process — the deployment host's
        actual resolved configuration) rather than the job's isolated
        worktree, which can never contain a gitignored ``.env``.
        """
        location = str(source_meta.get("activation_location", ""))
        effect = str(source_meta.get("expected_live_effect", ""))
        var_name = activation.extract_activation_env_var(location, effect)
        verdict, evidence = activation.verify_env_activation(var_name, os.environ)

        if verdict is activation.ActivationVerification.satisfied:
            self._event(
                job,
                "build",
                "info",
                started,
                summary=f"already-satisfied verification passed: {evidence}",
                detail={"verification": "passed", "evidence": evidence, "source": "host_env"},
            )
            return await self._already_satisfied(job, started, Usage())

        detail = {"verification": verdict.value, "evidence": evidence, "source": "host_env"}
        if verdict is activation.ActivationVerification.unsatisfied:
            message = f"activation not yet applied: {evidence}"
            failure_code = "activation_not_applied"
        else:
            message = f"activation verification inconclusive: {evidence}"
            failure_code = "activation_unobservable"
        self._event(job, "build", "failed", started, summary=message, detail=detail)
        return await self._fail(
            job,
            message,
            failure_code=failure_code,
            failure_origin="deterministic_gate",
            retry_disposition="human_review",
            failure_detail=detail,
        )

    async def _fail(
        self,
        job: Job,
        message: str,
        *,
        failure_code: str | None = None,
        failure_origin: str | None = None,
        retry_disposition: str | None = None,
        failure_detail: dict | None = None,
    ) -> None:
        step = job.executing_step or _exec_step(job.stage)
        if failure_code is None and step == "deploy":
            candidate_signals = (
                "cannot find module",
                "could not resolve",
                "failed to resolve import",
                "missing package",
                "module not found",
                "rollup failed",
                "typeerror",
            )
            failure_code = (
                "deploy_candidate_error"
                if any(signal in message.lower() for signal in candidate_signals)
                else "deploy_environment_error"
            )
            failure_origin = failure_origin or (
                "candidate_code" if failure_code == "deploy_candidate_error" else "deployment"
            )
            retry_disposition = retry_disposition or (
                "fix_worktree" if failure_code == "deploy_candidate_error" else "remediation_job"
            )
        job.failed_step = step
        job.failure_code = failure_code
        job.failure_origin = failure_origin
        job.retry_disposition = retry_disposition
        job.failure_detail = failure_detail
        job.executing_step = None
        job.status = JobStatus.FAILED
        job.error = message
        self.store.save(job)
        if job.stage == Stage.DEPLOY:
            self.store.fail_promotion(job.id)
        if job.project_id:
            # Best-effort, no-op if this job never held it (non-schema jobs, or a
            # schema job that failed before the merge boundary).
            self.store.release_schema_lock(job.project_id, lock_owner_id(job.id))
        log.warning(
            "job %s FAILED at %s after %s attempt(s): %s",
            job.id,
            job.stage.value,
            job.attempts,
            message[:200],
        )
        # Leave the PR open with a note so a human can take over on GitHub.
        if job.branch:
            try:
                managed = await self._managed_repo(job)
                if await github.has_remote(managed):
                    await github.pr_comment(
                        managed,
                        job.branch,
                        f"### ❌ Hyqs build failed at {job.stage.value}\n\n"
                        f"```\n{message[:1500]}\n```\n\nLeaving this PR open for a human to take over.",
                    )
            except Exception:  # noqa: BLE001 - best effort
                log.warning("failed to comment on PR for job %s", job.id, exc_info=True)
        await self.notify(
            job.chat_id,
            f"❌ Job #{job.id} FAILED\nStage: {job.stage.value}\nAttempts: {job.attempts}\nReason: {message[:500]}",
            project_id=job.project_id,
            job_id=job.id,
        )

    async def _retry_or_fail(
        self,
        job: Job,
        failure: str,
        *,
        failure_detail: FailureDetail | None = None,
    ) -> None:
        """A TEST/REVIEW failure: route into the FIX loop, or give up if spent."""
        # Anchored, not a bare substring search: stages/test.py only ever emits this
        # exact "tests failed ((none)):\n[worktree-missing]" prefix when
        # testing.run_tests's worktree-missing branch fires, because the "(none)"
        # command token is fixed by our own code and never comes from the
        # candidate's subprocess output (pytest/lint/import-smoke stdout, which
        # candidate code controls, only ever lands in the *summary* tail after a
        # real command name). A malicious test/lint diagnostic that merely prints
        # the literal marker text cannot reproduce this prefix, so it cannot forge
        # a transient/auto-requeue classification for genuine breakage.
        if failure.startswith("tests failed ((none)):\n[worktree-missing]"):
            return await self._fail(
                job,
                failure,
                failure_code="worktree_missing",
                failure_origin="infrastructure",
                retry_disposition="same_step",
                failure_detail=failure_detail,
            )
        failed_step = job.executing_step or _exec_step(job.stage)
        if failed_step == "lint":
            code = "lint_failed"
        elif failed_step == "test":
            code = "test_failed"
        else:
            code = "gate_failed"
        gate_history: list = []
        if code == "gate_failed" and failed_step in _GATE_CONFLICT_GATES:
            gate_history = (
                job.failure_detail.get("gate_history", [])
                if isinstance(job.failure_detail, dict)
                else []
            )
            gate_result = job.review if failed_step == "review" else job.security_review
            current_signature = gate_finding_fingerprint(gate_result)
            if gate_conflict_detected(gate_history, failed_step, current_signature):
                conflict_detail = dict(failure_detail or {})
                conflict_detail["gate_history"] = [*gate_history, [failed_step, current_signature]]
                conflict_detail["attempts"] = job.attempts
                return await self._fail(
                    job,
                    _gate_conflict_message(job),
                    failure_code="gate_conflict",
                    failure_origin="ai_gate",
                    retry_disposition="human_review",
                    failure_detail=conflict_detail,
                )
            gate_history = [*gate_history, [failed_step, current_signature]]
        project = self.store.get_project(job.project_id) if job.project_id is not None else None
        cap = (
            project.max_fix_attempts
            if project is not None and project.max_fix_attempts is not None
            else self.max_attempts
        )
        if job.attempts >= cap:
            exhausted_detail = dict(failure_detail or {})
            exhausted_detail["attempts"] = job.attempts
            if gate_history:
                exhausted_detail["gate_history"] = gate_history
            return await self._fail(
                job,
                f"gave up after {job.attempts} fix attempt(s).\n\n{failure}",
                failure_code=code,
                failure_origin="candidate_code"
                if code in {"lint_failed", "test_failed"}
                else "ai_gate",
                retry_disposition="human_review",
                failure_detail=exhausted_detail,
            )
        # A repair can legitimately expose the next independent test, review,
        # or security finding. The global attempt cap bounds the loop; a changed
        # signature is progress and must not force premature human escalation.
        retry_detail = dict(failure_detail or {})
        retry_detail["attempt"] = job.attempts + 1
        if gate_history:
            retry_detail["gate_history"] = gate_history
        job.failed_step = failed_step
        job.failure_code = code
        job.failure_origin = (
            "candidate_code" if code in {"lint_failed", "test_failed"} else "ai_gate"
        )
        job.retry_disposition = "fix_worktree"
        job.failure_detail = retry_detail
        job.executing_step = None
        job.attempts += 1
        job.failure = failure
        job.stage = Stage.FIX
        job.status = JobStatus.PENDING
        self.store.save(job)
        await self.notify(
            job.chat_id,
            f"🛠️ Job #{job.id}: failure detected — self-healing (attempt {job.attempts}/{cap})…",
            project_id=job.project_id,
            job_id=job.id,
        )

    async def _apply_authorized_scope_amendment(self, job: Job) -> bool:
        """Apply the exact persisted gate authorization before provider I/O.

        The failure payload is only an identity pointer. Filesystem authority is
        recovered from exactly one durable failed event and evaluated by the
        collision policy owner before the store atomically widens the manifest.
        """
        detail = job.failure_detail if isinstance(job.failure_detail, dict) else {}
        identity = detail.get("authorized_gate_failure")
        if not isinstance(identity, dict):
            return True

        gate = str(identity.get("gate") or "").strip().lower().replace("-", "_")
        check_id = identity.get("check_id")
        event_id = identity.get("event_id")
        base_audit = {
            "gate": gate,
            "check_id": check_id,
            "event_id": event_id,
            "failure_event_id": event_id,
            "accepted": [],
            "rejected": [],
        }

        def reject(reason: str, *, extra: dict | None = None) -> bool:
            audit = {**base_audit, "persistence_status": "rejected", "reason": reason}
            if extra:
                audit.update(extra)
            self._event(
                job,
                "scope-amendment",
                "rejected",
                _now(),
                summary=f"authorized scope amendment rejected: {reason}",
                detail=audit,
                attempt=job.attempts,
            )
            return False

        if (
            gate not in AUTHORIZED_AMENDMENT_GATES
            or not isinstance(check_id, str)
            or not check_id
            or not isinstance(event_id, str)
            or not event_id
        ):
            return reject("invalid_authorization_identity")

        matches = []
        for event in self.store.list_events(job.id):
            event_detail = event.get("detail")
            evidence = (
                event_detail.get("authorized_gate_failure")
                if isinstance(event_detail, dict)
                else None
            )
            event_gate = str(event.get("stage") or "").lower().replace("-", "_")
            if (
                event.get("status") == "failed"
                and event_gate == gate
                and isinstance(evidence, dict)
                and evidence.get("gate") == identity.get("gate")
                and evidence.get("check_id") == check_id
                and evidence.get("event_id") == event_id
                and evidence == identity
            ):
                matches.append(event)
        if len(matches) != 1:
            return reject(
                "authorizing_event_missing" if not matches else "authorizing_event_ambiguous"
            )

        matched_detail = matches[0]["detail"]
        meta = job.source_meta or {}
        amendments = meta.get("scope_amendments") or {}
        sequence = int(amendments.get("sequence") or 0)
        raw_failing_paths = identity.get("failing_paths")
        if not isinstance(raw_failing_paths, list) or not raw_failing_paths:
            return reject("invalid_authorized_paths", extra={"sequence": sequence})
        try:
            failing_paths = tuple(
                normalize_scope_amendment_path(path) for path in raw_failing_paths
            )
        except (AttributeError, TypeError):
            return reject("invalid_authorized_paths", extra={"sequence": sequence})
        if any(path is None for path in failing_paths):
            return reject("invalid_authorized_paths", extra={"sequence": sequence})

        frozen_scope = meta.get("scope") or {}
        authorized_paths = {
            path
            for raw in (
                *(frozen_scope.get("allowed_paths") or []),
                *(amendments.get("cumulative_paths") or []),
            )
            if (path := normalize_scope_amendment_path(raw))
        }
        canonical_failing_paths = tuple(typing.cast(str, path) for path in failing_paths)
        if set(canonical_failing_paths) <= authorized_paths:
            audit = {
                **base_audit,
                "outcome": "already_authorized",
                "persistence_status": "already_authorized",
                "reason": None,
                "prior_sequence": sequence,
                "sequence": sequence,
                "already_authorized_paths": list(canonical_failing_paths),
                "cumulative_amendment_paths": list(amendments.get("cumulative_paths") or []),
                "cumulative_allowed_paths": sorted(authorized_paths),
            }
            self._event(
                job,
                "scope-amendment",
                "already_authorized",
                _now(),
                summary="authorized scope amendment already authorized",
                detail=audit,
                attempt=job.attempts,
            )
            return True

        worktree = self.worktrees / f"job-{job.id}"
        if not worktree.exists():
            return reject("worktree_missing", extra={"sequence": sequence})

        tracked_files = await gitops.tracked_files(str(worktree))
        try:
            current_diff = [row["path"] for row in await gitops.numstat(worktree, "HEAD~1..HEAD")]
        except (KeyError, RuntimeError, ValueError):
            return reject("current_diff_unavailable", extra={"sequence": sequence})

        active_manifests = []
        if job.project_id is not None:
            # Drain every page to completion: this scope-conflict check needs the
            # complete active-job set for one correctness check, not a bounded
            # page, so never break out after the first page.
            cursor = None
            while True:
                page = self.store.list_jobs_page(job.project_id, cursor=cursor)
                for active in page.jobs:
                    if active.id == job.id:
                        continue
                    active_scope = (active.source_meta or {}).get("scope") or {}
                    active_manifests.append(active_scope.get("allowed_paths") or [])
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor

        deterministic_evidence: dict[str, object] = {}
        for source in (matched_detail, detail):
            candidate = source.get("evidence")
            if isinstance(candidate, dict):
                for category, paths in candidate.items():
                    deterministic_evidence[str(category)] = paths
        relationships = matched_detail.get("relationships")
        if relationships is None:
            relationships = detail.get("relationships") or {}
        authorized_event = {
            **identity,
            "stage": gate,
            "failed": True,
            "gated": True,
            "authorized": True,
            "outcome": "failed",
        }
        decision = evaluate_scope_amendment(
            frozen_scope=frozen_scope,
            plan_story=job.plan or {},
            authorized_event=authorized_event,
            tracked_files=tracked_files,
            current_diff=current_diff,
            evidence=deterministic_evidence,
            relationships=relationships,
            amendment_history=amendments.get("cumulative_paths") or [],
            active_manifests=active_manifests,
        )
        limit_reasons = {"per_cycle_limit", "cumulative_limit"}
        if any(item.reason_code in limit_reasons for item in decision.rejected):
            decision = ScopeAmendmentDecision(
                (),
                decision.rejected
                + tuple(
                    ScopeAmendmentRejected(item.path, "limit_rejected_cycle")
                    for item in decision.accepted
                ),
            )
        persistence = self.store.append_scope_amendment(
            job.id,
            decision,
            authorizing_gate=gate,
            failure_event_id=event_id,
            expected_prior_sequence=sequence,
            cumulative_limit=SCOPE_AMENDMENT_CUMULATIVE_LIMIT,
        )
        audit = {
            **base_audit,
            "outcome": persistence.status,
            "accepted": [item.to_dict() for item in decision.accepted],
            "rejected": [item.to_dict() for item in decision.rejected],
            "prior_sequence": sequence,
            "sequence": persistence.sequence,
            "cumulative_amendment_paths": list(persistence.cumulative_amendment_paths),
            "cumulative_allowed_paths": list(persistence.cumulative_allowed_paths),
            "persistence_status": persistence.status,
            "reason": persistence.reason,
        }
        outcome = persistence.status
        can_continue = outcome == "idempotent" or (
            outcome == "applied" and bool(persistence.newly_accepted_paths)
        )
        if not can_continue:
            outcome = "rejected"
            audit["outcome"] = outcome
            audit["reason"] = audit["reason"] or "no_authorized_paths_accepted"
        self._event(
            job,
            "scope-amendment",
            outcome,
            _now(),
            summary=f"authorized scope amendment {outcome}",
            detail=audit,
            attempt=job.attempts,
        )
        return can_continue

    async def _rebase_conflict(self, job: Job, failure: str) -> None:
        """Merge-boundary conflict: resolve it with the fix_conflict agent, but on a
        SEPARATE ``rebase_attempts`` budget so merge contention never spends a FIX
        (bug-fix) credit. Backs off first (``rebase_retry_after``, honored by
        ``claim``) so a burst of concurrent merges can settle before we re-resolve.

        ``failure`` must be the actionable ``[merge-conflict] …`` message so the FIX
        stage dispatches ``stages.fix_conflict`` rather than the ordinary fixer.
        """
        if job.rebase_attempts >= self.rebase_max_attempts:
            return await self._fail(
                job,
                f"gave up after {job.rebase_attempts} rebase attempt(s).\n\n{failure}",
                failure_code="merge_conflict",
                failure_origin="candidate_code",
                retry_disposition="human_review",
                failure_detail={"rebase_attempts": job.rebase_attempts},
            )
        job.rebase_attempts += 1
        backoff = min(30 * (2 ** (job.rebase_attempts - 1)), 600)
        job.rebase_retry_after = time.time() + backoff
        job.failure = failure
        job.failed_step = job.executing_step or "merge"
        job.failure_code = "merge_conflict"
        job.failure_origin = "candidate_code"
        job.retry_disposition = "fix_worktree"
        job.failure_detail = {"rebase_attempt": job.rebase_attempts}
        job.executing_step = None
        job.stage = Stage.FIX
        job.status = JobStatus.PENDING
        job.owner = ""
        job.lease_until = 0.0
        self.store.save(job)
        log.info(
            "job %s: merge conflict (rebase attempt %s/%s); resolving via fix_conflict after %ss backoff",
            job.id,
            job.rebase_attempts,
            self.rebase_max_attempts,
            backoff,
        )
        await self.notify(
            job.chat_id,
            f"🔀 Job #{job.id}: merge conflict — resolving "
            f"(rebase attempt {job.rebase_attempts}/{self.rebase_max_attempts})…",
            project_id=job.project_id,
            job_id=job.id,
        )

    def _event(
        self,
        job: Job,
        stage: str,
        status: str,
        started: str,
        *,
        summary: str = "",
        detail: dict | None = None,
        usage=None,
        attempt: int = 0,
    ) -> None:
        """Record one step in the job's activity timeline (best-effort)."""
        try:
            self.store.add_event(
                job.id,
                stage,
                status,
                summary=summary[:4000],
                detail=detail or {},
                tokens=(usage.total_tokens if usage else 0),
                cost_usd=(usage.cost_usd if usage else 0.0),
                started_at=started,
                ended_at=_now(),
                attempt=attempt,
                agent_id=job.agent_id,
            )
        except Exception:  # noqa: BLE001 - the timeline must never break the pipeline
            log.warning("failed to record event for job %s stage %s", job.id, stage, exc_info=True)

    def _record_resource(self, job: Job, stage: str, record) -> None:
        """Persist a hardware resource row for one deterministic stage (best-effort)."""
        if record is None:
            return
        try:
            ru = ResourceUsage(
                job_id=job.id,
                stage=stage,
                attempt=job.attempts,
                wall_seconds=record.wall_seconds,
                sampled_at=record.sampled_at,
                cpu_seconds=getattr(record, "cpu_seconds", None),
                peak_rss_bytes=getattr(record, "peak_rss_bytes", None),
                io_read_bytes=getattr(record, "io_read_bytes", None),
                io_write_bytes=getattr(record, "io_write_bytes", None),
                net_bytes=getattr(record, "net_bytes", None),
                net_bytes_approx=getattr(record, "net_bytes_approx", False),
            )
            self.store.record_resource_usage(job.id, stage, job.attempts, ru)
        except Exception:  # noqa: BLE001 - never break the pipeline over telemetry
            log.warning(
                "failed to record resource for job %s stage %s", job.id, stage, exc_info=True
            )

    def _has_live_replacement_fleet(self) -> bool:
        """True if a blue-green replacement fleet is already alive on this host."""
        return self.store.has_fresh_replacement_worker(self._host, os.getpid())

    def _request_self_restart(self) -> None:
        try:
            r = subprocess.run(["systemctl", "--user", "restart", "hyqs-pipeline"], check=False)
            if r.returncode == 0:
                log.info("pipeline self-restart requested via systemctl")
            else:
                log.warning("systemctl restart hyqs-pipeline exited %s", r.returncode)
        except Exception:  # noqa: BLE001
            log.warning("could not request pipeline self-restart", exc_info=True)

    # --- the stage machine ---------------------------------------------
    def _backend(self, job: Job):
        """The coding agent for this job's current stage, per its roster entry.

        ``claim`` already stamped ``job.agent_id``/``provider`` for agentic
        stages; resolve the agent's provider+model into a backend (falling back
        to Claude with the runner default if the agent row vanished mid-flight).
        """
        if self.deployer_mode:
            raise RuntimeError("deployer mode workers must not construct AI backends")
        provider = job.provider or "claude"
        model = self.model
        if job.agent_id is not None:
            spec = self.store.get_agent(job.agent_id)
            if spec is not None:
                provider = spec.provider or provider
                model = spec.model or model
        return build_backend(provider, model, config=self.config)

    async def _advance(self, job: Job) -> None:
        # claim() set status=RUNNING + the lease; each handler advances the job
        # exactly one step. HANDLERS maps the job's current stage (the last
        # checkpoint reached) to the handler that does the *next* unit of work.
        handler = stages.HANDLERS.get(job.stage)
        if handler is None:
            return  # terminal (DONE) or nothing to do
        step = _exec_step(job.stage)
        job.executing_step = step
        self.store.set_executing_step(job.id, step)
        try:
            await handler(self, job)
        finally:
            job.executing_step = None
            self.store.clear_executing_step(job.id)


# The step a worker executes *out of* a given stage — what it's actually doing
# right now, for the live fleet view (keys line up with the UI's stage emojis).
_EXEC_STEP = {
    Stage.QUEUED: "plan",
    Stage.PLAN: "build",
    Stage.LINT: "lint",
    Stage.FIX: "fix",
    Stage.BUILD: "test",
    Stage.TEST: "review",
    Stage.REVIEW: "security",
    Stage.SECURITY: "design-review",
    Stage.DESIGN_REVIEW: "merge",
    Stage.MERGE_VERIFY: "merge-verify",
    Stage.DEPLOY: "deploy",
}


def _exec_step(stage: Stage) -> str:
    return _EXEC_STEP.get(stage, stage.value)


def _render_gate_block(name: str, gate_result: object) -> str:
    result = gate_result if isinstance(gate_result, dict) else {}
    lines = [f"{name}: {result.get('summary') or ''}"]
    findings = result.get("findings")
    if isinstance(findings, list):
        for f in findings:
            if isinstance(f, dict):
                lines.append(f"- [{f.get('severity')}] {f.get('note')}")
    return "\n".join(lines)


def _gate_conflict_message(job: Job) -> str:
    review_block = _render_gate_block("review", job.review)
    security_block = _render_gate_block("security", job.security_review)
    return (
        "The review and security gates enforce mutually exclusive requirements "
        "and keep alternating between the same two verdicts — this needs a "
        f"human ruling instead of another fix attempt.\n\n{review_block}\n\n{security_block}"
    )


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M UTC")
