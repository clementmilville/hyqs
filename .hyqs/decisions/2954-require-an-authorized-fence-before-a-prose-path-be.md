# Job #2954: Require an authorized fence before a prose path becomes mandatory scope

**Date:** 2026-08-01

This diff adds support for inline `Files:` and `Target files:` labels in addition to existing heading-based fences to declare paths as mandatory (explicit_path) rather than advisory (explicit_reference). It refactors the candidate classification logic to combine both label types into a single `mandatory_paths` set, fixing a false positive where bare prose mentions of files—like in background sections—were incorrectly forced to be declared as edit targets. Two new tests lock in that bare mentions remain advisory unless explicitly labeled, and that heading-fenced paths remain mandatory.
This diff adds support for inline `Files:` and `Target files:` labels in addition to existing heading-based fences to declare paths as mandatory (explicit_path) rather than advisory (explicit_reference). It refactors the candidate classification logic to combine both label types into a single `mandatory_paths` set, fixing a false positive where bare prose mentions of files—like in background sections—were incorrectly forced to be declared as edit targets. Two new tests lock in that bare mentions remain advisory unless explicitly labeled, and that heading-fenced paths remain mandatory.

## Files touched
- hyqs/pipeline/collision.py
- tests/test_collision.py
