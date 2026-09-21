# Job #4310: Gate /api/usage and /api/supervisor on a permission

**Date:** 2026-09-08

This diff hardens authorization across the pipeline and web layers. The testing module now isolates production database credentials by forwarding only a pre-scoped, disposable test database URL to job-controlled test subprocesses (addressing a prior security incident). The web API adds permission checks to supervisor and usage endpoints: supervisor endpoints require the `view_fleet` permission, while usage queries require either project membership (for scoped queries) or the `view_audit` permission (for cross-project data). Frontend and backend tests comprehensively validate these access-control rules across different credential types (webhooks, session tokens, API tokens).
This diff hardens authorization across the pipeline and web layers. The testing module now isolates production database credentials by forwarding only a pre-scoped, disposable test database URL to job-controlled test subprocesses (addressing a prior security incident). The web API adds permission checks to supervisor and usage endpoints: supervisor endpoints require the `view_fleet` permission, while usage queries require either project membership (for scoped queries) or the `view_audit` permission (for cross-project data). Frontend and backend tests comprehensively validate these access-control rules across different credential types (webhooks, session tokens, API tokens). Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/testing.py
- hyqs/web/app.py
- hyqs/web/frontend/src/views/AdminUsageCost.test.jsx
- tests/test_testing.py
- tests/test_web_fleet.py
