"""In-process MCP tool servers that give Hyqs custom capabilities.

Each function decorated with ``@tool`` becomes a tool the agent can call.
Group related tools into a server with ``create_sdk_mcp_server`` and register
the server in ``hyqs/core/agent.py``. This module is the seam where you bolt
on new "capacities".
"""

from .builtin import build_memory_server

__all__ = ["build_memory_server"]
