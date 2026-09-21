# Job #3170: MCP regression tests for scheduler_wait parity and isolation

**Date:** 2026-08-02

This diff adds comprehensive test coverage for scheduler wait reporting in the MCP server's job endpoints. It introduces seven test functions that validate scheduler_wait behavior across different blocking scenarios: file path conflicts between jobs, schema and merge lock contention, provider capacity exhaustion, and project slot occupation. The tests also verify that explicit job dependencies suppress scheduler_wait reporting, that wait status clears when locks are released, and critically, that blocking job IDs never leak across project boundaries. This ensures the job API properly communicates to clients why their jobs are delayed and by which specific blocking jobs, while maintaining project isolation.
This diff adds comprehensive test coverage for scheduler wait reporting in the MCP server's job endpoints. It introduces seven test functions that validate scheduler_wait behavior across different blocking scenarios: file path conflicts between jobs, schema and merge lock contention, provider capacity exhaustion, and project slot occupation. The tests also verify that explicit job dependencies suppress scheduler_wait reporting, that wait status clears when locks are released, and critically, that blocking job IDs never leak across project boundaries. This ensures the job API properly communicates to clients why their jobs are delayed and by which specific blocking jobs, while maintaining project isolation.

## Files touched
- tests/test_mcp_server.py
