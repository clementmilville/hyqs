# Job #2970: Add caller-supplied idempotency key to job creation

**Date:** 2026-08-01

The diff adds optional idempotency keys to job creation, allowing callers to safely retry `create_job` and `create_job_wave` across any time window without creating duplicates. When an idempotency key is provided for a project, subsequent calls with the same key return the previously created job instead of creating a new one, enforced by a unique database index on `(project_id, idempotency_key)`. The feature preserves backward compatibility—omitting the key leaves existing content-based deduplication (10-second window) unchanged, and NULL keys never collide. The implementation includes comprehensive test coverage for both single and batch job creation scenarios.
The diff adds optional idempotency keys to job creation, allowing callers to safely retry `create_job` and `create_job_wave` across any time window without creating duplicates. When an idempotency key is provided for a project, subsequent calls with the same key return the previously created job instead of creating a new one, enforced by a unique database index on `(project_id, idempotency_key)`. The feature preserves backward compatibility—omitting the key leaves existing content-based deduplication (10-second window) unchanged, and NULL keys never collide. The implementation includes comprehensive test coverage for both single and batch job creation scenarios.

## Files touched
- hyqs/pipeline/models.py
- hyqs/pipeline/store.py
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
- tests/test_store.py
