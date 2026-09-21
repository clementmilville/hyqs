# Job #2973: Implement generic HTTP webhook delivery with SSRF protection

**Date:** 2026-08-01

This diff hardens HTTP webhook security by comprehensively validating that all DNS-resolved addresses are public, including detecting private IPv4 addresses embedded in IPv6 transition formats (IPv4-mapped, NAT64, 6to4, Teredo). The `_is_global` function now unwraps all embedded IPv4 addresses from IPv6 and validates them individually, while the webhook validation logic rejects destinations that resolve to any mix of public and private addresses. IPv6 addresses in the Host header are now properly bracketed, and an explicit timeout is added to the outbound HTTP request. The test suite expands coverage with new tests for connection errors, mixed public/private DNS results, and specific private address classes.
This diff hardens HTTP webhook security by comprehensively validating that all DNS-resolved addresses are public, including detecting private IPv4 addresses embedded in IPv6 transition formats (IPv4-mapped, NAT64, 6to4, Teredo). The `_is_global` function now unwraps all embedded IPv4 addresses from IPv6 and validates them individually, while the webhook validation logic rejects destinations that resolve to any mix of public and private addresses. IPv6 addresses in the Host header are now properly bracketed, and an explicit timeout is added to the outbound HTTP request. The test suite expands coverage with new tests for connection errors, mixed public/private DNS results, and specific private address classes.

## Files touched
- hyqs/pipeline/notify_slack.py
- tests/test_notify_slack.py
- tests/test_pipeline_notifier.py
