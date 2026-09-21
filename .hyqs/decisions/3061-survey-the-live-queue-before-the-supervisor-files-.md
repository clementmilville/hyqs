# Job #3061: Survey the live queue before the supervisor files a remediation job

**Date:** 2026-08-01

This diff adds support for supervisor-generated fix jobs to depend on existing active jobs in the queue. When the incident analyst creates fix jobs, it now surveys the active job queue to detect overlapping file modifications and automatically chains the new fix jobs behind any conflicting jobs. External job dependencies are tracked via a new `depends_on_job_ids` field in both the remediation creation API and job metadata, ensuring fixes don't run concurrently with queue jobs that touch the same files. Three new test cases verify the dependency logic handles overlapping jobs, unknown scopes, and mixed intra-chain with queue-based dependencies correctly.
This diff adds support for supervisor-generated fix jobs to depend on existing active jobs in the queue. When the incident analyst creates fix jobs, it now surveys the active job queue to detect overlapping file modifications and automatically chains the new fix jobs behind any conflicting jobs. External job dependencies are tracked via a new `depends_on_job_ids` field in both the remediation creation API and job metadata, ensuring fixes don't run concurrently with queue jobs that touch the same files. Three new test cases verify the dependency logic handles overlapping jobs, unknown scopes, and mixed intra-chain with queue-based dependencies correctly.

## Files touched
- hyqs/pipeline/store.py
- hyqs/pipeline/supervisor.py
- tests/test_auto_requeue_after_fix.py
- tests/test_store.py
