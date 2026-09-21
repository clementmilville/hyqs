"""Focused failure-classification tests for provider adapters."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from hyqs.pipeline import agents
from hyqs.pipeline.limits import ProviderUnavailable
from hyqs.pipeline.providers import ClaudeBackend, Role, _claude_capacity_limit


class RateLimitEvent:
    def __init__(self, status: str, resets_at: int | None = None) -> None:
        self.rate_limit_info = SimpleNamespace(
            status=status,
            resets_at=resets_at,
            rate_limit_type="five_hour",
            utilization=1.0,
            overage_status=None,
        )


class AssistantMessage:
    def __init__(self, error=None, content=None) -> None:
        self.error = error
        self.content = content or []


class ResultMessage:
    def __init__(self, *, status=None, result="failed", error=None) -> None:
        self.is_error = True
        self.api_error_status = status
        self.result = result
        self.error = error
        self.resets_at = None
        self.subtype = "error"
        self.stop_reason = None
        self.num_turns = 1
        self.errors = []
        self.usage = {}
        self.total_cost_usd = 0


class TextBlock:
    pass


class ToolUseBlock:
    pass


class ClaudeAgentOptions:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


def _install_sdk(monkeypatch, messages: list[object]) -> None:
    module = ModuleType("claude_agent_sdk")

    async def query(**_kwargs):
        for message in messages:
            yield message

    module.AssistantMessage = AssistantMessage
    module.ClaudeAgentOptions = ClaudeAgentOptions
    module.RateLimitEvent = RateLimitEvent
    module.ResultMessage = ResultMessage
    module.TextBlock = TextBlock
    module.ToolUseBlock = ToolUseBlock
    module.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)


def test_claude_rejected_rate_limit_is_capacity_and_preserves_reset():
    event = RateLimitEvent("rejected", 1999999999)

    assert _claude_capacity_limit(event) == (
        1999999999,
        "Anthropic rate/usage limit reached",
    )


@pytest.mark.parametrize(
    "message",
    [
        AssistantMessage("billing_error"),
        AssistantMessage("authentication_error"),
        ResultMessage(status=400, error="invalid_request_error"),
        ResultMessage(status=503, error="service_unavailable"),
        ResultMessage(error="tool_error"),
    ],
)
def test_claude_non_capacity_signatures_are_not_limits(message):
    assert _claude_capacity_limit(message) is None


def test_claude_run_raises_capacity_exception_with_reset(monkeypatch):
    _install_sdk(monkeypatch, [RateLimitEvent("rejected", 1999999999)])

    async def _run():
        with pytest.raises(ProviderUnavailable) as exc_info:
            await ClaudeBackend("sonnet").run(
                prompt="do it", cwd="/tmp", role=Role.CODER, append_system=""
            )
        return exc_info.value

    error = asyncio.run(_run())
    assert error.provider == "claude"
    assert error.resets_at == 1999999999


def test_claude_stream_generic_failure_is_not_provider_unavailable(monkeypatch):
    _install_sdk(monkeypatch, [ResultMessage(status=401, result="authentication failed")])

    async def _run():
        with pytest.raises(RuntimeError) as exc_info:
            await agents._collect_stream(
                ClaudeBackend("sonnet"),
                prompt="do it",
                cwd="/tmp",
                role=Role.CODER,
                append_system="",
                log_sink=lambda _line: None,
            )
        return exc_info.value

    assert not isinstance(asyncio.run(_run()), ProviderUnavailable)


def test_collect_stream_generic_error_event_is_runtime_failure():
    class Backend:
        name = "test"

        async def stream(self, **_kwargs):
            yield {"type": "error", "message": "tool failed"}

    async def _run():
        with pytest.raises(RuntimeError) as exc_info:
            await agents._collect_stream(
                Backend(),
                prompt="do it",
                cwd="/tmp",
                role=Role.CODER,
                append_system="",
                log_sink=lambda _line: None,
            )
        return exc_info.value

    assert not isinstance(asyncio.run(_run()), ProviderUnavailable)
