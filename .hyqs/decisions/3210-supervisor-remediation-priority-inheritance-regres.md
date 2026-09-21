# Job #3210: Supervisor remediation priority-inheritance regression coverage

**Date:** 2026-08-02

This diff adds a comprehensive regression test suite for supervisor priority inheritance, ensuring that when the pipeline creates remediation jobs for failed deployments, Alembic conflicts, or gate checks, the new jobs inherit the priority from the original failing job. The tests cover three supervisor remediation functions using mocked stores and temporary filesystem state, verifying that priority information is preserved across different failure scenarios. This prevents regressions where priority could be lost when creating follow-up jobs to fix pipeline failures.
This diff adds a comprehensive regression test suite for supervisor priority inheritance, ensuring that when the pipeline creates remediation jobs for failed deployments, Alembic conflicts, or gate checks, the new jobs inherit the priority from the original failing job. The tests cover three supervisor remediation functions using mocked stores and temporary filesystem state, verifying that priority information is preserved across different failure scenarios. This prevents regressions where priority could be lost when creating follow-up jobs to fix pipeline failures.

## Files touched
- tests/test_supervisor.py
