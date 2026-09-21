# Job #3957: GitHub API transients (503, base-branch-modified) are classified as unknown and dead-end instead of retrying

**Date:** 2026-08-17

This diff hardens merge-stage failure recovery to distinguish GitHub API transients (5xx errors, rate limiting, abuse detection) and merge-race conditions from genuine merge conflicts. The merge.py stage now recognizes "base branch was modified" as a phantom conflict and reroutes it through the worktree-recreate/rebase recovery path instead of failing immediately. The supervisor.py janitor gains a new `remediate_merge_github_transient()` function that backs off and requeues transient failures exponentially (30s–600s) up to a dead-letter cap, decoupling them from the generic transient retry budget so GitHub outages don't starve other types of transients. Structured `github_evidence` is now threaded through `failure_detail` so the classifier can detect transients from the evidence text rather than fragile prose parsing, and tests validate the recovery paths and precedence rules.
This diff hardens merge-stage failure recovery to distinguish GitHub API transients (5xx errors, rate limiting, abuse detection) and merge-race conditions from genuine merge conflicts. The merge.py stage now recognizes "base branch was modified" as a phantom conflict and reroutes it through the worktree-recreate/rebase recovery path instead of failing immediately. The supervisor.py janitor gains a new `remediate_merge_github_transient()` function that backs off and requeues transient failures exponentially (30s–600s) up to a dead-letter cap, decoupling them from the generic transient retry budget so GitHub outages don't starve other types of transients. Structured `github_evidence` is now threaded through `failure_detail` so the classifier can detect transients from the evidence text rather than fragile prose parsing, and tests validate the recovery paths and precedence rules.

## Files touched
- hyqs/pipeline/stages/merge.py
- hyqs/pipeline/supervisor.py
- tests/test_self_healing_hardening.py
- tests/test_stages_merge_phantom_conflict.py
- tests/test_supervisor.py
