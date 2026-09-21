# Job #2891: Add MCP queue-collision survey tool (file overlap only)

**Date:** 2026-07-31

This diff introduces pre-creation collision detection for candidate jobs against a project's active queue. It adds a new `survey_job_queue()` function in the collision module that detects file-path overlaps between candidate jobs and active jobs (across plan declarations, idea descriptions, and granted scopes), and a corresponding MCP tool that exposes this check to project members before submitting jobs. The implementation includes a `QueueSurveyResult` dataclass to return collision findings and a list of jobs with unknown/unextractable scope, along with comprehensive unit and integration tests covering collision detection, auth checks, and edge cases.
This diff introduces pre-creation collision detection for candidate jobs against a project's active queue. It adds a new `survey_job_queue()` function in the collision module that detects file-path overlaps between candidate jobs and active jobs (across plan declarations, idea descriptions, and granted scopes), and a corresponding MCP tool that exposes this check to project members before submitting jobs. The implementation includes a `QueueSurveyResult` dataclass to return collision findings and a list of jobs with unknown/unextractable scope, along with comprehensive unit and integration tests covering collision detection, auth checks, and edge cases.

## Files touched
- hyqs/pipeline/collision.py
- hyqs/pipeline/store.py
- hyqs/web/mcp_server.py
- tests/test_collision.py
- tests/test_mcp_server.py
- tests/test_store.py
