# Job #3748: Gateway store: sqlite-backed clients, credentials, audit, idempotency

**Date:** 2026-08-16

This diff introduces a new SQLite-backed persistence layer for the inference gateway, managing client credentials with hashed secrets, audit logging of API requests, and idempotent response caching. The `GatewayStore` class provides client lifecycle operations (create, verify, rotate, revoke) with thread-safe access via a lock, while the audit schema explicitly excludes sensitive request content like projections and prompts. A comprehensive test suite validates credential handling, credential rotation, metadata updates, and idempotency cache behavior with TTL-based pruning.
This diff introduces a new SQLite-backed persistence layer for the inference gateway, managing client credentials with hashed secrets, audit logging of API requests, and idempotent response caching. The `GatewayStore` class provides client lifecycle operations (create, verify, rotate, revoke) with thread-safe access via a lock, while the audit schema explicitly excludes sensitive request content like projections and prompts. A comprehensive test suite validates credential handling, credential rotation, metadata updates, and idempotency cache behavior with TTL-based pruning.

## Files touched
- hyqs/inference_gateway_store.py
- tests/test_inference_gateway_store.py
