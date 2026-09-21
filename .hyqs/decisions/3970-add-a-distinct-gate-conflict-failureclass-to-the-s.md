# Job #3970: Add a distinct gate_conflict FailureClass to the supervisor's deterministic clas

**Date:** 2026-08-17

This diff adds support for a new failure classification called `gate_conflict` to the supervisor's failure categorization system. The new class is added to the `FailureClass` enum and integrated into the classification logic so that jobs with a `gate_conflict` code are classified accordingly, taking precedence over the job's `retry_disposition` setting. A test verifies this precedence by confirming that a job marked as both `gate_conflict` and `human_review` is correctly classified as `gate_conflict`. The documentation is updated to reflect this new class as one requiring notification and manual review.
This diff adds support for a new failure classification called `gate_conflict` to the supervisor's failure categorization system. The new class is added to the `FailureClass` enum and integrated into the classification logic so that jobs with a `gate_conflict` code are classified accordingly, taking precedence over the job's `retry_disposition` setting. A test verifies this precedence by confirming that a job marked as both `gate_conflict` and `human_review` is correctly classified as `gate_conflict`. The documentation is updated to reflect this new class as one requiring notification and manual review.

## Files touched
- hyqs/pipeline/supervisor.py
- tests/test_supervisor.py
