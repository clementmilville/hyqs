# Job #3064: [ai-fix] Add get_job_dependency_graph MCP tool with complete test scope. In hyqs

**Date:** 2026-08-01

Adds a new MCP tool `get_job_dependency_graph` that returns a job's complete dependency graph as both upstream dependencies and downstream dependents, including edges to already-satisfied jobs. Unlike existing unsatisfied-dependency queries that filter to non-terminal upstreams only, this read-only supplement exposes the full graph structure with job id, title, and status for each edge. The implementation enforces project-membership authorization and includes comprehensive test coverage for linear chains, diamond patterns, authorization checks, and edge cases.
Adds a new MCP tool `get_job_dependency_graph` that returns a job's complete dependency graph as both upstream dependencies and downstream dependents, including edges to already-satisfied jobs. Unlike existing unsatisfied-dependency queries that filter to non-terminal upstreams only, this read-only supplement exposes the full graph structure with job id, title, and status for each edge. The implementation enforces project-membership authorization and includes comprehensive test coverage for linear chains, diamond patterns, authorization checks, and edge cases.

## Files touched
- hyqs/web/mcp_server.py
- tests/test_mcp_job_tools.py
- tests/test_mcp_server.py
