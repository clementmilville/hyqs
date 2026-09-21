# Job #3964: Rewrite the archived-dependent unit test in test_pipeline_loop_fixes.py

**Date:** 2026-08-17

This diff refines the dependency failure-handling logic and adds validation of the alert throttling mechanism. The docstring clarifies that a PENDING job only becomes terminal-failed when its FAILED dependency is dead-lettered (exhausted retries), while archived dependencies trigger alerts instead and leave the dependent PENDING. The test now validates that when an archived dependency is encountered, the system correctly invokes `get_meta` and `set_meta` to throttle repeated alerts using timestamps, ensuring alerts don't spam for the same blocked dependency across multiple reconciliation cycles.
This diff refines the dependency failure-handling logic and adds validation of the alert throttling mechanism. The docstring clarifies that a PENDING job only becomes terminal-failed when its FAILED dependency is dead-lettered (exhausted retries), while archived dependencies trigger alerts instead and leave the dependent PENDING. The test now validates that when an archived dependency is encountered, the system correctly invokes `get_meta` and `set_meta` to throttle repeated alerts using timestamps, ensuring alerts don't spam for the same blocked dependency across multiple reconciliation cycles.

## Files touched
- tests/test_pipeline_loop_fixes.py
