# Job #3002: Expose supervisor remediation events over MCP

**Date:** 2026-08-01

This diff adds a new MCP tool `job_supervisor_events` that exposes the remediation timeline for a job—tracking actions like requeue, classify, gate-fix, and deploy-fix performed by the janitor supervisor. The tool enforces authorization by requiring the caller to be a project member and returns appropriate error messages for missing jobs or unauthorized access. Three tests cover the happy path (member retrieves events), permission denial (non-member is forbidden), and error handling (unknown job).
This diff adds a new MCP tool `job_supervisor_events` that exposes the remediation timeline for a job—tracking actions like requeue, classify, gate-fix, and deploy-fix performed by the janitor supervisor. The tool enforces authorization by requiring the caller to be a project member and returns appropriate error messages for missing jobs or unauthorized access. Three tests cover the happy path (member retrieves events), permission denial (non-member is forbidden), and error handling (unknown job).

## Files touched
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
