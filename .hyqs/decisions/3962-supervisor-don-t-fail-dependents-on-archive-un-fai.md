# Job #3962: Supervisor: don't fail dependents on archive; un-fail when the chain is valid ag

**Date:** 2026-08-17

This diff changes how archived dependencies are handled in the job pipeline. Previously, when a dependency was archived, its dependent jobs were automatically marked FAILED with an "archived" failure reason. Now they stay PENDING and receive a throttled alert instead, since a human may restore or re-point the archived dependency later. Additionally, the `remediate_dependency_blocked` function now distinguishes between "the recorded blocker is no longer present" and "there are still unsatisfied dependencies" to provide more accurate requeue messaging — using "chain valid again" when the original dependency block no longer applies, versus "deps satisfied" when other blocking dependencies resolved.
This diff changes how archived dependencies are handled in the job pipeline. Previously, when a dependency was archived, its dependent jobs were automatically marked FAILED with an "archived" failure reason. Now they stay PENDING and receive a throttled alert instead, since a human may restore or re-point the archived dependency later. Additionally, the `remediate_dependency_blocked` function now distinguishes between "the recorded blocker is no longer present" and "there are still unsatisfied dependencies" to provide more accurate requeue messaging — using "chain valid again" when the original dependency block no longer applies, versus "deps satisfied" when other blocking dependencies resolved.

## Files touched
- hyqs/pipeline/supervisor.py
- tests/test_dependency_cascade.py
- tests/test_pipeline_loop_fixes.py
- tests/test_supervisor.py
