# Job #3053: Regress JobStore.list_jobs_page

**Date:** 2026-08-01

This diff adds three comprehensive test cases for the job listing and pagination functionality. The tests verify that status filters correctly segregate jobs into all expected categories (pending, running, deploying, done, failed, cancelled, archived, plus composite filters "active" and "all"), that pagination traverses the full result set without gaps or duplicates across multiple pages, and that partial pages correctly terminate with a null cursor. Together, these tests ensure the store's `list_jobs_page` method handles filtering, sorting, and pagination correctly.
This diff adds three comprehensive test cases for the job listing and pagination functionality. The tests verify that status filters correctly segregate jobs into all expected categories (pending, running, deploying, done, failed, cancelled, archived, plus composite filters "active" and "all"), that pagination traverses the full result set without gaps or duplicates across multiple pages, and that partial pages correctly terminate with a null cursor. Together, these tests ensure the store's `list_jobs_page` method handles filtering, sorting, and pagination correctly.

## Files touched
- tests/test_store.py
