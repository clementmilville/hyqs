import asyncio
import json
import sys
import uuid

import pytest

from hyqs.pipeline.__main__ import _parse_args, _run_healthcheck, _run_plan_epic, _worker_is_healthy


def test_worker_is_healthy_true_for_fresh_heartbeat(store):
    asyncio.run(store.worker_heartbeat("w1", "host-a", 111, status="busy", now=1_000.0))
    assert _worker_is_healthy(store, [111], host="host-a", now=1_010.0, fresh_seconds=30.0)


def test_worker_is_healthy_false_for_stale_heartbeat(store):
    asyncio.run(store.worker_heartbeat("w2", "host-a", 222, status="busy", now=1_000.0))
    assert not _worker_is_healthy(store, [222], host="host-a", now=1_061.0, fresh_seconds=30.0)


def test_worker_is_healthy_false_when_no_heartbeat(store):
    assert not _worker_is_healthy(store, [999999], host="host-a", now=1_000.0)


def test_worker_is_healthy_true_when_child_pid_in_set_even_if_main_pid_differs(store):
    """Pinned uv-wrapper regression (job #634 follow-up): MainPID is the `uv
    run` wrapper, but the heartbeat is written by its python child pid. The
    gate must pass when the wrapper+child pid set is supplied, even though
    the heartbeat pid never equals the wrapper (MainPID)."""
    wrapper_pid, child_pid = 3236684, 3236693
    asyncio.run(store.worker_heartbeat("w3", "host-a", child_pid, status="busy", now=1_000.0))
    assert _worker_is_healthy(
        store, [wrapper_pid, child_pid], host="host-a", now=1_010.0, fresh_seconds=30.0
    )


def test_worker_is_healthy_false_when_only_wrapper_pid_supplied(store):
    wrapper_pid, child_pid = 3236684, 3236693
    asyncio.run(store.worker_heartbeat("w4", "host-a", child_pid, status="busy", now=1_000.0))
    assert not _worker_is_healthy(
        store, [wrapper_pid], host="host-a", now=1_010.0, fresh_seconds=30.0
    )


def test_worker_is_healthy_false_when_matching_pid_heartbeat_is_stale(store):
    wrapper_pid, child_pid = 3236684, 3236693
    asyncio.run(store.worker_heartbeat("w5", "host-a", child_pid, status="busy", now=1_000.0))
    assert not _worker_is_healthy(
        store, [wrapper_pid, child_pid], host="host-a", now=1_061.0, fresh_seconds=30.0
    )


def test_run_healthcheck_uses_store_dsn_from_config(monkeypatch, store):
    class _FakeConfig:
        db_url = store._dsn

    monkeypatch.setattr("hyqs.pipeline.__main__.Config.from_env", lambda: _FakeConfig())
    assert _run_healthcheck([999999]) is False


def test_parse_args_rejects_unknown_flag():
    with pytest.raises(SystemExit):
        _parse_args(["--bogus"])


def test_parse_args_requires_pid_for_healthcheck():
    with pytest.raises(SystemExit):
        _parse_args(["--healthcheck"])


def test_parse_args_healthcheck_with_pid():
    args = _parse_args(["--healthcheck", "--pid", "123"])
    assert args.healthcheck is True
    assert args.pids == [123]


def test_parse_args_healthcheck_with_multiple_pids():
    args = _parse_args(["--healthcheck", "--pid", "3236684,3236693"])
    assert args.healthcheck is True
    assert args.pids == [3236684, 3236693]


def test_main_healthcheck_never_starts_runner(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("should not start runner")

    monkeypatch.setattr("hyqs.pipeline.__main__.asyncio.run", _boom)
    monkeypatch.setattr(sys, "argv", ["hyqs-pipeline", "--healthcheck", "--pid", "999999"])

    from hyqs.pipeline.__main__ import main

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1


def test_parse_args_plan_epic_requires_goal():
    with pytest.raises(SystemExit):
        _parse_args(["--plan-epic", "5"])


def test_parse_args_plan_epic_with_goal():
    args = _parse_args(["--plan-epic", "5", "--goal", "Ship billing v2"])
    assert args.plan_epic == 5
    assert args.goal == "Ship billing v2"


def test_run_plan_epic_returns_error_for_unknown_epic(monkeypatch, store):
    class _FakeConfig:
        db_url = store._dsn

    monkeypatch.setattr("hyqs.pipeline.__main__.Config.from_env", lambda: _FakeConfig())

    assert _run_plan_epic(999999, "goal") == 1


def test_run_plan_epic_prints_dag_json_without_filing_jobs(monkeypatch, store, capsys):
    repo_path = f"/tmp/test-plan-epic-{uuid.uuid4()}"
    project = store.ensure_project_for_repo(repo_path)
    epic = store.create_epic(project.id, "Billing", "Handles invoices")

    class _FakeConfig:
        db_url = store._dsn

    async def _fake_architect(backend, prompt, cwd):
        yield {
            "type": "result",
            "summary": "Two-step plan",
            "rationale": "B needs A's schema",
            "jobs": [
                {
                    "title": "Job A",
                    "idea": "do A. done when: A works.",
                    "depends_on": [],
                    "priority": 0,
                    "scope": {"allowed_paths": ["a.py"], "interfaces": "exposes foo()"},
                }
            ],
        }

    def _must_not_file(*args, **kwargs):
        raise AssertionError("dry-run CLI must never file jobs")

    monkeypatch.setattr("hyqs.pipeline.__main__.Config.from_env", lambda: _FakeConfig())
    monkeypatch.setattr("hyqs.pipeline.providers.build_backend", lambda *a, **k: object())
    monkeypatch.setattr("hyqs.pipeline.agents.architect", _fake_architect)
    monkeypatch.setattr("hyqs.pipeline.store.JobStore.create_batch", _must_not_file)

    try:
        exit_code = _run_plan_epic(epic.id, "Ship billing v2")
    finally:
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM epics WHERE id = %s", (epic.id,))
            conn.execute("DELETE FROM project_agents WHERE project_id = %s", (project.id,))
            conn.execute("DELETE FROM projects WHERE id = %s", (project.id,))

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["summary"] == "Two-step plan"
    assert printed["rationale"] == "B needs A's schema"
    assert printed["jobs"][0]["scope"]["allowed_paths"] == ["a.py"]
