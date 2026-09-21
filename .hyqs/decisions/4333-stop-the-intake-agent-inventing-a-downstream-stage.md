# Job #4333: Stop the intake agent inventing a downstream stage that does not exist

**Date:** 2026-09-10

This diff adds explicit coordination between the intake agent's interview prompt and the completeness gate. It documents the nine required spec fields (name, slug, one_liner, problem, users, auth, features, data_model, stack) in the agent prompt and clarifies that the agent should only collect/update the spec and emit the completion marker when appropriate—never claim completeness itself, since only the `check_completeness` gate decides that. A new test ensures the prompt's required-fields list stays synchronized with the gate's authoritative list, preventing drift that would cause the agent and gate to disagree on completeness.
This diff adds explicit coordination between the intake agent's interview prompt and the completeness gate. It documents the nine required spec fields (name, slug, one_liner, problem, users, auth, features, data_model, stack) in the agent prompt and clarifies that the agent should only collect/update the spec and emit the completion marker when appropriate—never claim completeness itself, since only the `check_completeness` gate decides that. A new test ensures the prompt's required-fields list stays synchronized with the gate's authoritative list, preventing drift that would cause the agent and gate to disagree on completeness. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/intake/agent.py
- tests/test_intake_agent.py
