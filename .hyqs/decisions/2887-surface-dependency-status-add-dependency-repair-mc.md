# Job #2887: Surface dependency status + add dependency-repair MCP tools

**Date:** 2026-07-31

This diff exposes job dependency management through the MCP server, adding `add_job_dependency` and `remove_job_dependency` tools that mirror the existing REST handlers with permission checks. The `get_job` and `list_jobs` responses now include a `waiting_on` field showing unsatisfied dependencies for each job. Comprehensive tests verify permission enforcement (contributors can edit, viewers and non-members cannot) and correct dependency tracking across success and error paths.
This diff exposes job dependency management through the MCP server, adding `add_job_dependency` and `remove_job_dependency` tools that mirror the existing REST handlers with permission checks. The `get_job` and `list_jobs` responses now include a `waiting_on` field showing unsatisfied dependencies for each job. Comprehensive tests verify permission enforcement (contributors can edit, viewers and non-members cannot) and correct dependency tracking across success and error paths.

## Files touched
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
