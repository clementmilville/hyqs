"""Tests for the gate isolation guard (hyqs/pipeline/gate_guard.py).

Real git repo in tmp_path, real subprocess calls — no Postgres, matching the
style of tests/test_invariants.py's integration test.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from hyqs.pipeline.gate_guard import GateIsolationError, run_guarded_gate


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "app.py").write_text("print('hello')\n")
    (repo / "identity").mkdir()
    (repo / "identity" / "alembic.ini").write_text("[alembic]\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def test_no_mutation_returns_result_and_invokes_fn_once(tmp_path):
    repo = _make_repo(tmp_path)
    calls = []

    async def fn():
        calls.append(1)
        return "ok"

    result = asyncio.run(run_guarded_gate("review", fn, repo))

    assert result == "ok"
    assert len(calls) == 1


def test_lockfile_only_mutation_is_tolerated(tmp_path):
    repo = _make_repo(tmp_path)
    calls = []

    async def fn():
        calls.append(1)
        (repo / "identity" / "uv.lock").write_text("locked\n")
        return "ok"

    result = asyncio.run(run_guarded_gate("review", fn, repo))

    assert result == "ok"
    assert len(calls) == 1
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert status == ""


def test_lockfile_plus_tracked_file_change_still_raises(tmp_path):
    repo = _make_repo(tmp_path)
    calls = []

    async def fn():
        calls.append(1)
        (repo / "identity" / "uv.lock").write_text("locked\n")
        (repo / "app.py").write_text("print('changed')\n")
        return "ok"

    with pytest.raises(GateIsolationError):
        asyncio.run(run_guarded_gate("review", fn, repo))

    assert len(calls) == 2


def test_no_changes_returns_result_normally(tmp_path):
    repo = _make_repo(tmp_path)
    calls = []

    async def fn():
        calls.append(1)
        return "ok"

    result = asyncio.run(run_guarded_gate("review", fn, repo))

    assert result == "ok"
    assert len(calls) == 1


def test_head_change_with_no_working_tree_diff_still_raises(tmp_path):
    repo = _make_repo(tmp_path)
    calls = []

    async def fn():
        calls.append(1)
        (repo / "app.py").write_text(f"print('committed change {len(calls)}')\n")
        _git("commit", "-a", "-q", "-m", f"gate committed {len(calls)}", cwd=repo)
        return "ok"

    with pytest.raises(GateIsolationError):
        asyncio.run(run_guarded_gate("review", fn, repo))

    assert len(calls) == 2
