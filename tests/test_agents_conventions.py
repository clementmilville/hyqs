"""Tests for the self-sustaining-CONVENTIONS.md standing rule + reviewer scrutiny note.

No Postgres needed — mocks the backend, matching the pattern used by
tests/test_agents_personas.py and tests/test_stages_scope_manifest.py.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline import agents
from hyqs.pipeline.models import Job, JobStatus, Stage, Usage


def _make_backend(text: str = "") -> MagicMock:
    result = MagicMock()
    result.text = text
    result.usage = Usage()
    backend = MagicMock()
    backend.run = AsyncMock(return_value=result)
    return backend


def _wrap(data: dict) -> str:
    return f"<<<RESULT_JSON>>>\n{json.dumps(data)}\n<<<END_RESULT>>>"


_VERDICT = {"verdict": "pass", "summary": "ok", "findings": []}


def test_build_sys_contains_standing_rule():
    assert "NEVER weaken" in agents.BUILD_SYS


def test_build_sys_limits_execution_to_targeted_tests():
    assert "smallest directly relevant test targets" in agents.BUILD_SYS
    assert "Do not run the full test suite" in agents.BUILD_SYS
    assert "Do not perform progressively broader" in agents.BUILD_SYS
    assert "Deterministic LINT runs next" in agents.BUILD_SYS
    assert "Deterministic TEST owns authoritative" in agents.BUILD_SYS
    assert "Never weaken or omit tests" in agents.BUILD_SYS


def test_build_sys_enforces_reuse_layering_and_downstream_ownership():
    assert "Reuse verified project symbols, fixtures" in agents.BUILD_SYS
    assert "Do not copy" in agents.BUILD_SYS
    assert "Do not run formatters or linters" in agents.BUILD_SYS
    assert "Deterministic TEST owns authoritative" in agents.BUILD_SYS
    assert "Do not perform a broad code review" in agents.BUILD_SYS
    assert "inspect the final diff" in agents.BUILD_SYS
    assert "Do not claim that lint, broad tests, security" in agents.BUILD_SYS


def test_plan_sys_enforces_stage_ownership_and_focused_investigation():
    assert "Do not edit files or run tests" in agents.PLAN_SYS
    assert "repository file map and symbol index first" in agents.PLAN_SYS
    assert "Do not perform broad exploratory scans" in agents.PLAN_SYS
    assert "BUILD owns implementation and regression tests" in agents.PLAN_SYS
    assert "Deterministic LINT and TEST" in agents.PLAN_SYS


def test_plan_sys_requires_verified_reuse_and_minimal_test_coverage():
    assert "Every item in reuses must be verified" in agents.PLAN_SYS
    assert "Never infer a fully-qualified symbol name" in agents.PLAN_SYS
    assert "smallest regression coverage" in agents.PLAN_SYS
    assert "Keep tests alongside" in agents.PLAN_SYS
    assert "Do not duplicate lower-level cryptographic" in agents.PLAN_SYS


def test_fix_sys_contains_standing_rule():
    assert "NEVER weaken" in agents.FIX_SYS


def test_fix_sys_enforces_convergence_integrity_and_revalidation_ownership():
    assert "Preserve every original story" in agents.FIX_SYS
    assert "Address only the reported blocker" in agents.FIX_SYS
    assert "Never delete, skip, weaken" in agents.FIX_SYS
    assert "Run only the smallest targeted reproducer" in agents.FIX_SYS
    assert "Do not run formatters, linters, broad suites" in agents.FIX_SYS
    assert "pipeline restarts deterministic LINT and TEST" in agents.FIX_SYS
    assert "Do not claim that lint, broad tests, review" in agents.FIX_SYS


def test_conflict_fix_sys_omits_standing_rule():
    assert "NEVER weaken" not in agents.CONFLICT_FIX_SYS


def test_review_includes_scrutiny_note_when_conventions_changed(tmp_path):
    backend = _make_backend(_wrap(_VERDICT))

    asyncio.run(
        agents.review(
            backend,
            str(tmp_path),
            "main",
            idea="Add a new breakpoint system",
            conventions_changed=True,
        )
    )

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "Constitution Scrutiny" in prompt
    assert "Add a new breakpoint system" in prompt


def test_review_omits_scrutiny_note_by_default(tmp_path):
    backend = _make_backend(_wrap(_VERDICT))

    asyncio.run(agents.review(backend, str(tmp_path), "main"))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "Constitution Scrutiny" not in prompt


def test_review_omits_scrutiny_note_when_conventions_unchanged(tmp_path):
    backend = _make_backend(_wrap(_VERDICT))

    asyncio.run(
        agents.review(
            backend,
            str(tmp_path),
            "main",
            idea="Add a new breakpoint system",
            conventions_changed=False,
        )
    )

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "Constitution Scrutiny" not in prompt


def test_review_sys_separates_functional_and_downstream_ownership():
    assert "functional correctness, completeness, integration" in agents.REVIEW_SYS
    assert "Do not run tests, builds, formatters" in agents.REVIEW_SYS
    assert "SECURITY owns exploitability" in agents.REVIEW_SYS
    assert "DESIGN REVIEW owns visual quality" in agents.REVIEW_SYS
    assert "normal valid use produces incorrect state" in agents.REVIEW_SYS
    assert "regression coverage explicitly required by the plan" in agents.REVIEW_SYS
    assert "Do not fail on speculation" in agents.REVIEW_SYS


def test_security_sys_preserves_independent_zero_tolerance_defense_in_depth():
    assert "either layer can block" in agents.SECURITY_SYS
    assert "Do not assume those scanners found everything" in agents.SECURITY_SYS
    assert "do not ignore a vulnerability merely because it is scanner-detectable" in (
        agents.SECURITY_SYS
    )
    assert "own complete semantic inspection" in agents.SECURITY_SYS
    assert "pre-existing dangerous behavior newly reachable" in agents.SECURITY_SYS
    assert "attacker capability or untrusted input" in agents.SECURITY_SYS
    assert "reachable sink or state transition" in agents.SECURITY_SYS
    assert "never permits a confirmed vulnerability" in agents.SECURITY_SYS


def test_review_includes_claims_check_when_is_fix_job(tmp_path):
    backend = _make_backend(_wrap(_VERDICT))

    asyncio.run(
        agents.review(
            backend,
            str(tmp_path),
            "main",
            idea="Rename the widget module to gadget across 6 files",
            title="Rename widget to gadget",
            is_fix_job=True,
        )
    )

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "Claims-vs-Diff Check" in prompt
    assert "Rename the widget module to gadget across 6 files" in prompt
    assert "Rename widget to gadget" in prompt


def test_review_omits_claims_check_by_default(tmp_path):
    backend = _make_backend(_wrap(_VERDICT))

    asyncio.run(agents.review(backend, str(tmp_path), "main"))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "Claims-vs-Diff Check" not in prompt


def test_review_omits_claims_check_when_is_fix_job_false(tmp_path):
    backend = _make_backend(_wrap(_VERDICT))

    asyncio.run(
        agents.review(
            backend,
            str(tmp_path),
            "main",
            idea="Rename the widget module to gadget across 6 files",
            title="Rename widget to gadget",
            is_fix_job=False,
        )
    )

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "Claims-vs-Diff Check" not in prompt


def _make_job(**kwargs) -> Job:
    base = dict(
        id=1,
        idea="Add a new breakpoint system",
        repo_path="/fake/repo",
        chat_id=99,
        stage=Stage.TEST,
        status=JobStatus.RUNNING,
        project_id=1,
        branch="job-1",
        attempts=0,
    )
    base.update(kwargs)
    return Job(**base)


def _make_runner() -> MagicMock:
    rn = MagicMock()
    rn.worktrees = MagicMock()
    rn.timeout = 60
    rn.store = MagicMock()
    rn.store.get_symbol_index_text = MagicMock(return_value="")
    rn.store.record_usage = MagicMock()
    rn.notify = AsyncMock()
    rn._backend = MagicMock(return_value=MagicMock())
    rn._managed_repo = AsyncMock(return_value="/fake/repo")
    rn._event = MagicMock()
    rn._record_resource = MagicMock()
    rn._retry_or_fail = AsyncMock()
    return rn


def test_review_stage_passes_conventions_changed_true_when_in_diff():
    from hyqs.pipeline.stages import review

    job = _make_job()
    rn = _make_runner()

    with (
        patch("hyqs.pipeline.stages.review.gitops.default_branch", AsyncMock(return_value="main")),
        patch(
            "hyqs.pipeline.stages.review.gitops.numstat",
            AsyncMock(
                return_value=[
                    {"path": "CONVENTIONS.md"},
                    {"path": "hyqs/pipeline/store.py"},
                ]
            ),
        ),
        patch(
            "hyqs.pipeline.stages.review.run_guarded_gate",
            AsyncMock(return_value=(_VERDICT, Usage())),
        ) as guarded_gate,
        patch("hyqs.pipeline.stages.review.github.has_remote", AsyncMock(return_value=False)),
        patch("hyqs.pipeline.stages.review.decisions.load_digest", MagicMock(return_value="")),
    ):
        asyncio.run(review.run(rn, job))

    guarded_gate.assert_called_once()
    assert guarded_gate.call_args.kwargs["conventions_changed"] is True
    assert guarded_gate.call_args.kwargs["idea"] == job.idea


def test_review_stage_passes_conventions_changed_false_when_not_in_diff():
    from hyqs.pipeline.stages import review

    job = _make_job()
    rn = _make_runner()

    with (
        patch("hyqs.pipeline.stages.review.gitops.default_branch", AsyncMock(return_value="main")),
        patch(
            "hyqs.pipeline.stages.review.gitops.numstat",
            AsyncMock(return_value=[{"path": "hyqs/pipeline/store.py"}]),
        ),
        patch(
            "hyqs.pipeline.stages.review.run_guarded_gate",
            AsyncMock(return_value=(_VERDICT, Usage())),
        ) as guarded_gate,
        patch("hyqs.pipeline.stages.review.github.has_remote", AsyncMock(return_value=False)),
        patch("hyqs.pipeline.stages.review.decisions.load_digest", MagicMock(return_value="")),
    ):
        asyncio.run(review.run(rn, job))

    guarded_gate.assert_called_once()
    assert guarded_gate.call_args.kwargs["conventions_changed"] is False


def test_review_stage_passes_is_fix_job_true_when_ai_fix_for_set():
    from hyqs.pipeline.stages import review

    job = _make_job(source_meta={"ai_fix_for": 5})
    rn = _make_runner()

    with (
        patch("hyqs.pipeline.stages.review.gitops.default_branch", AsyncMock(return_value="main")),
        patch(
            "hyqs.pipeline.stages.review.gitops.numstat",
            AsyncMock(return_value=[{"path": "hyqs/pipeline/store.py"}]),
        ),
        patch(
            "hyqs.pipeline.stages.review.run_guarded_gate",
            AsyncMock(return_value=(_VERDICT, Usage())),
        ) as guarded_gate,
        patch("hyqs.pipeline.stages.review.github.has_remote", AsyncMock(return_value=False)),
        patch("hyqs.pipeline.stages.review.decisions.load_digest", MagicMock(return_value="")),
    ):
        asyncio.run(review.run(rn, job))

    guarded_gate.assert_called_once()
    assert guarded_gate.call_args.kwargs["is_fix_job"] is True
    assert guarded_gate.call_args.kwargs["title"] == job.title


def test_review_stage_passes_is_fix_job_false_when_no_source_meta():
    from hyqs.pipeline.stages import review

    job = _make_job(source_meta=None)
    rn = _make_runner()

    with (
        patch("hyqs.pipeline.stages.review.gitops.default_branch", AsyncMock(return_value="main")),
        patch(
            "hyqs.pipeline.stages.review.gitops.numstat",
            AsyncMock(return_value=[{"path": "hyqs/pipeline/store.py"}]),
        ),
        patch(
            "hyqs.pipeline.stages.review.run_guarded_gate",
            AsyncMock(return_value=(_VERDICT, Usage())),
        ) as guarded_gate,
        patch("hyqs.pipeline.stages.review.github.has_remote", AsyncMock(return_value=False)),
        patch("hyqs.pipeline.stages.review.decisions.load_digest", MagicMock(return_value="")),
    ):
        asyncio.run(review.run(rn, job))

    guarded_gate.assert_called_once()
    assert guarded_gate.call_args.kwargs["is_fix_job"] is False
    assert guarded_gate.call_args.kwargs["title"] == job.title
