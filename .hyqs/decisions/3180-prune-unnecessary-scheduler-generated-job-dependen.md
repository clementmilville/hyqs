# Job #3180: Prune unnecessary scheduler-generated job dependencies

**Date:** 2026-08-02

The diff adds a new helper function `repoint_dependency_provenance` that fans out a cancelled split parent's dependents onto its split children with deterministic ordering. The `repoint_split_dependents` method is enhanced to validate the resulting dependency graph remains acyclic before making any mutations, raising `ValueError` if a cycle would form—mirroring the safety checks in `add_job_dependency`. It also immediately reconciles auto dependencies within the same transaction to remove edges with known-disjoint scopes rather than waiting for the periodic sweep. Tests verify the helper function's behavior and confirm that the method now detects cycles, rejects them without modifying state, and removes redundant auto edges post-repointing.
The diff adds a new helper function `repoint_dependency_provenance` that fans out a cancelled split parent's dependents onto its split children with deterministic ordering. The `repoint_split_dependents` method is enhanced to validate the resulting dependency graph remains acyclic before making any mutations, raising `ValueError` if a cycle would form—mirroring the safety checks in `add_job_dependency`. It also immediately reconciles auto dependencies within the same transaction to remove edges with known-disjoint scopes rather than waiting for the periodic sweep. Tests verify the helper function's behavior and confirm that the method now detects cycles, rejects them without modifying state, and removes redundant auto edges post-repointing.

## Files touched
- hyqs/pipeline/collision.py
- hyqs/pipeline/store.py
- tests/test_collision.py
- tests/test_store.py
