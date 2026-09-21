"""Tests for the JobCancelled primitive and run_cancellable() watcher."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hyqs.pipeline import agents
from hyqs.pipeline.models import JobStatus, Usage
from hyqs.pipeline.providers import AgentRun


class _StubJob:
    def __init__(self, status: JobStatus) -> None:
        self.status = status


class _StubStore:
    """A store stub whose .get(job_id) reports a fixed status.

    ``flip_after`` calls, if set, switches the reported status to CANCELLED
    after that many .get() calls — used to simulate a cancellation landing
    partway through a run.
    """

    def __init__(self, status: JobStatus = JobStatus.RUNNING, flip_after: int | None = None) -> None:
        self.status = status
        self.flip_after = flip_after
        self._calls = 0

    def get(self, job_id: int) -> _StubJob:
        self._calls += 1
        if self.flip_after is not None and self._calls >= self.flip_after:
            self.status = JobStatus.CANCELLED
        return _StubJob(self.status)


def _make_slow_backend(delay: float = 10.0) -> MagicMock:
    """An AgentBackend whose run() never returns before ``delay`` seconds."""

    async def _slow_run(**kwargs):
        await asyncio.sleep(delay)
        return AgentRun(text="unreachable", usage=Usage())

    backend = MagicMock()
    backend.run = AsyncMock(side_effect=_slow_run)
    return backend


def test_run_cancellable_returns_result_when_never_cancelled():
    store = _StubStore(JobStatus.RUNNING)

    async def _work():
        await asyncio.sleep(0.05)
        return "done"

    result = asyncio.run(
        agents.run_cancellable(_work(), store=store, job_id=1, poll_interval=0.02)
    )
    assert result == "done"


def test_run_cancellable_raises_job_cancelled_and_cancels_task():
    store = _StubStore(JobStatus.RUNNING)
    task_cancelled = False

    async def _work():
        nonlocal task_cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            task_cancelled = True
            raise
        return "should not reach here"

    async def _flip_status_soon():
        await asyncio.sleep(0.03)
        store.status = JobStatus.CANCELLED

    async def _run():
        flipper = asyncio.ensure_future(_flip_status_soon())
        try:
            await agents.run_cancellable(
                _work(), store=store, job_id=42, poll_interval=0.02
            )
        finally:
            await flipper

    with pytest.raises(agents.JobCancelled) as exc_info:
        asyncio.run(_run())

    assert exc_info.value.job_id == 42
    assert task_cancelled is True


def _make_fast_backend(text: str) -> MagicMock:
    """An AgentBackend whose run() resolves immediately with ``text``."""
    result = AgentRun(text=text, usage=Usage())
    backend = MagicMock()
    backend.run = AsyncMock(return_value=result)
    return backend


def _patch_fast_poll(monkeypatch) -> None:
    """Make agents.run_cancellable poll much faster, for quick tests.

    _invoke() hard-codes poll_interval=7.0 in production (job runs are long,
    so 7s cancellation granularity is fine there). Tests need to observe
    cancellation quickly, so this wraps the real run_cancellable with a much
    shorter poll_interval while exercising the real wiring end-to-end.
    """
    real_run_cancellable = agents.run_cancellable

    async def _fast_run_cancellable(coro, *, store, job_id, poll_interval=7.0):
        return await real_run_cancellable(coro, store=store, job_id=job_id, poll_interval=0.02)

    monkeypatch.setattr(agents, "run_cancellable", _fast_run_cancellable)


def test_build_with_store_job_id_raises_job_cancelled_before_timeout(monkeypatch):
    _patch_fast_poll(monkeypatch)
    backend = _make_slow_backend(delay=5.0)
    store = _StubStore(JobStatus.RUNNING, flip_after=2)

    async def _run():
        return await asyncio.wait_for(
            agents.build(
                backend,
                "idea",
                {"stories": []},
                "/tmp/worktree",
                timeout=None,
                store=store,
                job_id=7,
            ),
            timeout=1.0,
        )

    with pytest.raises(agents.JobCancelled) as exc_info:
        asyncio.run(_run())
    assert exc_info.value.job_id == 7


def test_plan_with_store_job_id_raises_job_cancelled_before_timeout(monkeypatch, tmp_path):
    _patch_fast_poll(monkeypatch)
    backend = _make_slow_backend(delay=5.0)
    store = _StubStore(JobStatus.RUNNING, flip_after=2)

    async def _run():
        return await asyncio.wait_for(
            agents.plan(
                backend,
                "idea",
                str(tmp_path),
                timeout=None,
                store=store,
                job_id=99,
            ),
            timeout=1.0,
        )

    with pytest.raises(agents.JobCancelled) as exc_info:
        asyncio.run(_run())
    assert exc_info.value.job_id == 99


def test_build_without_store_job_id_behaves_as_before():
    backend = _make_fast_backend("did the thing")

    text, usage = asyncio.run(
        agents.build(backend, "idea", {"stories": []}, "/tmp/worktree")
    )

    assert text == "did the thing"
    assert isinstance(usage, Usage)


def test_build_with_normal_non_cancelled_run_still_completes():
    backend = _make_fast_backend("did the thing")
    store = _StubStore(JobStatus.RUNNING)

    text, usage = asyncio.run(
        agents.build(
            backend,
            "idea",
            {"stories": []},
            "/tmp/worktree",
            store=store,
            job_id=7,
        )
    )

    assert text == "did the thing"
    assert isinstance(usage, Usage)
