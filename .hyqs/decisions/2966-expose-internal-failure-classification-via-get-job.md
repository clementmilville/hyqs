# Job #2966: Expose internal failure classification via get_job/list_jobs

**Date:** 2026-08-01

This diff surfaces job failure classification metadata through the API by adding five new fields (`failed_step`, `failure_code`, `failure_origin`, `retry_disposition`, `failure_detail`) to job dictionary serialization, with `failure_code` also included in the lightweight summary projection. It implements project-membership filtering for non-admin users when listing unscoped jobs, ensuring they only see jobs from projects they belong to. Documentation is updated to reflect `failure_code` in summary responses, and extensive test coverage validates both the new failure fields and the access-control filtering behavior.
This diff surfaces job failure classification metadata through the API by adding five new fields (`failed_step`, `failure_code`, `failure_origin`, `retry_disposition`, `failure_detail`) to job dictionary serialization, with `failure_code` also included in the lightweight summary projection. It implements project-membership filtering for non-admin users when listing unscoped jobs, ensuring they only see jobs from projects they belong to. Documentation is updated to reflect `failure_code` in summary responses, and extensive test coverage validates both the new failure fields and the access-control filtering behavior.

## Files touched
- hyqs/web/app.py
- hyqs/web/mcp_server.py
- tests/test_job_list_endpoint.py
- tests/test_mcp_server.py
