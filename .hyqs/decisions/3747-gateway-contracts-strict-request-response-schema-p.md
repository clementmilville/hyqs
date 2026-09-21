# Job #3747: Gateway contracts: strict request/response schema, policy catalog, model allowli

**Date:** 2026-08-16

This diff introduces a validation layer for strategy-inference requests that enforces a closed vocabulary for providers, models, policies, and projection fields. The module prevents clients from injecting arbitrary prompts, tools, or unauthorized configurations by validating every request field against fixed allowlists and checking that projections stay within size and nesting constraints. Comprehensive regression tests verify that the validator correctly rejects malformed or dangerous inputs while accepting well-formed requests and that prompt rendering is deterministic across equivalent projections.
This diff introduces a validation layer for strategy-inference requests that enforces a closed vocabulary for providers, models, policies, and projection fields. The module prevents clients from injecting arbitrary prompts, tools, or unauthorized configurations by validating every request field against fixed allowlists and checking that projections stay within size and nesting constraints. Comprehensive regression tests verify that the validator correctly rejects malformed or dangerous inputs while accepting well-formed requests and that prompt rendering is deterministic across equivalent projections.

## Files touched
- hyqs/inference_gateway_contracts.py
- tests/test_inference_gateway_contracts.py
