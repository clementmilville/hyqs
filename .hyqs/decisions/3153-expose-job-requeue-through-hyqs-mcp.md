# Job #3153: Expose job requeue through Hyqs MCP

**Date:** 2026-08-02

This diff adds a `requeue_job` feature that allows users to retry failed jobs from a specific stage, exposed through both REST and MCP interfaces. The implementation extracts job validation logic into a shared `_requeue_stage_error()` helper that checks whether a job is eligible for requeuing (must be in FAILED status and stage must be in a whitelist), ensuring both APIs enforce the same rules. A new MCP tool `requeue_job()` mirrors the existing REST endpoint, complete with permission checks and dependency tracking in the response. Comprehensive test coverage verifies authorization, error cases, and validates that the REST and MCP implementations agree on eligibility criteria.
This diff adds a `requeue_job` feature that allows users to retry failed jobs from a specific stage, exposed through both REST and MCP interfaces. The implementation extracts job validation logic into a shared `_requeue_stage_error()` helper that checks whether a job is eligible for requeuing (must be in FAILED status and stage must be in a whitelist), ensuring both APIs enforce the same rules. A new MCP tool `requeue_job()` mirrors the existing REST endpoint, complete with permission checks and dependency tracking in the response. Comprehensive test coverage verifies authorization, error cases, and validates that the REST and MCP implementations agree on eligibility criteria.

## Files touched
- hyqs/web/app.py
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
