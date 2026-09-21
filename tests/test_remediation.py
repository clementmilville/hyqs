from __future__ import annotations

from hyqs.pipeline.collision import _parse_alembic_revision
from hyqs.pipeline.remediation import (
    build_ai_fix_idea,
    collect_remediation_candidate_evidence,
    compute_chain_dependencies,
    format_conflict_reason,
    format_remediation_badge,
    generate_alembic_merge_revision,
)


def test_format_remediation_badge_none_and_present_are_distinct():
    none_badge = format_remediation_badge(None)
    present_badge = format_remediation_badge(42)

    assert none_badge != present_badge
    assert none_badge == "No active remediation"
    assert present_badge == "Remediation in progress (job #42)"


def test_format_remediation_badge_is_deterministic():
    assert format_remediation_badge(7) == format_remediation_badge(7)


def test_format_conflict_reason_mentions_both_job_ids():
    reason = format_conflict_reason(1, 2)

    assert "1" in reason
    assert "2" in reason


def test_build_ai_fix_idea_includes_job_ref_and_analyst_idea():
    idea = build_ai_fix_idea(543, "build", "", "swap the mock for a real client", None, None)

    assert idea.startswith("[ai-fix] swap the mock for a real client")
    assert "job #543" in idea
    assert "swap the mock for a real client" in idea


def test_build_ai_fix_idea_includes_failure_text():
    idea = build_ai_fix_idea(543, "build", "AssertionError: boom", "fix it", None, None)

    assert "AssertionError: boom" in idea


def test_build_ai_fix_idea_appends_matched_story_text_verbatim():
    stories = [
        {
            "id": "S1",
            "title": "Add helper",
            "task": "Add build_ai_fix_idea to remediation.py verbatim task text",
            "acceptance": "tests assert the exact acceptance string appears",
            "target_files": ["hyqs/pipeline/remediation.py"],
        },
        {
            "id": "S2",
            "title": "Unrelated",
            "task": "unrelated task text",
            "acceptance": "unrelated acceptance text",
            "target_files": [],
        },
    ]

    idea = build_ai_fix_idea(543, "build", "", "fix S1", stories, ["S1"])

    assert idea.startswith("[ai-fix] ")
    assert "Add build_ai_fix_idea to remediation.py verbatim task text" in idea
    assert "tests assert the exact acceptance string appears" in idea
    assert "unrelated task text" not in idea
    assert "unrelated acceptance text" not in idea


def test_build_ai_fix_idea_no_story_text_when_covers_stories_missing_or_unmatched():
    stories = [{"id": "S1", "task": "t", "acceptance": "a", "target_files": []}]

    no_covers = build_ai_fix_idea(543, "build", "", "fix it", stories, None)
    no_match = build_ai_fix_idea(543, "build", "", "fix it", stories, ["S9"])

    assert "Task: t" not in no_covers
    assert "Acceptance: a" not in no_covers
    assert "Task: t" not in no_match
    assert "Acceptance: a" not in no_match

    for idea in (no_covers, no_match):
        assert "job #543" in idea
        assert "Analyst notes:\nfix it" in idea


def test_collect_remediation_candidate_evidence_carries_structured_parent_failure_and_review_paths():
    evidence = collect_remediation_candidate_evidence(
        parent_idea="Parent\nFiles: hyqs/pipeline/store.py",
        parent_plan={
            "stories": [
                {"id": "S1", "target_files": ["hyqs/pipeline/supervisor.py"]},
                {"id": "S2", "target_files": ["hyqs/web/app.py"]},
            ]
        },
        covers_stories=["S1"],
        failure_text="tests/test_store.py:42: AssertionError",
        failure_detail={"traceback": "hyqs/pipeline/store.py:10"},
        reviews=[{"finding": "Update hyqs/pipeline/models.py"}],
        events=[{"detail": {"unexpected_paths": ["hyqs/pipeline/runner.py"]}}],
    )

    assert evidence["parent_manifest"] == [
        "hyqs/pipeline/supervisor.py",
        "hyqs/web/app.py",
        "hyqs/pipeline/store.py",
    ]
    assert evidence["covered_story"] == ["hyqs/pipeline/supervisor.py"]
    assert evidence["failure_path"] == ["tests/test_store.py"]
    assert "hyqs/pipeline/models.py" in evidence["reviewer_path"]
    assert evidence["rejected_diff"] == ["hyqs/pipeline/runner.py"]


def test_compute_chain_dependencies_auto_serializes_overlapping_siblings():
    entries = [
        {"idea": "fix a\n\nFiles: hyqs/pipeline/remediation.py"},
        {"idea": "fix b\n\nFiles: hyqs/pipeline/remediation.py"},
    ]

    assert compute_chain_dependencies(entries) == [[], [0]]


def test_compute_chain_dependencies_leaves_disjoint_siblings_parallel():
    entries = [
        {"idea": "fix a\n\nFiles: hyqs/pipeline/remediation.py"},
        {"idea": "fix b\n\nFiles: hyqs/web/app.py"},
    ]

    assert compute_chain_dependencies(entries) == [[], []]


def test_compute_chain_dependencies_three_way_overlap_forms_linear_chain_not_cycle():
    entries = [
        {"idea": "fix a\n\nFiles: hyqs/pipeline/remediation.py"},
        {"idea": "fix b\n\nFiles: hyqs/pipeline/remediation.py"},
        {"idea": "fix c\n\nFiles: hyqs/pipeline/remediation.py"},
    ]

    assert compute_chain_dependencies(entries) == [[], [0], [0, 1]]


def test_compute_chain_dependencies_preserves_declared_depends_on_without_duplicating():
    entries = [
        {"idea": "fix a\n\nFiles: hyqs/pipeline/remediation.py"},
        {"idea": "fix b\n\nFiles: hyqs/pipeline/remediation.py", "depends_on": [0]},
    ]

    assert compute_chain_dependencies(entries) == [[], [0]]


def test_compute_chain_dependencies_no_files_label_produces_no_auto_edges():
    entries = [{"idea": "fix a"}, {"idea": "fix b"}]

    assert compute_chain_dependencies(entries) == [[], []]


def test_generate_alembic_merge_revision_is_order_independent():
    forward = generate_alembic_merge_revision(["b", "a"])
    reverse = generate_alembic_merge_revision(["a", "b"])

    assert forward == reverse


def test_generate_alembic_merge_revision_filename_matches_content_revision():
    filename, content = generate_alembic_merge_revision(["a", "b"])

    assert filename.endswith("_merge_heads.py")
    assert filename.startswith(content.split('revision = "')[1].split('"')[0])


def test_generate_alembic_merge_revision_parseable_with_sorted_down_revision(tmp_path):
    filename, content = generate_alembic_merge_revision(["b", "a"])
    path = tmp_path / filename
    path.write_text(content)

    revision, down_revision = _parse_alembic_revision(path)

    assert revision is not None
    assert down_revision == ("a", "b")


def test_generate_alembic_merge_revision_has_noop_upgrade_and_downgrade():
    _, content = generate_alembic_merge_revision(["a", "b"])

    assert "def upgrade():\n    pass" in content
    assert "def downgrade():\n    pass" in content
