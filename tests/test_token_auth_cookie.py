"""Regression tests for TokenAuth's session-cookie fallback and the
restriction of ``?token=`` query-param auth to the GET SSE routes.

Builds the real Starlette app (hyqs.web.app.build_app) with the shared
real-Postgres ``store`` fixture, per the project convention of never mocking
the database, and drives it through starlette.testclient.TestClient.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.web import oauth
from hyqs.web.app import build_app

_PASSWORD = "correct-password"  # pragma: allowlist secret


def _make_client(store) -> TestClient:
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token="test-web-token")
    return TestClient(build_app(config, MagicMock(), store))


def _make_session(store) -> str:
    user = store.create_user(f"cookie-{uuid.uuid4()}@example.com", _PASSWORD)
    return store.create_session(user.id)


def test_cookie_only_request_authenticates_a_normal_route(store):
    client = _make_client(store)
    token = _make_session(store)
    client.cookies.set(oauth.SESSION_COOKIE_NAME, token)

    response = client.get("/api/projects")

    assert response.status_code == 200


def test_query_token_alone_is_rejected_on_a_non_sse_route(store):
    client = _make_client(store)
    token = _make_session(store)

    response = client.get("/api/projects", params={"token": token})

    assert response.status_code == 401


def test_query_token_still_authenticates_the_jobs_stream_sse_route(store):
    client = _make_client(store)
    token = _make_session(store)

    with patch("hyqs.web.app.EventSourceResponse", return_value=JSONResponse({"ok": True})):
        response = client.get("/api/jobs/stream", params={"token": token})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_bearer_header_still_authenticates_a_normal_route(store):
    client = _make_client(store)
    token = _make_session(store)

    response = client.get("/api/projects", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


def test_logout_with_cookie_deletes_session_and_clears_cookie(store):
    client = _make_client(store)
    token = _make_session(store)
    client.cookies.set(oauth.SESSION_COOKIE_NAME, token)

    response = client.post("/api/auth/logout")

    assert response.status_code == 204
    assert store.get_session(token) is None
    set_cookie_headers = response.headers.get_list("set-cookie")
    deletion_header = next(
        h for h in set_cookie_headers if h.startswith(f"{oauth.SESSION_COOKIE_NAME}=")
    )
    assert "max-age=0" in deletion_header.lower()
