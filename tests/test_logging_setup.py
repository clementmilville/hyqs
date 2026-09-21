from __future__ import annotations

import asyncio
import io
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import structlog

from hyqs.config import Config
from hyqs.pipeline.logging_setup import configure_logging
from hyqs.pipeline.models import Job, JobStatus, Stage
from hyqs.pipeline.runner import PipelineRunner


def _capture_root_output() -> io.StringIO:
    """Redirect the root handler's stream so tests can inspect emitted JSON lines."""
    buf = io.StringIO()
    logging.getLogger().handlers[0].stream = buf
    return buf


def test_from_env_defaults_log_format_to_json(monkeypatch):
    monkeypatch.delenv("HYQS_LOG_FORMAT", raising=False)
    assert Config.from_env().log_format == "json"


def test_from_env_honors_log_format_console(monkeypatch):
    monkeypatch.setenv("HYQS_LOG_FORMAT", "console")
    assert Config.from_env().log_format == "console"


def test_configure_logging_json_renders_stdlib_and_structlog_calls_as_json():
    configure_logging("json")
    buf = _capture_root_output()

    logging.getLogger("hyqs.runner").info("plain stdlib log")
    structlog.get_logger("hyqs.struct").info("structlog log", foo="bar")

    lines = [json.loads(line) for line in buf.getvalue().strip().splitlines()]
    assert len(lines) == 2
    for record in lines:
        assert "level" in record
        assert "timestamp" in record
    assert lines[0]["event"] == "plain stdlib log"
    assert lines[1]["event"] == "structlog log"
    assert lines[1]["foo"] == "bar"


def test_configure_logging_merges_bound_contextvars_into_stdlib_calls():
    configure_logging("json")
    buf = _capture_root_output()

    with structlog.contextvars.bound_contextvars(job_id=42, stage="build", project_id=7):
        logging.getLogger("hyqs.runner").info("with context")
    logging.getLogger("hyqs.runner").info("after context")

    lines = [json.loads(line) for line in buf.getvalue().strip().splitlines()]
    with_ctx, after_ctx = lines
    assert with_ctx["job_id"] == 42
    assert with_ctx["stage"] == "build"
    assert with_ctx["project_id"] == 7
    assert "job_id" not in after_ctx
    assert "stage" not in after_ctx
    assert "project_id" not in after_ctx


def _job(**kw) -> Job:
    defaults = dict(
        id=7,
        idea="test idea",
        repo_path="/fake/repo",
        chat_id=1,
        stage=Stage.TEST,
        status=JobStatus.RUNNING,
        project_id=3,
    )
    defaults.update(kw)
    return Job(**defaults)


def _make_runner(tmp_path) -> PipelineRunner:
    store = MagicMock()
    store.renew_lease = AsyncMock()
    store.worker_heartbeat = AsyncMock()
    config = SimpleNamespace(model="test-model", data_dir=str(tmp_path / "data"))
    return PipelineRunner(store, config, notify=AsyncMock())


def test_run_stage_with_retry_binds_job_context_for_nested_log_calls(tmp_path):
    """A log line emitted from an unrelated logger during a bound stage run must
    carry job_id/stage/project_id; contextvars must be gone once the block exits."""
    configure_logging("json")
    buf = _capture_root_output()
    rn = _make_runner(tmp_path)
    job = _job()

    async def _fake_advance(_job):
        logging.getLogger("hyqs.some.nested.module").info("nested log during stage")

    async def _run():
        with patch.object(rn, "_advance", new_callable=AsyncMock, side_effect=_fake_advance):
            with structlog.contextvars.bound_contextvars(
                job_id=job.id, stage=job.stage.value, project_id=job.project_id
            ):
                await rn._run_stage_with_retry("w1", job)
        logging.getLogger("hyqs.some.nested.module").info("log after stage")

    asyncio.run(_run())

    lines = [json.loads(line) for line in buf.getvalue().strip().splitlines()]
    during, after = lines
    assert during["job_id"] == job.id
    assert during["stage"] == "test"
    assert during["project_id"] == 3
    assert "job_id" not in after
