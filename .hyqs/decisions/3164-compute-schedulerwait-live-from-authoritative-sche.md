# Job #3164: Compute SchedulerWait live from authoritative scheduler state

**Date:** 2026-08-02

Refactors the manifest-conflict check to also return which specific files overlap (not just a boolean), enabling more precise diagnostics. Adds a new `get_scheduler_wait()` method that performs read-only checks to explain why a pending job isn't running — examining schema/merge locks, file scope overlaps, provider capacity, and per-project concurrency limits in priority order. Includes comprehensive test coverage for all blocking conditions and their precedence rules.
Refactors the manifest-conflict check to also return which specific files overlap (not just a boolean), enabling more precise diagnostics. Adds a new `get_scheduler_wait()` method that performs read-only checks to explain why a pending job isn't running — examining schema/merge locks, file scope overlaps, provider capacity, and per-project concurrency limits in priority order. Includes comprehensive test coverage for all blocking conditions and their precedence rules.

## Files touched
- hyqs/pipeline/store.py
- tests/test_store.py
