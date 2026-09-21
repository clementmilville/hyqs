# Job #4311: Make POST /api/projects create-only and constrain repo_path

**Date:** 2026-09-08

This diff adds atomic project creation to prevent duplicate registrations by the same repository path. It introduces `create_project_exclusive()` in the store, which uses SQL's `ON CONFLICT DO NOTHING` to atomically insert or return `None` if the repo path already exists. The web route now validates that repos must live inside the projects directory before attempting creation, calls the new exclusive method, and returns HTTP 409 if a project for that path already exists. The changes include comprehensive tests for both the atomic database operation and the route's validation logic, ensuring requests can't hijack existing projects or escape the projects directory boundary.
This diff adds atomic project creation to prevent duplicate registrations by the same repository path. It introduces `create_project_exclusive()` in the store, which uses SQL's `ON CONFLICT DO NOTHING` to atomically insert or return `None` if the repo path already exists. The web route now validates that repos must live inside the projects directory before attempting creation, calls the new exclusive method, and returns HTTP 409 if a project for that path already exists. The changes include comprehensive tests for both the atomic database operation and the route's validation logic, ensuring requests can't hijack existing projects or escape the projects directory boundary. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/store.py
- hyqs/web/app.py
- tests/test_project_create_route.py
- tests/test_store.py
