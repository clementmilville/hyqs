"""Tests for target_files support in the plan schema and build prompt."""

import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hyqs.pipeline import agents
from hyqs.pipeline.contracts import (
    SchemaValidationError,
    parse_already_satisfied_verification,
    parse_and_validate,
)
from hyqs.pipeline.models import Job, Usage
from hyqs.pipeline.stages import build as build_stage
from hyqs.pipeline.stages import plan as plan_stage

_STAGES_DIR = Path(__file__).resolve().parent.parent / "hyqs" / "pipeline" / "stages"


def _make_backend(text: str = "") -> MagicMock:
    result = MagicMock()
    result.text = text
    result.usage = Usage()
    backend = MagicMock()
    backend.run = AsyncMock(return_value=result)
    return backend


def _minimal_plan(**kwargs) -> dict:
    base = {
        "summary": "test plan",
        "stories": [
            {
                "id": "S1",
                "title": "Do it",
                "task": "do it",
                "acceptance": "done",
                "target_files": ["hyqs/pipeline/agents.py"],
            }
        ],
        "reuses": [],
        "adds": [],
        "file_impact": [
            {
                "path": "hyqs/pipeline/agents.py",
                "role": "implementation",
                "justification": "Update planner behavior",
                "status": "existing",
                "provenance": "idea requirement",
            }
        ],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "Internal pipeline behavior only.",
        },
        "target_files": ["hyqs/pipeline/agents.py"],
        "activation": {
            "config_change_required": False,
            "activation_location": "N/A",
            "expected_live_effect": "The planner behavior updates on merge.",
        },
    }
    base.update(kwargs)
    return base


def _wrap(plan: dict) -> str:
    return f"<<<RESULT_JSON>>>\n{json.dumps(plan)}\n<<<END_RESULT>>>"


def test_parse_and_validate_accepts_plan_with_target_files():
    plan = _minimal_plan()
    result = parse_and_validate("plan", _wrap(plan))
    assert result["target_files"] == ["hyqs/pipeline/agents.py"]


def test_parse_and_validate_rejects_new_plan_without_target_files():
    plan = _minimal_plan()
    plan.pop("target_files")
    with pytest.raises(SchemaValidationError, match="target_files"):
        parse_and_validate("plan", _wrap(plan))


def test_legacy_stored_plan_without_strict_fields_remains_readable(tmp_path):
    plan = _minimal_plan()
    plan.pop("target_files")
    plan.pop("file_impact")
    backend = _make_backend()

    asyncio.run(agents.build(backend, "legacy idea", plan, str(tmp_path)))

    assert "## Files to edit/create" not in backend.run.call_args.kwargs["prompt"]


def test_plan_contract_accepts_api_visible_frontend_impact():
    plan = _minimal_plan(
        ui_impact={
            "touches_backend_surface": True,
            "frontend_changes": ["Update the API client and rendered field."],
            "no_ui_change_reason": "",
        }
    )
    assert parse_and_validate("plan", _wrap(plan))["ui_impact"]["frontend_changes"]


def test_plan_contract_rejects_api_visible_plan_without_frontend_impact():
    plan = _minimal_plan(
        ui_impact={
            "touches_backend_surface": True,
            "frontend_changes": [],
            "no_ui_change_reason": "Claimed omission.",
        }
    )
    with pytest.raises(SchemaValidationError, match="required frontend_changes"):
        parse_and_validate("plan", _wrap(plan))


@pytest.mark.parametrize(
    "path", ["/tmp/x.py", "C:/tmp/x.py", "../x.py", "./x.py", "a//b.py", "a\\b.py"]
)
def test_plan_contract_rejects_unsafe_or_noncanonical_paths(path):
    plan = _minimal_plan(target_files=[path])
    with pytest.raises(SchemaValidationError, match="canonical|safe"):
        parse_and_validate("plan", _wrap(plan))


def test_plan_contract_rejects_exact_union_mismatch():
    plan = _minimal_plan(target_files=["hyqs/pipeline/agents.py", "extra.py"])
    with pytest.raises(SchemaValidationError, match=r"exactly equal.*extra=.*extra.py"):
        parse_and_validate("plan", _wrap(plan))


def test_plan_contract_rejects_duplicate_story_ids():
    plan = _minimal_plan()
    plan["stories"].append({**plan["stories"][0], "depends_on": ["S1"]})
    with pytest.raises(SchemaValidationError, match="duplicate story id"):
        parse_and_validate("plan", _wrap(plan))


def test_plan_contract_rejects_forward_or_unknown_dependency():
    plan = _minimal_plan()
    plan["stories"][0]["depends_on"] = ["S2"]
    with pytest.raises(SchemaValidationError, match="unknown or non-earlier"):
        parse_and_validate("plan", _wrap(plan))


def test_plan_contract_adds_missing_test_only_story_implementation_dependency():
    plan = _minimal_plan()
    plan["stories"].append(
        {
            "id": "S2",
            "title": "Test it",
            "task": "exercise S1",
            "acceptance": "covered",
            "target_files": ["tests/test_agents.py"],
        }
    )
    plan["file_impact"].append(
        {
            "path": "tests/test_agents.py",
            "role": "regression tests",
            "justification": "Exercise planner changes",
            "status": "new",
            "provenance": "S2",
        }
    )
    plan["target_files"].append("tests/test_agents.py")

    result = parse_and_validate("plan", _wrap(plan))

    assert result["stories"][1]["depends_on"] == ["S1"]


def test_plan_contract_preserves_declared_test_dependencies_before_added_ones():
    plan = _minimal_plan()
    second = {
        **plan["stories"][0],
        "id": "S2",
        "title": "Second implementation",
        "target_files": ["hyqs/pipeline/contracts.py"],
    }
    plan["stories"].extend(
        [
            second,
            {
                "id": "S3",
                "title": "Test both",
                "task": "exercise both implementations",
                "acceptance": "covered",
                "target_files": ["tests/test_agents.py"],
                "depends_on": ["S2"],
            },
        ]
    )
    plan["file_impact"].extend(
        [
            {
                "path": "hyqs/pipeline/contracts.py",
                "role": "second implementation",
                "justification": "Exercise dependency completion",
                "status": "existing",
                "provenance": "S2",
            },
            {
                "path": "tests/test_agents.py",
                "role": "regression tests",
                "justification": "Exercise both implementations",
                "status": "new",
                "provenance": "S3",
            },
        ]
    )
    plan["target_files"].extend(["hyqs/pipeline/contracts.py", "tests/test_agents.py"])

    result = parse_and_validate("plan", _wrap(plan))

    assert result["stories"][2]["depends_on"] == ["S2", "S1"]


@pytest.mark.parametrize("suffix", ["js", "jsx", "ts", "tsx"])
def test_plan_contract_recognizes_spec_frontend_files_as_test_only(suffix):
    test_path = f"admin_ui/src/api.spec.{suffix}"
    plan = _minimal_plan()
    plan["stories"].append(
        {
            "id": "S2",
            "title": "Test it",
            "task": "exercise S1",
            "acceptance": "covered",
            "target_files": [test_path],
        }
    )
    plan["file_impact"].append(
        {
            "path": test_path,
            "role": "regression tests",
            "justification": "Exercise planner changes",
            "status": "new",
            "provenance": "S2",
        }
    )
    plan["target_files"].append(test_path)

    result = parse_and_validate("plan", _wrap(plan))

    assert result["stories"][1]["depends_on"] == ["S1"]


def test_already_satisfied_contract_accepts_pass_and_fail():
    for verdict in ("pass", "fail"):
        result = parse_already_satisfied_verification(
            _wrap({"verdict": verdict, "evidence": "GET /health is registered"})
        )
        assert result["verdict"] == verdict


def test_already_satisfied_contract_rejects_missing_or_invalid_fields():
    for result in (
        {"verdict": "maybe", "evidence": "uncertain"},
        {"verdict": "pass"},
        {"verdict": "fail", "evidence": ""},
        {"verdict": "pass", "evidence": "   "},
    ):
        with pytest.raises(SchemaValidationError):
            parse_already_satisfied_verification(_wrap(result))


def test_build_prompt_includes_files_section_when_target_files_present(tmp_path):
    plan = _minimal_plan(target_files=["hyqs/pipeline/agents.py", "tests/test_foo.py"])
    backend = _make_backend()

    asyncio.run(agents.build(backend, "test idea", plan, str(tmp_path)))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert (
        "## Files to edit/create (planner-identified edit targets — a focused subset of the repository files listed above; open these first)"
        in prompt
    )
    assert "- hyqs/pipeline/agents.py" in prompt
    assert "- tests/test_foo.py" in prompt


def test_build_and_fix_prompts_preserve_complete_planner_scope(tmp_path):
    plan = _minimal_plan(
        stories=[
            {
                "id": "S1",
                "title": "Implement",
                "task": "Change execution integration",
                "acceptance": "Typed identity is required",
                "target_files": ["hyqs/pipeline/runner.py"],
                "depends_on": [],
            },
            {
                "id": "S2",
                "title": "Regress",
                "task": "Cover recovery",
                "acceptance": "Mismatches remain untouched",
                "target_files": ["tests/test_pipeline_runner.py"],
                "depends_on": ["S1"],
            },
        ],
        file_impact=[
            {
                "path": "hyqs/pipeline/runner.py",
                "role": "execution integration",
                "justification": "Verify typed PR identity",
                "status": "existing",
                "provenance": "S1",
            },
            {
                "path": "tests/test_pipeline_runner.py",
                "role": "focused regression",
                "justification": "Cover mismatched identities",
                "status": "existing",
                "provenance": "S2",
            },
        ],
        target_files=["hyqs/pipeline/runner.py", "tests/test_pipeline_runner.py"],
    )
    build_backend = _make_backend()
    fix_backend = _make_backend()

    asyncio.run(agents.build(build_backend, "idea", plan, str(tmp_path)))
    asyncio.run(
        agents.fix(
            fix_backend,
            "idea",
            "failed source job is being rebuilt on current main",
            str(tmp_path),
            plan_data=plan,
        )
    )

    for prompt in (
        build_backend.run.call_args.kwargs["prompt"],
        fix_backend.run.call_args.kwargs["prompt"],
    ):
        assert '"depends_on": [\n        "S1"' in prompt
        assert '"target_files": [\n        "tests/test_pipeline_runner.py"' in prompt
        assert '"role": "focused regression"' in prompt
        assert "absent or stale prior diff" in prompt
        assert "does not authorize dropping" in prompt


def test_build_prompt_omits_files_section_when_target_files_absent(tmp_path):
    plan = _minimal_plan()
    plan.pop("target_files")
    backend = _make_backend()

    asyncio.run(agents.build(backend, "test idea", plan, str(tmp_path)))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Files to edit/create" not in prompt


def test_plan_prompt_orders_stable_context_before_request_and_guides_complete_splits(
    tmp_path,
):
    backend = _make_backend(_wrap(_minimal_plan()))
    (tmp_path / "CONVENTIONS.md").write_text("CONVENTION_MARKER")

    asyncio.run(
        agents.plan(
            backend,
            "IDEA_MARKER",
            str(tmp_path),
            file_manifest="MANIFEST_MARKER",
            symbol_context="SYMBOL_MARKER",
            decision_digest="DECISION_MARKER",
        )
    )

    prompt = backend.run.call_args.kwargs["prompt"]
    assert (
        prompt.index("MANIFEST_MARKER")
        < prompt.index("SYMBOL_MARKER")
        < prompt.index("CONVENTION_MARKER")
        < prompt.index("DECISION_MARKER")
        < prompt.index("IDEA_MARKER")
        < prompt.index("Produce the plan.")
    )
    assert "NEVER shrink an oversized plan by dropping required files" in " ".join(
        agents.PLAN_SYS.split()
    )
    assert "split them deterministically" in agents.PLAN_SYS


def test_plan_prompt_schema_exemplar_includes_required_file_impact():
    schema_line = next(
        line for line in agents.PLAN_SYS.splitlines() if line.startswith("matching the schema:")
    )

    assert '"file_impact": [' in schema_line
    assert all(
        f'"{field}"' in schema_line
        for field in ("path", "role", "justification", "status", "provenance")
    )


def test_plan_contract_correction_is_compact_and_excludes_heavy_context(tmp_path):
    invalid = _minimal_plan()
    invalid.pop("file_impact")
    first = MagicMock(text=_wrap(invalid), usage=Usage())
    second = MagicMock(text=_wrap(_minimal_plan()), usage=Usage())
    backend = MagicMock()
    backend.run = AsyncMock(side_effect=[first, second])
    heavy_manifest = "MANIFEST_MARKER " + ("m" * 2_000)
    heavy_idea = "IDEA_MARKER " + ("i" * 2_000)
    (tmp_path / "CONVENTIONS.md").write_text("CONVENTION_MARKER " + ("c" * 2_000))

    result, _ = asyncio.run(
        agents.plan(
            backend,
            heavy_idea,
            str(tmp_path),
            file_manifest=heavy_manifest,
            symbol_context="SYMBOL_MARKER",
            decision_digest="DECISION_MARKER",
        )
    )

    initial = backend.run.call_args_list[0].kwargs["prompt"]
    correction = backend.run.call_args_list[1].kwargs["prompt"]
    assert result["file_impact"]
    assert len(correction) < len(initial)
    assert "Validation error:" in correction
    assert "Previous Structured Plan or Output" in correction
    assert '"summary": "test plan"' in correction
    for marker in (
        "MANIFEST_MARKER",
        "IDEA_MARKER",
        "CONVENTION_MARKER",
        "SYMBOL_MARKER",
        "DECISION_MARKER",
    ):
        assert marker not in correction
    assert correction.endswith("Return the corrected complete plan.")


def test_build_prompt_includes_bounded_no_diff_evidence_only_when_supplied(tmp_path):
    backend = _make_backend()
    evidence = "missing route " + ("x" * 3_000)

    asyncio.run(
        agents.build(
            backend,
            "test idea",
            _minimal_plan(),
            str(tmp_path),
            no_diff_retry_evidence=evidence,
        )
    )

    prompt = backend.run.call_args.kwargs["prompt"]
    assert prompt.count("Independent no-diff verifier evidence") == 1
    evidence_block = prompt.split("<<<VERIFIER_EVIDENCE>>>", 1)[1].split(
        "<<<END_VERIFIER_EVIDENCE>>>", 1
    )[0]
    assert len(evidence_block.strip()) == agents.NO_DIFF_RETRY_EVIDENCE_LIMIT

    ordinary_backend = _make_backend()
    asyncio.run(agents.build(ordinary_backend, "test idea", _minimal_plan(), str(tmp_path)))
    assert (
        "Independent no-diff verifier evidence"
        not in ordinary_backend.run.call_args.kwargs["prompt"]
    )


def test_build_stage_refreshes_base_before_creating_worktree(tmp_path, monkeypatch):
    calls = []
    managed = tmp_path / "managed"
    rn = MagicMock()
    rn.worktrees = tmp_path / "worktrees"
    rn._managed_repo = AsyncMock(return_value=managed)
    rn._backend.return_value = MagicMock()
    rn._fail = AsyncMock()
    job = Job(id=2276, idea="dependent", repo_path=str(managed), chat_id=1)

    async def _default_branch(repo):
        calls.append(("default_branch", repo))
        return "main"

    async def _fresh_base(repo, base):
        calls.append(("fresh_base", repo, base))
        return "main"

    async def _create_worktree(repo, branch, path, base=""):
        calls.append(("create_worktree", repo, branch, path, base))
        return MagicMock(ok=False, stderr="stop after plumbing")

    monkeypatch.setattr(build_stage.gitops, "default_branch", _default_branch)
    monkeypatch.setattr(build_stage.gitops, "fresh_base", _fresh_base)
    monkeypatch.setattr(build_stage.gitops, "create_worktree", _create_worktree)

    asyncio.run(build_stage.run(rn, job))

    assert calls == [
        ("default_branch", managed),
        ("fresh_base", managed, "main"),
        (
            "create_worktree",
            managed,
            "hyqs/job-2276",
            tmp_path / "worktrees" / "job-2276",
            "main",
        ),
    ]
    rn._fail.assert_awaited_once_with(job, "worktree create failed: stop after plumbing")


def test_plan_stage_refreshes_base_before_creating_detached_worktree(tmp_path, monkeypatch):
    calls = []
    managed = tmp_path / "managed"
    rn = MagicMock()
    rn.worktrees = tmp_path / "worktrees"
    rn._managed_repo = AsyncMock(return_value=managed)
    rn._backend.return_value = MagicMock()
    rn.notify = AsyncMock()
    job = Job(id=2277, idea="dependent", repo_path=str(managed), chat_id=1)

    async def _record(name, result, *args, **kwargs):
        calls.append((name, *args, kwargs))
        return result

    monkeypatch.setattr(
        plan_stage.gitops, "is_git_repo", lambda repo: _record("is_git_repo", True, repo)
    )
    monkeypatch.setattr(
        plan_stage.gitops,
        "default_branch",
        lambda repo: _record("default_branch", "main", repo),
    )
    monkeypatch.setattr(
        plan_stage.gitops,
        "fresh_base",
        lambda repo, base: _record("fresh_base", "origin/main", repo, base),
    )
    monkeypatch.setattr(
        plan_stage.gitops,
        "create_detached_worktree",
        lambda repo, path, ref="": _record(
            "create_detached_worktree", MagicMock(ok=True), repo, path, ref
        ),
    )
    monkeypatch.setattr(plan_stage.gitops, "tracked_files", AsyncMock(return_value=[]))
    monkeypatch.setattr(plan_stage.gitops, "remove_worktree", AsyncMock())
    monkeypatch.setattr(
        plan_stage.agents,
        "plan",
        AsyncMock(side_effect=RuntimeError("stop after plumbing")),
    )

    with pytest.raises(RuntimeError, match="stop after plumbing"):
        asyncio.run(plan_stage.run(rn, job))

    assert calls[1:] == [
        ("default_branch", managed, {}),
        ("fresh_base", managed, "main", {}),
        (
            "create_detached_worktree",
            managed,
            tmp_path / "worktrees" / "job-2277-plan",
            "origin/main",
            {},
        ),
    ]


def _stage_filenames() -> list[str]:
    return sorted(
        p.name for p in _STAGES_DIR.glob("*.py") if p.name not in ("__init__.py", "_common.py")
    )


def _rn_notify_offenders(kwarg: str) -> list[str]:
    offenders = []
    for filename in _stage_filenames():
        path = _STAGES_DIR / filename
        tree = ast.parse(path.read_text(), filename=filename)
        for node in ast.walk(tree):
            is_notify_call = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "notify"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "rn"
            )
            if is_notify_call and not any(kw.arg == kwarg for kw in node.keywords):
                offenders.append(f"{filename}:{node.lineno}")
    return offenders


def test_all_rn_notify_calls_pass_project_id():
    """Every rn.notify(...) call across all stage files must carry project_id=,
    or Slack notifications silently fail to resolve a project-scoped
    webhook/credential (see job #986)."""
    assert _rn_notify_offenders("project_id") == []


def test_all_rn_notify_calls_pass_job_id():
    """Every rn.notify(...) call across all stage files must carry job_id=,
    or Slack notifications can't be threaded per-job (see job #989)."""
    assert _rn_notify_offenders("job_id") == []


def _dag_story(sid: str, **kwargs) -> dict:
    base = {"id": sid, "title": sid, "task": "do it", "acceptance": "done"}
    base.update(kwargs)
    return base


def _component_for(sid: str, components: list[dict]) -> dict:
    """The single split-DAG component containing story ``sid``, or fail loudly."""
    matches = [c for c in components if any(s["id"] == sid for s in c["stories"])]
    assert len(matches) == 1, f"expected exactly one component containing {sid}, got {matches}"
    return matches[0]


def test_split_dag_fork_leaves_disjoint_successors_edge_free():
    # A shares a.py with B, and a different file (c.py) with C. B and C share no file
    # and declare no depends_on between them: a fork producing two independent branches.
    plan_data = {
        "stories": [
            _dag_story("A", target_files=["a.py", "c.py"]),
            _dag_story("B", target_files=["a.py"]),
            _dag_story("C", target_files=["c.py"]),
        ],
    }

    components = plan_stage._compute_split_chains(plan_data)

    assert components is not None
    component = _component_for("A", components)
    assert component["predecessors"]["A"] == []
    assert component["predecessors"]["B"] == ["A"]
    assert component["predecessors"]["C"] == ["A"]
    assert sorted(component["terminal_ids"]) == ["B", "C"]


def test_split_dag_join_records_both_independent_predecessors():
    # A and B are independent of each other but both share a file with C: a join.
    plan_data = {
        "stories": [
            _dag_story("A", target_files=["a.py"]),
            _dag_story("B", target_files=["b.py"]),
            _dag_story("C", target_files=["a.py", "b.py"]),
        ],
    }

    components = plan_stage._compute_split_chains(plan_data)

    assert components is not None
    component = _component_for("A", components)
    assert component["predecessors"]["A"] == []
    assert component["predecessors"]["B"] == []
    assert component["predecessors"]["C"] == ["A", "B"]
    assert component["terminal_ids"] == ["C"]


def test_split_dag_multi_file_overlap_gets_exact_predecessor_set():
    # D shares a distinct file with each of A, B, C and must get a predecessor edge
    # to each of them, no more and no less.
    plan_data = {
        "stories": [
            _dag_story("A", target_files=["a.py"]),
            _dag_story("B", target_files=["b.py"]),
            _dag_story("C", target_files=["c.py"]),
            _dag_story("D", target_files=["a.py", "b.py", "c.py"]),
        ],
    }

    components = plan_stage._compute_split_chains(plan_data)

    assert components is not None
    component = _component_for("A", components)
    assert component["predecessors"]["D"] == ["A", "B", "C"]
    assert component["terminal_ids"] == ["D"]


def test_split_dag_declared_dependency_honored_without_shared_files():
    plan_data = {
        "stories": [
            _dag_story("A", target_files=["a.py"]),
            _dag_story("B", target_files=["b.py"], depends_on=["A"]),
        ],
    }

    components = plan_stage._compute_split_chains(plan_data)

    assert components is not None
    component = _component_for("A", components)
    assert component["predecessors"]["A"] == []
    assert component["predecessors"]["B"] == ["A"]
    assert component["terminal_ids"] == ["B"]


def test_split_dag_declared_dependency_wins_over_file_tie_break_direction():
    # By file-order tie-break alone, A (earlier) would precede B. But B declares no
    # dependency on A; instead A declares depends_on B, reversing the direction.
    plan_data = {
        "stories": [
            _dag_story("A", target_files=["shared.py"], depends_on=["B"]),
            _dag_story("B", target_files=["shared.py"]),
        ],
    }

    components = plan_stage._compute_split_chains(plan_data)

    assert components is not None
    component = _component_for("A", components)
    assert component["predecessors"]["B"] == []
    assert component["predecessors"]["A"] == ["B"]
    assert component["terminal_ids"] == ["A"]


def test_split_dag_cycle_falls_back_to_sorted_id_linear_chain():
    plan_data = {
        "stories": [
            _dag_story("A", target_files=["a.py"], depends_on=["B"]),
            _dag_story("B", target_files=["b.py"], depends_on=["A"]),
        ],
    }

    components = plan_stage._compute_split_chains(plan_data)

    assert components is not None
    component = _component_for("A", components)
    assert [s["id"] for s in component["stories"]] == ["A", "B"]
    assert component["predecessors"]["A"] == []
    assert component["predecessors"]["B"] == ["A"]
    assert component["terminal_ids"] == ["B"]


def test_split_dag_unknown_scope_without_per_story_files_returns_none():
    plan_data = {
        "stories": [_dag_story("A"), _dag_story("B", target_files=["b.py"])],
    }

    assert plan_stage._compute_split_chains(plan_data) is None
