"""Tests for hyqs/pipeline/decisions.py — AUTO-ADR record/prune/digest.

No Postgres fixture: decisions.py never touches JobStore (that's the point —
recording a decision cannot spawn a deploy job/cycle).
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from hyqs.pipeline import decisions, gitops


async def _run(cwd: Path, *args: str) -> None:
    res = await gitops.git(cwd, *args)
    assert res.ok, f"git {args} failed: {res.stderr}"


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await _run(path, "init", "-b", "main")
    await _run(path, "config", "user.email", "test@example.com")
    await _run(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    await _run(path, "add", "-A")
    await _run(path, "commit", "-m", "initial")


async def _make_remote_setup(tmp_path: Path) -> tuple[Path, Path]:
    """Return (managed, upstream): managed is a bare clone with 'origin' -> upstream."""
    seed = tmp_path / "seed"
    await _init_repo(seed)
    upstream = tmp_path / "upstream.git"
    res = await gitops.git(tmp_path, "clone", "--bare", str(seed), str(upstream))
    assert res.ok, res.stderr
    managed = tmp_path / "managed.git"
    res = await gitops.git(tmp_path, "clone", "--bare", str(upstream), str(managed))
    assert res.ok, res.stderr
    return managed, upstream


# ---------------------------------------------------------------------------
# Pure rendering / filename functions
# ---------------------------------------------------------------------------


def test_decision_filename_slugifies_title():
    assert decisions.decision_filename(42, "Add Auto-ADR Support!") == "42-add-auto-adr-support.md"


def test_decision_filename_falls_back_when_title_empty():
    assert decisions.decision_filename(7, "???") == "7.md"


def test_render_decision_includes_all_fields():
    content = decisions.render_decision(
        job_id=1,
        title="Add foo",
        date="2026-07-12",
        summary="Did the thing.",
        files_touched=["a.py", "b.py"],
    )
    assert "# Job #1: Add foo" in content
    assert "**Date:** 2026-07-12" in content
    assert "Did the thing." in content
    assert "- a.py" in content
    assert "- b.py" in content


def test_render_decision_handles_no_files_touched():
    content = decisions.render_decision(
        job_id=1, title="t", date="2026-07-12", summary="s", files_touched=[]
    )
    assert "(none recorded)" in content


# ---------------------------------------------------------------------------
# is_janitorial_job
# ---------------------------------------------------------------------------


def test_is_janitorial_job_true_for_structured_marker():
    assert decisions.is_janitorial_job(title="Add a foo() helper", source_meta={"kind": "gate-fix"})


def test_is_janitorial_job_true_for_title_prefix_fallback():
    assert decisions.is_janitorial_job(
        title="[gate-fix] [symbol-collision] in /home/hyqs/hyqs-ai", source_meta=None
    )


def test_is_janitorial_job_true_for_title_prefix_with_job_header():
    # As parsed from an on-disk decision file's "# Job #<id>: <title>" header.
    assert decisions.is_janitorial_job(
        title="Job #667: [gate-fix] [symbol-collision] in /home/hyqs/hyqs-ai",
        source_meta=None,
    )


def test_is_janitorial_job_false_for_normal_job():
    assert not decisions.is_janitorial_job(title="Add a foo() helper", source_meta={})
    assert not decisions.is_janitorial_job(title="Add a foo() helper", source_meta=None)


# ---------------------------------------------------------------------------
# load_digest
# ---------------------------------------------------------------------------


def test_load_digest_excludes_legacy_gate_fix_files(tmp_path):
    d = tmp_path / ".hyqs" / "decisions"
    d.mkdir(parents=True)
    (d / "1-normal.md").write_text(
        decisions.render_decision(
            job_id=1, title="Add feature", date="2026-07-01", summary="did A", files_touched=[]
        )
    )
    (d / "2-gate-fix.md").write_text(
        decisions.render_decision(
            job_id=2,
            title="[gate-fix] [symbol-collision] in /repo",
            date="2026-07-02",
            summary="did B",
            files_touched=[],
        )
    )
    digest = decisions.load_digest(tmp_path)
    assert "Add feature" in digest
    assert "gate-fix" not in digest


def test_load_digest_empty_when_no_decisions_dir(tmp_path):
    assert decisions.load_digest(tmp_path) == ""


def test_load_digest_lists_newest_first(tmp_path):
    d = tmp_path / ".hyqs" / "decisions"
    d.mkdir(parents=True)
    (d / "1-first.md").write_text(
        decisions.render_decision(
            job_id=1, title="First", date="2026-07-01", summary="did A", files_touched=[]
        )
    )
    (d / "2-second.md").write_text(
        decisions.render_decision(
            job_id=2, title="Second", date="2026-07-02", summary="did B", files_touched=[]
        )
    )
    digest = decisions.load_digest(tmp_path)
    assert digest.index("Second") < digest.index("First")
    assert "did B" in digest
    assert "did A" in digest


def test_load_digest_respects_limit(tmp_path):
    d = tmp_path / ".hyqs" / "decisions"
    d.mkdir(parents=True)
    for i in range(1, 21):
        (d / f"{i}-job.md").write_text(
            decisions.render_decision(
                job_id=i, title=f"Job {i}", date="2026-07-01", summary="s", files_touched=[]
            )
        )
    digest = decisions.load_digest(tmp_path, limit=15)
    assert digest.count("- 2026-07-01") == 15
    assert "Job 20" in digest  # newest
    assert "Job 5" not in digest  # pruned by limit (only 6..20 kept)


def test_load_digest_respects_char_cap(tmp_path):
    d = tmp_path / ".hyqs" / "decisions"
    d.mkdir(parents=True)
    for i in range(1, 6):
        (d / f"{i}-job.md").write_text(
            decisions.render_decision(
                job_id=i, title=f"Job {i}", date="2026-07-01", summary="x" * 200, files_touched=[]
            )
        )
    digest = decisions.load_digest(tmp_path, char_cap=300)
    assert len(digest) <= 320  # cap plus the truncation marker


# ---------------------------------------------------------------------------
# list_decisions / parse_decision_meta / resolve_decision_path / filter_by_epic
# ---------------------------------------------------------------------------


def test_list_decisions_empty_when_no_decisions_dir(tmp_path):
    assert decisions.list_decisions(tmp_path) == []


def test_list_decisions_returns_newest_first(tmp_path):
    d = tmp_path / ".hyqs" / "decisions"
    d.mkdir(parents=True)
    (d / "1-first.md").write_text(
        decisions.render_decision(
            job_id=1, title="First", date="2026-07-01", summary="did A", files_touched=[]
        )
    )
    (d / "3-third.md").write_text(
        decisions.render_decision(
            job_id=3, title="Third", date="2026-07-03", summary="did C", files_touched=[]
        )
    )
    (d / "2-second.md").write_text(
        decisions.render_decision(
            job_id=2, title="Second", date="2026-07-02", summary="did B", files_touched=[]
        )
    )
    result = decisions.list_decisions(tmp_path)
    assert [r["job_id"] for r in result] == [3, 2, 1]


def test_parse_decision_meta_extracts_fields(tmp_path):
    p = tmp_path / "5-add-thing.md"
    p.write_text(
        decisions.render_decision(
            job_id=5,
            title="Add thing",
            date="2026-07-05",
            summary="Shipped the thing.",
            files_touched=["a.py"],
        )
    )
    meta = decisions.parse_decision_meta(p)
    assert meta == {
        "filename": "5-add-thing.md",
        "job_id": 5,
        "title": "Job #5: Add thing",
        "date": "2026-07-05",
        "first_line": "Shipped the thing.",
    }


def test_parse_decision_meta_returns_none_for_unmatched_filename(tmp_path):
    p = tmp_path / "not-a-decision.md"
    p.write_text("hello")
    assert decisions.parse_decision_meta(p) is None


def test_resolve_decision_path_rejects_traversal(tmp_path):
    d = tmp_path / ".hyqs" / "decisions"
    d.mkdir(parents=True)
    (d / "1-x.md").write_text("x")
    assert decisions.resolve_decision_path(tmp_path, "../../etc/passwd") is None
    assert decisions.resolve_decision_path(tmp_path, "not-a-decision.md") is None
    assert decisions.resolve_decision_path(tmp_path, "../1-x.md") is None
    assert decisions.resolve_decision_path(tmp_path, "sub/1-x.md") is None


def test_resolve_decision_path_returns_path_for_valid_filename(tmp_path):
    d = tmp_path / ".hyqs" / "decisions"
    d.mkdir(parents=True)
    (d / "1-x.md").write_text("x")
    resolved = decisions.resolve_decision_path(tmp_path, "1-x.md")
    assert resolved == (d / "1-x.md").resolve()


def test_filter_by_epic_keeps_only_matching_job_ids():
    entries = [
        {"job_id": 1, "title": "a"},
        {"job_id": 2, "title": "b"},
        {"job_id": 3, "title": "c"},
    ]
    job_epic_map = {1: 10, 2: 20, 3: 10}
    result = decisions.filter_by_epic(entries, job_epic_map, 10)
    assert [r["job_id"] for r in result] == [1, 3]


# ---------------------------------------------------------------------------
# _prune
# ---------------------------------------------------------------------------


def test_prune_keeps_only_newest_by_job_id(tmp_path):
    d = tmp_path / "decisions"
    d.mkdir()
    for i in range(1, 6):
        (d / f"{i}-x.md").write_text("x")
    decisions._prune(d, retention=3)
    remaining = sorted(p.name for p in d.glob("*.md"))
    assert remaining == ["3-x.md", "4-x.md", "5-x.md"]


# ---------------------------------------------------------------------------
# record_decision — remote (push) path
# ---------------------------------------------------------------------------


def test_record_decision_writes_and_pushes_one_file(tmp_path):
    managed, upstream = asyncio.run(_make_remote_setup(tmp_path))
    ok = asyncio.run(
        decisions.record_decision(
            managed,
            "main",
            tmp_path / "worktrees",
            job_id=1,
            title="Add feature",
            summary="Shipped the feature.",
            files_touched=["hyqs/foo.py"],
        )
    )
    assert ok is True
    show = asyncio.run(gitops.git(upstream, "show", "main:.hyqs/decisions/1-add-feature.md"))
    assert show.ok, show.stderr
    assert "Shipped the feature." in show.stdout
    # the disposable worktree must be cleaned up
    assert not (tmp_path / "worktrees" / "job-1-decision").exists()


def test_record_decision_prunes_to_retention_over_remote(tmp_path):
    managed, upstream = asyncio.run(_make_remote_setup(tmp_path))
    for i in range(1, 6):
        ok = asyncio.run(
            decisions.record_decision(
                managed,
                "main",
                tmp_path / "worktrees",
                job_id=i,
                title=f"job {i}",
                summary="s",
                files_touched=[],
                retention=3,
            )
        )
        assert ok is True
    ls = asyncio.run(gitops.git(upstream, "ls-tree", "-r", "--name-only", "main"))
    decision_files = [p for p in ls.stdout.splitlines() if p.startswith(".hyqs/decisions/")]
    assert len(decision_files) == 3
    assert sorted(decision_files) == [
        ".hyqs/decisions/3-job-3.md",
        ".hyqs/decisions/4-job-4.md",
        ".hyqs/decisions/5-job-5.md",
    ]


def test_two_concurrent_record_decisions_produce_two_conflict_free_files(tmp_path):
    managed, upstream = asyncio.run(_make_remote_setup(tmp_path))

    async def _both():
        return await asyncio.gather(
            decisions.record_decision(
                managed,
                "main",
                tmp_path / "worktrees",
                job_id=1,
                title="alpha",
                summary="did alpha",
                files_touched=[],
            ),
            decisions.record_decision(
                managed,
                "main",
                tmp_path / "worktrees",
                job_id=2,
                title="beta",
                summary="did beta",
                files_touched=[],
            ),
        )

    results = asyncio.run(_both())
    assert results == [True, True]
    ls = asyncio.run(gitops.git(upstream, "ls-tree", "-r", "--name-only", "main"))
    decision_files = {p for p in ls.stdout.splitlines() if p.startswith(".hyqs/decisions/")}
    assert decision_files == {".hyqs/decisions/1-alpha.md", ".hyqs/decisions/2-beta.md"}


# ---------------------------------------------------------------------------
# record_decision — local (no remote) path
# ---------------------------------------------------------------------------


def test_record_decision_local_repo_no_remote(tmp_path):
    managed = tmp_path / "local"
    asyncio.run(_init_repo(managed))
    ok = asyncio.run(
        decisions.record_decision(
            managed,
            "main",
            tmp_path / "worktrees",
            job_id=1,
            title="local change",
            summary="did it locally",
            files_touched=["x.py"],
        )
    )
    assert ok is True
    content = (managed / ".hyqs" / "decisions" / "1-local-change.md").read_text()
    assert "did it locally" in content


# ---------------------------------------------------------------------------
# Zero interaction with JobStore — proves this can't spawn a deploy job/cycle
# ---------------------------------------------------------------------------


def test_decisions_module_has_no_store_interaction():
    source = inspect.getsource(decisions)
    assert "JobStore" not in source
    assert "import store" not in source
    assert "from .store" not in source
    assert "from hyqs.pipeline.store" not in source
