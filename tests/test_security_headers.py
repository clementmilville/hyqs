"""Regression tests for the global SecurityHeaders ASGI middleware.

Builds the real Starlette app (hyqs.web.app.build_app) with the shared
real-Postgres ``store`` fixture, per the project convention of never mocking
the database, and drives it through starlette.testclient.TestClient.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from starlette.testclient import TestClient

import hyqs.web.app as web_app
from hyqs.config import Config
from hyqs.web.app import build_app

_EXPECTED_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
}


def _make_client(store, tmp_path) -> TestClient:
    config = Config(
        model="sonnet",
        permission_mode="acceptEdits",
        web_token="test-web-token",
        geoip_db=tmp_path / "missing.mmdb",
    )
    app = build_app(config, MagicMock(), store)
    return TestClient(app)


def test_api_response_carries_security_headers(store, tmp_path):
    client = _make_client(store, tmp_path)

    resp = client.get("/api/health")

    assert resp.status_code == 200
    for name, value in _EXPECTED_HEADERS.items():
        assert resp.headers[name] == value
    assert "default-src 'self'" in resp.headers["content-security-policy-report-only"]
    assert "content-security-policy" not in resp.headers


def test_unauthorized_response_carries_security_headers(store, tmp_path):
    client = _make_client(store, tmp_path)

    resp = client.get("/api/projects")

    assert resp.status_code == 401
    for name, value in _EXPECTED_HEADERS.items():
        assert resp.headers[name] == value
    assert "content-security-policy-report-only" in resp.headers


def test_static_asset_response_carries_security_headers(store, tmp_path, monkeypatch):
    frontend_dist = tmp_path / "dist"
    frontend_dist.mkdir()
    (frontend_dist / "index.html").write_text("<html></html>")
    monkeypatch.setattr(web_app, "FRONTEND_DIST", frontend_dist)

    client = _make_client(store, tmp_path)

    resp = client.get("/")

    assert resp.status_code == 200
    for name, value in _EXPECTED_HEADERS.items():
        assert resp.headers[name] == value
    assert "content-security-policy-report-only" in resp.headers


def test_get_started_redirects_to_public_install_chapter(tmp_path):
    client = _make_client(MagicMock(), tmp_path)

    for path in ("/get-started", "/get-started/"):
        resp = client.get(path, follow_redirects=False)

        assert resp.status_code == 308
        assert resp.headers["location"] == "/how-it-works/get-started/"
        for name, value in _EXPECTED_HEADERS.items():
            assert resp.headers[name] == value
