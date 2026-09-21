"""Regression tests for the public site-statistics endpoint."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.pipeline.models import JobStatus
from hyqs.pipeline.store import JobStore
from hyqs.web import app as web_app

_WEB_TOKEN = "test-web-token"
_DOCUMENTED_KEYS = {
    "generated_at",
    "shipped_total",
    "shipped_last_7d",
    "shipped_last_24h",
    "minutes_since_last_ship",
    "avg_minutes_between_ships_last_7d",
    "first_job_at",
    "active_days",
    "humans",
    "projects",
    "gate_stops_total",
    "gate_stops_by_gate",
    "self_healed_shipped",
    "fix_rounds",
    "conflicts_resolved",
    "median_lead_minutes",
    "p25_lead_minutes",
    "off_hours_share",
    "cost_per_shipped_usd",
    "median_cost_shipped_usd",
    "running_now",
    "workers_online",
}


def _reset_public_site_stats_state() -> None:
    web_app._SITE_STATS_CACHE.update(data=None, expires_at=0.0)
    web_app._site_stats_rate_limit_hits.clear()


def _make_client(store) -> TestClient:
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_WEB_TOKEN)
    return TestClient(web_app.build_app(config, MagicMock(), store))


def _get_uncached(client: TestClient):
    _reset_public_site_stats_state()
    return client.get("/api/public/site-stats")


def _assert_numeric_tree(value) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _assert_numeric_tree(nested)
        return
    assert value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


def test_public_site_stats_allows_anonymous_access_and_returns_documented_keys(store):
    client = _make_client(store)
    try:
        response = _get_uncached(client)

        assert response.status_code == 200
        assert set(response.json()) == _DOCUMENTED_KEYS
    finally:
        _reset_public_site_stats_state()


def test_public_site_stats_returns_seeded_counts_and_excludes_operational_jobs(store):
    client = _make_client(store)
    repo_path = f"/tmp/public-site-stats-{uuid.uuid4().hex}"
    job_ids: list[int] = []
    project_id = None
    try:
        baseline = store.site_stats().to_dict()
        shipped_a = store.create(idea="eligible shipped a", repo_path=repo_path, chat_id=1)
        shipped_b = store.create(idea="eligible shipped b", repo_path=repo_path, chat_id=1)
        operational = store.create(
            idea="operational shipped",
            repo_path=repo_path,
            chat_id=1,
            source_actor="auto-deploy",
        )
        archived = store.create(idea="archived shipped", repo_path=repo_path, chat_id=1)
        job_ids = [shipped_a.id, shipped_b.id, operational.id, archived.id]
        project_id = shipped_a.project_id
        now = datetime.now(timezone.utc)
        with store._connection() as conn:
            for job, attempts in (
                (shipped_a, 0),
                (shipped_b, 1),
                (operational, 7),
                (archived, 5),
            ):
                conn.execute(
                    "UPDATE jobs SET status = %s, attempts = %s, created_at = %s, "
                    "updated_at = %s, archived = %s WHERE id = %s",
                    (
                        JobStatus.DONE.value,
                        attempts,
                        (now - timedelta(minutes=10)).isoformat(),
                        now.isoformat(),
                        job.id == archived.id,
                        job.id,
                    ),
                )
        store.add_event(shipped_b.id, "security", "failed")
        store.add_event(operational.id, "security", "failed")
        store.add_event(archived.id, "security", "failed")

        response = _get_uncached(client)

        assert response.status_code == 200
        body = response.json()
        assert body["shipped_total"] == baseline["shipped_total"] + 2
        assert body["shipped_last_7d"] == baseline["shipped_last_7d"] + 2
        assert body["shipped_last_24h"] == baseline["shipped_last_24h"] + 2
        assert body["self_healed_shipped"] == baseline["self_healed_shipped"] + 1
        assert body["gate_stops_total"] == baseline["gate_stops_total"] + 1
        assert (
            body["gate_stops_by_gate"]["security"] == baseline["gate_stops_by_gate"]["security"] + 1
        )
    finally:
        _reset_public_site_stats_state()
        if job_ids and project_id is not None:
            store.delete_project(project_id)


def test_public_site_stats_exposes_only_documented_timestamp_strings_and_numeric_values(store):
    client = _make_client(store)
    try:
        response = _get_uncached(client)

        assert response.status_code == 200
        body = response.json()
        assert isinstance(body["generated_at"], str)
        assert body["first_job_at"] is None or isinstance(body["first_job_at"], str)
        for key, value in body.items():
            if key not in {"generated_at", "first_job_at"}:
                _assert_numeric_tree(value)
    finally:
        _reset_public_site_stats_state()


def test_public_site_stats_reuses_cached_result_within_sixty_seconds(store, monkeypatch):
    calls = 0
    original = JobStore.site_stats

    def counting_site_stats(self):
        nonlocal calls
        calls += 1
        return original(self)

    monkeypatch.setattr(JobStore, "site_stats", counting_site_stats)
    client = _make_client(store)
    try:
        _reset_public_site_stats_state()
        first = client.get("/api/public/site-stats")
        second = client.get("/api/public/site-stats")

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json() == first.json()
        assert calls == 1
    finally:
        _reset_public_site_stats_state()
