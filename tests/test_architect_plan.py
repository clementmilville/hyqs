"""Tests for the ARCHITECT epic-goal-to-job-DAG streaming flow in hyqs/pipeline/agents.py."""

import asyncio
import json
from unittest.mock import MagicMock

from hyqs.pipeline.agents import ARCHITECT_SYS, _build_architect_prompt, architect
from hyqs.pipeline.contracts import RESULT_END_MARKER, RESULT_START_MARKER


def _wrap(data: dict) -> str:
    return f"{RESULT_START_MARKER}\n{json.dumps(data)}\n{RESULT_END_MARKER}"


def _make_streaming_backend(events: list[dict]) -> MagicMock:
    backend = MagicMock()

    async def _stream(**kwargs):
        for event in events:
            yield event

    backend.stream = _stream
    return backend


def _well_formed_job(**overrides) -> dict:
    job = {
        "title": "Job A",
        "idea": "Do A. done when: A works.",
        "depends_on": [],
        "priority": 0,
        "scope": {"allowed_paths": ["a.py"], "interfaces": "exposes foo()"},
    }
    job.update(overrides)
    return job


def test_build_architect_prompt_includes_goal_and_epic_context():
    prompt = _build_architect_prompt(
        "Demo Project",
        "/repo",
        "Billing",
        "Handles invoices",
        "Add usage-based pricing",
        "func foo.bar",
        "decision: use stripe",
    )
    assert "Project: Demo Project" in prompt
    assert "Repository: /repo" in prompt
    assert "Epic name: Billing" in prompt
    assert "Epic description: Handles invoices" in prompt
    assert "Goal: Add usage-based pricing" in prompt
    assert "func foo.bar" in prompt
    assert "decision: use stripe" in prompt


def test_build_architect_prompt_omits_empty_sections():
    prompt = _build_architect_prompt(
        "Demo Project", "/repo", "Billing", "", "Add usage-based pricing", "", ""
    )
    assert "Epic description: (none provided)" in prompt
    assert "Symbol index" not in prompt
    assert "Recent decisions" not in prompt


def test_architect_yields_well_formed_jobs_dag_with_scope_and_priority():
    result_data = {
        "summary": "Two-step plan",
        "rationale": "B depends on A because it consumes A's schema.",
        "jobs": [
            _well_formed_job(title="Job A", priority=1),
            _well_formed_job(
                title="Job B",
                depends_on=[0],
                scope={"allowed_paths": ["b.py"], "interfaces": "consumes foo()"},
            ),
        ],
    }
    backend = _make_streaming_backend(
        [
            {"type": "text", "delta": "thinking..."},
            {"type": "result", "text": _wrap(result_data)},
        ]
    )

    events = asyncio.run(_collect(architect(backend, "prompt", "/repo")))

    assert events[0] == {"type": "text", "delta": "thinking..."}
    result_event = events[-1]
    assert result_event["type"] == "result"
    assert result_event["summary"] == "Two-step plan"
    assert result_event["rationale"] == result_data["rationale"]
    assert result_event["jobs"] == result_data["jobs"]
    assert result_event["jobs"][0]["priority"] == 1
    assert result_event["jobs"][1]["scope"]["allowed_paths"] == ["b.py"]
    assert result_event["jobs"][1]["scope"]["interfaces"] == "consumes foo()"


def test_architect_degrades_to_empty_jobs_on_malformed_output():
    backend = _make_streaming_backend(
        [{"type": "result", "text": "not a valid sentinel block at all"}]
    )

    events = asyncio.run(_collect(architect(backend, "prompt", "/repo")))

    assert len(events) == 1
    assert events[0] == {
        "type": "result",
        "summary": "",
        "jobs": [],
        "raw": "not a valid sentinel block at all",
    }


def test_architect_degrades_to_empty_jobs_on_schema_invalid_output():
    # scope.allowed_paths must be an array of strings; here it's a bare string,
    # so this fails schema validation even though the sentinel block parses fine.
    bad_data = {
        "summary": "oops",
        "rationale": "oops",
        "jobs": [_well_formed_job(scope={"allowed_paths": "a.py", "interfaces": "x"})],
    }
    backend = _make_streaming_backend([{"type": "result", "text": _wrap(bad_data)}])

    events = asyncio.run(_collect(architect(backend, "prompt", "/repo")))

    assert len(events) == 1
    assert events[0]["jobs"] == []
    assert events[0]["summary"] == ""
    assert "raw" in events[0]


def test_architect_never_touches_a_store():
    """The ARCHITECT stream is proposal-only — it must never file jobs itself."""
    store = MagicMock()
    backend = _make_streaming_backend(
        [{"type": "result", "text": _wrap({"summary": "s", "rationale": "r", "jobs": []})}]
    )

    asyncio.run(_collect(architect(backend, "prompt", "/repo")))

    store.create_batch.assert_not_called()


def test_architect_sys_prompt_forbids_filing_jobs():
    assert "CANNOT create, queue, or file jobs" in ARCHITECT_SYS
    assert "depends_on" in ARCHITECT_SYS
    assert "priority" in ARCHITECT_SYS
    assert "scope" in ARCHITECT_SYS
    assert "rationale" in ARCHITECT_SYS


async def _collect(agen):
    return [event async for event in agen]
