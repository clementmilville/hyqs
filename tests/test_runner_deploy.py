"""Regression tests for the deploy-stage coalesce fix.

The coalesce short-circuit must compare origin/<base> (what will be shipped)
against last_deploy.deployed_commit, NOT the stale local HEAD.  GitHub-PR
merges land on origin without advancing the local checkout, so HEAD stays
frozen and the old (wrong) check caused an endless coalesce loop.

Five tests cover both the docker (user-project) and release.sh (platform /
hyqs-web) deploy paths, plus the fetch-failure fallback.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline import deploy as deploy_module
from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import Job, JobStatus, Release, Stage
from hyqs.pipeline.stages.deploy import run as deploy_run

# LOCAL_SHA simulates the stale local HEAD (what the checkout is frozen at).
# ORIGIN_SHA simulates a new commit that merged to origin/main via GitHub PR.
LOCAL_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ORIGIN_SHA = "1111111111111111111111111111111111111111"

_SUCCESS_DEPLOY_RESULT = {
    "deployed": True,
    "command": "bash deploy/release.sh",
    "output": "deployed ok",
    "self_update": False,
    "verified": True,
    "resource": None,
}


def _git_result(ok: bool, stdout: str = "", stderr: str = "") -> GitResult:
    return GitResult(ok=ok, stdout=stdout, stderr=stderr, code=0 if ok else 1)


def _make_job(project_id: int = 1, source_meta: dict | None = None) -> Job:
    return Job(
        id=42,
        idea="test deploy",
        repo_path="/fake/repo",
        chat_id=99,
        stage=Stage.DEPLOY,
        status=JobStatus.RUNNING,
        project_id=project_id,
        attempts=0,
        source_meta=source_meta,
    )


def _make_runner(last_deployed_sha: str, deploy_cmd: str = "") -> MagicMock:
    store = MagicMock()
    store.acquire_deploy_lock = AsyncMock(return_value=True)
    store.release_deploy_lock = MagicMock()
    store.get_project.return_value = MagicMock(deploy_config="")
    store.get_last_deploy.return_value = {"deployed_commit": last_deployed_sha}
    store.finalize_deploy_done = MagicMock()
    store.append_log = MagicMock()
    store.save = MagicMock()
    store.advance_promotion_deploying = MagicMock()
    store.advance_promotion_deployed = MagicMock()

    config = MagicMock()
    config.pipeline_deploy_cmd = deploy_cmd
    config.projects_dir = "/fake/projects"
    config.pipeline_web_service = ""

    rn = MagicMock()
    rn.store = store
    rn.config = config
    rn.deployer_mode = False
    rn.notify = AsyncMock()
    rn._managed_repo = AsyncMock(return_value="/fake/repo")
    rn._event = MagicMock()
    rn._fail = AsyncMock()
    rn._retry_or_fail = AsyncMock()
    rn._record_resource = MagicMock()
    rn._request_self_restart = MagicMock()
    return rn


def _make_git_side_effect(
    *,
    fetch_ok: bool = True,
    origin_sha: str = ORIGIN_SHA,
    local_sha: str = LOCAL_SHA,
):
    """Return an async side_effect for gitops.git covering all calls the stage makes."""

    async def _git(repo_path, *args):
        if args == ("rev-parse", "HEAD"):
            return _git_result(True, local_sha + "\n")
        if args[0] == "fetch":
            return _git_result(fetch_ok, stderr="" if fetch_ok else "network error")
        if args[0] == "rev-parse" and str(args[1]).startswith("origin/"):
            return _git_result(True, origin_sha + "\n")
        return _git_result(True)

    return _git


# ---------------------------------------------------------------------------
# Test 1 — user-project (docker): origin ahead, local HEAD stale → no coalesce
# ---------------------------------------------------------------------------


def test_origin_ahead_local_stale_does_not_coalesce_user_project():
    """Regression: origin/main is ahead of last_deploy while local HEAD == last_deploy.

    This is the exact bug that froze user projects after GitHub-PR merges:
    the old code compared local HEAD to last_deploy and they matched, so every
    subsequent deploy coalesced and the app never got new code.
    """

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_SUCCESS_DEPLOY_RESULT,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,  # origin is AHEAD
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)  # last deployed == local HEAD (stale)

            await deploy_run(rn, job)

            mock_run_deploy.assert_called_once()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 2 — user-project: origin matches last_deploy → coalesce fires correctly
# ---------------------------------------------------------------------------


def test_origin_matches_last_deploy_coalesces_user_project():
    """origin/main == last_deploy.deployed_commit → correct coalesce.

    run_deploy must not be called; the job must be finalised as DONE with
    the origin sha recorded as deployed_commit.
    """

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=ORIGIN_SHA)  # last deployed == origin → coalesce

            await deploy_run(rn, job)

            mock_run_deploy.assert_not_called()
            rn.store.finalize_deploy_done.assert_called_once_with(job.id, ORIGIN_SHA)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 3 — fetch failure falls through to a real deploy (no silent coalesce)
# ---------------------------------------------------------------------------


def test_fetch_failure_falls_through_to_real_deploy():
    """A fetch failure must not silently skip the deploy — fall through to run_deploy."""

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_SUCCESS_DEPLOY_RESULT,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(fetch_ok=False)
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)

            await deploy_run(rn, job)

            mock_run_deploy.assert_called_once()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 4 — release.sh path: origin ahead, local HEAD stale → no coalesce
# ---------------------------------------------------------------------------


def test_origin_ahead_local_stale_does_not_coalesce_release_sh_path():
    """Same regression as test 1 for the non-user-project (release.sh) deploy path.

    Guards the hyqs-web self-deploy: the single coalesce fix in stages/deploy.py
    covers both docker and release.sh paths because the check runs before
    run_deploy branches into either path.
    """

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value={**_SUCCESS_DEPLOY_RESULT, "command": "bash deploy/release.sh"},
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=2)
            # deploy_cmd simulates the platform (non-docker) project
            rn = _make_runner(last_deployed_sha=LOCAL_SHA, deploy_cmd="bash deploy/release.sh")

            await deploy_run(rn, job)

            mock_run_deploy.assert_called_once()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 5 — release.sh path: origin matches last_deploy → coalesce fires
# ---------------------------------------------------------------------------


def test_origin_matches_last_deploy_coalesces_release_sh_path():
    """Coalesce fires correctly for the release.sh path just as for docker.

    Verifies the coalesce decision is identical regardless of deploy mechanism.
    """

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=2)
            rn = _make_runner(last_deployed_sha=ORIGIN_SHA, deploy_cmd="bash deploy/release.sh")

            await deploy_run(rn, job)

            mock_run_deploy.assert_not_called()
            rn.store.finalize_deploy_done.assert_called_once_with(job.id, ORIGIN_SHA)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests 6-7 — self_update branch: gate the self-restart on a live replacement
# fleet, to avoid the duplicate-fleet bug (job #1433).
# ---------------------------------------------------------------------------

_SELF_UPDATE_DEPLOY_RESULT = {
    "deployed": True,
    "command": "bash deploy/release.sh",
    "output": "deployed ok",
    "self_update": True,
    "verified": True,
    "resource": None,
}


def test_self_update_skips_self_restart_when_live_replacement_fleet_exists():
    """release.sh already blue-green swapped in a fresh fleet: don't cold-start
    the singular service on top of it — that's what produced the duplicate fleet."""

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_SELF_UPDATE_DEPLOY_RESULT,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)
            rn._has_live_replacement_fleet = MagicMock(return_value=True)

            await deploy_run(rn, job)

            mock_run_deploy.assert_called_once()
            rn.store.save_deploying.assert_called_once()
            rn._has_live_replacement_fleet.assert_called_once()
            rn._request_self_restart.assert_not_called()

    asyncio.run(_run())


def test_self_update_requests_self_restart_when_no_live_replacement_fleet():
    """A plain restartless deploy cmd never swaps fleets: the systemctl restart
    fallback must still fire so the pipeline picks up its own code update."""

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_SELF_UPDATE_DEPLOY_RESULT,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)
            rn._has_live_replacement_fleet = MagicMock(return_value=False)

            await deploy_run(rn, job)

            mock_run_deploy.assert_called_once()
            rn.store.save_deploying.assert_called_once()
            rn._has_live_replacement_fleet.assert_called_once()
            rn._request_self_restart.assert_called_once()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 8 — a successful deploy triggers the immediate dependent-unblock hook
# ---------------------------------------------------------------------------


def test_successful_deploy_triggers_unblock_ready_dependents():
    """Every DONE transition in the deploy stage must fire the immediate
    dependent-unblock check (job #1440) — not just wait for the next janitor
    scan. This proves the wiring; the underlying logic is covered by
    tests/test_dependency_auto_unblock.py."""

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_SUCCESS_DEPLOY_RESULT,
            ),
            patch(
                "hyqs.pipeline.stages.deploy.supervisor.unblock_ready_dependents",
                new_callable=AsyncMock,
            ) as mock_unblock,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)

            await deploy_run(rn, job)

            mock_unblock.assert_awaited_once_with(rn.store, job.id, rn.config, rn.notify)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests 9-10 — artifact fields (image_ref/image_digest/signed) are forwarded
# from the deploy result into the ledger write, defaulting safely when absent.
# ---------------------------------------------------------------------------


def test_record_deploy_forwards_artifact_fields_when_present():
    """A docker deploy with registry push+sign returns image_ref/image_digest/
    signed in its result dict; those must land on the record_deploy() call."""

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value={
                    **_SUCCESS_DEPLOY_RESULT,
                    "image_ref": "registry.example.com/proj/app",
                    "image_digest": "sha256:abc123",
                    "signed": True,
                },
            ),
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)

            await deploy_run(rn, job)

            rn.store.record_deploy.assert_called_once_with(
                job.project_id,
                LOCAL_SHA,
                LOCAL_SHA,
                "pipeline_job",
                job_id=job.id,
                environment_id=None,
                image_ref="registry.example.com/proj/app",
                image_digest="sha256:abc123",
                signed=True,
            )

    asyncio.run(_run())


def test_record_deploy_defaults_artifact_fields_when_absent():
    """Non-docker deploys (release.sh) or a not-configured registry return no
    image_ref/image_digest/signed keys — record_deploy must default them
    safely, identical to pre-change behavior."""

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_SUCCESS_DEPLOY_RESULT,
            ),
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)

            await deploy_run(rn, job)

            rn.store.record_deploy.assert_called_once_with(
                job.project_id,
                LOCAL_SHA,
                LOCAL_SHA,
                "pipeline_job",
                job_id=job.id,
                environment_id=None,
                image_ref=None,
                image_digest=None,
                signed=False,
            )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests 11-13 — a signed-push deploy creates a release (idempotent on digest)
# and points the deploying environment at it; an unconfigured registry
# (no image_digest) creates no release at all (job #1783/#1784, Epic 81).
# ---------------------------------------------------------------------------


def test_successful_signed_deploy_creates_release_and_updates_environment():
    """A docker deploy with a fresh image_digest creates exactly one release
    row and points the deploying environment's current_release_id at it."""

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value={
                    **_SUCCESS_DEPLOY_RESULT,
                    "image_ref": "registry.example.com/proj/app",
                    "image_digest": "sha256:abc123",
                    "signed": True,
                },
            ),
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)
            fake_env = MagicMock(id=7)
            rn.store.get_or_create_default_environment.return_value = fake_env
            rn.store.get_release_by_digest.return_value = None
            fake_release = MagicMock(id=99)
            rn.store.create_release.return_value = fake_release

            await deploy_run(rn, job)

            rn.store.get_release_by_digest.assert_called_once_with(job.project_id, "sha256:abc123")
            rn.store.create_release.assert_called_once_with(
                job.project_id,
                LOCAL_SHA,
                "registry.example.com/proj/app",
                "sha256:abc123",
                signature_ref="registry.example.com/proj/app@sha256:abc123",
                built_by=f"job-{job.id}",
            )
            rn.store.set_environment_current_release.assert_called_once_with(7, 99)

    asyncio.run(_run())


def test_signed_deploy_is_idempotent_on_existing_digest():
    """If a release already exists for this image_digest, do not create a
    duplicate — but still point the environment's current_release_id at it."""

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value={
                    **_SUCCESS_DEPLOY_RESULT,
                    "image_ref": "registry.example.com/proj/app",
                    "image_digest": "sha256:abc123",
                    "signed": True,
                },
            ),
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)
            fake_env = MagicMock(id=7)
            rn.store.get_or_create_default_environment.return_value = fake_env
            existing_release = MagicMock(id=55)
            rn.store.get_release_by_digest.return_value = existing_release

            await deploy_run(rn, job)

            rn.store.create_release.assert_not_called()
            rn.store.set_environment_current_release.assert_called_once_with(7, 55)

    asyncio.run(_run())


def test_deploy_without_registry_creates_no_release():
    """No image_digest in the deploy result (no registry configured) must
    create zero releases and leave current_release_id untouched — exactly
    today's behavior."""

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_SUCCESS_DEPLOY_RESULT,
            ),
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)

            await deploy_run(rn, job)

            rn.store.create_release.assert_not_called()
            rn.store.set_environment_current_release.assert_not_called()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests 14-15 — promotion-linked jobs (source_meta carries environment_id +
# release_id) drive the promotion lifecycle through the DEPLOY stage; jobs
# with no such source_meta behave exactly as before (job #1792).
# ---------------------------------------------------------------------------


def test_promotion_linked_deploy_advances_promotion_lifecycle():
    """A DEPLOY-stage job whose source_meta carries environment_id/release_id
    must: mark DEPLOYING immediately, thread environment_id through the lock/
    lookup/record calls, and advance the promotion to DEPLOYED on success."""

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_SUCCESS_DEPLOY_RESULT,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=1, source_meta={"environment_id": 5, "release_id": 9})
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)

            await deploy_run(rn, job)

            mock_run_deploy.assert_called_once()
            rn.store.advance_promotion_deploying.assert_called_once_with(job.id)
            rn.store.acquire_deploy_lock.assert_called_once_with(
                job.project_id, f"job-{job.id}", environment_id=5
            )
            rn.store.get_last_deploy.assert_called_once_with(job.project_id, environment_id=5)
            rn.store.record_deploy.assert_called_once_with(
                job.project_id,
                LOCAL_SHA,
                LOCAL_SHA,
                "pipeline_job",
                job_id=job.id,
                environment_id=5,
                image_ref=None,
                image_digest=None,
                signed=False,
            )
            rn.store.advance_promotion_deployed.assert_called_once_with(job.id, 5, 9)

    asyncio.run(_run())


def test_unlinked_deploy_never_touches_promotion_lifecycle():
    """A job with no source_meta (or source_meta=None) must not call
    advance_promotion_deployed, while advance_promotion_deploying is still
    called (a cheap no-op for jobs unlinked to a promotion)."""

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_SUCCESS_DEPLOY_RESULT,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True,
                origin_sha=ORIGIN_SHA,
                local_sha=LOCAL_SHA,
            )
            job = _make_job(project_id=1, source_meta=None)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)

            await deploy_run(rn, job)

            mock_run_deploy.assert_called_once()
            rn.store.advance_promotion_deploying.assert_called_once_with(job.id)
            rn.store.acquire_deploy_lock.assert_called_once_with(
                job.project_id, f"job-{job.id}", environment_id=None
            )
            rn.store.advance_promotion_deployed.assert_not_called()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 16 — runner._fail() fails the linked promotion only for DEPLOY-stage
# failures (job #1792).
# ---------------------------------------------------------------------------


def test_fail_fails_promotion_only_for_deploy_stage():
    """PipelineRunner._fail() must call store.fail_promotion for a DEPLOY-stage
    failure, and must NOT call it for a failure at any other stage."""
    from hyqs.pipeline.runner import PipelineRunner

    async def _run():
        rn = MagicMock(spec=PipelineRunner)
        rn.store = MagicMock()
        rn.store.save = MagicMock()
        rn.store.release_schema_lock = MagicMock()
        rn.store.fail_promotion = MagicMock()
        rn._managed_repo = AsyncMock(return_value="/fake/repo")
        rn.notify = AsyncMock()

        deploy_job = _make_job(project_id=1)
        deploy_job.branch = ""
        deploy_job.stage = Stage.DEPLOY
        await PipelineRunner._fail(rn, deploy_job, "boom")
        rn.store.fail_promotion.assert_called_once_with(deploy_job.id)

        rn.store.fail_promotion.reset_mock()
        build_job = _make_job(project_id=1)
        build_job.branch = ""
        build_job.stage = Stage.BUILD
        await PipelineRunner._fail(rn, build_job, "boom")
        rn.store.fail_promotion.assert_not_called()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests 17-20 — deploy.run_deploy()'s release-aware pull-vs-build dispatch
# (Epic 81 step 4, job #1787): a promotion-driven deploy with a signed release
# pulls by digest instead of building from source; everything else is
# byte-for-byte unchanged.
# ---------------------------------------------------------------------------


def _make_release(image_digest: str | None = "sha256:abc123") -> Release:
    return Release(
        id=9,
        project_id=1,
        source_commit="deadbeef",
        image_ref="registry.example.com/proj/app",
        image_digest=image_digest,
    )


def test_run_deploy_with_release_none_matches_pre_change_docker_deploy_call(tmp_path):
    repo_path = tmp_path / "projects" / "myproj"
    repo_path.mkdir(parents=True)

    async def _run():
        with (
            patch(
                "hyqs.pipeline.docker_deploy.docker_deploy",
                new_callable=AsyncMock,
                return_value={"deployed": True},
            ) as mock_docker_deploy,
            patch(
                "hyqs.pipeline.docker_deploy.pull_deploy", new_callable=AsyncMock
            ) as mock_pull_deploy,
        ):
            result = await deploy_module.run_deploy(
                repo_path,
                projects_dir=tmp_path / "projects",
                deploy_config="{}",
            )

            mock_docker_deploy.assert_called_once_with(repo_path, "{}", log_sink=None)
            mock_pull_deploy.assert_not_called()
            assert result == {"deployed": True}

    asyncio.run(_run())


def test_run_deploy_with_signed_release_and_pull_configured_calls_pull_deploy(tmp_path):
    repo_path = tmp_path / "projects" / "myproj"
    repo_path.mkdir(parents=True)
    release = _make_release()

    async def _run():
        with (
            patch(
                "hyqs.pipeline.deploy.registry_push.pull_configured",
                return_value=True,
            ),
            patch(
                "hyqs.pipeline.docker_deploy.pull_deploy",
                new_callable=AsyncMock,
                return_value={"deployed": True, "image_digest": "sha256:abc123"},
            ) as mock_pull_deploy,
            patch(
                "hyqs.pipeline.docker_deploy.docker_deploy", new_callable=AsyncMock
            ) as mock_docker_deploy,
        ):
            result = await deploy_module.run_deploy(
                repo_path,
                projects_dir=tmp_path / "projects",
                deploy_config="{}",
                release=release,
            )

            mock_pull_deploy.assert_called_once_with(repo_path, release, "{}", log_sink=None)
            mock_docker_deploy.assert_not_called()
            assert result == {"deployed": True, "image_digest": "sha256:abc123"}

    asyncio.run(_run())


def test_run_deploy_falls_back_when_release_has_no_digest(tmp_path):
    repo_path = tmp_path / "projects" / "myproj"
    repo_path.mkdir(parents=True)
    release = _make_release(image_digest=None)

    async def _run():
        with (
            patch(
                "hyqs.pipeline.deploy.registry_push.pull_configured",
                return_value=True,
            ),
            patch(
                "hyqs.pipeline.docker_deploy.docker_deploy",
                new_callable=AsyncMock,
                return_value={"deployed": True},
            ) as mock_docker_deploy,
            patch(
                "hyqs.pipeline.docker_deploy.pull_deploy", new_callable=AsyncMock
            ) as mock_pull_deploy,
        ):
            await deploy_module.run_deploy(
                repo_path,
                projects_dir=tmp_path / "projects",
                deploy_config="{}",
                release=release,
            )

            mock_docker_deploy.assert_called_once()
            mock_pull_deploy.assert_not_called()

    asyncio.run(_run())


def test_run_deploy_falls_back_when_pull_not_configured(tmp_path):
    repo_path = tmp_path / "projects" / "myproj"
    repo_path.mkdir(parents=True)
    release = _make_release()

    async def _run():
        with (
            patch(
                "hyqs.pipeline.deploy.registry_push.pull_configured",
                return_value=False,
            ),
            patch(
                "hyqs.pipeline.docker_deploy.docker_deploy",
                new_callable=AsyncMock,
                return_value={"deployed": True},
            ) as mock_docker_deploy,
            patch(
                "hyqs.pipeline.docker_deploy.pull_deploy", new_callable=AsyncMock
            ) as mock_pull_deploy,
        ):
            await deploy_module.run_deploy(
                repo_path,
                projects_dir=tmp_path / "projects",
                deploy_config="{}",
                release=release,
            )

            mock_docker_deploy.assert_called_once()
            mock_pull_deploy.assert_not_called()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests 21-22 — the DEPLOY stage looks up job.source_meta's release_id via
# store.get_release() and forwards it to run_deploy() (job #1787).
# ---------------------------------------------------------------------------


def test_deploy_stage_with_no_release_id_never_calls_get_release():
    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_SUCCESS_DEPLOY_RESULT,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True, origin_sha=ORIGIN_SHA, local_sha=LOCAL_SHA
            )
            job = _make_job(project_id=1, source_meta=None)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)
            rn.store.get_release = MagicMock()

            await deploy_run(rn, job)

            rn.store.get_release.assert_not_called()
            _, call_kwargs = mock_run_deploy.call_args
            assert call_kwargs["release"] is None

    asyncio.run(_run())


def test_deploy_stage_with_release_id_looks_up_and_forwards_release():
    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_SUCCESS_DEPLOY_RESULT,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True, origin_sha=ORIGIN_SHA, local_sha=LOCAL_SHA
            )
            job = _make_job(project_id=1, source_meta={"environment_id": 5, "release_id": 9})
            rn = _make_runner(last_deployed_sha=LOCAL_SHA)
            fake_release = MagicMock()
            rn.store.get_release = MagicMock(return_value=fake_release)

            await deploy_run(rn, job)

            rn.store.get_release.assert_called_once_with(9)
            _, call_kwargs = mock_run_deploy.call_args
            assert call_kwargs["release"] is fake_release

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests 23-25 — a confirmed hyqs-web blue-green swap notifies holders of
# long-lived connections (SSE/MCP) to reconnect; anything short of a
# confirmed swap must stay silent (job #3050).
# ---------------------------------------------------------------------------

_WEB_SWAP_CONFIRMED_RESULT = {
    "deployed": True,
    "command": "bash deploy/release.sh",
    "output": (
        "hyqs-web@2.service (checked pids: 12345) is healthy (matching pid "
        "confirmed via http://127.0.0.1:8000/health)."
    ),
    "self_update": False,
    "verified": True,
    "resource": None,
}

_WEB_SWAP_SKIPPED_RESULT = {
    "deployed": True,
    "command": "bash deploy/release.sh",
    "output": "SKIP_WEB_RELEASE: all changed paths are limited to decisions, docs, or tests",
    "self_update": False,
    "verified": True,
    "resource": None,
}

_WEB_SWAP_FAILED_RESULT = {
    "deployed": False,
    "command": "bash deploy/release.sh",
    "output": "release.sh exited 1",
    "self_update": False,
    "verified": False,
    "resource": None,
}


def _deploy_event_notify_calls(rn):
    return [c for c in rn.notify.call_args_list if c.kwargs.get("event_type") == "deploy"]


def test_confirmed_web_swap_notifies_once_naming_served_commit():
    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_WEB_SWAP_CONFIRMED_RESULT,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True, origin_sha=ORIGIN_SHA, local_sha=LOCAL_SHA
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA, deploy_cmd="bash deploy/release.sh")

            await deploy_run(rn, job)

            mock_run_deploy.assert_called_once()
            swap_calls = _deploy_event_notify_calls(rn)
            assert len(swap_calls) == 1
            message = swap_calls[0].args[1]
            assert LOCAL_SHA[:12] in message

    asyncio.run(_run())


def test_skipped_web_release_emits_no_swap_notification():
    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_WEB_SWAP_SKIPPED_RESULT,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True, origin_sha=ORIGIN_SHA, local_sha=LOCAL_SHA
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA, deploy_cmd="bash deploy/release.sh")

            await deploy_run(rn, job)

            mock_run_deploy.assert_called_once()
            assert _deploy_event_notify_calls(rn) == []

    asyncio.run(_run())


def test_release_failed_before_swap_emits_no_swap_notification():
    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
            ) as mock_git,
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
                return_value=_WEB_SWAP_FAILED_RESULT,
            ) as mock_run_deploy,
        ):
            mock_git.side_effect = _make_git_side_effect(
                fetch_ok=True, origin_sha=ORIGIN_SHA, local_sha=LOCAL_SHA
            )
            job = _make_job(project_id=1)
            rn = _make_runner(last_deployed_sha=LOCAL_SHA, deploy_cmd="bash deploy/release.sh")

            await deploy_run(rn, job)

            mock_run_deploy.assert_called_once()
            rn._fail.assert_called_once()
            assert _deploy_event_notify_calls(rn) == []

    asyncio.run(_run())
