# Job #4123: Investigate why the Codex adapter re-sends ~52% of its context as fresh input on every call

**Date:** 2026-09-02

This diff fixes a billing bug in Codex's token accounting and ships investigation documentation. Codex reports `input_tokens` as an OpenAI-style total already inclusive of cached tokens, but the billing code was treating it as disjoint from cache buckets and charging the full input rate on cached tokens twice — the fix subtracts cached and cache-write tokens to bill only the fresh portion at full rate. The new documentation `codex-prompt-caching-investigation.md` concludes that Codex's ~50% fresh-input ratio cannot be fixed at the adapter level; it's structural to the CLI's invariant preamble strategy, unlike Claude's SDK-level caching, so the routing decision is returned to the roster owner.
This diff fixes a billing bug in Codex's token accounting and ships investigation documentation. Codex reports `input_tokens` as an OpenAI-style total already inclusive of cached tokens, but the billing code was treating it as disjoint from cache buckets and charging the full input rate on cached tokens twice — the fix subtracts cached and cache-write tokens to bill only the fresh portion at full rate. The new documentation `codex-prompt-caching-investigation.md` concludes that Codex's ~50% fresh-input ratio cannot be fixed at the adapter level; it's structural to the CLI's invariant preamble strategy, unlike Claude's SDK-level caching, so the routing decision is returned to the roster owner. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- docs/codex-prompt-caching-investigation.md
- hyqs/pipeline/codex.py
- tests/test_codex.py
