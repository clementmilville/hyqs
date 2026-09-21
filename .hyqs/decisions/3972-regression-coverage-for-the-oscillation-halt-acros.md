# Job #3972: Regression coverage for the oscillation halt across runner, classifier, and fixe

**Date:** 2026-08-17

This diff adds detection and halting logic for gate oscillation, where review and security gates alternately reject the same findings indefinitely. It introduces a new `gate_conflict` failure classification that halts the retry loop instead of burning through attempts, while ensuring the fixer agent only sees gate-specific evidence (not internal `gate_history` bookkeeping). The implementation distinguishes between genuine oscillation—identical findings repeating—and legitimate progress where findings genuinely change, so the system only stops when it detects the same gates stuck on the same issues, preventing infinite loops on job #3950-class incidents.
This diff adds detection and halting logic for gate oscillation, where review and security gates alternately reject the same findings indefinitely. It introduces a new `gate_conflict` failure classification that halts the retry loop instead of burning through attempts, while ensuring the fixer agent only sees gate-specific evidence (not internal `gate_history` bookkeeping). The implementation distinguishes between genuine oscillation—identical findings repeating—and legitimate progress where findings genuinely change, so the system only stops when it detects the same gates stuck on the same issues, preventing infinite loops on job #3950-class incidents.

## Files touched
- tests/test_pipeline_loop_fixes.py
