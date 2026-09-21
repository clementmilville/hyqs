# Job #3010: De-duplicate and extend the MCP registration vocabulary

**Date:** 2026-08-01

This diff adds support for a new `needs_attention` webhook event type alongside the existing `job_complete` and `deploy` events. The `_VALID_WEBHOOK_EVENTS` constant is refactored to be imported from `hyqs.web.app` rather than defined locally in the MCP server module. A new test verifies that webhooks can be registered with the `needs_attention` event type. The docstring for webhook registration is updated to document the new event type as valid.
This diff adds support for a new `needs_attention` webhook event type alongside the existing `job_complete` and `deploy` events. The `_VALID_WEBHOOK_EVENTS` constant is refactored to be imported from `hyqs.web.app` rather than defined locally in the MCP server module. A new test verifies that webhooks can be registered with the `needs_attention` event type. The docstring for webhook registration is updated to document the new event type as valid.

## Files touched
- hyqs/web/mcp_server.py
- tests/test_mcp_server.py
