# Job #4254: Endpoint regression tests for GET /api/admin/page-views

**Date:** 2026-09-06

This diff adds a comprehensive regression test suite for the GET /api/admin/page-views endpoint, testing permission enforcement, per-day unique visitor summation, date range and path filtering, and verification that visitor_hash is excluded from API responses. The tests use Starlette's TestClient against a real Postgres database (following the project's convention of never mocking the database), with helper functions for seating test data and managing authenticated users with varying permission levels. The suite ensures the endpoint correctly gates access via the view_audit permission, aggregates visitor counts per day without deduplicating across days, and respects time-window and path filters while protecting sensitive visitor identity information.
This diff adds a comprehensive regression test suite for the GET /api/admin/page-views endpoint, testing permission enforcement, per-day unique visitor summation, date range and path filtering, and verification that visitor_hash is excluded from API responses. The tests use Starlette's TestClient against a real Postgres database (following the project's convention of never mocking the database), with helper functions for seating test data and managing authenticated users with varying permission levels. The suite ensures the endpoint correctly gates access via the view_audit permission, aggregates visitor counts per day without deduplicating across days, and respects time-window and path filters while protecting sensitive visitor identity information. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- tests/test_page_views_admin.py
