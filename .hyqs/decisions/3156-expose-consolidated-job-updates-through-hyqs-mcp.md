# Job #3156: Expose consolidated job updates through Hyqs MCP

**Date:** 2026-08-02

This diff adds an atomic multi-field job-update capability to both REST and MCP APIs. It extracts job-field validation (title, idea, priority, epic) into shared helper functions in `app.py`, then uses them in a new `update_job` MCP tool and a new `JobStore.update_job_fields()` method that performs all updates in a single database transaction. The approach ensures validation parity between REST and MCP entry points while enabling clients to update multiple fields atomically without making separate requests.
This diff adds an atomic multi-field job-update capability to both REST and MCP APIs. It extracts job-field validation (title, idea, priority, epic) into shared helper functions in `app.py`, then uses them in a new `update_job` MCP tool and a new `JobStore.update_job_fields()` method that performs all updates in a single database transaction. The approach ensures validation parity between REST and MCP entry points while enabling clients to update multiple fields atomically without making separate requests.

## Files touched
- hyqs/pipeline/store.py
- hyqs/web/app.py
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
- tests/test_store.py
