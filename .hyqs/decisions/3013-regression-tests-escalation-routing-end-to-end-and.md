# Job #3013: Regression tests: escalation routing end-to-end and vocabulary isolation

**Date:** 2026-08-01

This diff adds six new tests to validate webhook event routing and notification behavior in the pipeline supervisor. The tests verify that needs_attention and job_complete webhooks are properly isolated (routing only to their respective handlers), that remediation functions like stale branch and gate-no-changes recovery emit the correct event types or suppress them appropriately, and that the janitor supervisor escalates unrecognized failures as needs_attention events. The expanded imports include new mocking utilities and supervisor remediation functions being tested.
This diff adds six new tests to validate webhook event routing and notification behavior in the pipeline supervisor. The tests verify that needs_attention and job_complete webhooks are properly isolated (routing only to their respective handlers), that remediation functions like stale branch and gate-no-changes recovery emit the correct event types or suppress them appropriately, and that the janitor supervisor escalates unrecognized failures as needs_attention events. The expanded imports include new mocking utilities and supervisor remediation functions being tested.

## Files touched
- tests/test_pipeline_notifier.py
