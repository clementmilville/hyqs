# Job #2972: Detect a shifting failure signature to distinguish flaky from fixable retries

**Date:** 2026-08-01

This diff adds intelligent retry escalation for pipeline jobs that experience consecutive failures of different types. It introduces a `failure_signature()` function that compares only stable failure classifications (failed step, failure code, and gate identifiers) while ignoring volatile diagnostic text, allowing the system to detect when two failures are fundamentally unrelated. When a job fails more than once, the runner now checks if consecutive failures have different signatures and escalates to human review instead of continuing to retry, preventing wasted attempts on unrelated errors. The first failure always retries normally since there's no predecessor to compare against.
This diff adds intelligent retry escalation for pipeline jobs that experience consecutive failures of different types. It introduces a `failure_signature()` function that compares only stable failure classifications (failed step, failure code, and gate identifiers) while ignoring volatile diagnostic text, allowing the system to detect when two failures are fundamentally unrelated. When a job fails more than once, the runner now checks if consecutive failures have different signatures and escalates to human review instead of continuing to retry, preventing wasted attempts on unrelated errors. The first failure always retries normally since there's no predecessor to compare against.

## Files touched
- hyqs/pipeline/models.py
- hyqs/pipeline/runner.py
- tests/test_pipeline_runner.py
