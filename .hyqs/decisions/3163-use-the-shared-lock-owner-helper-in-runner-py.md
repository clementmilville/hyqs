# Job #3163: Use the shared lock-owner helper in runner.py

**Date:** 2026-08-02

The diff centralizes lock owner ID generation by introducing a `lock_owner_id()` function and replacing two inline string formats (`f"job-{job.id}"`) with calls to it. This extracts the lock naming convention into a single source of truth so that the format can be maintained in one place rather than duplicated across schema lock release calls.
The diff centralizes lock owner ID generation by introducing a `lock_owner_id()` function and replacing two inline string formats (`f"job-{job.id}"`) with calls to it. This extracts the lock naming convention into a single source of truth so that the format can be maintained in one place rather than duplicated across schema lock release calls.

## Files touched
- hyqs/pipeline/runner.py
