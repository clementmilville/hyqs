# Job #2971: Implement generic HTTP webhook delivery and honor event subscriptions

**Date:** 2026-08-01

This diff hardens shutdown and DNS reliability across the hyqs application. The main entry points (`hyqs/__main__.py` and `hyqs/pipeline/__main__.py`) now wrap their startup and runtime logic in try-finally blocks to guarantee cleanup operations (closing notifiers and job databases) execute even if exceptions occur during shutdown. The HTTP webhook sender adds a timeout to DNS resolution to prevent indefinite hangs on unresponsive DNS servers, and handles the resulting `TimeoutError`. Two new tests verify that DNS timeouts are handled gracefully and that client cleanup doesn't close caller-provided HTTP clients.
This diff hardens shutdown and DNS reliability across the hyqs application. The main entry points (`hyqs/__main__.py` and `hyqs/pipeline/__main__.py`) now wrap their startup and runtime logic in try-finally blocks to guarantee cleanup operations (closing notifiers and job databases) execute even if exceptions occur during shutdown. The HTTP webhook sender adds a timeout to DNS resolution to prevent indefinite hangs on unresponsive DNS servers, and handles the resulting `TimeoutError`. Two new tests verify that DNS timeouts are handled gracefully and that client cleanup doesn't close caller-provided HTTP clients.

## Files touched
- hyqs/__main__.py
- hyqs/pipeline/__main__.py
- hyqs/pipeline/notify_slack.py
- tests/test_notify_slack.py
