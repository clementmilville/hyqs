# Job #3225: REST route regression coverage for include_operational wiring and the new reliab

**Date:** 2026-08-02

This diff adds three new test cases that verify default parameter behavior for three performance API endpoints: `/api/projects/1/performance/stage-stats`, `/api/projects/1/performance/slowest-jobs`, and `/api/projects/1/performance/headline`. Each test confirms that when the `include_operational` parameter is not supplied in the request, the endpoint correctly defaults it to `False` when calling the underlying webhook store methods. These tests ensure that the API maintains a consistent default behavior for filtering operational data across all three performance metric endpoints.
This diff adds three new test cases that verify default parameter behavior for three performance API endpoints: `/api/projects/1/performance/stage-stats`, `/api/projects/1/performance/slowest-jobs`, and `/api/projects/1/performance/headline`. Each test confirms that when the `include_operational` parameter is not supplied in the request, the endpoint correctly defaults it to `False` when calling the underlying webhook store methods. These tests ensure that the API maintains a consistent default behavior for filtering operational data across all three performance metric endpoints.

## Files touched
- tests/test_web_fleet.py
