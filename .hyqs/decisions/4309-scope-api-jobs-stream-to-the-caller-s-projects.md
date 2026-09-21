# Job #4309: Scope /api/jobs/stream to the caller's projects

**Date:** 2026-09-08

The `/api/jobs/stream` endpoint now filters active jobs based on project membership for non-admin users, returning only jobs belonging to projects where the caller is a member. Previously the endpoint listed all active jobs regardless of access. A new test verifies this filtering works correctly and that membership is rechecked on each stream event.
The `/api/jobs/stream` endpoint now filters active jobs based on project membership for non-admin users, returning only jobs belonging to projects where the caller is a member. Previously the endpoint listed all active jobs regardless of access. A new test verifies this filtering works correctly and that membership is rechecked on each stream event. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/app.py
- tests/test_job_list_endpoint.py
