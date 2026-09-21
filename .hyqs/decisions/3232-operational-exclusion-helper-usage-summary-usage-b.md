# Job #3232: Operational-exclusion helper + usage_summary/_usage_by_project

**Date:** 2026-08-02

This diff adds filtering to exclude auto-deploy operational jobs from usage summaries by default. A new `_operational_exclusion_clause()` method identifies operational jobs (either via `source_actor = 'auto-deploy'` or legacy `supervisor` + `Auto-deploy:` title patterns), and the `usage_summary()` and `_usage_by_project()` methods now filter these out from token counts, costs, and project rollups unless `include_operational=True` is passed. The changes also track and report how many operational jobs were excluded, dev-session usage is always included regardless of the flag, and comprehensive test coverage validates the filtering behavior across various scenarios.
This diff adds filtering to exclude auto-deploy operational jobs from usage summaries by default. A new `_operational_exclusion_clause()` method identifies operational jobs (either via `source_actor = 'auto-deploy'` or legacy `supervisor` + `Auto-deploy:` title patterns), and the `usage_summary()` and `_usage_by_project()` methods now filter these out from token counts, costs, and project rollups unless `include_operational=True` is passed. The changes also track and report how many operational jobs were excluded, dev-session usage is always included regardless of the flag, and comprehensive test coverage validates the filtering behavior across various scenarios.

## Files touched
- hyqs/pipeline/store.py
- tests/test_store.py
