# Job #4097: Enforce activation semantics in contracts.py

**Date:** 2026-08-22

This diff adds semantic validation to plan contracts: when a plan marks `config_change_required` as true, it must provide non-blank `activation_location` and `expected_live_effect` fields. The validation is enforced in `_validate_plan_semantics()` with two new checks that raise `SchemaValidationError` if either field is missing or whitespace-only. Three test cases verify the validation rejects incomplete config changes while allowing blank fields when no config change is needed.
This diff adds semantic validation to plan contracts: when a plan marks `config_change_required` as true, it must provide non-blank `activation_location` and `expected_live_effect` fields. The validation is enforced in `_validate_plan_semantics()` with two new checks that raise `SchemaValidationError` if either field is missing or whitespace-only. Three test cases verify the validation rejects incomplete config changes while allowing blank fields when no config change is needed.

## Files touched
- hyqs/pipeline/contracts.py
- tests/test_contracts.py
