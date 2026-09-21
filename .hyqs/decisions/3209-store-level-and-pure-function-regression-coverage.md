# Job #3209: Store-level and pure-function regression coverage

**Date:** 2026-08-02

This diff adds comprehensive test coverage for job scheduling and claiming logic in the pipeline store, specifically targeting critical-path boost calculations, priority aging, and various gates that control when boosted jobs can be claimed. The tests validate that critical-path boosts (driven by dependent job counts and chain depth) are properly capped, that priority aging scales with job age until hitting a cap, and that the system correctly ranks jobs by effective priority while respecting dependencies, host pinning, stage allowlists, and provider availability. These tests address regressions in job #3209 related to boost-versus-gate ranking when multiple priorities and gates are in play.
This diff adds comprehensive test coverage for job scheduling and claiming logic in the pipeline store, specifically targeting critical-path boost calculations, priority aging, and various gates that control when boosted jobs can be claimed. The tests validate that critical-path boosts (driven by dependent job counts and chain depth) are properly capped, that priority aging scales with job age until hitting a cap, and that the system correctly ranks jobs by effective priority while respecting dependencies, host pinning, stage allowlists, and provider availability. These tests address regressions in job #3209 related to boost-versus-gate ranking when multiple priorities and gates are in play.

## Files touched
- tests/test_store.py
