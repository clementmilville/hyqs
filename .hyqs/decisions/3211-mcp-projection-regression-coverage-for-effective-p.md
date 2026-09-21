# Job #3211: MCP projection regression coverage for effective priority

**Date:** 2026-08-02

This diff adds a test that verifies the MCP server properly applies a remediation priority boost to fix-forward jobs across multiple API projections. The test creates a parent job needing remediation and a child job marked as fixing it, then confirms that both `get_job` and `list_jobs` endpoints—regardless of summary mode—return an elevated `effective_priority` and include "remediation" in the `priority_reasons`. This ensures consistency in how the server surfaces remediation priority boosts across different query paths.
This diff adds a test that verifies the MCP server properly applies a remediation priority boost to fix-forward jobs across multiple API projections. The test creates a parent job needing remediation and a child job marked as fixing it, then confirms that both `get_job` and `list_jobs` endpoints—regardless of summary mode—return an elevated `effective_priority` and include "remediation" in the `priority_reasons`. This ensures consistency in how the server surfaces remediation priority boosts across different query paths.

## Files touched
- tests/test_mcp_server.py
