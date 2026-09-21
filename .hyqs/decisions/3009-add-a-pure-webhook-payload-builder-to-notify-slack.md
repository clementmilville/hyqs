# Job #3009: Add a pure webhook payload builder to notify_slack.py

**Date:** 2026-08-01

This diff adds a `build_webhook_payload()` function to centralize the construction of HTTP webhook JSON bodies, ensuring a single authoritative format across the backend. The function includes basic fields for all event types (project_id, job_id, event_type, summary) and conditionally adds epic_id and reason only for needs_attention events, preserving a legacy 4-key shape for job_complete and deploy events. Summary text is truncated to 500 characters. Five new tests cover the function's behavior across event types, default values, and the truncation logic.
This diff adds a `build_webhook_payload()` function to centralize the construction of HTTP webhook JSON bodies, ensuring a single authoritative format across the backend. The function includes basic fields for all event types (project_id, job_id, event_type, summary) and conditionally adds epic_id and reason only for needs_attention events, preserving a legacy 4-key shape for job_complete and deploy events. Summary text is truncated to 500 characters. Five new tests cover the function's behavior across event types, default values, and the truncation logic.

## Files touched
- hyqs/pipeline/notify_slack.py
- tests/test_notify_slack.py
