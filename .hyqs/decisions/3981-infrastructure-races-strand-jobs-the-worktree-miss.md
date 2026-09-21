# Job #3981: Infrastructure races strand jobs: the worktree-missing guard raises an untyped failure, and classified-transient jobs are not being recovered

**Date:** 2026-08-17

The diff adds infrastructure-fault handling for missing or empty worktrees in the pipeline. When testing detects a vanished worktree, it now returns a typed failure with a `[worktree-missing]` marker; the runner's retry logic short-circuits this straight to an infrastructure failure without wasting a FIX attempt, since resuming at the failed stage cannot converge. The supervisor then reroutes `worktree_missing` to BUILD (which recreates it) instead of retrying the failed stage. A security-hardened comment documents how the marker is anchored to prevent malicious test output from forging the signal.
The diff adds infrastructure-fault handling for missing or empty worktrees in the pipeline. When testing detects a vanished worktree, it now returns a typed failure with a `[worktree-missing]` marker; the runner's retry logic short-circuits this straight to an infrastructure failure without wasting a FIX attempt, since resuming at the failed stage cannot converge. The supervisor then reroutes `worktree_missing` to BUILD (which recreates it) instead of retrying the failed stage. A security-hardened comment documents how the marker is anchored to prevent malicious test output from forging the signal.

## Files touched
- hyqs/pipeline/runner.py
- hyqs/pipeline/stages/fix.py
- hyqs/pipeline/supervisor.py
- hyqs/pipeline/testing.py
- tests/test_pipeline_loop_fixes.py
- tests/test_pipeline_runner.py
- tests/test_supervisor.py
- tests/test_testing.py
