# Job #2955: Capture connection forensics in audit_log alongside the actor label

**Date:** 2026-08-01

The diff adds connection-level forensic tracking to the audit log by capturing Postgres's native connection metadata—backend PID, client address, and connection start time—alongside the self-reported actor field. The schema adds three new columns to `audit_log`, the audit trigger function captures these values from Postgres system functions for every INSERT/UPDATE/DELETE operation, and the `list_audit` API now returns them serialized to clients. This enriches the audit trail with server-verified connection data that is harder to spoof than actor strings and helps with forensic investigation of who actually made changes.
The diff adds connection-level forensic tracking to the audit log by capturing Postgres's native connection metadata—backend PID, client address, and connection start time—alongside the self-reported actor field. The schema adds three new columns to `audit_log`, the audit trigger function captures these values from Postgres system functions for every INSERT/UPDATE/DELETE operation, and the `list_audit` API now returns them serialized to clients. This enriches the audit trail with server-verified connection data that is harder to spoof than actor strings and helps with forensic investigation of who actually made changes.

## Files touched
- hyqs/pipeline/store.py
- tests/test_audit_trail.py
- tests/test_store.py
