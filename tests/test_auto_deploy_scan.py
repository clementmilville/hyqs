"""Tests for the auto-deploy poller (_auto_deploy_scan), environment-scoped.

Regression coverage for ledger-app job #681/#682 incident: the poller
used to guard only against jobs already at stage='deploy'
(get_active_deploy_job), so an in-flight feature/fix job still mid-merge was
invisible to it and it filed a redundant "Auto-deploy: origin/main advanced"
job for the same commit the in-flight job was about to ship itself.

Job #1810 re-scoped the poller from project-wide to environment-scoped: it now
iterates only auto_deploy=TRUE environments (#1809) and compares origin/<base>
against that environment's own last deploy, so a promotion-driven environment
(auto_deploy=FALSE) is never auto-deployed.

Uses AsyncMock/MagicMock — no Postgres or git subprocess required.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import Environment, JobSource, JobStatus, Project, Stage
from hyqs.pipeline.supervisor import _auto_deploy_scan

TIP_SHA = "2222222222222222222222222222222222222222"
DEPLOYED_SHA = "1111111111111111111111111111111111111111"

PROJECT = Project(id=1, name="demo", repo_path="/fake/repo")
DEFAULT_ENV = Environment(id=10, project_id=1, name="dev", kind="dev", auto_deploy=True)


def _git_result(ok: bool, stdout: str = "", stderr: str = "") -> GitResult:
    return GitResult(ok=ok, stdout=stdout, stderr=stderr, code=0 if ok else 1)


def _git_side_effect(tip_sha: str = TIP_SHA):
    async def _git(repo_path, *args):
        if args[0] == "fetch":
            return _git_result(True)
        if args[0] == "rev-parse" and str(args[1]).startswith("origin/"):
            return _git_result(True, tip_sha + "\n")
        return _git_result(True)

    return _git


def _make_jobs_store(
    *,
    environments: list[Environment] | None = None,
    projects: dict[int, Project] | None = None,
    active_project_job=None,
    last_deploy_by_env: dict[int, dict | None] | None = None,
) -> MagicMock:
    if environments is None:
        environments = [DEFAULT_ENV]
    if projects is None:
        projects = {PROJECT.id: PROJECT}
    if last_deploy_by_env is None:
        last_deploy_by_env = {}
    jobs = MagicMock()
    jobs.list_auto_deploy_environments.return_value = environments
    jobs.get_project.side_effect = lambda pid: projects.get(pid)
    jobs.get_last_deploy.side_effect = lambda project_id, *, environment_id=None: (
        last_deploy_by_env.get(environment_id)
    )
    jobs.get_active_project_job.return_value = active_project_job
    jobs.create.return_value = MagicMock(id=999)
    meta: dict[str, str] = {}
    jobs.get_meta.side_effect = lambda key, default="": meta.get(key, default)
    jobs.set_meta.side_effect = lambda key, value: meta.__setitem__(key, value)
    return jobs


def _run_scan(jobs: MagicMock, notify=None, config=None, tip_sha: str = TIP_SHA) -> None:
    if config is None:
        config = MagicMock()
        # High default so the pre-existing tests (one scan each) are unaffected
        # by the new attempt cap — relying on MagicMock's implicit int() (which
        # returns 1) would silently break them.
        config.pipeline_auto_deploy_max_attempts = 1000
    with (
        patch(
            "hyqs.pipeline.supervisor.gitops.git",
            new_callable=AsyncMock,
            side_effect=_git_side_effect(tip_sha),
        ),
        patch(
            "hyqs.pipeline.supervisor.gitops.default_branch",
            new_callable=AsyncMock,
            return_value="main",
        ),
    ):
        asyncio.run(_auto_deploy_scan(jobs, config, notify))


def test_active_non_deploy_stage_job_suppresses_duplicate_auto_deploy():
    """An in-flight job mid-merge should stop the poller from filing a duplicate."""
    active_job = MagicMock(id=42, stage=Stage.MERGE, status=JobStatus.RUNNING)
    jobs = _make_jobs_store(
        active_project_job=active_job,
        last_deploy_by_env={DEFAULT_ENV.id: {"deployed_commit": DEPLOYED_SHA, "deployed_at": ""}},
    )

    _run_scan(jobs)

    jobs.get_active_project_job.assert_called_once_with(1)
    jobs.create.assert_not_called()


def test_no_active_job_still_enqueues_auto_deploy_for_out_of_band_advance():
    """With nothing in flight, a genuinely-undeployed advance still gets auto-deployed."""
    jobs = _make_jobs_store(
        active_project_job=None,
        last_deploy_by_env={DEFAULT_ENV.id: {"deployed_commit": DEPLOYED_SHA, "deployed_at": ""}},
    )

    _run_scan(jobs)

    jobs.create.assert_called_once()
    call_kwargs = jobs.create.call_args.kwargs
    assert call_kwargs["initial_stage"] == Stage.DEPLOY
    assert call_kwargs["source"] == JobSource.SUPERVISOR
    assert call_kwargs["source_actor"] == "auto-deploy"


def test_completed_deploy_only_job_does_not_block_later_advance():
    """A finished poller-created deploy job (status=done) is no longer 'active',
    so it must not suppress a later legitimate advance forever."""
    jobs = _make_jobs_store(
        active_project_job=None,  # get_active_project_job only matches pending/running/deploying
        last_deploy_by_env={DEFAULT_ENV.id: {"deployed_commit": DEPLOYED_SHA, "deployed_at": ""}},
    )

    _run_scan(jobs)

    jobs.create.assert_called_once()


def test_already_deployed_tip_skips_without_checking_active_job():
    jobs = _make_jobs_store(
        active_project_job=None,
        last_deploy_by_env={DEFAULT_ENV.id: {"deployed_commit": TIP_SHA, "deployed_at": ""}},
    )

    _run_scan(jobs)

    jobs.create.assert_not_called()


def test_repeated_failed_deploys_at_same_tip_stop_after_max_attempts():
    """Every prior attempt at this tip failed (a FAILED job is never 'active'), so
    the poller would otherwise re-file an identical deploy job on every scan
    forever. It must stop after pipeline_auto_deploy_max_attempts."""
    jobs = _make_jobs_store(
        active_project_job=None,
        last_deploy_by_env={DEFAULT_ENV.id: {"deployed_commit": DEPLOYED_SHA, "deployed_at": ""}},
    )
    config = MagicMock()
    config.pipeline_auto_deploy_max_attempts = 3

    for _ in range(5):
        _run_scan(jobs, config=config)

    assert jobs.create.call_count == 3


def test_new_commit_resets_attempt_cap_after_previous_tip_capped():
    """A genuinely new commit (new tip) must get its own fresh attempt counter,
    so a pushed fix still deploys normally even after the old tip capped out."""
    jobs = _make_jobs_store(
        active_project_job=None,
        last_deploy_by_env={DEFAULT_ENV.id: {"deployed_commit": DEPLOYED_SHA, "deployed_at": ""}},
    )
    config = MagicMock()
    config.pipeline_auto_deploy_max_attempts = 3

    for _ in range(5):
        _run_scan(jobs, config=config)
    assert jobs.create.call_count == 3

    new_tip = "3333333333333333333333333333333333333333"
    _run_scan(jobs, config=config, tip_sha=new_tip)

    assert jobs.create.call_count == 4


def test_single_auto_deploy_env_matches_legacy_single_project_behavior():
    """A project with exactly one auto_deploy=TRUE environment (host_id=None)
    behaves identically to the pre-#1810 single-project poller: one deploy job
    filed when the tip advances, none when already deployed at that env's tip."""
    jobs = _make_jobs_store(
        active_project_job=None,
        last_deploy_by_env={DEFAULT_ENV.id: {"deployed_commit": DEPLOYED_SHA, "deployed_at": ""}},
    )

    _run_scan(jobs)

    jobs.create.assert_called_once()

    jobs_deployed = _make_jobs_store(
        active_project_job=None,
        last_deploy_by_env={DEFAULT_ENV.id: {"deployed_commit": TIP_SHA, "deployed_at": ""}},
    )
    _run_scan(jobs_deployed)
    jobs_deployed.create.assert_not_called()


def test_promotion_only_environment_never_auto_deployed():
    """A second environment with auto_deploy=FALSE must never trigger jobs.create,
    even when origin has advanced past its deployed_commit — it ships only via
    promote_release. list_auto_deploy_environments is what filters it out, so it
    must never even appear in the environments the scan iterates."""
    promo_env = Environment(id=11, project_id=1, name="prod", kind="prod", auto_deploy=False)
    # Only the auto_deploy=TRUE environment is returned — mirrors list_auto_deploy_environments'
    # own SQL filter (WHERE auto_deploy = TRUE).
    jobs = _make_jobs_store(
        environments=[DEFAULT_ENV],
        active_project_job=None,
        last_deploy_by_env={DEFAULT_ENV.id: {"deployed_commit": DEPLOYED_SHA, "deployed_at": ""}},
    )

    _run_scan(jobs)

    jobs.create.assert_called_once()
    assert jobs.create.call_args.kwargs["source_meta"] == {"environment_id": DEFAULT_ENV.id}
    # The promotion-only env's id never surfaces anywhere in the filed job.
    assert promo_env.id != jobs.create.call_args.kwargs["source_meta"]["environment_id"]


def test_two_auto_deploy_environments_have_independent_attempt_caps():
    """Two auto_deploy=TRUE environments (different env.id) must each get their
    own attempt-cap counter — capping one at max_attempts must not suppress the
    other."""
    env_a = Environment(id=20, project_id=1, name="dev", kind="dev", auto_deploy=True)
    env_b = Environment(id=21, project_id=1, name="staging-auto", kind="dev", auto_deploy=True)
    jobs = _make_jobs_store(
        environments=[env_a, env_b],
        active_project_job=None,
        last_deploy_by_env={
            env_a.id: {"deployed_commit": DEPLOYED_SHA, "deployed_at": ""},
            env_b.id: {"deployed_commit": DEPLOYED_SHA, "deployed_at": ""},
        },
    )
    config = MagicMock()
    config.pipeline_auto_deploy_max_attempts = 2

    for _ in range(5):
        _run_scan(jobs, config=config)

    # Each env is capped at 2 attempts per scan-round, so 5 scans * 2 envs
    # caps out at 2 jobs per env = 4 total, not 2.
    assert jobs.create.call_count == 4
    env_ids_seen = {c.kwargs["source_meta"]["environment_id"] for c in jobs.create.call_args_list}
    assert env_ids_seen == {env_a.id, env_b.id}


def test_filed_job_source_meta_has_environment_id_and_no_release_id():
    """The auto-deploy job carries environment_id but NOT release_id, so
    stages/deploy.py's build-from-source path fires, not pull-by-digest."""
    jobs = _make_jobs_store(
        active_project_job=None,
        last_deploy_by_env={DEFAULT_ENV.id: {"deployed_commit": DEPLOYED_SHA, "deployed_at": ""}},
    )

    _run_scan(jobs)

    source_meta = jobs.create.call_args.kwargs["source_meta"]
    assert source_meta == {"environment_id": DEFAULT_ENV.id}
    assert "release_id" not in source_meta
