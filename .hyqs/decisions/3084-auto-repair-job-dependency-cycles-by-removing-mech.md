# Job #3084: Auto-repair job dependency cycles by removing mechanically-added edges

**Date:** 2026-08-02

This diff implements automatic repair of dependency cycles in the job pipeline by removing single provenance-safe edges—specifically edges from remediation jobs to their own incidents or edges derived from a job's queue-survey metadata. When a repairable edge is found, the system removes it, rebuilds the entire dependency graph, and rescans for the next repairable cycle, repeating until the 25-repair-per-scan budget is exhausted. Cycles with no proven-safe edges to remove continue to trigger manual alerts as before, maintaining a fail-closed posture where only edges explicitly recorded in job metadata are candidates for automatic removal.
This diff implements automatic repair of dependency cycles in the job pipeline by removing single provenance-safe edges—specifically edges from remediation jobs to their own incidents or edges derived from a job's queue-survey metadata. When a repairable edge is found, the system removes it, rebuilds the entire dependency graph, and rescans for the next repairable cycle, repeating until the 25-repair-per-scan budget is exhausted. Cycles with no proven-safe edges to remove continue to trigger manual alerts as before, maintaining a fail-closed posture where only edges explicitly recorded in job metadata are candidates for automatic removal.

## Files touched
- hyqs/pipeline/supervisor.py
- tests/test_pipeline_notifier.py
