# Job #3231: Static exclusion caption on PerformanceTab's pipeline-detail view

**Date:** 2026-08-02

The diff adds a clarification hint to the Performance Tab explaining that pipeline metrics exclude operational auto-deploy jobs. This message appears at the top of the tab to set user expectations about data coverage. The corresponding test is updated to verify the hint renders correctly, ensuring the change doesn't regress. This is a UX improvement to prevent confusion about missing job types in the metrics display.
The diff adds a clarification hint to the Performance Tab explaining that pipeline metrics exclude operational auto-deploy jobs. This message appears at the top of the tab to set user expectations about data coverage. The corresponding test is updated to verify the hint renders correctly, ensuring the change doesn't regress. This is a UX improvement to prevent confusion about missing job types in the metrics display.

## Files touched
- hyqs/web/frontend/src/views/PerformanceTab.jsx
- hyqs/web/frontend/src/views/PerformanceTab.test.jsx
