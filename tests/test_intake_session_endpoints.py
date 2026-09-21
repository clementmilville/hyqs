"""Regression tests for `missing_required` on serialized intake sessions (job #4332).

`check_completeness` in hyqs/intake/gate.py is the single source of truth for
which required draft-spec fields are still outstanding. Previously that list
was computed and then discarded in two routes, so no session response ever
carried it. `intake_session_to_dict` now populates `missing_required` from
`check_completeness`, and every route that serializes a session (create,
list, get, non-streaming message, draft-spec patch) exposes it for free.

Follows the established pattern from tests/test_project_create_route.py: a
per-test MagicMock store, build_app(config, orchestrator, store) wrapped in
starlette.testclient.TestClient, and a platform-admin session so ownership
checks (`session.created_by != caller_id`) are bypassed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.intake.gate import _REQUIRED, check_completeness
from hyqs.intake.models import IntakeSession
from hyqs.pipeline.models import User
from hyqs.web.app import build_app, intake_session_to_dict

_TOKEN = "test-web-token"

_INCOMPLETE_SPEC = {"name": "Demo"}

_COMPLETE_SPEC = {
    "name": "Demo",
    "slug": "demo",
    "one_liner": "A demo app",
    "problem": "Demoing things is hard",
    "users": "Demo users",
    "auth": "none",
    "features": ["feature one"],
    "data_model": "one table",
    "stack": "python",
}


def _make_session(**overrides) -> IntakeSession:
    defaults = dict(
        id=1,
        session_id="sess-1",
        created_by="1",
        draft_spec=dict(_INCOMPLETE_SPEC),
        messages=[],
        status="in_progress",
        project_id=None,
        created_at="2026-09-01T00:00:00Z",
        updated_at="2026-09-01T00:00:00Z",
    )
    defaults.update(overrides)
    return IntakeSession(**defaults)


def _make_client(store: MagicMock, tmp_path) -> TestClient:
    store.get_session.return_value = User(id=1, email="admin@example.com", is_platform_admin=True)
    store.get_role_permissions.return_value = {"platform_admin": ["create_project"]}
    config = Config(
        model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN, projects_dir=tmp_path
    )
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})
    return client


@pytest.fixture
def store() -> MagicMock:
    return MagicMock()


# --- serializer -------------------------------------------------------------


def test_intake_session_to_dict_reports_missing_required_for_incomplete_spec():
    session = _make_session(draft_spec=dict(_INCOMPLETE_SPEC))

    result = intake_session_to_dict(session)

    assert result["missing_required"] == check_completeness(_INCOMPLETE_SPEC)
    assert result["missing_required"]
    assert set(result["missing_required"]) <= set(_REQUIRED)


def test_intake_session_to_dict_reports_empty_missing_required_for_complete_spec():
    session = _make_session(draft_spec=dict(_COMPLETE_SPEC))

    result = intake_session_to_dict(session)

    assert result["missing_required"] == []


# --- routes -------------------------------------------------------------


def test_create_intake_session_route_exposes_missing_required(store, tmp_path):
    session = _make_session(draft_spec={})
    store.create_intake_session.return_value = session
    client = _make_client(store, tmp_path)

    resp = client.post("/api/intake/sessions")

    assert resp.status_code == 201
    assert resp.json()["session"]["missing_required"] == check_completeness({})


def test_list_intake_sessions_route_exposes_missing_required(store, tmp_path):
    session = _make_session(draft_spec=dict(_COMPLETE_SPEC))
    store.list_intake_sessions.return_value = [session]
    client = _make_client(store, tmp_path)

    resp = client.get("/api/intake/sessions")

    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["missing_required"] == []


def test_get_intake_session_route_exposes_missing_required(store, tmp_path):
    session = _make_session(draft_spec=dict(_INCOMPLETE_SPEC))
    store.get_intake_session.return_value = session
    client = _make_client(store, tmp_path)

    resp = client.get(f"/api/intake/sessions/{session.session_id}")

    assert resp.status_code == 200
    assert resp.json()["session"]["missing_required"] == check_completeness(_INCOMPLETE_SPEC)


def test_intake_message_route_response_includes_session_with_missing_required(store, tmp_path):
    session = _make_session(draft_spec=dict(_INCOMPLETE_SPEC))
    store.get_intake_session.return_value = session
    client = _make_client(store, tmp_path)

    with patch(
        "hyqs.web.app.interview_turn",
        new=AsyncMock(return_value=("reply text", dict(_COMPLETE_SPEC), "confirm", True)),
    ):
        resp = client.post(
            f"/api/intake/sessions/{session.session_id}/message",
            json={"message": "hello"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "session" in body
    assert body["session"]["missing_required"] == check_completeness(session.draft_spec)
    store.update_intake_session.assert_called_once()


def test_patch_intake_draft_spec_route_exposes_missing_required(store, tmp_path):
    session = _make_session(draft_spec=dict(_INCOMPLETE_SPEC))
    store.get_intake_session.return_value = session
    client = _make_client(store, tmp_path)

    resp = client.patch(
        f"/api/intake/sessions/{session.session_id}/draft_spec",
        json={"draft_spec": {"slug": "demo"}, "base_updated_at": session.updated_at},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "missing" in body
    assert body["session"]["missing_required"] == check_completeness(session.draft_spec)
