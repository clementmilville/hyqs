# Job #3973: Close the test_supervisor.py coverage gap flagged by source job #3959's scope-in

**Date:** 2026-08-17

The test replaces a fixture-based job factory with direct `Job` instantiation, eliminating the dependency on `tmp_path` and the helper function `_make_failed_job()`. This makes the test setup more explicit and self-contained, with all relevant fields shown directly in one place rather than scattered across a helper call and subsequent attribute assignments. The test now validates the same behavior — that gate_conflict takes precedence over human_review — but with less indirection and no need for temporary file system resources.
The test replaces a fixture-based job factory with direct `Job` instantiation, eliminating the dependency on `tmp_path` and the helper function `_make_failed_job()`. This makes the test setup more explicit and self-contained, with all relevant fields shown directly in one place rather than scattered across a helper call and subsequent attribute assignments. The test now validates the same behavior — that gate_conflict takes precedence over human_review — but with less indirection and no need for temporary file system resources.

## Files touched
- tests/test_supervisor.py
