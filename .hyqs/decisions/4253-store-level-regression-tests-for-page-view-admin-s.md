# Job #4253: Store-level regression tests for page_view_admin_summary

**Date:** 2026-09-06

This test expansion validates the `page_view_admin_summary` function's handling of unique visitor counting across date boundaries and filtering. The test adds a second day of traffic where the same visitor hash reappears, proving the implementation sums daily distinct counts (2+1=3) rather than doing a naive global `COUNT(DISTINCT)` (which would incorrectly return 2). The expanded assertions verify all grouping dimensions (`by_day`, `by_country`, `by_city`, `by_ref`, `by_referrer`, `by_device`), filter combinations (`since`, `until`, `path`), and that visitor hashes are never leaked in result rows—catching a privacy bug that could have slipped through the original sparse assertions.
This test expansion validates the `page_view_admin_summary` function's handling of unique visitor counting across date boundaries and filtering. The test adds a second day of traffic where the same visitor hash reappears, proving the implementation sums daily distinct counts (2+1=3) rather than doing a naive global `COUNT(DISTINCT)` (which would incorrectly return 2). The expanded assertions verify all grouping dimensions (`by_day`, `by_country`, `by_city`, `by_ref`, `by_referrer`, `by_device`), filter combinations (`since`, `until`, `path`), and that visitor hashes are never leaked in result rows—catching a privacy bug that could have slipped through the original sparse assertions. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- tests/test_store.py
