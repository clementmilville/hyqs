# Job #3083: Detect and alert on job dependency cycles in the janitor scan

**Date:** 2026-08-02

This diff adds automatic detection and alerting for dependency cycles among active jobs in the pipeline supervisor. It implements a cycle-detection algorithm using Kosaraju's method to find strongly connected components in the job dependency graph, then notifies users with details of all jobs in the cycle while limiting notifications to once per cycle per 12 hours to avoid spam. The detection runs as part of the existing janitor scan process and only triggers on cycles within active (non-terminal) jobs. Tests verify the detection correctly identifies cycles of various lengths, ignores non-cyclic patterns like chains and diamonds, and properly deduplicates repeated alerts.
This diff adds automatic detection and alerting for dependency cycles among active jobs in the pipeline supervisor. It implements a cycle-detection algorithm using Kosaraju's method to find strongly connected components in the job dependency graph, then notifies users with details of all jobs in the cycle while limiting notifications to once per cycle per 12 hours to avoid spam. The detection runs as part of the existing janitor scan process and only triggers on cycles within active (non-terminal) jobs. Tests verify the detection correctly identifies cycles of various lengths, ignores non-cyclic patterns like chains and diamonds, and properly deduplicates repeated alerts.

## Files touched
- hyqs/pipeline/supervisor.py
- tests/test_pipeline_notifier.py
