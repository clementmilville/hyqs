"""Tests for gitops.fresh_base and the git() timeout kwarg.

Real git repos in tmp_path, real subprocess calls — no Postgres, matching the
style of tests/test_gate_guard.py.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from hyqs.pipeline import gitops


def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _make_origin(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("config", "user.email", "test@example.com", cwd=origin)
    _git("config", "user.name", "Test", cwd=origin)
    (origin / "base.py").write_text("v1\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-q", "-m", "init", cwd=origin)
    return origin


def test_fresh_base_scopes_three_dot_diff_correctly(tmp_path):
    """Reproduces the stale-local-ref bug: a job branch forked from a fresh
    main (already containing another job's commit), but the local `main` ref
    later regresses behind that commit. Without fetching first, the three-dot
    diff's merge-base falls back to the older commit and wrongly attributes
    the other job's file to this branch; fresh_base fixes the scoping.
    """
    origin = _make_origin(tmp_path)
    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=clone)
    _git("config", "user.name", "Test", cwd=clone)

    old_main = subprocess.run(
        ["git", "rev-parse", "main"], cwd=clone, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Detach HEAD so `main` isn't the checked-out branch — `git fetch <url>
    # main:main` refuses to update a ref that's checked out.
    _git("checkout", "-q", "--detach", "main", cwd=clone)

    # Another job's commit lands on origin's main after the clone.
    (origin / "other.py").write_text("other\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-q", "-m", "other job's change", cwd=origin)

    # This job's branch is created AFTER fetching that commit — i.e. it truly
    # forked from origin's current main.
    _git("fetch", "-q", "origin", "main:main", cwd=clone)
    _git("checkout", "-q", "-b", "feature", "main", cwd=clone)
    (clone / "feature.py").write_text("feature\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "this job's change", cwd=clone)

    # Simulate the local main ref regressing to the pre-fetch commit (the
    # "ancient local ref" failure mode from the postmortem).
    _git("branch", "-f", "main", old_main, cwd=clone)

    def _changed_files() -> set[str]:
        res = subprocess.run(
            ["git", "diff", "--numstat", "main...feature"],
            cwd=clone,
            check=True,
            capture_output=True,
            text=True,
        )
        return {line.split("\t")[-1] for line in res.stdout.splitlines()}

    # Before fetching: the stale local main wrongly attributes other.py to this branch.
    assert _changed_files() == {"feature.py", "other.py"}

    result = asyncio.run(gitops.fresh_base(clone, "main"))

    assert result == "main"
    assert _changed_files() == {"feature.py"}


def test_fresh_base_returns_base_unchanged_when_no_remote(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "f.py").write_text("v1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)

    result = asyncio.run(gitops.fresh_base(repo, "main"))

    assert result == "main"


def test_fresh_base_falls_back_when_remote_unreachable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "f.py").write_text("v1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", str(tmp_path / "does-not-exist.git"), cwd=repo)

    result = asyncio.run(gitops.fresh_base(repo, "main"))

    assert result == "main"


def test_fresh_base_repairs_named_invalid_unregistered_job_ref_and_retries(tmp_path):
    origin = _make_origin(tmp_path)
    repo = tmp_path / "repo"
    _git("clone", "-q", str(origin), str(repo), cwd=tmp_path)
    _git("checkout", "-q", "--detach", "main", cwd=repo)
    broken = repo / ".git" / "refs" / "heads" / "hyqs" / "job-2176"
    broken.parent.mkdir(parents=True)
    broken.write_text("1" * 40 + "\n")
    (origin / "fresh.py").write_text("fresh\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-q", "-m", "fresh", cwd=origin)

    result = asyncio.run(gitops.fresh_base(repo, "main"))

    assert result == "main"
    assert not broken.exists()
    shown = subprocess.run(
        ["git", "show", "main:fresh.py"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert shown.stdout == "fresh\n"


def test_invalid_ref_repair_unlinks_broken_loose_ref_when_update_ref_fails(tmp_path, monkeypatch):
    repo = _make_origin(tmp_path)
    broken = repo / ".git" / "refs" / "heads" / "hyqs" / "job-2176"
    broken.parent.mkdir(parents=True)
    broken.write_text("not-an-object\n")
    original_git = gitops.git

    async def fail_update_ref(repo_path, *args, **kwargs):
        if args == ("update-ref", "-d", "refs/heads/hyqs/job-2176"):
            return gitops.GitResult(
                ok=False,
                stdout="",
                stderr="unable to resolve reference",
                code=128,
            )
        return await original_git(repo_path, *args, **kwargs)

    monkeypatch.setattr(gitops, "git", fail_update_ref)

    repaired = asyncio.run(
        gitops._repair_fetch_blocking_ref(repo, "fatal: bad object refs/heads/hyqs/job-2176")
    )

    assert repaired == "refs/heads/hyqs/job-2176"
    assert not broken.exists()


@pytest.mark.parametrize(
    "ref",
    [
        "refs/heads/main",
        "refs/heads/master",
        "refs/remotes/origin/hyqs/job-1",
        "refs/heads/feature-broken",
    ],
)
def test_invalid_ref_repair_refuses_protected_nonlocal_and_non_hyqs_refs(tmp_path, ref):
    repo = _make_origin(tmp_path)

    repaired = asyncio.run(gitops._repair_fetch_blocking_ref(repo, f"fatal: bad object {ref}"))

    assert repaired is None


def test_invalid_ref_repair_refuses_valid_job_ref(tmp_path):
    repo = _make_origin(tmp_path)
    _git("branch", "hyqs/job-valid", "main", cwd=repo)

    repaired = asyncio.run(
        gitops._repair_fetch_blocking_ref(repo, "fatal: bad object refs/heads/hyqs/job-valid")
    )

    assert repaired is None
    assert asyncio.run(gitops.git(repo, "show-ref", "--verify", "refs/heads/hyqs/job-valid")).ok


def test_invalid_ref_repair_refuses_live_worktree_ref(tmp_path):
    repo = _make_origin(tmp_path)
    _git("branch", "hyqs/job-live", "main", cwd=repo)
    live = tmp_path / "live"
    _git("worktree", "add", "-q", str(live), "hyqs/job-live", cwd=repo)
    broken = repo / ".git" / "refs" / "heads" / "hyqs" / "job-live"
    broken.write_text("2" * 40 + "\n")

    repaired = asyncio.run(
        gitops._repair_fetch_blocking_ref(repo, "fatal: bad object refs/heads/hyqs/job-live")
    )

    assert repaired is None
    assert broken.exists()


def test_invalid_ref_repair_refuses_ambiguous_diagnostic(tmp_path):
    repo = _make_origin(tmp_path)
    first = repo / ".git" / "refs" / "heads" / "hyqs" / "job-1"
    second = repo / ".git" / "refs" / "heads" / "hyqs" / "job-2"
    first.parent.mkdir(parents=True)
    first.write_text("3" * 40 + "\n")
    second.write_text("4" * 40 + "\n")

    repaired = asyncio.run(
        gitops._repair_fetch_blocking_ref(
            repo,
            "bad object refs/heads/hyqs/job-1; bad object refs/heads/hyqs/job-2",
        )
    )

    assert repaired is None
    assert first.exists()
    assert second.exists()


def test_create_worktree_uses_fresh_remote_base_without_mutating_shared_checkout(tmp_path):
    origin = _make_origin(tmp_path)
    managed = tmp_path / "managed"
    _git("clone", "-q", str(origin), str(managed), cwd=tmp_path)

    stale_main = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=managed,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git("checkout", "-q", "--detach", stale_main, cwd=managed)
    (managed / "local-notes.txt").write_text("preserve me\n")
    shared_head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=managed,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    shared_status_before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=managed,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    (origin / "prerequisite.py").write_text("contract = 'complete'\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-q", "-m", "complete prerequisite", cwd=origin)
    prerequisite_commit = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=origin,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    async def _create():
        base = await gitops.fresh_base(managed, "main")
        return await gitops.create_worktree(
            managed,
            "hyqs/job-2276",
            tmp_path / "dependent",
            base=base,
        )

    result = asyncio.run(_create())

    assert result.ok, result.stderr
    assert (tmp_path / "dependent" / "prerequisite.py").read_text() == "contract = 'complete'\n"
    dependent_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path / "dependent",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert dependent_head == prerequisite_commit
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=managed,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == shared_head_before
    )
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=managed,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == shared_status_before
    )


def test_create_worktree_explicit_local_base_survives_unreachable_origin(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "local.py").write_text("offline\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "local base", cwd=repo)
    _git("checkout", "-q", "--detach", "main", cwd=repo)
    _git("remote", "add", "origin", str(tmp_path / "unreachable.git"), cwd=repo)

    async def _create():
        base = await gitops.fresh_base(repo, "main")
        return await gitops.create_worktree(
            repo, "hyqs/job-offline", tmp_path / "offline-worktree", base=base
        )

    result = asyncio.run(_create())

    assert result.ok, result.stderr
    assert (tmp_path / "offline-worktree" / "local.py").read_text() == "offline\n"


def test_git_timeout_returns_promptly_instead_of_hanging(tmp_path):
    class _HangingProcess:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _HangingProcess()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
        result = asyncio.run(gitops.git(tmp_path, "status", timeout=0.2))

    assert result.ok is False
    assert result.code == -1
    assert "timed out" in result.stderr


# --- base_has_advanced (job #4308 regression) --------------------------------
#
# Every merge-stage test mocks base_has_advanced, so when job #4307's
# argument-injection hardening added --end-of-options to the rev-parse inside
# it, nothing caught that the function started returning True unconditionally.
# These exercise it against real git.


def _repo_with_branch(tmp_path):
    """A repo whose `work` branch already contains everything on `main`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "a.txt").write_text("v1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("checkout", "-q", "-b", "work", cwd=repo)
    (repo / "b.txt").write_text("v1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "work", cwd=repo)
    return repo


def test_base_has_advanced_is_false_when_branch_contains_base(tmp_path):
    """The loop-causing case: base is an ancestor, so it has NOT advanced."""
    repo = _repo_with_branch(tmp_path)

    assert asyncio.run(gitops.base_has_advanced(repo, "work", "main")) is False


def test_base_has_advanced_is_true_after_base_moves(tmp_path):
    repo = _repo_with_branch(tmp_path)
    _git("checkout", "-q", "main", cwd=repo)
    (repo / "c.txt").write_text("v1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "advance", cwd=repo)

    assert asyncio.run(gitops.base_has_advanced(repo, "work", "main")) is True


def test_base_has_advanced_becomes_false_once_base_is_merged_in(tmp_path):
    """Absorbing the advance must end the loop, not perpetuate it."""
    repo = _repo_with_branch(tmp_path)
    _git("checkout", "-q", "main", cwd=repo)
    (repo / "c.txt").write_text("v1\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "advance", cwd=repo)
    _git("checkout", "-q", "work", cwd=repo)
    _git("merge", "-q", "--no-edit", "main", cwd=repo)

    assert asyncio.run(gitops.base_has_advanced(repo, "work", "main")) is False


def test_base_has_advanced_rev_parse_output_is_a_bare_object_id(tmp_path):
    """Guards the exact defect: rev-parse must not echo its own option marker."""
    repo = _repo_with_branch(tmp_path)

    res = asyncio.run(gitops.git(repo, "rev-parse", "--verify", "--end-of-options", "main"))

    assert res.ok
    assert res.stdout.strip().splitlines() == [res.stdout.strip()]
    assert "--end-of-options" not in res.stdout
