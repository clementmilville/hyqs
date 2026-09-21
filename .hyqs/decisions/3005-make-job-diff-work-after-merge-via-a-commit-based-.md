# Job #3005: Make job_diff work after merge via a commit-based fallback

**Date:** 2026-08-01

This change exposes `merge_delta_sha` and `deployed_commit` fields in job responses, allowing clients to track what commit was deployed. It extracts diff-fetching logic into a reusable `_job_diff_payload` function that provides graceful fallback: when a PR branch is deleted at merge, the endpoint now shows the diff of the landed commit instead of failing. Both the HTTP diff endpoint and MCP tool are refactored to use this shared function, which also replaces raw git errors with structured error messages. This enables diffs to remain accessible even after branches are cleaned up by squash-merge workflows.
This change exposes `merge_delta_sha` and `deployed_commit` fields in job responses, allowing clients to track what commit was deployed. It extracts diff-fetching logic into a reusable `_job_diff_payload` function that provides graceful fallback: when a PR branch is deleted at merge, the endpoint now shows the diff of the landed commit instead of failing. Both the HTTP diff endpoint and MCP tool are refactored to use this shared function, which also replaces raw git errors with structured error messages. This enables diffs to remain accessible even after branches are cleaned up by squash-merge workflows.

## Files touched
- hyqs/web/app.py
- hyqs/web/mcp_server.py
- tests/test_mcp_job_tools.py
- tests/test_mcp_server.py
