# Job #3224: Apply exclusion + metadata to MCP project_performance and agent_stats tools

**Date:** 2026-08-02

This diff adds an `include_operational` parameter to the `project_performance` and `agent_stats` MCP tools to control whether operational auto-deploy jobs are included in performance statistics; by default they're excluded. Both endpoints now return envelope objects that include an `excludes_operational` flag to signal the filtering state, and their underlying store calls are updated to forward the parameter. The change lets callers optionally see complete stats or exclude noisy operational jobs, with comprehensive tests verifying parameter forwarding and authorization checks.
This diff adds an `include_operational` parameter to the `project_performance` and `agent_stats` MCP tools to control whether operational auto-deploy jobs are included in performance statistics; by default they're excluded. Both endpoints now return envelope objects that include an `excludes_operational` flag to signal the filtering state, and their underlying store calls are updated to forward the parameter. The change lets callers optionally see complete stats or exclude noisy operational jobs, with comprehensive tests verifying parameter forwarding and authorization checks.

## Files touched
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
