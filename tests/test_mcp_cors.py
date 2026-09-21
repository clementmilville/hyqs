"""Tests for the /mcp CORS preflight fix.

Builds the real Starlette app (hyqs.web.app.build_app) with a MagicMock store/
orchestrator so the OPTIONS-preflight wrapper and FastMCP's own auth gating can
be checked with starlette.testclient.TestClient, without a live Postgres
connection (mirrors tests/test_job_resolution_endpoints.py's _make_client).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.web.app import build_app

_TOKEN = "test-web-token"


def _make_client() -> TestClient:
    store = MagicMock()
    store.list_users.return_value = ["existing-admin"]
    store.get_role_permissions.return_value = {"platform_admin": []}
    config = Config(
        model="sonnet",
        permission_mode="acceptEdits",
        web_token=_TOKEN,
        # The MCP resource URL is derived from this when
        # HYQS_MCP_RESOURCE_URL is not set explicitly.
        web_base_url="https://hyqs.example.com",
    )
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    return TestClient(app)


def test_options_mcp_route_returns_200_with_cors_headers_unauthenticated():
    client = _make_client()

    resp = client.options("/mcp")

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "POST" in resp.headers["access-control-allow-methods"]
    assert "authorization" in resp.headers["access-control-allow-headers"]


def test_options_mcp_mount_subpath_returns_200_with_cors_headers_unauthenticated():
    client = _make_client()

    resp = client.options("/mcp/some-subpath")

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "POST" in resp.headers["access-control-allow-methods"]
    assert "authorization" in resp.headers["access-control-allow-headers"]


def test_options_on_unrouted_oauth_discovery_variant_returns_200_with_cors_headers():
    """Regression test: OAuth discovery has multiple valid well-known URL
    constructions (RFC 8414/9728); only some are registered as explicit routes.
    Any other path must still answer OPTIONS instead of falling through to the
    static-file/placeholder catch-all's plain-text 405.
    """
    client = _make_client()

    resp = client.options("/.well-known/oauth-authorization-server")

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"


def test_get_protected_resource_metadata_returns_valid_json():
    """Regression test: the RFC 9728 PRM route previously crashed with a 500
    (double-wrapped via request_response()) on real GET requests — only its
    OPTIONS preflight was covered by a test, which let the crash ship.
    """
    client = _make_client()

    resp = client.get("/.well-known/oauth-protected-resource/mcp")

    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == "https://hyqs.example.com/mcp"
    assert body["authorization_servers"] == ["https://hyqs.example.com/mcp"]


def test_get_authorization_server_metadata_at_rfc8414_canonical_path():
    """Regression test: real MCP clients (e.g. Claude Code) only probe the RFC
    8414 canonical discovery path (origin + well-known + resource path), never
    FastMCP's mount-relative one (/mcp/.well-known/oauth-authorization-server).
    Without this route the client 404s out of discovery entirely and falls
    back to guessing endpoints at the bare origin (e.g. POST /register, which
    405s and breaks the whole OAuth login flow).
    """
    client = _make_client()

    resp = client.get("/.well-known/oauth-authorization-server/mcp")

    assert resp.status_code == 200
    body = resp.json()
    assert body["issuer"] == "https://hyqs.example.com/mcp"
    assert body["registration_endpoint"] == "https://hyqs.example.com/mcp/register"
    assert body["authorization_endpoint"] == "https://hyqs.example.com/mcp/authorize"
    assert body["token_endpoint"] == "https://hyqs.example.com/mcp/token"


def test_get_openid_configuration_aliases_serve_same_metadata():
    client = _make_client()

    for path in (
        "/.well-known/openid-configuration/mcp",
        "/mcp/.well-known/openid-configuration",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.json()["issuer"] == "https://hyqs.example.com/mcp"


def test_post_mcp_without_auth_header_is_rejected():
    client = _make_client()

    resp = client.post("/mcp", json={})

    assert resp.status_code in (401, 403)


def test_get_mcp_without_auth_header_is_rejected():
    client = _make_client()

    resp = client.get("/mcp")

    assert resp.status_code in (401, 403)
