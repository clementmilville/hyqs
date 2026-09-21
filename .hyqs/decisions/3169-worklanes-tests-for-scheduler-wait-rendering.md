# Job #3169: WorkLanes tests for scheduler_wait rendering

**Date:** 2026-08-02

This diff adds responsive test coverage for the WorkLanes component, including a new viewport mock utility and three parameterized tests that verify job lane classification (Queued vs. Blocked) works correctly at both desktop and mobile widths. The tests ensure scheduler-wait-only jobs and dependency-waiting jobs correctly land in the Blocked lane, while free jobs stay in Queued, and they verify that mobile viewports render job dependencies as buttons instead of text labels. The changes are pure test additions with no production code modifications.
This diff adds responsive test coverage for the WorkLanes component, including a new viewport mock utility and three parameterized tests that verify job lane classification (Queued vs. Blocked) works correctly at both desktop and mobile widths. The tests ensure scheduler-wait-only jobs and dependency-waiting jobs correctly land in the Blocked lane, while free jobs stay in Queued, and they verify that mobile viewports render job dependencies as buttons instead of text labels. The changes are pure test additions with no production code modifications.

## Files touched
- hyqs/web/frontend/src/components/WorkLanes.test.jsx
