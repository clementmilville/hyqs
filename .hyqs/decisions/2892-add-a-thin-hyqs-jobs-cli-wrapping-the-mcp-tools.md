# Job #2892: Add a thin hyqs-jobs CLI wrapping the MCP tools

**Date:** 2026-07-31

The diff introduces a new `hyqs-jobs` CLI command that provides a lightweight interface to hyqs job operations via OAuth-authenticated MCP tool calls, entirely separate from the main `hyqs` console script that boots the full web/pipeline/scheduler service. The CLI reuses the existing Google OAuth infrastructure from the MCP server, caches tokens persistently on disk to avoid repeated browser flows, and maps argparse subcommands (projects, epics, jobs) to corresponding MCP tool invocations with semantic exit codes (0 success, 1 error, 3 forbidden, 4 not found, 2 transport failure). Comprehensive tests verify OAuth client setup, response classification, argument dispatch, and end-to-end execution flows.
The diff introduces a new `hyqs-jobs` CLI command that provides a lightweight interface to hyqs job operations via OAuth-authenticated MCP tool calls, entirely separate from the main `hyqs` console script that boots the full web/pipeline/scheduler service. The CLI reuses the existing Google OAuth infrastructure from the MCP server, caches tokens persistently on disk to avoid repeated browser flows, and maps argparse subcommands (projects, epics, jobs) to corresponding MCP tool invocations with semantic exit codes (0 success, 1 error, 3 forbidden, 4 not found, 2 transport failure). Comprehensive tests verify OAuth client setup, response classification, argument dispatch, and end-to-end execution flows.

## Files touched
- hyqs/cli/__init__.py
- hyqs/cli/client.py
- hyqs/cli/main.py
- pyproject.toml
- tests/test_cli.py
