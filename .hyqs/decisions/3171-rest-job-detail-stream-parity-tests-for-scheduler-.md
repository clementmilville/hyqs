# Job #3171: REST job-detail/stream parity tests for scheduler_wait

**Date:** 2026-08-02

This diff adds a `waiting_on` field to job detail responses (both REST and streaming endpoints) that reports which job IDs a pending job is waiting to complete before it can run, distinguishing this from `scheduler_wait` which now only appears when the job is blocked by scheduler constraints like capacity or file conflicts. The test suite is refactored to parameterize scenarios and verify that both fields are correctly projected in the API response, ensuring clients can differentiate between dependency-based and scheduler-based blocking reasons.
This diff adds a `waiting_on` field to job detail responses (both REST and streaming endpoints) that reports which job IDs a pending job is waiting to complete before it can run, distinguishing this from `scheduler_wait` which now only appears when the job is blocked by scheduler constraints like capacity or file conflicts. The test suite is refactored to parameterize scenarios and verify that both fields are correctly projected in the API response, ensuring clients can differentiate between dependency-based and scheduler-based blocking reasons.

## Files touched
- tests/test_job_detail_endpoint.py
