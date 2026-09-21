"""Tests for the guided job-resolution endpoints (requeue/fix-forward/idea/resolve).

Builds the real Starlette app (hyqs.web.app.build_app) with a MagicMock store/
orchestrator so each route's permission gating, stage-whitelist validation, and
store wiring can be checked with starlette.testclient.TestClient, without a
live Postgres connection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from hyqs.config import Config
from hyqs.pipeline.models import Job, JobSource, JobStatus, Stage
from hyqs.pipeline.store import RemediationLineage
from hyqs.web.app import build_app

_TOKEN = "test-web-token"


def _make_job(**overrides) -> Job:
    defaults = dict(
        id=692,
        idea="original idea",
        repo_path="/tmp/repo",
        chat_id=1,
        status=JobStatus.FAILED,
        stage=Stage.SECURITY,
        project_id=1,
        epic_id=7,
        failure="stored XSS in Transaction.currency",
    )
    defaults.update(overrides)
    return Job(**defaults)


def _make_client(store: MagicMock, *, permissions: list[str] | None = None) -> TestClient:
    store.list_users.return_value = ["existing-admin"]
    store.get_role_permissions.return_value = {"platform_admin": permissions or []}
    config = Config(model="sonnet", permission_mode="acceptEdits", web_token=_TOKEN)
    orchestrator = MagicMock()
    app = build_app(config, orchestrator, store)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {_TOKEN}"})
    return client


@pytest.fixture
def store() -> MagicMock:
    s = MagicMock()
    s.get_unsatisfied_deps.return_value = []
    s.get_scheduler_wait.return_value = None
    s.get_effective_priority.side_effect = lambda job: (job.priority, [])
    return s


def test_requeue_job_forbidden_without_resolve_job_permission(store):
    store.get.return_value = _make_job()
    client = _make_client(store, permissions=[])

    resp = client.post("/api/jobs/692/requeue", json={"stage": "build"})

    assert resp.status_code == 403
    store.requeue_job_at_stage.assert_not_called()


def test_requeue_job_rejects_stage_outside_whitelist(store):
    store.get.return_value = _make_job()
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post("/api/jobs/692/requeue", json={"stage": "merge"})

    assert resp.status_code == 400
    store.requeue_job_at_stage.assert_not_called()


def test_requeue_job_rejects_non_failed_job(store):
    store.get.return_value = _make_job(status=JobStatus.RUNNING)
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post("/api/jobs/692/requeue", json={"stage": "build"})

    assert resp.status_code == 409
    store.requeue_job_at_stage.assert_not_called()


def test_requeue_job_calls_store_with_whitelisted_stage(store):
    store.get.return_value = _make_job()
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post("/api/jobs/692/requeue", json={"stage": "build"})

    assert resp.status_code == 200
    store.requeue_job_at_stage.assert_called_once_with(692, Stage.BUILD)


def test_fix_forward_job_forbidden_without_resolve_job_permission(store):
    store.get.return_value = _make_job()
    client = _make_client(store, permissions=[])

    resp = client.post("/api/jobs/692/fix-forward", json={"idea": "fix the sanitizer"})

    assert resp.status_code == 403
    store.create.assert_not_called()


def test_fix_forward_job_requires_idea(store):
    store.get.return_value = _make_job()
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post("/api/jobs/692/fix-forward", json={"idea": "  "})

    assert resp.status_code == 400
    store.create.assert_not_called()


def test_fix_forward_job_creates_linked_job(store):
    source_job = _make_job()
    store.get.return_value = source_job
    store.create.return_value = _make_job(id=800, idea="fix the sanitizer", failure="")
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post("/api/jobs/692/fix-forward", json={"idea": "fix the sanitizer"})

    assert resp.status_code == 200
    _, kwargs = store.create.call_args
    assert kwargs["repo_path"] == source_job.repo_path
    assert kwargs["chat_id"] == source_job.chat_id
    assert kwargs["epic_id"] == source_job.epic_id
    assert kwargs["source"] == JobSource.UI
    assert kwargs["source_meta"] == {"fix_for": source_job.id}
    assert resp.json()["job"]["id"] == 800


def test_fix_forward_job_with_repoint_ids_calls_repoint_not_create(store):
    source_job = _make_job()
    store.get.return_value = source_job
    store.fix_forward_and_repoint.return_value = _make_job(
        id=800, idea="fix the sanitizer", failure=""
    )
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post(
        "/api/jobs/692/fix-forward",
        json={"idea": "fix the sanitizer", "repoint_dependent_ids": [693, 694]},
    )

    assert resp.status_code == 200
    args, _ = store.fix_forward_and_repoint.call_args
    assert args[0] == 692
    assert args[1] == "fix the sanitizer"
    assert args[2] == ""
    assert args[4] == [693, 694]
    store.create.assert_not_called()
    assert resp.json()["job"]["id"] == 800


def test_fix_forward_job_forbidden_without_resolve_job_permission_with_repoint(store):
    store.get.return_value = _make_job()
    client = _make_client(store, permissions=[])

    resp = client.post(
        "/api/jobs/692/fix-forward",
        json={"idea": "fix the sanitizer", "repoint_dependent_ids": [693]},
    )

    assert resp.status_code == 403
    store.fix_forward_and_repoint.assert_not_called()


def test_fix_forward_job_blocked_by_active_remediation(store):
    source_job = _make_job()
    active_job = _make_job(id=900, status=JobStatus.RUNNING)
    store.get.side_effect = lambda jid: source_job if jid == 692 else active_job
    store.get_active_remediation.return_value = 900
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post("/api/jobs/692/fix-forward", json={"idea": "fix the sanitizer"})

    assert resp.status_code == 409
    assert resp.json()["active_remediation_job_id"] == 900
    store.create.assert_not_called()


def test_fix_forward_job_override_bypasses_active_remediation(store):
    source_job = _make_job()
    active_job = _make_job(id=900, status=JobStatus.RUNNING)
    store.get.side_effect = lambda jid: source_job if jid == 692 else active_job
    store.get_active_remediation.return_value = 900
    store.create.return_value = _make_job(id=800, idea="fix the sanitizer", failure="")
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post(
        "/api/jobs/692/fix-forward",
        json={"idea": "fix the sanitizer", "override_active_remediation": True},
    )

    assert resp.status_code == 200
    store.create.assert_called_once()


def test_fix_forward_job_terminal_active_remediation_does_not_block(store):
    source_job = _make_job()
    active_job = _make_job(id=900, status=JobStatus.DONE)
    store.get.side_effect = lambda jid: source_job if jid == 692 else active_job
    store.get_active_remediation.return_value = 900
    store.create.return_value = _make_job(id=800, idea="fix the sanitizer", failure="")
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post("/api/jobs/692/fix-forward", json={"idea": "fix the sanitizer"})

    assert resp.status_code == 200
    store.create.assert_called_once()


def test_fix_forward_job_registers_new_job_as_active_remediation(store):
    source_job = _make_job()
    store.get.return_value = source_job
    store.get_active_remediation.return_value = None
    store.resolve_remediation_lineage.return_value = RemediationLineage(600, 1)
    store.create.return_value = _make_job(id=800, idea="fix the sanitizer", failure="")
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post("/api/jobs/692/fix-forward", json={"idea": "fix the sanitizer"})

    assert resp.status_code == 200
    store.set_active_remediation.assert_called_once_with(692, 800)
    source_meta = store.create.call_args.kwargs["source_meta"]
    assert source_meta["remediation_root_job_id"] == 600
    assert source_meta["remediation_depth"] == 1


def test_get_job_dependents_returns_list(store):
    store.get.return_value = _make_job()
    store.get_dependent_jobs.return_value = [
        _make_job(id=693, idea="dependent a", status=JobStatus.PENDING),
        _make_job(id=694, idea="dependent b", status=JobStatus.PENDING),
    ]
    client = _make_client(store, permissions=[])

    resp = client.get("/api/jobs/692/dependents")

    assert resp.status_code == 200
    dependents = resp.json()["dependents"]
    assert [d["id"] for d in dependents] == [693, 694]
    assert dependents[0]["status"] == "pending"


def test_get_job_depends_on_jobs_returns_list(store):
    store.get.return_value = _make_job()
    store.get_dependency_jobs.return_value = [
        _make_job(id=690, idea="dependency a", status=JobStatus.DONE),
        _make_job(id=691, idea="dependency b", status=JobStatus.DONE),
    ]
    client = _make_client(store, permissions=[])

    resp = client.get("/api/jobs/692/depends-on-jobs")

    assert resp.status_code == 200
    depends_on_jobs = resp.json()["depends_on_jobs"]
    assert [d["id"] for d in depends_on_jobs] == [690, 691]
    assert depends_on_jobs[0]["status"] == "done"


def test_get_job_depends_on_jobs_not_found(store):
    store.get.return_value = None
    client = _make_client(store, permissions=[])

    resp = client.get("/api/jobs/692/depends-on-jobs")

    assert resp.status_code == 404


def test_patch_job_idea_forbidden_without_edit_job_deps_permission(store):
    store.get.return_value = _make_job()
    client = _make_client(store, permissions=[])

    resp = client.patch("/api/jobs/692/idea", json={"idea": "a clearer idea"})

    assert resp.status_code == 403
    store.update_job_idea.assert_not_called()


def test_patch_job_idea_persists_via_update_job_idea(store):
    store.get.return_value = _make_job()
    client = _make_client(store, permissions=["edit_job_deps"])

    resp = client.patch("/api/jobs/692/idea", json={"idea": "a clearer idea"})

    assert resp.status_code == 200
    store.update_job_idea.assert_called_once_with(692, "a clearer idea")


def test_patch_job_idea_response_reports_needs_split_false_by_default(store):
    store.get.return_value = _make_job()
    client = _make_client(store, permissions=["edit_job_deps"])

    resp = client.patch("/api/jobs/692/idea", json={"idea": "a clearer idea"})

    assert resp.status_code == 200
    assert resp.json()["job"]["needs_split"] is False


def test_patch_job_idea_response_reports_needs_split_true_for_parked_job(store):
    store.get.return_value = _make_job(needs_split=True, archived=True)
    client = _make_client(store, permissions=["edit_job_deps"])

    resp = client.patch("/api/jobs/692/idea", json={"idea": "a clearer, smaller idea"})

    assert resp.status_code == 200
    assert resp.json()["job"]["needs_split"] is True


def test_resolve_job_forbidden_without_resolve_job_permission(store):
    store.get.return_value = _make_job()
    client = _make_client(store, permissions=[])

    resp = client.post("/api/jobs/692/resolve")

    assert resp.status_code == 403
    store.set_job_resolution.assert_not_called()


def test_resolve_job_rejects_non_terminal_job(store):
    store.get.return_value = _make_job(status=JobStatus.RUNNING)
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post("/api/jobs/692/resolve")

    assert resp.status_code == 409
    store.set_job_resolution.assert_not_called()


def test_resolve_job_sets_resolution_for_failed_job(store):
    store.get.return_value = _make_job(status=JobStatus.FAILED)
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post("/api/jobs/692/resolve")

    assert resp.status_code == 200
    store.set_job_resolution.assert_called_once_with(692, "resolved")


def test_resolve_job_allowed_for_cancelled_job(store):
    store.get.return_value = _make_job(status=JobStatus.CANCELLED)
    client = _make_client(store, permissions=["resolve_job"])

    resp = client.post("/api/jobs/692/resolve")

    assert resp.status_code == 200
    store.set_job_resolution.assert_called_once_with(692, "resolved")


def test_retry_job_without_body_calls_store_retry_with_force_false(store):
    store.get.return_value = _make_job(status=JobStatus.FAILED)
    store.retry.return_value = _make_job(status=JobStatus.PENDING, stage=Stage.QUEUED)
    client = _make_client(store, permissions=["retry_job"])

    with patch(
        "hyqs.web.app.github.cleanup_branch_for_retry", new_callable=AsyncMock, return_value=None
    ):
        resp = client.post("/api/jobs/692/retry")

    assert resp.status_code == 200
    store.retry.assert_called_once_with(692, force=False)


def test_retry_job_with_force_true_calls_store_retry_with_force(store):
    store.get.return_value = _make_job(status=JobStatus.FAILED)
    store.retry.return_value = _make_job(status=JobStatus.PENDING, stage=Stage.QUEUED)
    client = _make_client(store, permissions=["retry_job"])

    with patch(
        "hyqs.web.app.github.cleanup_branch_for_retry", new_callable=AsyncMock, return_value=None
    ):
        resp = client.post("/api/jobs/692/retry", json={"force": True})

    assert resp.status_code == 200
    store.retry.assert_called_once_with(692, force=True)


def test_retry_job_rejects_needs_split_job(store):
    store.get.return_value = _make_job(status=JobStatus.FAILED, needs_split=True)
    client = _make_client(store, permissions=["retry_job"])

    with patch(
        "hyqs.web.app.github.cleanup_branch_for_retry", new_callable=AsyncMock, return_value=None
    ):
        resp = client.post("/api/jobs/692/retry")

    assert resp.status_code == 409
    body = resp.json()
    assert "refile" in body["error"]
    assert "force=true" in body["error"]
    store.retry.assert_not_called()


def test_retry_job_with_force_true_bypasses_needs_split_rejection(store):
    store.get.return_value = _make_job(status=JobStatus.FAILED, needs_split=True)
    store.retry.return_value = _make_job(
        status=JobStatus.PENDING, stage=Stage.QUEUED, needs_split=False
    )
    client = _make_client(store, permissions=["retry_job"])

    with patch(
        "hyqs.web.app.github.cleanup_branch_for_retry", new_callable=AsyncMock, return_value=None
    ):
        resp = client.post("/api/jobs/692/retry", json={"force": True})

    assert resp.status_code == 200
    store.retry.assert_called_once_with(692, force=True)


def test_retry_job_blocked_by_active_remediation(store):
    job = _make_job(status=JobStatus.FAILED)
    active_job = _make_job(id=900, status=JobStatus.RUNNING)
    store.get.side_effect = lambda jid: job if jid == 692 else active_job
    store.get_active_remediation.return_value = 900
    client = _make_client(store, permissions=["retry_job"])

    with patch(
        "hyqs.web.app.github.cleanup_branch_for_retry", new_callable=AsyncMock, return_value=None
    ):
        resp = client.post("/api/jobs/692/retry")

    assert resp.status_code == 409
    assert resp.json()["active_remediation_job_id"] == 900
    store.retry.assert_not_called()


def test_retry_job_override_bypasses_active_remediation(store):
    job = _make_job(status=JobStatus.FAILED)
    active_job = _make_job(id=900, status=JobStatus.RUNNING)
    store.get.side_effect = lambda jid: job if jid == 692 else active_job
    store.get_active_remediation.return_value = 900
    store.retry.return_value = _make_job(status=JobStatus.PENDING, stage=Stage.QUEUED)
    client = _make_client(store, permissions=["retry_job"])

    with patch(
        "hyqs.web.app.github.cleanup_branch_for_retry", new_callable=AsyncMock, return_value=None
    ):
        resp = client.post("/api/jobs/692/retry", json={"override_active_remediation": True})

    assert resp.status_code == 200
    store.retry.assert_called_once_with(692, force=False)
