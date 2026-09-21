# Job #3008: Add needs_attention to the REST webhook vocabulary

**Date:** 2026-08-01

The webhook system now supports a new event type called "needs_attention" by adding it to the list of valid webhook events in the app. A corresponding test verifies that the webhook creation endpoint accepts this new event type and returns the expected response. A minor formatting change also cleans up an existing test by putting a long function call on a single line.
The webhook system now supports a new event type called "needs_attention" by adding it to the list of valid webhook events in the app. A corresponding test verifies that the webhook creation endpoint accepts this new event type and returns the expected response. A minor formatting change also cleans up an existing test by putting a long function call on a single line.

## Files touched
- hyqs/web/app.py
- tests/test_webhook_endpoints.py
