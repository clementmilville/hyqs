# Job #4305: Add missing object-level authorization to 8 REST routes (IDOR)

**Date:** 2026-09-08

This commit hardens REST API authorization by adding project-membership checks to eight state-changing routes (`/api/jobs/{id}/unarchive`, `/api/jobs/{id}/priority`, `/api/epics/*`, `/api/projects/{id}/backlog/suggest`, and `/api/jobs/{id}/backlog-sources`) that previously acted on objects without verifying the caller's access to the object's project — allowing cross-project API tokens to mutate data they shouldn't touch. It also rewrites the authorization regression test suite from a manual three-case audit into a comprehensive enumeration that requires every state-changing endpoint to either prove it rejects cross-project callers or land in an explicit allowlist with documented justification, making unguarded routes fail loudly on merge. A minor test fix also excludes SHA-256 digests from device-fingerprint checks to avoid spurious failures on hash byte coincidences.
This commit hardens REST API authorization by adding project-membership checks to eight state-changing routes (`/api/jobs/{id}/unarchive`, `/api/jobs/{id}/priority`, `/api/epics/*`, `/api/projects/{id}/backlog/suggest`, and `/api/jobs/{id}/backlog-sources`) that previously acted on objects without verifying the caller's access to the object's project — allowing cross-project API tokens to mutate data they shouldn't touch. It also rewrites the authorization regression test suite from a manual three-case audit into a comprehensive enumeration that requires every state-changing endpoint to either prove it rejects cross-project callers or land in an explicit allowlist with documented justification, making unguarded routes fail loudly on merge. A minor test fix also excludes SHA-256 digests from device-fingerprint checks to avoid spurious failures on hash byte coincidences. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/app.py
- tests/test_page_views.py
- tests/test_security_authz_manual_fixes.py
