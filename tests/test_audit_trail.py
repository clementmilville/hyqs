"""Audit-trail tests: trigger stamping, actor plumbing, noise filter, retention.

Run against the isolated *_test database (see conftest); JobStore's schema init
creates the audit_log table, provenance columns, and hyqs_audit trigger there.
"""

from __future__ import annotations

import uuid

from hyqs.pipeline.models import Stage
from hyqs.pipeline.store import set_db_actor


def _fresh_project(store):
    path = f"/tmp/test-audit-{uuid.uuid4()}"
    return store.create_project(f"audit-{uuid.uuid4()}", path)


def _cleanup_project(store, pid: int) -> None:
    with store._pool.connection() as conn:
        conn.execute("DELETE FROM project_agents WHERE project_id=%s", (pid,))
        conn.execute("DELETE FROM projects WHERE id=%s", (pid,))


def test_insert_stamps_created_by_and_logs(store):
    set_db_actor("test:audit-insert")
    p = _fresh_project(store)
    try:
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT created_by, updated_by FROM projects WHERE id=%s", (p.id,)
            ).fetchone()
        assert row["created_by"] == "test:audit-insert"
        assert row["updated_by"] == "test:audit-insert"
        entries = store.list_audit(table="projects", row_pk=str(p.id))
        assert any(e["action"] == "insert" and e["actor"] == "test:audit-insert" for e in entries)
    finally:
        set_db_actor("test:pytest")
        _cleanup_project(store, p.id)


def test_update_stamps_updated_by_and_records_diff(store):
    set_db_actor("test:audit-creator")
    p = _fresh_project(store)
    try:
        set_db_actor("test:audit-editor")
        store.update_project(p.id, description="new description")
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT created_by, updated_by FROM projects WHERE id=%s", (p.id,)
            ).fetchone()
        assert row["created_by"] == "test:audit-creator"  # creator preserved
        assert row["updated_by"] == "test:audit-editor"
        entries = store.list_audit(table="projects", row_pk=str(p.id))
        upd = next(e for e in entries if e["action"] == "update")
        assert upd["actor"] == "test:audit-editor"
        assert "description" in upd["changed"]
    finally:
        set_db_actor("test:pytest")
        _cleanup_project(store, p.id)


def test_lease_only_update_produces_no_audit_row(store):
    set_db_actor("test:audit-lease")
    # initial_stage=DONE keeps this job out of claim()'s fleet-wide candidate
    # query (stage != DONE) so a concurrently-running claim() elsewhere in the
    # shared test database can't grab it and mutate status/owner between the
    # before/after audit reads below.
    job = store.create(
        idea="audit lease test",
        repo_path=f"/tmp/test-audit-{uuid.uuid4()}",
        chat_id=1,
        initial_stage=Stage.DONE,
    )
    try:
        before = len(store.list_audit(table="jobs", row_pk=str(job.id), limit=1000))
        with store._pool.connection() as conn:
            conn.execute("UPDATE jobs SET lease_until=12345.0 WHERE id=%s", (job.id,))
        after = len(store.list_audit(table="jobs", row_pk=str(job.id), limit=1000))
        assert after == before  # heartbeat churn is not audited
    finally:
        pid = job.project_id
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id=%s", (job.id,))
            conn.execute("DELETE FROM project_agents WHERE project_id=%s", (pid,))
            conn.execute("DELETE FROM projects WHERE id=%s", (pid,))


def test_unset_actor_falls_back_to_db_user(store):
    set_db_actor("")  # simulate a write path that never set an actor
    p = _fresh_project(store)
    try:
        with store._pool.connection() as conn:
            row = conn.execute("SELECT created_by FROM projects WHERE id=%s", (p.id,)).fetchone()
        assert row["created_by"].startswith("db:")  # attributed to the session user
    finally:
        set_db_actor("test:pytest")
        _cleanup_project(store, p.id)


def test_prune_audit_log_returns_count(store):
    assert store.prune_audit_log(days=3650) >= 0


def test_insert_stamps_backend_forensics(store):
    set_db_actor("test:audit-forensics")
    p = _fresh_project(store)
    try:
        entries = store.list_audit(table="projects", row_pk=str(p.id))
        entry = next(e for e in entries if e["action"] == "insert")
        assert entry["backend_pid"] is not None
        assert entry["backend_start"] is not None
    finally:
        set_db_actor("test:pytest")
        _cleanup_project(store, p.id)
