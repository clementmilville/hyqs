# Job #3055: Simplify mcp_server.py's list_jobs to use list_jobs_page

**Date:** 2026-08-01

This diff adds cursor-based pagination and expands the job status vocabulary for the `list_jobs` MCP tool. The status filter now accepts nine values (`pending`, `running`, `deploying`, `cancelled` are new; `active`, `done`, `failed`, `archived`, `all` unchanged), and callers can now fetch additional pages by passing the last job ID as a `cursor` parameter to get the next 50 older jobs. The underlying implementation switches from `store.list_active()` to `store.list_jobs_page()`, and the change is backward-compatible—omitting the cursor parameter fetches the first page as before. Tests verify the new statuses work correctly, pagination doesn't overlap pages, and non-positive cursor values are rejected.
This diff adds cursor-based pagination and expands the job status vocabulary for the `list_jobs` MCP tool. The status filter now accepts nine values (`pending`, `running`, `deploying`, `cancelled` are new; `active`, `done`, `failed`, `archived`, `all` unchanged), and callers can now fetch additional pages by passing the last job ID as a `cursor` parameter to get the next 50 older jobs. The underlying implementation switches from `store.list_active()` to `store.list_jobs_page()`, and the change is backward-compatible—omitting the cursor parameter fetches the first page as before. Tests verify the new statuses work correctly, pagination doesn't overlap pages, and non-positive cursor values are rejected.

## Files touched
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
