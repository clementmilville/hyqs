# Job #4245: Consent-free sendBeacon script on the public how-it-works page

**Date:** 2026-09-06

This diff adds a client-side analytics tracker to the how-it-works page that collects page view events along with device and browser metadata (language, timezone, screen resolution, hardware concurrency, referrer). The script sends this data to a backend `/api/public/page-view` endpoint using `navigator.sendBeacon` when available, falling back to `fetch` with keepalive for older browsers. The collection is wrapped in error handling to prevent tracking failures from breaking the page.
This diff adds a client-side analytics tracker to the how-it-works page that collects page view events along with device and browser metadata (language, timezone, screen resolution, hardware concurrency, referrer). The script sends this data to a backend `/api/public/page-view` endpoint using `navigator.sendBeacon` when available, falling back to `fetch` with keepalive for older browsers. The collection is wrapped in error handling to prevent tracking failures from breaking the page. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/frontend/public/how-it-works/index.html
