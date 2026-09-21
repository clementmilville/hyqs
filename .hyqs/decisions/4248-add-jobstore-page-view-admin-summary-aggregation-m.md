# Job #4248: Add JobStore.page_view_admin_summary aggregation method

**Date:** 2026-09-06

This diff adds a new `page_view_admin_summary()` method to the `JobStore` class that aggregates page view analytics into multiple dimensions: daily breakdowns, top countries and cities, referrals, referrer hosts, devices, and a recent sample. The method accepts time-bounded queries (optionally filtered by path) and deliberately excludes visitor hashes from the recent sample for privacy reasons, while calculating unique visitors as a sum of daily distinct counts rather than a global count since the hash rotates daily. Two integration tests verify that the aggregation correctly filters by time window and path, and confirm that visitor hashes are never exposed in the recent data.
This diff adds a new `page_view_admin_summary()` method to the `JobStore` class that aggregates page view analytics into multiple dimensions: daily breakdowns, top countries and cities, referrals, referrer hosts, devices, and a recent sample. The method accepts time-bounded queries (optionally filtered by path) and deliberately excludes visitor hashes from the recent sample for privacy reasons, while calculating unique visitors as a sum of daily distinct counts rather than a global count since the hash rotates daily. Two integration tests verify that the aggregation correctly filters by time window and path, and confirm that visitor hashes are never exposed in the recent data. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/store.py
- tests/test_store.py
