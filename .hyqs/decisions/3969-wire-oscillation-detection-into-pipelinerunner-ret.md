# Job #3969: Wire oscillation detection into PipelineRunner._retry_or_fail

**Date:** 2026-08-17

This diff adds detection for when the review and security gates become deadlocked, alternating between two mutually exclusive verdicts rather than converging—for example, one requiring stricter input validation and the other requiring looser validation. When such oscillation is detected on the fourth consecutive failure, the job halts with a `gate_conflict` status and escalates to human review instead of burning the fix-attempt budget. The implementation fingerprints each gate's findings to compare against historical patterns, and includes helper functions to render the conflicting verdicts for the human reviewer. Tests verify that genuinely diverging findings (not oscillation) still retry normally, and that non-gate failures don't get caught by this logic.
This diff adds detection for when the review and security gates become deadlocked, alternating between two mutually exclusive verdicts rather than converging—for example, one requiring stricter input validation and the other requiring looser validation. When such oscillation is detected on the fourth consecutive failure, the job halts with a `gate_conflict` status and escalates to human review instead of burning the fix-attempt budget. The implementation fingerprints each gate's findings to compare against historical patterns, and includes helper functions to render the conflicting verdicts for the human reviewer. Tests verify that genuinely diverging findings (not oscillation) still retry normally, and that non-gate failures don't get caught by this logic.

## Files touched
- hyqs/pipeline/models.py
- hyqs/pipeline/runner.py
- tests/test_pipeline_runner.py
