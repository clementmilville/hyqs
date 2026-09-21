# Job #3227: Frontend API client for the new deployment-reliability endpoint

**Date:** 2026-08-02

Adds a new `getDeploymentReliability()` API client function that queries a deployment reliability endpoint with an optional `since` parameter to filter results by date, returning the breakdown data from the response. The function follows the same pattern as existing breakdown queries like `getJobsFiledByBreakdown()`. Test coverage includes both the parameterless case and when a `since` date is provided. A minor formatting change collapses a multiline EventSource constructor onto a single line.
Adds a new `getDeploymentReliability()` API client function that queries a deployment reliability endpoint with an optional `since` parameter to filter results by date, returning the breakdown data from the response. The function follows the same pattern as existing breakdown queries like `getJobsFiledByBreakdown()`. Test coverage includes both the parameterless case and when a `since` date is provided. A minor formatting change collapses a multiline EventSource constructor onto a single line.

## Files touched
- hyqs/web/frontend/src/api.js
- hyqs/web/frontend/src/api.test.js
