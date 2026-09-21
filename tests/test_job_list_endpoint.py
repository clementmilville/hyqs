"""Regression tests for the filtered GET /api/jobs collection."""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

from starlette.requests import Request
from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.pipeline.models import (
    Job,
    JobSource,
    JobStatus,
    SchedulerWait,
    SchedulerWaitReason,
    Stage,
    User,
)
from hyqs.web.app import build_app

_TOKEN = "test-web-token"


def _make_job(job_id: int, **overrides) -> Job:
    defaults = {
        "id": job_id,
        "idea": f"job {job_id}",
        "repo_path": "/tmp/repo",
        "chat_id": 1,
        "status": JobStatus.RUNNING,
        "stage": Stage.PLAN,
        "project_id": 41,
    }
    defaults.update(overrides)
    return Job(**defaults)


def test_list_jobs_serializes_executor_matrix_and_preserves_legacy_fields():
    jobs = [
        _make_job(1, agent_id=3, provider="claude"),
        _make_job(2, stage=Stage.LINT),
        _make_job(3, status=JobStatus.DEPLOYING, stage=Stage.DEPLOY),
        _make_job(4, status=JobStatus.PENDING, stage=Stage.PLAN),
        _make_job(
            5,
            status=JobStatus.DONE,
            stage=Stage.DEPLOY,
            agent_id=8,
            provider="codex",
            source=JobSource.MCP,
            source_actor="automation@example.com",
        ),
    ]
    store = MagicMock()
    store.list_active.return_value = jobs
    store.get_unsatisfied_deps.side_effect = lambda job_id: [99] if job_id == 4 else []
    store.get_scheduler_wait.return_value = None
    store.get_effective_priority.side_effect = lambda job: (job.priority, [])
    app = build_app(
        Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN),
        MagicMock(),
        store,
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})

    response = client.get("/api/jobs?status=all")

    assert response.status_code == 200
    assert set(response.json()) == {"jobs"}
    items = {item["id"]: item for item in response.json()["jobs"]}
    assert items[1]["current_executor"] == {
        "kind": "agent",
        "label": "claude",
        "agent_id": 3,
        "provider": "claude",
    }
    assert items[2]["current_executor"]["kind"] == "pipeline"
    assert items[3]["current_executor"]["kind"] == "pipeline"
    assert items[4]["current_executor"] == {
        "kind": "waiting",
        "label": "Waiting assignment",
        "agent_id": None,
        "provider": "",
    }
    assert items[4]["waiting_on"] == [99]
    assert items[5]["current_executor"] is None
    assert items[5]["agent_id"] == 8
    assert items[5]["provider"] == "codex"
    assert items[5]["source"] == "mcp"
    assert items[5]["source_actor"] == "automation@example.com"
    store.list_active.assert_called_once_with(50, status="all")
    assert items[1]["scheduler_wait"] is None


def test_list_jobs_includes_scheduler_wait_when_a_job_is_blocked():
    jobs = [
        _make_job(1, status=JobStatus.PENDING),
        _make_job(2, status=JobStatus.PENDING),
    ]
    wait = SchedulerWait(
        reason=SchedulerWaitReason.MERGE_LOCK,
        summary="waiting on the merge lock",
        blocking_job_ids=[9],
        conflicting_paths=[],
    )
    store = MagicMock()
    store.list_active.return_value = jobs
    store.get_unsatisfied_deps.return_value = []
    store.get_scheduler_wait.side_effect = lambda job, waiting_on: wait if job.id == 1 else None
    store.get_effective_priority.side_effect = lambda job: (job.priority, [])
    app = build_app(
        Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN),
        MagicMock(),
        store,
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})

    response = client.get("/api/jobs?status=all")

    items = {item["id"]: item for item in response.json()["jobs"]}
    assert items[1]["waiting_on"] == []
    assert items[1]["scheduler_wait"] == {
        "reason": "merge_lock",
        "summary": "waiting on the merge lock",
        "blocking_job_ids": [9],
        "conflicting_paths": [],
    }
    assert items[2]["waiting_on"] == []
    assert items[2]["scheduler_wait"] is None


def test_list_jobs_unscoped_filters_failure_details_to_member_projects():
    member_job = _make_job(
        1,
        project_id=41,
        failure_code="test_failure",
        failure_detail={"output": "member diagnostic"},
    )
    other_job = _make_job(
        2,
        project_id=42,
        failure_code="security_failure",
        failure_detail={"output": "other project secret"},
    )
    store = MagicMock()
    store.get_session.return_value = User(id=7, email="member@example.com", is_platform_admin=False)
    store.list_active.return_value = [member_job, other_job]
    member = MagicMock(user_id="7")
    store.list_project_members.side_effect = lambda project_id: [member] if project_id == 41 else []
    store.get_unsatisfied_deps.return_value = []
    store.get_scheduler_wait.return_value = None
    store.get_effective_priority.side_effect = lambda job: (job.priority, [])
    app = build_app(
        Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN),
        MagicMock(),
        store,
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})

    response = client.get("/api/jobs?status=all")

    assert response.status_code == 200
    jobs = response.json()["jobs"]
    assert [job["id"] for job in jobs] == [1]
    assert jobs[0]["failure_code"] == "test_failure"
    assert jobs[0]["failure_detail"] == {"output": "member diagnostic"}


def test_jobs_stream_filters_projects_and_rechecks_membership(monkeypatch):
    member_job = _make_job(1, project_id=41)
    other_job = _make_job(2, project_id=42)
    store = MagicMock()
    store.list_active.return_value = [member_job, other_job]
    member = MagicMock(user_id="7")
    membership_checks = {41: 0, 42: 0}

    def list_project_members(project_id):
        membership_checks[project_id] += 1
        if project_id == 41 and membership_checks[project_id] <= 2:
            return [member]
        return []

    store.list_project_members.side_effect = list_project_members
    store.get_unsatisfied_deps.return_value = []
    store.get_scheduler_wait.return_value = None
    store.get_effective_priority.side_effect = lambda job: (job.priority, [])
    app = build_app(
        Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN),
        MagicMock(),
        store,
    )
    route = next(
        route for route in app.app.routes if getattr(route, "path", None) == "/api/jobs/stream"
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/jobs/stream",
            "headers": [],
            "state": {
                "user_id": 7,
                "user_email": "member@example.com",
                "is_platform_admin": False,
                "token_project_id": None,
                "token_role": None,
                "api_token_id": None,
            },
        }
    )
    request.is_disconnected = AsyncMock(side_effect=[False, False, False, True])
    monkeypatch.setattr("hyqs.web.app.asyncio.sleep", AsyncMock())

    async def collect_events():
        response = await route.endpoint(request)
        return [event async for event in response.body_iterator]

    emitted = asyncio.run(collect_events())

    assert [event["event"] for event in emitted] == ["jobs", "jobs"]
    assert [[job["id"] for job in json.loads(event["data"])] for event in emitted] == [
        [1],
        [],
    ]
    assert store.list_active.call_count == 3
    assert membership_checks == {41: 3, 42: 3}


def test_list_jobs_filters_deploying_as_active_only(store):
    project = store.create_project(
        f"Job List Project {uuid.uuid4()}",
        f"/tmp/test-job-list-{uuid.uuid4()}",
    )
    job = store.create(
        idea="deployment awaiting verification",
        repo_path=project.repo_path,
        chat_id=1,
    )
    job.status = JobStatus.DEPLOYING
    job.stage = Stage.DEPLOY
    store.save(job)
    app = build_app(
        Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN),
        MagicMock(),
        store,
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})

    active = client.get(f"/api/jobs?project_id={project.id}&status=active")
    done = client.get(f"/api/jobs?project_id={project.id}&status=done")
    failed = client.get(f"/api/jobs?project_id={project.id}&status=failed")

    assert active.status_code == 200
    assert {item["id"]: item["status"] for item in active.json()["jobs"]}[job.id] == "deploying"
    assert job.id not in {item["id"] for item in done.json()["jobs"]}
    assert job.id not in {item["id"] for item in failed.json()["jobs"]}

    store.delete_project(project.id)


def test_list_jobs_preserves_filter_vocabulary(store):
    app = build_app(
        Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN),
        MagicMock(),
        store,
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})

    for status in ("active", "done", "failed", "archived", "all"):
        assert client.get(f"/api/jobs?status={status}").status_code == 200

    response = client.get("/api/jobs?status=deploying")
    assert response.status_code == 400
    assert response.json() == {
        "error": "status must be one of: active, done, failed, archived, all"
    }
