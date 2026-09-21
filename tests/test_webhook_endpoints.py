"""Tests for the webhook + Slack-credential web endpoints (S7).

Builds the real Starlette app (hyqs.web.app.build_app) with a MagicMock store
so each route's validation and store wiring can be checked with
starlette.testclient.TestClient, without a live Postgres connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.pipeline.models import Webhook
from hyqs.web.app import build_app

_TOKEN = "test-web-token"


def _make_webhook(**overrides) -> Webhook:
    defaults = dict(
        id=7,
        project_id=1,
        url="https://example.com/hook",
        event_type="job_complete",
        created_by="user:member@example.com",
        active=True,
        kind="http",
    )
    defaults.update(overrides)
    return Webhook(**defaults)


def _make_client(store: MagicMock, *, permissions: list[str] | None = None) -> TestClient:
    store.list_users.return_value = ["existing-admin"]
    store.get_role_permissions.return_value = {"platform_admin": permissions or []}
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})
    return client


@pytest.fixture
def store() -> MagicMock:
    s = MagicMock()
    s.get_project.return_value = MagicMock(id=1)
    return s


# --- GET /api/projects/{id}/webhooks --------------------------------------


def test_list_webhooks_route_returns_webhooks(store):
    store.list_webhooks.return_value = [_make_webhook()]
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.get("/api/projects/1/webhooks")

    assert resp.status_code == 200
    webhooks = resp.json()["webhooks"]
    assert [w["id"] for w in webhooks] == [7]


def test_list_webhooks_route_rejects_without_permission(store):
    client = _make_client(store, permissions=[])

    resp = client.get("/api/projects/1/webhooks")

    assert resp.status_code == 403
    store.list_webhooks.assert_not_called()


def test_list_webhooks_route_rejects_unknown_project(store):
    store.get_project.return_value = None
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.get("/api/projects/1/webhooks")

    assert resp.status_code == 404
    store.list_webhooks.assert_not_called()


# --- POST /api/projects/{id}/webhooks -------------------------------------


def test_create_webhook_route_accepts_valid_http_webhook(store):
    store.create_webhook.return_value = _make_webhook()
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post(
        "/api/projects/1/webhooks",
        json={"url": "https://example.com/hook", "event_type": "job_complete", "kind": "http"},
    )

    assert resp.status_code == 201
    assert resp.json()["webhook"]["id"] == 7
    store.create_webhook.assert_called_once()


def test_create_webhook_route_accepts_valid_needs_attention_webhook(store):
    store.create_webhook.return_value = _make_webhook(event_type="needs_attention")
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post(
        "/api/projects/1/webhooks",
        json={"url": "https://example.com/hook", "event_type": "needs_attention", "kind": "http"},
    )

    assert resp.status_code == 201
    assert resp.json()["webhook"]["event_type"] == "needs_attention"
    store.create_webhook.assert_called_once()


def test_create_webhook_route_rejects_unknown_project(store):
    store.get_project.return_value = None
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post(
        "/api/projects/1/webhooks",
        json={"url": "https://example.com/hook", "event_type": "job_complete"},
    )

    assert resp.status_code == 404
    store.create_webhook.assert_not_called()


def test_create_webhook_route_rejects_without_permission(store):
    client = _make_client(store, permissions=[])

    resp = client.post(
        "/api/projects/1/webhooks",
        json={"url": "https://example.com/hook", "event_type": "job_complete"},
    )

    assert resp.status_code == 403
    store.create_webhook.assert_not_called()


def test_create_webhook_route_rejects_missing_url(store):
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post("/api/projects/1/webhooks", json={"url": "", "event_type": "job_complete"})

    assert resp.status_code == 400
    assert resp.json()["error"] == "url is required"
    store.create_webhook.assert_not_called()


def test_create_webhook_route_rejects_url_without_scheme(store):
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post(
        "/api/projects/1/webhooks",
        json={"url": "example.com/hook", "event_type": "job_complete", "kind": "http"},
    )

    assert resp.status_code == 400
    assert "http" in resp.json()["error"]
    store.create_webhook.assert_not_called()


def test_create_webhook_route_rejects_invalid_event_type(store):
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post(
        "/api/projects/1/webhooks",
        json={"url": "https://example.com/hook", "event_type": "bogus"},
    )

    assert resp.status_code == 400
    assert "event_type" in resp.json()["error"]
    store.create_webhook.assert_not_called()


def test_create_webhook_route_rejects_slack_url_not_matching_channel_id(store):
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post(
        "/api/projects/1/webhooks",
        json={"url": "not-a-channel", "event_type": "job_complete", "kind": "slack"},
    )

    assert resp.status_code == 400
    assert "channel ID" in resp.json()["error"]
    store.create_webhook.assert_not_called()


def test_create_webhook_route_accepts_valid_slack_channel_id(store):
    store.create_webhook.return_value = _make_webhook(url="C0123456", kind="slack")
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post(
        "/api/projects/1/webhooks",
        json={"url": "C0123456", "event_type": "job_complete", "kind": "slack"},
    )

    assert resp.status_code == 201
    store.create_webhook.assert_called_once()


def test_create_webhook_route_rejects_invalid_json(store):
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post(
        "/api/projects/1/webhooks",
        content="not json",
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid JSON"
    store.create_webhook.assert_not_called()


# --- POST /api/webhooks/{id}/active ---------------------------------------


def test_set_webhook_active_route_toggles_active(store):
    store.get_webhook.return_value = _make_webhook()
    store.set_webhook_active.return_value = _make_webhook(active=False)
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post("/api/webhooks/7/active", json={"active": False})

    assert resp.status_code == 200
    assert resp.json()["webhook"]["active"] is False
    store.set_webhook_active.assert_called_once_with(7, False)


def test_set_webhook_active_route_rejects_unknown_webhook(store):
    store.get_webhook.return_value = None
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post("/api/webhooks/999/active", json={"active": True})

    assert resp.status_code == 404
    store.set_webhook_active.assert_not_called()


def test_set_webhook_active_route_rejects_without_permission(store):
    store.get_webhook.return_value = _make_webhook()
    client = _make_client(store, permissions=[])

    resp = client.post("/api/webhooks/7/active", json={"active": True})

    assert resp.status_code == 403
    store.set_webhook_active.assert_not_called()


def test_set_webhook_active_route_rejects_non_boolean_active(store):
    store.get_webhook.return_value = _make_webhook()
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post("/api/webhooks/7/active", json={"active": "yes"})

    assert resp.status_code == 400
    assert resp.json()["error"] == "active must be a boolean"
    store.set_webhook_active.assert_not_called()


def test_set_webhook_active_route_rejects_invalid_json(store):
    store.get_webhook.return_value = _make_webhook()
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post(
        "/api/webhooks/7/active",
        content="not json",
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid JSON"
    store.set_webhook_active.assert_not_called()


# --- DELETE /api/webhooks/{id} ---------------------------------------------


def test_delete_webhook_route_deletes(store):
    store.get_webhook.return_value = _make_webhook()
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.delete("/api/webhooks/7")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "id": 7}
    store.delete_webhook.assert_called_once_with(7)


def test_delete_webhook_route_rejects_unknown_webhook(store):
    store.get_webhook.return_value = None
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.delete("/api/webhooks/999")

    assert resp.status_code == 404
    store.delete_webhook.assert_not_called()


def test_delete_webhook_route_rejects_without_permission(store):
    store.get_webhook.return_value = _make_webhook()
    client = _make_client(store, permissions=[])

    resp = client.delete("/api/webhooks/7")

    assert resp.status_code == 403
    store.delete_webhook.assert_not_called()


# --- GET /api/projects/{id}/slack-credentials -------------------------------


def test_get_slack_credentials_route_reports_not_configured(store):
    store.get_project_slack_token.return_value = None
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.get("/api/projects/1/slack-credentials")

    assert resp.status_code == 200
    assert resp.json() == {"configured": False, "status": "not_configured"}


def test_get_slack_credentials_route_reports_authenticated(store):
    store.get_project_slack_token.return_value = "xoxb-secret-token"
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.get("/api/projects/1/slack-credentials")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"configured": True, "status": "authenticated"}
    assert "xoxb-secret-token" not in resp.text


def test_get_slack_credentials_route_rejects_unknown_project(store):
    store.get_project.return_value = None
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.get("/api/projects/1/slack-credentials")

    assert resp.status_code == 404
    store.get_project_slack_token.assert_not_called()


def test_get_slack_credentials_route_rejects_without_permission(store):
    client = _make_client(store, permissions=[])

    resp = client.get("/api/projects/1/slack-credentials")

    assert resp.status_code == 403
    store.get_project_slack_token.assert_not_called()


# --- POST /api/projects/{id}/slack-credentials ------------------------------


def test_set_slack_credentials_route_accepts_valid_token(store):
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post("/api/projects/1/slack-credentials", json={"bot_token": " xoxb-abc123 "})

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"configured": True, "status": "authenticated"}
    assert "xoxb-abc123" not in resp.text
    store.set_project_slack_token.assert_called_once()
    args, _ = store.set_project_slack_token.call_args
    assert args[0] == 1
    assert args[1] == "xoxb-abc123"
    assert args[2].startswith("user:")


def test_set_slack_credentials_route_rejects_missing_token(store):
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post("/api/projects/1/slack-credentials", json={})

    assert resp.status_code == 400
    assert resp.json()["error"] == "bot_token is required"
    store.set_project_slack_token.assert_not_called()


def test_set_slack_credentials_route_rejects_whitespace_only_token(store):
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post("/api/projects/1/slack-credentials", json={"bot_token": "   "})

    assert resp.status_code == 400
    assert resp.json()["error"] == "bot_token is required"
    store.set_project_slack_token.assert_not_called()


def test_set_slack_credentials_route_rejects_unknown_project(store):
    store.get_project.return_value = None
    client = _make_client(store, permissions=["manage_webhooks"])

    resp = client.post("/api/projects/1/slack-credentials", json={"bot_token": "xoxb-abc123"})

    assert resp.status_code == 404
    store.set_project_slack_token.assert_not_called()


def test_set_slack_credentials_route_rejects_without_permission(store):
    client = _make_client(store, permissions=[])

    resp = client.post("/api/projects/1/slack-credentials", json={"bot_token": "xoxb-abc123"})

    assert resp.status_code == 403
    store.set_project_slack_token.assert_not_called()
