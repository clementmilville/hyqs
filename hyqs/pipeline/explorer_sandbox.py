"""Path-guard hook factory for the EXPLORER agent role.

Returns a PreToolUse hook callback that deterministically denies any
Read/Glob/Grep/Bash whose resolved target escapes the sandboxed repo root.
This is the security boundary for the job-chat endpoint.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

# Characters that make per-token analysis unreliable or enable stdin/stdout
# redirect, pipe chaining, or command substitution.
_COMMAND_LEVEL_DENY_RE = re.compile(r"[`;|<>]")

# Commands whose primary purpose is environment introspection or network egress.
# EXPLORER agents do not need any of these — their tool list already omits Bash,
# but this guard acts as defence-in-depth if the hook is ever called for Bash.
_BLOCKED_COMMANDS = frozenset(
    [
        "env",
        "printenv",
        "set",
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "ssh",
        "scp",
        "sftp",
        "ftp",
        "telnet",
        "socat",
        "openssl",
    ]
)

# Finds absolute paths embedded inside shell tokens, e.g. the '/etc/passwd'
# inside  python3 -c "open('/etc/passwd').read()".
# Match '/' preceded by a non-path character so that 'src/foo.py' is NOT
# flagged (the '/' there is preceded by 'c', a word character).
_EMBEDDED_PATH_RE = re.compile(r"""(?:(?<=['"()\s=&|])|^)(/[^\s'"`;|&()\\]*)""")


def make_path_guard(root: Path):
    """Return an async PreToolUse hook that blocks reads outside *root*."""
    resolved_root = root.resolve()

    async def _guard(hook_input, tool_use_id, context):
        tool_name = hook_input["tool_name"]
        tool_input = hook_input["tool_input"]

        def _deny(reason: str = "path escapes sandbox") -> dict:
            return {
                "decision": "block",
                "reason": reason,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                },
            }

        def _allow() -> dict:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                },
            }

        def _path_escapes(path_str: str) -> bool:
            try:
                return not Path(path_str).resolve().is_relative_to(resolved_root)
            except Exception:
                return True

        if tool_name == "Read":
            path_str = tool_input.get("file_path") or ""
            if not path_str:
                return _allow()
            if _path_escapes(path_str):
                return _deny()
            return _allow()

        if tool_name in ("Glob", "Grep"):
            path_str = tool_input.get("path") or ""
            if not path_str:
                return _allow()
            if _path_escapes(path_str):
                return _deny()
            return _allow()

        if tool_name == "Bash":
            command = tool_input.get("command") or ""

            # Block stdin/stdout redirects, pipes, backticks, and semicolons.
            if _COMMAND_LEVEL_DENY_RE.search(command):
                return _deny("command contains disallowed shell operator")

            try:
                tokens = shlex.split(command)
            except ValueError:
                return _deny("command cannot be safely parsed")

            if tokens and tokens[0].lower() in _BLOCKED_COMMANDS:
                return _deny(f"command '{tokens[0]}' not permitted in explorer sandbox")

            for token in tokens:
                # Tilde expansion produces paths without a leading '/' but
                # resolves to arbitrary filesystem locations.
                if token.startswith("~"):
                    return _deny("tilde expansion not permitted")

                # Variable and command substitution ($HOME, ${X}, $(cmd))
                # expand to arbitrary strings at runtime.
                if "$" in token:
                    return _deny("variable/command expansion not permitted")

                # Relative path traversal out of the sandbox root.
                if (
                    token == ".."
                    or token.startswith("../")
                    or "/../" in token
                    or token.endswith("/..")
                ):
                    return _deny("relative path traversal")

                # Absolute paths as bare tokens.
                if token.startswith("/"):
                    if _path_escapes(token):
                        return _deny()

                # Absolute paths embedded inside a token, e.g. inside a -c
                # argument: python3 -c "open('/etc/passwd').read()"
                for m in _EMBEDDED_PATH_RE.finditer(token):
                    if _path_escapes(m.group(1)):
                        return _deny()

            return _allow()

        # Allow our in-process project-tools MCP server.  These tools are
        # defined in chat_tools.py and are project-scoped in Python; they take
        # only structured scalar arguments and have no filesystem-path surface
        # that this guard needs to police.
        if tool_name.startswith("mcp__project-tools__"):
            return _allow()

        # Deny-by-default: any unrecognised tool is blocked rather than permitted.
        return _deny("tool not permitted in explorer sandbox")

    return _guard
