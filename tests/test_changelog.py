"""Tests for the changelog feature: list_deploys store method, attribution logic,
and endpoint authorization (Job #326)."""

import uuid
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(store) -> int:
    path = f"/tmp/test-changelog-{uuid.uuid4()}"
    p = store.create_project(f"test-changelog-{uuid.uuid4()}", path)
    return p.id


def _cleanup(store, project_id: int) -> None:
    store.delete_project(project_id)


# ---------------------------------------------------------------------------
# S1: list_deploys store method
# ---------------------------------------------------------------------------


def test_list_deploys_returns_empty(store):
    pid = _make_project(store)
    try:
        assert store.list_deploys(pid) == []
    finally:
        _cleanup(store, pid)


def test_list_deploys_returns_newest_first(store):
    pid = _make_project(store)
    try:
        store.record_deploy(pid, "sha-first", None, "pipeline_job")
        store.record_deploy(pid, "sha-second", "sha-first", "pipeline_job")
        rows = store.list_deploys(pid)
        assert rows[0]["deployed_commit"] == "sha-second"
        assert rows[1]["deployed_commit"] == "sha-first"
    finally:
        _cleanup(store, pid)


def test_list_deploys_includes_all_fields(store):
    pid = _make_project(store)
    try:
        store.record_deploy(pid, "sha-abc", None, "manual", verified=False)
        rows = store.list_deploys(pid)
        assert len(rows) == 1
        row = rows[0]
        for key in (
            "id",
            "deployed_commit",
            "previous_commit",
            "deployed_at",
            "verified",
            "trigger",
            "job_id",
        ):
            assert key in row, f"missing key {key!r}"
        assert row["deployed_commit"] == "sha-abc"
        assert row["trigger"] == "manual"
        assert row["verified"] is False
        assert row["previous_commit"] is None
    finally:
        _cleanup(store, pid)


# ---------------------------------------------------------------------------
# S4: endpoint auth and attribution logic
# ---------------------------------------------------------------------------


def test_changelog_endpoint_forbidden_for_non_member(store):
    """The membership gate used by the endpoint denies non-members."""
    from hyqs.web.auth import AuthContext, is_project_member

    pid = _make_project(store)
    try:
        user = store.create_user(f"nonmember-{uuid.uuid4()}@test.com", "pw")
        ctx = AuthContext(user_id=user.id, user_email=user.email, is_platform_admin=False)
        assert not is_project_member(store, ctx, pid)
    finally:
        _cleanup(store, pid)


def test_changelog_attribution_job_vs_raw():
    """Commit lines matching 'Job #N' map to kind='job'; others map to kind='commit'."""
    from hyqs.web.app import _parse_changelog_commits

    mock_job = MagicMock()
    mock_job.title = "Add feature"
    mock_job.implementation_summary = "Feature summary"

    mock_store = MagicMock()
    mock_store.get.return_value = mock_job

    raw_log = "abc1234\x1fJob #123: Add feature\x1fAlice\ndef5678\x1fFix typo\x1fBob"
    changes = _parse_changelog_commits(raw_log, mock_store)

    assert len(changes) == 2
    assert changes[0]["kind"] == "job"
    assert changes[0]["job_id"] == 123
    assert changes[0]["title"] == "Add feature"
    assert changes[1]["kind"] == "commit"
    assert changes[1]["sha"] == "def5678"
    assert changes[1]["subject"] == "Fix typo"
    assert changes[1]["author"] == "Bob"


def test_changelog_attaches_decision_filename_for_matching_job():
    """A job change entry with a matching decisions_by_job entry gets decision_filename/first_line."""
    from hyqs.web.app import _parse_changelog_commits

    mock_job = MagicMock()
    mock_job.title = "Add feature"
    mock_job.implementation_summary = "Feature summary"

    mock_store = MagicMock()
    mock_store.get.return_value = mock_job

    raw_log = "abc1234\x1fJob #123: Add feature\x1fAlice"
    decisions_by_job = {123: {"filename": "123-add-feature.md", "first_line": "Add feature ADR"}}

    changes = _parse_changelog_commits(raw_log, mock_store, decisions_by_job=decisions_by_job)

    assert len(changes) == 1
    assert changes[0]["decision_filename"] == "123-add-feature.md"
    assert changes[0]["decision_first_line"] == "Add feature ADR"


def test_changelog_omits_decision_fields_when_no_match():
    """A job change entry with no matching decision omits the decision keys."""
    from hyqs.web.app import _parse_changelog_commits

    mock_job = MagicMock()
    mock_job.title = "Add feature"
    mock_job.implementation_summary = "Feature summary"

    mock_store = MagicMock()
    mock_store.get.return_value = mock_job

    raw_log = "abc1234\x1fJob #123: Add feature\x1fAlice"

    changes = _parse_changelog_commits(raw_log, mock_store, decisions_by_job={})

    assert len(changes) == 1
    assert "decision_filename" not in changes[0]
    assert "decision_first_line" not in changes[0]


def test_changelog_commit_entries_never_get_decision_fields():
    """Non-job commit entries are unaffected by decisions_by_job, even if job_id happens to match."""
    from hyqs.web.app import _parse_changelog_commits

    mock_store = MagicMock()

    raw_log = "def5678\x1fFix typo\x1fBob"
    decisions_by_job = {123: {"filename": "123-add-feature.md", "first_line": "Add feature ADR"}}

    changes = _parse_changelog_commits(raw_log, mock_store, decisions_by_job=decisions_by_job)

    assert len(changes) == 1
    assert changes[0]["kind"] == "commit"
    assert "decision_filename" not in changes[0]
    assert "decision_first_line" not in changes[0]


def test_changelog_default_decisions_by_job_backward_compatible():
    """Calling _parse_changelog_commits without the third arg still works exactly as before."""
    from hyqs.web.app import _parse_changelog_commits

    mock_job = MagicMock()
    mock_job.title = "Add feature"
    mock_job.implementation_summary = "Feature summary"

    mock_store = MagicMock()
    mock_store.get.return_value = mock_job

    raw_log = "abc1234\x1fJob #123: Add feature\x1fAlice"

    changes = _parse_changelog_commits(raw_log, mock_store)

    assert len(changes) == 1
    assert changes[0]["kind"] == "job"
    assert "decision_filename" not in changes[0]


def test_changelog_handles_initial_deploy(store):
    """A deploy row with previous_commit=None is treated as the initial deploy."""
    pid = _make_project(store)
    try:
        store.record_deploy(pid, "sha-init", None, "pipeline_job")
        rows = store.list_deploys(pid)
        assert len(rows) == 1
        # The handler detects initial_deploy when previous_commit is None
        assert rows[0]["previous_commit"] is None
        assert rows[0]["deployed_commit"] == "sha-init"
    finally:
        _cleanup(store, pid)
