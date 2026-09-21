# Job #4334: Show what the intake spec is still missing, and let the user create the project

**Date:** 2026-09-10

This adds a manual "Create project" button to the intake workflow with server-driven field validation. Instead of computing required fields client-side, the component now uses the authoritative `missing_required` list from the server, which correctly identifies incomplete nested objects that naive truthiness checks would miss. The UI displays an inline requirements banner showing outstanding fields and a disabled button until all requirements are met, with error messaging that surfaces server validation failures (like 422 responses) directly to the user.
This adds a manual "Create project" button to the intake workflow with server-driven field validation. Instead of computing required fields client-side, the component now uses the authoritative `missing_required` list from the server, which correctly identifies incomplete nested objects that naive truthiness checks would miss. The UI displays an inline requirements banner showing outstanding fields and a disabled button until all requirements are met, with error messaging that surfaces server validation failures (like 422 responses) directly to the user. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/frontend/src/styles.css
- hyqs/web/frontend/src/views/IntakeView.jsx
- hyqs/web/frontend/src/views/IntakeView.test.jsx
