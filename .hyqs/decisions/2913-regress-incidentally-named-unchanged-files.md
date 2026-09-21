# Job #2913: Regress incidentally named unchanged files

**Date:** 2026-07-31

This diff extends a test for collision detection to verify that plan scope validation catches missing candidate files. It adds test cases that check: (1) test files corresponding to unchanged source files remain disjoint from the reasons dict, confirming they're not incorrectly marked as touched, and (2) when a required collision.py candidate is omitted from a plan's target files, the validator reports a "candidate_omission" error with the missing file identified. The additional narrative in the test documents why those files were investigated during the original debugging.
This diff extends a test for collision detection to verify that plan scope validation catches missing candidate files. It adds test cases that check: (1) test files corresponding to unchanged source files remain disjoint from the reasons dict, confirming they're not incorrectly marked as touched, and (2) when a required collision.py candidate is omitted from a plan's target files, the validator reports a "candidate_omission" error with the missing file identified. The additional narrative in the test documents why those files were investigated during the original debugging.

## Files touched
- tests/test_collision.py
