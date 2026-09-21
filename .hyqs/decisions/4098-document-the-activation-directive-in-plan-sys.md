# Job #4098: Document the activation directive in PLAN_SYS

**Date:** 2026-08-22

This diff adds a new "CONFIGURATION ACTIVATION DIRECTIVE" section that guides agents on how to handle opt-in configuration for new features, requiring explicit naming of the configuration location and expected behavioral change when activation is needed. It also refines the documentation for the existing `activation` schema fields to clarify that `config_change_required` specifically targets compatibility-preserving defaults and to specify what values to use when no configuration activation is required. The changes tighten the contract for how story plans document and activate new configuration-gated behavior, preventing implicit defaults and making deployment activation steps explicit.
This diff adds a new "CONFIGURATION ACTIVATION DIRECTIVE" section that guides agents on how to handle opt-in configuration for new features, requiring explicit naming of the configuration location and expected behavioral change when activation is needed. It also refines the documentation for the existing `activation` schema fields to clarify that `config_change_required` specifically targets compatibility-preserving defaults and to specify what values to use when no configuration activation is required. The changes tighten the contract for how story plans document and activate new configuration-gated behavior, preventing implicit defaults and making deployment activation steps explicit.

## Files touched
- hyqs/pipeline/agents.py
