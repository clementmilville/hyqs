"""Regression tests for the global MaxRequestBodySize ASGI middleware.

Builds the real Starlette app (hyqs.web.app.build_app) with the shared
real-Postgres ``store`` fixture, per the project convention of never mocking
the database, and drives it through starlette.testclient.TestClient.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.web.app import build_app


def _make_client(store, tmp_path, *, max_request_body_bytes: int) -> TestClient:
    config = Config(
        model="sonnet",
        permission_mode="acceptEdits",
        web_token="test-web-token",
        geoip_db=tmp_path / "missing.mmdb",
        max_request_body_bytes=max_request_body_bytes,
    )
    app = build_app(config, MagicMock(), store)
    return TestClient(app)


def test_oversized_content_length_rejected_with_413(store, tmp_path):
    client = _make_client(store, tmp_path, max_request_body_bytes=100)

    resp = client.post(
        "/api/public/page-view",
        content=b"x" * 200,
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 413


def test_normal_sized_request_unaffected(store, tmp_path):
    client = _make_client(store, tmp_path, max_request_body_bytes=25 * 1024 * 1024)

    resp = client.get("/api/health")

    assert resp.status_code == 200
