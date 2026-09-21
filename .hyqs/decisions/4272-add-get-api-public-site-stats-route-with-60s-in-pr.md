# Job #4272: Add GET /api/public/site-stats route with 60s in-process cache

**Date:** 2026-09-07

This diff adds a new public `/api/public/site-stats` endpoint that exposes aggregated site statistics without requiring authentication. The endpoint caches results for 60 seconds and uses an asyncio lock to coalesce concurrent requests during cache misses into a single database query, preventing thundering-herd load on the store. Per-IP rate limiting (10 requests per 60-second window) prevents unauthenticated callers from hammering the expensive aggregation query, mirroring the existing page-view beacon's rate limiter. The implementation gracefully degrades by returning stale cached data on database failures, ensuring the endpoint remains available even under adverse conditions.
This diff adds a new public `/api/public/site-stats` endpoint that exposes aggregated site statistics without requiring authentication. The endpoint caches results for 60 seconds and uses an asyncio lock to coalesce concurrent requests during cache misses into a single database query, preventing thundering-herd load on the store. Per-IP rate limiting (10 requests per 60-second window) prevents unauthenticated callers from hammering the expensive aggregation query, mirroring the existing page-view beacon's rate limiter. The implementation gracefully degrades by returning stale cached data on database failures, ensuring the endpoint remains available even under adverse conditions. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/app.py
