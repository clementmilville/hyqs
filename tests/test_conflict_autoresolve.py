"""Unit + behavior tests for hyqs.pipeline.conflict_autoresolve (job #2272).

S1 (resolve_pure_append_conflicts) is pure, file-content-only — no git or
filesystem I/O needed. S2 (autoresolve_conflict) is exercised against a real
tmp_path git repo, following the project convention of mocking only external
I/O that cannot run in CI (there is none here: git itself runs for real).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from hyqs.pipeline.conflict_autoresolve import (
    AutoresolveResult,
    autoresolve_conflict,
    resolve_pure_append_conflicts,
)

# Mirrors the grant_audit.py enum-tail-append shape from the job #2272 incident.
_ENUM_APPEND_CONFLICT = '''\
class GrantAuditEventType(str, enum.Enum):
    sync_app_roles = "sync_app_roles"
<<<<<<< HEAD
    sync_org_events = "sync_org_events"
    force_sync_org = "force_sync_org"
=======
    impersonate_start = "impersonate_start"
    impersonate_end = "impersonate_end"
    impersonate_token_issued = "impersonate_token_issued"
>>>>>>> job-2243
'''


def test_resolves_two_sided_enum_tail_append():
    resolved = resolve_pure_append_conflicts(_ENUM_APPEND_CONFLICT)
    assert resolved is not None
    assert "<<<<<<<" not in resolved
    assert "=======" not in resolved
    assert ">>>>>>>" not in resolved
    for member in (
        "sync_org_events",
        "force_sync_org",
        "impersonate_start",
        "impersonate_end",
        "impersonate_token_issued",
    ):
        assert member in resolved
    # Valid, importable-shaped Python: compiles without a SyntaxError.
    compile(resolved, "<test>", "exec")


def test_rejects_hunk_where_one_side_is_empty():
    text = (
        "line a\n"
        "<<<<<<< HEAD\n"
        "=======\n"
        "added_only_by_theirs\n"
        ">>>>>>> other\n"
    )
    assert resolve_pure_append_conflicts(text) is None


def test_rejects_hunk_that_looks_like_an_edit():
    # Both blocks non-empty but the SAME line count (1-vs-1): without diff3
    # ancestor markers this is indistinguishable from a modify/modify edit of
    # the same original line, so it must be rejected rather than guessed at.
    text = (
        "line a\n"
        "<<<<<<< HEAD\n"
        "value = 1\n"
        "=======\n"
        "value = 2\n"
        ">>>>>>> other\n"
    )
    assert resolve_pure_append_conflicts(text) is None


def test_rejects_diff3_ancestor_markers():
    text = (
        "line a\n"
        "<<<<<<< HEAD\n"
        "ours line\n"
        "||||||| base\n"
        "original line\n"
        "=======\n"
        "theirs line\n"
        ">>>>>>> other\n"
    )
    assert resolve_pure_append_conflicts(text) is None


def test_rejects_malformed_unbalanced_markers():
    text = "<<<<<<< HEAD\nours line\n=======\ntheirs line\n"  # missing >>>>>>>
    assert resolve_pure_append_conflicts(text) is None


def test_returns_none_when_no_conflict_markers_present():
    assert resolve_pure_append_conflicts("just some ordinary file text\n") is None


def test_multi_hunk_file_only_resolves_when_all_hunks_are_safe():
    safe_hunk = (
        "<<<<<<< HEAD\n"
        "ours_a\n"
        "ours_b\n"
        "=======\n"
        "theirs_a\n"
        ">>>>>>> other\n"
    )
    unsafe_hunk = (
        "<<<<<<< HEAD\n"
        "=======\n"
        "theirs_only\n"
        ">>>>>>> other\n"
    )
    all_safe = safe_hunk + safe_hunk
    assert resolve_pure_append_conflicts(all_safe) is not None

    mixed = safe_hunk + unsafe_hunk
    assert resolve_pure_append_conflicts(mixed) is None


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(path: Path) -> str:
    """Init a repo and return its initial branch name (varies by git config)."""
    path.mkdir(parents=True, exist_ok=True)
    _run(path, "init", "-q")
    _run(path, "config", "user.email", "test@example.com")
    _run(path, "config", "user.name", "Test")
    return _run(path, "branch", "--show-current").stdout.strip()


def test_autoresolve_conflict_resolves_pure_append_and_commits(tmp_path):
    repo = tmp_path / "repo"
    default_branch = _init_repo(repo)
    target = repo / "grant_audit.py"
    target.write_text(
        "class GrantAuditEventType(str, enum.Enum):\n"
        '    sync_app_roles = "sync_app_roles"\n'
    )
    _run(repo, "add", ".")
    _run(repo, "commit", "-q", "-m", "base")
    _run(repo, "checkout", "-q", "-b", "feature-a")
    target.write_text(
        "class GrantAuditEventType(str, enum.Enum):\n"
        '    sync_app_roles = "sync_app_roles"\n'
        '    sync_org_events = "sync_org_events"\n'
        '    force_sync_org = "force_sync_org"\n'
    )
    _run(repo, "commit", "-q", "-am", "feature-a append")
    _run(repo, "checkout", "-q", default_branch)
    target.write_text(
        "class GrantAuditEventType(str, enum.Enum):\n"
        '    sync_app_roles = "sync_app_roles"\n'
        '    impersonate_start = "impersonate_start"\n'
    )
    _run(repo, "commit", "-q", "-am", "main append")

    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", "feature-a"],
        capture_output=True,
        text=True,
    )
    assert merge.returncode != 0  # genuine conflict

    result = asyncio.run(autoresolve_conflict(repo))

    assert result.ok is True
    assert result.files == ["grant_audit.py"]
    status = _run(repo, "status", "--porcelain")
    assert status.stdout.strip() == ""  # clean — merge committed
    resolved_text = target.read_text()
    assert "sync_org_events" in resolved_text
    assert "impersonate_start" in resolved_text
    compile(resolved_text, "<test>", "exec")
    # HEAD advanced past the merge commit.
    log = _run(repo, "log", "--oneline", "-1")
    assert "Merge" in log.stdout or log.stdout.strip() != ""


def test_autoresolve_conflict_leaves_repo_untouched_when_one_hunk_is_an_edit(tmp_path):
    repo = tmp_path / "repo"
    default_branch = _init_repo(repo)
    target = repo / "config.py"
    target.write_text("VALUE = 1\nOTHER = 'x'\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-q", "-m", "base")
    _run(repo, "checkout", "-q", "-b", "feature-b")
    target.write_text("VALUE = 2\nOTHER = 'x'\n")
    _run(repo, "commit", "-q", "-am", "feature-b edits VALUE")
    _run(repo, "checkout", "-q", default_branch)
    target.write_text("VALUE = 3\nOTHER = 'x'\n")
    _run(repo, "commit", "-q", "-am", "main edits VALUE")

    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", "feature-b"],
        capture_output=True,
        text=True,
    )
    assert merge.returncode != 0

    status_before = _run(repo, "status", "--porcelain").stdout

    result = asyncio.run(autoresolve_conflict(repo))

    assert result.ok is False
    status_after = _run(repo, "status", "--porcelain").stdout
    assert status_after == status_before  # untouched — still conflicted (UU)
    assert "UU config.py" in status_after
    log = _run(repo, "log", "--oneline")
    assert "Merge" not in log.stdout  # no commit was created


def test_autoresolve_conflict_does_not_partially_resolve_multi_file_conflict(tmp_path):
    repo = tmp_path / "repo"
    default_branch = _init_repo(repo)
    safe_file = repo / "enum_file.py"
    unsafe_file = repo / "config.py"
    safe_file.write_text("A = 1\n")
    unsafe_file.write_text("VALUE = 1\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-q", "-m", "base")
    _run(repo, "checkout", "-q", "-b", "feature-c")
    safe_file.write_text("A = 1\nB_FROM_FEATURE = 2\nB2_FROM_FEATURE = 22\n")
    unsafe_file.write_text("VALUE = 2\n")
    _run(repo, "commit", "-q", "-am", "feature-c changes")
    _run(repo, "checkout", "-q", default_branch)
    safe_file.write_text("A = 1\nC_FROM_MAIN = 3\n")
    unsafe_file.write_text("VALUE = 3\n")
    _run(repo, "commit", "-q", "-am", "main changes")

    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", "feature-c"],
        capture_output=True,
        text=True,
    )
    assert merge.returncode != 0

    result = asyncio.run(autoresolve_conflict(repo))

    assert result.ok is False
    status = _run(repo, "status", "--porcelain").stdout
    assert "UU enum_file.py" in status
    assert "UU config.py" in status
    # Zero files touched: the safe file's working-tree content still carries
    # its own conflict markers, unmodified by this call.
    assert "<<<<<<<" in safe_file.read_text()


def test_autoresolve_conflict_no_unmerged_paths_returns_not_ok(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "f.py").write_text("A = 1\n")
    _run(repo, "add", ".")
    _run(repo, "commit", "-q", "-m", "base")

    result = asyncio.run(autoresolve_conflict(repo))

    assert result.ok is False
    assert isinstance(result, AutoresolveResult)
