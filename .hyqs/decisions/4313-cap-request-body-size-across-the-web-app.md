# Job #4313: Cap request body size across the web app

**Date:** 2026-09-15

This diff adds a global request body size limit (default 25 MiB) to prevent OOM attacks on unauthenticated routes by rejecting oversized requests before they're buffered into memory. A new `MaxRequestBodySize` middleware rejects declared oversized `Content-Length` headers immediately and caps chunked bodies mid-stream, coordinated with nginx's `client_max_body_size` at the edge. The limit is configurable via `HYQS_MAX_REQUEST_BODY_BYTES` environment variable, with deployment documentation for hand-configured nginx vhosts and comprehensive tests for the middleware behavior.
This diff adds a global request body size limit (default 25 MiB) to prevent OOM attacks on unauthenticated routes by rejecting oversized requests before they're buffered into memory. A new `MaxRequestBodySize` middleware rejects declared oversized `Content-Length` headers immediately and caps chunked bodies mid-stream, coordinated with nginx's `client_max_body_size` at the edge. The limit is configurable via `HYQS_MAX_REQUEST_BODY_BYTES` environment variable, with deployment documentation for hand-configured nginx vhosts and comprehensive tests for the middleware behavior. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- deploy/README.md
- hyqs/config.py
- hyqs/web/app.py
- tests/test_config.py
- tests/test_max_body_size.py
