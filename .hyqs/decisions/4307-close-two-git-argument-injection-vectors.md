# Job #4307: Close two git argument-injection vectors

**Date:** 2026-09-08

This diff hardens git command invocations against argument injection attacks by adding option terminators (`--end-of-options` or `--`) before all user-controlled arguments (branch names, refs, URLs) throughout the deploy and gitops modules, preventing git from interpreting dash-prefixed input as option flags. It introduces URL validation for GitHub origins to reject malicious URLs containing spaces, dashes, or non-GitHub schemes before they reach git, and disables git's external protocol handler during clone operations to prevent protocol-based exploits. New tests verify that dash-prefixed refs are safely handled and unsafe origins are rejected before any side effects occur.
This diff hardens git command invocations against argument injection attacks by adding option terminators (`--end-of-options` or `--`) before all user-controlled arguments (branch names, refs, URLs) throughout the deploy and gitops modules, preventing git from interpreting dash-prefixed input as option flags. It introduces URL validation for GitHub origins to reject malicious URLs containing spaces, dashes, or non-GitHub schemes before they reach git, and disables git's external protocol handler during clone operations to prevent protocol-based exploits. New tests verify that dash-prefixed refs are safely handled and unsafe origins are rejected before any side effects occur. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/deploy.py
- hyqs/pipeline/gitops.py
- tests/test_deploy.py
- tests/test_deploy_reconcile.py
- tests/test_worktree_isolation.py
