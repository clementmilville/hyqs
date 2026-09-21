# Job #2910: Regression tests for reuse-only vs modification-evidenced cascade

**Date:** 2026-07-31

This diff adds two test cases that validate the distinction between reusing an existing, unmodified function versus explicitly modifying it in a plan. The first test ensures that when a function is merely reused (mentioned but not changed), its nearest test is not unnecessarily pulled into scope and the plan passes validation without it. The second test verifies the opposite: when the same function is explicitly named as a file to be modified, its nearest test becomes required and a plan omitting it fails with a regression_test_omission error. Together, these tests establish a key behavioral rule for planning — reuse mentions should not force test inclusion, but explicit modifications should.
This diff adds two test cases that validate the distinction between reusing an existing, unmodified function versus explicitly modifying it in a plan. The first test ensures that when a function is merely reused (mentioned but not changed), its nearest test is not unnecessarily pulled into scope and the plan passes validation without it. The second test verifies the opposite: when the same function is explicitly named as a file to be modified, its nearest test becomes required and a plan omitting it fails with a regression_test_omission error. Together, these tests establish a key behavioral rule for planning — reuse mentions should not force test inclusion, but explicit modifications should.

## Files touched
- tests/test_collision.py
