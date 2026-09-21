"""Regression tests for job #4316: the web console's orchestrator session must
be keyed on the authenticated caller instead of the shared ``WEB_CHAT_ID``
constant, so distinct users never share one Claude agent session/conversation
history. ``jobs.chat_id`` (job-row provenance) is a separate concern and must
keep recording ``WEB_CHAT_ID`` regardless of who created the job.

Follows the established pattern from tests/test_security_authz_manual_fixes.py:
a per-test MagicMock store, build_app(config, orchestrator, store) wrapped in
starlette.testclient.TestClient.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.pipeline.models import User
from hyqs.web.app import _PLATFORM_TOKEN_SESSION_ID, WEB_CHAT_ID, build_app

_WEB_TOKEN = "test-web-token"  # pragma: allowlist secret
_USER_A_TOKEN = "session-token-a"  # pragma: allowlist secret
_USER_B_TOKEN = "session-token-b"  # pragma: allowlist secret
_USER_A_ID = 101
_USER_B_ID = 202


def _make_user(user_id: int) -> User:
    return User(id=user_id, email=f"user{user_id}@example.com", is_platform_admin=True)


def _build_store() -> MagicMock:
    store = MagicMock()
    sessions = {_USER_A_TOKEN: _make_user(_USER_A_ID), _USER_B_TOKEN: _make_user(_USER_B_ID)}
    store.get_session.side_effect = lambda token: sessions.get(token)
    store.get_role_permissions.return_value = {"platform_admin": ["admin_console", "queue_job"]}
    # Job-creation side effects irrelevant to this job's fix: stubbed just
    # enough that the handlers reach their store.create/store.create_batch call.
    store.find_recent_duplicate.return_value = None
    store.get_epic.return_value = None
    store.get_unsatisfied_deps.return_value = []
    store.get_scheduler_wait.return_value = None
    store.get_effective_priority.return_value = (0, [])
    return store


def _make_orchestrator() -> MagicMock:
    orchestrator = MagicMock()
    orchestrator.ask = AsyncMock(return_value="hi")

    async def _stream_ask(chat_id, text):
        yield {"type": "result", "text": "hi"}

    orchestrator.stream_ask = MagicMock(side_effect=_stream_ask)
    return orchestrator


def _build_client(store: MagicMock, orchestrator: MagicMock) -> TestClient:
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_WEB_TOKEN)
    app = build_app(config, orchestrator, store)
    return TestClient(app, raise_server_exceptions=False)


def test_chat_uses_distinct_session_per_principal():
    store = _build_store()
    orchestrator = _make_orchestrator()
    client = _build_client(store, orchestrator)

    client.headers.update({"Authorization": f"Bearer {_USER_A_TOKEN}"})
    resp_a = client.post("/api/chat", json={"text": "hi"})
    client.headers.update({"Authorization": f"Bearer {_USER_B_TOKEN}"})
    resp_b = client.post("/api/chat", json={"text": "hi"})
    client.headers.update({"Authorization": f"Bearer {_WEB_TOKEN}"})
    resp_platform = client.post("/api/chat", json={"text": "hi"})

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert resp_platform.status_code == 200
    session_ids = [call.args[0] for call in orchestrator.ask.call_args_list]
    assert session_ids == [_USER_A_ID, _USER_B_ID, _PLATFORM_TOKEN_SESSION_ID]
    assert len(set(session_ids)) == 3


def test_chat_stream_uses_distinct_session_per_principal():
    store = _build_store()
    orchestrator = _make_orchestrator()
    client = _build_client(store, orchestrator)

    client.headers.update({"Authorization": f"Bearer {_USER_A_TOKEN}"})
    resp_a = client.post("/api/chat/stream", json={"text": "hi"})
    client.headers.update({"Authorization": f"Bearer {_USER_B_TOKEN}"})
    resp_b = client.post("/api/chat/stream", json={"text": "hi"})

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    session_ids = [call.args[0] for call in orchestrator.stream_ask.call_args_list]
    assert session_ids == [_USER_A_ID, _USER_B_ID]


def test_create_job_still_records_web_chat_id_regardless_of_caller():
    store = _build_store()
    orchestrator = _make_orchestrator()
    client = _build_client(store, orchestrator)

    for token in (_USER_A_TOKEN, _USER_B_TOKEN):
        client.headers.update({"Authorization": f"Bearer {token}"})
        client.post("/api/jobs", json={"repo": "acme/repo", "idea": "do the thing", "epic_id": 1})

    assert store.create.call_count == 2
    for call in store.create.call_args_list:
        assert call.kwargs["chat_id"] == WEB_CHAT_ID


def test_create_job_batch_still_records_web_chat_id_regardless_of_caller():
    store = _build_store()
    orchestrator = _make_orchestrator()
    client = _build_client(store, orchestrator)

    for token in (_USER_A_TOKEN, _USER_B_TOKEN):
        client.headers.update({"Authorization": f"Bearer {token}"})
        client.post(
            "/api/jobs/batch",
            json={"repo": "acme/repo", "jobs": [{"title": "Do the thing", "epic_id": 1}]},
        )

    assert store.create_batch.call_count == 2
    for call in store.create_batch.call_args_list:
        assert call.kwargs["chat_id"] == WEB_CHAT_ID
