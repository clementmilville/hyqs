# Job #3052: Add JobStore.list_jobs_page with SQL-bound cursor+status pagination

**Date:** 2026-08-01

This diff extracts the `TERMINAL_JOB_STATUSES` constant from `agents.py` to `models.py` to make it reusable across the codebase, then updates the import in `agents.py` to reference it from its new location. The `list_jobs_page` method's `status` parameter signature is relaxed from `str` with a default to `str | None` with a default of `None`, and the method body sets `status = status or "active"` to preserve the original behavior; a new test verifies that passing `status=None` explicitly matches the behavior of omitting the parameter entirely.
This diff extracts the `TERMINAL_JOB_STATUSES` constant from `agents.py` to `models.py` to make it reusable across the codebase, then updates the import in `agents.py` to reference it from its new location. The `list_jobs_page` method's `status` parameter signature is relaxed from `str` with a default to `str | None` with a default of `None`, and the method body sets `status = status or "active"` to preserve the original behavior; a new test verifies that passing `status=None` explicitly matches the behavior of omitting the parameter entirely.

## Files touched
- hyqs/pipeline/agents.py
- hyqs/pipeline/models.py
- hyqs/pipeline/store.py
- tests/test_store.py
