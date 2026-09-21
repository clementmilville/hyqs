"""Built-in tools: durable memory + reminders, backed by MemoryStore.

These double as a worked example of the tool pattern. To add a capability:
define a ``@tool`` coroutine returning ``{"content": [{"type": "text", ...}]}``
and add it to the ``tools=[...]`` list below.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_agent_sdk import create_sdk_mcp_server, tool

from hyqs.memory import MemoryStore


def _text(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}]}


def build_memory_server(store: MemoryStore):
    """Build the 'memory' MCP server bound to a MemoryStore instance."""

    @tool("remember_fact", "Store a durable fact about the user or the world.", {"text": str})
    async def remember_fact(args: dict) -> dict:
        fact_id = store.add_fact(args["text"])
        return _text(f"Remembered (fact #{fact_id}).")

    @tool("recall_facts", "List everything Hyqs has remembered.", {})
    async def recall_facts(args: dict) -> dict:
        rows = store.list_facts()
        if not rows:
            return _text("No facts stored yet.")
        body = "\n".join(f"#{r['id']} ({r['created_at']}): {r['text']}" for r in rows)
        return _text(body)

    @tool(
        "set_reminder",
        "Schedule a reminder that Hyqs will proactively send to this chat.",
        {"chat_id": int, "text": str, "minutes_from_now": float},
    )
    async def set_reminder(args: dict) -> dict:
        due = datetime.now(timezone.utc) + timedelta(minutes=float(args["minutes_from_now"]))
        due_iso = due.isoformat()
        rid = store.add_reminder(int(args["chat_id"]), args["text"], due_iso)
        return _text(f"Reminder #{rid} set for {due_iso} (UTC).")

    return create_sdk_mcp_server(
        name="memory",
        version="0.1.0",
        tools=[remember_fact, recall_facts, set_reminder],
    )
