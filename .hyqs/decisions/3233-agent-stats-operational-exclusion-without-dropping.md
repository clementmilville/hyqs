# Job #3233: agent_stats operational exclusion without dropping agents

**Date:** 2026-08-02

This diff adds an optional toggle to the `agent_stats()` method to exclude auto-deploy operational jobs from agent cost and performance metrics by default. When `include_operational=False` (the new default), job events belonging to auto-deploy jobs are filtered out during the join so they don't contribute to total cost, average duration, or fix rate—but agents with only operational events still appear in results with sensible zero-filled defaults. The SQL filtering uses a `NOT EXISTS` clause rather than a WHERE filter to preserve the left-join semantics. Three tests verify the behavior: one confirms ordinary events are counted while operational ones are excluded, one checks that agents with only operational events get zero defaults, and one confirms that `include_operational=True` restores the unfiltered behavior.
This diff adds an optional toggle to the `agent_stats()` method to exclude auto-deploy operational jobs from agent cost and performance metrics by default. When `include_operational=False` (the new default), job events belonging to auto-deploy jobs are filtered out during the join so they don't contribute to total cost, average duration, or fix rate—but agents with only operational events still appear in results with sensible zero-filled defaults. The SQL filtering uses a `NOT EXISTS` clause rather than a WHERE filter to preserve the left-join semantics. Three tests verify the behavior: one confirms ordinary events are counted while operational ones are excluded, one checks that agents with only operational events get zero defaults, and one confirms that `include_operational=True` restores the unfiltered behavior.

## Files touched
- hyqs/pipeline/store.py
- tests/test_store.py
