# Job #4121: Pricing table: Claude 5 family, gpt-5.6-sol, and cache-tier aware cost

**Date:** 2026-09-02

This diff introduces prompt caching support to the pricing module and updates model prices for the latest Claude and OpenAI releases. The `compute_cost` function now accepts optional `cache_creation_tokens` and `cache_read_tokens` parameters that price at the `w5` and `read` rates respectively (falling back to the input rate for models without cache tier definitions). Three new Claude models are added (`claude-opus-5`, `claude-sonnet-5`, `claude-fable-5`) along with `gpt-5.6-sol`, and a comprehensive test suite validates the new pricing logic, cached token calculations, and backward compatibility.
This diff introduces prompt caching support to the pricing module and updates model prices for the latest Claude and OpenAI releases. The `compute_cost` function now accepts optional `cache_creation_tokens` and `cache_read_tokens` parameters that price at the `w5` and `read` rates respectively (falling back to the input rate for models without cache tier definitions). Three new Claude models are added (`claude-opus-5`, `claude-sonnet-5`, `claude-fable-5`) along with `gpt-5.6-sol`, and a comprehensive test suite validates the new pricing logic, cached token calculations, and backward compatibility. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- hyqs/pipeline/pricing.py
- tests/test_pricing.py
