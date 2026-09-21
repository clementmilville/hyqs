# Job #3011: Tag the supervisor's give-up notifications with event_type=needs_attention

**Date:** 2026-08-01

The diff adds structured event categorization to webhook notifications by introducing `event_type` and `reason` parameters to the notification system. Previously, webhooks only received "deploy" or "job_complete" events inferred from job status; now supervisory failures and escalations are explicitly categorized as "needs_attention" events with reason codes (e.g., "gate_no_changes", "deploy_failed", "job_stale"). The notification payload builder now accepts `epic_id` and `reason` fields, and the notifier can handle cases where `job_id` is absent (such as project-level deploy staleness), making webhooks more actionable for distinguishing between successful completions and failures requiring human intervention.
The diff adds structured event categorization to webhook notifications by introducing `event_type` and `reason` parameters to the notification system. Previously, webhooks only received "deploy" or "job_complete" events inferred from job status; now supervisory failures and escalations are explicitly categorized as "needs_attention" events with reason codes (e.g., "gate_no_changes", "deploy_failed", "job_stale"). The notification payload builder now accepts `epic_id` and `reason` fields, and the notifier can handle cases where `job_id` is absent (such as project-level deploy staleness), making webhooks more actionable for distinguishing between successful completions and failures requiring human intervention.

## Files touched
- hyqs/pipeline/__main__.py
- hyqs/pipeline/supervisor.py
- tests/test_pipeline_notifier.py
