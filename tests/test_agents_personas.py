"""Tests for domain specialist persona checklists spliced into gate prompts."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from hyqs.pipeline import SECURITY_SYS, CurrentExecutor, agents, personas, resolve_current_executor
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


def _job(**overrides) -> Job:
    values = {
        "id": 1,
        "idea": "idea",
        "repo_path": "/tmp/repo",
        "chat_id": 1,
    }
    values.update(overrides)
    return Job(**values)


def test_resolve_current_executor_covers_job_state_precedence():
    cases = [
        (
            "queued",
            _job(stage=Stage.QUEUED, status=JobStatus.PENDING),
            CurrentExecutor(kind="waiting", label="Waiting assignment"),
        ),
        (
            "ai running",
            _job(
                stage=Stage.PLAN,
                status=JobStatus.RUNNING,
                agent_id=42,
                provider="codex",
            ),
            CurrentExecutor(kind="agent", label="codex", agent_id=42, provider="codex"),
        ),
        (
            "deterministic running",
            _job(stage=Stage.LINT, status=JobStatus.RUNNING, owner="worker-1"),
            CurrentExecutor(kind="pipeline", label="Pipeline"),
        ),
        (
            "deploying",
            _job(
                stage=Stage.DEPLOY,
                status=JobStatus.DEPLOYING,
                agent_id=42,
                provider="codex",
            ),
            CurrentExecutor(kind="pipeline", label="Pipeline"),
        ),
        (
            "blocked pending",
            _job(
                stage=Stage.BUILD,
                status=JobStatus.PENDING,
                agent_id=42,
                provider="codex",
                owner="stale-worker",
            ),
            CurrentExecutor(kind="waiting", label="Waiting assignment"),
        ),
        (
            "running AI stage without assignment",
            _job(stage=Stage.TEST, status=JobStatus.RUNNING),
            CurrentExecutor(kind="waiting", label="Waiting assignment"),
        ),
    ]

    for name, job, expected in cases:
        assert resolve_current_executor(job) == expected, name


def test_resolve_current_executor_clears_stale_terminal_ownership():
    for status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
        job = _job(
            stage=Stage.BUILD,
            status=status,
            agent_id=42,
            provider="codex",
            owner="stale-worker",
        )

        assert resolve_current_executor(job) is None
        assert job.provider == "codex"


def test_current_executor_api_is_exported_from_package():
    assert CurrentExecutor is not None
    assert resolve_current_executor is agents.resolve_current_executor


def test_build_checklist_deterministic_order():
    checklist = personas.build_checklist({"infra", "db", "authz", "frontend"})
    db_idx = checklist.index("DB Specialist")
    frontend_idx = checklist.index("Frontend Specialist")
    authz_idx = checklist.index("Authz Specialist")
    infra_idx = checklist.index("Infra Specialist")
    assert db_idx < frontend_idx < authz_idx < infra_idx


def test_build_checklist_only_includes_matched_domains():
    checklist = personas.build_checklist({"db"})
    assert "DB Specialist" in checklist
    assert "Frontend Specialist" not in checklist
    assert "Authz Specialist" not in checklist
    assert "Infra Specialist" not in checklist


def test_build_checklist_empty_for_empty_set():
    assert personas.build_checklist(set()) == ""


def test_review_includes_persona_checklist_when_provided(tmp_path):
    backend = _make_backend(_wrap(_VERDICT))
    checklist = personas.build_checklist({"db"})

    asyncio.run(agents.review(backend, str(tmp_path), "main", persona_checklist=checklist))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Domain Specialist Checklist" in prompt
    assert "DB Specialist" in prompt


def test_review_omits_persona_checklist_block_when_default(tmp_path):
    backend = _make_backend(_wrap(_VERDICT))

    asyncio.run(agents.review(backend, str(tmp_path), "main"))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Domain Specialist Checklist" not in prompt


def test_security_includes_persona_checklist_when_provided(tmp_path):
    backend = _make_backend(_wrap(_VERDICT))
    checklist = personas.build_checklist({"authz"})

    asyncio.run(agents.security(backend, str(tmp_path), "main", persona_checklist=checklist))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Domain Specialist Checklist" in prompt
    assert "Authz Specialist" in prompt


def test_security_omits_persona_checklist_block_when_default(tmp_path):
    backend = _make_backend(_wrap(_VERDICT))

    asyncio.run(agents.security(backend, str(tmp_path), "main"))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Domain Specialist Checklist" not in prompt


def test_security_prompt_is_public_and_documents_optional_authoritative_path():
    assert SECURITY_SYS is agents.SECURITY_SYS
    assert '"path": str' in SECURITY_SYS
    assert "authoritative" in SECURITY_SYS
    assert "canonical concrete" in SECURITY_SYS
    assert "Omit `path`" in SECURITY_SYS


def test_security_preserves_canonical_finding_path(tmp_path):
    verdict = {
        "verdict": "fail",
        "summary": "unsafe input",
        "findings": [
            {
                "severity": "high",
                "note": "Untrusted command input.",
                "path": "hyqs/web/app.py",
            }
        ],
    }
    backend = _make_backend(_wrap(verdict))

    result, _usage = asyncio.run(agents.security(backend, str(tmp_path), "main"))

    assert result == verdict


def test_design_review_includes_persona_checklist_when_provided(tmp_path):
    backend = _make_backend(_wrap(_VERDICT))
    checklist = personas.build_checklist({"frontend"})

    asyncio.run(agents.design_review(backend, str(tmp_path), "main", persona_checklist=checklist))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Domain Specialist Checklist" in prompt
    assert "Frontend Specialist" in prompt


def test_design_review_omits_persona_checklist_block_when_default(tmp_path):
    backend = _make_backend(_wrap(_VERDICT))

    asyncio.run(agents.design_review(backend, str(tmp_path), "main"))

    prompt = backend.run.call_args.kwargs["prompt"]
    assert "## Domain Specialist Checklist" not in prompt
