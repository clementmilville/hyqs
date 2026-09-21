# Job #4244: POST /api/public/page-view beacon endpoint with hashing, filtering, rate limitin

**Date:** 2026-09-06

This diff adds an unauthenticated `/api/public/page-view` endpoint that records page view analytics without requiring auth, collecting data like path, referrer, geolocation, device type, and timezone. The implementation anonymizes sensitive data by hashing the visitor IP and device traits using a daily salt, rather than storing raw IPs, and includes bot detection and per-IP rate limiting (30 requests/minute) to prevent abuse. A comprehensive test suite validates that invalid inputs are safely rejected, that hashing is deterministic within a day, and that all privacy protections work end-to-end against the real database.
This diff adds an unauthenticated `/api/public/page-view` endpoint that records page view analytics without requiring auth, collecting data like path, referrer, geolocation, device type, and timezone. The implementation anonymizes sensitive data by hashing the visitor IP and device traits using a daily salt, rather than storing raw IPs, and includes bot detection and per-IP rate limiting (30 requests/minute) to prevent abuse. A comprehensive test suite validates that invalid inputs are safely rejected, that hashing is deterministic within a day, and that all privacy protections work end-to-end against the real database. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/app.py
- tests/test_page_views.py
