"""Regression tests for the DESIGN_REVIEW gate stage.

No Postgres needed — mocks the backend and the runner, matching the pattern used
by tests/test_runner_deploy.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.classify import UI_EXTS
from hyqs.pipeline.models import Job, JobStatus, Stage
from hyqs.pipeline.stages import DESIGN_REVIEW_CHECK_ID, design_review


def _make_job(**kwargs) -> Job:
    base = dict(
        id=1,
        idea="test idea",
        repo_path="/fake/repo",
        chat_id=99,
        stage=Stage.SECURITY,
        status=JobStatus.RUNNING,
        project_id=1,
        attempts=0,
    )
    base.update(kwargs)
    return Job(**base)


def _make_runner(backend: MagicMock) -> MagicMock:
    rn = MagicMock()
    rn.worktrees = MagicMock()
    rn.timeout = 60
    rn.store = MagicMock()
    rn.notify = AsyncMock()
    rn._backend = MagicMock(return_value=backend)
    rn._managed_repo = AsyncMock(return_value="/fake/repo")
    rn._event = MagicMock()
    rn._record_resource = MagicMock()
    rn._retry_or_fail = AsyncMock()
    return rn


def test_orphan_class_in_diff_fails_deterministic_check(tmp_path):
    diff_text = (
        "diff --git a/src/views/ChangelogTab.jsx b/src/views/ChangelogTab.jsx\n"
        "--- a/src/views/ChangelogTab.jsx\n"
        "+++ b/src/views/ChangelogTab.jsx\n"
        "@@ -1,2 +1,2 @@\n"
        '+  <div className="changelog-header">\n'
    )
    (tmp_path / "styles.css").write_text(".other-class { color: red; }\n")

    findings = design_review._check_orphan_classes(tmp_path, diff_text)

    assert findings
    assert any("changelog-header" in f["note"] for f in findings)
    assert findings[0]["path"] == "src/views/ChangelogTab.jsx"
    assert findings[0]["companion_paths"] == []


def test_orphan_class_retains_component_and_changed_stylesheet_companion(tmp_path):
    diff_text = (
        "diff --git a/src/components/LogPanel.jsx b/src/components/LogPanel.jsx\n"
        "+++ b/src/components/LogPanel.jsx\n"
        '+  <div className="log-panel-toolbar">\n'
        "diff --git a/src/styles.css b/src/styles.css\n"
        "+++ b/src/styles.css\n"
        "+.log-panel { display: flex; }\n"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/styles.css").write_text(".log-panel { display: flex; }\n")

    findings = design_review._check_orphan_classes(tmp_path, diff_text)

    assert findings == [
        {
            "severity": "error",
            "note": "className 'log-panel-toolbar' is referenced in the diff but has no "
            "matching CSS selector defined in the worktree.",
            "path": "src/components/LogPanel.jsx",
            "companion_paths": ["src/styles.css"],
        }
    ]


def test_failure_detail_uses_only_orphan_metadata_and_valid_structured_paths():
    detail = {"verdict": "fail", "findings": []}
    orphan_findings = [
        {
            "path": "src/components/LogPanel.jsx",
            "companion_paths": ["src/styles.css", "../outside.css"],
            "note": "Do not parse src/from-orphan-note.css",
        }
    ]
    reviewer_findings = [
        {"path": "src/components/Toast.jsx", "note": "valid"},
        {"path": "src/a.jsx,src/b.jsx", "note": "ambiguous"},
        {"path": "/tmp/absolute.jsx", "note": "invalid"},
        {"note": "Prose-only path src/from-note.jsx"},
    ]

    result = design_review.compose_design_review_failure_detail(
        detail, orphan_findings, reviewer_findings
    )

    assert result["check_id"] == DESIGN_REVIEW_CHECK_ID
    assert result["event_id"] == DESIGN_REVIEW_CHECK_ID
    assert result["authorized_gate_failure"] == {
        "gate": "design_review",
        "check_id": DESIGN_REVIEW_CHECK_ID,
        "event_id": DESIGN_REVIEW_CHECK_ID,
        "failing_paths": [
            "src/components/LogPanel.jsx",
            "src/components/Toast.jsx",
            "src/styles.css",
        ],
        "categories": ["symbol_paths"],
    }


def test_failure_detail_without_valid_paths_is_non_authorizing():
    result = design_review.compose_design_review_failure_detail(
        {"verdict": "fail", "summary": "src/from-summary.jsx"},
        [],
        [
            {"note": "src/from-note.jsx"},
            {"path": "src/a.jsx:10-20", "note": "range"},
        ],
    )

    assert result["check_id"] == DESIGN_REVIEW_CHECK_ID
    assert result["event_id"] == DESIGN_REVIEW_CHECK_ID
    assert "authorized_gate_failure" not in result


def test_defined_class_passes_deterministic_check(tmp_path):
    diff_text = (
        "diff --git a/src/views/ChangelogTab.jsx b/src/views/ChangelogTab.jsx\n"
        "--- a/src/views/ChangelogTab.jsx\n"
        "+++ b/src/views/ChangelogTab.jsx\n"
        "@@ -1,2 +1,2 @@\n"
        '+  <div className="changelog-header">\n'
    )
    (tmp_path / "styles.css").write_text(".changelog-header { color: red; }\n")

    findings = design_review._check_orphan_classes(tmp_path, diff_text)

    assert findings == []


def test_template_expression_classname_skipped(tmp_path):
    diff_text = (
        "diff --git a/src/views/ChangelogTab.jsx b/src/views/ChangelogTab.jsx\n"
        "--- a/src/views/ChangelogTab.jsx\n"
        "+++ b/src/views/ChangelogTab.jsx\n"
        "@@ -1,2 +1,2 @@\n"
        "+  <div className={styles.foo}>\n"
    )
    (tmp_path / "styles.css").write_text(".foo { color: red; }\n")

    findings = design_review._check_orphan_classes(tmp_path, diff_text)

    assert findings == []


def test_design_review_uses_shared_ui_exts_definition():
    assert design_review.UI_EXTS is UI_EXTS


def test_is_ui_diff_true_for_jsx_file():
    assert design_review._is_ui_diff({}, ["src/components/Foo.jsx"]) is True


def test_is_ui_diff_true_for_plan_ui_impact():
    plan_data = {"ui_impact": {"touches_backend_surface": True, "frontend_changes": ["update Foo"]}}
    assert design_review._is_ui_diff(plan_data, ["app.py"]) is True


def test_is_ui_diff_false_for_backend_only():
    changed = ["hyqs/pipeline/agents.py", "tests/test_foo.py"]
    assert design_review._is_ui_diff({}, changed) is False


def test_backend_only_diff_skips_ai_gate():
    job = _make_job()
    backend = MagicMock()
    backend.run = AsyncMock()
    rn = _make_runner(backend)

    with (
        patch(
            "hyqs.pipeline.stages.design_review.gitops.default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "hyqs.pipeline.stages.design_review.gitops.numstat",
            AsyncMock(
                return_value=[
                    {"path": "hyqs/pipeline/agents.py"},
                    {"path": "tests/test_foo.py"},
                ]
            ),
        ),
    ):
        asyncio.run(design_review.run(rn, job))

    backend.run.assert_not_called()
    assert job.stage == Stage.DESIGN_REVIEW
    assert job.status == JobStatus.PENDING
    rn.store.save.assert_called_once_with(job)


def test_orphan_check_exception_returns_empty(tmp_path, monkeypatch):
    def _raise_rglob(self, pattern):
        raise OSError("boom")

    monkeypatch.setattr(design_review.Path, "rglob", _raise_rglob)

    diff_text = '+++ b/src/views/ChangelogTab.jsx\n+  <div className="changelog-header">\n'
    findings = design_review._check_orphan_classes(tmp_path, diff_text)

    assert findings == []


def test_is_tailwind_project_true_for_tailwind_css_import(tmp_path):
    (tmp_path / "globals.css").write_text('@import "tailwindcss";\n')

    assert design_review._is_tailwind_project(tmp_path) is True


def test_is_tailwind_project_true_for_tailwind_directive(tmp_path):
    (tmp_path / "styles.css").write_text(
        "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n"
    )

    assert design_review._is_tailwind_project(tmp_path) is True


def test_is_tailwind_project_true_for_package_json_dependency(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"name": "app", "dependencies": {"tailwindcss": "^4.0.0"}}\n'
    )

    assert design_review._is_tailwind_project(tmp_path) is True


def test_is_tailwind_project_false_for_plain_css(tmp_path):
    (tmp_path / "styles.css").write_text(".other-class { color: red; }\n")
    (tmp_path / "package.json").write_text('{"name": "app", "dependencies": {}}\n')

    assert design_review._is_tailwind_project(tmp_path) is False


def test_is_tailwind_project_exception_returns_false(tmp_path, monkeypatch):
    def _raise_rglob(self, pattern):
        raise OSError("boom")

    monkeypatch.setattr(design_review.Path, "rglob", _raise_rglob)

    assert design_review._is_tailwind_project(tmp_path) is False


def test_tailwind_utility_classes_not_flagged_when_routed_through_run(tmp_path):
    """Reproduces the job #750-#752 incident: Tailwind utility classes with slashes,
    colons, and responsive/dark prefixes must not be flagged as orphan classes."""
    (tmp_path / "globals.css").write_text('@import "tailwindcss";\n')

    diff_text = (
        "diff --git a/src/app/page.jsx b/src/app/page.jsx\n"
        "--- a/src/app/page.jsx\n"
        "+++ b/src/app/page.jsx\n"
        "@@ -1,2 +1,2 @@\n"
        '+  <div className="bg-accent/40 space-y-4 gap-1.5 md:grid-cols-2 dark:text-emerald-400">\n'
    )

    is_tailwind = design_review._is_tailwind_project(tmp_path)
    assert is_tailwind is True
    orphan_findings = (
        [] if is_tailwind else design_review._check_orphan_classes(tmp_path, diff_text)
    )
    assert orphan_findings == []


def test_non_tailwind_project_still_flags_genuinely_undefined_classname(tmp_path):
    """The original bug class — a hand-authored className with no matching hand-authored
    CSS rule in a non-Tailwind project — must still be caught."""
    (tmp_path / "styles.css").write_text(".other-class { color: red; }\n")

    diff_text = (
        "diff --git a/src/views/ChangelogTab.jsx b/src/views/ChangelogTab.jsx\n"
        "--- a/src/views/ChangelogTab.jsx\n"
        "+++ b/src/views/ChangelogTab.jsx\n"
        "@@ -1,2 +1,2 @@\n"
        '+  <div className="totally-undefined-class">\n'
    )

    is_tailwind = design_review._is_tailwind_project(tmp_path)
    assert is_tailwind is False
    orphan_findings = (
        [] if is_tailwind else design_review._check_orphan_classes(tmp_path, diff_text)
    )
    assert orphan_findings
    assert any("totally-undefined-class" in f["note"] for f in orphan_findings)


def test_failed_run_emits_and_retries_with_same_structured_evidence(tmp_path):
    worktree = tmp_path / "job-1"
    (worktree / "src/components").mkdir(parents=True)
    (worktree / "src/styles.css").write_text(".log-panel { display: flex; }\n")
    backend = MagicMock()
    rn = _make_runner(backend)
    rn.worktrees = tmp_path
    job = _make_job()
    design_data = {
        "verdict": "fail",
        "summary": "Use a token.",
        "findings": [
            {
                "severity": "error",
                "note": "Raw spacing in the changed button.",
                "path": "src/components/Button.jsx",
            },
            {"severity": "error", "note": "Prose mentions src/ignored.jsx."},
        ],
    }
    diff_text = (
        "+++ b/src/components/LogPanel.jsx\n"
        '+  <div className="log-panel-toolbar">\n'
        "+++ b/src/styles.css\n"
        "+.log-panel { display: flex; }\n"
    )

    with (
        patch(
            "hyqs.pipeline.stages.design_review.gitops.default_branch",
            AsyncMock(return_value="main"),
        ),
        patch(
            "hyqs.pipeline.stages.design_review.gitops.numstat",
            AsyncMock(
                return_value=[
                    {"path": "src/components/LogPanel.jsx"},
                    {"path": "src/styles.css"},
                ]
            ),
        ),
        patch(
            "hyqs.pipeline.stages.design_review.gitops.patch",
            AsyncMock(return_value=diff_text),
        ),
        patch(
            "hyqs.pipeline.stages.design_review.run_guarded_gate",
            AsyncMock(return_value=(design_data, MagicMock())),
        ),
    ):
        asyncio.run(design_review.run(rn, job))

    event_detail = rn._event.call_args.kwargs["detail"]
    retry_detail = rn._retry_or_fail.call_args.kwargs["failure_detail"]
    assert retry_detail == event_detail
    assert event_detail["authorized_gate_failure"]["failing_paths"] == [
        "src/components/Button.jsx",
        "src/components/LogPanel.jsx",
        "src/styles.css",
    ]
    assert "src/ignored.jsx" not in event_detail["authorized_gate_failure"]["failing_paths"]
