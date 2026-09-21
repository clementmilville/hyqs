# Job #3966: Real-store regression: archive-then-rewire chain recovers without manual retry

**Date:** 2026-08-17

The `_run_janitor_scan()` helper now returns the notification mock, allowing tests to inspect what alerts the scan emitted. The existing test is expanded to verify that archiving a dependency triggers a throttled alert with the correct event type and reason, then confirms the cooldown prevents duplicate alerts on a second pass. A new test validates that a chain of archived dependencies (A→B→C→D) recovers correctly when the root is replaced and satisfied dependents automatically cascade through QUEUED→DONE without manual intervention.
The `_run_janitor_scan()` helper now returns the notification mock, allowing tests to inspect what alerts the scan emitted. The existing test is expanded to verify that archiving a dependency triggers a throttled alert with the correct event type and reason, then confirms the cooldown prevents duplicate alerts on a second pass. A new test validates that a chain of archived dependencies (A→B→C→D) recovers correctly when the root is replaced and satisfied dependents automatically cascade through QUEUED→DONE without manual intervention.

## Files touched
- tests/test_dependency_cascade.py
