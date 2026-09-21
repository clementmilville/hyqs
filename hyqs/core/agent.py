"""The orchestrator: one persistent Claude agent session per chat.

Each ``chat_id`` gets its own ``ClaudeSDKClient`` so conversation context is
preserved across messages. ``chat_id`` identifies one authenticated
principal's session (e.g. a ``users.id``, or a reserved sentinel for the
platform web-token bypass) — callers must key it on the caller's identity, not
share a single constant across all users. Tools, memory and (later) skills are
wired in here.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from hyqs.config import Config
from hyqs.memory import MemoryStore
from hyqs.pipeline.providers import _summarize_tool_input
from hyqs.tools import build_memory_server

log = logging.getLogger("hyqs.agent")

# The `hyqs` package doubles as a local plugin: its `.claude-plugin/plugin.json`
# + `skills/` dir ship Hyqs's own know-how, loaded into every session below.
PACKAGE_DIR = Path(__file__).resolve().parent.parent

# Tools we explicitly pre-approve (no callback round-trip).
MEMORY_TOOLS = [
    "mcp__memory__remember_fact",
    "mcp__memory__recall_facts",
    "mcp__memory__set_reminder",
]
BUILTIN_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
]

SYSTEM_APPEND = """\
You are Hyqs, a personal 24/7 AI orchestration agent working for your owner.
You communicate via the web console, so keep replies concise and chat-friendly
(short paragraphs, minimal markdown, no giant code dumps unless asked).

You have durable memory: use the `remember_fact` tool to persist anything the
owner tells you to remember, and `recall_facts` to look it up. You can schedule
proactive reminders with `set_reminder` for THIS chat session — its id is
{chat_id}.

You can use tools to actually get things done (files, shell, web). Be proactive
and decisive: when a task is clear, do it and report the result rather than
asking for confirmation on every step.
"""


class Orchestrator:
    def __init__(self, config: Config, store: MemoryStore) -> None:
        self.config = config
        self.store = store
        self._memory_server = build_memory_server(store)
        self._clients: dict[int, ClaudeSDKClient] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def _permit(
        self, tool_name: str, tool_input: dict, context: ToolPermissionContext
    ) -> PermissionResultAllow:
        # Unattended operation: auto-approve so the agent never blocks waiting
        # for a human. Tighten this (deny-list, confirmation channel) as needed.
        log.info("tool-call: %s", tool_name)
        return PermissionResultAllow()

    def _build_options(self, chat_id: int) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=self.config.model,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": SYSTEM_APPEND.format(chat_id=chat_id),
            },
            permission_mode=self.config.permission_mode,  # type: ignore[arg-type]
            mcp_servers={"memory": self._memory_server},
            allowed_tools=MEMORY_TOOLS + BUILTIN_TOOLS,
            can_use_tool=self._permit,
            cwd=str(self.config.data_dir),
            # Load Hyqs's bundled skills (hyqs/skills/) via the local plugin.
            # `skills="all"` enables every discovered skill and wires up the
            # Skill tool for us.
            plugins=[{"type": "local", "path": str(PACKAGE_DIR)}],
            skills="all",
        )

    async def _client_for(self, chat_id: int) -> ClaudeSDKClient:
        client = self._clients.get(chat_id)
        if client is None:
            client = ClaudeSDKClient(options=self._build_options(chat_id))
            await client.connect()
            self._clients[chat_id] = client
            self._locks[chat_id] = asyncio.Lock()
            log.info("opened agent session for chat %s", chat_id)
        return client

    async def ask(self, chat_id: int, text: str) -> str:
        """Send a message on the given principal's session and return the agent's full text reply."""
        client = await self._client_for(chat_id)
        async with self._locks[chat_id]:
            await client.query(text)
            chunks: list[str] = []
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
            reply = "\n".join(c for c in chunks if c).strip()
            return reply or "(Hyqs had nothing to say.)"

    async def stream_ask(self, chat_id: int, text: str):
        """Stream a message on the given principal's session as it's generated."""
        client = await self._client_for(chat_id)
        async with self._locks[chat_id]:
            await client.query(text)
            chunks: list[str] = []
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
                            yield {"type": "text", "delta": block.text}
                        elif isinstance(block, ToolUseBlock):
                            yield {
                                "type": "tool_use",
                                "tool": block.name,
                                "input_summary": _summarize_tool_input(
                                    block.name, block.input or {}
                                ),
                            }
            reply = "\n".join(c for c in chunks if c).strip() or "(Hyqs had nothing to say.)"
            yield {"type": "result", "text": reply}

    async def aclose(self) -> None:
        for chat_id, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                log.warning("error closing session %s", chat_id, exc_info=True)
        self._clients.clear()
        self._locks.clear()
