# Job #3236: deployment_reliability_breakdown

**Date:** 2026-08-02

This diff adds a new `deployment_reliability_breakdown()` method to the JobStore that reports per-project reliability metrics for auto-deploy operational jobs, which are intentionally excluded from other usage and performance rollups. The method supports filtering by date range (`since`) and project ID, and returns counts of jobs by terminal status (done, failed, cancelled) along with a computed failure rate. Five comprehensive tests validate that the method correctly isolates operational jobs, computes metrics accurately, and respects the provided filters.
This diff adds a new `deployment_reliability_breakdown()` method to the JobStore that reports per-project reliability metrics for auto-deploy operational jobs, which are intentionally excluded from other usage and performance rollups. The method supports filtering by date range (`since`) and project ID, and returns counts of jobs by terminal status (done, failed, cancelled) along with a computed failure rate. Five comprehensive tests validate that the method correctly isolates operational jobs, computes metrics accurately, and respects the provided filters.

## Files touched
- hyqs/pipeline/store.py
- tests/test_store.py
