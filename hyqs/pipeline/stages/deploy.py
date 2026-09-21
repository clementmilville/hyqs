"""Deploy stage: run the deploy, verify the live commit after a self-update restart.

Stage handler for DEPLOY → DONE. Extracted from PipelineRunner._advance; logic unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .. import deploy, deployer_state, gitops, supervisor
from ..models import JobStatus, Stage, _now

if TYPE_CHECKING:
    from ..models import Job
    from ..runner import PipelineRunner

log = logging.getLogger("hyqs.runner")


async def run(rn: "PipelineRunner", job: "Job") -> None:
    started = _now()
    _environment_id = (job.source_meta or {}).get("environment_id")
    _release_id = (job.source_meta or {}).get("release_id")
    _release = rn.store.get_release(_release_id) if _release_id else None
    rn.store.advance_promotion_deploying(job.id)
    managed = await rn._managed_repo(job)
    base = await gitops.default_branch(managed)
    deploy_cmd = getattr(rn.config, "pipeline_deploy_cmd", "")
    _project = rn.store.get_project(job.project_id) if job.project_id else None
    _deploy_config = _project.deploy_config if _project else ""
    _web_service = getattr(rn.config, "pipeline_web_service", "")
    try:
        _health_url = json.loads(_deploy_config or "{}").get("web_health_url", "")
    except ValueError:
        log.warning(
            "project %s deploy_config is not valid JSON; ignoring web_health_url", job.project_id
        )
        _health_url = ""
    if _health_url:
        try:
            deploy._validate_health_url(_health_url)
        except ValueError as exc:
            return await rn._fail(job, f"invalid web_health_url in deploy_config: {exc}")

    async def _deploy_sink(line: str) -> None:
        rn.store.append_log(job.id, job.stage.value, line, job.attempts)

    _prev = await gitops.git(job.repo_path, "rev-parse", "HEAD")
    _prev_commit = _prev.stdout.strip() if _prev.ok else ""
    _prev_deploy = (
        rn.store.get_last_deploy(job.project_id, environment_id=_environment_id)
        if job.project_id
        else None
    )
    _prev_deploy_sha = _prev_deploy.get("deployed_commit") if _prev_deploy else None

    # Acquire per-project deploy lock to serialize concurrent deploys.
    _lock_owner = f"job-{job.id}"
    _locked = False
    if job.project_id:
        _locked = await rn.store.acquire_deploy_lock(
            job.project_id, _lock_owner, environment_id=_environment_id
        )
        if not _locked:
            return await rn._fail(job, "deploy lock timeout: another deploy is running")

    try:
        # Coalescing: compare origin/<base> (what we are about to ship) against the
        # last deployed sha.  Using local HEAD is wrong for GitHub-PR merges: those
        # land on origin without advancing the local checkout, so HEAD stays frozen
        # and every subsequent job would coalesce — the app never gets new code.
        if job.project_id and _locked:
            _fetch = await gitops.git(job.repo_path, "fetch", "origin", base)
            if not _fetch.ok:
                log.warning(
                    "job %s: coalesce fetch failed (%s); falling through to real deploy",
                    job.id,
                    _fetch.stderr.strip(),
                )
            else:
                _target = await gitops.git(job.repo_path, "rev-parse", f"origin/{base}")
                _target_sha = _target.stdout.strip() if _target.ok else ""
                if (
                    _target_sha
                    and _prev_deploy
                    and _prev_deploy.get("deployed_commit") == _target_sha
                ):
                    log.info("job %s: coalescing — sha %s already live", job.id, _target_sha[:12])
                    rn._event(
                        job, "deploy", "done", started, summary="deploy/done (coalesced)", detail={}
                    )
                    job.deployed_commit = _target_sha
                    job.stage = Stage.DONE
                    job.status = JobStatus.DONE
                    rn.store.finalize_deploy_done(job.id, _target_sha)
                    rn.store.record_deploy(
                        job.project_id,
                        _target_sha,
                        _prev_deploy_sha,
                        "pipeline_job",
                        job_id=job.id,
                        environment_id=_environment_id,
                    )
                    if _environment_id and _release_id:
                        rn.store.advance_promotion_deployed(job.id, _environment_id, _release_id)
                    if rn.deployer_mode:
                        env = rn.store.get_or_create_default_environment(job.project_id)
                        deployer_state.record_deploy(
                            rn.config.data_dir,
                            job_id=job.id,
                            project_id=job.project_id,
                            environment_id=env.id,
                            deployed_commit=_target_sha,
                        )
                    rn.store.release_schema_lock(job.project_id, f"job-{job.id}")
                    await rn.notify(
                        job.chat_id,
                        f"✅ Job #{job.id}: already deployed. Done.",
                        project_id=job.project_id,
                        job_id=job.id,
                    )
                    await supervisor.unblock_ready_dependents(
                        rn.store, job.id, rn.config, rn.notify
                    )
                    return

        result = await deploy.run_deploy(
            job.repo_path,
            deploy_cmd,
            projects_dir=getattr(rn.config, "projects_dir", ""),
            deploy_config=_deploy_config,
            log_sink=_deploy_sink,
            base_branch=base,
            web_service=_web_service,
            prev_commit=_prev_commit,
            health_url=_health_url,
            release=_release,
        )
    finally:
        if _locked and job.project_id:
            rn.store.release_deploy_lock(job.project_id, _lock_owner)

    rn._record_resource(job, "deploy", result.get("resource"))
    log.info("job %s deploy via `%s`: deployed=%s", job.id, result["command"], result["deployed"])
    detail = {"command": result.get("command", ""), "output": result.get("output", "")}
    if not result.get("deployed"):
        rn._event(
            job,
            "deploy",
            "failed",
            started,
            summary=f"{result.get('command')} — failed",
            detail=detail,
        )
        deploy_err = f"deploy failed ({result.get('command')}):\n{result.get('output', '')}"
        # Always fail at DEPLOY — never route into the in-job FIX loop. The worktree
        # was removed at merge, so FIX cannot run; worse, failing at stage FIX hides
        # the deploy stage from the supervisor's classifier, which is what files the
        # fix-forward job (remediate_deploy_failed) on current main.
        return await rn._fail(job, deploy_err)

    if result.get("command") == "(none)":
        # No deploy command configured — commit hash is not meaningful.
        deployed_commit = ""
    else:
        _head = await gitops.git(job.repo_path, "rev-parse", "HEAD")
        deployed_commit = _head.stdout.strip() if _head.ok else ""
        if not deployed_commit:
            return await rn._fail(
                job,
                f"deploy succeeded but could not read HEAD commit: {_head.stderr}",
            )

    if deploy.web_swap_confirmed(result):
        _project_name = _project.name if _project else f"project {job.project_id}"
        await rn.notify(
            job.chat_id,
            f"\U0001f504 {_project_name}: hyqs-web was just replaced (now serving "
            f"{deployed_commit[:12]}). Streaming and MCP sessions were reset — "
            "reconnect if yours dropped.",
            project_id=job.project_id,
            job_id=job.id,
            event_type="deploy",
        )

    # S1: check verified BEFORE self_update so a failed health check blocks the restart.
    if not result.get("verified", True):
        rn._event(
            job,
            "deploy",
            "failed",
            started,
            summary=f"{result.get('command')} — service did not come live",
            detail=detail,
        )
        return await rn._fail(
            job,
            f"deploy succeeded but service did not come live: {result.get('command')}",
        )

    if result.get("self_update"):
        # Deploy touched pipeline code: save DEPLOYING and restart.
        # _reconcile_startup() on the new process finalizes to DONE once
        # it confirms the running commit matches deployed_commit.
        rn.store.save_deploying(job.id, deployed_commit)
        job.deployed_commit = deployed_commit
        job.stage = Stage.DEPLOY
        job.status = JobStatus.DEPLOYING
        rn._event(
            job,
            "deploy",
            "deploying",
            started,
            summary="restarting to apply pipeline update…",
            detail=detail,
        )
        await rn.notify(
            job.chat_id,
            f"♻️ Job #{job.id}: restarting to apply pipeline update…",
            project_id=job.project_id,
            job_id=job.id,
        )
        # A blue-green deploy command (release.sh) may already have started a
        # replacement fleet before we got here. Cold-starting the singular
        # service on top of it produces a duplicate fleet, so only restart
        # when no live replacement is detected.
        if rn._has_live_replacement_fleet():
            log.info(
                "job %s: blue-green swap already handed over to a live replacement "
                "fleet; skipping self-restart",
                job.id,
            )
        else:
            rn._request_self_restart()
        return

    rn._event(
        job,
        "deploy",
        "done",
        started,
        summary=f"{result.get('command')} — deployed",
        detail=detail,
    )
    job.deployed_commit = deployed_commit or None
    job.stage = Stage.DONE
    job.status = JobStatus.DONE
    if deployed_commit:
        rn.store.finalize_deploy_done(job.id, deployed_commit)
        if job.project_id:
            rn.store.record_deploy(
                job.project_id,
                deployed_commit,
                _prev_deploy_sha,
                "pipeline_job",
                job_id=job.id,
                environment_id=_environment_id,
                image_ref=result.get("image_ref") or None,
                image_digest=result.get("image_digest") or None,
                signed=bool(result.get("signed")),
            )
            image_digest = result.get("image_digest") or None
            env = None
            if image_digest or rn.deployer_mode:
                env = rn.store.get_or_create_default_environment(job.project_id)
            if image_digest:
                release = rn.store.get_release_by_digest(job.project_id, image_digest)
                if release is None:
                    release = rn.store.create_release(
                        job.project_id,
                        deployed_commit,
                        result.get("image_ref") or "",
                        image_digest,
                        signature_ref=(
                            f"{result.get('image_ref')}@{image_digest}"
                            if result.get("signed")
                            else None
                        ),
                        built_by=f"job-{job.id}",
                    )
                rn.store.set_environment_current_release(env.id, release.id)
            if rn.deployer_mode:
                deployer_state.record_deploy(
                    rn.config.data_dir,
                    job_id=job.id,
                    project_id=job.project_id,
                    environment_id=env.id,
                    deployed_commit=deployed_commit,
                )
            if _environment_id and _release_id:
                rn.store.advance_promotion_deployed(job.id, _environment_id, _release_id)
    else:
        rn.store.save(job)
    if job.project_id:
        rn.store.release_schema_lock(job.project_id, f"job-{job.id}")
    await rn.notify(
        job.chat_id,
        f"\U0001f680 Job #{job.id} deployed. Done.",
        project_id=job.project_id,
        job_id=job.id,
    )
    await supervisor.unblock_ready_dependents(rn.store, job.id, rn.config, rn.notify)
    return
