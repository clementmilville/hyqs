# Job #3235: performance_trend operational exclusion

**Date:** 2026-08-02

The diff adds an `include_operational` parameter to the `performance_trend` method that controls whether auto-deploy operational jobs are included in daily cost, token, and job-outcome metrics. By default (when `False`), these operational jobs are excluded from both usage and job buckets to provide cleaner performance trends; callers can pass `True` to restore unfiltered per-day figures. The implementation adds filtering logic to both the usage and jobs SQL queries using an exclusion clause defined elsewhere in the codebase. Tests verify that operational jobs are correctly excluded by default, included when requested, and that date-range filtering works correctly in both modes.
The diff adds an `include_operational` parameter to the `performance_trend` method that controls whether auto-deploy operational jobs are included in daily cost, token, and job-outcome metrics. By default (when `False`), these operational jobs are excluded from both usage and job buckets to provide cleaner performance trends; callers can pass `True` to restore unfiltered per-day figures. The implementation adds filtering logic to both the usage and jobs SQL queries using an exclusion clause defined elsewhere in the codebase. Tests verify that operational jobs are correctly excluded by default, included when requested, and that date-range filtering works correctly in both modes.

## Files touched
- hyqs/pipeline/store.py
- tests/test_store.py
