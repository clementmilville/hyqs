"""Regression tests for hyqs.pipeline.collision.check_symbol_collisions."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyqs.pipeline.collision import (
    AUTHORIZED_AMENDMENT_GATES,
    AUTHORIZED_GATE_EVIDENCE_CATEGORIES,
    SCOPE_AMENDMENT_CUMULATIVE_LIMIT,
    SCOPE_AMENDMENT_PER_CYCLE_LIMIT,
    PlanningCandidate,
    QueueSurveyResult,
    _is_alembic_merge_resolution,
    _is_frontend_path,
    _lockfile_dir_in_scope,
    build_planning_candidates,
    check_alembic_head_collisions,
    check_manifest_conflict,
    check_out_of_lane,
    check_overlapping_merge_hunks,
    check_symbol_collisions,
    classify_dependency_scope,
    discover_alembic_heads,
    evaluate_scope,
    evaluate_scope_amendment,
    extract_scope_from_idea,
    job_declared_scope_paths,
    minimize_auto_dependencies,
    normalize_scope_amendment_path,
    repoint_dependency_provenance,
    survey_job_queue,
    validate_plan_scope_completeness,
)
from hyqs.pipeline.stages import (
    AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT,
    AuthorizedGateFailureEvidence,
    authorized_gate_failure,
    build_authorized_gate_failure_evidence,
    compose_authorized_gate_failure_detail,
)


def test_dependency_scope_requires_non_empty_allowed_paths():
    assert classify_dependency_scope(None).known is False
    assert classify_dependency_scope({"allowed_paths": []}).known is False
    assert classify_dependency_scope({"allowed_paths": ["src/*.py", "src/*.py"]}).paths == (
        "src/*.py",
    )


def test_minimize_auto_dependencies_preserves_overlap_unknown_and_protected_edges():
    scopes = {
        1: {"allowed_paths": ["src/*.py"]},
        2: {"allowed_paths": ["src/store.py"]},
        3: {"allowed_paths": ["web/App.jsx"]},
        4: None,
    }
    edges = [
        (2, 1, "auto"),
        (3, 1, "auto"),
        (4, 3, "auto"),
        (3, 2, "semantic"),
    ]

    assert minimize_auto_dependencies(scopes, edges) == [
        (2, 1, "auto"),
        (3, 2, "semantic"),
        (4, 3, "auto"),
    ]


def test_minimize_auto_dependencies_removes_only_redundant_generated_edge():
    scope = {"allowed_paths": ["src/*.py"]}
    scopes = {1: scope, 2: scope, 3: scope}
    edges = [(3, 1, "auto"), (3, 2, "semantic"), (2, 1, "user")]

    assert minimize_auto_dependencies(scopes, reversed(edges)) == [
        (2, 1, "user"),
        (3, 2, "semantic"),
    ]


def test_minimize_auto_dependencies_rejects_cycle_without_hiding_it():
    with pytest.raises(ValueError, match="dependency cycle"):
        minimize_auto_dependencies({}, [(1, 2, "auto"), (2, 1, "auto")])


def test_minimize_auto_dependencies_handles_large_wave_deterministically():
    count = 1200
    scopes = {index: {"allowed_paths": ["src/*.py"]} for index in range(count)}
    edges = [(index, index - 1, "auto") for index in range(1, count)]

    assert minimize_auto_dependencies(scopes, reversed(edges)) == edges


def test_repoint_dependency_provenance_fans_single_dependent_across_children():
    assert repoint_dependency_provenance([(10, "semantic")], [1, 2, 3]) == [
        (10, 1, "semantic"),
        (10, 2, "semantic"),
        (10, 3, "semantic"),
    ]


def test_repoint_dependency_provenance_fans_each_dependent_independently():
    edges = [(10, "semantic"), (11, "auto")]

    assert repoint_dependency_provenance(edges, [1, 2]) == [
        (10, 1, "semantic"),
        (10, 2, "semantic"),
        (11, 1, "auto"),
        (11, 2, "auto"),
    ]


def test_repoint_dependency_provenance_empty_child_ids_returns_empty():
    assert repoint_dependency_provenance([(10, "semantic")], []) == []


def test_repoint_dependency_provenance_output_order_independent_of_input_order():
    forward = [(10, "user"), (11, "auto"), (12, "semantic")]
    reversed_edges = list(reversed(forward))

    assert repoint_dependency_provenance(forward, [2, 1]) == repoint_dependency_provenance(
        reversed_edges, [1, 2]
    )


def _amend_scope(**overrides):
    values = {
        "frozen_scope": {"allowed_paths": ["ui/LogPanel.jsx"]},
        "plan_story": {"target_files": ["ui/LogPanel.jsx"]},
        "authorized_event": {"stage": "test", "failing_paths": ["tests/log_panel.test.jsx"]},
        "tracked_files": [
            "ui/LogPanel.jsx",
            "tests/log_panel.test.jsx",
            "ui/log-panel.css",
            "ui/index.js",
        ],
        "current_diff": [],
        "evidence": {},
        "relationships": {},
        "amendment_history": [],
        "active_manifests": [],
    }
    values.update(overrides)
    return evaluate_scope_amendment(**values)


def test_scope_amendment_accepts_direct_failure_and_log_panel_companions():
    decision = _amend_scope(
        evidence={"coverage": ["ui/log-panel.css"], "imports": ["ui/index.js"]},
        relationships={
            "ui/LogPanel.jsx": ["ui/log-panel.css", "ui/index.js"],
        },
    )

    assert decision.accepted_paths == (
        "tests/log_panel.test.jsx",
        "ui/index.js",
        "ui/log-panel.css",
    )
    assert decision.rejected == ()
    assert decision.to_dict()["accepted"][1]["provenance"] == [
        "direct_relationship:ui/LogPanel.jsx",
        "imports",
    ]


@pytest.mark.parametrize("stage", ["test", "lint", "security", "design-review"])
def test_scope_amendment_authorized_gates_accept_named_fixture(stage):
    assert _amend_scope(
        authorized_event={"stage": stage, "paths": ["tests/log_panel.test.jsx"]}
    ).accepted_paths == ("tests/log_panel.test.jsx",)


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        ("ui/*.jsx", "glob_path"),
        ("ui/", "directory_path"),
        ("../escape.py", "unsafe_path"),
        ("/tmp/escape.py", "unsafe_path"),
    ],
)
def test_scope_amendment_rejects_non_concrete_paths(candidate, reason):
    decision = _amend_scope(authorized_event={"stage": "test", "paths": [candidate]})
    assert decision.rejected[0].reason_code == reason


def test_scope_amendment_rejects_build_ai_repeats_unrelated_sensitive_and_conflicts():
    tracked = [
        "tests/log_panel.test.jsx",
        "ui/new.jsx",
        "auth/config.py",
        "other/product.py",
    ]
    assert (
        _amend_scope(
            tracked_files=tracked,
            authorized_event={"stage": "build", "paths": ["ui/new.jsx"]},
        )
        .rejected[0]
        .reason_code
        == "unauthorized_event"
    )
    assert (
        _amend_scope(
            tracked_files=tracked,
            evidence={"ai": ["ui/new.jsx"]},
        )
        .rejected[0]
        .reason_code
        == "ai_only_evidence"
    )
    assert (
        _amend_scope(
            tracked_files=tracked,
            evidence={"imports": ["other/product.py"]},
        )
        .rejected[0]
        .reason_code
        == "unrelated_product_area"
    )
    assert (
        _amend_scope(
            tracked_files=tracked,
            authorized_event={"stage": "security", "paths": ["auth/config.py"]},
        )
        .rejected[0]
        .reason_code
        == "sensitive_path"
    )
    assert (
        _amend_scope(amendment_history=["tests/log_panel.test.jsx"]).rejected[0].reason_code
        == "repeated_path"
    )
    assert (
        _amend_scope(active_manifests=[["tests/*.jsx"]]).rejected[0].reason_code
        == "manifest_conflict"
    )


def test_scope_amendment_sensitive_operator_authorization_and_later_evidence():
    sensitive = _amend_scope(
        tracked_files=["auth/config.py"],
        authorized_event={
            "stage": "security",
            "paths": ["auth/config.py"],
            "operator_authorized_paths": ["auth/config.py"],
        },
    )
    assert sensitive.accepted_paths == ("auth/config.py",)

    later = _amend_scope(
        amendment_history=["tests/log_panel.test.jsx"],
        evidence={"imports": ["ui/index.js"]},
        relationships={"ui/LogPanel.jsx": ["ui/index.js"]},
    )
    assert later.accepted_paths == ("ui/index.js",)


def test_scope_amendment_repeated_paths_remain_policy_rejections_and_new_paths_evaluate():
    decision = _amend_scope(
        authorized_event={
            "stage": "test",
            "paths": [
                "'ui/LogPanel.jsx'",
                "tests/log_panel.test.jsx",
                "ui/index.js",
            ],
        },
        amendment_history=["tests/log_panel.test.jsx"],
    )

    assert decision.accepted_paths == ("ui/index.js",)
    assert {(item.candidate, item.reason_code) for item in decision.rejected} == {
        ("tests/log_panel.test.jsx", "repeated_path"),
        ("ui/LogPanel.jsx", "repeated_path"),
    }
    assert normalize_scope_amendment_path("'ui/LogPanel.jsx'") == "ui/LogPanel.jsx"


def test_scope_amendment_limits_are_exact_and_decision_is_public():
    from hyqs.pipeline import ScopeAmendmentDecision
    from hyqs.pipeline import evaluate_scope_amendment as public_evaluator

    paths = [f"tests/fixture_{number}.py" for number in range(4)]
    decision = _amend_scope(
        authorized_event={"stage": "test", "paths": paths},
        tracked_files=paths,
        per_cycle_limit=2,
        cumulative_limit=3,
    )
    assert isinstance(decision, ScopeAmendmentDecision)
    assert public_evaluator is evaluate_scope_amendment
    assert len(decision.accepted) == 2
    assert {item.reason_code for item in decision.rejected} == {"per_cycle_limit"}

    cumulative = _amend_scope(
        authorized_event={"stage": "test", "paths": paths[:2]},
        tracked_files=paths,
        amendment_history=["old/one.py", "old/two.py"],
        per_cycle_limit=3,
        cumulative_limit=3,
    )
    assert len(cumulative.accepted) == 1
    assert cumulative.rejected[0].reason_code == "cumulative_limit"


def test_authorized_gate_contract_public_exports_share_collision_policy():
    import hyqs.pipeline as pipeline
    import hyqs.pipeline.stages as stages

    assert pipeline.AUTHORIZED_AMENDMENT_GATES is AUTHORIZED_AMENDMENT_GATES
    assert stages.AUTHORIZED_AMENDMENT_GATES is AUTHORIZED_AMENDMENT_GATES
    assert pipeline.AUTHORIZED_GATE_EVIDENCE_CATEGORIES is AUTHORIZED_GATE_EVIDENCE_CATEGORIES
    assert stages.AUTHORIZED_GATE_EVIDENCE_CATEGORIES is AUTHORIZED_GATE_EVIDENCE_CATEGORIES
    assert pipeline.SCOPE_AMENDMENT_PER_CYCLE_LIMIT is SCOPE_AMENDMENT_PER_CYCLE_LIMIT
    assert stages.SCOPE_AMENDMENT_PER_CYCLE_LIMIT is SCOPE_AMENDMENT_PER_CYCLE_LIMIT
    assert pipeline.SCOPE_AMENDMENT_CUMULATIVE_LIMIT is SCOPE_AMENDMENT_CUMULATIVE_LIMIT
    assert stages.SCOPE_AMENDMENT_CUMULATIVE_LIMIT is SCOPE_AMENDMENT_CUMULATIVE_LIMIT
    assert pipeline.normalize_scope_amendment_path is normalize_scope_amendment_path
    assert stages.normalize_scope_amendment_path is normalize_scope_amendment_path
    assert pipeline.AuthorizedGateFailureEvidence is AuthorizedGateFailureEvidence
    assert pipeline.authorized_gate_failure is authorized_gate_failure
    assert build_authorized_gate_failure_evidence is authorized_gate_failure
    assert pipeline.compose_authorized_gate_failure_detail is compose_authorized_gate_failure_detail
    assert stages.compose_authorized_gate_failure_detail is compose_authorized_gate_failure_detail


def test_authorized_gate_failure_detail_preserves_existing_fields_without_mutation():
    detail = {
        "event": "test_failed",
        "retry": {"attempt": 2, "remaining": 1},
        "message": "coverage gate failed",
    }

    composed = compose_authorized_gate_failure_detail(
        detail,
        "test",
        "coverage/project",
        "event/42",
        ["./tests/z_test.py", "tests/a_test.py", "tests/z_test.py"],
        ["imports", "coverage", "imports"],
    )

    assert composed == {
        **detail,
        "authorized_gate_failure": {
            "gate": "test",
            "check_id": "coverage/project",
            "event_id": "event/42",
            "failing_paths": ["tests/a_test.py", "tests/z_test.py"],
            "categories": ["coverage", "imports"],
        },
    }
    assert "authorized_gate_failure" not in detail


@pytest.mark.parametrize(
    "paths",
    [
        [],
        [""],
        ["/tmp/x.py"],
        ["../x.py"],
        ["ui/*.py"],
        ["ui/[x].py"],
    ],
)
def test_authorized_gate_failure_detail_rejects_unsafe_paths(paths):
    with pytest.raises(ValueError):
        compose_authorized_gate_failure_detail(
            {"retry": 1},
            "test",
            "check-1",
            "event-1",
            paths,
            ["coverage"],
        )


def test_authorized_gate_failure_detail_enforces_shared_exact_path_bound():
    paths = [f"tests/path_{number}.py" for number in range(AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT)]

    detail = compose_authorized_gate_failure_detail(
        {},
        "test",
        "check-1",
        "event-1",
        [*paths, "./tests/path_0.py"],
        ["coverage"],
    )
    assert (
        len(detail["authorized_gate_failure"]["failing_paths"])
        == AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT
    )

    with pytest.raises(ValueError):
        compose_authorized_gate_failure_detail(
            {},
            "test",
            "check-1",
            "event-1",
            [*paths, "tests/overflow.py"],
            ["coverage"],
        )


@pytest.mark.parametrize("gate", sorted(AUTHORIZED_AMENDMENT_GATES))
def test_authorized_gate_producer_and_evaluator_accept_every_shared_gate(gate):
    payload = authorized_gate_failure(
        gate,
        "check/stable:1",
        "event/stable:1",
        ["./tests/z_test.py", "tests/a_test.py", "tests/z_test.py"],
        ["imports", "coverage", "imports"],
    )

    assert payload == AuthorizedGateFailureEvidence(
        gate=gate,
        check_id="check/stable:1",
        event_id="event/stable:1",
        failing_paths=("tests/a_test.py", "tests/z_test.py"),
        categories=("coverage", "imports"),
    )
    assert payload.to_dict()["failing_paths"] == ["tests/a_test.py", "tests/z_test.py"]
    assert (
        _amend_scope(
            authorized_event=payload.to_dict(), tracked_files=list(payload.failing_paths)
        ).accepted_paths
        == payload.failing_paths
    )

    canonical = _amend_scope(
        authorized_event={
            "stage": gate,
            "paths": ["./tests/a_test.py", "tests/a_test.py"],
        },
        tracked_files=["tests/a_test.py"],
    )
    assert canonical.accepted_paths == ("tests/a_test.py",)


@pytest.mark.parametrize("category", sorted(AUTHORIZED_GATE_EVIDENCE_CATEGORIES))
def test_authorized_gate_producer_and_evaluator_accept_every_shared_category(category):
    payload = authorized_gate_failure("test", "check-1", "event-1", ["ui/related.py"], [category])
    decision = _amend_scope(
        authorized_event={"gate": payload.gate, "failing_paths": []},
        tracked_files=["ui/related.py"],
        evidence={category: payload.failing_paths},
        relationships={"ui/LogPanel.jsx": payload.failing_paths},
    )
    assert decision.accepted_paths == ("ui/related.py",)


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/x.py",
        "../x.py",
        "ui/*.py",
        "ui/[x].py",
        "ui/{x}.py",
        "ui/folder/",
        "ui/folder",
        "this is prose.py",
        "C:/tmp/x.py",
    ],
)
def test_authorized_gate_failure_rejects_non_concrete_paths(path):
    assert normalize_scope_amendment_path(path) is None
    with pytest.raises(ValueError):
        authorized_gate_failure("test", "check-1", "event-1", [path], ["coverage"])


@pytest.mark.parametrize("gate", ["build", "review", "unknown"])
def test_authorized_gate_failure_rejects_unsupported_gates(gate):
    with pytest.raises(ValueError):
        authorized_gate_failure(gate, "check-1", "event-1", ["tests/a.py"], ["coverage"])


@pytest.mark.parametrize("identity", ["", "space value", "unstable!", "x" * 256])
def test_authorized_gate_failure_rejects_unstable_identities(identity):
    with pytest.raises(ValueError):
        authorized_gate_failure("test", identity, "event-1", ["tests/a.py"], ["coverage"])
    with pytest.raises(ValueError):
        authorized_gate_failure("test", "check-1", identity, ["tests/a.py"], ["coverage"])


@pytest.mark.parametrize("category", ["unknown", "ai", "speculative", "model"])
def test_authorized_gate_failure_rejects_unknown_categories(category):
    with pytest.raises(ValueError):
        authorized_gate_failure("test", "check-1", "event-1", ["tests/a.py"], [category])


def test_authorized_gate_failure_enforces_exact_candidate_bound():
    paths = [f"tests/path_{number}.py" for number in range(AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT)]
    assert (
        len(
            authorized_gate_failure("test", "check-1", "event-1", paths, ["coverage"]).failing_paths
        )
        == AUTHORIZED_GATE_EVIDENCE_PATH_LIMIT
    )
    with pytest.raises(ValueError):
        authorized_gate_failure(
            "test", "check-1", "event-1", [*paths, "tests/overflow.py"], ["coverage"]
        )
    with pytest.raises(ValueError):
        authorized_gate_failure("test", "check-1", "event-1", [], ["coverage"])


def test_planning_candidates_are_bounded_stable_and_provenance_bearing():
    tracked = [
        "hyqs/pipeline/remediation.py",
        "hyqs/pipeline/__init__.py",
        "tests/test_remediation.py",
        "hyqs/web/frontend/src/App.jsx",
    ]
    index = {
        "build_ai_fix_idea": [
            {"module": "hyqs.pipeline.remediation", "callers": ["hyqs.pipeline.supervisor"]}
        ]
    }
    idea = "Change build_ai_fix_idea. Files: hyqs/pipeline/remediation.py"

    first = build_planning_candidates(tracked, index, idea, limit=3)
    second = build_planning_candidates(list(reversed(tracked)), index, idea, limit=3)

    assert first == second
    assert len(first) == 2
    assert all(candidate.reasons for candidate in first)
    assert first[0].path == "hyqs/pipeline/remediation.py"
    assert "explicit_path" in first[0].reasons
    assert any(c.path == "tests/test_remediation.py" for c in first)
    assert not any("/frontend/" in c.path for c in first)


def test_planning_candidates_reject_fabricated_broad_and_unmarked_new_paths():
    result = build_planning_candidates(
        ["hyqs/pipeline/store.py"],
        {},
        "Improve the pipeline with imaginary/new.py and hyqs/pipeline/",
        explicit_new_files=["hyqs/pipeline/created.py", "../escape.py", "broad/"],
    )

    assert [candidate.path for candidate in result] == ["hyqs/pipeline/created.py"]
    assert result[0].status == "new"
    assert result[0].reasons == ("explicit_new",)


def test_planning_candidates_symbol_only_match_does_not_cascade():
    tracked = [
        "service/__init__.py",
        "service/models.py",
        "service/alembic/versions/001_users.py",
        "service/caller.py",
        "tests/test_models.py",
    ]
    result = build_planning_candidates(
        tracked,
        {"User": [{"module": "service.models", "callers": ["service.caller"]}]},
        "Update User",
    )
    by_path = {candidate.path: candidate.reasons for candidate in result}
    paths = set(by_path)

    assert "explicit_symbol" in by_path["service/models.py"]
    assert "indexed_caller" in by_path["service/caller.py"]
    assert "tests/test_models.py" not in paths
    assert "service/__init__.py" not in paths
    assert "service/alembic/versions/001_users.py" not in paths


def test_planning_candidates_modification_evidence_still_cascades():
    tracked = [
        "service/__init__.py",
        "service/models.py",
        "service/alembic/versions/001_users.py",
        "tests/test_models.py",
    ]
    result = build_planning_candidates(
        tracked,
        {"User": [{"module": "service.models"}]},
        "Update User. Files: service/models.py",
    )
    by_path = {candidate.path: candidate.reasons for candidate in result}

    assert "explicit_path" in by_path["service/models.py"]
    assert "nearest_test" in by_path["tests/test_models.py"]
    assert "package_export" in by_path["service/__init__.py"]
    assert "model_migration" in by_path["service/alembic/versions/001_users.py"]


def test_reuse_only_symbol_match_does_not_require_nearest_test():
    """Mirrors job #2895's check_permission: a pure reuse mention of an
    existing, unmodified function must never pull its nearest test into
    scope, and a plan omitting that test must still validate clean."""
    tracked = ["hyqs/web/auth.py", "hyqs/web/mcp_server.py", "tests/test_auth.py"]
    index = {"check_permission": [{"module": "hyqs.web.auth"}]}
    idea = "Use check_permission to authorize the new tool. Files: hyqs/web/mcp_server.py"

    candidates = build_planning_candidates(tracked, index, idea)
    by_path = {candidate.path: candidate.reasons for candidate in candidates}

    assert by_path["hyqs/web/auth.py"] == ("explicit_symbol",)
    assert "tests/test_auth.py" not in by_path

    plan = {
        "target_files": ["hyqs/web/mcp_server.py"],
        "stories": [{"target_files": ["hyqs/web/mcp_server.py"]}],
        "file_impact": [{"path": "hyqs/web/mcp_server.py", "status": "modify"}],
    }
    assert validate_plan_scope_completeness(plan, tracked, candidates).passed


def test_explicit_path_named_source_still_requires_nearest_test():
    """Same check_permission/hyqs.web.auth pairing, but the idea names the
    source file as an explicit/modified path — its nearest test is still
    required, unlike the reuse-only case above."""
    tracked = ["hyqs/web/auth.py", "tests/test_auth.py"]
    index = {"check_permission": [{"module": "hyqs.web.auth"}]}
    idea = "Update check_permission. Files: hyqs/web/auth.py"

    candidates = build_planning_candidates(tracked, index, idea)
    by_path = {candidate.path: candidate.reasons for candidate in candidates}

    assert "explicit_path" in by_path["hyqs/web/auth.py"]
    assert "nearest_test" in by_path["tests/test_auth.py"]

    plan = {
        "target_files": ["hyqs/web/auth.py"],
        "stories": [{"target_files": ["hyqs/web/auth.py"]}],
        "file_impact": [{"path": "hyqs/web/auth.py", "status": "modify"}],
    }
    result = validate_plan_scope_completeness(plan, tracked, candidates)

    assert not result.passed
    assert "regression_test_omission" in result.reason_codes
    assert "tests/test_auth.py" in result.missing_paths


def test_frontend_symbol_match_does_not_infer_python_package_export():
    tracked = [
        "hyqs/web/frontend/src/components/WorkLanes.jsx",
        "hyqs/web/frontend/src/components/WorkLanes.test.jsx",
        "hyqs/pipeline/models.py",
        "hyqs/pipeline/__init__.py",
    ]
    idea = (
        "Update WorkLanes and CurrentExecutor. Files: "
        "hyqs/web/frontend/src/components/WorkLanes.jsx, "
        "hyqs/web/frontend/src/components/WorkLanes.test.jsx"
    )

    candidates = build_planning_candidates(
        tracked,
        {"CurrentExecutor": [{"module": "hyqs.pipeline.models"}]},
        idea,
    )
    paths = {candidate.path for candidate in candidates}

    assert "hyqs/web/frontend/src/components/WorkLanes.jsx" in paths
    assert "hyqs/pipeline/models.py" not in paths
    assert "hyqs/pipeline/__init__.py" not in paths
    declared = tracked[:2]
    plan = {
        "target_files": declared,
        "stories": [{"target_files": declared}],
        "file_impact": [{"path": path, "status": "modify"} for path in declared],
    }
    assert validate_plan_scope_completeness(plan, tracked, candidates).passed


def test_existing_python_module_does_not_require_package_export():
    tracked = ["service/__init__.py", "service/models.py", "tests/test_models.py"]
    candidates = build_planning_candidates(
        tracked,
        {"User": [{"module": "service.models"}]},
        "Update User",
    )
    plan = {
        "target_files": ["service/models.py", "tests/test_models.py"],
        "stories": [
            {"target_files": ["service/models.py", "tests/test_models.py"]},
        ],
        "file_impact": [
            {"path": "service/models.py", "status": "modify"},
            {"path": "tests/test_models.py", "status": "modify"},
        ],
    }

    result = validate_plan_scope_completeness(plan, tracked, candidates)

    assert result.passed


def test_new_python_module_still_requires_package_export():
    tracked = ["service/__init__.py"]
    candidates = [PlanningCandidate("service/__init__.py", "existing", ("package_export",))]
    plan = {
        "target_files": ["service/models.py"],
        "stories": [{"target_files": ["service/models.py"]}],
        "file_impact": [{"path": "service/models.py", "status": "new"}],
    }

    result = validate_plan_scope_completeness(plan, tracked, candidates)

    assert result.reason_codes == ("model_export_omission",)
    assert result.missing_paths == ("service/__init__.py",)


def test_reference_context_paths_are_advisory_not_required():
    tracked = [
        "backend/dashboard/router.py",
        "backend/dashboard/readiness.py",
        "backend/alembic/versions/0034_rls.py",
        "backend/dashboard/__init__.py",
        "backend/dashboard/test_readiness.py",
    ]
    idea = """\
## Scope fence -- ONLY files this job may change
- backend/dashboard/router.py
## Context -- study these first
- backend/dashboard/readiness.py -- mirror its conventions
- backend/alembic/versions/0034_rls.py -- use this pattern
"""
    candidates = build_planning_candidates(tracked, {}, idea)
    reasons = {item.path: item.reasons for item in candidates}
    plan = {
        "target_files": ["backend/dashboard/router.py"],
        "stories": [{"target_files": ["backend/dashboard/router.py"]}],
        "file_impact": [{"path": "backend/dashboard/router.py", "status": "existing"}],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "Explicit backend-only scope.",
        },
    }

    assert reasons["backend/dashboard/router.py"] == ("explicit_path",)
    assert "explicit_reference" in reasons["backend/dashboard/readiness.py"]
    assert "explicit_reference" in reasons["backend/alembic/versions/0034_rls.py"]
    assert "backend/dashboard/test_readiness.py" not in reasons
    assert validate_plan_scope_completeness(plan, tracked, candidates).passed


def test_allowed_target_files_fence_keeps_later_research_paths_advisory():
    tracked = [
        "docs/architecture/README.md",
        "docs/architecture/07-compliance-os-spine.md",
        "docs/product/acme-compliance-os-blueprint.md",
        "backend/acme/compliance_os/router.py",
    ]
    idea = """\
## Allowed target files
- `docs/architecture/08-compliance-os-control-matrix.md` (NEW FILE)
- `docs/architecture/README.md`

## What to do
Author the matrix after reading these shipped artifacts:
- `backend/acme/compliance_os/router.py`
- `docs/architecture/07-compliance-os-spine.md`
- `docs/product/acme-compliance-os-blueprint.md`
"""
    candidates = build_planning_candidates(
        tracked,
        {},
        idea,
        explicit_new_files=["docs/architecture/08-compliance-os-control-matrix.md"],
    )
    reasons = {item.path: item.reasons for item in candidates}
    plan = {
        "target_files": [
            "docs/architecture/08-compliance-os-control-matrix.md",
            "docs/architecture/README.md",
        ],
        "stories": [
            {
                "target_files": [
                    "docs/architecture/08-compliance-os-control-matrix.md",
                    "docs/architecture/README.md",
                ]
            }
        ],
        "file_impact": [
            {
                "path": "docs/architecture/08-compliance-os-control-matrix.md",
                "status": "new",
            },
            {"path": "docs/architecture/README.md", "status": "existing"},
        ],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "Documentation-only change.",
        },
    }

    assert reasons["docs/architecture/README.md"] == ("explicit_path",)
    assert reasons["backend/acme/compliance_os/router.py"] == ("explicit_reference",)
    assert reasons["docs/architecture/07-compliance-os-spine.md"] == ("explicit_reference",)
    assert validate_plan_scope_completeness(plan, tracked, candidates).passed


def test_no_change_fenced_paths_stay_advisory_despite_narrative_mentions():
    tracked = [
        "hyqs/pipeline/collision.py",
        "tests/test_collision.py",
        "hyqs/pipeline/store.py",
        "hyqs/pipeline/gitops.py",
        "hyqs/pipeline/github.py",
        "hyqs/pipeline/deploy.py",
        "hyqs/pipeline/docker_deploy.py",
        "hyqs/pipeline/npm_audit.py",
        "hyqs/pipeline/sast.py",
        "tests/test_store.py",
        "tests/test_gitops.py",
        "tests/test_github.py",
        "tests/test_deploy.py",
        "tests/test_docker_deploy.py",
        "tests/test_npm_audit.py",
        "tests/test_sast.py",
    ]
    unchanged_paths = [
        "hyqs/pipeline/store.py",
        "hyqs/pipeline/gitops.py",
        "hyqs/pipeline/github.py",
        "hyqs/pipeline/deploy.py",
        "hyqs/pipeline/docker_deploy.py",
        "hyqs/pipeline/npm_audit.py",
        "hyqs/pipeline/sast.py",
    ]
    unchanged_nearest_tests = {
        "tests/test_store.py",
        "tests/test_gitops.py",
        "tests/test_github.py",
        "tests/test_deploy.py",
        "tests/test_docker_deploy.py",
        "tests/test_npm_audit.py",
        "tests/test_sast.py",
    }
    idea = """\
## Target files
- hyqs/pipeline/collision.py
- tests/test_collision.py

## Incident narrative
Job #2895 traced a false-positive candidate_omission cascade through
hyqs/pipeline/store.py, hyqs/pipeline/gitops.py, hyqs/pipeline/github.py,
hyqs/pipeline/deploy.py, hyqs/pipeline/docker_deploy.py,
hyqs/pipeline/npm_audit.py, and hyqs/pipeline/sast.py before the root cause
was isolated to collision.py's planning-candidate classification.
The investigation repeatedly inspected hyqs/pipeline/store.py,
hyqs/pipeline/gitops.py, hyqs/pipeline/github.py, hyqs/pipeline/deploy.py,
hyqs/pipeline/docker_deploy.py, hyqs/pipeline/npm_audit.py, and
hyqs/pipeline/sast.py while ruling out persistence, git, deploy, audit, and
security-stage causes.

## No functional change -- byte-for-byte unchanged
- hyqs/pipeline/store.py
- hyqs/pipeline/gitops.py
- hyqs/pipeline/github.py
- hyqs/pipeline/deploy.py
- hyqs/pipeline/docker_deploy.py
- hyqs/pipeline/npm_audit.py
- hyqs/pipeline/sast.py
"""

    candidates = build_planning_candidates(tracked, {}, idea)
    reasons = {item.path: item.reasons for item in candidates}

    assert reasons["hyqs/pipeline/collision.py"] == ("explicit_path",)
    assert "explicit_path" in reasons["tests/test_collision.py"]
    for path in unchanged_paths:
        assert reasons[path] == ("explicit_reference",)
    assert unchanged_nearest_tests.isdisjoint(reasons)

    plan = {
        "target_files": ["hyqs/pipeline/collision.py", "tests/test_collision.py"],
        "stories": [{"target_files": ["hyqs/pipeline/collision.py", "tests/test_collision.py"]}],
        "file_impact": [
            {"path": "hyqs/pipeline/collision.py", "status": "existing"},
            {"path": "tests/test_collision.py", "status": "existing"},
        ],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "Backend-only classification fix.",
        },
    }
    assert validate_plan_scope_completeness(plan, tracked, candidates).passed

    omitted_target_plan = {
        **plan,
        "target_files": ["tests/test_collision.py"],
        "stories": [{"target_files": ["tests/test_collision.py"]}],
        "file_impact": [
            {"path": "tests/test_collision.py", "status": "existing"},
        ],
    }
    omitted = validate_plan_scope_completeness(omitted_target_plan, tracked, candidates)

    assert "candidate_omission" in omitted.reason_codes
    assert "hyqs/pipeline/collision.py" in omitted.missing_paths


def test_no_change_inline_marker_line_declares_path_advisory():
    tracked = ["hyqs/pipeline/collision.py", "hyqs/pipeline/store.py"]
    idea = """\
## Target files
- hyqs/pipeline/collision.py

## Incident narrative
Job #2895 also touched hyqs/pipeline/store.py while tracing the cascade.
hyqs/pipeline/store.py is byte-for-byte unchanged in this fix.
"""

    candidates = build_planning_candidates(tracked, {}, idea)
    reasons = {item.path: item.reasons for item in candidates}

    assert reasons["hyqs/pipeline/collision.py"] == ("explicit_path",)
    assert reasons["hyqs/pipeline/store.py"] == ("explicit_reference",)


def test_bare_prose_mention_without_files_label_is_advisory_not_required():
    """A path cited only in ordinary background prose -- no 'Files:'/'Target
    files:' label or fence anywhere in the idea -- must default to advisory
    and never force a missing-candidate failure. Mirrors the false positive
    from job #2895's investigation: a docs-only job whose prose described
    already-shipped backend files was forced to declare them as required
    edit targets."""
    tracked = [
        "hyqs/pipeline/collision.py",
        "tests/test_collision.py",
        "docs/architecture/README.md",
    ]
    idea = """\
Fix the scope-completeness false positive in collision.py's classification
logic.

## Background
docs/architecture/README.md documents the current planning-candidate
behavior this job is correcting.
"""

    candidates = build_planning_candidates(tracked, {}, idea)
    reasons = {item.path: item.reasons for item in candidates}

    assert reasons["docs/architecture/README.md"] == ("explicit_reference",)

    plan = {
        "target_files": ["hyqs/pipeline/collision.py", "tests/test_collision.py"],
        "stories": [{"target_files": ["hyqs/pipeline/collision.py", "tests/test_collision.py"]}],
        "file_impact": [
            {"path": "hyqs/pipeline/collision.py", "status": "existing"},
            {"path": "tests/test_collision.py", "status": "existing"},
        ],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "Backend-only classification fix.",
        },
    }

    assert validate_plan_scope_completeness(plan, tracked, candidates).passed


def test_heading_target_files_fence_still_mandates_path_and_requires_nearest_test():
    """A path declared inside a heading-triggered 'Target files:' fence is
    still classified explicit_path (mandatory), so omitting it from a plan's
    manifest still fails scope completeness -- locking in that a genuinely
    required companion file (its nearest test) is still caught."""
    tracked = ["hyqs/web/auth.py", "tests/test_auth.py"]
    idea = """\
## Target files
- hyqs/web/auth.py

## Why
Tighten the check_permission validation logic described in hyqs/web/auth.py.
"""

    candidates = build_planning_candidates(tracked, {}, idea)
    by_path = {candidate.path: candidate.reasons for candidate in candidates}

    assert "explicit_path" in by_path["hyqs/web/auth.py"]
    assert "nearest_test" in by_path["tests/test_auth.py"]

    plan = {
        "target_files": ["hyqs/web/auth.py"],
        "stories": [{"target_files": ["hyqs/web/auth.py"]}],
        "file_impact": [{"path": "hyqs/web/auth.py", "status": "modify"}],
    }
    result = validate_plan_scope_completeness(plan, tracked, candidates)

    assert not result.passed
    assert "regression_test_omission" in result.reason_codes
    assert "tests/test_auth.py" in result.missing_paths


def test_internal_plan_ignores_frontend_root_reference_candidates():
    plan = {
        "target_files": ["backend/router.py"],
        "stories": [{"target_files": ["backend/router.py"]}],
        "file_impact": [{"path": "backend/router.py", "status": "existing"}],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "Explicit backend-only scope.",
        },
    }
    candidates = [
        PlanningCandidate("frontend/package.json", "existing", ("explicit_path",)),
        PlanningCandidate("frontend/src/app/layout.tsx", "existing", ("explicit_path",)),
    ]

    result = validate_plan_scope_completeness(
        plan,
        ["backend/router.py", "frontend/package.json", "frontend/src/app/layout.tsx"],
        candidates,
    )

    assert result.passed


def test_is_frontend_path_recognizes_browser_rendered_files():
    for path in (
        "admin_ui/src/api.ts",
        "admin_ui/src/api.test.ts",
        "admin_ui/src/main.js",
        "templates/dashboard.html",
        "ui/status.vue",
        "hyqs/web/frontend/src/components/WorkLanes.jsx",
        "hyqs/web/frontend/src/App.tsx",
        "hyqs/web/frontend/src/styles.css",
    ):
        assert _is_frontend_path(path), path

    for path in ("hyqs/pipeline/collision.py", "hyqs/pipeline/models.py", "README.md"):
        assert not _is_frontend_path(path), path


def test_completeness_accepts_ts_frontend_outside_frontend_dir_job_3037():
    """Reproduces the failed job #3037 shape: an HTTP-visible plan whose
    frontend coverage lives under admin_ui/ (not a 'frontend/'-prefixed
    directory) as .ts files, which _is_frontend_path previously missed."""
    plan = {
        "target_files": [
            "admin_ui/src/api.ts",
            "admin_ui/src/api.test.ts",
            "hyqs/web/app.py",
        ],
        "stories": [
            {
                "target_files": [
                    "admin_ui/src/api.ts",
                    "admin_ui/src/api.test.ts",
                    "hyqs/web/app.py",
                ]
            }
        ],
        "file_impact": [
            {"path": "admin_ui/src/api.ts", "status": "new"},
            {"path": "admin_ui/src/api.test.ts", "status": "new"},
            {"path": "hyqs/web/app.py", "status": "existing"},
        ],
        "ui_impact": {
            "touches_backend_surface": True,
            "frontend_changes": ["add admin_ui API client"],
            "no_ui_change_reason": "",
        },
    }
    tracked = ["hyqs/web/app.py"]

    result = validate_plan_scope_completeness(plan, tracked, [])

    assert result.passed
    assert "frontend_coverage_omission" not in result.reason_codes


def test_completeness_accepts_server_rendered_html_dashboard_job_3599():
    """Server-rendered templates are frontend coverage even without a frontend/ root."""
    plan = {
        "target_files": ["main.py", "templates/dashboard.html", "tests/test_news.py"],
        "stories": [
            {
                "target_files": [
                    "main.py",
                    "templates/dashboard.html",
                    "tests/test_news.py",
                ]
            }
        ],
        "file_impact": [
            {"path": "main.py", "status": "existing"},
            {"path": "templates/dashboard.html", "status": "new"},
            {"path": "tests/test_news.py", "status": "new"},
        ],
        "ui_impact": {
            "touches_backend_surface": True,
            "frontend_changes": ["add a server-rendered news dashboard"],
            "no_ui_change_reason": "",
        },
    }

    result = validate_plan_scope_completeness(plan, ["main.py"], [])

    assert result.passed
    assert "frontend_coverage_omission" not in result.reason_codes


def test_internal_only_plan_does_not_require_ts_frontend_candidate():
    """Backend-only safety is unchanged: a .ts/.js frontend candidate present
    only as candidate evidence (never declared) is still skipped when the
    plan explicitly declares no UI change."""
    plan = {
        "target_files": ["hyqs/pipeline/collision.py"],
        "stories": [{"target_files": ["hyqs/pipeline/collision.py"]}],
        "file_impact": [{"path": "hyqs/pipeline/collision.py", "status": "existing"}],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "Backend-only classification fix.",
        },
    }
    candidates = [
        PlanningCandidate("admin_ui/src/api.ts", "existing", ("explicit_path",)),
        PlanningCandidate("admin_ui/src/api.test.ts", "existing", ("nearest_test",)),
    ]

    result = validate_plan_scope_completeness(
        plan,
        ["hyqs/pipeline/collision.py", "admin_ui/src/api.ts", "admin_ui/src/api.test.ts"],
        candidates,
    )

    assert result.passed


def test_new_task_module_does_not_infer_package_export():
    tracked = ["service/tasks/__init__.py", "service/tasks/existing.py"]
    candidates = build_planning_candidates(
        tracked,
        {},
        "Add service/tasks/new_task.py",
        explicit_new_files=["service/tasks/new_task.py"],
    )

    assert not any(item.path == "service/tasks/__init__.py" for item in candidates)


def test_explicit_python_path_remains_authoritative_for_frontend_idea():
    tracked = [
        "hyqs/web/frontend/src/components/WorkLanes.jsx",
        "hyqs/pipeline/models.py",
        "hyqs/pipeline/__init__.py",
    ]
    idea = (
        "Update the frontend WorkLanes. Files: "
        "hyqs/web/frontend/src/components/WorkLanes.jsx, hyqs/pipeline/models.py"
    )

    candidates = build_planning_candidates(tracked, {}, idea)
    by_path = {candidate.path: candidate.reasons for candidate in candidates}

    assert "explicit_path" in by_path["hyqs/pipeline/models.py"]
    assert "package_export" in by_path["hyqs/pipeline/__init__.py"]


def test_mixed_language_symbols_are_checked_within_each_declared_surface():
    tracked = [
        "hyqs/web/frontend/src/components/WorkLanes.jsx",
        "hyqs/web/frontend/src/components/WorkLanes.test.jsx",
        "hyqs/pipeline/models.py",
        "hyqs/pipeline/__init__.py",
        "tests/test_models.py",
        "other/service.py",
        "other/__init__.py",
    ]
    index = {
        "WorkLanes": [
            {"module": "hyqs/web/frontend/src/components/WorkLanes.jsx"},
        ],
        "CurrentExecutor": [
            {"module": "hyqs.pipeline.models"},
            {"module": "other.service"},
        ],
    }
    idea = (
        "Update WorkLanes and CurrentExecutor. Files: "
        "hyqs/web/frontend/src/components/WorkLanes.jsx, hyqs/pipeline/models.py"
    )

    candidates = build_planning_candidates(tracked, index, idea)
    paths = {candidate.path for candidate in candidates}

    assert "hyqs/web/frontend/src/components/WorkLanes.test.jsx" in paths
    assert "tests/test_models.py" in paths
    assert "hyqs/pipeline/__init__.py" in paths
    assert "other/service.py" not in paths
    assert "other/__init__.py" not in paths


class _FakeStore:
    def __init__(self, index: dict[str, list[dict]]) -> None:
        self._index = index

    def get_symbol_index_names(self, project_id: int) -> dict[str, list[dict]]:
        return self._index


def test_main_collision_across_distinct_modules_is_not_flagged(tmp_path: Path) -> None:
    pkg = tmp_path / "hyqs" / "pipeline"
    pkg.mkdir(parents=True)
    (tmp_path / "hyqs" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")

    (pkg / "migrate_sqlite.py").write_text("def main() -> None:\n    ...\n")
    (pkg / "architect_cli.py").write_text("def main() -> None:\n    ...\n")

    store = _FakeStore(
        {
            "main": [
                {
                    "module": "hyqs.pipeline.migrate_sqlite",
                    "kind": "function",
                    "signature": "def main() -> None:",
                }
            ]
        }
    )

    assert check_symbol_collisions(str(tmp_path), project_id=1, store=store) == []


def test_genuine_duplicate_non_conventional_symbol_is_still_flagged(tmp_path: Path) -> None:
    pkg = tmp_path / "hyqs" / "pipeline"
    pkg.mkdir(parents=True)
    (tmp_path / "hyqs" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")

    (pkg / "existing_module.py").write_text("def do_thing():\n    ...\n")
    (pkg / "new_module.py").write_text("def do_thing():\n    ...\n")

    store = _FakeStore(
        {
            "do_thing": [
                {
                    "module": "hyqs.pipeline.existing_module",
                    "kind": "function",
                    "signature": "def do_thing():",
                }
            ]
        }
    )

    messages = check_symbol_collisions(str(tmp_path), project_id=1, store=store)

    assert len(messages) == 1
    assert "do_thing" in messages[0]


def test_alembic_revision_entrypoints_are_not_flagged(tmp_path: Path) -> None:
    versions = tmp_path / "service" / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "001_existing.py").write_text(
        "def upgrade():\n    ...\n\ndef downgrade():\n    ...\n"
    )
    (versions / "002_new.py").write_text("def upgrade():\n    ...\n\ndef downgrade():\n    ...\n")
    store = _FakeStore(
        {
            name: [
                {
                    "module": "service.alembic.versions.001_existing",
                    "kind": "function",
                    "signature": f"def {name}():",
                }
            ]
            for name in ("upgrade", "downgrade")
        }
    )

    messages = check_symbol_collisions(
        tmp_path,
        project_id=1,
        store=store,
        changed_files=["service/alembic/versions/002_new.py"],
    )

    assert messages == []


def test_upgrade_and_downgrade_outside_alembic_versions_are_flagged(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    for module in ("existing", "new"):
        (app / f"{module}.py").write_text("def upgrade():\n    ...\n\ndef downgrade():\n    ...\n")
    store = _FakeStore(
        {
            name: [
                {
                    "module": "app.existing",
                    "kind": "function",
                    "signature": f"def {name}():",
                }
            ]
            for name in ("upgrade", "downgrade")
        }
    )

    messages = check_symbol_collisions(tmp_path, project_id=1, store=store)

    assert {message.split("'")[1] for message in messages} == {"upgrade", "downgrade"}


@pytest.mark.parametrize(
    "directory,module_prefix",
    [
        ("alembic_versions", "alembic_versions"),
        ("alembic/version", "alembic.version"),
        ("my_alembic/versions", "my_alembic.versions"),
        ("alembic/versions_extra", "alembic.versions_extra"),
    ],
)
def test_alembic_versions_near_match_paths_are_not_exempted(
    tmp_path: Path, directory: str, module_prefix: str
) -> None:
    modules = tmp_path / directory
    modules.mkdir(parents=True)
    (modules / "existing.py").write_text("def upgrade():\n    ...\n")
    (modules / "new.py").write_text("def upgrade():\n    ...\n")
    store = _FakeStore(
        {
            "upgrade": [
                {
                    "module": f"{module_prefix}.existing",
                    "kind": "function",
                    "signature": "def upgrade():",
                }
            ]
        }
    )

    messages = check_symbol_collisions(tmp_path, project_id=1, store=store)

    assert len(messages) == 1
    assert "upgrade" in messages[0]


# job #2140: the baseline symbol index is only refreshed periodically (during
# some job's PLAN stage), so it can lag a just-merged file. A job that merely
# *inherits* a pre-existing collision via git — without touching the
# colliding file itself — must never be blamed for introducing it.


def test_collision_not_flagged_when_new_module_absent_from_changed_files(tmp_path: Path) -> None:
    pkg = tmp_path / "hyqs" / "pipeline"
    pkg.mkdir(parents=True)
    (tmp_path / "hyqs" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")

    (pkg / "existing_module.py").write_text("def do_thing():\n    ...\n")
    (pkg / "new_module.py").write_text("def do_thing():\n    ...\n")

    store = _FakeStore(
        {
            "do_thing": [
                {
                    "module": "hyqs.pipeline.existing_module",
                    "kind": "function",
                    "signature": "def do_thing():",
                }
            ]
        }
    )

    messages = check_symbol_collisions(
        str(tmp_path), project_id=1, store=store, changed_files=["some/other/file.py"]
    )

    assert messages == []


def test_collision_still_flagged_when_new_module_is_in_changed_files(tmp_path: Path) -> None:
    pkg = tmp_path / "hyqs" / "pipeline"
    pkg.mkdir(parents=True)
    (tmp_path / "hyqs" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")

    (pkg / "existing_module.py").write_text("def do_thing():\n    ...\n")
    (pkg / "new_module.py").write_text("def do_thing():\n    ...\n")

    store = _FakeStore(
        {
            "do_thing": [
                {
                    "module": "hyqs.pipeline.existing_module",
                    "kind": "function",
                    "signature": "def do_thing():",
                }
            ]
        }
    )

    messages = check_symbol_collisions(
        str(tmp_path),
        project_id=1,
        store=store,
        changed_files=["hyqs/pipeline/new_module.py"],
    )

    assert len(messages) == 1
    assert "do_thing" in messages[0]


# job #1424: symbol-collision gate scoped to workspace member, not whole
# repo — a symbol duplicated across independently-deployed services (each
# with its own project manifest) is framework convention, not a coherence
# bug. Mirrors Acme's backend/ + identity/ layout.


def test_same_symbol_across_workspace_members_is_not_flagged(tmp_path: Path) -> None:
    backend = tmp_path / "backend" / "pkg"
    identity = tmp_path / "identity" / "pkg"
    backend.mkdir(parents=True)
    identity.mkdir(parents=True)
    (tmp_path / "backend" / "pyproject.toml").write_text("[project]\nname = 'backend'\n")
    (tmp_path / "identity" / "pyproject.toml").write_text("[project]\nname = 'identity'\n")
    (backend / "__init__.py").write_text("")
    (identity / "__init__.py").write_text("")
    (backend / "main.py").write_text("def create_app():\n    ...\n")
    (identity / "main.py").write_text("def create_app():\n    ...\n")

    # 'backend' is the baseline-known side; 'identity' is new to this job.
    store = _FakeStore(
        {
            "create_app": [
                {
                    "module": "backend.pkg.main",
                    "kind": "function",
                    "signature": "def create_app():",
                }
            ]
        }
    )

    assert check_symbol_collisions(str(tmp_path), project_id=1, store=store) == []


def test_same_symbol_within_one_workspace_member_is_still_flagged(tmp_path: Path) -> None:
    backend = tmp_path / "backend" / "pkg"
    backend.mkdir(parents=True)
    (tmp_path / "backend" / "pyproject.toml").write_text("[project]\nname = 'backend'\n")
    (backend / "__init__.py").write_text("")
    (backend / "existing.py").write_text("def create_app():\n    ...\n")
    (backend / "new.py").write_text("def create_app():\n    ...\n")

    store = _FakeStore(
        {
            "create_app": [
                {
                    "module": "backend.pkg.existing",
                    "kind": "function",
                    "signature": "def create_app():",
                }
            ]
        }
    )

    messages = check_symbol_collisions(str(tmp_path), project_id=1, store=store)

    assert len(messages) == 1
    assert "create_app" in messages[0]


def test_no_sub_manifests_reproduces_flat_repo_collision_behavior(tmp_path: Path) -> None:
    pkg = tmp_path / "hyqs" / "pipeline"
    pkg.mkdir(parents=True)
    (tmp_path / "hyqs" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "existing_module.py").write_text("def do_thing():\n    ...\n")
    (pkg / "new_module.py").write_text("def do_thing():\n    ...\n")

    store = _FakeStore(
        {
            "do_thing": [
                {
                    "module": "hyqs.pipeline.existing_module",
                    "kind": "function",
                    "signature": "def do_thing():",
                }
            ]
        }
    )

    messages = check_symbol_collisions(str(tmp_path), project_id=1, store=store)

    assert len(messages) == 1
    assert "do_thing" in messages[0]


def test_check_manifest_conflict_true_for_overlapping_globs() -> None:
    assert check_manifest_conflict(["hyqs/pipeline/store.py"], ["hyqs/pipeline/*.py"]) is True


def test_check_manifest_conflict_true_for_identical_literal_paths() -> None:
    assert check_manifest_conflict(["hyqs/pipeline/store.py"], ["hyqs/pipeline/store.py"]) is True


def test_check_manifest_conflict_false_for_disjoint_globs() -> None:
    assert check_manifest_conflict(["hyqs/pipeline/store.py"], ["hyqs/web/*.py"]) is False


def test_check_manifest_conflict_false_when_either_side_empty() -> None:
    assert check_manifest_conflict([], ["hyqs/pipeline/*.py"]) is False
    assert check_manifest_conflict(["hyqs/pipeline/store.py"], []) is False
    assert check_manifest_conflict([], []) is False


def test_check_manifest_conflict_true_for_identical_bracketed_paths() -> None:
    """Job #2239: fnmatch parses '[token]' as a 1-char glob class, so a literal
    Next.js dynamic-route path can never match itself under fnmatch alone —
    the literal-equality fast path must catch it.
    """
    path = "frontend/src/app/[id]/page.tsx"
    assert check_manifest_conflict([path], [path]) is True


def test_check_out_of_lane_empty_scope_is_a_no_op() -> None:
    assert check_out_of_lane(None, ["a.py", "b.py"]) == []
    assert check_out_of_lane({"allowed_paths": []}, ["a.py", "b.py"]) == []


def test_check_out_of_lane_partitions_changed_files() -> None:
    scope = {"allowed_paths": ["hyqs/pipeline/store.py", "tests/test_store.py"]}
    changed_files = ["hyqs/pipeline/store.py", "hyqs/web/app.py", "tests/test_store.py"]

    out_of_lane = check_out_of_lane(scope, changed_files)

    assert out_of_lane == ["hyqs/web/app.py"]


def test_evaluate_scope_manifest_less_is_not_enforced() -> None:
    result = evaluate_scope(None, ["b.py", "a.py"])

    assert result.passed
    assert not result.enforced
    assert result.to_dict()["unexpected_paths"] == []


def test_evaluate_scope_reports_exact_manifest_and_bounded_sorted_paths() -> None:
    scope = {"allowed_paths": ["declared.py", "tests/*.py"]}

    result = evaluate_scope(
        scope, ["z.py", "declared.py", "a.py", "z.py", "m.py"], unexpected_limit=2
    )

    assert not result.passed
    assert result.allowed_paths == ("declared.py", "tests/*.py")
    assert result.unexpected_paths == ("a.py", "m.py")
    assert result.unexpected_count == 3
    assert result.truncated


def test_evaluate_scope_preserves_lockfile_exemption() -> None:
    result = evaluate_scope(
        {"allowed_paths": ["identity/app.py"]},
        ["identity/app.py", "identity/uv.lock"],
    )

    assert result.enforced
    assert result.passed


# job #2239: fnmatch parses its pattern argument for glob syntax, where
# '[...]' means "match any single character in this set" — not a literal
# bracketed folder name. Next.js dynamic-route directories are literally
# named `[token]`, `[id]`, etc, so when a planner lists such a path verbatim
# in allowed_paths, fnmatch alone can never match it against itself,
# un-healably rejecting the job's own file as out of lane on every retry
# (root cause of job #2237's exhaustion/escalation).


def test_check_out_of_lane_matches_literal_bracketed_route_file() -> None:
    path = "frontend/src/app/invite/[token]/page.tsx"
    scope = {"allowed_paths": [path]}

    assert check_out_of_lane(scope, [path]) == []


def test_check_out_of_lane_matches_literal_bracketed_route_directory() -> None:
    path = "frontend/src/app/[id]/page.tsx"
    scope = {"allowed_paths": [path]}

    assert check_out_of_lane(scope, [path]) == []


def test_check_out_of_lane_still_applies_genuine_glob_patterns() -> None:
    scope = {"allowed_paths": ["*.py"]}

    assert check_out_of_lane(scope, ["foo.py"]) == []
    assert check_out_of_lane(scope, ["foo.js"]) == ["foo.js"]


# job #1423: dependency lockfiles are regenerated as a side effect of running
# the package manager in the job's worktree, so no planner ever lists them —
# they are exempt from the out-of-lane check, but only inside a directory the
# manifest already covers (security review, job #1423 fix-2): a lockfile in a
# directory the manifest never authorizes still trips the gate, so a job
# can't use a lockfile to smuggle changes into an unrelated project.


def test_check_out_of_lane_exempts_nested_lockfile_in_covered_dir() -> None:
    scope = {"allowed_paths": ["identity/alembic.ini"]}

    assert check_out_of_lane(scope, ["identity/alembic.ini", "identity/uv.lock"]) == []


def test_check_out_of_lane_exempts_root_level_lockfile_in_covered_dir() -> None:
    scope = {"allowed_paths": ["pyproject.toml"]}

    assert check_out_of_lane(scope, ["package-lock.json"]) == []


@pytest.mark.parametrize(
    "lockfile",
    [
        "uv.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
        "go.sum",
    ],
)
def test_check_out_of_lane_exempts_every_known_lockfile_basename_in_covered_dir(
    lockfile: str,
) -> None:
    scope = {"allowed_paths": ["nested/dir/app.py"]}

    assert check_out_of_lane(scope, [f"nested/dir/{lockfile}"]) == []


@pytest.mark.parametrize("not_a_lockfile", ["src/mylock.py", "notes/uv.lock.md"])
def test_check_out_of_lane_rejects_lockfile_like_names_that_dont_match_exactly(
    not_a_lockfile: str,
) -> None:
    scope = {"allowed_paths": ["identity/alembic.ini"]}

    assert check_out_of_lane(scope, [not_a_lockfile]) == [not_a_lockfile]


def test_lockfile_dir_in_scope_matches_literal_bracketed_directory() -> None:
    """Job #2239: a lockfile dropped inside a bracketed Next.js dynamic-route
    directory must still resolve as in-scope via literal equality, since
    fnmatch alone treats '[token]' as a 1-char glob class.
    """
    allowed_paths = ["frontend/src/app/invite/[token]/page.tsx"]

    assert _lockfile_dir_in_scope(
        "frontend/src/app/invite/[token]/package-lock.json", allowed_paths
    )


def test_check_out_of_lane_rejects_lockfile_outside_any_covered_dir() -> None:
    """Security review, job #1423 fix-2: a job scoped to identity/ can't drop
    an uv.lock into an unrelated project's directory and have it wave through.
    """
    scope = {"allowed_paths": ["identity/alembic.ini"]}

    assert check_out_of_lane(scope, ["other-project/uv.lock"]) == ["other-project/uv.lock"]


def test_check_out_of_lane_acceptance_criteria() -> None:
    scope = {"allowed_paths": ["identity/alembic.ini"]}

    assert check_out_of_lane(scope, ["identity/alembic.ini", "identity/uv.lock"]) == []
    assert check_out_of_lane(scope, ["identity/secret.py"]) == ["identity/secret.py"]


# job #1135 scenario: the idea's "Files:" list spans well past a ~72-80 char
# commit-subject cutoff, so a truncated-subject-derived manifest omits files
# the idea explicitly named.
_JOB_1135_IDEA = (
    "FRONTEND ONLY. Files: new frontend/src/components/page-header.tsx + "
    "a/page.tsx, b/page.tsx, c/page.tsx, d/page.tsx, e/page.tsx, f/page.tsx, "
    "g/page.tsx"
)


def test_extract_scope_from_idea_returns_full_file_enumeration() -> None:
    paths = extract_scope_from_idea(_JOB_1135_IDEA)

    assert paths == [
        "frontend/src/components/page-header.tsx",
        "a/page.tsx",
        "b/page.tsx",
        "c/page.tsx",
        "d/page.tsx",
        "e/page.tsx",
        "f/page.tsx",
        "g/page.tsx",
    ]


def test_extract_scope_from_idea_diverges_from_truncated_subject() -> None:
    full_paths = extract_scope_from_idea(_JOB_1135_IDEA)
    truncated_subject = _JOB_1135_IDEA.split("\n")[0][:80]
    truncated_paths = extract_scope_from_idea(truncated_subject)

    assert full_paths != truncated_paths
    assert set(truncated_paths) < set(full_paths)


def test_extract_scope_from_idea_supports_target_files_label() -> None:
    idea = "Some prose. Target files: hyqs/pipeline/store.py, tests/test_store.py"

    assert extract_scope_from_idea(idea) == [
        "hyqs/pipeline/store.py",
        "tests/test_store.py",
    ]


def test_extract_scope_from_idea_returns_empty_list_without_label() -> None:
    assert extract_scope_from_idea("Just a plain idea with no file list.") == []


def test_extract_scope_from_idea_ignores_files_suffix_without_word_boundary() -> None:
    idea = "Update user profiles: admin.py needs a fix for last_login.py"

    assert extract_scope_from_idea(idea) == []


def test_extract_scope_from_idea_captures_full_route_group_path() -> None:
    idea = "Target files: frontend/src/app/(dashboard)/governance/bra/page.tsx"

    assert extract_scope_from_idea(idea) == [
        "frontend/src/app/(dashboard)/governance/bra/page.tsx",
    ]


def test_extract_scope_from_idea_captures_full_dynamic_segment_paths() -> None:
    idea = (
        "Target files: frontend/src/app/blog/[id]/page.tsx, "
        "frontend/src/app/docs/[...slug]/page.tsx"
    )

    assert extract_scope_from_idea(idea) == [
        "frontend/src/app/blog/[id]/page.tsx",
        "frontend/src/app/docs/[...slug]/page.tsx",
    ]


def test_extract_scope_from_idea_captures_full_optional_catch_all_path() -> None:
    idea = "Target files: frontend/src/app/shop/[[...optional]]/page.tsx"

    assert extract_scope_from_idea(idea) == [
        "frontend/src/app/shop/[[...optional]]/page.tsx",
    ]


def test_extract_scope_from_idea_ignores_prose_parenthetical_without_extension() -> None:
    idea = (
        "Target files: hyqs/pipeline/collision.py "
        "(absolute path — route group has literal parens), "
        "tests/test_collision.py"
    )

    assert extract_scope_from_idea(idea) == [
        "hyqs/pipeline/collision.py",
        "tests/test_collision.py",
    ]


def test_job_declared_scope_paths_returns_empty_for_bare_idea_and_no_plan() -> None:
    assert job_declared_scope_paths("", None) == []


def test_job_declared_scope_paths_uses_plan_target_files_when_idea_has_no_label() -> None:
    plan = {"stories": [{"id": "S1", "target_files": ["hyqs/pipeline/foo.py"]}]}

    paths = job_declared_scope_paths("Just a plain idea with no file list.", plan)

    assert paths == ["hyqs/pipeline/foo.py"]


def test_job_declared_scope_paths_puts_plan_paths_first_then_idea_only_paths() -> None:
    idea = "Files: a.py, b.py"
    plan = {"stories": [{"id": "S1", "target_files": ["a.py", "c.py"]}]}

    paths = job_declared_scope_paths(idea, plan)

    assert paths == ["a.py", "c.py", "b.py"]


# Alembic merge-file exemption in the out-of-lane gate: resolving two heads
# into a merge migration is a framework-mandated action whose exact path a
# planner can't predict in advance.


def _write_migration(versions_dir: Path, filename: str, revision: str, down_revision) -> None:
    versions_dir.mkdir(parents=True, exist_ok=True)
    (versions_dir / filename).write_text(
        f"revision = {revision!r}\ndown_revision = {down_revision!r}\n"
    )


def test_discover_alembic_heads_linear_chain_has_single_head(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "a")

    assert discover_alembic_heads(versions) == {"b"}


def test_discover_alembic_heads_shared_parent_yields_two_heads(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")

    assert discover_alembic_heads(versions) == {"a", "b"}


def test_discover_alembic_heads_excludes_named_file(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")
    _write_migration(versions, "merge.py", "merge1", ("a", "b"))

    assert discover_alembic_heads(versions, exclude="merge.py") == {"a", "b"}
    # Including the merge file itself, only 'merge1' remains unreferenced.
    assert discover_alembic_heads(versions) == {"merge1"}


def test_discover_alembic_heads_missing_dir_returns_empty_set(tmp_path: Path) -> None:
    assert discover_alembic_heads(tmp_path / "nonexistent") == set()


def test_is_alembic_merge_resolution_true_for_genuine_merge(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")
    _write_migration(versions, "merge.py", "merge1", ("a", "b"))

    assert _is_alembic_merge_resolution("alembic/versions/merge.py", tmp_path) is True


def test_is_alembic_merge_resolution_false_for_wrong_down_revision_pair(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")
    _write_migration(versions, "merge.py", "merge1", ("a", "c"))

    assert _is_alembic_merge_resolution("alembic/versions/merge.py", tmp_path) is False


def test_is_alembic_merge_resolution_false_outside_versions_dir(tmp_path: Path) -> None:
    (tmp_path / "hyqs" / "pipeline").mkdir(parents=True)
    unrelated = tmp_path / "hyqs" / "pipeline" / "merge.py"
    unrelated.write_text("revision = 'merge1'\ndown_revision = ('a', 'b')\n")

    assert _is_alembic_merge_resolution("hyqs/pipeline/merge.py", tmp_path) is False


def test_is_alembic_merge_resolution_false_for_nonexistent_file(tmp_path: Path) -> None:
    assert _is_alembic_merge_resolution("alembic/versions/ghost.py", tmp_path) is False


def test_check_out_of_lane_exempts_genuine_alembic_merge_file(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")
    _write_migration(versions, "merge.py", "merge1", ("a", "b"))

    scope = {"allowed_paths": ["hyqs/pipeline/unrelated.py"]}
    changed_files = ["alembic/versions/merge.py"]

    assert check_out_of_lane(scope, changed_files, worktree=tmp_path) == []


def test_check_out_of_lane_rejects_merge_file_with_wrong_heads(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")
    _write_migration(versions, "merge.py", "merge1", ("a", "c"))

    scope = {"allowed_paths": ["hyqs/pipeline/unrelated.py"]}
    changed_files = ["alembic/versions/merge.py"]

    assert check_out_of_lane(scope, changed_files, worktree=tmp_path) == changed_files


def test_check_out_of_lane_rejects_stale_head_set_merge_file(tmp_path: Path) -> None:
    """A down_revision pair that was a valid head set at some earlier point
    in the chain, but has since been superseded by a later revision, must
    not be exempted — only the *current* head set counts.
    """
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")
    # c advances past 'a', so the true current heads are now {b, c}.
    _write_migration(versions, "c.py", "c", "a")
    _write_migration(versions, "merge.py", "merge1", ("a", "b"))

    scope = {"allowed_paths": ["hyqs/pipeline/unrelated.py"]}
    changed_files = ["alembic/versions/merge.py"]

    assert check_out_of_lane(scope, changed_files, worktree=tmp_path) == changed_files


def test_check_out_of_lane_rejects_well_shaped_merge_file_outside_versions_dir(
    tmp_path: Path,
) -> None:
    """A genuinely correct revision/down_revision merge pair only counts as
    an alembic merge resolution when its changed-file path is actually under
    an alembic/versions/ directory — the exemption is path-scoped, not
    content-scoped.
    """
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")

    outside_dir = tmp_path / "hyqs" / "pipeline"
    outside_dir.mkdir(parents=True)
    (outside_dir / "merge.py").write_text("revision = 'merge1'\ndown_revision = ('a', 'b')\n")

    scope = {"allowed_paths": ["hyqs/pipeline/unrelated.py"]}
    changed_files = ["hyqs/pipeline/merge.py"]

    assert check_out_of_lane(scope, changed_files, worktree=tmp_path) == changed_files


def test_check_out_of_lane_rejects_unrelated_file_even_with_worktree(tmp_path: Path) -> None:
    scope = {"allowed_paths": ["hyqs/pipeline/unrelated.py"]}
    changed_files = ["hyqs/pipeline/other.py"]

    assert check_out_of_lane(scope, changed_files, worktree=tmp_path) == changed_files


def test_check_out_of_lane_without_worktree_arg_behaves_as_before(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")
    _write_migration(versions, "merge.py", "merge1", ("a", "b"))

    scope = {"allowed_paths": ["hyqs/pipeline/unrelated.py"]}
    changed_files = ["alembic/versions/merge.py"]

    # No third positional/keyword arg: the merge file is not exempted.
    assert check_out_of_lane(scope, changed_files) == changed_files


def test_check_alembic_head_collisions_flags_two_heads(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "root")

    messages = check_alembic_head_collisions(tmp_path, ["alembic/versions/b.py"])

    assert len(messages) == 1
    assert "alembic/versions" in messages[0]
    assert "a.py" in messages[0] and "b.py" in messages[0]


def test_check_alembic_head_collisions_clean_chain_returns_empty(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    _write_migration(versions, "root.py", "root", None)
    _write_migration(versions, "a.py", "a", "root")
    _write_migration(versions, "b.py", "b", "a")

    assert check_alembic_head_collisions(tmp_path, ["alembic/versions/b.py"]) == []


def test_check_alembic_head_collisions_skips_when_diff_has_no_alembic_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("discover_alembic_heads should not be called")

    monkeypatch.setattr("hyqs.pipeline.collision.discover_alembic_heads", _fail_if_called)

    changed_files = ["hyqs/pipeline/unrelated.py", "tests/test_unrelated.py"]

    assert check_alembic_head_collisions(tmp_path, changed_files) == []


def test_check_alembic_head_collisions_checks_multiple_directories_independently(
    tmp_path: Path,
) -> None:
    versions_a = tmp_path / "service_a" / "alembic" / "versions"
    _write_migration(versions_a, "root.py", "root", None)
    _write_migration(versions_a, "a.py", "a", "root")
    _write_migration(versions_a, "b.py", "b", "a")

    versions_b = tmp_path / "service_b" / "alembic" / "versions"
    _write_migration(versions_b, "root.py", "root", None)
    _write_migration(versions_b, "a.py", "a", "root")
    _write_migration(versions_b, "b.py", "b", "root")

    changed_files = [
        "service_a/alembic/versions/b.py",
        "service_b/alembic/versions/b.py",
    ]

    messages = check_alembic_head_collisions(tmp_path, changed_files)

    assert len(messages) == 1
    assert "service_b/alembic/versions" in messages[0]
    assert not any("service_a" in m for m in messages)


# --- survey_job_queue ----------------------------------------------------------


def test_survey_job_queue_reports_overlap_with_plan_declared_target_files() -> None:
    active_jobs = [
        {
            "id": 1,
            "idea": "Add a helper",
            "plan": {"stories": [{"id": "S1", "target_files": ["hyqs/pipeline/foo.py"]}]},
            "source_meta": None,
        }
    ]
    candidates = [{"key": "c1", "title": "Touch foo.py", "target_files": ["hyqs/pipeline/foo.py"]}]

    result = survey_job_queue(candidates, active_jobs)

    assert result.overlaps == {"c1": [1]}
    assert result.unknown_target_file_jobs == []


def test_survey_job_queue_reports_overlap_with_idea_declared_target_files() -> None:
    active_jobs = [
        {
            "id": 2,
            "idea": "Fix a bug. Target files: hyqs/pipeline/bar.py",
            "plan": None,
            "source_meta": None,
        }
    ]
    candidates = [{"key": "c1", "title": "Touch bar.py", "target_files": ["hyqs/pipeline/bar.py"]}]

    result = survey_job_queue(candidates, active_jobs)

    assert result.overlaps == {"c1": [2]}
    assert result.unknown_target_file_jobs == []


def test_survey_job_queue_reports_overlap_with_granted_scope_allowed_paths() -> None:
    active_jobs = [
        {
            "id": 3,
            "idea": "A job with no declared files up front",
            "plan": None,
            "source_meta": {"scope": {"allowed_paths": ["hyqs/pipeline/baz.py"]}},
        }
    ]
    candidates = [{"key": "c1", "title": "Touch baz.py", "target_files": ["hyqs/pipeline/baz.py"]}]

    result = survey_job_queue(candidates, active_jobs)

    assert result.overlaps == {"c1": [3]}
    assert result.unknown_target_file_jobs == []


def test_survey_job_queue_flags_job_with_no_extractable_scope_as_unknown() -> None:
    active_jobs = [
        {
            "id": 4,
            "idea": "A vague job idea with no file list",
            "plan": None,
            "source_meta": None,
        }
    ]
    candidates = [
        {"key": "c1", "title": "Touch something", "target_files": ["hyqs/pipeline/baz.py"]}
    ]

    result = survey_job_queue(candidates, active_jobs)

    assert result.overlaps == {"c1": []}
    assert result.unknown_target_file_jobs == [4]


def test_survey_job_queue_returns_empty_list_for_non_colliding_candidate() -> None:
    active_jobs = [
        {
            "id": 5,
            "idea": "Add a helper",
            "plan": {"stories": [{"id": "S1", "target_files": ["hyqs/pipeline/foo.py"]}]},
            "source_meta": None,
        }
    ]
    candidates = [
        {"key": "c1", "title": "Unrelated change", "target_files": ["hyqs/pipeline/other.py"]}
    ]

    result = survey_job_queue(candidates, active_jobs)

    assert result.overlaps == {"c1": []}
    assert result.unknown_target_file_jobs == []


def test_queue_survey_result_to_dict_round_trips_both_fields() -> None:
    result = QueueSurveyResult(overlaps={"c1": [1, 2]}, unknown_target_file_jobs=[3])

    assert result.to_dict() == {
        "overlaps": {"c1": [1, 2]},
        "unknown_target_file_jobs": [3],
    }


def test_check_overlapping_merge_hunks_flags_same_file_overlapping_ranges() -> None:
    job_patch = (
        "diff --git a/model_runner/server.py b/model_runner/server.py\n"
        "--- a/model_runner/server.py\n"
        "+++ b/model_runner/server.py\n"
        "@@ -10,4 +10,5 @@\n"
        " unchanged\n"
        "-old line\n"
        "+new job line\n"
    )
    base_patch = (
        "diff --git a/model_runner/server.py b/model_runner/server.py\n"
        "--- a/model_runner/server.py\n"
        "+++ b/model_runner/server.py\n"
        "@@ -11,2 +11,3 @@\n"
        " unchanged\n"
        "-old line\n"
        "+new base line\n"
    )

    messages = check_overlapping_merge_hunks(job_patch, base_patch)

    assert len(messages) == 1
    assert "model_runner/server.py" in messages[0]
    assert "11" in messages[0]


def test_check_overlapping_merge_hunks_ignores_disjoint_ranges() -> None:
    job_patch = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,3 +1,3 @@\n a\n"
    base_patch = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -50,3 +50,3 @@\n z\n"

    assert check_overlapping_merge_hunks(job_patch, base_patch) == []


def test_check_overlapping_merge_hunks_ignores_different_files() -> None:
    job_patch = "diff --git a/a.py b/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n a\n"
    base_patch = "diff --git a/b.py b/b.py\n+++ b/b.py\n@@ -1,3 +1,3 @@\n b\n"

    assert check_overlapping_merge_hunks(job_patch, base_patch) == []


def test_check_overlapping_merge_hunks_empty_patches_return_empty() -> None:
    assert check_overlapping_merge_hunks("", "") == []
    assert check_overlapping_merge_hunks("some text with no hunks", "") == []
