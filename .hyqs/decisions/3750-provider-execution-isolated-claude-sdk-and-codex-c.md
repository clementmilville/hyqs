# Job #3750: Provider execution: isolated Claude SDK and Codex CLI strategy calls

**Date:** 2026-08-16

This diff introduces isolated provider execution for the strategy-inference gateway, enabling strategy decisions from either the Claude Agent SDK or Codex CLI in sandboxed temporary directories that are always cleaned up afterward. The implementation (`run_claude_strategy` and `run_codex_strategy`) strictly prevents tool use and file mutations, treating any such attempts as hard failures, while translating provider-specific errors into a closed failure vocabulary and scrubbing sensitive details from messages. Comprehensive regression tests cover both happy paths and failure modes—capacity limits, credential expiration, timeouts, malformed output, and environment variable isolation for the Codex subprocess.
This diff introduces isolated provider execution for the strategy-inference gateway, enabling strategy decisions from either the Claude Agent SDK or Codex CLI in sandboxed temporary directories that are always cleaned up afterward. The implementation (`run_claude_strategy` and `run_codex_strategy`) strictly prevents tool use and file mutations, treating any such attempts as hard failures, while translating provider-specific errors into a closed failure vocabulary and scrubbing sensitive details from messages. Comprehensive regression tests cover both happy paths and failure modes—capacity limits, credential expiration, timeouts, malformed output, and environment variable isolation for the Codex subprocess.

## Files touched
- hyqs/inference_gateway_providers.py
- tests/test_inference_gateway_providers.py
