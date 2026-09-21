# Job #3167: Distinguish implicit scheduler waits from explicit dependency blocking in Work l

**Date:** 2026-08-02

This diff adds support for displaying scheduler-based blocking constraints in the work lanes UI. Jobs pending with a `scheduler_wait` flag (indicating resource conflicts like file overlaps) are now classified as blocked, and the UI displays the scheduler's summary message instead of "Ready to start." Dependency-based waits take priority when both `waiting_on` and `scheduler_wait` are present, with tests confirming the classification and display logic handle both cases correctly.
This diff adds support for displaying scheduler-based blocking constraints in the work lanes UI. Jobs pending with a `scheduler_wait` flag (indicating resource conflicts like file overlaps) are now classified as blocked, and the UI displays the scheduler's summary message instead of "Ready to start." Dependency-based waits take priority when both `waiting_on` and `scheduler_wait` are present, with tests confirming the classification and display logic handle both cases correctly.

## Files touched
- hyqs/web/frontend/src/components/WorkLanes.jsx
- hyqs/web/frontend/src/components/WorkLanes.test.jsx
