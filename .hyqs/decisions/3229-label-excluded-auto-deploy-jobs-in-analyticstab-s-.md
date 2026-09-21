# Job #3229: Label excluded auto-deploy jobs in AnalyticsTab's headline stat row

**Date:** 2026-08-02

This diff adds a disclosure message to the analytics view explaining that certain metrics exclude auto-deploy jobs, and specifies how many were excluded. The hint appears conditionally when the backend provides exclusion metadata, with proper pluralization handling (job vs. jobs). Two new tests verify the caption displays correctly when exclusion data is present and is omitted when absent.
This diff adds a disclosure message to the analytics view explaining that certain metrics exclude auto-deploy jobs, and specifies how many were excluded. The hint appears conditionally when the backend provides exclusion metadata, with proper pluralization handling (job vs. jobs). Two new tests verify the caption displays correctly when exclusion data is present and is omitted when absent.

## Files touched
- hyqs/web/frontend/src/views/AnalyticsTab.jsx
- hyqs/web/frontend/src/views/AnalyticsTab.test.jsx
