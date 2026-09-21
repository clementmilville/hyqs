# Job #2890: Add selective-field / summary mode to MCP get_job and list_jobs

**Date:** 2026-07-31

This diff adds a lightweight summary projection for job objects in the MCP server. A new `job_to_summary_dict()` function creates a reduced view of jobs excluding large text blobs (idea, plan, review) while preserving essential metadata and status fields. The `list_jobs()` and `get_job()` MCP tools now accept an optional `summary` parameter that returns this lighter projection when enabled, reducing payload size for scenarios where full job details aren't needed. Comprehensive tests verify both summary and full-dict response modes work correctly.
This diff adds a lightweight summary projection for job objects in the MCP server. A new `job_to_summary_dict()` function creates a reduced view of jobs excluding large text blobs (idea, plan, review) while preserving essential metadata and status fields. The `list_jobs()` and `get_job()` MCP tools now accept an optional `summary` parameter that returns this lighter projection when enabled, reducing payload size for scenarios where full job details aren't needed. Comprehensive tests verify both summary and full-dict response modes work correctly.

## Files touched
- hyqs/web/app.py
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
