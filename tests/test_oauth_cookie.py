"""Regression tests for the OAuth callback's session-cookie handoff.

Builds the real Starlette app (hyqs.web.app.build_app) with the shared
real-Postgres ``store`` fixture, per the project convention of never mocking
the database. Only the outbound Google HTTP calls (token exchange + userinfo)
are mocked, per the project convention of mocking external I/O.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.web import oauth
from hyqs.web.app import build_app


def _make_client(store) -> TestClient:
    config = Config(
        model="sonnet",
        permission_mode="acceptEdits",
        web_token="test-web-token",
        google_client_id="test-google-client-id",
        google_client_secret="test-google-client-secret",  # pragma: allowlist secret
    )
    return TestClient(build_app(config, MagicMock(), store), base_url="https://testserver")


def _mock_response(status_code: int, json_body: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_body
    return response


def _session_cookie_header(response) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{oauth.SESSION_COOKIE_NAME}="):
            return header
    raise AssertionError(f"no {oauth.SESSION_COOKIE_NAME} Set-Cookie header found")


def test_google_callback_redirects_to_bare_root_with_no_token_in_url(store):
    email = f"oauth-{uuid.uuid4()}@example.com"
    store.create_invitation(email)
    client = _make_client(store)
    state = "test-state-value"

    token_resp = _mock_response(200, {"access_token": "g-access-token"})
    userinfo_resp = _mock_response(200, {"email": email, "name": "OAuth User"})

    client.cookies.set(oauth._STATE_COOKIE, state)
    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=token_resp)),
        patch("httpx.AsyncClient.get", new=AsyncMock(return_value=userinfo_resp)),
    ):
        response = client.get(
            "/api/auth/oauth/google/callback",
            params={"state": state, "code": "authcode"},
            follow_redirects=False,
        )

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/"


def test_google_callback_sets_httponly_secure_samesite_lax_session_cookie(store):
    email = f"oauth-{uuid.uuid4()}@example.com"
    store.create_invitation(email)
    client = _make_client(store)
    state = "test-state-value-2"

    token_resp = _mock_response(200, {"access_token": "g-access-token"})
    userinfo_resp = _mock_response(200, {"email": email, "name": "OAuth User"})

    client.cookies.set(oauth._STATE_COOKIE, state)
    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=token_resp)),
        patch("httpx.AsyncClient.get", new=AsyncMock(return_value=userinfo_resp)),
    ):
        response = client.get(
            "/api/auth/oauth/google/callback",
            params={"state": state, "code": "authcode"},
            follow_redirects=False,
        )

    header = _session_cookie_header(response)
    assert "httponly" in header.lower()
    assert "secure" in header.lower()
    assert "samesite=lax" in header.lower()
    assert f"max-age={oauth.SESSION_COOKIE_MAX_AGE}" in header.lower()


def test_google_callback_session_cookie_authenticates_a_subsequent_request(store):
    email = f"oauth-{uuid.uuid4()}@example.com"
    store.create_invitation(email)
    client = _make_client(store)
    state = "test-state-value-3"

    token_resp = _mock_response(200, {"access_token": "g-access-token"})
    userinfo_resp = _mock_response(200, {"email": email, "name": "OAuth User"})

    client.cookies.set(oauth._STATE_COOKIE, state)
    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=token_resp)),
        patch("httpx.AsyncClient.get", new=AsyncMock(return_value=userinfo_resp)),
    ):
        client.get(
            "/api/auth/oauth/google/callback",
            params={"state": state, "code": "authcode"},
            follow_redirects=False,
        )

    session_token = client.cookies.get(oauth.SESSION_COOKIE_NAME)
    assert session_token

    response = client.get("/api/projects")

    assert response.status_code == 200
