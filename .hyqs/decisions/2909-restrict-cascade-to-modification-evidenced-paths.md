# Job #2909: Restrict cascade to modification-evidenced paths

**Date:** 2026-07-31

This diff refines the candidate filtering logic in hyqs' collision detection to distinguish between "reuse-only" discovery reasons and explicit path evidence. It extracts the reuse-only set (`explicit_symbol`, `indexed_caller`, `explicit_reference`) into a named constant and changes the filtering condition from an exact match on `explicit_reference` to a subset check — skipping cascading to tests, exports, and migrations only when candidates were discovered purely through symbol reuse, not when they have explicit path evidence from the commit message. The test split confirms this: symbol-only matches now stay isolated, while explicitly-mentioned paths still trigger cascading behavior. This makes the collision resolver more conservative about inferring related files unless the user explicitly named them.
This diff refines the candidate filtering logic in hyqs' collision detection to distinguish between "reuse-only" discovery reasons and explicit path evidence. It extracts the reuse-only set (`explicit_symbol`, `indexed_caller`, `explicit_reference`) into a named constant and changes the filtering condition from an exact match on `explicit_reference` to a subset check — skipping cascading to tests, exports, and migrations only when candidates were discovered purely through symbol reuse, not when they have explicit path evidence from the commit message. The test split confirms this: symbol-only matches now stay isolated, while explicitly-mentioned paths still trigger cascading behavior. This makes the collision resolver more conservative about inferring related files unless the user explicitly named them.

## Files touched
- hyqs/pipeline/collision.py
- tests/test_collision.py
