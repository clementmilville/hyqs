# Job #3085: Run the incident analyst on a strong model, set by configuration

**Date:** 2026-08-02

The diff makes the incident analyst model configurable instead of hardcoded, adding a new `pipeline_incident_analyst_model` config field that defaults to "sonnet" and can be overridden via the `HYQS_PIPELINE_INCIDENT_ANALYST_MODEL` environment variable. The supervisor now reads this config value when building the backend for diagnosis, replacing the previously hardcoded Haiku model with a stronger model better suited to the harder reasoning task of incident analysis. The change includes comprehensive test coverage verifying the model is read from config, defaults to sonnet, can be overridden, and that diagnosis is still correctly gated by the existing caps and provider-pause checks.
The diff makes the incident analyst model configurable instead of hardcoded, adding a new `pipeline_incident_analyst_model` config field that defaults to "sonnet" and can be overridden via the `HYQS_PIPELINE_INCIDENT_ANALYST_MODEL` environment variable. The supervisor now reads this config value when building the backend for diagnosis, replacing the previously hardcoded Haiku model with a stronger model better suited to the harder reasoning task of incident analysis. The change includes comprehensive test coverage verifying the model is read from config, defaults to sonnet, can be overridden, and that diagnosis is still correctly gated by the existing caps and provider-pause checks.

## Files touched
- hyqs/config.py
- hyqs/pipeline/supervisor.py
- tests/test_auto_requeue_after_fix.py
