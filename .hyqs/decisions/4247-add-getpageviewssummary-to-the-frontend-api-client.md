# Job #4247: Add getPageViewsSummary to the frontend API client

**Date:** 2026-09-06

This diff introduces a new `getPageViewsSummary` API client function that fetches page-views analytics data with optional filtering by date range (`since`, `until`) and path. The implementation builds query parameters conditionally and calls `/api/admin/page-views`, following the same pattern as other admin API functions. Comprehensive tests verify that query parameters are correctly formatted, omitted when falsy, and URL-encoded properly, plus error handling for forbidden responses. This enables the admin dashboard to display page-views summaries with flexible filtering options.
This diff introduces a new `getPageViewsSummary` API client function that fetches page-views analytics data with optional filtering by date range (`since`, `until`) and path. The implementation builds query parameters conditionally and calls `/api/admin/page-views`, following the same pattern as other admin API functions. Comprehensive tests verify that query parameters are correctly formatted, omitted when falsy, and URL-encoded properly, plus error handling for forbidden responses. This enables the admin dashboard to display page-views summaries with flexible filtering options. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/frontend/src/api.js
- hyqs/web/frontend/src/api.test.js
