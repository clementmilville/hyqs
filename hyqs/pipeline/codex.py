"""Codex backend — OpenAI's coding agent via its CLI (``codex exec``).

Unlike Claude (in-process SDK), Codex ships its own agent harness as a CLI that
runs the tool loop itself. So this backend shells out: it runs ``codex exec`` in
the job's worktree, lets Codex edit files directly, and returns its stdout as the
stage text. The exact invocation is configurable (``HYQS_CODEX_*``) because CLI
flags drift between Codex versions; the defaults target unattended full-auto.

Provider-unavailable signals (quota/rate limit) are detected from the CLI's
output and re-raised as the shared :class:`ProviderUnavailable`, so the
deterministic pause gate treats Codex exactly like Claude — but independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from pathlib import Path

from .limits import MAX_PROVIDER_PAUSE_SECONDS, ProviderUnavailable
from .models import Usage
from .pricing import compute_cost
from .providers import AgentRun, Role

log = logging.getLogger("hyqs.codex")

_STREAM_CHUNK_SIZE = 64 * 1024
_MAX_JSONL_EVENT_SIZE = 8 * 1024 * 1024
_DIAGNOSTIC_TAIL_SIZE = 64 * 1024
_OVERSIZED_EVENT_ERROR = "Codex stream transport error: JSONL event exceeded the 8 MiB limit"

_CAPACITY_ERROR_TYPES = {
    "rate_limit_exceeded",
    "rate_limit_error",
    "quota_exceeded",
    "usage_limit_exceeded",
    "overloaded",
    "overloaded_error",
}
_TEXT_CAPACITY_RE = re.compile(
    r"\b(?:429(?:\s+too many requests)?|too many requests|rate limit exceeded|"
    r"quota (?:is )?exhausted|quota exceeded|usage limit (?:reached|exceeded))\b",
    re.IGNORECASE,
)

# Best-effort token parse from Codex's summary line, e.g. "tokens used: 1234".
_TOKENS_RE = re.compile(r"tokens?\s*(?:used)?[:=]?\s*([0-9][0-9,]*)", re.IGNORECASE)
# Split-token patterns for when Codex emits a per-direction breakdown.
_INPUT_TOKENS_RE = re.compile(r"input\s+tokens?\s*[:=]\s*([0-9][0-9,]*)", re.IGNORECASE)
_OUTPUT_TOKENS_RE = re.compile(r"output\s+tokens?\s*[:=]\s*([0-9][0-9,]*)", re.IGNORECASE)

_ROLE_HINT = {
    Role.PLANNER: "You are the PLANNER. Do not modify files; only inspect and plan.",
    Role.CODER: "You are the CODER. Edit files in the working directory to implement the task.",
    Role.REVIEWER: "You are the REVIEWER. Do not modify files; only review.",
}


def _walk_error_payload(value: object):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_error_payload(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_error_payload(nested)


def _reset_timestamp(payload: dict) -> int | None:
    maximum = int(time.time()) + MAX_PROVIDER_PAUSE_SECONDS
    for obj in _walk_error_payload(payload):
        for key in ("resets_at", "reset_at", "reset_timestamp", "retry_at"):
            value = obj.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return min(int(value), maximum)
            if isinstance(value, str) and value.strip().isdigit():
                return min(int(value.strip()), maximum)
    return None


def _structured_limit(payload: dict) -> bool:
    for obj in _walk_error_payload(payload):
        status = obj.get("status") or obj.get("status_code")
        if status == 429 or status == "429":
            return True
        for key in ("type", "code", "error_type"):
            value = obj.get(key)
            if isinstance(value, str) and value.lower().replace("-", "_") in _CAPACITY_ERROR_TYPES:
                return True
    return False


def _parse_error_json(line: str) -> dict | None:
    start = line.find("{")
    if start < 0:
        return None
    try:
        value = json.loads(line[start:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _classify_limit_error(out: str, err: str) -> int | None | bool:
    """Return reset timestamp/True for a confirmed capacity error, else False.

    Restrict matching to error-bearing lines so words in the echoed agent prompt
    cannot turn an unsupported-model response into a false provider pause.
    """
    for source, text in (("stdout", out), ("stderr", err)):
        for line in text.splitlines():
            if "error" not in line.lower():
                continue
            payload = _parse_error_json(line)
            if payload is not None and _structured_limit(payload):
                # Codex's stderr is the transport channel for CLI failures.
                # Stdout may contain model-authored JSON, so never trust reset
                # metadata found there even when it resembles an error event.
                if source == "stderr":
                    return _reset_timestamp(payload) or True
                return True
            if _TEXT_CAPACITY_RE.search(line):
                return True
    return False


def _is_limit_error(out: str, err: str) -> bool:
    """Compatibility predicate for tests and callers without reset metadata."""
    return _classify_limit_error(out, err) is not False


class CodexBackend:
    """OpenAI Codex via ``codex exec``, run unattended in the worktree."""

    name = "codex"

    def __init__(self, model: str = "", *, config=None) -> None:
        self.model = model
        self.bin = (getattr(config, "codex_bin", "") or "codex") if config else "codex"
        extra = getattr(config, "codex_extra_args", "") if config else ""
        # Defaults to full-auto so the agent runs without approval prompts.
        self.extra_args = shlex.split(extra) if extra else ["exec", "--full-auto"]

    def _command(self, prompt: str) -> list[str]:
        cmd = [self.bin, *self.extra_args]
        if self.model:
            cmd += ["-m", self.model]
        cmd.append(prompt)
        return cmd

    def _stream_command(self, prompt: str) -> list[str]:
        # --json is only meaningful for `codex exec`; keep any other configured
        # flags (e.g. --full-auto) but drop a redundant leading "exec".
        rest = [a for a in self.extra_args if a != "exec"]
        cmd = [self.bin, "exec", "--json", *rest]
        if self.model:
            cmd += ["-m", self.model]
        cmd.append(prompt)
        return cmd

    async def run(
        self,
        *,
        prompt: str,
        cwd: str | Path,
        role: Role,
        append_system: str,
        max_turns: int = 60,  # noqa: ARG002 - the Codex CLI manages its own loop
        timeout: int | None = None,
    ) -> AgentRun:
        # Codex exec has no separate system channel, so fold the role + the
        # stage's system instructions into the prompt.
        full_prompt = f"{_ROLE_HINT.get(role, '')}\n\n{append_system}\n\n---\n\n{prompt}".strip()
        cmd = self._command(full_prompt)
        log.info("codex exec (model=%s) in %s", self.model or "<default>", cwd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise

        out = out_b.decode("utf-8", "replace")
        err = err_b.decode("utf-8", "replace")

        if proc.returncode != 0:
            limit = _classify_limit_error(out, err)
            if limit is not False:
                resets_at = limit if type(limit) is int else None
                raise ProviderUnavailable(self.name, resets_at, "Codex reported a rate/usage limit")
            raise RuntimeError(
                f"codex exec failed (exit {proc.returncode}): {err[:500] or out[:500]}"
            )

        return AgentRun(text=out.strip(), usage=self._usage(out, err))

    async def stream(
        self,
        *,
        prompt: str,
        cwd,
        role,
        append_system: str,
        max_turns: int = 60,
        timeout=None,
    ):
        """Stream ``codex exec --json`` events as they're emitted, translating
        Codex's JSONL schema into the same event shape ClaudeBackend.stream() yields."""
        full_prompt = f"{_ROLE_HINT.get(role, '')}\n\n{append_system}\n\n---\n\n{prompt}".strip()
        cmd = self._stream_command(full_prompt)
        log.info("codex exec --json (model=%s) in %s", self.model or "<default>", cwd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stderr_tail = bytearray()

        def _append_tail(tail: bytearray, data: bytes) -> None:
            tail.extend(data)
            if len(tail) > _DIAGNOSTIC_TAIL_SIZE:
                del tail[:-_DIAGNOSTIC_TAIL_SIZE]

        async def _drain_stderr() -> None:
            while True:
                chunk = await proc.stderr.read(_STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                _append_tail(stderr_tail, chunk)

        stderr_task = asyncio.create_task(_drain_stderr())
        stdout_tail = bytearray()
        text_chunks: list[str] = []
        record_buffer = bytearray()
        completed = False

        try:
            async with asyncio.timeout(timeout):
                while True:
                    chunk = await proc.stdout.read(_STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    _append_tail(stdout_tail, chunk)
                    record_buffer.extend(chunk)
                    while True:
                        newline = record_buffer.find(b"\n")
                        if newline < 0:
                            break
                        record = bytes(record_buffer[:newline])
                        del record_buffer[: newline + 1]
                        if len(record) > _MAX_JSONL_EVENT_SIZE:
                            raise RuntimeError(_OVERSIZED_EVENT_ERROR)
                        event = self._parse_stream_record(record, text_chunks)
                        if event is not None:
                            yield event
                    if len(record_buffer) > _MAX_JSONL_EVENT_SIZE:
                        raise RuntimeError(_OVERSIZED_EVENT_ERROR)

                if record_buffer:
                    event = self._parse_stream_record(bytes(record_buffer), text_chunks)
                    if event is not None:
                        yield event

                await proc.wait()
                await stderr_task
                completed = True
        finally:
            if not completed:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)

        if proc.returncode != 0:
            stdout_text = stdout_tail.decode("utf-8", "replace")
            stderr_text = stderr_tail.decode("utf-8", "replace")
            limit = _classify_limit_error(stdout_text, stderr_text)
            if limit is not False:
                resets_at = limit if type(limit) is int else None
                raise ProviderUnavailable(self.name, resets_at, "Codex reported a rate/usage limit")
            diagnostic = stderr_text or stdout_text
            raise RuntimeError(f"codex exec failed (exit {proc.returncode}): {diagnostic[-500:]}")

    def _parse_stream_record(self, record: bytes, text_chunks: list[str]) -> dict | None:
        """Translate one complete JSONL record, skipping malformed records."""
        decoded = record.decode("utf-8", "replace").strip()
        if not decoded:
            return None
        try:
            obj = json.loads(decoded)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        obj_type = obj.get("type")
        if obj_type == "item.completed":
            translated = self._translate_item(obj.get("item") or {})
            if translated is not None and translated["type"] == "text":
                text_chunks.append(translated["delta"])
            return translated
        if obj_type == "turn.completed":
            return self._result_event(obj.get("usage") or {}, text_chunks)
        return None

    def _translate_item(self, item: dict) -> dict | None:
        """One recognized ``item.completed`` payload -> a stream event, or None
        to skip (unrecognized item types, and Codex-reported item errors)."""
        kind = item.get("type")
        if kind == "agent_message":
            return {"type": "text", "delta": item.get("text", "")}
        if kind == "command_execution":
            return {
                "type": "tool_use",
                "tool": "command_execution",
                "input_summary": str(item.get("command", ""))[:200],
            }
        if kind == "file_change":
            changes = item.get("changes") or []
            summary = ", ".join(f"{c.get('kind', '')} {c.get('path', '')}".strip() for c in changes)
            return {"type": "tool_use", "tool": "file_change", "input_summary": summary[:200]}
        return None

    def _result_event(self, usage_raw: dict, text_chunks: list[str]) -> dict:
        input_tokens = int(usage_raw.get("input_tokens", 0) or 0)
        output_tokens = int(usage_raw.get("output_tokens", 0) or 0) + int(
            usage_raw.get("reasoning_output_tokens", 0) or 0
        )
        cache_read_tokens = int(usage_raw.get("cached_input_tokens", 0) or 0)
        cache_creation_tokens = int(usage_raw.get("cache_write_input_tokens", 0) or 0)
        # Codex's input_tokens is an OpenAI-style total inclusive of both cache
        # buckets (confirmed via `codex exec --json`: cached_input_tokens and
        # cache_write_input_tokens are always <= input_tokens), unlike Anthropic's
        # already-disjoint usage fields that ClaudeBackend passes straight
        # through. Bill only the fresh remainder at the full rate so
        # compute_cost's disjoint-bucket contract never prices a token twice.
        fresh_input_tokens = max(input_tokens - cache_read_tokens - cache_creation_tokens, 0)
        return {
            "type": "result",
            "text": "\n".join(text_chunks),
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_creation_tokens": cache_creation_tokens,
                "cost_usd": compute_cost(
                    self.model,
                    fresh_input_tokens,
                    output_tokens,
                    cache_creation_tokens,
                    cache_read_tokens,
                ),
                "model": self.model,
                "provider": self.name,
            },
        }

    def _usage(self, out: str, err: str) -> Usage:
        haystack = out + "\n" + err
        m_in = _INPUT_TOKENS_RE.search(haystack)
        m_out = _OUTPUT_TOKENS_RE.search(haystack)
        if m_in and m_out:
            try:
                input_tokens = int(m_in.group(1).replace(",", ""))
                output_tokens = int(m_out.group(1).replace(",", ""))
            except ValueError:
                input_tokens = output_tokens = 0
        else:
            # Codex CLI does not guarantee an input/output breakdown; record the
            # total as output_tokens with input_tokens=0.
            m = _TOKENS_RE.search(haystack)
            input_tokens = 0
            output_tokens = 0
            if m:
                try:
                    output_tokens = int(m.group(1).replace(",", ""))
                except ValueError:
                    pass
        cost = compute_cost(self.model, input_tokens, output_tokens)
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            model=self.model,
            provider=self.name,
        )
