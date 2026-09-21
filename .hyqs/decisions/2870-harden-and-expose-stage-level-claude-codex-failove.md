# Job #2870: Harden and expose stage-level Claude/Codex failover

**Date:** 2026-07-31

This diff makes provider failover atomic by consolidating the pause-and-requeue operation into a single database transaction (`record_provider_failover`) rather than separate pause and job-save calls, preventing stale workers from partially overwriting job state during ownership races. The `_pause` handler now routes exclusively through this atomic contract and tracks whether an alternate provider is immediately available to send contextual notifications (immediate failover vs. waiting for the pause to expire). A new `ProviderFailoverTransition` model captures the outcome of each failover attempt, including whether the operation actually applied and whether an alternate destination provider exists, with `_resolve_failover_destination` filling in the chosen destination provider as jobs are later claimed. The changes are covered by a comprehensive test suite exercising both mocked boundaries and real Postgres routing state.
This diff makes provider failover atomic by consolidating the pause-and-requeue operation into a single database transaction (`record_provider_failover`) rather than separate pause and job-save calls, preventing stale workers from partially overwriting job state during ownership races. The `_pause` handler now routes exclusively through this atomic contract and tracks whether an alternate provider is immediately available to send contextual notifications (immediate failover vs. waiting for the pause to expire). A new `ProviderFailoverTransition` model captures the outcome of each failover attempt, including whether the operation actually applied and whether an alternate destination provider exists, with `_resolve_failover_destination` filling in the chosen destination provider as jobs are later claimed. The changes are covered by a comprehensive test suite exercising both mocked boundaries and real Postgres routing state.

## Files touched
- hyqs/pipeline/models.py
- hyqs/pipeline/runner.py
- hyqs/pipeline/store.py
- tests/test_pipeline_runner.py
- tests/test_store.py
