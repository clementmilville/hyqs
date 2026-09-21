# Job #4300: Add the Visitor analytics admin page and reach it from the nav

**Date:** 2026-09-07

This diff adds a new "Visitor analytics" admin console feature that displays comprehensive analytics on page views across multiple dimensions—including geography, referrers, device types, languages, OS versions, and temporal patterns. The feature integrates a new AdminVisitorAnalytics component that fetches data via `getPageViewsSummary`, supports filtering by time range (7/30/90 days) and page path, and visualizes insights like unique visitor counts, pages per visitor, and returning visitor patterns. Access to the feature is gated by the `view_audit` permission, and the implementation emphasizes privacy by storing only salted, daily-rotating visitor hashes (no IP addresses). The navigation, routing, permissions, and test suite have been updated accordingly to surface this new admin section.
This diff adds a new "Visitor analytics" admin console feature that displays comprehensive analytics on page views across multiple dimensions—including geography, referrers, device types, languages, OS versions, and temporal patterns. The feature integrates a new AdminVisitorAnalytics component that fetches data via `getPageViewsSummary`, supports filtering by time range (7/30/90 days) and page path, and visualizes insights like unique visitor counts, pages per visitor, and returning visitor patterns. Access to the feature is gated by the `view_audit` permission, and the implementation emphasizes privacy by storing only salted, daily-rotating visitor hashes (no IP addresses). The navigation, routing, permissions, and test suite have been updated accordingly to surface this new admin section. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/frontend/src/components/Sidebar.jsx
- hyqs/web/frontend/src/components/Sidebar.test.jsx
- hyqs/web/frontend/src/constants.js
- hyqs/web/frontend/src/views/AdminConsole.jsx
- hyqs/web/frontend/src/views/AdminVisitorAnalytics.jsx
- hyqs/web/frontend/src/views/AdminVisitorAnalytics.test.jsx
