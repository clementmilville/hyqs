# Job #2974: [ai-fix] Update test_notify_slack.py to mock getaddrinfo and verify hardened sen

**Date:** 2026-08-01

This diff extends hyqs's notification system to support generic HTTP webhooks in addition to Slack webhooks, with strong security protections against SSRF attacks. The `_build_notifier` function now creates an async HTTP client and sends JSON payloads to matching webhooks filtered by event type (job completion or deployment); a new `send_http_webhook` function validates that webhook destinations resolve exclusively to public IP addresses, unwraps IPv4 addresses embedded in IPv6 (NAT64/IPv4-compatible prefixes), and pins the connection by hostname while preserving SNI. The notifier's lifecycle is properly managed—the HTTP client is closed on shutdown in both the main entry point and pipeline runner—and comprehensive tests verify that webhook delivery is secure (rejecting private addresses), event-filtered, and resilient to individual delivery failures.
This diff extends hyqs's notification system to support generic HTTP webhooks in addition to Slack webhooks, with strong security protections against SSRF attacks. The `_build_notifier` function now creates an async HTTP client and sends JSON payloads to matching webhooks filtered by event type (job completion or deployment); a new `send_http_webhook` function validates that webhook destinations resolve exclusively to public IP addresses, unwraps IPv4 addresses embedded in IPv6 (NAT64/IPv4-compatible prefixes), and pins the connection by hostname while preserving SNI. The notifier's lifecycle is properly managed—the HTTP client is closed on shutdown in both the main entry point and pipeline runner—and comprehensive tests verify that webhook delivery is secure (rejecting private addresses), event-filtered, and resilient to individual delivery failures.

## Files touched
- hyqs/__main__.py
- hyqs/pipeline/__main__.py
- hyqs/pipeline/notify_slack.py
- tests/test_notify_slack.py
- tests/test_pipeline_notifier.py
