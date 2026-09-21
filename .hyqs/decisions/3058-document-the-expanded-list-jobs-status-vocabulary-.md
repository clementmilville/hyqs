# Job #3058: Document the expanded list_jobs status vocabulary and cursor param

**Date:** 2026-08-01

The diff adds cursor-based pagination support to the `list_jobs` tool by introducing an optional `cursor` parameter that accepts the ID of the last returned job to fetch the next older page. It also expands the valid `status` values from four options to eight, adding `pending`, `running`, `deploying`, and `cancelled` to provide finer-grained job state tracking. These changes enable clients to paginate through large job lists and better observe job lifecycle stages during execution.
The diff adds cursor-based pagination support to the `list_jobs` tool by introducing an optional `cursor` parameter that accepts the ID of the last returned job to fetch the next older page. It also expands the valid `status` values from four options to eight, adding `pending`, `running`, `deploying`, and `cancelled` to provide finer-grained job state tracking. These changes enable clients to paginate through large job lists and better observe job lifecycle stages during execution.

## Files touched
- docs/mcp.md
