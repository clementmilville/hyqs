"""Regression tests for the prompt/efficiency pass (items 3–5 of the 2026-07-12 review).

3. Prompt assembly is stable-first / instruction-last (cache-prefix reuse + recency).
4. _collect_stream keeps cache-token fields; the planner re-runs once on malformed
   output instead of failing the job.
5. Deterministic fast-path: claim_fastpath re-claims a specific PENDING job only
   when its next step needs no AI agent; npm ci uses the shared offline cache.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hyqs.pipeline import agents
from hyqs.pipeline.models import JobStatus, Usage


def _agent_run(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, usage=Usage(input_tokens=1, output_tokens=1))


_VALID_PLAN = (
    "<<<RESULT_JSON>>>\n"
    '{"summary": "s", "stories": [{"id": "S1", "title": "Repair PLAN fixture",'
    ' "task": "Update the shared valid PLAN fixture for the strict schema.",'
    ' "acceptance": "The prompt-speed PLAN tests parse the fixture successfully.",'
    ' "target_files": ["tests/test_prompt_speed_pass.py"], "depends_on": []}],'
    ' "reuses": [], "adds": [],'
    ' "file_impact": [{"path": "tests/test_prompt_speed_pass.py", "role": "test",'
    ' "justification": "Updates the shared PLAN regression fixture.", "status": "existing",'
    ' "provenance": "idea"}],'
    ' "ui_impact": {"touches_backend_surface": false, "frontend_changes": [],'
    ' "no_ui_change_reason": "Internal planner regression fixture."},'
    ' "activation": {"config_change_required": false,'
    ' "activation_location": "n/a",'
    ' "expected_live_effect": "No live effect; internal test fixture only."},'
    ' "target_files": ["tests/test_prompt_speed_pass.py"]}\n'
    "<<<END_RESULT>>>"
)


# ---------------------------------------------------------------------------
# 3 — prompt ordering: stable context first, task/instruction last
# ---------------------------------------------------------------------------


def test_build_prompt_orders_stable_context_before_task(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(return_value=_agent_run("done"))
    plan = {"summary": "sum", "stories": [], "target_files": ["a.py"]}

    asyncio.run(
        agents.build(
            backend,
            "the idea",
            plan,
            str(tmp_path),
            file_manifest="a.py\nb.py",
            symbol_context="## Codebase Symbol Index\nfoo.bar",
        )
    )

    prompt = backend.run.call_args.kwargs["prompt"]
    assert prompt.index("## Repository files") < prompt.index("## Codebase Symbol Index")
    assert prompt.index("## Codebase Symbol Index") < prompt.index("Original idea:")
    assert prompt.rstrip().endswith("Implement all of it now.")


def test_build_prompt_includes_verified_plan_contract(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(return_value=_agent_run("done"))
    plan = {
        "summary": "sum",
        "stories": [],
        "target_files": ["api.py", "ui.js"],
        "reuses": ["app.auth.verify_lease"],
        "adds": ["app.api.rotate_session"],
        "ui_impact": {
            "touches_backend_surface": True,
            "frontend_changes": ["Update the session client"],
            "no_ui_change_reason": "",
        },
    }

    asyncio.run(agents.build(backend, "the idea", plan, str(tmp_path)))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Verified implementation contract" in prompt
    assert "app.auth.verify_lease" in prompt
    assert "app.api.rotate_session" in prompt
    assert "Update the session client" in prompt
    assert prompt.index("## Verified implementation contract") < prompt.index(
        "## Complete planner implementation and regression scope"
    )


def test_plan_prompt_puts_idea_after_context_and_instruction_last(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(return_value=_agent_run(_VALID_PLAN))

    asyncio.run(agents.plan(backend, "the idea", str(tmp_path), file_manifest="a.py"))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert prompt.index("## Repository files") < prompt.index("Idea to implement:")
    assert prompt.rstrip().endswith("Produce the plan.")


def test_review_prompt_puts_diff_instruction_last(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(
        return_value=_agent_run(
            '<<<RESULT_JSON>>>\n{"verdict": "pass", "summary": "ok", "findings": []}\n<<<END_RESULT>>>'
        )
    )

    asyncio.run(agents.review(backend, str(tmp_path), "main", symbol_context="sym-index"))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert prompt.index("## Codebase Symbol Index") < prompt.index(
        "Review the diff of the current branch"
    )
    assert "git diff main...HEAD" in prompt.splitlines()[-1] or prompt.rstrip().endswith(
        "to see the changes."
    )


def test_review_prompt_includes_full_implementation_contract(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(
        return_value=_agent_run(
            '<<<RESULT_JSON>>>\n{"verdict": "pass", "summary": "ok", "findings": []}\n<<<END_RESULT>>>'
        )
    )
    plan = {
        "summary": "Rotate sessions",
        "stories": [
            {
                "id": "S1",
                "title": "Rotation",
                "task": "Rotate the session.",
                "acceptance": "The old session cannot be reused.",
            }
        ],
        "reuses": ["app.sessions.rotate"],
        "adds": ["app.sessions.RotationResult"],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "Internal behavior.",
        },
        "target_files": ["app/sessions.py", "tests/test_sessions.py"],
    }

    asyncio.run(
        agents.review(
            backend,
            str(tmp_path),
            "main",
            idea="Make rotation atomic",
            plan_data=plan,
        )
    )

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Original idea\nMake rotation atomic" in prompt
    assert "## Stories and acceptance criteria" in prompt
    assert "The old session cannot be reused" in prompt
    assert "app.sessions.rotate" in prompt
    assert "app.sessions.RotationResult" in prompt
    assert "## Planned target files" in prompt


def test_security_prompt_includes_trust_boundary_context(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(
        return_value=_agent_run(
            '<<<RESULT_JSON>>>\n{"verdict": "pass", "summary": "ok", "findings": []}\n<<<END_RESULT>>>'
        )
    )
    plan = {
        "summary": "Bind actor and subject",
        "stories": [
            {
                "id": "S1",
                "title": "Delegated lease",
                "task": "Keep actor and subject distinct.",
                "acceptance": "Authorization uses subject while audit retains actor.",
            }
        ],
        "reuses": ["app.auth.verify_lease"],
        "adds": ["app.auth.DelegatedPrincipal"],
        "ui_impact": {
            "touches_backend_surface": False,
            "frontend_changes": [],
            "no_ui_change_reason": "Internal authorization context.",
        },
        "target_files": ["app/auth.py"],
    }

    asyncio.run(
        agents.security(
            backend,
            str(tmp_path),
            "main",
            idea="Support delegated authorization",
            plan_data=plan,
            decision_digest="Actor and subject are never interchangeable.",
        )
    )

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Original idea\nSupport delegated authorization" in prompt
    assert "## Security-relevant stories and acceptance criteria" in prompt
    assert "Authorization uses subject while audit retains actor" in prompt
    assert "app.auth.verify_lease" in prompt
    assert "app.auth.DelegatedPrincipal" in prompt
    assert "Actor and subject are never interchangeable" in prompt


def test_plan_prompt_includes_decision_digest_when_present(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(return_value=_agent_run(_VALID_PLAN))

    asyncio.run(
        agents.plan(
            backend,
            "the idea",
            str(tmp_path),
            decision_digest="- 2026-07-01 — Add foo: shipped foo",
        )
    )
    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Recent Architecture Decisions" in prompt
    assert "shipped foo" in prompt
    assert prompt.index("## Recent Architecture Decisions") < prompt.index("Idea to implement:")


def test_plan_prompt_omits_decision_digest_when_absent(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(return_value=_agent_run(_VALID_PLAN))

    asyncio.run(agents.plan(backend, "the idea", str(tmp_path)))
    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Recent Architecture Decisions" not in prompt


def test_plan_prompt_includes_reask_context_when_present(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(return_value=_agent_run(_VALID_PLAN))

    asyncio.run(
        agents.plan(
            backend,
            "the idea",
            str(tmp_path),
            reask_context="Story S1 exceeds the per-story target_files limit.",
        )
    )
    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Previous Plan Was Rejected" in prompt
    assert "Story S1 exceeds the per-story target_files limit." in prompt
    assert prompt.index("## Previous Plan Was Rejected") < prompt.index("Idea to implement:")


def test_plan_prompt_omits_reask_context_when_absent(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(return_value=_agent_run(_VALID_PLAN))

    asyncio.run(agents.plan(backend, "the idea", str(tmp_path)))
    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Previous Plan Was Rejected" not in prompt


def test_review_prompt_includes_decision_digest_when_present(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(
        return_value=_agent_run(
            '<<<RESULT_JSON>>>\n{"verdict": "pass", "summary": "ok", "findings": []}\n<<<END_RESULT>>>'
        )
    )

    asyncio.run(
        agents.review(
            backend,
            str(tmp_path),
            "main",
            decision_digest="- 2026-07-01 — Add bar: shipped bar",
        )
    )
    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Recent Architecture Decisions" in prompt
    assert "shipped bar" in prompt


def test_review_prompt_omits_decision_digest_when_absent(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(
        return_value=_agent_run(
            '<<<RESULT_JSON>>>\n{"verdict": "pass", "summary": "ok", "findings": []}\n<<<END_RESULT>>>'
        )
    )

    asyncio.run(agents.review(backend, str(tmp_path), "main"))
    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Recent Architecture Decisions" not in prompt


# ---------------------------------------------------------------------------
# 4 — cache tokens kept; plan re-runs once on malformed output
# ---------------------------------------------------------------------------


def test_collect_stream_keeps_cache_token_fields():
    async def _stream(**_kw):
        yield {
            "type": "result",
            "text": "hello",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_tokens": 100,
                "cache_read_tokens": 2000,
                "cost_usd": 0.5,
            },
        }

    backend = MagicMock()
    backend.stream = _stream

    text, usage = asyncio.run(
        agents._collect_stream(
            backend,
            prompt="p",
            cwd=".",
            role=agents.Role.REVIEWER,
            append_system="",
            log_sink=lambda _l: None,
        )
    )
    assert text == "hello"
    assert usage.cache_creation_tokens == 100
    assert usage.cache_read_tokens == 2000
    assert usage.total_tokens == 2115


def test_plan_reruns_once_on_malformed_output(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(side_effect=[_agent_run("garbage"), _agent_run(_VALID_PLAN)])

    data, usage = asyncio.run(agents.plan(backend, "idea", str(tmp_path)))

    assert backend.run.call_count == 2
    assert data["summary"] == "s"
    assert usage.input_tokens == 2  # both runs accounted


def test_plan_raises_after_two_malformed_outputs(tmp_path):
    backend = MagicMock()
    backend.run = AsyncMock(side_effect=[_agent_run("garbage"), _agent_run("more garbage")])

    with pytest.raises(ValueError):
        asyncio.run(agents.plan(backend, "idea", str(tmp_path)))
    assert backend.run.call_count == 2


# ---------------------------------------------------------------------------
# 5 — deterministic fast-path claim (real DB; rows cleaned up)
# ---------------------------------------------------------------------------


def _insert_job(store, stage: str, status: str = "pending") -> int:
    from hyqs.pipeline.models import _now

    now = _now()
    with store._pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO jobs(idea, title, repo_path, chat_id, stage, status, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                "fastpath test",
                "fastpath test",
                "/tmp/fastpath-test-repo",
                0,
                stage,
                status,
                now,
                now,
            ),
        ).fetchone()
        return int(row["id"])


def _delete_job(store, job_id: int) -> None:
    with store._pool.connection() as conn:
        conn.execute("DELETE FROM jobs WHERE id=%s", (job_id,))


def test_claim_fastpath_claims_deterministic_stage(store):
    jid = _insert_job(store, stage="lint")  # next step: lint (deterministic)
    try:
        job = asyncio.run(store.claim_fastpath(jid, "w-test", time.time(), 90))
        assert job is not None
        assert job.status == JobStatus.RUNNING
        assert job.owner == "w-test"
        # Already RUNNING now: a second fast-path claim must lose.
        assert asyncio.run(store.claim_fastpath(jid, "w-test", time.time(), 90)) is None
    finally:
        _delete_job(store, jid)


def test_claim_fastpath_refuses_agentic_stage(store):
    jid = _insert_job(store, stage="test")  # next step: review (agentic)
    try:
        assert asyncio.run(store.claim_fastpath(jid, "w-test", time.time(), 90)) is None
    finally:
        _delete_job(store, jid)


def test_claim_fastpath_refuses_cancelled_job(store):
    jid = _insert_job(store, stage="lint", status="cancelled")
    try:
        assert asyncio.run(store.claim_fastpath(jid, "w-test", time.time(), 90)) is None
    finally:
        _delete_job(store, jid)


def test_claim_fastpath_honors_rebase_backoff(store):
    jid = _insert_job(store, stage="security")  # merge step is deterministic
    try:
        with store._pool.connection() as conn:
            conn.execute(
                "UPDATE jobs SET rebase_retry_after=%s WHERE id=%s",
                (time.time() + 300, jid),
            )
        assert asyncio.run(store.claim_fastpath(jid, "w-test", time.time(), 90)) is None
    finally:
        _delete_job(store, jid)
