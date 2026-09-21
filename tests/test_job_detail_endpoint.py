"""Tests for GET /api/jobs/{job_id} (single-job detail endpoint).

Mocked-store tests build the real Starlette app (hyqs.web.app.build_app) with a
MagicMock store so lookup/permission gating can be checked with
starlette.testclient.TestClient, following tests/test_job_resolution_endpoints.py.
The round-trip test uses the real Postgres ``store`` fixture per CONVENTIONS.md §7.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.pipeline import resolve_current_executor as exported_resolve_current_executor
from hyqs.pipeline.agents import resolve_current_executor
from hyqs.pipeline.models import (
    Job,
    JobSource,
    JobStatus,
    SchedulerWait,
    SchedulerWaitReason,
    Stage,
    User,
)
from hyqs.web import job_to_dict as exported_job_to_dict
from hyqs.web.app import build_app, job_projection_extras, job_to_dict, job_to_summary_dict

_TOKEN = "test-web-token"


def _make_job(**overrides) -> Job:
    defaults = dict(
        id=692,
        idea="original idea",
        repo_path="/tmp/repo",
        chat_id=1,
        status=JobStatus.RUNNING,
        stage=Stage.PLAN,
        project_id=1,
        epic_id=7,
    )
    defaults.update(overrides)
    return Job(**defaults)


def _make_client(store, *, permissions: list[str] | None = None) -> TestClient:
    store.list_users.return_value = ["existing-admin"]
    store.get_role_permissions.return_value = {"platform_admin": permissions or []}
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})
    return client


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {
                "status": JobStatus.RUNNING,
                "stage": Stage.PLAN,
                "agent_id": 3,
                "provider": "claude",
            },
            {"kind": "agent", "label": "claude", "agent_id": 3, "provider": "claude"},
        ),
        (
            {"status": JobStatus.RUNNING, "stage": Stage.LINT},
            {"kind": "pipeline", "label": "Pipeline", "agent_id": None, "provider": ""},
        ),
        (
            {"status": JobStatus.DEPLOYING, "stage": Stage.DEPLOY},
            {"kind": "pipeline", "label": "Pipeline", "agent_id": None, "provider": ""},
        ),
        (
            {"status": JobStatus.PENDING, "stage": Stage.PLAN},
            {"kind": "waiting", "label": "Waiting assignment", "agent_id": None, "provider": ""},
        ),
        (
            {"status": JobStatus.RUNNING, "stage": Stage.PLAN},
            {"kind": "waiting", "label": "Waiting assignment", "agent_id": None, "provider": ""},
        ),
        (
            {"status": JobStatus.PENDING, "stage": Stage.TEST, "agent_id": 3, "provider": "claude"},
            {"kind": "waiting", "label": "Waiting assignment", "agent_id": None, "provider": ""},
        ),
    ],
)
def test_job_to_dict_serializes_current_executor(overrides, expected):
    assert job_to_dict(_make_job(**overrides))["current_executor"] == expected


@pytest.mark.parametrize("status", [JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED])
def test_job_to_dict_clears_stale_terminal_executor(status):
    serialized = job_to_dict(
        _make_job(status=status, agent_id=3, provider="claude"), waiting_on=[17]
    )

    assert serialized["current_executor"] is None
    assert serialized["agent_id"] == 3
    assert serialized["provider"] == "claude"
    assert serialized["waiting_on"] == [17]


def test_job_to_dict_preserves_identity_source_and_web_export():
    job = _make_job(
        agent_id=3,
        provider="claude",
        source=JobSource.MCP,
        source_actor="automation@example.com",
        source_meta={"request_id": "abc"},
    )

    assert exported_job_to_dict is job_to_dict
    serialized = exported_job_to_dict(job, waiting_on=[17])
    assert serialized["agent_id"] == 3
    assert serialized["provider"] == "claude"
    assert serialized["source"] == "mcp"
    assert serialized["source_actor"] == "automation@example.com"
    assert serialized["source_meta"] == {"request_id": "abc"}
    assert serialized["waiting_on"] == [17]


def test_job_to_dict_serializes_scheduler_wait_when_present():
    wait = SchedulerWait(
        reason=SchedulerWaitReason.FILE_OVERLAP,
        summary="waiting on file overlap with job #4",
        blocking_job_ids=[4],
        conflicting_paths=["hyqs/web/app.py"],
    )
    serialized = job_to_dict(_make_job(), scheduler_wait=wait)

    assert serialized["scheduler_wait"] == {
        "reason": "file_overlap",
        "summary": "waiting on file overlap with job #4",
        "blocking_job_ids": [4],
        "conflicting_paths": ["hyqs/web/app.py"],
    }


def test_job_to_dict_scheduler_wait_defaults_to_none():
    assert job_to_dict(_make_job())["scheduler_wait"] is None


def test_job_to_dict_effective_priority_defaults_to_base_priority():
    job = _make_job(priority=5)
    serialized = job_to_dict(job)
    assert serialized["priority"] == 5
    assert serialized["effective_priority"] == 5
    assert serialized["priority_reasons"] == []


def test_job_to_dict_serializes_effective_priority_when_provided():
    reasons = [{"reason": "remediation", "amount": 3, "detail": "remediation boost"}]
    serialized = job_to_dict(_make_job(priority=5), effective_priority=8, priority_reasons=reasons)
    assert serialized["priority"] == 5
    assert serialized["effective_priority"] == 8
    assert serialized["priority_reasons"] == reasons


def test_job_to_summary_dict_effective_priority_defaults_to_base_priority():
    job = _make_job(priority=5)
    serialized = job_to_summary_dict(job)
    assert serialized["priority"] == 5
    assert serialized["effective_priority"] == 5
    assert serialized["priority_reasons"] == []


def test_job_to_summary_dict_serializes_effective_priority_when_provided():
    reasons = [{"reason": "aging", "amount": 1, "detail": "aging boost"}]
    serialized = job_to_summary_dict(
        _make_job(priority=5), effective_priority=6, priority_reasons=reasons
    )
    assert serialized["priority"] == 5
    assert serialized["effective_priority"] == 6
    assert serialized["priority_reasons"] == reasons


def test_job_projection_extras_includes_effective_priority(mock_store):
    job = _make_job(priority=5)
    reasons = [{"reason": "critical_path", "amount": 2, "detail": "critical path boost"}]
    mock_store.get_effective_priority.side_effect = None
    mock_store.get_effective_priority.return_value = (7, reasons)

    extras = job_projection_extras(mock_store, job)

    mock_store.get_effective_priority.assert_called_once_with(job)
    assert extras["effective_priority"] == 7
    assert extras["priority_reasons"] == reasons


def test_pipeline_exports_shared_current_executor_resolver():
    assert exported_resolve_current_executor is resolve_current_executor


@pytest.fixture
def mock_store() -> MagicMock:
    s = MagicMock()
    s.get_unsatisfied_deps.return_value = []
    s.get_scheduler_wait.return_value = None
    s.get_session.return_value = None
    s.get_effective_priority.side_effect = lambda job: (job.priority, [])
    return s


def test_get_job_returns_job_dict_for_project_member(mock_store):
    mock_store.get.return_value = _make_job(agent_id=3, provider="claude")
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/692")

    assert resp.status_code == 200
    body = resp.json()["job"]
    assert body["id"] == 692
    assert body["idea"] == "original idea"
    assert body["waiting_on"] == []
    assert body["agent_id"] == 3
    assert body["provider"] == "claude"
    assert body["current_executor"] == {
        "kind": "agent",
        "label": "claude",
        "agent_id": 3,
        "provider": "claude",
    }
    assert body["scheduler_wait"] is None


def test_get_job_includes_effective_priority_and_reasons(mock_store):
    reasons = [{"reason": "aging", "amount": 1, "detail": "aging boost"}]
    mock_store.get.return_value = _make_job(priority=4)
    mock_store.get_effective_priority.side_effect = None
    mock_store.get_effective_priority.return_value = (5, reasons)
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/692")

    assert resp.status_code == 200
    body = resp.json()["job"]
    assert body["priority"] == 4
    assert body["effective_priority"] == 5
    assert body["priority_reasons"] == reasons


@pytest.mark.parametrize(
    ("waiting_on", "scheduler_wait", "expected_scheduler_wait"),
    [
        (
            [],
            SchedulerWait(
                reason=SchedulerWaitReason.PROVIDER_CAPACITY,
                summary="waiting on provider capacity",
                blocking_job_ids=[],
                conflicting_paths=[],
            ),
            {
                "reason": "provider_capacity",
                "summary": "waiting on provider capacity",
                "blocking_job_ids": [],
                "conflicting_paths": [],
            },
        ),
        ([17], None, None),
        ([], None, None),
    ],
)
def test_get_job_projects_pending_wait_state(
    mock_store, waiting_on, scheduler_wait, expected_scheduler_wait
):
    mock_store.get.return_value = _make_job(status=JobStatus.PENDING)
    mock_store.get_unsatisfied_deps.return_value = waiting_on
    mock_store.get_scheduler_wait.return_value = scheduler_wait
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/692")

    assert resp.status_code == 200
    body = resp.json()["job"]
    assert body["waiting_on"] == waiting_on
    assert body["scheduler_wait"] == expected_scheduler_wait


@pytest.mark.parametrize(
    ("overrides", "expected_kind"),
    [
        ({"agent_id": 3, "provider": "claude"}, "agent"),
        ({"stage": Stage.LINT}, "pipeline"),
        ({"status": JobStatus.DEPLOYING, "stage": Stage.DEPLOY}, "pipeline"),
        ({"status": JobStatus.PENDING}, "waiting"),
    ],
)
def test_get_job_serializes_each_nonterminal_executor_state(mock_store, overrides, expected_kind):
    mock_store.get.return_value = _make_job(**overrides)
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/692")

    assert resp.status_code == 200
    assert set(resp.json()) == {"job"}
    assert resp.json()["job"]["current_executor"]["kind"] == expected_kind


def test_get_job_terminal_preserves_stale_ownership_and_filing_fields(mock_store):
    mock_store.get.return_value = _make_job(
        status=JobStatus.FAILED,
        agent_id=3,
        provider="claude",
        source=JobSource.CLI,
        source_actor="Ada",
    )
    client = _make_client(mock_store)

    body = client.get("/api/jobs/692").json()["job"]

    assert body["current_executor"] is None
    assert (body["agent_id"], body["provider"]) == (3, "claude")
    assert (body["source"], body["source_actor"]) == ("cli", "Ada")


def test_get_job_returns_404_for_unknown_job(mock_store):
    mock_store.get.return_value = None
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/999")

    assert resp.status_code == 404
    assert resp.json()["error"] == "not found"


def test_get_job_returns_403_for_non_member(mock_store):
    mock_store.get.return_value = _make_job(project_id=1)
    mock_store.list_project_members.return_value = []
    mock_store.get_session.return_value = User(
        id=42, email="outsider@example.com", is_platform_admin=False
    )
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/692")

    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden"


def test_job_events_returns_stable_envelope_with_enriched_mixed_agents(mock_store):
    mock_store.get.return_value = _make_job()
    events = [
        {
            "id": 1,
            "stage": "plan",
            "agent_id": 3,
            "agent_name": "Planner",
            "agent_provider": "claude",
            "agent_model": "claude-sonnet",
        },
        {
            "id": 2,
            "stage": "lint",
            "agent_id": None,
            "agent_name": None,
            "agent_provider": None,
            "agent_model": None,
        },
        {
            "id": 3,
            "stage": "review",
            "agent_id": 9,
            "agent_name": None,
            "agent_provider": None,
            "agent_model": None,
        },
    ]
    mock_store.list_events.return_value = events
    mock_store.list_supervisor_events_for_job.return_value = []
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/692/events")

    assert resp.status_code == 200
    assert resp.json() == {"events": events, "supervisor_events": []}


def test_job_events_returns_404_for_unknown_job(mock_store):
    mock_store.get.return_value = None
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/999/events")

    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


def test_job_events_returns_403_for_non_member(mock_store):
    mock_store.get.return_value = _make_job(project_id=1)
    mock_store.list_project_members.return_value = []
    mock_store.get_session.return_value = User(
        id=42, email="outsider@example.com", is_platform_admin=False
    )
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/692/events")

    assert resp.status_code == 403
    assert resp.json() == {"error": "forbidden"}


def test_job_detail_stream_returns_404_for_unknown_job(mock_store):
    mock_store.get.return_value = None
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/999/stream")

    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


def test_job_detail_stream_returns_403_for_non_member(mock_store):
    mock_store.get.return_value = _make_job(project_id=1)
    mock_store.list_project_members.return_value = []
    mock_store.get_session.return_value = User(
        id=42, email="outsider@example.com", is_platform_admin=False
    )
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/692/stream")

    assert resp.status_code == 403
    assert resp.json() == {"error": "forbidden"}


def test_job_detail_stream_emits_complete_terminal_snapshot(mock_store):
    mock_store.get.return_value = _make_job(
        status=JobStatus.DONE,
        stage=Stage.DEPLOY,
        agent_id=3,
        provider="claude",
    )
    mock_store.get_unsatisfied_deps.return_value = [17]
    mock_store.list_events.return_value = [{"id": 4, "event": "stage_done"}]
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/692/stream")

    assert resp.status_code == 200
    assert "event: job" in resp.text
    data_line = next(line for line in resp.text.splitlines() if line.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["job"]["id"] == 692
    assert payload["job"]["status"] == "done"
    assert payload["job"]["stage"] == "deploy"
    assert payload["job"]["agent_id"] == 3
    assert payload["job"]["provider"] == "claude"
    assert payload["job"]["current_executor"] is None
    assert payload["job"]["waiting_on"] == [17]
    assert payload["events"] == [{"id": 4, "event": "stage_done"}]
    assert resp.text.count("event: job") == 1


def test_job_detail_stream_suppresses_unchanged_and_emits_refreshes(mock_store, monkeypatch):
    running = _make_job(project_id=None, agent_id=3, provider="claude")
    reassigned = _make_job(project_id=None, agent_id=8, provider="codex")
    done = _make_job(
        project_id=None,
        status=JobStatus.DONE,
        stage=Stage.DEPLOY,
        agent_id=8,
        provider="codex",
    )
    mock_store.get.side_effect = [running, running, running, reassigned, done]
    mock_store.get_unsatisfied_deps.side_effect = [[17], [17], [], []]
    assigned_event = {
        "id": 1,
        "event": "started",
        "agent_id": 3,
        "agent_name": "Planner",
        "agent_provider": "claude",
        "agent_model": "claude-sonnet",
    }
    deterministic_event = {
        "id": 2,
        "event": "linted",
        "agent_id": None,
        "agent_name": None,
        "agent_provider": None,
        "agent_model": None,
    }
    historical_event = {
        "id": 3,
        "event": "reassigned",
        "agent_id": 9,
        "agent_name": None,
        "agent_provider": None,
        "agent_model": None,
    }
    done_event = {"id": 4, "event": "done"}
    mock_store.list_events.side_effect = [
        [assigned_event],
        [assigned_event],
        [assigned_event, deterministic_event, historical_event],
        [assigned_event, deterministic_event, historical_event, done_event],
    ]
    mock_store.list_users.return_value = ["existing-admin"]
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    app = build_app(config, MagicMock(), mock_store)
    route = next(
        route
        for route in app.app.routes
        if getattr(route, "path", None) == "/api/jobs/{job_id:int}/stream"
    )
    request = MagicMock()
    request.path_params = {"job_id": 692}
    request.is_disconnected = AsyncMock(return_value=False)
    monkeypatch.setattr("hyqs.web.app.asyncio.sleep", AsyncMock())

    async def collect_events():
        response = await route.endpoint(request)
        return [event async for event in response.body_iterator]

    emitted = asyncio.run(collect_events())

    assert [event["event"] for event in emitted] == ["job", "job", "job"]
    payloads = [json.loads(event["data"]) for event in emitted]
    assert payloads[0]["job"]["waiting_on"] == [17]
    assert payloads[0]["job"]["current_executor"] == {
        "kind": "agent",
        "label": "claude",
        "agent_id": 3,
        "provider": "claude",
    }
    assert payloads[0]["events"] == [assigned_event]
    assert payloads[1]["job"]["agent_id"] == 8
    assert payloads[1]["job"]["provider"] == "codex"
    assert payloads[1]["job"]["waiting_on"] == []
    assert payloads[1]["job"]["current_executor"] == {
        "kind": "agent",
        "label": "codex",
        "agent_id": 8,
        "provider": "codex",
    }
    assert payloads[1]["events"] == [
        assigned_event,
        deterministic_event,
        historical_event,
    ]
    assert payloads[2]["job"]["status"] == "done"
    assert payloads[2]["job"]["current_executor"] is None
    assert payloads[2]["events"][-1] == done_event
    assert mock_store.get.call_count == 5


@pytest.mark.parametrize(
    ("waiting_on", "scheduler_wait", "expected_scheduler_wait"),
    [
        (
            [],
            SchedulerWait(
                reason=SchedulerWaitReason.FILE_OVERLAP,
                summary="waiting on file overlap with job #4",
                blocking_job_ids=[4],
                conflicting_paths=["hyqs/web/app.py"],
            ),
            {
                "reason": "file_overlap",
                "summary": "waiting on file overlap with job #4",
                "blocking_job_ids": [4],
                "conflicting_paths": ["hyqs/web/app.py"],
            },
        ),
        ([17], None, None),
        ([], None, None),
    ],
)
def test_job_detail_stream_projects_pending_wait_state(
    mock_store,
    monkeypatch,
    waiting_on,
    scheduler_wait,
    expected_scheduler_wait,
):
    pending = _make_job(project_id=None, status=JobStatus.PENDING)
    mock_store.get.side_effect = [pending, pending]
    mock_store.get_unsatisfied_deps.return_value = waiting_on
    mock_store.get_scheduler_wait.return_value = scheduler_wait
    mock_store.list_events.return_value = []
    app = build_app(
        Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN),
        MagicMock(),
        mock_store,
    )
    route = next(
        route
        for route in app.app.routes
        if getattr(route, "path", None) == "/api/jobs/{job_id:int}/stream"
    )
    request = MagicMock()
    request.path_params = {"job_id": 692}
    request.is_disconnected = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr("hyqs.web.app.asyncio.sleep", AsyncMock())

    async def collect_events():
        response = await route.endpoint(request)
        return [event async for event in response.body_iterator]

    emitted = asyncio.run(collect_events())

    assert len(emitted) == 1
    payload = json.loads(emitted[0]["data"])
    assert payload["job"]["waiting_on"] == waiting_on
    assert payload["job"]["scheduler_wait"] == expected_scheduler_wait


def test_job_detail_stream_reflects_scheduler_wait_appearing_and_clearing(mock_store, monkeypatch):
    pending = _make_job(project_id=None, status=JobStatus.PENDING)
    mock_store.get.side_effect = [pending, pending, pending, pending]
    wait = SchedulerWait(
        reason=SchedulerWaitReason.SCHEMA_LOCK,
        summary="waiting on schema lock held by job #5",
        blocking_job_ids=[5],
        conflicting_paths=[],
    )
    mock_store.get_scheduler_wait.side_effect = [wait, wait, None]
    mock_store.list_events.return_value = []
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    app = build_app(config, MagicMock(), mock_store)
    route = next(
        route
        for route in app.app.routes
        if getattr(route, "path", None) == "/api/jobs/{job_id:int}/stream"
    )
    request = MagicMock()
    request.path_params = {"job_id": 692}
    request.is_disconnected = AsyncMock(side_effect=[False, False, False, True])
    monkeypatch.setattr("hyqs.web.app.asyncio.sleep", AsyncMock())

    async def collect_events():
        response = await route.endpoint(request)
        return [event async for event in response.body_iterator]

    emitted = asyncio.run(collect_events())

    payloads = [json.loads(event["data"]) for event in emitted]
    assert len(payloads) == 2
    assert payloads[0]["job"]["waiting_on"] == []
    assert payloads[0]["job"]["scheduler_wait"]["reason"] == "schema_lock"
    assert payloads[0]["job"]["scheduler_wait"]["blocking_job_ids"] == [5]
    assert payloads[1]["job"]["waiting_on"] == []
    assert payloads[1]["job"]["scheduler_wait"] is None
    assert payloads[1]["job"]["status"] == "pending"


def test_get_job_round_trips_created_job_fields(store):
    repo_path = f"/tmp/test-get-job-{uuid.uuid4()}"
    job = store.create(
        idea="build the widget",
        repo_path=repo_path,
        chat_id=1,
        source=JobSource.UI,
    )
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})

    resp = client.get(f"/api/jobs/{job.id}")

    assert resp.status_code == 200
    body = resp.json()["job"]
    assert body["id"] == job.id
    assert body["idea"] == "build the widget"
    assert body["repo_path"] == repo_path
    assert body["project_id"] == job.project_id
    assert body["source"] == "ui"
    assert body["agent_id"] == job.agent_id
    assert body["provider"] == job.provider

    store.delete_project(job.project_id)


def test_get_job_usage_returns_usage_list_for_project_member(mock_store):
    mock_store.get.return_value = _make_job()
    usage_rows = [
        {
            "source": "build",
            "model": "claude-sonnet-4-6",
            "provider": "claude",
            "input_tokens": 300,
            "output_tokens": 75,
            "cache_creation_tokens": 10,
            "cache_read_tokens": 5,
            "cost_usd": 0.0323,
            "runs": 2,
        }
    ]
    mock_store.get_job_usage.return_value = usage_rows
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/692/usage")

    assert resp.status_code == 200
    assert resp.json() == usage_rows
    mock_store.get_job_usage.assert_called_once_with(692)


def test_get_job_usage_returns_404_for_unknown_job(mock_store):
    mock_store.get.return_value = None
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/999/usage")

    assert resp.status_code == 404
    assert resp.json()["error"] == "not found"


def test_get_job_usage_returns_403_for_non_member(mock_store):
    mock_store.get.return_value = _make_job(project_id=1)
    mock_store.list_project_members.return_value = []
    mock_store.get_session.return_value = User(
        id=42, email="outsider@example.com", is_platform_admin=False
    )
    client = _make_client(mock_store)

    resp = client.get("/api/jobs/692/usage")

    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden"
