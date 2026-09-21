# Job #3965: Add un-fail regression test to test_auto_requeue_after_fix.py

**Date:** 2026-08-17

This diff adds a test case for the `remediate_dependency_blocked` function that verifies the behavior when a job's original blocking dependency has been resolved. The test confirms that when a job blocked by dependency 570 has that specific blocker cleared (even though other dependencies remain unsatisfied), the job is requeued with zero attempts so it can be re-evaluated against its current dependency graph. This ensures jobs aren't stuck in a blocked state once their specific blocker is resolved, allowing them to retry when appropriate.
This diff adds a test case for the `remediate_dependency_blocked` function that verifies the behavior when a job's original blocking dependency has been resolved. The test confirms that when a job blocked by dependency 570 has that specific blocker cleared (even though other dependencies remain unsatisfied), the job is requeued with zero attempts so it can be re-evaluated against its current dependency graph. This ensures jobs aren't stuck in a blocked state once their specific blocker is resolved, allowing them to retry when appropriate.

## Files touched
- tests/test_auto_requeue_after_fix.py
