# Job #3051: Let hyqs-jobs authenticate with a scoped API token, not only browser OAuth

**Date:** 2026-08-01

This diff adds non-interactive API token authentication to the hyqs-jobs CLI, allowing scripts and cron jobs to authenticate via the `HYQS_JOBS_API_TOKEN` environment variable with a bearer token instead of requiring an interactive OAuth browser flow. When the env var is set, the client skips the OAuth machinery entirely and talks directly to the server; when unset, it falls back to the existing interactive OAuth flow. The diff also improves error handling by distinguishing auth rejections (HTTP 401/403) from other transport failures, returning a specific forbidden message with a dedicated exit code instead of collapsing them into a generic transport error. Tests ensure the token path works correctly, validates that blank tokens are ignored, and verifies that auth rejections are properly handled.
This diff adds non-interactive API token authentication to the hyqs-jobs CLI, allowing scripts and cron jobs to authenticate via the `HYQS_JOBS_API_TOKEN` environment variable with a bearer token instead of requiring an interactive OAuth browser flow. When the env var is set, the client skips the OAuth machinery entirely and talks directly to the server; when unset, it falls back to the existing interactive OAuth flow. The diff also improves error handling by distinguishing auth rejections (HTTP 401/403) from other transport failures, returning a specific forbidden message with a dedicated exit code instead of collapsing them into a generic transport error. Tests ensure the token path works correctly, validates that blank tokens are ignored, and verifies that auth rejections are properly handled.

## Files touched
- hyqs/cli/client.py
- hyqs/cli/main.py
- tests/test_cli.py
