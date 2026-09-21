# Job #3086: Raise and externalise the analyst's per-scan and per-job budgets

**Date:** 2026-08-02

This change replaces hardcoded frequency limits on the AI incident analyst with configurable parameters. The analyst previously ran at most twice per job and twice per scan cycle; now those caps are `pipeline_incident_analyst_job_cap` (default 6) and `pipeline_incident_analyst_scan_budget` (default 8), both overridable via environment variables. The config defaults are conservative, but operators can now tune the analyst's invocation rate to balance cost and responsiveness without modifying code. Tests were added to verify the new config parameters work correctly and that the scan budget actually stops the analyst when exhausted.
This change replaces hardcoded frequency limits on the AI incident analyst with configurable parameters. The analyst previously ran at most twice per job and twice per scan cycle; now those caps are `pipeline_incident_analyst_job_cap` (default 6) and `pipeline_incident_analyst_scan_budget` (default 8), both overridable via environment variables. The config defaults are conservative, but operators can now tune the analyst's invocation rate to balance cost and responsiveness without modifying code. Tests were added to verify the new config parameters work correctly and that the scan budget actually stops the analyst when exhausted.

## Files touched
- hyqs/config.py
- hyqs/pipeline/supervisor.py
- tests/test_auto_requeue_after_fix.py
- tests/test_pipeline_notifier.py
