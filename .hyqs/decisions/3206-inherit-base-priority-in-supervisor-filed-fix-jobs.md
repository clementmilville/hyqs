# Job #3206: Inherit base priority in supervisor-filed fix jobs

**Date:** 2026-08-02

Supervisor jobs that remediate gate, deploy, and alembic failures now inherit their parent job's priority instead of using hardcoded values (100 for gate-fix, 50 for deploy-fix and alembic-merge-fix). This ensures fix jobs maintain priority parity with the work that spawned them, preventing high-priority jobs from being blocked by lower-priority remediation. The change removes "highest-priority" language from comments and test assertions to reflect the new behavior.
Supervisor jobs that remediate gate, deploy, and alembic failures now inherit their parent job's priority instead of using hardcoded values (100 for gate-fix, 50 for deploy-fix and alembic-merge-fix). This ensures fix jobs maintain priority parity with the work that spawned them, preventing high-priority jobs from being blocked by lower-priority remediation. The change removes "highest-priority" language from comments and test assertions to reflect the new behavior.

## Files touched
- hyqs/pipeline/supervisor.py
- tests/test_supervisor_gate_fix.py
