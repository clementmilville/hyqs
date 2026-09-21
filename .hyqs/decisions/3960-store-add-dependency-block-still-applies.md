# Job #3960: Store: add dependency_block_still_applies

**Date:** 2026-08-17

This diff adds a new method `dependency_block_still_applies()` to `JobStore` that checks whether a job is still genuinely blocked by a specific dependency recorded in its failure details. The method returns true if no dependency was recorded or if that dependency remains unsatisfied, and false once the dependency completes, gets manually resolved, or the edge is rewired to a different dependency. Four comprehensive test cases cover all scenarios: missing dependency IDs, active blocks, completed dependencies, and rewired edges.
This diff adds a new method `dependency_block_still_applies()` to `JobStore` that checks whether a job is still genuinely blocked by a specific dependency recorded in its failure details. The method returns true if no dependency was recorded or if that dependency remains unsatisfied, and false once the dependency completes, gets manually resolved, or the edge is rewired to a different dependency. Four comprehensive test cases cover all scenarios: missing dependency IDs, active blocks, completed dependencies, and rewired edges.

## Files touched
- hyqs/pipeline/store.py
- tests/test_store.py
