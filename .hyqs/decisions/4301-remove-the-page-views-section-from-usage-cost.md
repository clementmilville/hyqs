# Job #4301: Remove the Page views section from Usage & Cost

**Date:** 2026-09-07

This commit removes the entire page views analytics section from AdminUsageCost and migrates it to a dedicated visitor-analytics page. The removed code includes the PageViewsSection component, helper components like DimensionTable, and all related API calls and state management for visitor analytics. A simple link now directs users to the new location at #admin/visitor-analytics, and tests are updated to verify the navigation link rather than full analytics rendering.
This commit removes the entire page views analytics section from AdminUsageCost and migrates it to a dedicated visitor-analytics page. The removed code includes the PageViewsSection component, helper components like DimensionTable, and all related API calls and state management for visitor analytics. A simple link now directs users to the new location at #admin/visitor-analytics, and tests are updated to verify the navigation link rather than full analytics rendering. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/frontend/src/views/AdminUsageCost.jsx
- hyqs/web/frontend/src/views/AdminUsageCost.test.jsx
