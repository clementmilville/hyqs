# Job #4101: Regression tests for merge-stage activation follow-up filing

**Date:** 2026-08-22

This diff adds comprehensive regression tests for the merge stage's activation follow-up job filing logic (job #4099). The test suite covers three scenarios: when a plan requires configuration changes, it verifies that a follow-up job is filed and activation instructions are appended to the summary; when configuration changes aren't required, it verifies that only a "none required" note is appended without creating a follow-up job; and for legacy plans without activation metadata, it ensures the implementation summary remains unmodified. The tests use fake stores and patched git/GitHub calls to isolate the merge logic and validate the correct behavior across all three cases.
This diff adds comprehensive regression tests for the merge stage's activation follow-up job filing logic (job #4099). The test suite covers three scenarios: when a plan requires configuration changes, it verifies that a follow-up job is filed and activation instructions are appended to the summary; when configuration changes aren't required, it verifies that only a "none required" note is appended without creating a follow-up job; and for legacy plans without activation metadata, it ensures the implementation summary remains unmodified. The tests use fake stores and patched git/GitHub calls to isolate the merge logic and validate the correct behavior across all three cases. Activation: none required; this change alters behaviour unconditionally.

## Files touched
- tests/test_stages_merge_activation_followup.py
