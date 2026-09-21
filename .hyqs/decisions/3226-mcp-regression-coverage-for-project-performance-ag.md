# Job #3226: MCP regression coverage for project_performance/agent_stats exclusion parity

**Date:** 2026-08-02

This diff adds two end-to-end tests that verify the MCP server correctly filters operational jobs from performance statistics. `test_project_performance_excludes_operational_jobs_end_to_end` creates ordinary and operational jobs (identified by source_actor or legacy title patterns), then confirms that `project_performance` excludes them by default but includes them when `include_operational=True` is passed, checking that metrics like job counts and token totals are correctly filtered. `test_agent_stats_excludes_operational_jobs_end_to_end` does the same for agent statistics, verifying that costs and durations are properly filtered based on the operational-job flag. Both tests complement earlier mock-based tests by validating the complete filtering behavior end-to-end with real data.
This diff adds two end-to-end tests that verify the MCP server correctly filters operational jobs from performance statistics. `test_project_performance_excludes_operational_jobs_end_to_end` creates ordinary and operational jobs (identified by source_actor or legacy title patterns), then confirms that `project_performance` excludes them by default but includes them when `include_operational=True` is passed, checking that metrics like job counts and token totals are correctly filtered. `test_agent_stats_excludes_operational_jobs_end_to_end` does the same for agent statistics, verifying that costs and durations are properly filtered based on the operational-job flag. Both tests complement earlier mock-based tests by validating the complete filtering behavior end-to-end with real data.

## Files touched
- tests/test_mcp_server.py
