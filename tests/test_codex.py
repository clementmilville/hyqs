"""Tests for the Codex backend's subprocess handling."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyqs.pipeline import agents
from hyqs.pipeline.codex import CodexBackend, _is_limit_error
from hyqs.pipeline.limits import ProviderUnavailable
from hyqs.pipeline.pricing import PRICING
from hyqs.pipeline.providers import Role, build_backend


async def _hang_communicate(*_args, **_kwargs):
    await asyncio.sleep(1000)


def test_run_kills_and_reaps_subprocess_on_task_cancellation():
    """Cancelling the awaiting task must kill+reap the codex exec child process
    before CancelledError propagates, mirroring the existing timeout path."""
    proc = MagicMock()
    proc.communicate = _hang_communicate
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    async def _run():
        with patch(
            "hyqs.pipeline.codex.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            backend = CodexBackend()
            task = asyncio.create_task(
                backend.run(prompt="do it", cwd="/tmp", role=Role.CODER, append_system="")
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(_run())

    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


def test_backend_without_model_delegates_to_codex_cli_default():
    backend = build_backend("codex")

    assert backend.model == ""
    assert backend._command("do it") == ["codex", "exec", "--full-auto", "do it"]


def test_unsupported_model_is_not_misclassified_as_rate_limit():
    prompt = "Recover gracefully after a rate limit or exhausted quota."
    error = (
        'ERROR: {"type":"error","status":400,"error":'
        '{"type":"invalid_request_error","message":"The model is not supported."}}'
    )

    assert not _is_limit_error(prompt, error)


@pytest.mark.parametrize(
    "error",
    [
        'ERROR: {"type":"error","status":429,"error":{"type":"rate_limit_exceeded"}}',
        "ERROR: too many requests",
        'ERROR: {"type":"error","status":503,"error":{"type":"overloaded"}}',
    ],
)
def test_explicit_provider_capacity_errors_are_rate_limits(error):
    assert _is_limit_error("", error)


@pytest.mark.parametrize(
    "error",
    [
        'ERROR: {"status":503,"error":{"type":"service_unavailable"}}',
        'ERROR: {"status":401,"error":{"type":"authentication_error"}}',
        'ERROR: {"status":400,"error":{"type":"invalid_request_error"}}',
        'ERROR: {"error":{"type":"tool_error","message":"rate limit in prompt"}}',
        "ERROR: billing account has insufficient_quota",
    ],
)
def test_non_capacity_errors_are_not_rate_limits(error):
    assert not _is_limit_error("", error)


def test_run_capacity_error_preserves_reset_timestamp():
    proc = MagicMock()
    proc.communicate = AsyncMock(
        return_value=(
            b"",
            b'ERROR: {"status":429,"error":{"type":"rate_limit_exceeded"},'
            b'"resets_at":1999999999}\n',
        )
    )
    proc.returncode = 1

    async def _run():
        with patch(
            "hyqs.pipeline.codex.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            with pytest.raises(ProviderUnavailable) as exc_info:
                await CodexBackend().run(
                    prompt="do it", cwd="/tmp", role=Role.CODER, append_system=""
                )
            return exc_info.value

    with patch("hyqs.pipeline.codex.time.time", return_value=1999999000):
        error = asyncio.run(_run())
    assert error.provider == "codex"
    assert error.resets_at == 1999999999


def test_run_capacity_error_clamps_untrusted_far_future_reset():
    proc = MagicMock()
    proc.communicate = AsyncMock(
        return_value=(
            b"",
            b'ERROR: {"status":429,"resets_at":4102444800}\n',
        )
    )
    proc.returncode = 1

    async def _run():
        with patch(
            "hyqs.pipeline.codex.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            with pytest.raises(ProviderUnavailable) as exc_info:
                await CodexBackend().run(
                    prompt="do it", cwd="/tmp", role=Role.CODER, append_system=""
                )
            return exc_info.value

    with patch("hyqs.pipeline.codex.time.time", return_value=2000000000):
        error = asyncio.run(_run())
    assert error.resets_at == 2000021600


def test_run_does_not_trust_reset_metadata_from_model_stdout():
    proc = MagicMock()
    proc.communicate = AsyncMock(
        return_value=(
            b'ERROR: {"status":429,"resets_at":4102444800}\n',
            b"",
        )
    )
    proc.returncode = 1

    async def _run():
        with patch(
            "hyqs.pipeline.codex.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            with pytest.raises(ProviderUnavailable) as exc_info:
                await CodexBackend().run(
                    prompt="do it", cwd="/tmp", role=Role.CODER, append_system=""
                )
            return exc_info.value

    assert asyncio.run(_run()).resets_at is None


def _jsonl(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _make_proc(
    chunks: list[bytes], *, returncode: int = 0, stderr_lines: list[bytes] | None = None
) -> MagicMock:
    proc = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.read = AsyncMock(side_effect=[*chunks, b""])
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(side_effect=[*(stderr_lines or []), b""])
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    proc.returncode = returncode
    return proc


SUCCESS_LINES = [
    _jsonl({"type": "thread.started", "thread_id": "t1"}),
    _jsonl({"type": "turn.started"}),
    _jsonl(
        {
            "type": "item.completed",
            "item": {"id": "1", "type": "agent_message", "text": "Hello world"},
        }
    ),
    # item.started for a tool must be ignored — only item.completed is translated.
    _jsonl(
        {
            "type": "item.started",
            "item": {"id": "2", "type": "command_execution", "command": "ls -la"},
        }
    ),
    _jsonl(
        {
            "type": "item.completed",
            "item": {
                "id": "2",
                "type": "command_execution",
                "command": "ls -la",
                "aggregated_output": "a\nb",
                "exit_code": 0,
                "status": "completed",
            },
        }
    ),
    _jsonl(
        {
            "type": "item.completed",
            "item": {
                "id": "3",
                "type": "file_change",
                "changes": [{"path": "foo.py", "kind": "add"}],
                "status": "completed",
            },
        }
    ),
    _jsonl(
        {
            "type": "item.completed",
            "item": {"id": "4", "type": "error", "message": "ignored item-level error"},
        }
    ),
    _jsonl(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 10,
                "cache_write_input_tokens": 5,
                "output_tokens": 50,
                "reasoning_output_tokens": 20,
            },
        }
    ),
]


def _run_stream(backend: CodexBackend, proc: MagicMock) -> list[dict]:
    async def _run():
        with patch(
            "hyqs.pipeline.codex.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            return [
                e
                async for e in backend.stream(
                    prompt="do it", cwd="/tmp", role=Role.CODER, append_system=""
                )
            ]

    return asyncio.run(_run())


def test_stream_translates_jsonl_events_to_shared_shape():
    proc = _make_proc(SUCCESS_LINES)

    events = _run_stream(CodexBackend(), proc)

    assert events == [
        {"type": "text", "delta": "Hello world"},
        {"type": "tool_use", "tool": "command_execution", "input_summary": "ls -la"},
        {"type": "tool_use", "tool": "file_change", "input_summary": "add foo.py"},
        {
            "type": "result",
            "text": "Hello world",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 70,
                "cache_read_tokens": 10,
                "cache_creation_tokens": 5,
                "cost_usd": 0.0,
                "model": "",
                "provider": "codex",
            },
        },
    ]


def test_stream_usage_flows_through_collect_stream():
    proc = _make_proc(SUCCESS_LINES)

    async def _run():
        with patch(
            "hyqs.pipeline.codex.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            backend = CodexBackend()
            return await agents._collect_stream(
                backend,
                prompt="do it",
                cwd="/tmp",
                role=Role.CODER,
                append_system="",
                log_sink=lambda _: None,
            )

    text, usage = asyncio.run(_run())

    assert text == "Hello world"
    assert usage.input_tokens == 100
    assert usage.output_tokens == 70
    assert usage.cache_read_tokens == 10
    assert usage.cache_creation_tokens == 5
    assert usage.provider == "codex"


def test_collect_stream_falls_back_to_configured_model_and_all_token_classes():
    backend = MagicMock(model="claude-sonnet-4-6")
    backend.name = "claude"

    async def stream(**_kwargs):
        yield {
            "type": "result",
            "text": "done",
            "usage": {
                "input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
                "cache_creation_tokens": 1_000_000,
                "cache_read_tokens": 1_000_000,
                "cost_usd": 0,
            },
        }

    backend.stream = stream
    _, usage = asyncio.run(
        agents._collect_stream(
            backend,
            prompt="do it",
            cwd="/tmp",
            role=Role.CODER,
            append_system="",
            log_sink=lambda _: None,
        )
    )

    assert usage.model == "claude-sonnet-4-6"
    assert usage.provider == "claude"
    assert usage.cost_usd == 22.05


def test_collect_stream_unknown_event_model_keeps_zero_cost_and_wins_over_backend():
    backend = MagicMock(model="claude-sonnet-4-6")
    backend.name = "claude"

    async def stream(**_kwargs):
        yield {
            "type": "result",
            "text": "done",
            "usage": {"input_tokens": 100, "cost_usd": 0, "model": "unknown-model"},
        }

    backend.stream = stream
    _, usage = asyncio.run(
        agents._collect_stream(
            backend,
            prompt="do it",
            cwd="/tmp",
            role=Role.CODER,
            append_system="",
            log_sink=lambda _: None,
        )
    )

    assert usage.model == "unknown-model"
    assert usage.cost_usd == 0.0


def test_collect_stream_preserves_nonzero_provider_cost():
    backend = MagicMock(model="claude-sonnet-4-6")
    backend.name = "claude"

    async def stream(**_kwargs):
        yield {
            "type": "result",
            "text": "done",
            "usage": {"input_tokens": 1_000_000, "cost_usd": 1.25},
        }

    backend.stream = stream
    _, usage = asyncio.run(
        agents._collect_stream(
            backend,
            prompt="do it",
            cwd="/tmp",
            role=Role.CODER,
            append_system="",
            log_sink=lambda _: None,
        )
    )

    assert usage.cost_usd == 1.25


def test_codex_result_event_clamps_fresh_input_when_cache_exceeds_total():
    """cached_input_tokens + cache_write_input_tokens reported >= input_tokens
    (a degenerate CLI payload) must clamp fresh input at zero, never negative."""
    event = CodexBackend(model="gpt-5.6-sol")._result_event(
        {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cached_input_tokens": 1_000_000,
            "cache_write_input_tokens": 1_000_000,
        },
        ["done"],
    )

    assert event["usage"]["cost_usd"] == 35.5
    assert event["usage"]["model"] == "gpt-5.6-sol"
    assert event["usage"]["provider"] == "codex"


def test_codex_result_event_bills_only_fresh_input_at_full_rate():
    """input_tokens is Codex's OpenAI-style total, already inclusive of the 10
    cached + 5 newly-cached tokens. Only the remaining 85 fresh tokens should
    price at the full input rate — no token may be billed at both the in rate
    and its cache tier."""
    event = CodexBackend(model="gpt-5.6-sol")._result_event(
        {
            "input_tokens": 100,
            "cached_input_tokens": 10,
            "cache_write_input_tokens": 5,
            "output_tokens": 50,
            "reasoning_output_tokens": 20,
        },
        ["done"],
    )

    p = PRICING["gpt-5.6-sol"]
    expected_cost = (85 * p["in"] + 70 * p["out"] + 5 * p["in"] + 10 * p["read"]) / 1_000_000
    assert event["usage"]["cost_usd"] == pytest.approx(expected_cost)
    # The raw (cache-inclusive) input_tokens figure is still reported as-is —
    # only the cost math treats the cache buckets as disjoint.
    assert event["usage"]["input_tokens"] == 100
    assert event["usage"]["cache_read_tokens"] == 10
    assert event["usage"]["cache_creation_tokens"] == 5


def test_stream_rate_limit_failure_surfaces_as_provider_unavailable():
    proc = _make_proc([], returncode=1, stderr_lines=[b"Error: 429 too many requests\n"])

    async def _run():
        with patch(
            "hyqs.pipeline.codex.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            backend = CodexBackend()
            with pytest.raises(ProviderUnavailable):
                await agents._collect_stream(
                    backend,
                    prompt="do it",
                    cwd="/tmp",
                    role=Role.CODER,
                    append_system="",
                    log_sink=lambda _: None,
                )

    asyncio.run(_run())


def test_stream_rate_limit_preserves_reset_timestamp():
    proc = _make_proc(
        [],
        returncode=1,
        stderr_lines=[
            b'Error: {"status":429,"error":{"type":"rate_limit_error"},"reset_at":"1999999999"}\n'
        ],
    )

    async def _run():
        with patch(
            "hyqs.pipeline.codex.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            with pytest.raises(ProviderUnavailable) as exc_info:
                async for _ in CodexBackend().stream(
                    prompt="do it", cwd="/tmp", role=Role.CODER, append_system=""
                ):
                    pass
            return exc_info.value

    with patch("hyqs.pipeline.codex.time.time", return_value=1999999000):
        assert asyncio.run(_run()).resets_at == 1999999999


def test_stream_non_limit_failure_raises_runtime_error():
    proc = _make_proc(
        [], returncode=1, stderr_lines=[b"invalid_request_error: unsupported model\n"]
    )

    async def _run():
        with patch(
            "hyqs.pipeline.codex.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            backend = CodexBackend()
            with pytest.raises(RuntimeError) as exc_info:
                async for _ in backend.stream(
                    prompt="do it", cwd="/tmp", role=Role.CODER, append_system=""
                ):
                    pass
            assert not isinstance(exc_info.value, ProviderUnavailable)

    asyncio.run(_run())


def test_stream_skips_malformed_json_line():
    lines = [
        b"not json at all\n",
        _jsonl(
            {"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": "hi"}}
        ),
        _jsonl({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}}),
    ]
    proc = _make_proc(lines)

    events = _run_stream(CodexBackend(), proc)

    assert events == [
        {"type": "text", "delta": "hi"},
        {
            "type": "result",
            "text": "hi",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "cost_usd": 0.0,
                "model": "",
                "provider": "codex",
            },
        },
    ]


def test_stream_accepts_jsonl_event_larger_than_default_reader_limit():
    text = "x" * (70 * 1024)
    proc = _make_proc(
        [
            _jsonl(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": text},
                }
            )
        ]
    )

    assert _run_stream(CodexBackend(), proc) == [{"type": "text", "delta": text}]


def test_stream_reassembles_large_event_split_across_many_chunks():
    text = "fragmented" * 10_000
    record = _jsonl(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": text},
        }
    )
    proc = _make_proc([record[index : index + 997] for index in range(0, len(record), 997)])

    assert _run_stream(CodexBackend(), proc) == [{"type": "text", "delta": text}]


def test_stream_reads_multiple_events_from_one_chunk():
    chunk = _jsonl(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "one"}}
    ) + _jsonl({"type": "item.completed", "item": {"type": "agent_message", "text": "two"}})

    assert _run_stream(CodexBackend(), _make_proc([chunk])) == [
        {"type": "text", "delta": "one"},
        {"type": "text", "delta": "two"},
    ]


def test_stream_processes_final_event_without_trailing_newline():
    record = json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}}
    ).encode()

    assert _run_stream(CodexBackend(), _make_proc([record])) == [{"type": "text", "delta": "final"}]


def test_stream_rejects_event_over_eight_mib_and_reaps_process():
    proc = _make_proc([b"x" * (8 * 1024 * 1024 + 1)])

    with pytest.raises(RuntimeError, match="JSONL event exceeded the 8 MiB limit"):
        _run_stream(CodexBackend(), proc)

    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


def test_stream_timeout_kills_and_reaps_process():
    proc = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.read = _hang_read
    proc.stderr = MagicMock()
    proc.stderr.read = _hang_read
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    async def _run():
        with patch(
            "hyqs.pipeline.codex.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            with pytest.raises(TimeoutError):
                async for _ in CodexBackend().stream(
                    prompt="do it",
                    cwd="/tmp",
                    role=Role.CODER,
                    append_system="",
                    timeout=0.01,
                ):
                    pass

    asyncio.run(_run())
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


def test_stream_drains_large_stderr_concurrently_with_large_stdout():
    text = "x" * (70 * 1024)
    stdout = _jsonl({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    proc = _make_proc(
        [stdout[:40_000], stdout[40_000:]],
        stderr_lines=[b"e" * 70_000, b"f" * 70_000],
    )

    assert _run_stream(CodexBackend(), proc) == [{"type": "text", "delta": text}]
    assert proc.stderr.read.await_count == 3


def test_stream_nonzero_exit_uses_bounded_diagnostic_tail():
    marker = b"tail-marker"
    proc = _make_proc(
        [],
        returncode=2,
        stderr_lines=[b"a" * (70 * 1024), marker],
    )

    with pytest.raises(RuntimeError) as exc_info:
        _run_stream(CodexBackend(), proc)

    message = str(exc_info.value)
    assert marker.decode() in message
    assert len(message) < 600


async def _hang_read(*_args, **_kwargs):
    await asyncio.sleep(1000)


def test_stream_kills_and_reaps_subprocess_on_task_cancellation():
    proc = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.read = _hang_read
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(side_effect=[b""])
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    async def _run():
        with patch(
            "hyqs.pipeline.codex.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            backend = CodexBackend()

            async def _consume():
                async for _ in backend.stream(
                    prompt="do it", cwd="/tmp", role=Role.CODER, append_system=""
                ):
                    pass

            task = asyncio.create_task(_consume())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(_run())

    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()
