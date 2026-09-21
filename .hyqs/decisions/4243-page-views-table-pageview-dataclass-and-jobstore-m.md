# Job #4243: page_views table, PageView dataclass, and JobStore methods

**Date:** 2026-09-06

This diff adds a page view analytics system to track website usage, consisting of a new `PageView` model, a `page_views` database table with geolocation and device metadata, and store methods to record and query page views with optional filtering by date range and path. The visitor tracking uses a persistent salt for hashing visitor identities instead of storing raw identifiers. Tests validate schema idempotency, round-trip record/query operations with various filters, and salt persistence across store instances.
This diff adds a page view analytics system to track website usage, consisting of a new `PageView` model, a `page_views` database table with geolocation and device metadata, and store methods to record and query page views with optional filtering by date range and path. The visitor tracking uses a persistent salt for hashing visitor identities instead of storing raw identifiers. Tests validate schema idempotency, round-trip record/query operations with various filters, and salt persistence across store instances. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/models.py
- hyqs/pipeline/store.py
- tests/test_store.py
