# Job #3172: REST job-list parity test for scheduler_wait

**Date:** 2026-08-02

The test adds assertions verifying that the `waiting_on` field is an empty list for jobs blocked by scheduler conditions like merge locks. This ensures the API response includes the `waiting_on` field consistently across jobs, distinguishing between direct job dependencies and scheduler-based blocking reasons.
The test adds assertions verifying that the `waiting_on` field is an empty list for jobs blocked by scheduler conditions like merge locks. This ensures the API response includes the `waiting_on` field consistently across jobs, distinguishing between direct job dependencies and scheduler-based blocking reasons.

## Files touched
- tests/test_job_list_endpoint.py
