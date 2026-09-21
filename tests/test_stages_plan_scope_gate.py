"""Tests for the deterministic post-PLAN scope gate.

No Postgres needed — mocks the backend, gitops, and the runner, matching the
pattern used by tests/test_stages_design_review.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from hyqs.pipeline.collision import PlanningCandidate, validate_plan_scope_completeness
from hyqs.pipeline.gitops import GitResult
from hyqs.pipeline.models import Job, JobStatus, Stage, Usage
from hyqs.pipeline.stages import plan as plan_stage


def _make_job(**kwargs) -> Job:
    base = dict(
        id=1,
        idea="test idea",
        repo_path="/fake/repo",
        chat_id=99,
        stage=Stage.QUEUED,
        status=JobStatus.RUNNING,
        project_id=None,
        attempts=0,
    )
    base.update(kwargs)
    return Job(**base)


def _make_runner(tmp_path) -> MagicMock:
    rn = MagicMock()
    rn.worktrees = tmp_path
    rn.timeout = 60
    rn.store = MagicMock()
    rn.notify = AsyncMock()
    rn._backend = MagicMock(return_value=MagicMock())
    rn._managed_repo = AsyncMock(return_value="/fake/managed")
    rn._event = MagicMock()
    rn._record_resource = MagicMock()
    rn._fail = AsyncMock()
    return rn


def _story(sid: str, title: str = "do it", **kwargs) -> dict:
    base = {"id": sid, "title": title, "task": "do it", "acceptance": "done"}
    base.update(kwargs)
    return base


def _plan_data(n_stories: int = 1, n_target_files: int = 1, **kwargs) -> dict:
    base = {
        "summary": "test plan",
        "stories": [_story(f"S{i}") for i in range(1, n_stories + 1)],
        "reuses": [],
        "adds": [],
        "target_files": [f"file{i}.py" for i in range(1, n_target_files + 1)],
        "ui_impact": {"touches_backend_surface": False},
    }
    base.update(kwargs)
    return base


def _run_plan(rn: MagicMock, job: Job, plan_data: dict) -> None:
    with (
        patch.object(plan_stage.gitops, "is_git_repo", AsyncMock(return_value=True)),
        patch.object(plan_stage.gitops, "tracked_files", AsyncMock(return_value=[])),
        patch.object(
            plan_stage.gitops,
            "create_detached_worktree",
            AsyncMock(return_value=GitResult(ok=False, stdout="", stderr="no worktree", code=1)),
        ),
        patch.object(plan_stage.decisions, "load_digest", MagicMock(return_value="")),
        patch.object(plan_stage.agents, "plan", AsyncMock(return_value=(plan_data, Usage()))),
    ):
        asyncio.run(plan_stage.run(rn, job))


def _complete_plan(paths: list[str], *, status: str = "existing", **kwargs) -> dict:
    plan = {
        "summary": "complete",
        "stories": [_story("S1", target_files=paths)],
        "reuses": [],
        "adds": [],
        "file_impact": [
            {
                "path": path,
                "role": "implementation",
                "justification": "required",
                "status": status,
                "provenance": "story",
            }
            for path in paths
        ],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "internal-only change",
        },
        "target_files": paths,
    }
    plan.update(kwargs)
    return plan


def test_completeness_rejects_existing_path_missing_from_tracked_files():
    result = validate_plan_scope_completeness(
        _complete_plan(["src/missing.py"]), ["src/existing.py"], []
    )

    assert result.reason_codes == ("existing_path_not_tracked",)
    assert result.missing_paths == ("src/missing.py",)


def test_completeness_accepts_explicitly_new_file_impact_path():
    result = validate_plan_scope_completeness(
        _complete_plan(["src/new.py"], status="new"), ["src/existing.py"], []
    )

    assert result.passed
    assert result.manifest == ("src/new.py",)


def test_completeness_reports_plan_union_and_relationship_omissions():
    plan = _complete_plan(["src/model.py"])
    plan["target_files"] = ["src/other.py"]
    candidates = [
        PlanningCandidate("src/store.py", "existing", ("persistence_layer",)),
        PlanningCandidate("tests/test_model.py", "existing", ("nearest_test",)),
    ]

    result = validate_plan_scope_completeness(
        plan,
        ["src/model.py", "src/other.py", "src/store.py", "tests/test_model.py"],
        candidates,
    )

    assert result.reason_codes == (
        "persistence_omission",
        "plan_union_mismatch",
        "regression_test_omission",
    )
    assert set(result.missing_paths) == {
        "src/model.py",
        "src/other.py",
        "src/store.py",
        "tests/test_model.py",
    }


def test_completeness_internal_only_plan_does_not_require_frontend_candidate():
    plan = _complete_plan(["hyqs/pipeline/store.py"])
    candidate = PlanningCandidate("hyqs/web/frontend/src/api.js", "existing", ("indexed_caller",))

    result = validate_plan_scope_completeness(
        plan,
        ["hyqs/pipeline/store.py", "hyqs/web/frontend/src/api.js"],
        [candidate],
    )

    assert result.passed


def test_completeness_http_visible_plan_requires_frontend_path():
    plan = _complete_plan(["hyqs/web/app.py"])
    plan["ui_impact"] = {
        "touches_backend_surface": True,
        "frontend_changes": ["update API client"],
        "no_ui_change_reason": "",
    }

    result = validate_plan_scope_completeness(plan, ["hyqs/web/app.py"], [])

    assert result.reason_codes == ("frontend_coverage_omission",)


def test_oversized_stories_fails_before_build(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    plan_data = _plan_data(n_stories=6, n_target_files=1)

    _run_plan(rn, job, plan_data)

    rn._fail.assert_awaited_once()
    failed_job, message = rn._fail.call_args.args
    assert failed_job is job
    for i in range(1, 7):
        assert f"S{i}" in message
        assert "do it" in message
    assert "depends_on" in message
    assert job.stage != Stage.PLAN
    rn.store.save.assert_not_called()


def test_oversized_target_files_fails_before_build(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    plan_data = _plan_data(n_stories=1, n_target_files=9)

    _run_plan(rn, job, plan_data)

    rn._fail.assert_awaited_once()
    _, message = rn._fail.call_args.args
    assert "9 target_files" in message
    assert job.stage != Stage.PLAN
    rn.store.save.assert_not_called()


def test_plan_under_thresholds_proceeds_to_build(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    plan_data = _plan_data(n_stories=3, n_target_files=4)

    _run_plan(rn, job, plan_data)

    rn._fail.assert_not_called()
    assert job.stage == Stage.PLAN


def test_plan_passes_focused_candidates_before_complete_fallback(tmp_path):
    job = _make_job(idea="Change runner. Files: hyqs/pipeline/runner.py")
    rn = _make_runner(tmp_path)
    planner = AsyncMock(return_value=(_plan_data(), Usage()))
    files = ["hyqs/pipeline/runner.py", "tests/test_runner_deploy.py", "README.md"]
    with (
        patch.object(plan_stage.gitops, "is_git_repo", AsyncMock(return_value=True)),
        patch.object(plan_stage.gitops, "tracked_files", AsyncMock(return_value=files)),
        patch.object(
            plan_stage.gitops,
            "create_detached_worktree",
            AsyncMock(return_value=GitResult(ok=False, stdout="", stderr="no worktree", code=1)),
        ),
        patch.object(plan_stage.decisions, "load_digest", MagicMock(return_value="")),
        patch.object(plan_stage.agents, "plan", planner),
    ):
        asyncio.run(plan_stage.run(rn, job))

    kwargs = planner.await_args.kwargs
    manifest = kwargs["file_manifest"]
    focused_path = "hyqs/pipeline/runner.py [existing] provenance=explicit_path"
    assert focused_path in manifest
    assert "## Complete tracked repository fallback\n" + "\n".join(files) in manifest
    assert manifest.index(focused_path) < manifest.index("## Complete tracked repository fallback")
    assert job.status == JobStatus.PENDING
    rn.store.save.assert_called_once_with(job)


def test_scope_gate_bypass_allows_oversized_plan(tmp_path):
    job = _make_job(source_meta={"scope_gate_bypass": True})
    rn = _make_runner(tmp_path)
    plan_data = _plan_data(n_stories=9, n_target_files=11)

    _run_plan(rn, job, plan_data)

    rn._fail.assert_not_called()
    assert job.stage == Stage.PLAN
    assert job.status == JobStatus.PENDING
    rn.store.save.assert_called_once_with(job)


def _fake_created_jobs():
    """A store.create side_effect returning distinct fake Job-like objects with incrementing ids."""
    counter = {"n": 100}

    def _create(**kwargs):
        counter["n"] += 1
        job = MagicMock()
        job.id = counter["n"]
        return job

    return _create


def test_oversized_plan_with_independent_stories_splits_and_supersedes(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    rn.store.create = MagicMock(side_effect=_fake_created_jobs())
    # 9 stories, each with its own disjoint file: oversized on story count (>5) and
    # target_files count (>8), but every story is independent and small enough to split.
    stories = [_story(f"S{i}", target_files=[f"file{i}.py"]) for i in range(1, 10)]
    plan_data = _plan_data(
        n_stories=0,
        stories=stories,
        target_files=[f"file{i}.py" for i in range(1, 10)],
    )

    _run_plan(rn, job, plan_data)

    rn._fail.assert_not_called()
    assert rn.store.create.call_count == 9
    for call in rn.store.create.call_args_list:
        assert call.kwargs.get("depends_on") is None
    assert job.status == JobStatus.CANCELLED
    assert job.resolution == "superseded-by-split"
    assert "#101" in job.error and "#109" in job.error
    rn.store.save.assert_called_once_with(job)


def test_oversized_plan_with_shared_files_chains_sequentially(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    rn.store.create = MagicMock(side_effect=_fake_created_jobs())
    # 6 stories (>5, oversized on story count): S1/S2 share a.py, the rest are independent.
    stories = [
        _story("S1", target_files=["a.py"]),
        _story("S2", target_files=["a.py", "b.py"]),
        _story("S3", target_files=["c.py"]),
        _story("S4", target_files=["d.py"]),
        _story("S5", target_files=["e.py"]),
        _story("S6", target_files=["f.py"]),
    ]
    plan_data = _plan_data(
        n_stories=0,
        stories=stories,
        target_files=["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"],
    )

    _run_plan(rn, job, plan_data)

    rn._fail.assert_not_called()
    calls = rn.store.create.call_args_list
    assert len(calls) == 6
    # S1 -> S2 chain (share a.py): S1 created first (id 101), S2's job depends on it.
    assert calls[0].kwargs.get("depends_on") is None
    assert calls[1].kwargs.get("depends_on") == [101]
    # The remaining independent stories carry no dependency.
    for call in calls[2:]:
        assert call.kwargs.get("depends_on") is None
    assert job.status == JobStatus.CANCELLED
    assert job.resolution == "superseded-by-split"
    rn.store.save.assert_called_once_with(job)


def test_oversized_plan_fork_and_join_split_into_dag_not_one_chain(tmp_path):
    """S1 shares a file with each of S2 and S3 (fork); S2 and S3 share no file and
    declare no depends_on, so their created jobs must not depend on each other. S7
    then shares a file with both S2 and S3 (join): its job must depend on both."""
    job = _make_job()
    rn = _make_runner(tmp_path)
    rn.store.create = MagicMock(side_effect=_fake_created_jobs())
    stories = [
        _story("S1", target_files=["a.py", "b.py"]),
        _story("S2", target_files=["a.py", "x.py"]),
        _story("S3", target_files=["b.py", "y.py"]),
        _story("S4", target_files=["x.py", "y.py"]),
        _story("S5", target_files=["m.py"]),
        _story("S6", target_files=["n.py"]),
    ]
    plan_data = _plan_data(
        n_stories=0,
        stories=stories,
        target_files=["a.py", "b.py", "x.py", "y.py", "m.py", "n.py"],
    )

    _run_plan(rn, job, plan_data)

    rn._fail.assert_not_called()
    # Recover each story's create() kwargs and assigned job id from the mocked
    # incrementing-id side effect (ids are assigned 101, 102, ... in call order).
    kwargs_by_story = {}
    ids_by_story = {}
    for i, call in enumerate(rn.store.create.call_args_list):
        sid = call.kwargs["source_meta"]["story_id"]
        kwargs_by_story[sid] = call.kwargs
        ids_by_story[sid] = 101 + i

    # Fork: S1 is a shared predecessor of both S2 and S3; S2 and S3 don't depend on
    # each other.
    assert kwargs_by_story["S1"].get("depends_on") is None
    assert kwargs_by_story["S2"].get("depends_on") == [ids_by_story["S1"]]
    assert kwargs_by_story["S3"].get("depends_on") == [ids_by_story["S1"]]
    # Join: S4 shares a file with both S2 (x.py) and S3 (y.py), so its job depends
    # on both of their ids.
    assert sorted(kwargs_by_story["S4"].get("depends_on")) == sorted(
        [ids_by_story["S2"], ids_by_story["S3"]]
    )
    assert job.status == JobStatus.CANCELLED
    assert job.resolution == "superseded-by-split"


def test_split_repoints_dependents_and_records_traceability_event(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    rn.store.create = MagicMock(side_effect=_fake_created_jobs())
    rn.store.repoint_split_dependents = MagicMock(return_value=[55, 56])
    stories = [_story(f"S{i}", target_files=[f"file{i}.py"]) for i in range(1, 10)]
    plan_data = _plan_data(
        n_stories=0,
        stories=stories,
        target_files=[f"file{i}.py" for i in range(1, 10)],
    )

    _run_plan(rn, job, plan_data)

    ids = list(range(101, 110))
    rn.store.repoint_split_dependents.assert_called_once_with(job.id, ids)
    assert rn.store.add_event.call_count == 2
    for call, dep_id in zip(rn.store.add_event.call_args_list, [55, 56]):
        args, kwargs = call
        assert args[0] == dep_id
        assert args[1] == "plan"
        assert args[2] == "info"
        assert f"#{job.id}" in kwargs["summary"]
        assert kwargs["detail"] == {"repointed_from": job.id, "repointed_to": ids}
    # All 9 stories are independent (own file each, no shared files/depends_on), so
    # every created job is terminal — the split-cancelled event and job.error/chat
    # notification describe the DAG's terminal set rather than a serial id range.
    cancelled_call = next(c for c in rn._event.call_args_list if c.args[2] == "cancelled")
    assert cancelled_call.kwargs["detail"]["terminal_job_ids"] == ids
    for i in ids:
        assert f"#{i}" in job.error
    notify_text = rn.notify.await_args.args[1]
    assert "terminal:" in notify_text
    for i in ids:
        assert f"#{i}" in notify_text


def test_split_with_no_dependents_makes_no_repoint_traceability_calls(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    rn.store.create = MagicMock(side_effect=_fake_created_jobs())
    # rn.store.repoint_split_dependents is left as an unconfigured MagicMock:
    # calling it returns another MagicMock, which iterates as empty by
    # default — exercising the "job has no dependents" path.
    stories = [_story(f"S{i}", target_files=[f"file{i}.py"]) for i in range(1, 10)]
    plan_data = _plan_data(
        n_stories=0,
        stories=stories,
        target_files=[f"file{i}.py" for i in range(1, 10)],
    )

    _run_plan(rn, job, plan_data)

    rn.store.repoint_split_dependents.assert_called_once()
    rn.store.add_event.assert_not_called()


def test_oversized_plan_without_per_story_files_falls_back_to_fail(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    rn.store.create = MagicMock()
    plan_data = _plan_data(n_stories=6, n_target_files=1)

    _run_plan(rn, job, plan_data)

    rn.store.create.assert_not_called()
    rn._fail.assert_awaited_once()
    assert job.status != JobStatus.CANCELLED


def test_oversized_plan_with_one_story_still_too_big_falls_back_to_fail(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    rn.store.create = MagicMock()
    # S1 alone itemizes more than _MAX_PLAN_TARGET_FILES files; the plan is oversized
    # overall (10 target_files total), but a per-story split can't help S1.
    stories = [
        _story("S1", target_files=[f"file{i}.py" for i in range(1, 10)]),
        _story("S2", target_files=["other.py"]),
    ]
    plan_data = _plan_data(
        n_stories=0,
        stories=stories,
        target_files=[f"file{i}.py" for i in range(1, 10)] + ["other.py"],
    )

    _run_plan(rn, job, plan_data)

    rn.store.create.assert_not_called()
    rn._fail.assert_awaited_once()
    assert job.status != JobStatus.CANCELLED


def _run_plan_with_side_effect(rn: MagicMock, job: Job, side_effect) -> MagicMock:
    plan_mock = AsyncMock(side_effect=side_effect)
    with (
        patch.object(plan_stage.gitops, "is_git_repo", AsyncMock(return_value=True)),
        patch.object(plan_stage.gitops, "tracked_files", AsyncMock(return_value=[])),
        patch.object(
            plan_stage.gitops,
            "create_detached_worktree",
            AsyncMock(return_value=GitResult(ok=False, stdout="", stderr="no worktree", code=1)),
        ),
        patch.object(plan_stage.decisions, "load_digest", MagicMock(return_value="")),
        patch.object(plan_stage.agents, "plan", plan_mock),
    ):
        asyncio.run(plan_stage.run(rn, job))
    return plan_mock


def test_reask_triggers_once_and_succeeds(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    # First plan: 6 stories (oversized), no per-story target_files -> unsplittable.
    oversized_plan = _plan_data(n_stories=6, n_target_files=1)
    # Reask plan: fits within limits.
    fixed_plan = _plan_data(n_stories=2, n_target_files=2)

    plan_mock = _run_plan_with_side_effect(
        rn, job, [(oversized_plan, Usage()), (fixed_plan, Usage())]
    )

    assert plan_mock.await_count == 2
    reask_call = plan_mock.await_args_list[1]
    reask_context = reask_call.kwargs["reask_context"]
    assert "S1" in reask_context
    assert "no target_files were itemized" in reask_context
    rn._fail.assert_not_called()
    assert job.stage == Stage.PLAN
    assert job.status == JobStatus.PENDING
    assert job.plan_reask_attempts == 1
    rn.store.save.assert_called_once_with(job)


def test_reask_still_oversized_falls_back_to_fail(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    oversized_plan = _plan_data(n_stories=6, n_target_files=1)

    plan_mock = _run_plan_with_side_effect(
        rn, job, [(oversized_plan, Usage()), (oversized_plan, Usage())]
    )

    assert plan_mock.await_count == 2
    rn._fail.assert_awaited_once()
    assert job.status != JobStatus.CANCELLED
    assert job.plan_reask_attempts == 1
    rn.store.mark_needs_split.assert_called_once_with(job.id)
    assert job.needs_split is True
    _, message = rn._fail.call_args.args
    assert "parked" in message
    assert "bare retry is refused" in message
    assert "refile" in message
    assert "force=true" in message


def test_reask_still_oversized_parks_job_and_marks_needs_split(tmp_path):
    """Companion to test_reask_still_oversized_falls_back_to_fail: the park path
    is only reached after the bounded re-ask already ran (chains stays None both
    times), so this asserts the parking side effects in isolation."""
    job = _make_job()
    rn = _make_runner(tmp_path)
    oversized_plan = _plan_data(n_stories=1, n_target_files=9)

    _run_plan_with_side_effect(rn, job, [(oversized_plan, Usage()), (oversized_plan, Usage())])

    rn.store.mark_needs_split.assert_called_once_with(job.id)
    assert job.needs_split is True
    rn._fail.assert_awaited_once()
    _, message = rn._fail.call_args.args
    assert "refile a new, smaller job" in message


def test_reask_not_attempted_when_budget_already_spent(tmp_path):
    job = _make_job(plan_reask_attempts=1)
    rn = _make_runner(tmp_path)
    oversized_plan = _plan_data(n_stories=6, n_target_files=1)

    plan_mock = _run_plan_with_side_effect(rn, job, [(oversized_plan, Usage())])

    assert plan_mock.await_count == 1
    rn._fail.assert_awaited_once()
    assert job.plan_reask_attempts == 1


def test_scope_gate_message_groups_stories_by_shared_target_files():
    plan_data = {
        "stories": [
            _story("S1", target_files=["a.py"]),
            _story("S2", target_files=["a.py", "b.py"]),
            _story("S3", target_files=["c.py"]),
        ],
        "target_files": ["a.py", "b.py", "c.py"],
    }

    message = plan_stage._scope_gate_message(plan_data)

    assert "S1: do it — target_files: a.py" in message
    assert "S3: do it — target_files: c.py" in message
    assert "S1 -> S2" in message


def test_compute_split_chains_groups_independent_stories():
    plan_data = {
        "stories": [
            _story("S1", target_files=["a.py"]),
            _story("S2", target_files=["b.py"]),
            _story("S3", target_files=["c.py"]),
        ],
    }

    components = plan_stage._compute_split_chains(plan_data)

    assert components is not None
    assert sorted(c["stories"][0]["id"] for c in components) == ["S1", "S2", "S3"]
    for component in components:
        sid = component["stories"][0]["id"]
        assert len(component["stories"]) == 1
        assert component["predecessors"] == {sid: []}
        assert component["terminal_ids"] == [sid]


def test_compute_split_chains_chains_stories_sharing_files():
    plan_data = {
        "stories": [
            _story("S1", target_files=["a.py"]),
            _story("S2", target_files=["a.py", "b.py"]),
            _story("S3", target_files=["c.py"]),
        ],
    }

    components = plan_stage._compute_split_chains(plan_data)

    assert components is not None
    chained = [c for c in components if len(c["stories"]) > 1]
    independent = [c for c in components if len(c["stories"]) == 1]
    assert len(chained) == 1
    assert [s["id"] for s in chained[0]["stories"]] == ["S1", "S2"]
    assert chained[0]["predecessors"] == {"S1": [], "S2": ["S1"]}
    assert chained[0]["terminal_ids"] == ["S2"]
    assert len(independent) == 1
    assert independent[0]["stories"][0]["id"] == "S3"


def test_compute_split_chains_returns_none_without_per_story_files():
    plan_data = {
        "stories": [_story("S1"), _story("S2")],
    }

    assert plan_stage._compute_split_chains(plan_data) is None


def test_compute_split_chains_returns_none_when_one_story_still_oversized():
    plan_data = {
        "stories": [
            _story("S1", target_files=[f"file{i}.py" for i in range(1, 10)]),
            _story("S2", target_files=["other.py"]),
        ],
    }

    assert plan_stage._compute_split_chains(plan_data) is None


def test_compute_split_chains_returns_none_when_depends_on_chain_has_oversized_story():
    # A depends_on-only edge (no shared files) still pulls stories into one chain,
    # and the existing oversize guard must still fire within that chain.
    plan_data = {
        "stories": [
            _story("S1", target_files=[f"file{i}.py" for i in range(1, 10)]),
            _story("S2", target_files=["other.py"], depends_on=["S1"]),
        ],
    }

    assert plan_stage._compute_split_chains(plan_data) is None


def test_compute_split_chains_serializes_declared_dependency_without_shared_files():
    # S2 depends_on S1 but shares no target_files — must be pulled into the same
    # component and given a direct predecessor edge, not two independent components.
    plan_data = {
        "stories": [
            _story("S1", target_files=["a.py"]),
            _story("S2", target_files=["b.py"], depends_on=["S1"]),
        ],
    }

    components = plan_stage._compute_split_chains(plan_data)

    assert components is not None
    assert len(components) == 1
    assert [s["id"] for s in components[0]["stories"]] == ["S1", "S2"]
    assert components[0]["predecessors"] == {"S1": [], "S2": ["S1"]}
    assert components[0]["terminal_ids"] == ["S2"]


def test_compute_split_chains_puts_test_only_story_last_via_depends_on():
    # A file-disjoint test-only story that depends_on every other story is pulled
    # into one component and ordered last (after all the stories it exercises), with
    # a direct predecessor edge to each of them (a join, not a chain).
    plan_data = {
        "stories": [
            _story("S1", target_files=["primitives.py"]),
            _story("S2", target_files=["router.py"]),
            _story("S3", target_files=["session.py"]),
            _story(
                "S6",
                title="tests",
                target_files=["tests/test_login.py"],
                depends_on=["S1", "S2", "S3"],
            ),
        ],
    }

    components = plan_stage._compute_split_chains(plan_data)

    assert components is not None
    assert len(components) == 1
    component = components[0]
    story_ids = [s["id"] for s in component["stories"]]
    assert story_ids[-1] == "S6"
    assert set(story_ids) == {"S1", "S2", "S3", "S6"}
    assert component["predecessors"]["S6"] == ["S1", "S2", "S3"]
    assert component["predecessors"]["S1"] == []
    assert component["predecessors"]["S2"] == []
    assert component["predecessors"]["S3"] == []
    assert component["terminal_ids"] == ["S6"]


def test_compute_split_chains_ignores_depends_on_cycle_falls_back_to_sorted_id():
    # A declared cycle between two file-disjoint stories must not raise or infinite
    # loop — fall back to a deterministic sorted-id linear chain for that component.
    plan_data = {
        "stories": [
            _story("S1", target_files=["a.py"], depends_on=["S2"]),
            _story("S2", target_files=["b.py"], depends_on=["S1"]),
        ],
    }

    components = plan_stage._compute_split_chains(plan_data)

    assert components is not None
    assert len(components) == 1
    component = components[0]
    assert [s["id"] for s in component["stories"]] == ["S1", "S2"]
    assert component["predecessors"] == {"S1": [], "S2": ["S1"]}
    assert component["terminal_ids"] == ["S2"]


def test_compute_split_chains_no_depends_on_matches_prior_independent_grouping():
    # Back-compat: stories with no depends_on and no shared files still produce
    # independent single-story components exactly as before.
    plan_data = {
        "stories": [
            _story("S1", target_files=["a.py"]),
            _story("S2", target_files=["b.py"]),
            _story("S3", target_files=["c.py"]),
        ],
    }

    components = plan_stage._compute_split_chains(plan_data)

    assert components is not None
    assert sorted(c["stories"][0]["id"] for c in components) == ["S1", "S2", "S3"]
    for component in components:
        assert len(component["stories"]) == 1


def test_plan_touches_migration_matches_alembic_versions_path():
    plan_data = {
        "stories": [_story("S1", target_files=["alembic/versions/0012_add_col.py"])],
    }

    assert plan_stage._plan_touches_migration(plan_data) is True


def test_plan_touches_migration_matches_bare_versions_path():
    plan_data = {
        "target_files": ["versions/0012_add_col.py"],
        "stories": [_story("S1", target_files=["models/x.py"])],
    }

    assert plan_stage._plan_touches_migration(plan_data) is True


def test_plan_touches_migration_false_for_ordinary_files():
    plan_data = {
        "stories": [_story("S1", target_files=["models/x.py", "routers/y.py"])],
        "target_files": ["models/x.py", "routers/y.py"],
    }

    assert plan_stage._plan_touches_migration(plan_data) is False


def test_migration_bearing_plan_is_never_split_or_parked(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    # 7 stories (>5, oversized on story count), but one carries an Alembic migration
    # path: the plan must build as one job, not split or park.
    stories = [_story(f"S{i}", target_files=[f"file{i}.py"]) for i in range(1, 7)]
    stories.append(_story("S7", target_files=["alembic/versions/0012_add_col.py"]))
    plan_data = _plan_data(
        n_stories=0,
        stories=stories,
        target_files=[f"file{i}.py" for i in range(1, 7)] + ["alembic/versions/0012_add_col.py"],
    )

    _run_plan(rn, job, plan_data)

    rn._fail.assert_not_called()
    rn.store.mark_needs_split.assert_not_called()
    assert job.needs_split is False
    assert job.stage == Stage.PLAN
    assert job.status == JobStatus.PENDING
    rn.store.save.assert_called_once_with(job)


def test_reconcile_plan_scope_called_on_normal_successful_plan(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    rn.store.reconcile_plan_scope = MagicMock(return_value=[])
    plan_data = _plan_data(n_stories=3, n_target_files=4)

    _run_plan(rn, job, plan_data)

    rn.store.reconcile_plan_scope.assert_called_once_with(job.id, job.idea, plan_data)


def test_reconcile_plan_scope_called_on_bypassed_oversized_plan(tmp_path):
    job = _make_job(source_meta={"scope_gate_bypass": True})
    rn = _make_runner(tmp_path)
    rn.store.reconcile_plan_scope = MagicMock(return_value=[])
    plan_data = _plan_data(n_stories=9, n_target_files=11)

    _run_plan(rn, job, plan_data)

    rn.store.reconcile_plan_scope.assert_called_once_with(job.id, job.idea, plan_data)


def test_reconcile_plan_scope_not_called_when_plan_splits(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    rn.store.create = MagicMock(side_effect=_fake_created_jobs())
    rn.store.reconcile_plan_scope = MagicMock(return_value=[])
    stories = [_story(f"S{i}", target_files=[f"file{i}.py"]) for i in range(1, 10)]
    plan_data = _plan_data(
        n_stories=0,
        stories=stories,
        target_files=[f"file{i}.py" for i in range(1, 10)],
    )

    _run_plan(rn, job, plan_data)

    rn.store.reconcile_plan_scope.assert_not_called()


def test_reconcile_plan_scope_not_called_when_plan_parked_needs_split(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    rn.store.reconcile_plan_scope = MagicMock(return_value=[])
    oversized_plan = _plan_data(n_stories=6, n_target_files=1)

    _run_plan_with_side_effect(rn, job, [(oversized_plan, Usage()), (oversized_plan, Usage())])

    rn.store.reconcile_plan_scope.assert_not_called()


def test_reconcile_plan_scope_invoked_with_story_declared_test_path_not_in_idea(tmp_path):
    """Reproduces the reported bug: the idea's 'Target files:' list omits a test
    path that a story's own target_files declares. reconcile_plan_scope must be
    invoked with plan_data containing that story, so the store-level union (S1)
    would place the test path in allowed_paths before REVIEW/FIX's
    check_out_of_lane ever runs."""
    job = _make_job(idea="Add a widget. Target files: src/widget.py")
    rn = _make_runner(tmp_path)
    rn.store.reconcile_plan_scope = MagicMock(return_value=["tests/test_widget.py"])
    plan_data = _plan_data(
        n_stories=1,
        target_files=["src/widget.py"],
        stories=[_story("S1", target_files=["src/widget.py", "tests/test_widget.py"])],
    )

    _run_plan(rn, job, plan_data)

    rn.store.reconcile_plan_scope.assert_called_once_with(job.id, job.idea, plan_data)
    called_plan_data = rn.store.reconcile_plan_scope.call_args.args[2]
    assert any(
        "tests/test_widget.py" in story.get("target_files", [])
        for story in called_plan_data["stories"]
    )


def test_non_migration_plan_with_seven_stories_still_splits(tmp_path):
    job = _make_job()
    rn = _make_runner(tmp_path)
    rn.store.create = MagicMock(side_effect=_fake_created_jobs())
    # Same shape as above but with no migration path: existing split behavior applies.
    stories = [_story(f"S{i}", target_files=[f"file{i}.py"]) for i in range(1, 8)]
    plan_data = _plan_data(
        n_stories=0,
        stories=stories,
        target_files=[f"file{i}.py" for i in range(1, 8)],
    )

    _run_plan(rn, job, plan_data)

    rn._fail.assert_not_called()
    assert rn.store.create.call_count == 7
    assert job.status == JobStatus.CANCELLED
    assert job.resolution == "superseded-by-split"
