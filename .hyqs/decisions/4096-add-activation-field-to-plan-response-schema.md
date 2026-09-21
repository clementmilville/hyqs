# Job #4096: Add activation field to plan-response schema

**Date:** 2026-08-22

This diff adds a required `activation` field to the plan response schema that captures deployment requirements. The new field documents whether config changes, feature flags, migrations, or other manual steps are needed for code to take effect in production, where that activation occurs, and what the observable effect will be. The schema is updated in both the JSON schema file and the Python documentation, with test fixtures updated to include valid `activation` objects that specify no special activation needed, allowing the planner to explicitly declare deploy-time dependencies alongside the implementation plan.
This diff adds a required `activation` field to the plan response schema that captures deployment requirements. The new field documents whether config changes, feature flags, migrations, or other manual steps are needed for code to take effect in production, where that activation occurs, and what the observable effect will be. The schema is updated in both the JSON schema file and the Python documentation, with test fixtures updated to include valid `activation` objects that specify no special activation needed, allowing the planner to explicitly declare deploy-time dependencies alongside the implementation plan.

## Files touched
- hyqs/pipeline/agents.py
- hyqs/pipeline/schemas/plan-response.schema.json
- tests/test_contracts.py
- tests/test_prompt_speed_pass.py
- tests/test_stages_plan_build.py
