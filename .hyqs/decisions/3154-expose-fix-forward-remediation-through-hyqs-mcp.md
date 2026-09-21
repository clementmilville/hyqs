# Job #3154: Expose fix-forward remediation through Hyqs MCP

**Date:** 2026-08-02

This diff extracts the active-remediation-conflict check into a shared helper function and uses it to implement a new MCP tool `fix_forward_job` alongside the existing REST endpoint, ensuring both entry points enforce identical validation logic. The helper determines whether a job can be fixed forward by checking if an override flag is set or if there's no non-terminal active remediation already in flight. A comprehensive test matrix verifies that the REST and MCP implementations agree on conflict detection across all scenarios (with/without active remediations, various terminal states, and override behavior).
This diff extracts the active-remediation-conflict check into a shared helper function and uses it to implement a new MCP tool `fix_forward_job` alongside the existing REST endpoint, ensuring both entry points enforce identical validation logic. The helper determines whether a job can be fixed forward by checking if an override flag is set or if there's no non-terminal active remediation already in flight. A comprehensive test matrix verifies that the REST and MCP implementations agree on conflict detection across all scenarios (with/without active remediations, various terminal states, and override behavior).

## Files touched
- hyqs/web/app.py
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
