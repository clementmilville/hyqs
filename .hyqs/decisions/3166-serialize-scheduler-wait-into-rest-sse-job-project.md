# Job #3166: Serialize scheduler_wait into REST/SSE job projections

**Date:** 2026-08-02

This diff adds scheduler wait diagnostics to job API responses, letting clients see why a job is blocked (e.g., file conflicts, provider capacity, schema locks). It introduces a new `scheduler_wait` field to `job_to_dict` and `job_to_summary_dict`, then refactors ten+ call sites to use a new `job_projection_extras` helper that fetches both unsatisfied dependencies and scheduler wait state together, eliminating repeated calls to `store.get_unsatisfied_deps()`. Tests verify the field serializes correctly when present and defaults to null.
This diff adds scheduler wait diagnostics to job API responses, letting clients see why a job is blocked (e.g., file conflicts, provider capacity, schema locks). It introduces a new `scheduler_wait` field to `job_to_dict` and `job_to_summary_dict`, then refactors ten+ call sites to use a new `job_projection_extras` helper that fetches both unsatisfied dependencies and scheduler wait state together, eliminating repeated calls to `store.get_unsatisfied_deps()`. Tests verify the field serializes correctly when present and defaults to null.

## Files touched
- hyqs/web/app.py
- tests/test_job_detail_endpoint.py
- tests/test_job_list_endpoint.py
- tests/test_job_resolution_endpoints.py
