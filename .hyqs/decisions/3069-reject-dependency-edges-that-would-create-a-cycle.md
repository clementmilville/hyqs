# Job #3069: Reject dependency edges that would create a cycle

**Date:** 2026-08-01

This adds cycle detection to `add_job_dependency`, which supervisors use to add reverse-id dependencies that `create_batch` would reject based on id-ordering alone. A new `_dependency_topo_order` method extracts Kahn's algorithm for reuse across both code paths. Critically, dependency inserts are protected by a fleet-wide advisory lock that prevents two concurrent calls with disjoint endpoints from jointly creating a cycle that neither call individually would detect.
This adds cycle detection to `add_job_dependency`, which supervisors use to add reverse-id dependencies that `create_batch` would reject based on id-ordering alone. A new `_dependency_topo_order` method extracts Kahn's algorithm for reuse across both code paths. Critically, dependency inserts are protected by a fleet-wide advisory lock that prevents two concurrent calls with disjoint endpoints from jointly creating a cycle that neither call individually would detect.

## Files touched
- hyqs/pipeline/store.py
- tests/test_store.py
