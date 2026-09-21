# Job #3207: Expose effective priority + reasons in REST job projections

**Date:** 2026-08-02

The diff adds support for displaying jobs' effective (dynamically-adjusted) priorities alongside their base priorities in both the API and UI, alongside structured reasons explaining any boosts. The backend now calls `store.get_effective_priority()` to fetch the boosted priority and its reasoning details, passing these through the job serialization functions. On the frontend, a new `describePriorityReasons()` helper formats boost reasons into tooltips, and the UI displays a warning badge in job cards and table cells when effective priority exceeds base priority. The changes include comprehensive test coverage for all new serialization paths and UI components.
The diff adds support for displaying jobs' effective (dynamically-adjusted) priorities alongside their base priorities in both the API and UI, alongside structured reasons explaining any boosts. The backend now calls `store.get_effective_priority()` to fetch the boosted priority and its reasoning details, passing these through the job serialization functions. On the frontend, a new `describePriorityReasons()` helper formats boost reasons into tooltips, and the UI displays a warning badge in job cards and table cells when effective priority exceeds base priority. The changes include comprehensive test coverage for all new serialization paths and UI components.

## Files touched
- hyqs/web/app.py
- hyqs/web/frontend/src/components/JobCard.jsx
- hyqs/web/frontend/src/components/JobCard.test.jsx
- hyqs/web/frontend/src/components/jobTableShared.jsx
- hyqs/web/frontend/src/components/jobTableShared.test.jsx
- tests/test_job_detail_endpoint.py
- tests/test_job_list_endpoint.py
- tests/test_job_resolution_endpoints.py
