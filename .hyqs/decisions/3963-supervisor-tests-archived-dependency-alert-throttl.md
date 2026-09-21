# Job #3963: Supervisor tests: archived-dependency alert throttling

**Date:** 2026-08-17

The diff splits a single throttling test into two focused tests: one verifying that the first call to `_alert_archived_dependency` sends a notification and records a timestamp, and another verifying that subsequent calls within the cooldown period are no-ops. The refactored first test now uses the helper `_make_jobs()` instead of a bare `MagicMock()` and explicitly asserts the notification message contents, while the new second test isolates the cooldown-suppression logic by starting with a recent timestamp already in place. This separation improves test clarity and makes each test's responsibility obvious.
The diff splits a single throttling test into two focused tests: one verifying that the first call to `_alert_archived_dependency` sends a notification and records a timestamp, and another verifying that subsequent calls within the cooldown period are no-ops. The refactored first test now uses the helper `_make_jobs()` instead of a bare `MagicMock()` and explicitly asserts the notification message contents, while the new second test isolates the cooldown-suppression logic by starting with a recent timestamp already in place. This separation improves test clarity and makes each test's responsibility obvious.

## Files touched
- tests/test_supervisor.py
