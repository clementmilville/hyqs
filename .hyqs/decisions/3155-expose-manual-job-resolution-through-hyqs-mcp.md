# Job #3155: Expose manual job resolution through Hyqs MCP

**Date:** 2026-08-02

This diff introduces a new job resolution feature for both REST and MCP endpoints that allows marking terminal jobs (FAILED or CANCELLED) as manually resolved. The shared validation logic is extracted into a `_resolve_job_error()` helper function that both endpoints call, ensuring identical eligibility checking and preventing future drift between them. The REST endpoint now accepts an optional `resolution` field in the request body instead of hardcoding "resolved", while the MCP server gains a new `resolve_job()` tool with the same parameter. Comprehensive test coverage ensures both entry points agree on eligibility across various combinations of job status and resolution values.
This diff introduces a new job resolution feature for both REST and MCP endpoints that allows marking terminal jobs (FAILED or CANCELLED) as manually resolved. The shared validation logic is extracted into a `_resolve_job_error()` helper function that both endpoints call, ensuring identical eligibility checking and preventing future drift between them. The REST endpoint now accepts an optional `resolution` field in the request body instead of hardcoding "resolved", while the MCP server gains a new `resolve_job()` tool with the same parameter. Comprehensive test coverage ensures both entry points agree on eligibility across various combinations of job status and resolution values.

## Files touched
- hyqs/web/app.py
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
