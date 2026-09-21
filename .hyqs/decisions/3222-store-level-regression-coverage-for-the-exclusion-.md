# Job #3222: Store-level regression coverage for the exclusion predicate and deployment relia

**Date:** 2026-08-02

These tests validate that the store's performance analytics queries properly exclude legacy operational jobs by default—those identified by `source=JobSource.SUPERVISOR` and titles matching "Auto-deploy:"—and include them only when the `include_operational=True` flag is passed. They cover multiple query types (stage stats, slowest jobs, headline stats, agent stats, performance trend, deployment reliability) and verify edge cases like supervisor jobs without matching titles (which should be included as ordinary jobs) and projects containing only operational jobs. The changes ensure consistent filtering behavior across the analytics layer for distinguishing between user-initiated work and automated deployment operations.
These tests validate that the store's performance analytics queries properly exclude legacy operational jobs by default—those identified by `source=JobSource.SUPERVISOR` and titles matching "Auto-deploy:"—and include them only when the `include_operational=True` flag is passed. They cover multiple query types (stage stats, slowest jobs, headline stats, agent stats, performance trend, deployment reliability) and verify edge cases like supervisor jobs without matching titles (which should be included as ordinary jobs) and projects containing only operational jobs. The changes ensure consistent filtering behavior across the analytics layer for distinguishing between user-initiated work and automated deployment operations.

## Files touched
- tests/test_store.py
