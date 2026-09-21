# Job #3162: Add SchedulerWait types and shared lock-owner helpers

**Date:** 2026-08-02

This adds scheduler diagnostics infrastructure: an enum of five wait reasons (slot occupied, file overlap, schema/merge locks, provider capacity), a frozen dataclass to structure diagnostic info including blocking job IDs and conflicting paths with dict serialization, and helper functions to encode/decode job IDs as lock owner strings. Tests verify enum values, serialization, and ID round-tripping with malformed-input guards.
This adds scheduler diagnostics infrastructure: an enum of five wait reasons (slot occupied, file overlap, schema/merge locks, provider capacity), a frozen dataclass to structure diagnostic info including blocking job IDs and conflicting paths with dict serialization, and helper functions to encode/decode job IDs as lock owner strings. Tests verify enum values, serialization, and ID round-tripping with malformed-input guards.

## Files touched
- hyqs/pipeline/models.py
- tests/test_models_scheduler_wait.py
