# Job #3168: Wire scheduler_wait into MCP job tools

**Date:** 2026-08-02

This diff refactors job projection logic by extracting `waiting_on` and `scheduler_wait` computation into a new `job_projection_extras` helper function, replacing scattered manual calls to `store.get_unsatisfied_deps()` across all MCP endpoints. The change adds `scheduler_wait` to job summaries and full projections in list and get operations, with docstring updates reflecting the new field, and includes tests verifying that scheduler wait data flows through both queries and mutations while being properly gated on access control.
This diff refactors job projection logic by extracting `waiting_on` and `scheduler_wait` computation into a new `job_projection_extras` helper function, replacing scattered manual calls to `store.get_unsatisfied_deps()` across all MCP endpoints. The change adds `scheduler_wait` to job summaries and full projections in list and get operations, with docstring updates reflecting the new field, and includes tests verifying that scheduler wait data flows through both queries and mutations while being properly gated on access control.

## Files touched
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
