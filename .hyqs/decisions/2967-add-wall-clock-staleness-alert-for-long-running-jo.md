# Job #2967: Add wall-clock staleness alert for long-running jobs

**Date:** 2026-08-01

This diff adds a stale job detection feature that alerts when pipeline jobs have been active longer than a configured threshold (default 24 hours). The feature introduces a new configurable parameter `pipeline_job_stale_hours` (settable via `HYQS_PIPELINE_JOB_STALE_HOURS` environment variable) and implements throttled notifications—at most every 12 hours per job—to prevent alert fatigue. The `_check_job_staleness` function is integrated into the janitor scan loop to periodically check all active jobs and notify with relevant context (job title, epic ID, time active). Comprehensive test coverage validates config defaults/env parsing, notification logic, age calculations, and integration with the janitor scan.
This diff adds a stale job detection feature that alerts when pipeline jobs have been active longer than a configured threshold (default 24 hours). The feature introduces a new configurable parameter `pipeline_job_stale_hours` (settable via `HYQS_PIPELINE_JOB_STALE_HOURS` environment variable) and implements throttled notifications—at most every 12 hours per job—to prevent alert fatigue. The `_check_job_staleness` function is integrated into the janitor scan loop to periodically check all active jobs and notify with relevant context (job title, epic ID, time active). Comprehensive test coverage validates config defaults/env parsing, notification logic, age calculations, and integration with the janitor scan.

## Files touched
- hyqs/config.py
- hyqs/pipeline/supervisor.py
- tests/test_pipeline_notifier.py
