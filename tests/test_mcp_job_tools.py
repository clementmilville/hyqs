"""Tests for the MCP job write tools (create/cancel/retry/archive).

Uses the real Postgres ``store`` fixture per CONVENTIONS.md §7 — only external
I/O (git subprocess calls via hyqs.pipeline.github) is mocked.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hyqs.config import Config
from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import Job, JobStatus
from hyqs.web import mcp_server
from hyqs.web.app import _job_diff_payload, job_to_dict
from hyqs.web.auth import AuthContext


def _job(**kw) -> Job:
    defaults = dict(id=1, idea="fix the thing", repo_path="/tmp/repo", chat_id=1)
    defaults.update(kw)
    return Job(**defaults)


def _set_status(store, job_id: int, status: JobStatus) -> None:
    with store._pool.connection() as conn:
        conn.execute("UPDATE jobs SET status=%s WHERE id=%s", (status.value, job_id))


@pytest.fixture
def mcp_env(store):
    repo_path = f"/tmp/test-mcp-{uuid.uuid4()}"
    project = store.create_project(f"MCP Project {uuid.uuid4()}", repo_path)
    epic = store.create_epic(project.id, "Epic A")
    member = store.create_user(f"mcp-member-{uuid.uuid4()}@example.com", "pw")
    store.add_project_member(project.id, str(member.id), "contributor")
    outsider = store.create_user(f"mcp-outsider-{uuid.uuid4()}@example.com", "pw")

    env = SimpleNamespace(
        store=store,
        config=SimpleNamespace(data_dir="/tmp/hyqs-test-data", default_repo=""),
        repo_path=repo_path,
        project=project,
        epic=epic,
        member_ctx=AuthContext(user_id=member.id, user_email=member.email, is_platform_admin=False),
        outsider_ctx=AuthContext(
            user_id=outsider.id, user_email=outsider.email, is_platform_admin=False
        ),
        admin_ctx=AuthContext(user_id=None, user_email=None, is_platform_admin=True),
    )
    yield env
    store.delete_project(project.id)
    store.delete_user(member.id)
    store.delete_user(outsider.id)


# --- S1: github.cleanup_branch_for_retry -------------------------------------


async def _cleanup_branch(job, worktree_path="/tmp/wt"):
    from hyqs.pipeline import github

    return await github.cleanup_branch_for_retry(job, worktree_path)


def test_cleanup_branch_for_retry_removes_worktree_and_branch():
    job = _job(id=7, branch="hyqs/job-7")
    with (
        patch("hyqs.pipeline.github.remove_worktree", new_callable=AsyncMock) as mock_remove,
        patch("hyqs.pipeline.github.git", new_callable=AsyncMock) as mock_git,
        patch("hyqs.pipeline.github.has_remote", new_callable=AsyncMock, return_value=True),
    ):
        mock_git.return_value = GitResult(ok=True, stdout="", stderr="", code=0)
        error = asyncio.run(_cleanup_branch(job))

    assert error is None
    mock_remove.assert_awaited_once()
    assert any(c.args[1:] == ("worktree", "prune") for c in mock_git.await_args_list)
    assert any(c.args[1:] == ("branch", "-D", "hyqs/job-7") for c in mock_git.await_args_list)
    assert any(
        c.args[1:] == ("push", "origin", "--delete", "hyqs/job-7") for c in mock_git.await_args_list
    )


def test_cleanup_branch_for_retry_surfaces_real_remote_failure():
    job = _job(id=8, branch="hyqs/job-8")

    def _git_side_effect(repo, *args):
        if args[:3] == ("push", "origin", "--delete"):
            return GitResult(ok=False, stdout="", stderr="permission denied", code=1)
        return GitResult(ok=True, stdout="", stderr="", code=0)

    with (
        patch("hyqs.pipeline.github.remove_worktree", new_callable=AsyncMock),
        patch("hyqs.pipeline.github.git", new_callable=AsyncMock) as mock_git,
        patch("hyqs.pipeline.github.has_remote", new_callable=AsyncMock, return_value=True),
    ):
        mock_git.side_effect = _git_side_effect
        error = asyncio.run(_cleanup_branch(job))

    assert error is not None
    assert "permission denied" in error


def test_cleanup_branch_for_retry_treats_missing_remote_ref_as_success():
    job = _job(id=9, branch="hyqs/job-9")

    def _git_side_effect(repo, *args):
        if args[:3] == ("push", "origin", "--delete"):
            return GitResult(ok=False, stdout="", stderr="remote ref does not exist", code=1)
        return GitResult(ok=True, stdout="", stderr="", code=0)

    with (
        patch("hyqs.pipeline.github.remove_worktree", new_callable=AsyncMock),
        patch("hyqs.pipeline.github.git", new_callable=AsyncMock) as mock_git,
        patch("hyqs.pipeline.github.has_remote", new_callable=AsyncMock, return_value=True),
    ):
        mock_git.side_effect = _git_side_effect
        error = asyncio.run(_cleanup_branch(job))

    assert error is None


def test_cleanup_branch_for_retry_skips_remote_delete_without_remote():
    job = _job(id=10, branch="hyqs/job-10")
    with (
        patch("hyqs.pipeline.github.remove_worktree", new_callable=AsyncMock),
        patch("hyqs.pipeline.github.git", new_callable=AsyncMock) as mock_git,
        patch("hyqs.pipeline.github.has_remote", new_callable=AsyncMock, return_value=False),
    ):
        mock_git.return_value = GitResult(ok=True, stdout="", stderr="", code=0)
        error = asyncio.run(_cleanup_branch(job))

    assert error is None
    assert not any(c.args[1:3] == ("push", "origin") for c in mock_git.await_args_list)


# --- S2: create_job -----------------------------------------------------------


async def _create(env, ctx, **kw):
    kw.setdefault("project_id", env.project.id)
    return await mcp_server._create_job_impl(env.store, ctx, **kw)


def test_create_job_member_succeeds(mcp_env):
    result = asyncio.run(
        _create(mcp_env, mcp_env.member_ctx, idea="add a widget", epic_id=mcp_env.epic.id)
    )

    assert isinstance(result, dict)
    assert result["job"]["idea"] == "add a widget"
    assert result["job"]["project_id"] == mcp_env.project.id
    assert result["auto_epic"]["id"] == mcp_env.epic.id


def test_create_job_platform_admin_succeeds(mcp_env):
    result = asyncio.run(
        _create(mcp_env, mcp_env.admin_ctx, idea="admin idea", epic_id=mcp_env.epic.id)
    )

    assert isinstance(result, dict)
    assert result["job"]["idea"] == "admin idea"


def test_create_job_non_member_forbidden(mcp_env):
    result = asyncio.run(
        _create(mcp_env, mcp_env.outsider_ctx, idea="sneaky idea", epic_id=mcp_env.epic.id)
    )

    assert result == "error: forbidden — you are not a member of this project"


def test_create_job_deduplicates_recent_idea(mcp_env):
    idea = f"dedup idea {uuid.uuid4()}"
    first = asyncio.run(_create(mcp_env, mcp_env.member_ctx, idea=idea, epic_id=mcp_env.epic.id))
    second = asyncio.run(_create(mcp_env, mcp_env.member_ctx, idea=idea, epic_id=mcp_env.epic.id))

    assert second["deduplicated"] is True
    assert second["job"]["id"] == first["job"]["id"]


def test_create_job_missing_idea_returns_error(mcp_env):
    result = asyncio.run(_create(mcp_env, mcp_env.member_ctx, idea="   "))

    assert result == "error: idea is required"


def test_create_job_unknown_project_returns_error(mcp_env):
    result = asyncio.run(
        mcp_server._create_job_impl(
            mcp_env.store, mcp_env.admin_ctx, idea="idea", project_id=999999999
        )
    )

    assert result == "error: project not found"


def test_create_job_auto_assigns_epic_when_none_given(mcp_env):
    # Fresh project + repo path with no epics -> falls back to "General".
    repo_path = f"/tmp/test-mcp-auto-{uuid.uuid4()}"
    project = mcp_env.store.create_project(f"Auto Epic Project {uuid.uuid4()}", repo_path)
    mcp_env.store.add_project_member(project.id, str(mcp_env.member_ctx.user_id), "contributor")
    try:
        result = asyncio.run(
            mcp_server._create_job_impl(
                mcp_env.store,
                mcp_env.member_ctx,
                idea="auto epic idea",
                project_id=project.id,
            )
        )
        assert result["auto_epic"]["auto_assigned"] is True
        assert result["auto_epic"]["name"] == "General"
    finally:
        mcp_env.store.delete_project(project.id)


def test_create_job_fixes_job_id_unknown_returns_error(mcp_env):
    result = asyncio.run(
        _create(mcp_env, mcp_env.member_ctx, idea="fix idea", fixes_job_id=999999999)
    )

    assert result == "error: fixes_job_id not found"


def test_create_job_fixes_job_id_cross_project_returns_error(mcp_env):
    other_repo = f"/tmp/test-mcp-other-{uuid.uuid4()}"
    other_project = mcp_env.store.create_project(f"Other Project {uuid.uuid4()}", other_repo)
    incident = mcp_env.store.create(idea="incident", repo_path=other_repo, chat_id=1)
    try:
        result = asyncio.run(
            _create(mcp_env, mcp_env.member_ctx, idea="fix idea", fixes_job_id=incident.id)
        )
        assert result == "error: fixes_job_id belongs to a different project"
    finally:
        mcp_env.store.delete_project(other_project.id)


def test_create_job_fixes_job_id_rejects_when_remediation_active(mcp_env):
    incident = mcp_env.store.create(
        idea="incident job", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    active_fix = mcp_env.store.create(
        idea="active fix", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    mcp_env.store.set_active_remediation(incident.id, active_fix.id)

    result = asyncio.run(
        _create(
            mcp_env,
            mcp_env.member_ctx,
            idea=f"another fix {uuid.uuid4()}",
            fixes_job_id=incident.id,
        )
    )

    assert isinstance(result, str)
    assert f"job #{incident.id}" in result
    assert f"job #{active_fix.id}" in result


def test_create_job_fixes_job_id_allow_parallel_remediation_bypasses_conflict(mcp_env):
    incident = mcp_env.store.create(
        idea="incident job parallel",
        repo_path=mcp_env.repo_path,
        chat_id=1,
        epic_id=mcp_env.epic.id,
    )
    active_fix = mcp_env.store.create(
        idea="active fix parallel", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    mcp_env.store.set_active_remediation(incident.id, active_fix.id)

    result = asyncio.run(
        _create(
            mcp_env,
            mcp_env.member_ctx,
            idea=f"parallel fix {uuid.uuid4()}",
            fixes_job_id=incident.id,
            allow_parallel_remediation=True,
        )
    )

    assert isinstance(result, dict)
    created = mcp_env.store.get(result["job"]["id"])
    assert created.source_meta.get("fixes_job_id") == incident.id
    assert created.source_meta.get("allow_parallel_remediation") is True
    assert created.source_meta.get("remediation_root_job_id") == incident.id
    assert created.source_meta.get("remediation_depth") == 0


def test_create_job_fixes_job_id_succeeds_when_prior_remediation_terminal(mcp_env):
    incident = mcp_env.store.create(
        idea="incident job 2", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    stale_fix = mcp_env.store.create(
        idea="stale fix", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    _set_status(mcp_env.store, stale_fix.id, JobStatus.DONE)
    mcp_env.store.set_active_remediation(incident.id, stale_fix.id)

    result = asyncio.run(
        _create(
            mcp_env,
            mcp_env.member_ctx,
            idea=f"new fix {uuid.uuid4()}",
            fixes_job_id=incident.id,
        )
    )

    assert isinstance(result, dict)
    assert mcp_env.store.get_active_remediation(incident.id) == result["job"]["id"]


def test_create_job_fixes_job_id_registers_active_remediation(mcp_env):
    incident = mcp_env.store.create(
        idea="incident job 3", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )

    result = asyncio.run(
        _create(
            mcp_env,
            mcp_env.member_ctx,
            idea=f"first fix {uuid.uuid4()}",
            fixes_job_id=incident.id,
        )
    )

    assert isinstance(result, dict)
    assert mcp_env.store.get_active_remediation(incident.id) == result["job"]["id"]
    created = mcp_env.store.get(result["job"]["id"])
    assert created.source_meta.get("fixes_job_id") == incident.id
    assert created.source_meta.get("remediation_root_job_id") == incident.id
    assert created.source_meta.get("remediation_depth") == 0


# --- S3: cancel_job / retry_job ------------------------------------------------


def test_cancel_job_succeeds_for_member(mcp_env):
    job = mcp_env.store.create(
        idea="cancel me", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )

    result = asyncio.run(mcp_server._cancel_job_impl(mcp_env.store, mcp_env.member_ctx, job.id))

    assert result["job"]["status"] == "cancelled"


def test_cancel_job_already_terminal_returns_error(mcp_env):
    job = mcp_env.store.create(
        idea="cancel twice", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    mcp_env.store.cancel(job.id)

    result = asyncio.run(mcp_server._cancel_job_impl(mcp_env.store, mcp_env.member_ctx, job.id))

    assert result == "error: job already in terminal state"


def test_cancel_job_forbidden_for_non_member(mcp_env):
    job = mcp_env.store.create(
        idea="cancel forbidden", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )

    result = asyncio.run(mcp_server._cancel_job_impl(mcp_env.store, mcp_env.outsider_ctx, job.id))

    assert result == "error: forbidden — you are not a member of this project"


def test_retry_job_cleans_up_and_returns_updated_job(mcp_env):
    job = mcp_env.store.create(
        idea="retry me", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    mcp_env.store.cancel(job.id)

    with patch(
        "hyqs.web.mcp_server.github.cleanup_branch_for_retry",
        new_callable=AsyncMock,
        return_value=None,
    ) as mock_cleanup:
        result = asyncio.run(
            mcp_server._retry_job_impl(mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id)
        )

    mock_cleanup.assert_awaited_once()
    assert result["job"]["status"] == "pending"
    assert result["job"]["stage"] == "queued"


def test_retry_job_not_retryable_when_done(mcp_env):
    job = mcp_env.store.create(
        idea="already done", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    _set_status(mcp_env.store, job.id, JobStatus.DONE)

    result = asyncio.run(
        mcp_server._retry_job_impl(mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id)
    )

    assert result == "error: job is not in a retryable state"


def test_retry_job_refuses_needs_split(mcp_env):
    job = mcp_env.store.create(
        idea="too big", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    mcp_env.store.cancel(job.id)
    mcp_env.store.mark_needs_split(job.id)

    result = asyncio.run(
        mcp_server._retry_job_impl(mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id)
    )

    assert result == (
        "error: this job's plan was too large even after a re-ask and has been "
        "parked — a bare retry is refused; refile a new, smaller job, or retry "
        "with force=true to bypass the scope gate"
    )


def test_retry_job_with_force_true_unparks_needs_split(mcp_env):
    job = mcp_env.store.create(
        idea="too big", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    mcp_env.store.cancel(job.id)
    mcp_env.store.mark_needs_split(job.id)

    with patch(
        "hyqs.web.mcp_server.github.cleanup_branch_for_retry",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = asyncio.run(
            mcp_server._retry_job_impl(
                mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id, force=True
            )
        )

    assert isinstance(result, dict)
    assert result["job"]["needs_split"] is False
    assert result["job"]["status"] == JobStatus.PENDING.value


def test_job_to_dict_reports_needs_split_for_parked_job(mcp_env):
    # get_job/list_jobs in mcp_server.py both return job_to_dict(job) as-is, so
    # this covers what MCP clients see without needing a live MCP transport.
    job = mcp_env.store.create(
        idea="too big for one job", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    assert job_to_dict(job)["needs_split"] is False

    mcp_env.store.mark_needs_split(job.id)
    updated = mcp_env.store.get(job.id)

    assert job_to_dict(updated)["needs_split"] is True


def test_list_jobs_filters_deploying_as_active_only(mcp_env, monkeypatch):
    job = mcp_env.store.create(
        idea="deployment awaiting verification",
        repo_path=mcp_env.repo_path,
        chat_id=1,
        epic_id=mcp_env.epic.id,
    )
    _set_status(mcp_env.store, job.id, JobStatus.DEPLOYING)
    request = SimpleNamespace(state=SimpleNamespace(auth_context=mcp_env.member_ctx))
    monkeypatch.setattr(mcp_server, "get_http_request", lambda: request)
    mcp = mcp_server.build_mcp_server(
        mcp_env.store,
        Config(model="sonnet", permission_mode="acceptEdits"),
    )

    async def call(status: str):
        tool = await mcp.get_tool("list_jobs")
        return await tool.fn(project_id=mcp_env.project.id, status=status)

    active = asyncio.run(call("active"))
    done = asyncio.run(call("done"))
    failed = asyncio.run(call("failed"))

    assert {item["id"]: item["status"] for item in active}[job.id] == "deploying"
    assert job.id not in {item["id"] for item in done}
    assert job.id not in {item["id"] for item in failed}


def test_retry_job_forbidden_for_non_member(mcp_env):
    job = mcp_env.store.create(
        idea="retry forbidden", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    mcp_env.store.cancel(job.id)

    result = asyncio.run(
        mcp_server._retry_job_impl(mcp_env.store, mcp_env.config, mcp_env.outsider_ctx, job.id)
    )

    assert result == "error: forbidden — you are not a member of this project"


def test_retry_job_surfaces_cleanup_failure(mcp_env):
    job = mcp_env.store.create(
        idea="retry cleanup fails", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    mcp_env.store.cancel(job.id)

    with patch(
        "hyqs.web.mcp_server.github.cleanup_branch_for_retry",
        new_callable=AsyncMock,
        return_value="could not delete remote branch hyqs/job-X: boom",
    ):
        result = asyncio.run(
            mcp_server._retry_job_impl(mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id)
        )

    assert result == "error: could not delete remote branch hyqs/job-X: boom"
    # DB row must be untouched — still cancelled, not reset to pending.
    assert mcp_env.store.get(job.id).status == JobStatus.CANCELLED


def test_retry_job_without_force_does_not_set_bypass(mcp_env):
    job = mcp_env.store.create(
        idea="retry no force", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    mcp_env.store.cancel(job.id)

    with patch(
        "hyqs.web.mcp_server.github.cleanup_branch_for_retry",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = asyncio.run(
            mcp_server._retry_job_impl(mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id)
        )

    assert result["job"]["status"] == "pending"
    assert not (mcp_env.store.get(job.id).source_meta or {}).get("scope_gate_bypass")


def test_retry_job_with_force_sets_scope_gate_bypass(mcp_env):
    job = mcp_env.store.create(
        idea="retry with force", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    mcp_env.store.cancel(job.id)

    with patch(
        "hyqs.web.mcp_server.github.cleanup_branch_for_retry",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = asyncio.run(
            mcp_server._retry_job_impl(
                mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id, force=True
            )
        )

    assert result["job"]["status"] == "pending"
    updated = mcp_env.store.get(job.id)
    assert updated.source_meta.get("scope_gate_bypass") is True


def test_retry_job_rejects_when_remediation_active(mcp_env):
    job = mcp_env.store.create(
        idea="retry blocked", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    mcp_env.store.cancel(job.id)
    active_fix = mcp_env.store.create(
        idea="active fix for retry",
        repo_path=mcp_env.repo_path,
        chat_id=1,
        epic_id=mcp_env.epic.id,
    )
    mcp_env.store.set_active_remediation(job.id, active_fix.id)

    with patch(
        "hyqs.web.mcp_server.github.cleanup_branch_for_retry", new_callable=AsyncMock
    ) as mock_cleanup:
        result = asyncio.run(
            mcp_server._retry_job_impl(mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id)
        )

    assert isinstance(result, str)
    assert f"job #{job.id}" in result
    assert f"job #{active_fix.id}" in result
    mock_cleanup.assert_not_awaited()
    assert mcp_env.store.get(job.id).status == JobStatus.CANCELLED


def test_retry_job_succeeds_when_remediation_terminal(mcp_env):
    job = mcp_env.store.create(
        idea="retry allowed", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    mcp_env.store.cancel(job.id)
    stale_fix = mcp_env.store.create(
        idea="stale fix for retry",
        repo_path=mcp_env.repo_path,
        chat_id=1,
        epic_id=mcp_env.epic.id,
    )
    _set_status(mcp_env.store, stale_fix.id, JobStatus.DONE)
    mcp_env.store.set_active_remediation(job.id, stale_fix.id)

    with patch(
        "hyqs.web.mcp_server.github.cleanup_branch_for_retry",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = asyncio.run(
            mcp_server._retry_job_impl(mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id)
        )

    assert result["job"]["status"] == "pending"


# --- S4: archive_job ------------------------------------------------------------


def test_archive_job_non_terminal_skips_cleanup(mcp_env):
    job = mcp_env.store.create(
        idea="archive pending", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )

    with patch(
        "hyqs.web.mcp_server.github.cleanup_job_artifacts", new_callable=AsyncMock
    ) as mock_cleanup:
        result = asyncio.run(
            mcp_server._archive_job_impl(mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id)
        )

    assert result["job"]["archived"] is True
    mock_cleanup.assert_not_awaited()


def test_archive_job_terminal_triggers_cleanup(mcp_env):
    job = mcp_env.store.create(
        idea="archive failed", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    _set_status(mcp_env.store, job.id, JobStatus.FAILED)

    with patch(
        "hyqs.web.mcp_server.github.cleanup_job_artifacts", new_callable=AsyncMock, return_value=[]
    ) as mock_cleanup:
        result = asyncio.run(
            mcp_server._archive_job_impl(mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id)
        )

    assert result["job"]["archived"] is True
    mock_cleanup.assert_awaited_once()


def test_archive_job_unknown_id_returns_error(mcp_env):
    result = asyncio.run(
        mcp_server._archive_job_impl(mcp_env.store, mcp_env.config, mcp_env.member_ctx, 9_999_999)
    )

    assert result == "error: not found"


def test_archive_job_forbidden_for_non_member(mcp_env):
    job = mcp_env.store.create(
        idea="archive forbidden", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )

    result = asyncio.run(
        mcp_server._archive_job_impl(mcp_env.store, mcp_env.config, mcp_env.outsider_ctx, job.id)
    )

    assert result == "error: forbidden — you are not a member of this project"


# --- S1/S2: unarchive_job --------------------------------------------------------


def test_unarchive_job_round_trips(mcp_env):
    job = mcp_env.store.create(
        idea="unarchive me", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    asyncio.run(
        mcp_server._archive_job_impl(mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id)
    )

    result = asyncio.run(mcp_server._unarchive_job_impl(mcp_env.store, mcp_env.member_ctx, job.id))

    assert result["job"]["archived"] is False
    assert mcp_env.store.get(job.id).archived is False


def test_unarchive_job_unknown_id_returns_error(mcp_env):
    result = asyncio.run(
        mcp_server._unarchive_job_impl(mcp_env.store, mcp_env.member_ctx, 9_999_999)
    )

    assert result == "error: not found"


def test_unarchive_job_forbidden_for_non_member(mcp_env):
    job = mcp_env.store.create(
        idea="unarchive forbidden", repo_path=mcp_env.repo_path, chat_id=1, epic_id=mcp_env.epic.id
    )
    asyncio.run(
        mcp_server._archive_job_impl(mcp_env.store, mcp_env.config, mcp_env.member_ctx, job.id)
    )

    result = asyncio.run(
        mcp_server._unarchive_job_impl(mcp_env.store, mcp_env.outsider_ctx, job.id)
    )

    assert result == "error: forbidden — you are not a member of this project"


# --- S5: promote_release ---------------------------------------------------------


def _project_admin_ctx(mcp_env):
    admin = mcp_env.store.create_user(f"mcp-promoter-{uuid.uuid4()}@example.com", "pw")
    mcp_env.store.add_project_member(mcp_env.project.id, str(admin.id), "project_admin")
    return AuthContext(user_id=admin.id, user_email=admin.email, is_platform_admin=False)


def _release_and_env(mcp_env):
    environment = mcp_env.store.create_environment(mcp_env.project.id, "staging", "staging")
    release = mcp_env.store.create_release(
        mcp_env.project.id, "abc123", "registry/app:abc123", "sha256:deadbeef"
    )
    return release, environment


async def _promote(env, ctx, **kw):
    kw.setdefault("project_id", env.project.id)
    return await mcp_server._promote_release_impl(env.store, ctx, **kw)


def test_promote_release_project_admin_succeeds(mcp_env):
    admin_ctx = _project_admin_ctx(mcp_env)
    release, environment = _release_and_env(mcp_env)

    result = asyncio.run(
        _promote(mcp_env, admin_ctx, release_id=release.id, target_env_id=environment.id)
    )

    assert result["promotion"]["state"] == "dispatched"
    assert isinstance(result["job"], dict)
    assert result["job"]["stage"] == "deploy"


def test_promote_release_contributor_forbidden(mcp_env):
    release, environment = _release_and_env(mcp_env)

    result = asyncio.run(
        _promote(mcp_env, mcp_env.member_ctx, release_id=release.id, target_env_id=environment.id)
    )

    assert result == "error: forbidden — you are not a member of this project"


def test_promote_release_noop_when_already_current(mcp_env):
    admin_ctx = _project_admin_ctx(mcp_env)
    release, environment = _release_and_env(mcp_env)
    mcp_env.store.set_environment_current_release(environment.id, release.id)

    result = asyncio.run(
        _promote(mcp_env, admin_ctx, release_id=release.id, target_env_id=environment.id)
    )

    assert result == {"promotion": None, "noop": True}


def test_promote_release_unknown_release_returns_error(mcp_env):
    admin_ctx = _project_admin_ctx(mcp_env)
    _, environment = _release_and_env(mcp_env)

    result = asyncio.run(
        _promote(mcp_env, admin_ctx, release_id=9_999_999, target_env_id=environment.id)
    )

    assert result == "error: release not found"


def test_promote_release_unknown_environment_returns_error(mcp_env):
    admin_ctx = _project_admin_ctx(mcp_env)
    release, _ = _release_and_env(mcp_env)

    result = asyncio.run(
        _promote(mcp_env, admin_ctx, release_id=release.id, target_env_id=9_999_999)
    )

    assert result == "error: environment not found"


# --- offer_release -----------------------------------------------------------


async def _offer(env, ctx, **kw):
    kw.setdefault("project_id", env.project.id)
    return await mcp_server._offer_release_impl(env.store, ctx, **kw)


def test_offer_release_project_admin_succeeds(mcp_env):
    admin_ctx = _project_admin_ctx(mcp_env)
    release, environment = _release_and_env(mcp_env)

    result = asyncio.run(
        _offer(mcp_env, admin_ctx, release_id=release.id, target_env_id=environment.id)
    )

    assert result["promotion"]["kind"] == "offer"
    assert result["promotion"]["state"] == "available"
    assert result["promotion"]["deploy_job_id"] is None
    assert "job" not in result


def test_offer_release_platform_admin_succeeds(mcp_env):
    release, environment = _release_and_env(mcp_env)

    result = asyncio.run(
        _offer(mcp_env, mcp_env.admin_ctx, release_id=release.id, target_env_id=environment.id)
    )

    assert result["promotion"]["kind"] == "offer"
    assert result["promotion"]["state"] == "available"


def test_offer_release_contributor_forbidden(mcp_env):
    release, environment = _release_and_env(mcp_env)

    result = asyncio.run(
        _offer(mcp_env, mcp_env.member_ctx, release_id=release.id, target_env_id=environment.id)
    )

    assert result == "error: forbidden — you are not a member of this project"


def test_offer_release_noop_when_already_current(mcp_env):
    admin_ctx = _project_admin_ctx(mcp_env)
    release, environment = _release_and_env(mcp_env)
    mcp_env.store.set_environment_current_release(environment.id, release.id)

    result = asyncio.run(
        _offer(mcp_env, admin_ctx, release_id=release.id, target_env_id=environment.id)
    )

    assert result == {"promotion": None, "noop": True}


def test_offer_release_unknown_release_returns_error(mcp_env):
    admin_ctx = _project_admin_ctx(mcp_env)
    _, environment = _release_and_env(mcp_env)

    result = asyncio.run(
        _offer(mcp_env, admin_ctx, release_id=9_999_999, target_env_id=environment.id)
    )

    assert result == "error: release not found"


def test_offer_release_unknown_environment_returns_error(mcp_env):
    admin_ctx = _project_admin_ctx(mcp_env)
    release, _ = _release_and_env(mcp_env)

    result = asyncio.run(_offer(mcp_env, admin_ctx, release_id=release.id, target_env_id=9_999_999))

    assert result == "error: environment not found"


def test_offer_release_environment_from_other_project_returns_error(mcp_env):
    admin_ctx = _project_admin_ctx(mcp_env)
    release, _environment = _release_and_env(mcp_env)
    other_project = mcp_env.store.create_project(
        f"Other Offer Project {uuid.uuid4()}", f"/tmp/test-mcp-{uuid.uuid4()}"
    )
    other_env = mcp_env.store.get_or_create_default_environment(other_project.id)

    result = asyncio.run(
        _offer(mcp_env, admin_ctx, release_id=release.id, target_env_id=other_env.id)
    )

    assert result == "error: environment not found"


# --- S3: promote_release prod-gate on deploy.promote_prod --------------------


def _role_ctx(mcp_env, role: str):
    user = mcp_env.store.create_user(f"mcp-{role}-{uuid.uuid4()}@example.com", "pw")
    mcp_env.store.add_project_member(mcp_env.project.id, str(user.id), role)
    return AuthContext(user_id=user.id, user_email=user.email, is_platform_admin=False)


def test_promote_release_manager_succeeds_for_staging(mcp_env):
    ctx = _role_ctx(mcp_env, "release_manager")
    release, environment = _release_and_env(mcp_env)

    result = asyncio.run(
        _promote(mcp_env, ctx, release_id=release.id, target_env_id=environment.id)
    )

    assert result["promotion"]["state"] == "dispatched"


def test_promote_release_manager_forbidden_for_prod(mcp_env):
    ctx = _role_ctx(mcp_env, "release_manager")
    release, _staging = _release_and_env(mcp_env)
    prod_env = mcp_env.store.create_environment(mcp_env.project.id, "prod", "prod")

    result = asyncio.run(_promote(mcp_env, ctx, release_id=release.id, target_env_id=prod_env.id))

    assert result == (
        "error: forbidden — promoting to a prod environment requires deploy.promote_prod"
    )


def test_promote_prod_promoter_succeeds_for_prod(mcp_env):
    ctx = _role_ctx(mcp_env, "prod_promoter")
    release, _staging = _release_and_env(mcp_env)
    prod_env = mcp_env.store.create_environment(mcp_env.project.id, "prod", "prod")

    result = asyncio.run(_promote(mcp_env, ctx, release_id=release.id, target_env_id=prod_env.id))

    assert result["promotion"]["state"] == "dispatched"


def test_promote_viewer_forbidden(mcp_env):
    ctx = _role_ctx(mcp_env, "viewer")
    release, environment = _release_and_env(mcp_env)

    result = asyncio.run(
        _promote(mcp_env, ctx, release_id=release.id, target_env_id=environment.id)
    )

    assert result == "error: forbidden — you are not a member of this project"


def test_offer_viewer_forbidden(mcp_env):
    ctx = _role_ctx(mcp_env, "viewer")
    release, environment = _release_and_env(mcp_env)

    result = asyncio.run(_offer(mcp_env, ctx, release_id=release.id, target_env_id=environment.id))

    assert result == "error: forbidden — you are not a member of this project"


# --- _job_diff_payload: branch deleted at merge falls back to landed commit --


def test_job_diff_payload_returns_branch_diff_when_branch_resolves():
    job = _job(branch="job-1-branch", repo_path="/tmp/repo", deployed_commit="deadbeef")
    branch_diff = GitResult(ok=True, stdout="diff --git a/x b/x\n", stderr="", code=0)

    with (
        patch("hyqs.web.app.gitops.default_branch", new_callable=AsyncMock, return_value="main"),
        patch(
            "hyqs.web.app.gitops.git", new_callable=AsyncMock, return_value=branch_diff
        ) as mock_git,
    ):
        result = asyncio.run(_job_diff_payload(job))

    assert result == {
        "diff": "diff --git a/x b/x\n",
        "base": "main",
        "branch": "job-1-branch",
        "commit": None,
    }
    mock_git.assert_awaited_once_with("/tmp/repo", "diff", "main...job-1-branch")


def test_job_diff_payload_falls_back_to_deployed_commit_when_branch_gone():
    job = _job(branch="job-1-branch", repo_path="/tmp/repo", deployed_commit="abc123")
    branch_diff_failed = GitResult(ok=False, stdout="", stderr="fatal: ambiguous argument", code=1)
    commit_diff = GitResult(ok=True, stdout="diff --git a/y b/y\n", stderr="", code=0)

    with (
        patch("hyqs.web.app.gitops.default_branch", new_callable=AsyncMock, return_value="main"),
        patch(
            "hyqs.web.app.gitops.git",
            new_callable=AsyncMock,
            side_effect=[branch_diff_failed, commit_diff],
        ) as mock_git,
    ):
        result = asyncio.run(_job_diff_payload(job))

    assert result == {
        "diff": "diff --git a/y b/y\n",
        "base": "main",
        "branch": None,
        "commit": "abc123",
    }
    assert mock_git.await_args_list[0].args == ("/tmp/repo", "diff", "main...job-1-branch")
    assert mock_git.await_args_list[1].args == ("/tmp/repo", "diff", "abc123^..abc123")


def test_job_diff_payload_returns_structured_error_when_branch_and_commit_both_unavailable():
    job = _job(branch="job-1-branch", repo_path="/tmp/repo", deployed_commit=None)
    branch_diff_failed = GitResult(ok=False, stdout="", stderr="fatal: ambiguous argument", code=1)

    with (
        patch("hyqs.web.app.gitops.default_branch", new_callable=AsyncMock, return_value="main"),
        patch("hyqs.web.app.gitops.git", new_callable=AsyncMock, return_value=branch_diff_failed),
    ):
        result = asyncio.run(_job_diff_payload(job))

    assert "diff" not in result
    assert "error" in result
    assert "job-1-branch" in result["error"]
    assert "no landed commit is recorded" in result["error"]


def test_job_diff_payload_returns_structured_error_when_commit_diff_also_fails():
    job = _job(branch="job-1-branch", repo_path="/tmp/repo", deployed_commit="abc123")
    branch_diff_failed = GitResult(ok=False, stdout="", stderr="fatal: ambiguous argument", code=1)
    commit_diff_failed = GitResult(ok=False, stdout="", stderr="fatal: bad object abc123", code=1)

    with (
        patch("hyqs.web.app.gitops.default_branch", new_callable=AsyncMock, return_value="main"),
        patch(
            "hyqs.web.app.gitops.git",
            new_callable=AsyncMock,
            side_effect=[branch_diff_failed, commit_diff_failed],
        ),
    ):
        result = asyncio.run(_job_diff_payload(job))

    assert "diff" not in result
    assert "error" in result
    assert result["error"] != "fatal: bad object abc123"
    assert "fatal: bad object abc123" in result["error"]


# --- get_job_dependency_graph --------------------------------------------------


def _make_job(env, idea: str) -> Job:
    return env.store.create(idea=idea, repo_path=env.repo_path, chat_id=1, epic_id=env.epic.id)


def test_get_job_dependency_graph_reports_upstream_and_downstream(mcp_env):
    blocker = _make_job(mcp_env, "blocker job")
    job = _make_job(mcp_env, "middle job")
    dependent = _make_job(mcp_env, "dependent job")
    mcp_env.store.add_job_dependency(job.id, blocker.id)
    mcp_env.store.add_job_dependency(dependent.id, job.id)

    result = asyncio.run(
        mcp_server._get_job_dependency_graph_impl(mcp_env.store, mcp_env.member_ctx, job.id)
    )

    assert result["depends_on_jobs"] == [
        {"id": blocker.id, "title": blocker.title, "status": blocker.status.value}
    ]
    assert result["dependents"] == [
        {"id": dependent.id, "title": dependent.title, "status": dependent.status.value}
    ]


def test_get_job_dependency_graph_diamond_multi_parent(mcp_env):
    upstream_a = _make_job(mcp_env, "upstream a")
    upstream_b = _make_job(mcp_env, "upstream b")
    job = _make_job(mcp_env, "diamond middle")
    downstream_a = _make_job(mcp_env, "downstream a")
    downstream_b = _make_job(mcp_env, "downstream b")
    mcp_env.store.add_job_dependency(job.id, upstream_a.id)
    mcp_env.store.add_job_dependency(job.id, upstream_b.id)
    mcp_env.store.add_job_dependency(downstream_a.id, job.id)
    mcp_env.store.add_job_dependency(downstream_b.id, job.id)

    result = asyncio.run(
        mcp_server._get_job_dependency_graph_impl(mcp_env.store, mcp_env.member_ctx, job.id)
    )

    assert {d["id"] for d in result["depends_on_jobs"]} == {upstream_a.id, upstream_b.id}
    assert {d["id"] for d in result["dependents"]} == {downstream_a.id, downstream_b.id}


def test_get_job_dependency_graph_includes_done_upstream_unlike_unsatisfied_deps(mcp_env):
    blocker = _make_job(mcp_env, "done blocker")
    job = _make_job(mcp_env, "waits on done blocker")
    mcp_env.store.add_job_dependency(job.id, blocker.id)
    _set_status(mcp_env.store, blocker.id, JobStatus.DONE)

    assert mcp_env.store.get_unsatisfied_deps(job.id) == []

    result = asyncio.run(
        mcp_server._get_job_dependency_graph_impl(mcp_env.store, mcp_env.member_ctx, job.id)
    )
    assert result["depends_on_jobs"] == [
        {"id": blocker.id, "title": blocker.title, "status": JobStatus.DONE.value}
    ]


def test_get_job_dependency_graph_no_edges_returns_empty_lists(mcp_env):
    job = _make_job(mcp_env, "lonely job")

    result = asyncio.run(
        mcp_server._get_job_dependency_graph_impl(mcp_env.store, mcp_env.member_ctx, job.id)
    )

    assert result == {"depends_on_jobs": [], "dependents": []}


def test_get_job_dependency_graph_unknown_job_returns_error(mcp_env):
    result = asyncio.run(
        mcp_server._get_job_dependency_graph_impl(mcp_env.store, mcp_env.member_ctx, 9_999_999)
    )
    assert result == "error: job not found"


def test_get_job_dependency_graph_forbidden_for_non_member(mcp_env):
    job = _make_job(mcp_env, "graph forbidden")

    result = asyncio.run(
        mcp_server._get_job_dependency_graph_impl(mcp_env.store, mcp_env.outsider_ctx, job.id)
    )

    assert result == "error: forbidden — you are not a member of this project"
