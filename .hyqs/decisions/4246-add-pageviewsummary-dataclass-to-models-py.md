# Job #4246: Add PageViewSummary dataclass to models.py

**Date:** 2026-09-06

This diff introduces a new `PageViewSummary` dataclass to model aggregated analytics data across multiple dimensions—totals, daily breakdowns, geographic and device splits, referrers, and recent visits. The class includes a `to_dict()` method to serialize this data into a flat dictionary structure suitable for API responses or storage. Tests verify that the serialization preserves all expected fields correctly and crucially excludes sensitive data like `visitor_hash` from the output.
This diff introduces a new `PageViewSummary` dataclass to model aggregated analytics data across multiple dimensions—totals, daily breakdowns, geographic and device splits, referrers, and recent visits. The class includes a `to_dict()` method to serialize this data into a flat dictionary structure suitable for API responses or storage. Tests verify that the serialization preserves all expected fields correctly and crucially excludes sensitive data like `visitor_hash` from the output. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/models.py
- tests/test_models_page_view_summary.py
