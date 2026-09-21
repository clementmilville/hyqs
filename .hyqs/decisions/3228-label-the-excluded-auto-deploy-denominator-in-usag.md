# Job #3228: Label the excluded-auto-deploy denominator in UsageView's cost stat cards

**Date:** 2026-08-02

This diff adds a hint caption to the UsageView that displays when the API response indicates excluded operational auto-deploy jobs. The caption conditionally renders with proper pluralization ("job" vs "jobs") only when the backend signals that exclusions occurred and a count greater than zero. Two test cases ensure the caption appears correctly when exclusion data is present and gracefully handles its absence without rendering or crashing.
This diff adds a hint caption to the UsageView that displays when the API response indicates excluded operational auto-deploy jobs. The caption conditionally renders with proper pluralization ("job" vs "jobs") only when the backend signals that exclusions occurred and a count greater than zero. Two test cases ensure the caption appears correctly when exclusion data is present and gracefully handles its absence without rendering or crashing.

## Files touched
- hyqs/web/frontend/src/views/UsageView.jsx
- hyqs/web/frontend/src/views/UsageView.test.jsx
