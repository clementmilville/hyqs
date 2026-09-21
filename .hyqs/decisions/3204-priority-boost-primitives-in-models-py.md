# Job #3204: Priority-boost primitives in models.py

**Date:** 2026-08-02

This introduces a priority-boost system for pipeline job scheduling that increases effective priority based on three factors: remediation jobs get a fixed boost, jobs with many unresolved dependents get a depth-based boost (capped at 30), and jobs waiting longer get an aging boost (capped at 15). The system returns both the final priority and a structured breakdown of which boosts were applied. Supporting this are new data structures like a PriorityBoost dataclass and a tuple of metadata keys that identify remediation jobs. A test suite validates the boost calculations, their individual and combined behavior, and the capping mechanisms.
This introduces a priority-boost system for pipeline job scheduling that increases effective priority based on three factors: remediation jobs get a fixed boost, jobs with many unresolved dependents get a depth-based boost (capped at 30), and jobs waiting longer get an aging boost (capped at 15). The system returns both the final priority and a structured breakdown of which boosts were applied. Supporting this are new data structures like a PriorityBoost dataclass and a tuple of metadata keys that identify remediation jobs. A test suite validates the boost calculations, their individual and combined behavior, and the capping mechanisms.

## Files touched
- hyqs/pipeline/models.py
- tests/test_models_priority_boost.py
