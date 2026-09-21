# Job #4270: Add JobStore.site_stats() aggregate query method

**Date:** 2026-09-07

This diff adds a new `site_stats()` method to `JobStore` that computes global, cross-project operational telemetry for the platform—including shipped job counts (by timeframe), gate failures bucketed by type, cost metrics, lead times, off-hours share, and worker/user/project status. The method reuses the existing operational exclusion clause to filter archived and auto-deploy jobs, keeping metrics consistent with existing analytics conventions. Comprehensive tests validate the aggregation logic against an isolated database, ensuring correctness on both empty and hand-seeded datasets with proper filtering of excluded jobs.
This diff adds a new `site_stats()` method to `JobStore` that computes global, cross-project operational telemetry for the platform—including shipped job counts (by timeframe), gate failures bucketed by type, cost metrics, lead times, off-hours share, and worker/user/project status. The method reuses the existing operational exclusion clause to filter archived and auto-deploy jobs, keeping metrics consistent with existing analytics conventions. Comprehensive tests validate the aggregation logic against an isolated database, ensuring correctness on both empty and hand-seeded datasets with proper filtering of excluded jobs. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/store.py
- tests/test_store.py
