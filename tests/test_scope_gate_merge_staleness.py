"""Reproduction harness for the reported out-of-lane false-positive (job #1938).

Real git repos in tmp_path, real subprocess calls — no Postgres, matching the
style of tests/test_gitops_fresh_base.py.

Background: `check_out_of_lane` (hyqs/pipeline/collision.py) was repeatedly
observed flagging files a job's own commits never touched. The working
hypothesis was that a job branch that goes through one or more
`gitops.merge_base_into_worktree` cycles (the MERGE stage's in-lock loop that
absorbs a base advance before the job's own merge, or a real conflict
resolved via the FIX stage) could end up with `merge-base(fresh_base, HEAD)`
landing on an intermediate point rather than the job's true fork point, so
the triple-dot diff used by `gitops.numstat(worktree, f"{base}...HEAD")`
(stages/review.py, stages/fix.py) would include files from an unrelated main
commit that landed *after* the job's last merge-into-worktree cycle.

Each scenario below drives the *exact* production call sequence —
`gitops.fresh_base` + `gitops.numstat` with `...` (stages/review.py:88-114,
stages/fix.py:105-107), and separately the `merge-base` + `..` recompute used
by stages/merge_verify.py:90-114 — across increasingly adversarial git
histories, and asserts on the resulting changed-files set.

Finding (all three scenarios, verified via `git log --graph` + `git
merge-base` inspection alongside the assertions below): the misattribution
does NOT reproduce through this call path. `git diff A...B` diffs against
`merge-base(A, B)`, and because origin's `main` history in this pipeline is
strictly append-only (GitHub squash-merges only ever add commits, never
rewrite), the merge-base after N merge_base_into_worktree cycles always lands
exactly on the job's own last-merged-in main commit — never earlier, never
later — so a main commit that lands after the job's last merge is correctly
excluded every time. See job #1938's decision record / idea for the residual
hypothesis (something outside this code path, e.g. real-world git state at
the time) since the mechanism here is confirmed clean.
"""

from __future__ import annotations

import asyncio
import subprocess

from hyqs.pipeline import gitops


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_origin_and_fork(tmp_path, name: str):
    """An origin repo with one commit, plus a `feature` branch cloned+forked from it."""
    origin = tmp_path / f"{name}-origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("config", "user.email", "test@example.com", cwd=origin)
    _git("config", "user.name", "Test", cwd=origin)
    _git("config", "commit.gpgsign", "false", cwd=origin)
    (origin / "base.py").write_text("v1\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-q", "-m", "init", cwd=origin)

    clone = tmp_path / f"{name}-clone"
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=clone)
    _git("config", "user.name", "Test", cwd=clone)
    _git("config", "commit.gpgsign", "false", cwd=clone)
    _git("checkout", "-q", "-b", "feature", "main", cwd=clone)
    (clone / "feature.py").write_text("feature\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "jobs own change", cwd=clone)
    return origin, clone


def _add_main_commit(origin, filename: str, label: str):
    (origin / filename).write_text(label + "\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-q", "-m", label, cwd=origin)


def test_single_merge_cycle_then_further_advance_does_not_leak(tmp_path):
    """(a) One merge_base_into_worktree cycle, then main advances again.

    main: init -> +fileA (merged into feature) -> +fileB (lands after the
    job's cycle, never merged into feature).

    Finding: `git merge-base(main@fileB, HEAD)` resolves to the main@fileA
    commit (the point the job actually merged in) — not main@init and not
    main@fileB. The triple-dot diff from there shows only feature.py.
    fileB.py does NOT leak into changed_files.
    """
    origin, clone = _init_origin_and_fork(tmp_path, "single")

    _add_main_commit(origin, "fileA.py", "main advance 1 (fileA)")
    asyncio.run(gitops.fresh_base(clone, "main"))
    merge_res = asyncio.run(gitops.merge_base_into_worktree(clone, "main"))
    assert merge_res.ok, f"expected a clean merge, got: {merge_res.stderr}"
    head_oid = asyncio.run(gitops.local_head_oid(clone))
    assert len(head_oid) == 40 and head_oid == head_oid.lower()

    # Further main advance AFTER the job's only merge cycle — this is the file
    # the working hypothesis predicted would leak.
    _add_main_commit(origin, "fileB.py", "main advance 2 (fileB, after cycle)")

    base = asyncio.run(gitops.fresh_base(clone, "main"))
    mb = _git("merge-base", base, "HEAD", cwd=clone)
    mb_subject = _git("log", "-1", "--format=%s", mb, cwd=clone)
    # Confirms the merge-base landed on "main advance 1 (fileA)", the job's
    # actual last-merged-in point — not init and not the later fileB commit.
    assert mb_subject == "main advance 1 (fileA)"

    rows = asyncio.run(gitops.numstat(clone, f"{base}...HEAD"))
    changed_files = {row["path"] for row in rows}

    # No leak: fileB.py (landed after the job's cycle) is excluded.
    assert changed_files == {"feature.py"}


def test_two_merge_cycles_with_intervening_main_commit_does_not_leak(tmp_path):
    """(b) Two sequential merge_base_into_worktree cycles, with an unrelated
    main commit landing between them, then a further advance after the last
    cycle (the exact multi-cycle drift scenario from the working hypothesis).

    main: init -> +fileA (cycle 1) -> +fileB (between cycles, cycle 2 picks
    it up) -> +fileC (after the last cycle, never merged into feature).
    """
    origin, clone = _init_origin_and_fork(tmp_path, "double")

    _add_main_commit(origin, "fileA.py", "main advance 1 (fileA)")
    asyncio.run(gitops.fresh_base(clone, "main"))
    res1 = asyncio.run(gitops.merge_base_into_worktree(clone, "main"))
    assert res1.ok

    _add_main_commit(origin, "fileB.py", "main advance 2 (fileB, between cycles)")
    asyncio.run(gitops.fresh_base(clone, "main"))
    res2 = asyncio.run(gitops.merge_base_into_worktree(clone, "main"))
    assert res2.ok
    head_oid = asyncio.run(gitops.local_head_oid(clone))
    assert len(head_oid) == 40 and head_oid == head_oid.lower()

    _add_main_commit(origin, "fileC.py", "main advance 3 (fileC, after last cycle)")

    base = asyncio.run(gitops.fresh_base(clone, "main"))
    mb = _git("merge-base", base, "HEAD", cwd=clone)
    mb_subject = _git("log", "-1", "--format=%s", mb, cwd=clone)
    # The merge-base lands on the job's own second (most recent) merge-in
    # point, "main advance 2 (fileB, between cycles)" — proving two prior
    # cycles don't leave a stale intermediate merge-base behind.
    assert mb_subject == "main advance 2 (fileB, between cycles)"

    rows = asyncio.run(gitops.numstat(clone, f"{base}...HEAD"))
    changed_files = {row["path"] for row in rows}

    # No leak: fileC.py (landed after the job's last cycle) is excluded, and
    # fileA.py/fileB.py (legitimately merged in) are excluded too — this is
    # purely the job's own delta.
    assert changed_files == {"feature.py"}


def test_merge_verify_effective_base_recompute_does_not_leak(tmp_path):
    """(c) stages/merge_verify.py's effective_base recompute path
    (merge_verify.py:90-98): a real conflict is resolved (setting
    merge_delta_sha to the resolution commit), then main advances again
    before merge_verify.py re-derives `effective_base = merge-base(fresh
    main, HEAD)` and diffs `effective_base..HEAD` (double-dot, not
    triple-dot — mirrored exactly here).
    """
    origin, clone = _init_origin_and_fork(tmp_path, "verify")

    # Both origin and the job edit the same file so the merge conflicts —
    # forcing a real fix_conflict-style resolution commit, matching the
    # merge_delta_sha-setting path in stages/merge.py:92-109.
    (origin / "same.py").write_text("origin version\n")
    _git("add", "-A", cwd=origin)
    _git("commit", "-q", "-m", "main advance 1 (fileA + same.py)", cwd=origin)
    (clone / "same.py").write_text("job version\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "jobs own edit to same.py (will conflict)", cwd=clone)

    asyncio.run(gitops.fresh_base(clone, "main"))
    conflict_res = asyncio.run(gitops.merge_base_into_worktree(clone, "main"))
    assert not conflict_res.ok, "expected same.py to conflict"

    # Resolve, completing the pending merge commit — mirrors fix_conflict's
    # commit_all call in stages/fix.py:100.
    (clone / "same.py").write_text("merged version\n")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "--no-edit", cwd=clone)
    head_oid = asyncio.run(gitops.local_head_oid(clone))
    assert len(head_oid) == 40 and head_oid == head_oid.lower()

    # Further, unrelated main advance AFTER the conflict was resolved but
    # before merge_verify.py runs.
    _add_main_commit(origin, "fileC.py", "main advance 2 (fileC, after conflict resolved)")

    base = asyncio.run(gitops.fresh_base(clone, "main"))
    mb_res = asyncio.run(gitops.git(clone, "merge-base", base, "HEAD"))
    assert mb_res.ok
    effective_base = mb_res.stdout.strip()
    eb_subject = _git("log", "-1", "--format=%s", effective_base, cwd=clone)
    # merge-base recomputes to the job's own conflict-triggering main commit —
    # not the later fileC advance.
    assert eb_subject == "main advance 1 (fileA + same.py)"

    rows = asyncio.run(gitops.numstat(clone, f"{effective_base}..HEAD"))
    changed_files = {row["path"] for row in rows}

    # No leak: fileC.py (landed after the resolution) is excluded. same.py IS
    # present because the job's own conflict-resolution commit legitimately
    # touched it — that's the job's own delta, correctly included.
    assert changed_files == {"feature.py", "same.py"}
