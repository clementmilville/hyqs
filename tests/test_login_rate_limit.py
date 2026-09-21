"""Regression tests for POST /api/auth/login's progressive rate limiting.

Unit-level tests exercise ``_resolve_source_ip``/``_login_throttled`` directly
(no I/O needed). Integration-level tests build the real Starlette app
(``hyqs.web.app.build_app``) with the shared real-Postgres ``store`` fixture
and drive the endpoint through ``starlette.testclient.TestClient``, per the
project convention of never mocking the database.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

import hyqs.web.app as app_module
from hyqs.config import Config
from hyqs.web.app import build_app

_TIER_ONE_WINDOW_SECONDS, _TIER_ONE_MAX = app_module._LOGIN_RATE_LIMIT_TIERS[0]
_TIER_TWO_WINDOW_SECONDS, _TIER_TWO_MAX = app_module._LOGIN_RATE_LIMIT_TIERS[1]

_WRONG_PASSWORD = "wrong"  # pragma: allowlist secret
_CORRECT_PASSWORD = "correct-password"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _reset_login_rate_limit_state():
    app_module._login_rate_limit_hits_by_ip.clear()
    app_module._login_rate_limit_hits_by_email.clear()
    yield
    app_module._login_rate_limit_hits_by_ip.clear()
    app_module._login_rate_limit_hits_by_email.clear()


# --- _resolve_source_ip -----------------------------------------------------


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, headers=None, client_host="198.51.100.9"):
        self.headers = dict(headers or {})
        self.client = _FakeClient(client_host) if client_host is not None else None


def test_resolve_source_ip_trusts_single_hop_at_depth_one():
    request = _FakeRequest(headers={"x-forwarded-for": "203.0.113.5"})

    assert app_module._resolve_source_ip(request, 1) == "203.0.113.5"


def test_resolve_source_ip_counts_depth_from_the_right():
    request = _FakeRequest(headers={"x-forwarded-for": "203.0.113.5, 10.0.0.1"})

    assert app_module._resolve_source_ip(request, 1) == "10.0.0.1"
    assert app_module._resolve_source_ip(request, 2) == "203.0.113.5"


def test_resolve_source_ip_falls_back_when_header_absent():
    request = _FakeRequest(headers={}, client_host="198.51.100.9")

    assert app_module._resolve_source_ip(request, 1) == "198.51.100.9"


def test_resolve_source_ip_falls_back_when_fewer_hops_than_depth():
    request = _FakeRequest(headers={"x-forwarded-for": "203.0.113.5"}, client_host="198.51.100.9")

    assert app_module._resolve_source_ip(request, 2) == "198.51.100.9"


def test_resolve_source_ip_falls_back_when_depth_not_positive():
    request = _FakeRequest(headers={"x-forwarded-for": "203.0.113.5"}, client_host="198.51.100.9")

    assert app_module._resolve_source_ip(request, 0) == "198.51.100.9"


# --- _login_throttled --------------------------------------------------------


def test_login_throttled_allows_up_to_tier_one_cap_then_blocks():
    hits: "app_module.collections.OrderedDict[str, list[float]]" = (
        app_module.collections.OrderedDict()
    )
    key = "probe@example.com"

    for i in range(_TIER_ONE_MAX):
        assert app_module._login_throttled(hits, key, float(i)) is None

    retry_after = app_module._login_throttled(hits, key, float(_TIER_ONE_MAX))
    assert retry_after == _TIER_ONE_WINDOW_SECONDS


def test_login_throttled_blocked_attempt_is_not_recorded():
    hits: "app_module.collections.OrderedDict[str, list[float]]" = (
        app_module.collections.OrderedDict()
    )
    key = "probe@example.com"
    for i in range(_TIER_ONE_MAX):
        app_module._login_throttled(hits, key, float(i))
    before = list(hits[key])

    app_module._login_throttled(hits, key, float(_TIER_ONE_MAX))

    assert hits[key] == before


def test_login_throttled_tier_two_trips_after_cumulative_cap_across_windows():
    hits: "app_module.collections.OrderedDict[str, list[float]]" = (
        app_module.collections.OrderedDict()
    )
    key = "probe@example.com"
    now = 0.0
    total_recorded = 0
    # Space bursts more than the tier-one window apart so tier one never
    # trips, but stay well inside the tier-two window so its budget drains.
    while total_recorded < _TIER_TWO_MAX:
        burst = min(_TIER_ONE_MAX, _TIER_TWO_MAX - total_recorded)
        for i in range(burst):
            assert app_module._login_throttled(hits, key, now + i) is None
            total_recorded += 1
        now += _TIER_ONE_WINDOW_SECONDS + 1

    assert now < _TIER_TWO_WINDOW_SECONDS
    retry_after = app_module._login_throttled(hits, key, now)
    assert retry_after == _TIER_TWO_WINDOW_SECONDS


def test_login_throttled_independent_keys_do_not_share_budget():
    hits: "app_module.collections.OrderedDict[str, list[float]]" = (
        app_module.collections.OrderedDict()
    )
    for i in range(_TIER_ONE_MAX):
        app_module._login_throttled(hits, "a@example.com", float(i))

    assert app_module._login_throttled(hits, "a@example.com", float(_TIER_ONE_MAX)) is not None
    assert app_module._login_throttled(hits, "b@example.com", float(_TIER_ONE_MAX)) is None


# --- POST /api/auth/login ----------------------------------------------------

_WEB_TOKEN = "test-web-token"


def _make_client(store) -> TestClient:
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_WEB_TOKEN)
    return TestClient(build_app(config, MagicMock(), store))


def _login(client, *, email, password, ip):
    return client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
        headers={"x-forwarded-for": ip},
    )


def test_login_rejects_bad_credentials_without_throttling(store):
    client = _make_client(store)

    response = _login(
        client, email="nobody@example.com", password=_WRONG_PASSWORD, ip="203.0.113.20"
    )

    assert response.status_code == 401


def test_login_success_returns_200_with_token(store):
    client = _make_client(store)
    email = f"success-{uuid.uuid4()}@example.com"
    store.create_user(email, _CORRECT_PASSWORD, display_name="Success User")

    response = _login(client, email=email, password=_CORRECT_PASSWORD, ip="203.0.113.60")

    assert response.status_code == 200
    body = response.json()
    assert body["token"]
    assert body["user"]["email"] == email
    assert body["user"]["display_name"] == "Success User"


def test_login_throttles_by_ip_after_tier_one_cap(store):
    client = _make_client(store)
    ip = "203.0.113.21"

    for _ in range(_TIER_ONE_MAX):
        response = _login(
            client, email=f"{uuid.uuid4()}@example.com", password=_WRONG_PASSWORD, ip=ip
        )
        assert response.status_code == 401

    response = _login(client, email=f"{uuid.uuid4()}@example.com", password=_WRONG_PASSWORD, ip=ip)

    assert response.status_code == 429
    assert response.headers.get("retry-after") == str(int(_TIER_ONE_WINDOW_SECONDS))


def test_login_ip_throttle_keys_on_rightmost_trusted_hop_not_spoofed_leftmost(store):
    client = _make_client(store)
    trusted_hop = "203.0.113.55"

    for i in range(_TIER_ONE_MAX):
        response = client.post(
            "/api/auth/login",
            json={"email": f"{uuid.uuid4()}@example.com", "password": _WRONG_PASSWORD},
            headers={"x-forwarded-for": f"10.0.0.{i}, {trusted_hop}"},
        )
        assert response.status_code == 401

    response = client.post(
        "/api/auth/login",
        json={"email": f"{uuid.uuid4()}@example.com", "password": _WRONG_PASSWORD},
        headers={"x-forwarded-for": f"198.51.100.99, {trusted_hop}"},
    )

    assert response.status_code == 429
    assert response.headers.get("retry-after") == str(int(_TIER_ONE_WINDOW_SECONDS))


def test_login_ip_throttle_does_not_affect_a_different_ip(store):
    client = _make_client(store)
    throttled_ip = "203.0.113.22"
    other_ip = "203.0.113.23"

    for _ in range(_TIER_ONE_MAX):
        _login(
            client, email=f"{uuid.uuid4()}@example.com", password=_WRONG_PASSWORD, ip=throttled_ip
        )
    blocked = _login(
        client, email=f"{uuid.uuid4()}@example.com", password=_WRONG_PASSWORD, ip=throttled_ip
    )
    unaffected = _login(
        client, email=f"{uuid.uuid4()}@example.com", password=_WRONG_PASSWORD, ip=other_ip
    )

    assert blocked.status_code == 429
    assert unaffected.status_code == 401


def test_login_throttles_by_normalized_email_across_different_ips(store):
    client = _make_client(store)
    email = f"Case-{uuid.uuid4()}@Example.com"

    for i in range(_TIER_ONE_MAX):
        response = _login(client, email=email, password=_WRONG_PASSWORD, ip=f"203.0.113.{30 + i}")
        assert response.status_code == 401

    response = _login(client, email=email.lower(), password=_WRONG_PASSWORD, ip="203.0.113.99")

    assert response.status_code == 429
    assert response.headers.get("retry-after") == str(int(_TIER_ONE_WINDOW_SECONDS))


def test_login_throttled_request_never_calls_authenticate_user(store, monkeypatch):
    client = _make_client(store)
    ip = "203.0.113.40"
    email = store.create_user(f"real-{uuid.uuid4()}@example.com", _CORRECT_PASSWORD).email
    calls = []
    original = type(store).authenticate_user

    def counting_authenticate_user(self, email_arg, password_arg):
        calls.append((email_arg, password_arg))
        return original(self, email_arg, password_arg)

    monkeypatch.setattr(type(store), "authenticate_user", counting_authenticate_user)

    for _ in range(_TIER_ONE_MAX):
        _login(client, email=f"{uuid.uuid4()}@example.com", password=_WRONG_PASSWORD, ip=ip)
    assert len(calls) == _TIER_ONE_MAX

    response = _login(client, email=email, password=_CORRECT_PASSWORD, ip=ip)

    assert response.status_code == 429
    assert len(calls) == _TIER_ONE_MAX
