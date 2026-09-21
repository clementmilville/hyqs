# Job #3988: A hung stage held every worker slot for 8 hours: no fleet-liveness check, no slot reclaim, no alert

**Date:** 2026-08-18

This diff ships a multi-layered fix for job #3988, a fleet wedge where a hung stage's heartbeat kept renewing `status='busy'` indefinitely, blocking slots for ~8 hours invisibly. The root cause (documented in the expanded docstring) is that a network blip can leave `asyncio.wait_for`'s cancellation hanging, never returning from `_advance`, so the heartbeat's `finally` block that should clean up never runs. The fix introduces three complementary safeguards: `reclaim_orphaned_worker_slots()` that idles executor slots whose job is no longer RUNNING/DEPLOYING, `_check_fleet_liveness()` that alerts once per wedge episode when all executors are busy with no job advancement in a threshold window, and cleanup of stale branches against the correct managed-repo path instead of job.repo_path (fixing a requeue failure). It also silences log spam by only warning about judgment-class jobs on their first scan, not every pass—the same spam that had buried the original outage.
This diff ships a multi-layered fix for job #3988, a fleet wedge where a hung stage's heartbeat kept renewing `status='busy'` indefinitely, blocking slots for ~8 hours invisibly. The root cause (documented in the expanded docstring) is that a network blip can leave `asyncio.wait_for`'s cancellation hanging, never returning from `_advance`, so the heartbeat's `finally` block that should clean up never runs. The fix introduces three complementary safeguards: `reclaim_orphaned_worker_slots()` that idles executor slots whose job is no longer RUNNING/DEPLOYING, `_check_fleet_liveness()` that alerts once per wedge episode when all executors are busy with no job advancement in a threshold window, and cleanup of stale branches against the correct managed-repo path instead of job.repo_path (fixing a requeue failure). It also silences log spam by only warning about judgment-class jobs on their first scan, not every pass—the same spam that had buried the original outage.

## Files touched
- hyqs/pipeline/runner.py
- hyqs/pipeline/store.py
- hyqs/pipeline/supervisor.py
- tests/test_pipeline_notifier.py
- tests/test_store.py
- tests/test_supervisor.py
