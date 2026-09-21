"""Regression tests for the fleet snapshot bug (Job #724): _fleet_snapshot must not
crash with UnboundLocalError when the first-listed worker is idle, and must not
leak a busy worker's project_id onto a following idle worker.

Also covers the webhook CRUD REST endpoints (Job #980): list/create/toggle-active/
delete, backed by the real Starlette app with a MagicMock store — same pattern as
tests/test_backlog_endpoints.py.
"""

from unittest.mock import ANY, MagicMock, patch

import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.pipeline.models import ApiToken, User, Webhook
from hyqs.web.app import _fleet_snapshot, build_app


def _worker(id, *, job_id=None, status="idle", alive=True):
    return {
        "id": id,
        "host": "host1",
        "pid": 1000 + id,
        "status": status,
        "alive": alive,
        "job_id": job_id,
        "stage": "build" if job_id else None,
        "provider": "claude",
        "role": "worker",
        "started_at": 100.0,
        "last_seen": 100.0,
    }


def _make_store(workers):
    store = MagicMock()
    store.list_workers.return_value = workers
    store.list_active.return_value = []
    store.list_provider_pauses.return_value = []
    store.list_merge_locks.return_value = []
    return store


def _make_config():
    config = MagicMock()
    config.pipeline_lease_ttl = 90
    return config


def test_fleet_snapshot_all_idle_workers_no_exception():
    workers = [_worker(1, status="idle"), _worker(2, status="idle")]
    store = _make_store(workers)
    snapshot = _fleet_snapshot(store, _make_config())
    assert len(snapshot["workers"]) == 2
    assert all(w["project_id"] is None for w in snapshot["workers"])


def test_fleet_snapshot_idle_worker_after_busy_does_not_inherit_project_id():
    busy_job = MagicMock()
    busy_job.project_id = 42
    busy_job.idea = "busy job idea"

    store = _make_store(
        [
            _worker(1, job_id=7, status="busy"),
            _worker(2, job_id=None, status="idle"),
        ]
    )
    store.get.side_effect = lambda job_id: busy_job if job_id == 7 else None

    snapshot = _fleet_snapshot(store, _make_config())
    workers = snapshot["workers"]
    assert workers[0]["project_id"] == 42
    assert workers[1]["project_id"] is None


def test_fleet_snapshot_busy_worker_has_correct_project_id():
    job = MagicMock()
    job.project_id = 99
    job.idea = "the idea"

    store = _make_store([_worker(1, job_id=5, status="busy")])
    store.get.return_value = job

    snapshot = _fleet_snapshot(store, _make_config())
    assert snapshot["workers"][0]["project_id"] == 99
    assert snapshot["workers"][0]["idea"] == "the idea"


_TOKEN = "test-web-token"


def _make_webhook(**overrides) -> Webhook:
    defaults = dict(
        id=7,
        project_id=1,
        url="https://example.com/hook",
        event_type="job_complete",
        created_by="member@example.com",
        active=True,
        kind="http",
    )
    defaults.update(overrides)
    return Webhook(**defaults)


def _make_webhook_client(store: MagicMock, *, permissions: list[str] | None = None) -> TestClient:
    store.list_users.return_value = ["existing-admin"]
    store.get_role_permissions.return_value = {"platform_admin": permissions or []}
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})
    return client


def _make_session_client(store: MagicMock, user: User) -> TestClient:
    store.get_session.return_value = user
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    client = TestClient(build_app(config, MagicMock(), store))
    client.headers.update({"Authorization": "Bearer session-token"})
    return client


def _make_api_token_client(store: MagicMock, *, project_id: int) -> TestClient:
    store.get_session.return_value = None
    store.get_api_token_by_secret.return_value = ApiToken(
        id=9, project_id=project_id, name="scoped", role="viewer", last4="cret"
    )
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    client = TestClient(build_app(config, MagicMock(), store))
    client.headers.update({"Authorization": "Bearer scoped-token-secret"})
    return client


@pytest.fixture
def webhook_store() -> MagicMock:
    s = MagicMock()
    return s


def test_list_webhooks_route_returns_404_for_unknown_project(webhook_store):
    webhook_store.get_project.return_value = None
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.get("/api/projects/1/webhooks")

    assert resp.status_code == 404


def test_list_webhooks_route_returns_403_without_permission(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    client = _make_webhook_client(webhook_store, permissions=[])

    resp = client.get("/api/projects/1/webhooks")

    assert resp.status_code == 403


def test_list_webhooks_route_returns_webhooks_for_authorized_caller(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.list_webhooks.return_value = [_make_webhook()]
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.get("/api/projects/1/webhooks")

    assert resp.status_code == 200
    webhooks = resp.json()["webhooks"]
    assert [w["id"] for w in webhooks] == [7]


def test_create_webhook_route_returns_201_for_valid_http_config(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.create_webhook.return_value = _make_webhook()
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.post(
        "/api/projects/1/webhooks",
        json={"url": "https://example.com/hook", "event_type": "job_complete"},
    )

    assert resp.status_code == 201
    body = resp.json()["webhook"]
    assert body["project_id"] == 1
    assert body["url"] == "https://example.com/hook"
    assert body["event_type"] == "job_complete"
    assert body["kind"] == "http"


def test_create_webhook_route_rejects_empty_url(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.post("/api/projects/1/webhooks", json={"url": "", "event_type": "job_complete"})

    assert resp.status_code == 400
    assert "error" in resp.json()
    webhook_store.create_webhook.assert_not_called()


def test_create_webhook_route_rejects_invalid_event_type(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.post(
        "/api/projects/1/webhooks",
        json={"url": "https://example.com/hook", "event_type": "bogus"},
    )

    assert resp.status_code == 400
    webhook_store.create_webhook.assert_not_called()


def test_create_webhook_route_returns_403_without_permission(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    client = _make_webhook_client(webhook_store, permissions=[])

    resp = client.post(
        "/api/projects/1/webhooks",
        json={"url": "https://example.com/hook", "event_type": "job_complete"},
    )

    assert resp.status_code == 403
    webhook_store.create_webhook.assert_not_called()


def test_set_webhook_active_route_updates_active(webhook_store):
    webhook_store.get_webhook.return_value = _make_webhook()
    webhook_store.set_webhook_active.return_value = _make_webhook(active=False)
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.post("/api/webhooks/7/active", json={"active": False})

    assert resp.status_code == 200
    assert resp.json()["webhook"]["active"] is False
    webhook_store.set_webhook_active.assert_called_once_with(7, False)


def test_set_webhook_active_route_returns_404_for_unknown_webhook(webhook_store):
    webhook_store.get_webhook.return_value = None
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.post("/api/webhooks/999/active", json={"active": False})

    assert resp.status_code == 404


def test_set_webhook_active_route_returns_403_without_permission(webhook_store):
    webhook_store.get_webhook.return_value = _make_webhook()
    client = _make_webhook_client(webhook_store, permissions=[])

    resp = client.post("/api/webhooks/7/active", json={"active": False})

    assert resp.status_code == 403
    webhook_store.set_webhook_active.assert_not_called()


def test_set_webhook_active_route_rejects_missing_active_field(webhook_store):
    webhook_store.get_webhook.return_value = _make_webhook()
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.post("/api/webhooks/7/active", json={})

    assert resp.status_code == 400
    webhook_store.set_webhook_active.assert_not_called()


def test_delete_webhook_route_deletes_and_returns_200(webhook_store):
    webhook_store.get_webhook.return_value = _make_webhook()
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.delete("/api/webhooks/7")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "id": 7}
    webhook_store.delete_webhook.assert_called_once_with(7)


def test_delete_webhook_route_returns_404_for_unknown_webhook(webhook_store):
    webhook_store.get_webhook.return_value = None
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.delete("/api/webhooks/999")

    assert resp.status_code == 404


def test_delete_webhook_route_returns_403_without_permission(webhook_store):
    webhook_store.get_webhook.return_value = _make_webhook()
    client = _make_webhook_client(webhook_store, permissions=[])

    resp = client.delete("/api/webhooks/7")

    assert resp.status_code == 403
    webhook_store.delete_webhook.assert_not_called()


def test_get_slack_credentials_route_returns_404_for_unknown_project(webhook_store):
    webhook_store.get_project.return_value = None
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.get("/api/projects/1/slack-credentials")

    assert resp.status_code == 404


def test_get_slack_credentials_route_returns_403_without_permission(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    client = _make_webhook_client(webhook_store, permissions=[])

    resp = client.get("/api/projects/1/slack-credentials")

    assert resp.status_code == 403


def test_get_slack_credentials_route_returns_not_configured_when_no_token(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.get_project_slack_token.return_value = None
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.get("/api/projects/1/slack-credentials")

    assert resp.status_code == 200
    assert resp.json() == {"configured": False, "status": "not_configured"}


def test_get_slack_credentials_route_returns_authenticated_when_token_set(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.get_project_slack_token.return_value = "xoxb-secret-token"
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.get("/api/projects/1/slack-credentials")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"configured": True, "status": "authenticated"}
    assert "xoxb-secret-token" not in resp.text


def test_set_slack_credentials_route_returns_404_for_unknown_project(webhook_store):
    webhook_store.get_project.return_value = None
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.post("/api/projects/1/slack-credentials", json={"bot_token": "xoxb-abc"})

    assert resp.status_code == 404


def test_set_slack_credentials_route_returns_403_without_permission(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    client = _make_webhook_client(webhook_store, permissions=[])

    resp = client.post("/api/projects/1/slack-credentials", json={"bot_token": "xoxb-abc"})

    assert resp.status_code == 403
    webhook_store.set_project_slack_token.assert_not_called()


def test_set_slack_credentials_route_rejects_missing_bot_token(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.post("/api/projects/1/slack-credentials", json={})

    assert resp.status_code == 400
    assert "error" in resp.json()
    webhook_store.set_project_slack_token.assert_not_called()


def test_set_slack_credentials_route_rejects_blank_bot_token(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.post("/api/projects/1/slack-credentials", json={"bot_token": "   "})

    assert resp.status_code == 400
    webhook_store.set_project_slack_token.assert_not_called()


def test_set_slack_credentials_route_persists_token_and_returns_200(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    client = _make_webhook_client(webhook_store, permissions=["manage_webhooks"])

    resp = client.post("/api/projects/1/slack-credentials", json={"bot_token": "xoxb-abc"})

    assert resp.status_code == 200
    assert resp.json() == {"configured": True, "status": "authenticated"}
    webhook_store.set_project_slack_token.assert_called_once_with(1, "xoxb-abc", ANY)


def test_usage_route_with_no_params_forwards_none_project_id(webhook_store):
    webhook_store.usage_summary.return_value = {
        "by_source": [],
        "projects": [],
        "total_tokens": 0,
        "total_cost_usd": 0.0,
    }
    client = _make_webhook_client(webhook_store, permissions=["view_audit"])

    resp = client.get("/api/usage")

    assert resp.status_code == 200
    webhook_store.usage_summary.assert_called_once_with(
        since=None, project_id=None, include_operational=False
    )


def test_usage_route_with_project_id_forwards_it_to_store(webhook_store):
    webhook_store.usage_summary.return_value = {
        "by_source": [],
        "projects": [],
        "total_tokens": 0,
        "total_cost_usd": 0.0,
    }
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/usage?project_id=3")

    assert resp.status_code == 200
    webhook_store.usage_summary.assert_called_once_with(
        since=None, project_id=3, include_operational=False
    )


def test_usage_route_with_project_id_allows_project_member(webhook_store):
    webhook_store.usage_summary.return_value = {"projects": []}
    webhook_store.list_project_members.return_value = [MagicMock(user_id="42")]
    client = _make_session_client(
        webhook_store,
        User(id=42, email="member@example.com", is_platform_admin=False),
    )

    resp = client.get("/api/usage?project_id=3")

    assert resp.status_code == 200
    webhook_store.usage_summary.assert_called_once_with(
        since=None, project_id=3, include_operational=False
    )


def test_usage_route_with_project_id_rejects_non_member_without_store_read(webhook_store):
    webhook_store.list_project_members.return_value = [MagicMock(user_id="7")]
    client = _make_session_client(
        webhook_store,
        User(id=42, email="outsider@example.com", is_platform_admin=False),
    )

    resp = client.get("/api/usage?project_id=3")

    assert resp.status_code == 403
    webhook_store.usage_summary.assert_not_called()


def test_usage_route_with_project_id_allows_matching_api_token(webhook_store):
    webhook_store.usage_summary.return_value = {"projects": []}
    client = _make_api_token_client(webhook_store, project_id=3)

    resp = client.get("/api/usage?project_id=3")

    assert resp.status_code == 200
    webhook_store.usage_summary.assert_called_once_with(
        since=None, project_id=3, include_operational=False
    )


def test_usage_route_with_project_id_rejects_other_project_api_token(webhook_store):
    client = _make_api_token_client(webhook_store, project_id=4)

    resp = client.get("/api/usage?project_id=3")

    assert resp.status_code == 403
    webhook_store.usage_summary.assert_not_called()


def test_usage_route_without_project_rejects_project_member(webhook_store):
    client = _make_session_client(
        webhook_store,
        User(id=42, email="member@example.com", is_platform_admin=False),
    )
    webhook_store.list_platform_permissions.return_value = []

    resp = client.get("/api/usage")

    assert resp.status_code == 403
    webhook_store.usage_summary.assert_not_called()


def test_usage_route_without_project_rejects_project_api_token(webhook_store):
    client = _make_api_token_client(webhook_store, project_id=3)

    resp = client.get("/api/usage")

    assert resp.status_code == 403
    webhook_store.usage_summary.assert_not_called()


def test_usage_route_with_since_and_project_id_forwards_both(webhook_store):
    webhook_store.usage_summary.return_value = {
        "by_source": [],
        "projects": [],
        "total_tokens": 0,
        "total_cost_usd": 0.0,
    }
    client = _make_webhook_client(webhook_store, permissions=["view_audit"])

    resp = client.get("/api/usage?since=2026-01-01T00:00:00&project_id=3")

    assert resp.status_code == 200
    webhook_store.usage_summary.assert_called_once_with(
        since="2026-01-01T00:00:00", project_id=3, include_operational=False
    )


def test_usage_route_with_include_operational_true_forwards_it(webhook_store):
    webhook_store.usage_summary.return_value = {
        "by_source": [],
        "projects": [],
        "total_tokens": 0,
        "total_cost_usd": 0.0,
    }
    client = _make_webhook_client(webhook_store, permissions=["view_audit"])

    resp = client.get("/api/usage?include_operational=true")

    assert resp.status_code == 200
    webhook_store.usage_summary.assert_called_once_with(
        since=None, project_id=None, include_operational=True
    )


def test_supervisor_snapshot_returns_403_without_view_fleet(webhook_store):
    client = _make_webhook_client(webhook_store, permissions=[])

    resp = client.get("/api/supervisor")

    assert resp.status_code == 403
    webhook_store.get_failed_jobs.assert_not_called()
    webhook_store.list_workers.assert_not_called()


def test_supervisor_snapshot_returns_data_with_view_fleet(webhook_store):
    webhook_store.list_workers.return_value = []
    webhook_store.get_failed_jobs.return_value = []
    webhook_store.list_supervisor_events.return_value = []
    webhook_store.list_supervisor_requeues.return_value = []
    client = _make_webhook_client(webhook_store, permissions=["view_fleet"])

    resp = client.get("/api/supervisor")

    assert resp.status_code == 200
    assert resp.json()["needs_judgment"] == []
    webhook_store.get_failed_jobs.assert_called_once()


def test_supervisor_stream_returns_403_without_view_fleet(webhook_store):
    client = _make_webhook_client(webhook_store, permissions=[])

    resp = client.get("/api/supervisor/stream")

    assert resp.status_code == 403
    webhook_store.get_failed_jobs.assert_not_called()
    webhook_store.list_workers.assert_not_called()


def test_supervisor_stream_is_created_with_view_fleet(webhook_store):
    client = _make_webhook_client(webhook_store, permissions=["view_fleet"])

    with patch("hyqs.web.app.EventSourceResponse", return_value=JSONResponse({"ok": True})):
        resp = client.get("/api/supervisor/stream")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_jobs_filed_by_breakdown_route_returns_403_without_permission(webhook_store):
    client = _make_webhook_client(webhook_store, permissions=[])

    resp = client.get("/api/jobs/filed-by-breakdown")

    assert resp.status_code == 403
    webhook_store.jobs_filed_by_breakdown.assert_not_called()


def test_jobs_filed_by_breakdown_route_returns_200_with_breakdown(webhook_store):
    webhook_store.jobs_filed_by_breakdown.return_value = [
        {"source": "ui", "source_actor": "member@example.com", "count": 3},
    ]
    client = _make_webhook_client(webhook_store, permissions=["view_audit"])

    resp = client.get("/api/jobs/filed-by-breakdown")

    assert resp.status_code == 200
    assert resp.json()["breakdown"] == [
        {"source": "ui", "source_actor": "member@example.com", "count": 3}
    ]
    webhook_store.jobs_filed_by_breakdown.assert_called_once_with(since=None)


def test_jobs_filed_by_breakdown_route_forwards_since(webhook_store):
    webhook_store.jobs_filed_by_breakdown.return_value = []
    client = _make_webhook_client(webhook_store, permissions=["view_audit"])

    resp = client.get("/api/jobs/filed-by-breakdown?since=2026-01-01T00:00:00")

    assert resp.status_code == 200
    webhook_store.jobs_filed_by_breakdown.assert_called_once_with(since="2026-01-01T00:00:00")


def test_deployment_reliability_route_returns_403_without_permission(webhook_store):
    client = _make_webhook_client(webhook_store, permissions=[])

    resp = client.get("/api/deployment-reliability")

    assert resp.status_code == 403
    webhook_store.deployment_reliability_breakdown.assert_not_called()


def test_deployment_reliability_route_returns_200_with_breakdown(webhook_store):
    webhook_store.deployment_reliability_breakdown.return_value = [
        {"project_id": 1, "total": 5, "succeeded": 4, "failed": 1},
    ]
    client = _make_webhook_client(webhook_store, permissions=["view_audit"])

    resp = client.get("/api/deployment-reliability")

    assert resp.status_code == 200
    assert resp.json()["breakdown"] == [{"project_id": 1, "total": 5, "succeeded": 4, "failed": 1}]
    webhook_store.deployment_reliability_breakdown.assert_called_once_with(since=None)


def test_deployment_reliability_route_forwards_since(webhook_store):
    webhook_store.deployment_reliability_breakdown.return_value = []
    client = _make_webhook_client(webhook_store, permissions=["view_audit"])

    resp = client.get("/api/deployment-reliability?since=2026-01-01T00:00:00")

    assert resp.status_code == 200
    webhook_store.deployment_reliability_breakdown.assert_called_once_with(
        since="2026-01-01T00:00:00"
    )


def test_admin_page_views_route_returns_403_without_permission(webhook_store):
    client = _make_webhook_client(webhook_store, permissions=[])

    resp = client.get("/api/admin/page-views")

    assert resp.status_code == 403
    webhook_store.page_view_admin_summary.assert_not_called()


def test_admin_page_views_route_returns_200_with_summary(webhook_store):
    summary = MagicMock()
    summary.to_dict.return_value = {
        "totals": {"views": 5, "unique_visitors": 3},
        "by_day": [],
        "by_country": [],
        "by_city": [],
        "by_ref": [],
        "by_referrer": [],
        "by_device": [],
        "recent": [],
    }
    webhook_store.page_view_admin_summary.return_value = summary
    client = _make_webhook_client(webhook_store, permissions=["view_audit"])

    resp = client.get("/api/admin/page-views")

    assert resp.status_code == 200
    assert resp.json() == summary.to_dict.return_value
    _, kwargs = webhook_store.page_view_admin_summary.call_args
    assert kwargs["path"] is None
    assert kwargs["since"] < kwargs["until"]


def test_admin_page_views_route_forwards_since_until_and_path(webhook_store):
    summary = MagicMock()
    summary.to_dict.return_value = {}
    webhook_store.page_view_admin_summary.return_value = summary
    client = _make_webhook_client(webhook_store, permissions=["view_audit"])

    resp = client.get(
        "/api/admin/page-views",
        params={
            "since": "2026-01-01T00:00:00Z",
            "until": "2026-01-31T00:00:00Z",
            "path": "/how-it-works",
        },
    )

    assert resp.status_code == 200
    webhook_store.page_view_admin_summary.assert_called_once_with(
        since="2026-01-01T00:00:00Z",
        until="2026-01-31T00:00:00Z",
        path="/how-it-works",
    )


def test_agent_stats_route_forwards_include_operational_and_defaults_false(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.agent_stats.return_value = [{"agent_id": 1, "runs": 3}]
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/agent-stats")

    assert resp.status_code == 200
    body = resp.json()
    assert body["stats"] == [{"agent_id": 1, "runs": 3}]
    assert body["excludes_operational"] is True
    webhook_store.agent_stats.assert_called_once_with(1, include_operational=False)


def test_agent_stats_route_forwards_include_operational_true(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.agent_stats.return_value = []
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/agent-stats?include_operational=true")

    assert resp.status_code == 200
    assert resp.json()["excludes_operational"] is False
    webhook_store.agent_stats.assert_called_once_with(1, include_operational=True)


def test_perf_stage_stats_route_forwards_include_operational(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.performance_stage_stats.return_value = []
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/performance/stage-stats?include_operational=true")

    assert resp.status_code == 200
    webhook_store.performance_stage_stats.assert_called_once_with(
        1,
        from_date=None,
        to_date=None,
        stage=None,
        epic_id=None,
        status=None,
        provider=None,
        include_operational=True,
    )


def test_perf_stage_stats_route_defaults_include_operational_false(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.performance_stage_stats.return_value = []
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/performance/stage-stats")

    assert resp.status_code == 200
    webhook_store.performance_stage_stats.assert_called_once_with(
        1,
        from_date=None,
        to_date=None,
        stage=None,
        epic_id=None,
        status=None,
        provider=None,
        include_operational=False,
    )


def test_perf_slowest_jobs_route_forwards_include_operational(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.performance_slowest_jobs.return_value = []
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/performance/slowest-jobs?include_operational=true")

    assert resp.status_code == 200
    webhook_store.performance_slowest_jobs.assert_called_once_with(
        1,
        20,
        from_date=None,
        to_date=None,
        stage=None,
        epic_id=None,
        status=None,
        provider=None,
        include_operational=True,
    )


def test_perf_slowest_jobs_route_defaults_include_operational_false(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.performance_slowest_jobs.return_value = []
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/performance/slowest-jobs")

    assert resp.status_code == 200
    webhook_store.performance_slowest_jobs.assert_called_once_with(
        1,
        20,
        from_date=None,
        to_date=None,
        stage=None,
        epic_id=None,
        status=None,
        provider=None,
        include_operational=False,
    )


def test_perf_headline_route_forwards_include_operational(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.performance_headline_stats.return_value = {}
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/performance/headline?include_operational=true")

    assert resp.status_code == 200
    webhook_store.performance_headline_stats.assert_called_once_with(
        1,
        from_date=None,
        to_date=None,
        stage=None,
        epic_id=None,
        status=None,
        provider=None,
        include_operational=True,
    )


def test_perf_headline_route_defaults_include_operational_false(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.performance_headline_stats.return_value = {}
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/performance/headline")

    assert resp.status_code == 200
    webhook_store.performance_headline_stats.assert_called_once_with(
        1,
        from_date=None,
        to_date=None,
        stage=None,
        epic_id=None,
        status=None,
        provider=None,
        include_operational=False,
    )


def test_perf_trend_route_returns_200_with_day_bucketed_rows_for_member(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.performance_trend.return_value = [
        {"day": "2026-07-20", "cost_usd": 1.5, "tokens": 100, "completed": 2, "failed": 0},
    ]
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/performance/trend")

    assert resp.status_code == 200
    assert resp.json() == [
        {"day": "2026-07-20", "cost_usd": 1.5, "tokens": 100, "completed": 2, "failed": 0},
    ]


def test_perf_trend_route_returns_403_for_non_member(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.list_project_members.return_value = []
    webhook_store.get_session.return_value = User(
        id=42, email="outsider@example.com", is_platform_admin=False
    )
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/performance/trend")

    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden"
    webhook_store.performance_trend.assert_not_called()


def test_perf_trend_route_returns_404_for_unknown_project(webhook_store):
    webhook_store.get_project.return_value = None
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/performance/trend")

    assert resp.status_code == 404
    webhook_store.performance_trend.assert_not_called()


def test_perf_trend_route_forwards_from_and_to_query_params(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.performance_trend.return_value = []
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/performance/trend?from=2026-07-01&to=2026-07-20")

    assert resp.status_code == 200
    webhook_store.performance_trend.assert_called_once_with(
        1, from_date="2026-07-01", to_date="2026-07-20", include_operational=False
    )


def test_perf_trend_route_forwards_include_operational_true(webhook_store):
    webhook_store.get_project.return_value = MagicMock()
    webhook_store.performance_trend.return_value = []
    client = _make_webhook_client(webhook_store)

    resp = client.get("/api/projects/1/performance/trend?include_operational=true")

    assert resp.status_code == 200
    webhook_store.performance_trend.assert_called_once_with(
        1, from_date=None, to_date=None, include_operational=True
    )
