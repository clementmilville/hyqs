"""Regression tests for the unauthenticated POST /api/public/page-view beacon.

Builds the real Starlette app (hyqs.web.app.build_app) with the shared
real-Postgres ``store`` fixture, so hashing/rate-limit/GeoIP behavior is
checked end-to-end through starlette.testclient.TestClient, per the project
convention of never mocking the database.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

import hyqs.web.app as app_module
from hyqs.config import Config
from hyqs.web.app import build_app

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"


def _make_client(store, tmp_path, *, geoip_db=None) -> TestClient:
    config = Config(
        model="sonnet",
        permission_mode="acceptEdits",
        web_token="test-web-token",
        geoip_db=geoip_db if geoip_db is not None else tmp_path / "missing.mmdb",
    )
    app = build_app(config, MagicMock(), store)
    return TestClient(app)


def _unique_path() -> str:
    return f"/blog/{uuid.uuid4().hex}"


def _post(client, path, *, ip="203.0.113.10", ua=_UA, **body):
    payload = {"path": path, "ref": "", "lang": "en", "tz": "UTC"}
    payload.update(body)
    return client.post(
        "/api/public/page-view",
        json=payload,
        headers={"x-forwarded-for": ip, "user-agent": ua},
    )


@pytest.fixture(autouse=True)
def _reset_page_view_state():
    app_module._page_view_rate_limit_hits.clear()
    app_module._geoip_readers.clear()
    app_module._geoip_logged_paths.clear()
    yield
    app_module._page_view_rate_limit_hits.clear()
    app_module._geoip_readers.clear()
    app_module._geoip_logged_paths.clear()


def test_beacon_stores_row_with_country_none_when_no_geoip_db_configured(store, tmp_path):
    client = _make_client(store, tmp_path)
    path = _unique_path()

    resp = _post(client, path, ip="203.0.113.11")

    assert resp.status_code == 204
    rows = store.list_page_views(path=path)
    assert len(rows) == 1
    assert rows[0].country is None
    assert rows[0].region is None
    assert rows[0].city is None


def test_cached_payload_without_new_traits_still_stores(store, tmp_path):
    client = _make_client(store, tmp_path)
    path = _unique_path()

    response = _post(client, path)

    assert response.status_code == 204
    row = store.list_page_views(path=path)[0]
    assert row.win == ""
    assert row.color_scheme == ""


def test_stored_row_never_contains_raw_ip_or_exact_device_fingerprint(store, tmp_path):
    client = _make_client(store, tmp_path)
    # Include a dimension-like substring to prove unrelated user-controlled
    # text does not make the fingerprint-field assertion flaky.
    path = f"/blog/768-{uuid.uuid4().hex}"
    ip = "198.51.100.42"

    resp = _post(
        client,
        path,
        ip=ip,
        screen_w=1366,
        screen_h=768,
        win_w=1366,
        win_h=768,
        avail_w=1354,
        avail_h=728,
        dpr=2.0,
        hw=8,
    )

    assert resp.status_code == 204
    row = store.list_page_views(path=path)[0]
    string_fields = [
        row.path,
        row.ref,
        row.visitor_hash,
        row.country or "",
        row.region or "",
        row.city or "",
        row.referrer_host or "",
        row.ua_family or "",
        row.os_family or "",
        row.lang or "",
        row.tz or "",
        row.screen or "",
        row.win or "",
        row.viewport or "",
        row.device_memory or "",
        row.touch or "",
    ]
    assert not any(ip in field for field in string_fields)
    # Random identifiers such as `path` and `visitor_hash` can coincidentally
    # contain short digit substrings, so inspect only fields derived from the
    # submitted device dimensions for exact pixel values.
    fingerprint_fields = [
        row.screen or "",
        row.win or "",
        row.viewport or "",
        row.device_memory or "",
        row.touch or "",
    ]
    assert not any(
        exact in field for field in fingerprint_fields for exact in ("1366", "768", "1354", "728")
    )
    # `screen` is a coarse bucket (xs/sm/md/lg/xl), never the exact pixel size.
    assert row.screen in {"xs", "sm", "md", "lg", "xl"}


def test_window_dimensions_are_bucketed_for_hashing(store, tmp_path):
    client = _make_client(store, tmp_path)
    paths = [_unique_path() for _ in range(3)]

    _post(client, paths[0], win_w=1400, win_h=900)
    _post(client, paths[1], win_w=1000, win_h=900)
    _post(client, paths[2], win_w=1420, win_h=900)

    rows = [store.list_page_views(path=path)[0] for path in paths]
    assert rows[0].visitor_hash != rows[1].visitor_hash
    assert rows[0].visitor_hash == rows[2].visitor_hash
    assert rows[0].win == rows[2].win == "1400x900"
    assert rows[0].viewport == "lg"


@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        ("langs", "en,fr", "en,de"),
        ("os_version", "14.1", "15.0"),
        ("mem", 8, 16),
        ("touch", 0, 1),
    ],
)
def test_passive_trait_changes_visitor_hash(store, tmp_path, field, first, second):
    client = _make_client(store, tmp_path)
    path_a, path_b = _unique_path(), _unique_path()

    _post(client, path_a, **{field: first})
    _post(client, path_b, **{field: second})

    assert (
        store.list_page_views(path=path_a)[0].visitor_hash
        != store.list_page_views(path=path_b)[0].visitor_hash
    )


def test_color_scheme_is_stored_but_excluded_from_hash(store, tmp_path):
    client = _make_client(store, tmp_path)
    path_a, path_b = _unique_path(), _unique_path()

    _post(client, path_a, scheme="dark")
    _post(client, path_b, scheme="light")

    row_a = store.list_page_views(path=path_a)[0]
    row_b = store.list_page_views(path=path_b)[0]
    assert row_a.visitor_hash == row_b.visitor_hash
    assert (row_a.color_scheme, row_b.color_scheme) == ("dark", "light")


def test_same_ip_and_traits_same_day_yields_stable_hash_different_next_day(store, tmp_path):
    client = _make_client(store, tmp_path)
    ip = "192.0.2.5"

    app_module._page_view_rate_limit_hits.clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_module, "_today_utc_date_str", lambda: "2026-01-01")
        path_a = _unique_path()
        _post(client, path_a, ip=ip, screen_w=1920, screen_h=1080)
        path_b = _unique_path()
        _post(client, path_b, ip=ip, screen_w=1920, screen_h=1080)

    hash_a = store.list_page_views(path=path_a)[0].visitor_hash
    hash_b = store.list_page_views(path=path_b)[0].visitor_hash
    assert hash_a == hash_b

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_module, "_today_utc_date_str", lambda: "2026-01-02")
        path_c = _unique_path()
        _post(client, path_c, ip=ip, screen_w=1920, screen_h=1080)

    hash_c = store.list_page_views(path=path_c)[0].visitor_hash
    assert hash_c != hash_a


def test_same_ip_and_ua_different_screen_or_dpr_yields_different_hash(store, tmp_path):
    client = _make_client(store, tmp_path)
    ip = "192.0.2.6"

    path_a = _unique_path()
    _post(client, path_a, ip=ip, screen_w=1920, screen_h=1080, dpr=1.0)
    path_b = _unique_path()
    _post(client, path_b, ip=ip, screen_w=1024, screen_h=768, dpr=1.0)
    path_c = _unique_path()
    _post(client, path_c, ip=ip, screen_w=1920, screen_h=1080, dpr=2.0)

    hash_a = store.list_page_views(path=path_a)[0].visitor_hash
    hash_b = store.list_page_views(path=path_b)[0].visitor_hash
    hash_c = store.list_page_views(path=path_c)[0].visitor_hash
    assert hash_a != hash_b
    assert hash_a != hash_c


def test_bot_user_agent_is_dropped(store, tmp_path):
    client = _make_client(store, tmp_path)
    path = _unique_path()

    resp = _post(client, path, ip="203.0.113.20", ua="Googlebot/2.1 (+http://google.com/bot.html)")

    assert resp.status_code == 204
    assert store.list_page_views(path=path) == []


def test_api_prefixed_path_is_dropped(store, tmp_path):
    client = _make_client(store, tmp_path)
    path = "/api/whatever"

    resp = _post(client, path, ip="203.0.113.21")

    assert resp.status_code == 204
    assert store.list_page_views(path=path) == []


def test_dot_dot_path_is_dropped(store, tmp_path):
    client = _make_client(store, tmp_path)
    path = "/../secret"

    resp = _post(client, path, ip="203.0.113.22")

    assert resp.status_code == 204
    assert store.list_page_views(path=path) == []


def test_oversized_body_is_dropped(store, tmp_path):
    client = _make_client(store, tmp_path)
    path = _unique_path()

    resp = client.post(
        "/api/public/page-view",
        content=b'{"path": "%s", "pad": "%s"}' % (path.encode(), ("x" * 3000).encode()),
        headers={
            "x-forwarded-for": "203.0.113.23",
            "user-agent": _UA,
            "content-type": "application/json",
        },
    )

    assert resp.status_code == 204
    assert store.list_page_views(path=path) == []


def test_invalid_traits_normalize_to_empty_string_and_row_is_stored(store, tmp_path):
    client = _make_client(store, tmp_path)
    path = _unique_path()

    resp = _post(
        client,
        path,
        ip="203.0.113.24",
        screen_w=99999,
        dpr="not-a-number",
        hw=0,
        tz="x" * 200,
        lang="y" * 40,
        os_version="z" * 25,
        arch={"junk": True},
        platform="p" * 33,
        langs="l" * 65,
        win_w=199,
        win_h="junk",
        avail_w=10001,
        avail_h=False,
        mem=100,
        touch=21,
        scheme="sepia",
        hour_cycle="h25",
    )

    assert resp.status_code == 204
    row = store.list_page_views(path=path)[0]
    assert row.tz == ""
    assert row.lang == ""
    assert row.screen == ""
    assert row.os_version == ""
    assert row.arch == ""
    assert row.platform == ""
    assert row.langs == ""
    assert row.win == ""
    assert row.viewport == ""
    assert row.device_memory == ""
    assert row.touch == ""
    assert row.color_scheme == ""
    assert row.hour_cycle == ""


def test_malformed_json_body_still_returns_204(store, tmp_path):
    client = _make_client(store, tmp_path)

    resp = client.post(
        "/api/public/page-view",
        content=b"not json at all",
        headers={
            "x-forwarded-for": "203.0.113.25",
            "user-agent": _UA,
            "content-type": "application/json",
        },
    )

    assert resp.status_code == 204


def test_endpoint_reachable_without_any_auth_token(store, tmp_path):
    config = Config(
        model="sonnet",
        permission_mode="acceptEdits",
        web_token="some-token-required-everywhere-else",
        geoip_db=tmp_path / "missing.mmdb",
    )
    app = build_app(config, MagicMock(), store)
    client = TestClient(app)
    path = _unique_path()

    resp = client.post(
        "/api/public/page-view",
        json={"path": path},
        headers={"x-forwarded-for": "203.0.113.26", "user-agent": _UA},
    )

    assert resp.status_code == 204
    assert len(store.list_page_views(path=path)) == 1


def test_geoip_lookup_stub_populates_country_region_city(store, tmp_path, monkeypatch):
    client = _make_client(store, tmp_path)
    path = _unique_path()
    monkeypatch.setattr(
        app_module,
        "_geoip_lookup",
        lambda ip, db_path: ("France", "Ile-de-France", "Paris"),
    )

    resp = _post(client, path, ip="203.0.113.27")

    assert resp.status_code == 204
    row = store.list_page_views(path=path)[0]
    assert (row.country, row.region, row.city) == ("France", "Ile-de-France", "Paris")


def test_geoip_lookup_raising_degrades_to_none_and_still_stores_row(store, tmp_path, monkeypatch):
    client = _make_client(store, tmp_path)
    path = _unique_path()

    def _boom(ip, db_path):
        raise RuntimeError("reader exploded")

    monkeypatch.setattr(app_module, "_geoip_lookup", _boom)

    resp = _post(client, path, ip="203.0.113.28")

    assert resp.status_code == 204
    row = store.list_page_views(path=path)[0]
    assert (row.country, row.region, row.city) == (None, None, None)
