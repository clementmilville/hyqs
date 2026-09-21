# Job #3274: [ai-fix] Fix two blocking review gaps in tests/test_store.py for job #3222's ope

**Date:** 2026-08-02

This diff adds two new test cases to validate operational job filtering and edge-case handling in the store's reporting APIs. The first test verifies that the schema prevents NULL values in `source_actor` and `title` columns, then confirms that operational jobs (marked with a non-empty `source_actor` like "auto-deploy") are properly excluded from usage summaries, agent stats, and performance headlines unless `include_operational=True` is passed. The second test covers an edge case where a single pending operational job should contribute to the total job count with a zero failure rate, rather than being omitted from the deployment reliability breakdown entirely.
This diff adds two new test cases to validate operational job filtering and edge-case handling in the store's reporting APIs. The first test verifies that the schema prevents NULL values in `source_actor` and `title` columns, then confirms that operational jobs (marked with a non-empty `source_actor` like "auto-deploy") are properly excluded from usage summaries, agent stats, and performance headlines unless `include_operational=True` is passed. The second test covers an edge case where a single pending operational job should contribute to the total job count with a zero failure rate, rather than being omitted from the deployment reliability breakdown entirely.

## Files touched
- tests/test_store.py
