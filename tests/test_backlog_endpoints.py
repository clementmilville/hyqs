"""Tests for the backlog web endpoints (create/patch/list).

Builds the real Starlette app (hyqs.web.app.build_app) with a MagicMock store
so each route's validation and store wiring can be checked with
starlette.testclient.TestClient, without a live Postgres connection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.pipeline.models import BacklogItem, BacklogItemStatus, BacklogItemType
from hyqs.web.app import build_app

_TOKEN = "test-web-token"


def _make_item(**overrides) -> BacklogItem:
    defaults = dict(
        id=42,
        project_id=1,
        title="Do the thing",
        body="",
        type=BacklogItemType.IDEA,
        proposed_by="member@example.com",
        status=BacklogItemStatus.NEW,
    )
    defaults.update(overrides)
    return BacklogItem(**defaults)


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
    return s


def test_create_backlog_item_route_rejects_invalid_type(store):
    client = _make_client(store, permissions=["propose_backlog"])

    resp = client.post("/api/projects/1/backlog", json={"title": "Do the thing", "type": "task"})

    assert resp.status_code == 400
    assert "error" in resp.json()
    store.create_backlog_item.assert_not_called()


def test_create_backlog_item_route_accepts_valid_type(store):
    store.create_backlog_item.return_value = _make_item()
    client = _make_client(store, permissions=["propose_backlog"])

    resp = client.post("/api/projects/1/backlog", json={"title": "Do the thing", "type": "idea"})

    assert resp.status_code == 201
    store.create_backlog_item.assert_called_once()


def test_patch_backlog_item_route_rejects_invalid_status(store):
    store.get_backlog_item.return_value = _make_item()
    client = _make_client(store, permissions=["triage_backlog"])

    resp = client.patch("/api/backlog/42", json={"status": "bogus"})

    assert resp.status_code == 400
    store.patch_backlog_item.assert_not_called()


def test_patch_backlog_item_route_allows_propose_backlog_to_mark_completed(store):
    store.get_backlog_item.return_value = _make_item()
    client = _make_client(store, permissions=["propose_backlog"])

    resp = client.patch("/api/backlog/42", json={"status": "completed"})

    assert resp.status_code == 200
    store.patch_backlog_item.assert_called_once_with(42, status="completed")


def test_patch_backlog_item_route_rejects_propose_backlog_setting_accepted(store):
    store.get_backlog_item.return_value = _make_item()
    client = _make_client(store, permissions=["propose_backlog"])

    resp = client.patch("/api/backlog/42", json={"status": "accepted"})

    assert resp.status_code == 403
    store.patch_backlog_item.assert_not_called()


def test_patch_backlog_item_route_rejects_propose_backlog_setting_declined(store):
    store.get_backlog_item.return_value = _make_item()
    client = _make_client(store, permissions=["propose_backlog"])

    resp = client.patch("/api/backlog/42", json={"status": "declined"})

    assert resp.status_code == 403
    store.patch_backlog_item.assert_not_called()


def test_patch_backlog_item_route_rejects_propose_backlog_setting_epic_hint(store):
    store.get_backlog_item.return_value = _make_item()
    client = _make_client(store, permissions=["propose_backlog"])

    resp = client.patch("/api/backlog/42", json={"epic_hint": "x"})

    assert resp.status_code == 403
    store.patch_backlog_item.assert_not_called()


def test_patch_backlog_item_route_rejects_propose_backlog_smuggling_epic_hint_with_completed(store):
    store.get_backlog_item.return_value = _make_item()
    client = _make_client(store, permissions=["propose_backlog"])

    resp = client.patch("/api/backlog/42", json={"status": "completed", "epic_hint": "x"})

    assert resp.status_code == 403
    store.patch_backlog_item.assert_not_called()


def test_patch_backlog_item_route_triage_backlog_can_still_accept_and_decline(store):
    store.get_backlog_item.return_value = _make_item()
    # Every real role granting triage_backlog also grants propose_backlog (see
    # store._SEED_PERMISSIONS), so exercise both together like a real caller.
    client = _make_client(store, permissions=["triage_backlog", "propose_backlog"])

    resp = client.patch("/api/backlog/42", json={"status": "accepted"})
    assert resp.status_code == 200

    resp = client.patch("/api/backlog/42", json={"status": "declined"})
    assert resp.status_code == 200

    resp = client.patch("/api/backlog/42", json={"status": "completed"})
    assert resp.status_code == 200

    resp = client.patch("/api/backlog/42", json={"epic_hint": "x"})
    assert resp.status_code == 200


@pytest.mark.parametrize("terminal_status", ["declined", "completed", "converted"])
@pytest.mark.parametrize("permissions", [["triage_backlog"], ["propose_backlog"]])
def test_patch_backlog_item_route_rejects_status_edit_on_terminal_item(
    store, terminal_status, permissions
):
    store.get_backlog_item.return_value = _make_item(status=BacklogItemStatus(terminal_status))
    client = _make_client(store, permissions=permissions)

    resp = client.patch("/api/backlog/42", json={"status": "accepted"})

    assert resp.status_code == 409
    body = resp.json()
    assert "42" in body["error"]
    assert terminal_status in body["error"]
    store.patch_backlog_item.assert_not_called()


@pytest.mark.parametrize("terminal_status", ["declined", "completed", "converted"])
def test_patch_backlog_item_route_rejects_epic_hint_edit_on_terminal_item(store, terminal_status):
    store.get_backlog_item.return_value = _make_item(status=BacklogItemStatus(terminal_status))
    client = _make_client(store, permissions=["triage_backlog"])

    resp = client.patch("/api/backlog/42", json={"epic_hint": "x"})

    assert resp.status_code == 409
    store.patch_backlog_item.assert_not_called()


def test_patch_backlog_item_route_allows_non_status_edit_on_terminal_item(store):
    store.get_backlog_item.return_value = _make_item(status=BacklogItemStatus.COMPLETED)
    client = _make_client(store, permissions=["triage_backlog"])

    resp = client.patch("/api/backlog/42", json={})

    assert resp.status_code == 200
    store.patch_backlog_item.assert_called_once_with(42)


def test_refine_backlog_route_rejects_terminal_item(store):
    store.get_backlog_item.return_value = _make_item(status=BacklogItemStatus.DECLINED)
    with patch("hyqs.web.app.build_backend", return_value=MagicMock()):
        client = _make_client(store, permissions=["queue_job"])

    resp = client.post("/api/projects/1/backlog/refine", json={"item_ids": [42]})

    assert resp.status_code == 400
    assert "42" in resp.json()["error"]
    store.create_chat_session.assert_not_called()


def test_refine_backlog_route_allows_non_terminal_items(store):
    store.get_backlog_item.return_value = _make_item(status=BacklogItemStatus.NEW)
    store.get_project.return_value = MagicMock(repo_path="/tmp/repo")
    store.list_epics.return_value = []
    store.create_chat_session.return_value = MagicMock(session_id="sess-1")
    mock_backend = MagicMock()
    mock_backend.run = AsyncMock(return_value=MagicMock(text=""))
    with patch("hyqs.web.app.build_backend", return_value=mock_backend):
        client = _make_client(store, permissions=["queue_job"])

    resp = client.post("/api/projects/1/backlog/refine", json={"item_ids": [42]})

    assert resp.status_code == 200
    store.create_chat_session.assert_called_once()


def test_list_backlog_route_returns_items(store):
    store.list_backlog_items.return_value = [_make_item()]
    client = _make_client(store, permissions=[])

    resp = client.get("/api/projects/1/backlog")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["id"] for i in items] == [42]
