# Job #3205: get_effective_priority + claim-order integration + inherited priority for store-

**Date:** 2026-08-02

The job scheduling system now implements boost-aware prioritization, fetching a bounded candidate set from the database and re-sorting by effective priority (which layers on remediation, critical-path, and aging boosts) before selecting the next job to claim. The changes affect both the main `claimable()` method and the project-specific `claim()` path, using new helper methods `_effective_priority_conn()` and `get_effective_priority()` that compute these boosts by walking the job dependency tree. Additionally, supervisor-created remediation jobs and security baseline remediation jobs now inherit their triggering job's priority instead of using a hardcoded value, ensuring priority consistency across remediation chains. Tests verify that the boost system applies correctly, respects terminal job states, and doesn't interfere with manifest conflict detection.
The job scheduling system now implements boost-aware prioritization, fetching a bounded candidate set from the database and re-sorting by effective priority (which layers on remediation, critical-path, and aging boosts) before selecting the next job to claim. The changes affect both the main `claimable()` method and the project-specific `claim()` path, using new helper methods `_effective_priority_conn()` and `get_effective_priority()` that compute these boosts by walking the job dependency tree. Additionally, supervisor-created remediation jobs and security baseline remediation jobs now inherit their triggering job's priority instead of using a hardcoded value, ensuring priority consistency across remediation chains. Tests verify that the boost system applies correctly, respects terminal job states, and doesn't interfere with manifest conflict detection.

## Files touched
- hyqs/pipeline/store.py
- tests/test_store.py
