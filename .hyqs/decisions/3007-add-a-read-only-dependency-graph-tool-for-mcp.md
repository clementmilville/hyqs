# Job #3007: Add a read-only dependency graph tool for MCP

**Date:** 2026-08-01

The diff simplifies the docstrings for the job dependency graph functions, replacing verbose multi-line explanations with concise one-liners. It adds two test cases that verify the dependency graph's core behavior: that it preserves edges to completed upstream jobs (unlike the `waiting_on` field which filters to only unsatisfied dependencies), and that it correctly returns empty lists for jobs with no dependencies. The changes essentially document the gap between two concepts — the full historical dependency graph and the real-time set of blocking tasks — through test-driven documentation.
The diff simplifies the docstrings for the job dependency graph functions, replacing verbose multi-line explanations with concise one-liners. It adds two test cases that verify the dependency graph's core behavior: that it preserves edges to completed upstream jobs (unlike the `waiting_on` field which filters to only unsatisfied dependencies), and that it correctly returns empty lists for jobs with no dependencies. The changes essentially document the gap between two concepts — the full historical dependency graph and the real-time set of blocking tasks — through test-driven documentation.

## Files touched
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
