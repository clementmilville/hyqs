# Job #3095: Apply safe Ruff fixes before lint verification

**Date:** 2026-08-02

This diff introduces an intermediate safe-fix stage to the linting pipeline that runs `ruff check --fix` (without `--unsafe-fixes`) between formatting and the final lint check. The feature is Python-only; JS/TS entries get an empty fix list. The return value changes from `formatted` to `auto_fixed` and includes `fix_commands` in the output, with an improved git-status fallback to detect mutations that don't print a "reformatted" marker (like ruff's import-only fixes). The lint stage now commits and force-pushes any auto-fixes, while the final lint check remains authoritative for pass/fail. Comprehensive tests verify command ordering, scoping to changed files, mutation detection, and proper commit/push behavior.
This diff introduces an intermediate safe-fix stage to the linting pipeline that runs `ruff check --fix` (without `--unsafe-fixes`) between formatting and the final lint check. The feature is Python-only; JS/TS entries get an empty fix list. The return value changes from `formatted` to `auto_fixed` and includes `fix_commands` in the output, with an improved git-status fallback to detect mutations that don't print a "reformatted" marker (like ruff's import-only fixes). The lint stage now commits and force-pushes any auto-fixes, while the final lint check remains authoritative for pass/fail. Comprehensive tests verify command ordering, scoping to changed files, mutation detection, and proper commit/push behavior.

## Files touched
- hyqs/pipeline/linting.py
- hyqs/pipeline/stages/lint.py
- tests/test_linting.py
