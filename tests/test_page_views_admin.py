"""Regression tests for GET /api/admin/page-views.

Builds the real Starlette app (hyqs.web.app.build_app) with the shared
real-Postgres ``store`` fixture and starlette.testclient.TestClient, per the
project convention of never mocking the database. Covers the view_audit
permission gate, per-day unique-visitor summation, since/until/path
filtering, and the recent sample's exclusion of visitor_hash.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.web.app import build_app

_WEB_TOKEN = "test-web-token"


def _make_client(store) -> TestClient:
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_WEB_TOKEN)
    app = build_app(config, MagicMock(), store)
    return TestClient(app)


def _make_authed_user(store, *, grant_view_audit: bool) -> tuple[int, str]:
    email = f"page-views-admin-{uuid.uuid4().hex}@test.com"
    user = store.create_user(email, "password123")
    if grant_view_audit:
        store.grant_platform_permission(user.id, "view_audit", "test-suite")
    token = store.create_session(user.id)
    return user.id, token


def _seed_page_view(
    store,
    *,
    path: str,
    visitor_hash: str,
    ts: datetime,
    ref: str = "",
    viewport: str = "",
    color_scheme: str = "",
    langs: str = "",
    os_version: str = "",
    os_family: str | None = None,
) -> int:
    row = store.record_page_view(
        path=path,
        ref=ref,
        visitor_hash=visitor_hash,
        country=None,
        region=None,
        city=None,
        referrer_host=None,
        ua_family=None,
        os_family=os_family,
        lang=langs or None,
        tz=None,
        screen=None,
        viewport=viewport,
        color_scheme=color_scheme,
        langs=langs,
        os_version=os_version,
    )
    with store._connection() as conn:
        conn.execute("UPDATE page_views SET ts = %s WHERE id = %s", (ts, row.id))
    return row.id


def test_admin_page_views_enforces_view_audit_permission(store):
    client = _make_client(store)
    unprivileged_id, unprivileged_token = _make_authed_user(store, grant_view_audit=False)
    privileged_id, privileged_token = _make_authed_user(store, grant_view_audit=True)
    try:
        forbidden = client.get(
            "/api/admin/page-views",
            headers={"authorization": f"Bearer {unprivileged_token}"},
        )
        assert forbidden.status_code == 403

        allowed = client.get(
            "/api/admin/page-views",
            headers={"authorization": f"Bearer {privileged_token}"},
        )
        assert allowed.status_code == 200
    finally:
        store.delete_user(unprivileged_id)
        store.delete_user(privileged_id)


def test_admin_page_views_sums_per_day_unique_visitors(store):
    client = _make_client(store)
    _user_id, token = _make_authed_user(store, grant_view_audit=True)
    path = f"/blog/{uuid.uuid4().hex}"
    hash_a = uuid.uuid4().hex
    hash_b = uuid.uuid4().hex
    hash_c = uuid.uuid4().hex
    day_one = datetime(2026, 2, 1, 10, tzinfo=timezone.utc)
    day_two = day_one + timedelta(days=1)
    row_ids = []
    try:
        # Day one: two distinct visitors (hash_a twice, hash_b once) -> 2 unique.
        row_ids.append(_seed_page_view(store, path=path, visitor_hash=hash_a, ts=day_one))
        row_ids.append(
            _seed_page_view(store, path=path, visitor_hash=hash_a, ts=day_one + timedelta(hours=1))
        )
        row_ids.append(_seed_page_view(store, path=path, visitor_hash=hash_b, ts=day_one))
        # Day two: hash_a reappears plus a new hash_c -> 2 unique that day too,
        # even though hash_a already counted on day one (summed, not deduped globally).
        row_ids.append(_seed_page_view(store, path=path, visitor_hash=hash_a, ts=day_two))
        row_ids.append(_seed_page_view(store, path=path, visitor_hash=hash_c, ts=day_two))

        resp = client.get(
            "/api/admin/page-views",
            params={
                "since": day_one.isoformat(),
                "until": (day_two + timedelta(hours=1)).isoformat(),
                "path": path,
            },
            headers={"authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["by_day"]) == 2
        assert [row["unique_visitors"] for row in body["by_day"]] == [2, 2]
        assert sum(row["unique_visitors"] for row in body["by_day"]) == 4
        assert body["totals"]["unique_visitors"] == 4
        assert body["totals"]["views"] == 5
    finally:
        with store._connection() as conn:
            conn.execute(
                "DELETE FROM page_views WHERE visitor_hash = ANY(%s)",
                ([hash_a, hash_b, hash_c],),
            )
        store.delete_user(_user_id)


def test_admin_page_views_filters_by_range_and_path(store):
    client = _make_client(store)
    _user_id, token = _make_authed_user(store, grant_view_audit=True)
    path = f"/docs/{uuid.uuid4().hex}"
    other_path = f"/pricing/{uuid.uuid4().hex}"
    hash_in = uuid.uuid4().hex
    hash_out_of_window = uuid.uuid4().hex
    hash_other_path = uuid.uuid4().hex
    since = datetime(2026, 3, 1, tzinfo=timezone.utc)
    until = datetime(2026, 3, 2, tzinfo=timezone.utc)
    in_window = since + timedelta(hours=5)
    before_window = since - timedelta(days=1)
    try:
        _seed_page_view(store, path=path, visitor_hash=hash_in, ts=in_window)
        _seed_page_view(store, path=path, visitor_hash=hash_out_of_window, ts=before_window)
        _seed_page_view(store, path=other_path, visitor_hash=hash_other_path, ts=in_window)

        resp = client.get(
            "/api/admin/page-views",
            params={"since": since.isoformat(), "until": until.isoformat(), "path": path},
            headers={"authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["totals"]["views"] == 1
        recent_paths = {row["path"] for row in body["recent"]}
        assert recent_paths == {path}
    finally:
        with store._connection() as conn:
            conn.execute(
                "DELETE FROM page_views WHERE visitor_hash = ANY(%s)",
                ([hash_in, hash_out_of_window, hash_other_path],),
            )
        store.delete_user(_user_id)


def test_admin_page_views_recent_excludes_visitor_hash(store):
    client = _make_client(store)
    _user_id, token = _make_authed_user(store, grant_view_audit=True)
    path = f"/docs/{uuid.uuid4().hex}"
    visitor_hash = uuid.uuid4().hex
    ts = datetime(2026, 4, 1, tzinfo=timezone.utc)
    try:
        _seed_page_view(store, path=path, visitor_hash=visitor_hash, ts=ts)

        resp = client.get(
            "/api/admin/page-views",
            params={
                "since": "2000-01-01T00:00:00+00:00",
                "until": "2100-01-01T00:00:00+00:00",
                "path": path,
            },
            headers={"authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        recent = resp.json()["recent"]
        assert recent
        for row in recent:
            assert "visitor_hash" not in row
    finally:
        with store._connection() as conn:
            conn.execute("DELETE FROM page_views WHERE visitor_hash = %s", (visitor_hash,))
        store.delete_user(_user_id)


def test_admin_page_views_returns_richer_reader_dimensions(store):
    client = _make_client(store)
    user_id, token = _make_authed_user(store, grant_view_audit=True)
    hash_a = uuid.uuid4().hex
    hash_b = uuid.uuid4().hex
    paths = [f"/guide/{uuid.uuid4().hex}/{index}" for index in range(3)]
    day_one = datetime(2026, 6, 1, 8, tzinfo=timezone.utc)
    day_two = day_one + timedelta(days=1)
    rows = [
        (paths[0], hash_a, day_one, "desktop", "dark", "en-US", "15", "macOS"),
        (paths[1], hash_a, day_one + timedelta(hours=1), "desktop", "dark", "en-US", "15", "macOS"),
        (paths[0], hash_b, day_one + timedelta(hours=2), "mobile", "", "fr-FR", "", "Android"),
        (paths[2], hash_a, day_two, "desktop", "light", "en-US", "15", "macOS"),
    ]
    try:
        for path, visitor_hash, ts, viewport, scheme, langs, version, family in rows:
            _seed_page_view(
                store,
                path=path,
                visitor_hash=visitor_hash,
                ts=ts,
                viewport=viewport,
                color_scheme=scheme,
                langs=langs,
                os_version=version,
                os_family=family,
            )

        response = client.get(
            "/api/admin/page-views",
            params={
                "since": day_one.isoformat(),
                "until": (day_two + timedelta(hours=1)).isoformat(),
            },
            headers={"authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["by_path"][0] == {
            "path": paths[0],
            "views": 2,
            "unique_visitors": 2,
        }
        assert {row["viewport"] for row in body["by_viewport"]} == {"desktop", "mobile"}
        assert "unknown" in {row["color_scheme"] for row in body["by_color_scheme"]}
        assert len(body["by_hour"]) == 24
        assert {row["pages"]: row["visitors"] for row in body["pages_per_visitor"]}[2] == 1
        assert body["returning"]["multi_day"] == 1
        assert body["totals"]["avg_pages_per_visitor"] == 1.33
        assert all("visitor_hash" not in row for row in body["recent"])
    finally:
        with store._connection() as conn:
            conn.execute("DELETE FROM page_views WHERE visitor_hash = ANY(%s)", ([hash_a, hash_b],))
        store.delete_user(user_id)
