"""Tests for the deploys ledger (record_deploy + get_last_deploy)."""

import uuid

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(store) -> int:
    path = f"/tmp/test-deploys-{uuid.uuid4()}"
    p = store.create_project(f"test-deploys-{uuid.uuid4()}", path)
    return p.id


def _cleanup(store, project_id: int) -> None:
    store.delete_project(project_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_last_deploy_returns_none_when_no_deploys(store):
    pid = _make_project(store)
    try:
        assert store.get_last_deploy(pid) is None
    finally:
        _cleanup(store, pid)


def test_record_deploy_and_get_last_deploy_roundtrip(store):
    pid = _make_project(store)
    try:
        store.record_deploy(pid, "abc123", None, "pipeline_job")
        result = store.get_last_deploy(pid)
        assert result is not None
        assert result["deployed_commit"] == "abc123"
        assert "finished_at" in result
    finally:
        _cleanup(store, pid)


def test_get_last_deploy_returns_dict_shape(store):
    pid = _make_project(store)
    try:
        store.record_deploy(pid, "def456", None, "unknown")
        result = store.get_last_deploy(pid)
        assert set(result.keys()) == {"deployed_commit", "finished_at"}
    finally:
        _cleanup(store, pid)


def test_get_last_deploy_returns_most_recent(store):
    pid = _make_project(store)
    try:
        store.record_deploy(pid, "sha-first", None, "pipeline_job")
        store.record_deploy(pid, "sha-second", "sha-first", "pipeline_job")
        result = store.get_last_deploy(pid)
        assert result["deployed_commit"] == "sha-second"
    finally:
        _cleanup(store, pid)


def test_record_deploy_stores_previous_commit(store):
    pid = _make_project(store)
    try:
        store.record_deploy(pid, "sha-v1", None, "pipeline_job")
        store.record_deploy(pid, "sha-v2", "sha-v1", "pipeline_job")
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT previous_commit FROM deploys WHERE project_id=%s AND deployed_commit='sha-v2'",
                (pid,),
            ).fetchone()
        assert row is not None
        assert row["previous_commit"] == "sha-v1"
    finally:
        _cleanup(store, pid)


def test_record_deploy_all_trigger_values(store):
    pid = _make_project(store)
    try:
        for trigger in ("pipeline_job", "manual", "auto_poller", "unknown"):
            store.record_deploy(pid, f"sha-{trigger}", None, trigger)
        with store._pool.connection() as conn:
            rows = conn.execute(
                "SELECT trigger FROM deploys WHERE project_id=%s ORDER BY deployed_at",
                (pid,),
            ).fetchall()
        triggers = {r["trigger"] for r in rows}
        assert triggers == {"pipeline_job", "manual", "auto_poller", "unknown"}
    finally:
        _cleanup(store, pid)


def test_record_deploy_stamps_environment_id(store):
    pid = _make_project(store)
    try:
        store.record_deploy(pid, "sha-env", None, "pipeline_job")
        env = store.get_or_create_default_environment(pid)
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT environment_id FROM deploys WHERE project_id=%s AND deployed_commit='sha-env'",
                (pid,),
            ).fetchone()
        assert row is not None
        assert row["environment_id"] == env.id
    finally:
        _cleanup(store, pid)


def test_project_id_only_record_and_get_last_deploy_unchanged(store):
    """No environment_id passed: behaves byte-identically to pre-re-key project_id keying."""
    pid = _make_project(store)
    try:
        store.record_deploy(pid, "sha-first", None, "pipeline_job")
        store.record_deploy(pid, "sha-second", "sha-first", "pipeline_job")

        result = store.get_last_deploy(pid)

        assert result == {"deployed_commit": "sha-second", "finished_at": result["finished_at"]}
        assert set(result.keys()) == {"deployed_commit", "finished_at"}
    finally:
        _cleanup(store, pid)


def test_two_environments_under_one_project_maintain_independent_ledgers(store):
    pid = _make_project(store)
    try:
        env_a = store.create_environment(pid, "staging", "staging")
        env_b = store.create_environment(pid, "dev", "dev")

        store.record_deploy(pid, "sha-a1", None, "pipeline_job", environment_id=env_a.id)
        store.record_deploy(pid, "sha-b1", None, "pipeline_job", environment_id=env_b.id)
        store.record_deploy(
            pid, "sha-a2", "sha-a1", "pipeline_job", environment_id=env_a.id
        )

        last_a = store.get_last_deploy(pid, environment_id=env_a.id)
        last_b = store.get_last_deploy(pid, environment_id=env_b.id)

        assert last_a["deployed_commit"] == "sha-a2"
        assert last_b["deployed_commit"] == "sha-b1"
    finally:
        _cleanup(store, pid)
