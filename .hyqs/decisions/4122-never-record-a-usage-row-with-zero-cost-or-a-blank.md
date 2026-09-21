# Job #4122: Never record a usage row with zero cost or a blank model

**Date:** 2026-09-02

This diff adds model and provider attribution to usage tracking throughout the pipeline, enabling detailed cost accounting per LLM. The agent stream collection now extracts model and provider from event data with fallbacks to backend configuration, computes costs for all token types including cache tokens when not provided, and persists these fields alongside existing token counts. The CodexBackend result event now passes cache token classes to the cost function, and comprehensive tests verify the fallback logic, cost preservation, and database storage of attribution data.
This diff adds model and provider attribution to usage tracking throughout the pipeline, enabling detailed cost accounting per LLM. The agent stream collection now extracts model and provider from event data with fallbacks to backend configuration, computes costs for all token types including cache tokens when not provided, and persists these fields alongside existing token counts. The CodexBackend result event now passes cache token classes to the cost function, and comprehensive tests verify the fallback logic, cost preservation, and database storage of attribution data. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/agents.py
- hyqs/pipeline/codex.py
- tests/test_codex.py
- tests/test_store.py
