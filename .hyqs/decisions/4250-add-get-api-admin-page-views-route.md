# Job #4250: Add GET /api/admin/page-views route

**Date:** 2026-09-06

This diff adds a new admin API endpoint `/api/admin/page-views` that serves page view analytics with optional filtering by time range and path. The endpoint requires the `view_audit` permission and defaults to returning data from the past 30 days if no date range is specified. Three test cases verify that the endpoint properly enforces permissions, returns summary data when authorized, and correctly forwards query parameters to the underlying store method.
This diff adds a new admin API endpoint `/api/admin/page-views` that serves page view analytics with optional filtering by time range and path. The endpoint requires the `view_audit` permission and defaults to returning data from the past 30 days if no date range is specified. Three test cases verify that the endpoint properly enforces permissions, returns summary data when authorized, and correctly forwards query parameters to the underlying store method. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/app.py
- tests/test_web_fleet.py
