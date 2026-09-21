# Job #3223: Wire include_operational + exclusion metadata + new reliability route into REST 

**Date:** 2026-08-02

This diff adds an `include_operational` query parameter to reporting endpoints (agent stats, performance metrics, and usage summaries) to control whether operational vs. user-initiated job data is included in results, defaulting to excluding operational jobs. It also introduces a new `/api/deployment-reliability` endpoint to retrieve deployment success/failure breakdowns by project, with comprehensive test coverage for both the new parameter handling and the new endpoint.
This diff adds an `include_operational` query parameter to reporting endpoints (agent stats, performance metrics, and usage summaries) to control whether operational vs. user-initiated job data is included in results, defaulting to excluding operational jobs. It also introduces a new `/api/deployment-reliability` endpoint to retrieve deployment success/failure breakdowns by project, with comprehensive test coverage for both the new parameter handling and the new endpoint.

## Files touched
- hyqs/web/app.py
- tests/test_web_fleet.py
