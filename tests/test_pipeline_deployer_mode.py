"""Tests for constrained deployer-mode wiring on PipelineRunner (job #1780).

A deployer-mode worker (``stage_allowlist={Stage.DEPLOY}``, ``deployer_mode=True``)
must: (1) only ever claim DEPLOY jobs, (2) refuse to construct an AI backend, and
(3) record its own claim/deploy activity into the local ``deployer_state`` files —
while a normal full-fleet worker (``deployer_mode=False``, the default) never
touches those local files.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyqs.pipeline import deployer_state
from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import Environment, Job, JobStatus, Stage
from hyqs.pipeline.runner import PipelineRunner
from hyqs.pipeline.stages.deploy import run as deploy_run


class _FakeConfig:
    """A plain (non-Mock) config so unset attrs fall back to real defaults."""

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.model = "claude-test"
        self.projects_dir = ""
        self.pipeline_host_name = ""


def _make_job(job_id: int = 1, project_id: int = 1, stage: Stage = Stage.DEPLOY) -> Job:
    return Job(
        id=job_id,
        idea="deploy job",
        repo_path="/fake/repo",
        chat_id=99,
        stage=stage,
        status=JobStatus.PENDING,
        project_id=project_id,
        provider="",
        attempts=0,
    )


# ---------------------------------------------------------------------------
# S1a — _loop threads stage_allowlist into both claim() and claim_fastpath()
# ---------------------------------------------------------------------------


def test_loop_passes_stage_allowlist_to_claim_and_claim_fastpath(tmp_path):
    job = _make_job()
    store = MagicMock()
    store.worker_heartbeat = AsyncMock()
    store.worker_offline = MagicMock()
    store.claim = AsyncMock(side_effect=[job, None, None])
    store.claim_fastpath = AsyncMock(return_value=None)
    store.get_or_create_default_environment = MagicMock(
        return_value=Environment(id=100, project_id=1, name="default", kind="default")
    )

    config = _FakeConfig(tmp_path)
    rn = PipelineRunner(
        store, config, AsyncMock(), stage_allowlist={Stage.DEPLOY}, deployer_mode=True
    )
    rn._run_stage_with_retry = AsyncMock()

    async def _run():
        task = asyncio.create_task(rn._loop("worker-1"))
        for _ in range(500):
            if store.claim_fastpath.await_count >= 1:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    first_call = store.claim.await_args_list[0]
    assert first_call.args[0] == "worker-1"
    assert first_call.kwargs["stage_allowlist"] == {Stage.DEPLOY}

    fastpath_call = store.claim_fastpath.await_args_list[0]
    assert fastpath_call.args[0] == job.id
    assert fastpath_call.kwargs["stage_allowlist"] == {Stage.DEPLOY}


# ---------------------------------------------------------------------------
# S1b — a successful claim in deployer mode records a local claim event
# ---------------------------------------------------------------------------


def test_loop_records_local_claim_in_deployer_mode(tmp_path):
    job = _make_job(job_id=7, project_id=3)
    store = MagicMock()
    store.worker_heartbeat = AsyncMock()
    store.worker_offline = MagicMock()
    store.claim = AsyncMock(side_effect=[job, None])
    store.claim_fastpath = AsyncMock(return_value=None)
    store.get_or_create_default_environment = MagicMock(
        return_value=Environment(id=200, project_id=3, name="default", kind="default")
    )

    config = _FakeConfig(tmp_path)
    rn = PipelineRunner(store, config, AsyncMock(), deployer_mode=True)
    rn._run_stage_with_retry = AsyncMock()

    async def _run():
        task = asyncio.create_task(rn._loop("worker-1"))
        for _ in range(500):
            if store.claim_fastpath.await_count >= 1:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    state = deployer_state.read_state(tmp_path)
    assert state["last_claim"] == {"job_id": 7, "project_id": 3, "environment_id": 200}


# ---------------------------------------------------------------------------
# S2 — _backend() refuses to construct an AI backend in deployer mode
# ---------------------------------------------------------------------------


def test_backend_raises_in_deployer_mode_without_building(tmp_path):
    store = MagicMock()
    config = _FakeConfig(tmp_path)
    rn = PipelineRunner(store, config, AsyncMock(), deployer_mode=True)
    job = _make_job()

    with patch("hyqs.pipeline.runner.build_backend") as mock_build:
        with pytest.raises(RuntimeError, match="deployer mode workers must not construct AI backends"):
            rn._backend(job)
        mock_build.assert_not_called()


def test_backend_builds_normally_when_not_deployer_mode(tmp_path):
    store = MagicMock()
    config = _FakeConfig(tmp_path)
    rn = PipelineRunner(store, config, AsyncMock(), deployer_mode=False)
    job = _make_job()
    job.provider = "claude"

    with patch("hyqs.pipeline.runner.build_backend") as mock_build:
        rn._backend(job)
        mock_build.assert_called_once()


# ---------------------------------------------------------------------------
# S3 — DEPLOY stage writes local deployer_state records only in deployer mode
# ---------------------------------------------------------------------------

_SUCCESS_DEPLOY_RESULT = {
    "deployed": True,
    "command": "bash deploy/release.sh",
    "output": "deployed ok",
    "self_update": False,
    "verified": True,
    "resource": None,
}

LOCAL_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _make_deploy_runner(tmp_path, *, deployer_mode: bool) -> MagicMock:
    store = MagicMock()
    store.acquire_deploy_lock = AsyncMock(return_value=True)
    store.release_deploy_lock = MagicMock()
    store.get_project.return_value = MagicMock(deploy_config="")
    store.get_last_deploy.return_value = None
    store.finalize_deploy_done = MagicMock()
    store.append_log = MagicMock()
    store.save = MagicMock()
    store.record_deploy = MagicMock()
    store.release_schema_lock = MagicMock()
    store.get_or_create_default_environment = MagicMock(
        return_value=Environment(id=100, project_id=1, name="default", kind="default")
    )

    config = MagicMock()
    config.pipeline_deploy_cmd = ""
    config.projects_dir = "/fake/projects"
    config.pipeline_web_service = ""
    config.data_dir = str(tmp_path)

    rn = MagicMock()
    rn.store = store
    rn.config = config
    rn.deployer_mode = deployer_mode
    rn.notify = AsyncMock()
    rn._managed_repo = AsyncMock(return_value="/fake/repo")
    rn._event = MagicMock()
    rn._fail = AsyncMock()
    rn._retry_or_fail = AsyncMock()
    rn._record_resource = MagicMock()
    rn._request_self_restart = MagicMock()
    rn._has_live_replacement_fleet = MagicMock(return_value=False)
    return rn


def _git_result(ok: bool, stdout: str = "", stderr: str = "") -> GitResult:
    return GitResult(ok=ok, stdout=stdout, stderr=stderr, code=0 if ok else 1)


async def _git_side_effect(repo_path, *args):
    if args == ("rev-parse", "HEAD"):
        return _git_result(True, LOCAL_SHA + "\n")
    return _git_result(True)


@pytest.mark.parametrize("deployer_mode", [True, False])
def test_deploy_stage_normal_completion_records_local_state_only_in_deployer_mode(
    tmp_path, deployer_mode
):
    """Normal completion path (not the coalesce shortcut): record_deploy() at the
    end of run() must only mirror into deployer_state when deployer_mode=True."""
    job = _make_job(job_id=5, project_id=1)

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
                side_effect=_git_side_effect,
            ),
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
            ),
        ):
            rn = _make_deploy_runner(tmp_path, deployer_mode=deployer_mode)
            await deploy_run(rn, job)

    asyncio.run(_run())

    state = deployer_state.read_state(tmp_path)
    if deployer_mode:
        assert state["last_deploy"] == {
            "job_id": job.id,
            "project_id": job.project_id,
            "environment_id": 100,
            "deployed_commit": LOCAL_SHA,
        }
    else:
        assert state == {}


@pytest.mark.parametrize("deployer_mode", [True, False])
def test_deploy_stage_coalesce_shortcut_records_local_state_only_in_deployer_mode(
    tmp_path, deployer_mode
):
    """Coalescing (origin already matches last_deploy) is a separate early-return
    branch — it must get the same deployer_mode-gated deployer_state write."""
    job = _make_job(job_id=9, project_id=1)
    origin_sha = "1111111111111111111111111111111111111111"

    async def _git_coalesce_side_effect(repo_path, *args):
        if args == ("rev-parse", "HEAD"):
            return _git_result(True, LOCAL_SHA + "\n")
        if args[0] == "fetch":
            return _git_result(True)
        if args[0] == "rev-parse" and str(args[1]).startswith("origin/"):
            return _git_result(True, origin_sha + "\n")
        return _git_result(True)

    async def _run():
        with (
            patch(
                "hyqs.pipeline.stages.deploy.gitops.git",
                new_callable=AsyncMock,
                side_effect=_git_coalesce_side_effect,
            ),
            patch(
                "hyqs.pipeline.stages.deploy.gitops.default_branch",
                new_callable=AsyncMock,
                return_value="main",
            ),
            patch(
                "hyqs.pipeline.stages.deploy.deploy.run_deploy",
                new_callable=AsyncMock,
            ) as mock_run_deploy,
            patch(
                "hyqs.pipeline.stages.deploy.supervisor.unblock_ready_dependents",
                new_callable=AsyncMock,
            ),
        ):
            rn = _make_deploy_runner(tmp_path, deployer_mode=deployer_mode)
            rn.store.get_last_deploy.return_value = {"deployed_commit": origin_sha}

            await deploy_run(rn, job)

            mock_run_deploy.assert_not_called()

    asyncio.run(_run())

    state = deployer_state.read_state(tmp_path)
    if deployer_mode:
        assert state["last_deploy"] == {
            "job_id": job.id,
            "project_id": job.project_id,
            "environment_id": 100,
            "deployed_commit": origin_sha,
        }
    else:
        assert state == {}
