# Job #2907: Expose atomic multi-job wave creation as an MCP tool (v2)

**Date:** 2026-07-31

This diff introduces `create_job_wave`, a new MCP tool for atomically creating waves of interdependent jobs with comprehensive validation. It extracts validation and dependency-cycle detection logic from the REST handler into reusable functions (`_detect_batch_dependency_cycle` and `_parse_batch_job_common_fields`), then uses those in both the web app and the new MCP tool to ensure cycles and out-of-range dependency indexes are caught before any jobs are persisted. The store layer gains an additional upfront check to reject invalid `depends_on` indexes, and the implementation is backed by seven new tests covering linear chains, hot-file auto-chaining, permission checks, and validation edge cases.
This diff introduces `create_job_wave`, a new MCP tool for atomically creating waves of interdependent jobs with comprehensive validation. It extracts validation and dependency-cycle detection logic from the REST handler into reusable functions (`_detect_batch_dependency_cycle` and `_parse_batch_job_common_fields`), then uses those in both the web app and the new MCP tool to ensure cycles and out-of-range dependency indexes are caught before any jobs are persisted. The store layer gains an additional upfront check to reject invalid `depends_on` indexes, and the implementation is backed by seven new tests covering linear chains, hot-file auto-chaining, permission checks, and validation edge cases.

## Files touched
- hyqs/pipeline/store.py
- hyqs/web/app.py
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
- tests/test_store.py
