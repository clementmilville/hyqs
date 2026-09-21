"""Pluggable agent backends — one coding agent per AI provider.

The pipeline's stages don't make single LLM calls; they run an *autonomous
coding agent* in a worktree ("implement this plan", "review this diff"). Every
vendor ships its own agent harness (Claude via the Agent SDK, OpenAI's Codex
CLI, …) with its own tool loop, so the right seam is a **coding-agent backend**,
not a chat-completion client.

Each backend takes (prompt, cwd, role) and returns normalized
:class:`~hyqs.pipeline.models.Usage` + text, translating its own "out of quota /
unavailable" signal into the shared :class:`ProviderUnavailable`. The contract is
the abstraction — backends are free to use whatever transport fits (Claude stays
in-process for its rich rate-limit events; Codex shells out to its CLI).
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Protocol

from .limits import ProviderUnavailable
from .models import Usage

log = logging.getLogger("hyqs.providers")


def _summarize_tool_input(tool: str, inp: dict) -> str:
    if tool == "Read":
        return f"Read {inp.get('file_path', '')}"
    if tool == "Bash":
        cmd = (inp.get("command") or "")[:80]
        return f"Bash {cmd}"
    if tool == "Grep":
        return f"Grep {inp.get('pattern', '')}"
    if tool == "Edit":
        return f"Edit {inp.get('file_path', '')}"
    if tool == "Glob":
        return f"Glob {inp.get('pattern', '')}"
    return tool


class Role(str, enum.Enum):
    """What an agent is being asked to do — maps to each backend's tool config."""

    PLANNER = "planner"  # read-only: inspect the repo, produce a plan
    CODER = "coder"  # read/write: edit the worktree (build + fix)
    REVIEWER = "reviewer"  # read-only: review the diff
    EXPLORER = "explorer"  # sandboxed read-only: job-chat endpoint, no bypassPermissions


@dataclass
class AgentRun:
    text: str
    usage: Usage


class AgentBackend(Protocol):
    """A coding agent bound to one provider (and optionally a model)."""

    name: str

    async def run(
        self,
        *,
        prompt: str,
        cwd: str | Path,
        role: Role,
        append_system: str,
        max_turns: int = 60,
        timeout: int | None = None,
    ) -> AgentRun: ...

    def stream(
        self,
        *,
        prompt: str | list,
        cwd: str | Path,
        role: Role,
        append_system: str,
        max_turns: int = 60,
        timeout: int | None = None,
        mcp_servers: dict | None = None,
    ) -> AsyncIterator[dict]: ...


# --- Claude (in-process Agent SDK) --------------------------------------

# Tool lists per role. allowed_tools is advisory only (bypassPermissions ignores
# it for tool selection); the enforced write constraint on reader roles is
# disallowed_tools (which removes the write tools from the SDK's available set
# regardless of permission_mode) plus the gate_guard snapshot check that reverts
# any file mutation a Bash call might still make.
_CLAUDE_CODER_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "TodoWrite"]
_CLAUDE_READER_TOOLS = ["Read", "Glob", "Grep", "Bash"]
# EXPLORER intentionally omits Bash: Read/Glob/Grep take structured path arguments
# whose safety the hook can verify soundly; Bash requires parsing a shell string,
# which is an unsound surface for a sandboxed chat endpoint.
_CLAUDE_EXPLORER_TOOLS = ["Read", "Glob", "Grep"]
_CLAUDE_ROLE_TOOLS = {
    Role.PLANNER: _CLAUDE_READER_TOOLS,
    Role.CODER: _CLAUDE_CODER_TOOLS,
    Role.REVIEWER: _CLAUDE_READER_TOOLS,
}

# Write tools denied to reader roles (PLANNER/REVIEWER, the latter used by both
# the review and security gates). Passed as disallowed_tools — a plain list that
# works with a string prompt, unlike a can_use_tool callback which requires
# streaming-input (AsyncIterable) mode and is bypassed under bypassPermissions.
_REVIEWER_DISALLOWED_TOOLS = ["Write", "Edit", "NotebookEdit"]
# Bash is in this list so the SDK strips it from the model's context even if the
# model somehow requests it; _CLAUDE_EXPLORER_TOOLS already omits it from allowed_tools.
_EXPLORER_DISALLOWED_TOOLS = ["Write", "Edit", "NotebookEdit", "Bash"]

# Settings JSON passed to the SDK to enforce per-tool path deny rules at the
# CLI layer (per SDK SandboxSettings docstring: filesystem restrictions belong in
# permission rules, not sandbox settings).
_EXPLORER_SETTINGS = json.dumps(
    {
        "permissions": {
            "deny": ["Read(**/.env)", "Read(**/*.env)"],
        }
    }
)


def _sandbox_deps_available() -> bool:
    import shutil

    return bool(shutil.which("bwrap") and shutil.which("socat"))


# Confirmed capacity statuses and SDK error tags. Keep this classification in
# the adapter: the rest of the pipeline only understands ProviderUnavailable.
_LIMIT_STATUSES = {429, 529}
_LIMIT_ERRORS = {
    "rate_limit",
    "rate_limit_error",
    "usage_limit",
    "usage_limit_error",
    "quota_exceeded",
    "overloaded",
    "overloaded_error",
}


def _normalized_error_type(value: object) -> str:
    if isinstance(value, str):
        return value.strip().lower().replace("-", "_")
    if isinstance(value, dict):
        nested = value.get("type") or value.get("error_type") or value.get("code")
        return _normalized_error_type(nested)
    return ""


def _claude_capacity_limit(msg: object) -> tuple[int | None, str] | None:
    """Return reset/detail only for an SDK message proving a capacity rejection."""
    if type(msg).__name__ == "RateLimitEvent":
        info = getattr(msg, "rate_limit_info", None)
        if getattr(info, "status", None) == "rejected":
            return getattr(info, "resets_at", None), "Anthropic rate/usage limit reached"
        return None

    status = getattr(msg, "api_error_status", None)
    error = getattr(msg, "error", None)
    error_type = _normalized_error_type(error)
    if status in _LIMIT_STATUSES or error_type in _LIMIT_ERRORS:
        resets_at = getattr(msg, "resets_at", None)
        return resets_at, "Anthropic rate/usage limit reached"
    return None


class ClaudeBackend:
    """Anthropic Claude via the in-process Agent SDK (``claude_agent_sdk``)."""

    name = "claude"

    def __init__(self, model: str) -> None:
        self.model = model

    async def run(
        self,
        *,
        prompt: str,
        cwd: str | Path,
        role: Role,
        append_system: str,
        max_turns: int = 60,
        timeout: int | None = None,
    ) -> AgentRun:
        # Imported lazily so a Codex-only deployment needn't install the SDK.
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            RateLimitEvent,
            ResultMessage,
            TextBlock,
            query,
        )

        # CODER uses acceptEdits to auto-accept edits unattended while restricting
        # writes to paths inside cwd/add_dirs. REVIEWER/PLANNER use bypassPermissions
        # (so Bash read-only ops like git diff work without prompts) plus
        # disallowed_tools to remove Write/Edit from the SDK's available toolset.
        if role == Role.CODER:
            options = ClaudeAgentOptions(
                model=self.model,
                cwd=str(cwd),
                system_prompt={"type": "preset", "preset": "claude_code", "append": append_system},
                permission_mode="acceptEdits",
                add_dirs=[str(cwd)],
                allowed_tools=_CLAUDE_ROLE_TOOLS[role],
                max_turns=max_turns,
            )
        elif role == Role.EXPLORER:
            if not _sandbox_deps_available():
                raise RuntimeError(
                    "EXPLORER agent unavailable: sandbox requires bwrap and socat on PATH"
                )
            from claude_agent_sdk.types import HookMatcher

            from .explorer_sandbox import make_path_guard

            options = ClaudeAgentOptions(
                model=self.model,
                cwd=str(cwd),
                system_prompt={"type": "preset", "preset": "claude_code", "append": append_system},
                sandbox={
                    "enabled": True,
                    "autoAllowBashIfSandboxed": True,
                    "allowUnsandboxedCommands": False,
                    "network": {"allowedDomains": []},
                },
                hooks={"PreToolUse": [HookMatcher(hooks=[make_path_guard(Path(cwd))])]},
                allowed_tools=_CLAUDE_EXPLORER_TOOLS,
                disallowed_tools=_EXPLORER_DISALLOWED_TOOLS,
                settings=_EXPLORER_SETTINGS,
                max_turns=max_turns,
            )
        else:
            options = ClaudeAgentOptions(
                model=self.model,
                cwd=str(cwd),
                system_prompt={"type": "preset", "preset": "claude_code", "append": append_system},
                permission_mode="bypassPermissions",
                allowed_tools=_CLAUDE_ROLE_TOOLS[role],
                disallowed_tools=_REVIEWER_DISALLOWED_TOOLS,
                max_turns=max_turns,
            )
        chunks: list[str] = []
        usage = Usage(model=self.model, provider=self.name)

        async def _consume() -> None:
            nonlocal usage
            gen = query(prompt=prompt, options=options)
            try:
                async for msg in gen:
                    # DIAGNOSTIC: record the raw shape of every message so the next
                    # time a subscription/usage limit hits we have ground truth on
                    # how it actually surfaces (RateLimitEvent vs. an is_error
                    # ResultMessage vs. a silent empty turn). Observational only.
                    log.debug(
                        "claude stream msg [%s role=%s]: %s",
                        self.name,
                        role.value,
                        type(msg).__name__,
                    )
                    if isinstance(msg, RateLimitEvent):
                        info = msg.rate_limit_info
                        log.info(
                            "claude RateLimitEvent [role=%s]: status=%s type=%s "
                            "resets_at=%s utilization=%s overage_status=%s",
                            role.value,
                            info.status,
                            getattr(info, "rate_limit_type", None),
                            info.resets_at,
                            getattr(info, "utilization", None),
                            getattr(info, "overage_status", None),
                        )
                        limit = _claude_capacity_limit(msg)
                        if limit is not None:
                            raise ProviderUnavailable(self.name, *limit)
                    elif isinstance(msg, AssistantMessage):
                        err = getattr(msg, "error", None)
                        if err is not None:
                            log.info("claude AssistantMessage.error=%r [role=%s]", err, role.value)
                        limit = _claude_capacity_limit(msg)
                        if limit is not None:
                            raise ProviderUnavailable(self.name, *limit)
                        if err is not None:
                            raise RuntimeError(f"Claude assistant error: {err}")
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                chunks.append(block.text)
                    elif isinstance(msg, ResultMessage):
                        log.info(
                            "claude ResultMessage [role=%s]: subtype=%s is_error=%s "
                            "api_error_status=%s stop_reason=%s num_turns=%s "
                            "errors=%r result_len=%s",
                            role.value,
                            msg.subtype,
                            msg.is_error,
                            msg.api_error_status,
                            getattr(msg, "stop_reason", None),
                            getattr(msg, "num_turns", None),
                            getattr(msg, "errors", None),
                            len(msg.result or ""),
                        )
                        if msg.is_error:
                            # Short snippet only (not full agent output) so a limit/
                            # refusal message is captured without dumping code.
                            log.info(
                                "claude error result snippet [role=%s]: %.300r",
                                role.value,
                                msg.result or "",
                            )
                        usage = Usage.from_result(
                            msg.usage, msg.total_cost_usd, self.model, self.name
                        )
                        if msg.is_error:
                            limit = _claude_capacity_limit(msg)
                            if limit is not None:
                                raise ProviderUnavailable(self.name, *limit)
                            raise RuntimeError(msg.result or "Claude agent returned an error")
                        if msg.result:
                            chunks.append(msg.result)
            except asyncio.CancelledError:
                await gen.aclose()
                raise

        if timeout:
            await asyncio.wait_for(_consume(), timeout=timeout)
        else:
            await _consume()

        text = "\n".join(c for c in chunks if c).strip()
        if not text:
            # An AI stage that produced nothing is the fingerprint of a swallowed
            # limit/refusal — it currently flows on as "no changes → fail" instead
            # of pausing. Flag it loudly until detection is proven to cover it.
            log.warning(
                "claude %s run produced EMPTY text [role=%s model=%s] — possible "
                "undetected limit/refusal (would surface as 'no changes')",
                self.name,
                role.value,
                self.model,
            )
        return AgentRun(text=text, usage=usage)

    async def stream(
        self,
        *,
        prompt: str | list,
        cwd: str | Path,
        role: Role,
        append_system: str,
        max_turns: int = 60,
        timeout: int | None = None,  # noqa: ARG002 — kept for interface compat
        mcp_servers: dict | None = None,
    ) -> AsyncIterator[dict]:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            RateLimitEvent,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            query,
        )

        if role == Role.CODER:
            options = ClaudeAgentOptions(
                model=self.model,
                cwd=str(cwd),
                system_prompt={"type": "preset", "preset": "claude_code", "append": append_system},
                permission_mode="acceptEdits",
                add_dirs=[str(cwd)],
                allowed_tools=_CLAUDE_ROLE_TOOLS[role],
                max_turns=max_turns,
            )
        elif role == Role.EXPLORER:
            if not _sandbox_deps_available():
                raise RuntimeError(
                    "EXPLORER agent unavailable: sandbox requires bwrap and socat on PATH"
                )
            from claude_agent_sdk.types import HookMatcher

            from .explorer_sandbox import make_path_guard

            options = ClaudeAgentOptions(
                model=self.model,
                cwd=str(cwd),
                system_prompt={"type": "preset", "preset": "claude_code", "append": append_system},
                sandbox={
                    "enabled": True,
                    "autoAllowBashIfSandboxed": True,
                    "allowUnsandboxedCommands": False,
                    "network": {"allowedDomains": []},
                },
                hooks={"PreToolUse": [HookMatcher(hooks=[make_path_guard(Path(cwd))])]},
                allowed_tools=_CLAUDE_EXPLORER_TOOLS,
                disallowed_tools=_EXPLORER_DISALLOWED_TOOLS,
                settings=_EXPLORER_SETTINGS,
                mcp_servers=mcp_servers or {},
                max_turns=max_turns,
            )
        else:
            options = ClaudeAgentOptions(
                model=self.model,
                cwd=str(cwd),
                system_prompt={"type": "preset", "preset": "claude_code", "append": append_system},
                permission_mode="bypassPermissions",
                allowed_tools=_CLAUDE_ROLE_TOOLS[role],
                disallowed_tools=_REVIEWER_DISALLOWED_TOOLS,
                max_turns=max_turns,
            )

        text_chunks: list[str] = []
        gen = query(prompt=prompt, options=options)
        try:
            async for msg in gen:
                if isinstance(msg, RateLimitEvent):
                    limit = _claude_capacity_limit(msg)
                    if limit is not None:
                        raise ProviderUnavailable(self.name, *limit)
                elif isinstance(msg, AssistantMessage):
                    limit = _claude_capacity_limit(msg)
                    if limit is not None:
                        raise ProviderUnavailable(self.name, *limit)
                    err = getattr(msg, "error", None)
                    if err is not None:
                        raise RuntimeError(f"Claude assistant error: {err}")
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_chunks.append(block.text)
                            yield {"type": "text", "delta": block.text}
                        elif isinstance(block, ToolUseBlock):
                            yield {
                                "type": "tool_use",
                                "tool": block.name,
                                "input_summary": _summarize_tool_input(
                                    block.name, block.input or {}
                                ),
                            }
                        elif type(block).__name__ == "ThinkingBlock":
                            delta = (
                                getattr(block, "thinking", "") or getattr(block, "text", "") or ""
                            )
                            yield {"type": "thinking", "delta": delta}
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        limit = _claude_capacity_limit(msg)
                        if limit is not None:
                            raise ProviderUnavailable(self.name, *limit)
                        raise RuntimeError(msg.result or "Claude agent returned an error")
                    result_text = "\n".join(c for c in text_chunks if c).strip()
                    if not result_text and msg.result:
                        result_text = msg.result
                    usage_obj = Usage.from_result(
                        msg.usage, msg.total_cost_usd, self.model, self.name
                    )
                    yield {
                        "type": "result",
                        "text": result_text,
                        "usage": {
                            "input_tokens": usage_obj.input_tokens,
                            "output_tokens": usage_obj.output_tokens,
                            "cache_creation_tokens": usage_obj.cache_creation_tokens,
                            "cache_read_tokens": usage_obj.cache_read_tokens,
                            "cost_usd": usage_obj.cost_usd,
                        },
                    }
        except asyncio.CancelledError:
            await gen.aclose()
            raise


# --- registry -----------------------------------------------------------


def build_backend(provider: str, model: str = "", *, config=None) -> AgentBackend:
    """Construct the backend for ``provider`` (falling back to a sensible model)."""
    provider = (provider or "claude").strip().lower()
    if provider == "claude":
        resolved = (
            model
            or (getattr(config, "pipeline_model", "") or "")
            or getattr(config, "model", "sonnet")
        )
        return ClaudeBackend(resolved)
    if provider == "codex":
        from .codex import CodexBackend  # imported here to keep the dep optional

        # An empty model intentionally delegates selection to the authenticated
        # Codex CLI.  ChatGPT and API-key logins can expose different model sets,
        # so pinning a historical fallback here can make an otherwise healthy CLI
        # unusable.
        return CodexBackend(model or getattr(config, "codex_model", ""), config=config)
    raise ValueError(f"unknown agent provider: {provider!r}")


def known_providers() -> list[str]:
    return ["claude", "codex"]
