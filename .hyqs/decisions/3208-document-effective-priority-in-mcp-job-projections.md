# Job #3208: Document effective priority in MCP job projections

**Date:** 2026-08-02

This diff exposes two new fields—`effective_priority` and `priority_reasons`—in the hyqs MCP API responses for `get_job` and `list_jobs` endpoints (both full and summary modes). The docstrings are updated to document these additions, and four new tests verify that the fields are present and correctly set to the job's priority and an empty reasons list for jobs with default priority.
This diff exposes two new fields—`effective_priority` and `priority_reasons`—in the hyqs MCP API responses for `get_job` and `list_jobs` endpoints (both full and summary modes). The docstrings are updated to document these additions, and four new tests verify that the fields are present and correctly set to the job's priority and an empty reasons list for jobs with default priority.

## Files touched
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
