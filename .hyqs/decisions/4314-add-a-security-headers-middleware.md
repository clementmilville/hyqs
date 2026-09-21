# Job #4314: Add a security-headers middleware

**Date:** 2026-09-15

This diff adds a `SecurityHeaders` ASGI middleware that stamps baseline security response headers—including X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Strict-Transport-Security, and a report-only Content-Security-Policy—onto every outgoing response. The middleware is registered as the outermost layer so it applies universally, including to auth failures and static assets. A test suite verifies the headers appear on successful API calls, unauthorized responses, and static file serves, ensuring consistent security posture across the entire app.
This diff adds a `SecurityHeaders` ASGI middleware that stamps baseline security response headers—including X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Strict-Transport-Security, and a report-only Content-Security-Policy—onto every outgoing response. The middleware is registered as the outermost layer so it applies universally, including to auth failures and static assets. A test suite verifies the headers appear on successful API calls, unauthorized responses, and static file serves, ensuring consistent security posture across the entire app. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/web/app.py
- tests/test_security_headers.py
