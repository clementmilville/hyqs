# Job #3087: Consult the analyst when a deterministic remediation exhausts its budget

**Date:** 2026-08-02

This diff introduces a pre-dead-letter analyst consult feature that routes exhausted deterministic remediation paths to the AI incident analyst for one final diagnosis attempt before dead-lettering. Four remediation paths (stale branch, transient, env deploy, dependency blocked) now call `_consult_analyst_before_dead_letter` when they reach their requeue cap, allowing the analyst to catch issues the deterministic hypothesis missed. The feature is controlled by a new `pipeline_incident_analyst_predeadletter` config flag (enabled by default, disableable via `HYQS_PIPELINE_INCIDENT_ANALYST_PREDEADLETTER_DISABLED` env var) and includes refactored analyst logic that tracks both whether diagnosis ran and whether it productively resolved the job.
This diff introduces a pre-dead-letter analyst consult feature that routes exhausted deterministic remediation paths to the AI incident analyst for one final diagnosis attempt before dead-lettering. Four remediation paths (stale branch, transient, env deploy, dependency blocked) now call `_consult_analyst_before_dead_letter` when they reach their requeue cap, allowing the analyst to catch issues the deterministic hypothesis missed. The feature is controlled by a new `pipeline_incident_analyst_predeadletter` config flag (enabled by default, disableable via `HYQS_PIPELINE_INCIDENT_ANALYST_PREDEADLETTER_DISABLED` env var) and includes refactored analyst logic that tracks both whether diagnosis ran and whether it productively resolved the job.

## Files touched
- hyqs/config.py
- hyqs/pipeline/supervisor.py
- tests/test_auto_requeue_after_fix.py
