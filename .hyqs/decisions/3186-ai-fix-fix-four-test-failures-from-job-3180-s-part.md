# Job #3186: [ai-fix] Fix four test failures from job #3180's partial build in hyqs/pipeline/

**Date:** 2026-08-02

This diff adds dependency provenance tracking and automatic minimization of redundant job dependencies in the pipeline scheduler. It introduces a `DependencyProvenance` enum to categorize dependencies as user-declared, semantically-required (from remediation), or auto-generated (from collision detection), then implements `minimize_auto_dependencies()` to remove redundant auto-generated edges while preserving ordering constraints needed for file conflict prevention. The database schema gains a `provenance` column, dependency operations acquire advisory locks to prevent race conditions, and the reconciliation pipeline now periodically minimizes auto-dependencies per project. This reduces unnecessary serialization of independent jobs while maintaining the transitive safety of the dependency graph.
This diff adds dependency provenance tracking and automatic minimization of redundant job dependencies in the pipeline scheduler. It introduces a `DependencyProvenance` enum to categorize dependencies as user-declared, semantically-required (from remediation), or auto-generated (from collision detection), then implements `minimize_auto_dependencies()` to remove redundant auto-generated edges while preserving ordering constraints needed for file conflict prevention. The database schema gains a `provenance` column, dependency operations acquire advisory locks to prevent race conditions, and the reconciliation pipeline now periodically minimizes auto-dependencies per project. This reduces unnecessary serialization of independent jobs while maintaining the transitive safety of the dependency graph.

## Files touched
- hyqs/pipeline/collision.py
- hyqs/pipeline/models.py
- hyqs/pipeline/runner.py
- hyqs/pipeline/store.py
- hyqs/pipeline/supervisor.py
- tests/test_collision.py
- tests/test_store.py
