# Job #3234: _perf_filters + stage stats / slowest jobs / headline stats

**Date:** 2026-08-02

Performance reporting methods now exclude auto-deploy operational jobs by default. Three methods—`performance_stage_stats()`, `performance_slowest_jobs()`, and `performance_headline_stats()`—gain an `include_operational` parameter (defaults to False) that filters out jobs with `source_actor="auto-deploy"` from their queries. The headline stats endpoint additionally returns `excludes_operational` and `operational_jobs_excluded` keys to let callers know how many jobs were filtered out. Tests confirm the filtering works correctly across all three methods and that the response shape is consistent even when no jobs match the filters.
Performance reporting methods now exclude auto-deploy operational jobs by default. Three methods—`performance_stage_stats()`, `performance_slowest_jobs()`, and `performance_headline_stats()`—gain an `include_operational` parameter (defaults to False) that filters out jobs with `source_actor="auto-deploy"` from their queries. The headline stats endpoint additionally returns `excludes_operational` and `operational_jobs_excluded` keys to let callers know how many jobs were filtered out. Tests confirm the filtering works correctly across all three methods and that the response shape is consistent even when no jobs match the filters.

## Files touched
- hyqs/pipeline/store.py
- tests/test_store.py
