# Job #2969: Add a locked-down driver role for unattended job orchestration

**Date:** 2026-08-01

This diff introduces a new `automation_client` role for unattended external orchestration clients, granting job-level actions (queue, cancel, retry, edit dependencies, resolve) equivalent to contributors but explicitly excluding archive_job and all project-admin permissions. The role is wired through the database schema (as a new migration), backend auth layer, and frontend UI (member assignment, API token creation, permissions matrix), with comprehensive test coverage verifying both the granted and denied permissions. The implementation is additive and idempotent, using `ON CONFLICT DO NOTHING` in the migration to avoid failures on re-runs.
This diff introduces a new `automation_client` role for unattended external orchestration clients, granting job-level actions (queue, cancel, retry, edit dependencies, resolve) equivalent to contributors but explicitly excluding archive_job and all project-admin permissions. The role is wired through the database schema (as a new migration), backend auth layer, and frontend UI (member assignment, API token creation, permissions matrix), with comprehensive test coverage verifying both the granted and denied permissions. The implementation is additive and idempotent, using `ON CONFLICT DO NOTHING` in the migration to avoid failures on re-runs.

## Files touched
- hyqs/pipeline/store.py
- hyqs/web/app.py
- hyqs/web/frontend/src/components/ApiTokensPanel.jsx
- hyqs/web/frontend/src/components/ApiTokensPanel.test.jsx
- hyqs/web/frontend/src/components/MembersPanel.jsx
- hyqs/web/frontend/src/constants.js
- hyqs/web/frontend/src/views/AdminUserDetail.jsx
- hyqs/web/frontend/src/views/PermissionsMatrix.test.jsx
- tests/test_auth.py
- tests/test_store.py
